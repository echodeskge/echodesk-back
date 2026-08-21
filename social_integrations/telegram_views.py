"""Telegram (MTProto) integration views.

Login is a two-step flow using an EPHEMERAL Telethon client inside the
Django request (safe: the session is brand-new and unknown to the worker,
so there is no auth-key sharing). Once an account is persisted, ALL other
MTProto traffic — receiving updates and sending messages — goes through
the dedicated telegram-worker service; reusing a stored session from a
second process risks AuthKeyDuplicatedError, which permanently
invalidates the session.

Login state between /connect/ and /verify/ lives in the Django cache
(Redis) for 10 minutes, Fernet-encrypted: the code check must run on the
same DC/auth key that requested the code.
"""
import asyncio
import logging
import secrets

from django.conf import settings
from django.core.cache import cache
from django.db import connection as db_connection
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from crm.fields import _load_fernets

from .channels import http as social_http
from .models import ConversationArchive, ChatAssignment, TelegramAccount, TelegramMessage
from .pagination import SocialMessagePagination
from .permissions import (
    CanManageSocialConnections,
    CanSendSocialMessages,
    CanViewSocialMessages,
)
from .serializers import (
    TelegramAccountSerializer,
    TelegramConnectRequestSerializer,
    TelegramConnectResponseSerializer,
    TelegramDisconnectRequestSerializer,
    TelegramMessageSerializer,
    TelegramSendMessageResponseSerializer,
    TelegramSendMessageSerializer,
    TelegramStatusSerializer,
    TelegramVerifyRequestSerializer,
    TelegramVerifyResponseSerializer,
)

logger = logging.getLogger(__name__)

LOGIN_CACHE_TTL = 600  # seconds; Telegram codes expire well within this


def _api_credentials():
    cfg = getattr(settings, 'SOCIAL_INTEGRATIONS', {})
    api_id = cfg.get('TELEGRAM_API_ID')
    api_hash = cfg.get('TELEGRAM_API_HASH')
    if not api_id or not api_hash:
        return None, None
    return int(api_id), api_hash


def _login_cache_key(token):
    return f'tg-login:{token}'


def _worker_url(path):
    return f"{settings.TELEGRAM_WORKER_URL.rstrip('/')}{path}"


def _current_schema():
    return getattr(db_connection, 'schema_name', None)


def _nudge_worker(path, payload):
    """Best-effort control-plane call; the worker's reconcile loop is the fallback."""
    try:
        social_http.post(_worker_url(path), json=payload, timeout=10)
    except Exception as exc:  # noqa: BLE001 — never fail the request over a nudge
        logger.warning('telegram-worker nudge %s failed: %s', path, exc)


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run a Telethon coroutine from a sync view."""
    return asyncio.run(coro)


async def _send_code(phone):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id, api_hash = _api_credentials()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        return client.session.save(), sent.phone_code_hash
    finally:
        await client.disconnect()


async def _sign_in(session_string, phone, phone_code_hash, code, password):
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    api_id, api_hash = _api_credentials()
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        if password:
            await client.sign_in(password=password)
        else:
            try:
                await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                return {'status': 'password_required'}
        me = await client.get_me()
        return {
            'status': 'connected',
            'session': client.session.save(),
            'me': {
                'id': me.id,
                'first_name': me.first_name or '',
                'last_name': me.last_name or '',
                'username': me.username or '',
                'phone': f'+{me.phone}' if me.phone else phone,
            },
        }
    finally:
        # NEVER log_out() here — that would invalidate the session we are
        # about to persist. disconnect() only closes the socket.
        await client.disconnect()


@extend_schema(
    request=TelegramConnectRequestSerializer,
    responses={200: TelegramConnectResponseSerializer},
    description='Start Telegram login: sends an auth code to the phone number',
    summary='Telegram connect (send code)',
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def telegram_connect(request):
    serializer = TelegramConnectRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'error': 'Invalid data', 'details': serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST)

    api_id, api_hash = _api_credentials()
    if not api_id:
        return Response({'error': 'Telegram integration is not configured on this server'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)

    phone = serializer.validated_data['phone_number']

    from telethon.errors import FloodWaitError, PhoneNumberBannedError, PhoneNumberInvalidError

    try:
        session_string, phone_code_hash = _run_async(_send_code(phone))
    except PhoneNumberInvalidError:
        return Response({'error': 'Invalid phone number', 'error_code': 'phone_invalid'},
                        status=status.HTTP_400_BAD_REQUEST)
    except PhoneNumberBannedError:
        return Response({'error': 'This phone number is banned from Telegram', 'error_code': 'phone_banned'},
                        status=status.HTTP_400_BAD_REQUEST)
    except FloodWaitError as e:
        return Response({'error': 'Too many attempts, please wait', 'error_code': 'flood_wait',
                         'retry_after': e.seconds},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)
    except Exception as e:  # noqa: BLE001
        logger.error('Telegram send_code failed: %s', e)
        return Response({'error': f'Could not reach Telegram: {e}'},
                        status=status.HTTP_502_BAD_GATEWAY)

    token = secrets.token_urlsafe(32)
    fernet = _load_fernets()
    cache.set(_login_cache_key(token), {
        'session': fernet.encrypt(session_string.encode()).decode(),
        'phone': phone,
        'phone_code_hash': phone_code_hash,
    }, LOGIN_CACHE_TTL)

    return Response({'status': 'code_sent', 'login_token': token})


@extend_schema(
    request=TelegramVerifyRequestSerializer,
    responses={200: TelegramVerifyResponseSerializer},
    description='Complete Telegram login with the received code (and 2FA password if required)',
    summary='Telegram verify code',
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def telegram_verify(request):
    serializer = TelegramVerifyRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'error': 'Invalid data', 'details': serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST)

    token = serializer.validated_data['login_token']
    state = cache.get(_login_cache_key(token))
    if not state:
        return Response({'error': 'Login expired, please start over', 'error_code': 'login_expired'},
                        status=status.HTTP_410_GONE)

    fernet = _load_fernets()
    session_string = fernet.decrypt(state['session'].encode()).decode()

    from telethon.errors import (
        FloodWaitError,
        PasswordHashInvalidError,
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
    )

    try:
        result = _run_async(_sign_in(
            session_string,
            state['phone'],
            state['phone_code_hash'],
            serializer.validated_data.get('code', ''),
            serializer.validated_data.get('password', ''),
        ))
    except PhoneCodeInvalidError:
        return Response({'error': 'Invalid code', 'error_code': 'code_invalid'},
                        status=status.HTTP_400_BAD_REQUEST)
    except PhoneCodeExpiredError:
        cache.delete(_login_cache_key(token))
        return Response({'error': 'Code expired, please start over', 'error_code': 'code_expired'},
                        status=status.HTTP_410_GONE)
    except PasswordHashInvalidError:
        return Response({'error': 'Invalid 2FA password', 'error_code': 'password_invalid'},
                        status=status.HTTP_400_BAD_REQUEST)
    except FloodWaitError as e:
        return Response({'error': 'Too many attempts, please wait', 'error_code': 'flood_wait',
                         'retry_after': e.seconds},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)
    except Exception as e:  # noqa: BLE001
        logger.error('Telegram sign_in failed: %s', e)
        return Response({'error': f'Could not reach Telegram: {e}'},
                        status=status.HTTP_502_BAD_GATEWAY)

    if result['status'] == 'password_required':
        # Keep the login state alive for the password step.
        cache.set(_login_cache_key(token), state, LOGIN_CACHE_TTL)
        return Response({'status': 'password_required', 'account': None})

    me = result['me']
    account, _created = TelegramAccount.objects.update_or_create(
        telegram_user_id=me['id'],
        defaults={
            'phone_number': me['phone'],
            'first_name': me['first_name'],
            'last_name': me['last_name'],
            'username': me['username'],
            'session_string': result['session'],
            'is_active': True,
            'deactivated_at': None,
            'deactivation_reason': None,
            'deactivation_error': '',
            'auto_disabled_at': None,
            'failure_count': 0,
            'connected_by': request.user,
        },
    )
    cache.delete(_login_cache_key(token))

    _nudge_worker('/accounts/start', {
        'schema': _current_schema(),
        'account_id': account.id,
    })

    return Response({
        'status': 'connected',
        'account': TelegramAccountSerializer(account).data,
    })


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@extend_schema(
    responses={200: TelegramStatusSerializer},
    description='Telegram connection status for the current tenant',
    summary='Telegram status',
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def telegram_status(request):
    accounts = TelegramAccount.objects.all().order_by('-created_at')
    return Response({
        'connected': accounts.filter(is_active=True).exists(),
        'accounts': TelegramAccountSerializer(accounts, many=True).data,
    })


async def _log_out(session_string):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id, api_hash = _api_credentials()
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        await client.log_out()
    finally:
        await client.disconnect()


@extend_schema(
    request=TelegramDisconnectRequestSerializer,
    responses={200: TelegramStatusSerializer},
    description='Disconnect a Telegram account (revokes the session server-side)',
    summary='Telegram disconnect',
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def telegram_disconnect(request):
    serializer = TelegramDisconnectRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'error': 'Invalid data', 'details': serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        account = TelegramAccount.objects.get(id=serializer.validated_data['account_id'])
    except TelegramAccount.DoesNotExist:
        return Response({'error': 'Account not found'}, status=status.HTTP_404_NOT_FOUND)

    schema = _current_schema()

    # Stop the worker's client FIRST so log_out below doesn't race it.
    _nudge_worker('/accounts/stop', {'schema': schema, 'account_id': account.id})

    # Server-side revocation — clean break; ignore failures (session may
    # already be dead, which is fine).
    if account.session_string:
        try:
            _run_async(_log_out(account.session_string))
        except Exception as e:  # noqa: BLE001
            logger.warning('Telegram log_out failed for %s: %s', account.telegram_user_id, e)

    account.is_active = False
    account.deactivated_at = timezone.now()
    account.deactivation_reason = 'manual'
    account.save(update_fields=['is_active', 'deactivated_at', 'deactivation_reason'])

    from .platform_routing import route_deactivate
    route_deactivate('telegram', str(account.telegram_user_id))

    accounts = TelegramAccount.objects.all().order_by('-created_at')
    return Response({
        'connected': accounts.filter(is_active=True).exists(),
        'accounts': TelegramAccountSerializer(accounts, many=True).data,
    })


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------


@extend_schema(
    request=TelegramSendMessageSerializer,
    responses={200: TelegramSendMessageResponseSerializer},
    description='Send a Telegram message (text and/or media) to a user',
    summary='Send Telegram Message',
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanSendSocialMessages])
def telegram_send_message(request):
    from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
    request.parsers = [MultiPartParser(), FormParser(), JSONParser()]

    serializer = TelegramSendMessageSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'error': 'Invalid data', 'details': serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    message_text = data.get('message', '')
    media_file = request.FILES.get('media')

    if not message_text and not media_file:
        return Response({'error': 'Message text or media file is required'},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        account = TelegramAccount.objects.get(telegram_user_id=data['account_id'], is_active=True)
    except (TelegramAccount.DoesNotExist, ValueError):
        return Response({'error': 'Telegram account not found or not connected'},
                        status=status.HTTP_404_NOT_FOUND)

    # A conversation must exist (we only reply to inbound conversations in
    # v1 — that also guarantees a stored peer access_hash for the worker).
    peer_id = data['peer_id']
    if not TelegramMessage.objects.filter(account=account, peer_id=peer_id).exists():
        return Response({
            'error': 'No conversation found with this user. The user must message you first.',
            'error_code': 'no_conversation',
        }, status=status.HTTP_400_BAD_REQUEST)

    schema = _current_schema()

    # Persist uploaded media so the worker can read it from shared storage.
    media_payload = None
    if media_file:
        from django.core.files.storage import default_storage
        safe_name = media_file.name.replace('/', '_')[:100]
        path = f"telegram_media/{schema}/{account.telegram_user_id}/out_{secrets.token_hex(6)}_{safe_name}"
        saved_path = default_storage.save(path, media_file)
        media_payload = {
            'storage_path': saved_path,
            'mime': media_file.content_type or 'application/octet-stream',
            'filename': media_file.name,
        }

    try:
        worker_resp = social_http.post(_worker_url('/send'), json={
            'schema': schema,
            'account_id': account.id,
            'peer_id': int(peer_id),
            'text': message_text,
            'reply_to_message_id': data.get('reply_to_message_id') or None,
            'media': media_payload,
            'sent_by_id': request.user.id,
        }, timeout=60)
    except Exception as e:  # noqa: BLE001
        logger.error('telegram-worker send failed: %s', e)
        return Response({'error': 'Telegram service unavailable, please try again',
                         'error_code': 'worker_unavailable'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)

    if worker_resp.status_code != 200:
        try:
            detail = worker_resp.json()
        except ValueError:
            detail = {'error': worker_resp.text[:300]}
        return Response(detail, status=worker_resp.status_code)

    sent = worker_resp.json()

    # Idempotent post-send extras (mirrors whatsapp_send_message):
    ConversationArchive.objects.filter(
        platform='telegram',
        conversation_id=str(peer_id),
        account_id=str(account.telegram_user_id),
    ).delete()
    ChatAssignment.objects.filter(
        platform='telegram',
        conversation_id=str(peer_id),
        account_id=str(account.telegram_user_id),
        status='completed',
    ).delete()

    return Response({'success': True, 'message': sent.get('message')})


class TelegramMessageViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only Telegram message listing (mirrors WhatsAppMessageViewSet)."""

    serializer_class = TelegramMessageSerializer
    permission_classes = [IsAuthenticated, CanViewSocialMessages]
    pagination_class = SocialMessagePagination

    def get_queryset(self):
        queryset = TelegramMessage.objects.filter(is_deleted=False).select_related(
            'account', 'sent_by', 'reply_to',
        )

        account_id = self.request.query_params.get('account_id')
        if account_id:
            queryset = queryset.filter(account__telegram_user_id=account_id)

        peer_id = self.request.query_params.get('peer_id')
        if peer_id:
            queryset = queryset.filter(peer_id=peer_id)

        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(message_text__icontains=search)

        return queryset.order_by('-timestamp')
