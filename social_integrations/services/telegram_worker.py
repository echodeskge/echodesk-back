"""Telegram MTProto worker.

Runs as the dedicated `telegram-worker` service: one asyncio loop hosting
a supervised Telethon client per connected TelegramAccount across ALL
tenants, plus an aiohttp server that doubles as the send API, the control
plane (accounts/start|stop), and the compose healthcheck.

Design notes (see the plan doc for the full rationale):
- All ORM access goes through `db(schema, fn, *args)` — a bounded thread
  executor where `schema_context` is entered INSIDE the worker thread
  (it is thread-local).
- Sends are HTTP-only: Django never opens a Telethon client for a stored
  session (AuthKeyDuplicatedError would permanently kill the session).
- The worker creates the outgoing TelegramMessage row and broadcasts the
  WS frame itself, so its own `event.out` echo can be deduped via the
  in-flight set + unique message_id.
- Terminal auth errors auto-disable the account; a dead session is never
  retried.
"""
import asyncio
import logging
import mimetypes
import random
import signal
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

from asgiref.sync import sync_to_async

logger = logging.getLogger('telegram_worker')

WORKER_PORT = 8010
RECONCILE_INTERVAL = 300  # seconds
HEARTBEAT_INTERVAL = 15
HEARTBEAT_STALE_AFTER = 60
MEDIA_MAX_BYTES = 50 * 1024 * 1024
SEND_RATE_PER_MINUTE = 30  # soft per-account outbound cap (ban-risk guard)

_EXECUTOR = ThreadPoolExecutor(max_workers=8)


def _db_call(schema, fn, *args, **kwargs):
    from django.db import close_old_connections
    from tenant_schemas.utils import schema_context

    close_old_connections()
    with schema_context(schema):
        return fn(*args, **kwargs)


db = sync_to_async(_db_call, thread_sensitive=False, executor=_EXECUTOR)


# ---------------------------------------------------------------------------
# Sync ORM helpers (always called through `db`)
# ---------------------------------------------------------------------------


def _load_account(account_id):
    from social_integrations.models import TelegramAccount

    return TelegramAccount.objects.filter(id=account_id).first()


def _scan_accounts():
    """Return [(schema, account_id), ...] for every active account."""
    from tenant_schemas.utils import schema_context
    from tenants.models import Tenant

    found = []
    for tenant in Tenant.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                from social_integrations.models import TelegramAccount

                for acc_id in TelegramAccount.objects.filter(
                    is_active=True,
                ).values_list('id', flat=True):
                    found.append((tenant.schema_name, acc_id))
        except Exception:  # noqa: BLE001 — one broken tenant must not stop the scan
            logger.exception('account scan failed for tenant %s', tenant.schema_name)
    return found


def _scan_accounts_entry():
    """Executor entrypoint for the full scan (runs outside any schema)."""
    from django.db import close_old_connections

    close_old_connections()
    return _scan_accounts()


def _disable_account(account_id, reason, error_text):
    from django.utils import timezone

    from social_integrations.models import TelegramAccount
    from social_integrations.platform_routing import route_deactivate

    account = TelegramAccount.objects.filter(id=account_id).first()
    if not account:
        return
    account.is_active = False
    account.auto_disabled_at = timezone.now()
    account.deactivated_at = timezone.now()
    account.deactivation_reason = reason
    account.deactivation_error = (error_text or '')[:2000]
    account.failure_count += 1
    account.save(update_fields=[
        'is_active', 'auto_disabled_at', 'deactivated_at',
        'deactivation_reason', 'deactivation_error', 'failure_count',
    ])
    route_deactivate('telegram', str(account.telegram_user_id))
    logger.error('Telegram account %s auto-disabled: %s', account.telegram_user_id, reason)

    # Notify tenant admins (best-effort, mirrors email auto-disable).
    try:
        from django.contrib.auth import get_user_model

        from users.notification_utils import create_notification

        User = get_user_model()
        label = account.username or account.phone_number
        for admin in User.objects.filter(is_active=True, is_staff=True):
            create_notification(
                user=admin,
                notification_type='system',
                title='Telegram account disconnected',
                message=(
                    f'The Telegram account {label} was disconnected '
                    f'({reason}). Reconnect it from Settings → Social → Telegram.'
                ),
            )
    except Exception:  # noqa: BLE001
        logger.exception('failed to notify admins about disabled telegram account')


def _touch_heartbeat(account_id):
    from django.utils import timezone

    from social_integrations.models import TelegramAccount

    TelegramAccount.objects.filter(id=account_id).update(last_seen_at=timezone.now())


def _guess_message_type(mime, is_voice):
    if is_voice:
        return 'voice'
    if not mime:
        return 'document'
    if mime.startswith('image/'):
        return 'image'
    if mime.startswith('video/'):
        return 'video'
    if mime.startswith('audio/'):
        return 'audio'
    return 'document'


def _serialize_message(msg):
    from social_integrations.serializers import TelegramMessageSerializer

    return TelegramMessageSerializer(msg).data


def _persist_incoming(account_id, data):
    """Create the message row + run the standard inbound hooks.

    `data` is a plain dict extracted from the Telethon event (no Telethon
    objects cross the thread boundary). Returns (created, serialized, extras)
    where extras carries what the async side needs for the WS frame.
    """
    from social_integrations.models import TelegramAccount, TelegramMessage

    account = TelegramAccount.objects.filter(id=account_id).first()
    if account is None:
        return False, None, None

    message_id = f"{account.telegram_user_id}_{data['telegram_msg_id']}"
    msg, created = TelegramMessage.objects.get_or_create(
        message_id=message_id,
        defaults={
            'account': account,
            'telegram_msg_id': data['telegram_msg_id'],
            'peer_id': data['peer_id'],
            'peer_access_hash': data.get('peer_access_hash'),
            'peer_name': data.get('peer_name', ''),
            'peer_username': data.get('peer_username', ''),
            'message_text': data.get('text', ''),
            'message_type': data.get('message_type', 'text'),
            'media_url': data.get('media_url'),
            'media_mime_type': data.get('media_mime_type', ''),
            'attachments': data.get('attachments', []),
            'timestamp': data['timestamp'],
            'is_from_business': data['is_from_business'],
            'status': 'delivered',
            'is_delivered': True,
            'source': data.get('source', 'telegram_app'),
            'is_echo': data.get('is_echo', False),
            'reply_to_message_id': data.get('reply_to_message_id'),
            'reply_to': (
                TelegramMessage.objects.filter(
                    message_id=data['reply_to_message_id']
                ).first()
                if data.get('reply_to_message_id') else None
            ),
        },
    )
    if not created:
        return False, _serialize_message(msg), None

    extras = {'assigned_user_id': None}
    if not data['is_from_business']:
        from social_integrations import views as social_views

        account_key = str(account.telegram_user_id)
        peer_key = str(data['peer_id'])
        try:
            social_views.process_potential_rating_response(
                message_text=data.get('text', ''),
                platform='telegram',
                conversation_id=peer_key,
                account_id=account_key,
                message_id=message_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception('rating hook failed')
        try:
            social_views.auto_unarchive_conversation(
                platform='telegram', conversation_id=peer_key, account_id=account_key,
            )
        except Exception:  # noqa: BLE001
            logger.exception('auto-unarchive failed')
        try:
            social_views.process_auto_reply(
                platform='telegram',
                account_id=account_key,
                conversation_id=peer_key,
                sender_name=data.get('peer_name') or data.get('peer_username') or peer_key,
                connection=account,
            )
        except Exception:  # noqa: BLE001
            logger.exception('auto-reply hook failed')
        try:
            from social_integrations.services import ai_companion

            ai_companion.schedule_ai_reply('telegram', account_key, peer_key)
        except Exception:  # noqa: BLE001
            logger.exception('ai companion hook failed')
        try:
            extras['assigned_user_id'] = social_views.get_assignment_for_conversation(
                platform='telegram', conversation_id=peer_key, account_id=account_key,
            )
        except Exception:  # noqa: BLE001
            logger.exception('assignment lookup failed')
        try:
            from users.notification_utils import create_social_message_notification

            create_social_message_notification(
                platform='Telegram',
                sender_name=data.get('peer_name') or peer_key,
                message_text=data.get('text', ''),
                conversation_id=peer_key,
                sender_id=peer_key,
                account_id=account_key,
                assigned_user_id=extras['assigned_user_id'],
            )
        except Exception:  # noqa: BLE001
            logger.exception('notification creation failed')

    return created, _serialize_message(msg), extras


def _persist_outgoing(account_id, data):
    """Create the row for a message we just sent via /send."""
    from social_integrations.models import TelegramAccount, TelegramMessage

    account = TelegramAccount.objects.get(id=account_id)
    message_id = f"{account.telegram_user_id}_{data['telegram_msg_id']}"
    msg, _created = TelegramMessage.objects.get_or_create(
        message_id=message_id,
        defaults={
            'account': account,
            'telegram_msg_id': data['telegram_msg_id'],
            'peer_id': data['peer_id'],
            'peer_access_hash': data.get('peer_access_hash'),
            'peer_name': data.get('peer_name', ''),
            'peer_username': data.get('peer_username', ''),
            'message_text': data.get('text', ''),
            'message_type': data.get('message_type', 'text'),
            'media_url': data.get('media_url'),
            'media_mime_type': data.get('media_mime_type', ''),
            'attachments': data.get('attachments', []),
            'timestamp': data['timestamp'],
            'is_from_business': True,
            'status': 'sent',
            'source': 'echodesk',
            'sent_by_id': data.get('sent_by_id'),
            'reply_to_message_id': data.get('reply_to_message_id'),
            'reply_to': (
                TelegramMessage.objects.filter(
                    message_id=data['reply_to_message_id']
                ).first()
                if data.get('reply_to_message_id') else None
            ),
        },
    )
    return _serialize_message(msg)


def _lookup_peer_hash(account_id, peer_id):
    from social_integrations.models import TelegramMessage

    return (
        TelegramMessage.objects
        .filter(account_id=account_id, peer_id=peer_id, peer_access_hash__isnull=False)
        .order_by('-timestamp')
        .values_list('peer_access_hash', flat=True)
        .first()
    )


def _apply_edit(account_id, telegram_msg_id, new_text):
    from django.utils import timezone

    from social_integrations.models import TelegramAccount, TelegramMessage

    account = TelegramAccount.objects.filter(id=account_id).first()
    if not account:
        return None
    msg = TelegramMessage.objects.filter(
        message_id=f"{account.telegram_user_id}_{telegram_msg_id}"
    ).first()
    if not msg or msg.message_text == new_text:
        return None
    if not msg.is_edited:
        msg.original_text = msg.message_text
    msg.message_text = new_text
    msg.is_edited = True
    msg.edited_at = timezone.now()
    msg.save(update_fields=['message_text', 'original_text', 'is_edited', 'edited_at'])
    return {'peer_id': msg.peer_id, 'serialized': _serialize_message(msg)}


def _apply_deletes(account_id, telegram_msg_ids):
    from django.utils import timezone

    from social_integrations.models import TelegramAccount, TelegramMessage

    account = TelegramAccount.objects.filter(id=account_id).first()
    if not account:
        return []
    ids = [f"{account.telegram_user_id}_{i}" for i in telegram_msg_ids]
    revoked = list(
        TelegramMessage.objects.filter(message_id__in=ids, is_revoked=False)
        .values_list('message_id', 'peer_id')
    )
    TelegramMessage.objects.filter(message_id__in=ids, is_revoked=False).update(
        is_revoked=True, revoked_at=timezone.now(),
    )
    return revoked


def _save_media(schema_path, content, mime):
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    saved = default_storage.save(schema_path, ContentFile(content))
    return default_storage.url(saved)


def _read_media(storage_path):
    from django.core.files.storage import default_storage

    with default_storage.open(storage_path, 'rb') as fh:
        return fh.read()


async def _broadcast_new_message(schema, account_key, peer_id, serialized, assigned_user_id):
    from channels.layers import get_channel_layer
    from django.utils import timezone as dj_tz

    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    payload = dict(serialized)
    payload['platform'] = 'telegram'
    payload['account_id'] = account_key
    payload['telegram_account_id'] = account_key
    payload['conversation_id'] = str(peer_id)
    payload['sender_id'] = str(peer_id)
    payload['sender_name'] = serialized.get('peer_name') or serialized.get('peer_username') or str(peer_id)
    # The serializer's sent_by is the raw user PK; the WS convention across
    # platforms carries the display NAME in sent_by (frameToMessage prefers
    # it) — leaking the pk renders a stray "1" as the author badge.
    payload['sent_by'] = serialized.get('sent_by_name')
    try:
        await channel_layer.group_send(f'messages_{schema}', {
            'type': 'new_message',
            'message': payload,
            'conversation_id': str(peer_id),
            'timestamp': dj_tz.now().isoformat(),
            'assigned_user_id': assigned_user_id,
        })
    except Exception:  # noqa: BLE001
        logger.exception('WS broadcast failed for %s', schema)


# ---------------------------------------------------------------------------
# Per-account client management
# ---------------------------------------------------------------------------


class ManagedClient:
    def __init__(self, app, schema, account_id):
        self.app = app
        self.schema = schema
        self.account_id = account_id
        self.client = None
        self.task = None
        self.in_flight = set()  # telegram_msg_ids we sent via /send
        self.send_times = []  # rate-limit window
        self.account_key = None  # str(telegram_user_id)
        self.stopping = False

    @property
    def key(self):
        return (self.schema, self.account_id)

    async def supervise(self):
        from telethon import TelegramClient, events
        from telethon.errors import (
            AuthKeyDuplicatedError,
            AuthKeyUnregisteredError,
            FloodWaitError,
            SessionRevokedError,
            UserDeactivatedError,
        )
        from telethon.sessions import StringSession

        backoff = 5
        while not self.stopping:
            client = None
            try:
                account = await db(self.schema, _load_account, self.account_id)
                if account is None or not account.is_active:
                    logger.info('account %s/%s inactive — stopping supervisor', self.schema, self.account_id)
                    return
                self.account_key = str(account.telegram_user_id)

                client = TelegramClient(
                    StringSession(account.session_string),
                    self.app.api_id, self.app.api_hash,
                    catch_up=True, flood_sleep_threshold=60,
                )
                client.add_event_handler(self._on_new_message, events.NewMessage())
                client.add_event_handler(self._on_edited, events.MessageEdited())
                client.add_event_handler(self._on_deleted, events.MessageDeleted())

                await client.connect()
                if not await client.is_user_authorized():
                    raise AuthKeyUnregisteredError(request=None)

                self.client = client
                backoff = 5
                logger.info('telegram client up: %s/%s (@%s)', self.schema, self.account_id, self.account_key)
                await db(self.schema, _touch_heartbeat, self.account_id)

                heartbeat = asyncio.ensure_future(self._heartbeat_loop())
                try:
                    await client.disconnected
                finally:
                    heartbeat.cancel()
                if self.stopping:
                    return
                logger.warning('telegram client disconnected: %s/%s — reconnecting', self.schema, self.account_id)
            except (AuthKeyUnregisteredError, SessionRevokedError,
                    AuthKeyDuplicatedError, UserDeactivatedError) as e:
                await db(self.schema, _disable_account, self.account_id,
                         'session_revoked', str(e))
                self.app.registry.pop(self.key, None)
                return
            except FloodWaitError as e:
                logger.warning('FloodWait %ss for %s/%s', e.seconds, self.schema, self.account_id)
                await asyncio.sleep(e.seconds + 5)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception('client error %s/%s — retry in %ss', self.schema, self.account_id, backoff)
                await asyncio.sleep(backoff + random.uniform(0, backoff / 2))
                backoff = min(backoff * 2, 600)
            finally:
                self.client = None
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL * 4)
            try:
                await db(self.schema, _touch_heartbeat, self.account_id)
            except Exception:  # noqa: BLE001
                logger.exception('heartbeat failed for %s/%s', self.schema, self.account_id)

    # -- event handlers ------------------------------------------------

    async def _on_new_message(self, event):
        try:
            if not event.is_private:
                return
            message = event.message
            if message is None or getattr(message, 'action', None) is not None:
                return  # service message
            if event.out and message.id in self.in_flight:
                self.in_flight.discard(message.id)
                return  # our own /send echo — row already created

            peer_id = event.chat_id
            sender = await event.get_sender() if not event.out else await event.get_chat()
            access_hash = getattr(sender, 'access_hash', None)
            peer_name = ' '.join(filter(None, [
                getattr(sender, 'first_name', '') or '',
                getattr(sender, 'last_name', '') or '',
            ])).strip()
            peer_username = getattr(sender, 'username', '') or ''

            media_url = None
            media_mime = ''
            attachments = []
            message_type = 'text'
            if message.media is not None and not getattr(message, 'web_preview', None):
                media_url, media_mime, message_type, attachments = await self._download_media(message)

            reply_to_mid = None
            if message.reply_to and getattr(message.reply_to, 'reply_to_msg_id', None):
                reply_to_mid = f"{self.account_key}_{message.reply_to.reply_to_msg_id}"

            data = {
                'telegram_msg_id': message.id,
                'peer_id': peer_id,
                'peer_access_hash': access_hash,
                'peer_name': peer_name,
                'peer_username': peer_username,
                'text': message.message or '',
                'message_type': message_type,
                'media_url': media_url,
                'media_mime_type': media_mime,
                'attachments': attachments,
                'timestamp': message.date,
                'is_from_business': bool(event.out),
                'source': 'telegram_app',
                'is_echo': bool(event.out),
                'reply_to_message_id': reply_to_mid,
            }

            created, serialized, extras = await self._db_retry(_persist_incoming, self.account_id, data)
            if created and serialized:
                await _broadcast_new_message(
                    self.schema, self.account_key, peer_id, serialized,
                    (extras or {}).get('assigned_user_id'),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception('new-message handler failed for %s/%s', self.schema, self.account_id)

    async def _on_edited(self, event):
        try:
            if not event.is_private or event.message is None:
                return
            result = await self._db_retry(
                _apply_edit, self.account_id, event.message.id, event.message.message or '',
            )
            if result:
                from channels.layers import get_channel_layer

                channel_layer = get_channel_layer()
                if channel_layer:
                    await channel_layer.group_send(f'messages_{self.schema}', {
                        'type': 'conversation_update',
                        'platform': 'telegram',
                        'account_id': self.account_key,
                        'conversation_id': str(result['peer_id']),
                        'message': result['serialized'],
                    })
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception('edit handler failed')

    async def _on_deleted(self, event):
        try:
            if not event.deleted_ids:
                return
            revoked = await self._db_retry(_apply_deletes, self.account_id, list(event.deleted_ids))
            if revoked:
                from channels.layers import get_channel_layer

                channel_layer = get_channel_layer()
                if channel_layer:
                    for message_id, peer_id in revoked:
                        await channel_layer.group_send(f'messages_{self.schema}', {
                            'type': 'message_status_update',
                            'platform': 'telegram',
                            'conversation_id': str(peer_id),
                            'account_id': self.account_key,
                            'message_ids': [message_id],
                            'status': 'revoked',
                        })
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception('delete handler failed')

    async def _download_media(self, message):
        """Download message media to default_storage. Returns (url, mime, type, attachments)."""
        size = getattr(getattr(message, 'file', None), 'size', 0) or 0
        mime = getattr(getattr(message, 'file', None), 'mime_type', '') or ''
        is_voice = bool(getattr(getattr(message, 'file', None), 'voice', False)) or (
            mime == 'audio/ogg'
        )
        message_type = _guess_message_type(mime, is_voice)

        if size > MEDIA_MAX_BYTES:
            return None, mime, message_type, [{
                'type': message_type, 'too_large': True, 'size': size,
            }]

        buf = BytesIO()
        await self.client.download_media(message, buf)
        content = buf.getvalue()
        if not content:
            return None, mime, message_type, []

        name = getattr(getattr(message, 'file', None), 'name', None)
        if not name:
            ext = mimetypes.guess_extension(mime) or '.bin'
            name = f'media{ext}'
        safe_name = name.replace('/', '_')[:100]
        path = f"telegram_media/{self.schema}/{self.account_key}/{message.id}_{safe_name}"
        url = await db(self.schema, _save_media, path, content, mime)
        attachments = [{'type': message_type, 'url': url, 'filename': safe_name, 'mime_type': mime}]
        return url, mime, message_type, attachments

    async def _db_retry(self, fn, *args, attempts=3):
        last_exc = None
        for i in range(attempts):
            try:
                return await db(self.schema, fn, *args)
            except Exception as e:  # noqa: BLE001
                last_exc = e
                await asyncio.sleep(1 + i * 2)
        logger.error('DB op %s failed after %s attempts: %s', fn.__name__, attempts, last_exc)
        raise last_exc

    # -- send ----------------------------------------------------------

    def _rate_limited(self):
        import time

        now = time.monotonic()
        self.send_times = [t for t in self.send_times if now - t < 60]
        if len(self.send_times) >= SEND_RATE_PER_MINUTE:
            return True
        self.send_times.append(now)
        return False

    async def send(self, payload):
        """Send text/media to a peer. Returns (http_status, body_dict)."""
        from telethon.errors import FloodWaitError
        from telethon.tl.types import InputPeerUser

        if self.client is None or not self.client.is_connected():
            return 409, {'error': 'Telegram client not connected', 'error_code': 'client_down'}
        if self._rate_limited():
            return 429, {'error': 'Send rate limit reached for this account',
                         'error_code': 'rate_limited', 'retry_after': 60}

        peer_id = payload['peer_id']
        access_hash = await db(self.schema, _lookup_peer_hash, self.account_id, peer_id)
        if access_hash is None:
            return 422, {'error': 'Unknown peer — the user must message this account first',
                         'error_code': 'peer_unknown'}
        peer = InputPeerUser(peer_id, access_hash)

        reply_to = None
        raw_reply = payload.get('reply_to_message_id')
        if raw_reply:
            # Stored ids are '{account}_{msg_id}'; Telethon wants the raw int.
            try:
                reply_to = int(str(raw_reply).rsplit('_', 1)[-1])
            except ValueError:
                reply_to = None

        media = payload.get('media')
        try:
            if media:
                content = await db(self.schema, _read_media, media['storage_path'])
                buf = BytesIO(content)
                buf.name = media.get('filename') or 'file'
                mime = media.get('mime', '')
                is_voice = mime == 'audio/ogg'
                sent = await self.client.send_file(
                    peer, buf,
                    caption=payload.get('text') or '',
                    reply_to=reply_to,
                    voice_note=is_voice,
                    force_document=not (
                        mime.startswith('image/') or mime.startswith('video/') or is_voice
                    ),
                )
            else:
                sent = await self.client.send_message(
                    peer, payload.get('text') or '', reply_to=reply_to,
                )
        except FloodWaitError as e:
            return 429, {'error': 'Telegram rate limit', 'error_code': 'flood_wait',
                         'retry_after': e.seconds}
        except Exception as e:  # noqa: BLE001
            logger.exception('send failed for %s/%s', self.schema, self.account_id)
            return 502, {'error': f'Telegram error: {e}', 'error_code': 'rpc_error'}

        self.in_flight.add(sent.id)

        media_url = None
        media_mime = ''
        attachments = []
        message_type = 'text'
        if media:
            from django.core.files.storage import default_storage as _ds  # noqa: F401

            media_mime = media.get('mime', '')
            message_type = _guess_message_type(media_mime, media_mime == 'audio/ogg')
            media_url = await db(self.schema, _storage_url, media['storage_path'])
            attachments = [{
                'type': message_type, 'url': media_url,
                'filename': media.get('filename', ''), 'mime_type': media_mime,
            }]

        serialized = await db(self.schema, _persist_outgoing, self.account_id, {
            'telegram_msg_id': sent.id,
            'peer_id': peer_id,
            'peer_access_hash': access_hash,
            'text': payload.get('text', ''),
            'message_type': message_type,
            'media_url': media_url,
            'media_mime_type': media_mime,
            'attachments': attachments,
            'timestamp': sent.date,
            'sent_by_id': payload.get('sent_by_id'),
            'reply_to_message_id': payload.get('reply_to_message_id') or None,
        })
        await _broadcast_new_message(self.schema, self.account_key, peer_id, serialized, None)
        return 200, {'success': True, 'message': serialized}


def _storage_url(storage_path):
    from django.core.files.storage import default_storage

    return default_storage.url(storage_path)


# ---------------------------------------------------------------------------
# WorkerApp: loop, control plane, reconcile
# ---------------------------------------------------------------------------


class WorkerApp:
    def __init__(self):
        from django.conf import settings

        cfg = getattr(settings, 'SOCIAL_INTEGRATIONS', {})
        self.api_id = int(cfg.get('TELEGRAM_API_ID') or 0)
        self.api_hash = cfg.get('TELEGRAM_API_HASH') or ''
        self.registry = {}  # (schema, account_id) -> ManagedClient
        self.shutting_down = False
        self.last_beat = None

    # -- lifecycle -----------------------------------------------------

    async def run(self):
        import time

        if not self.api_id or not self.api_hash:
            logger.error('TELEGRAM_API_ID / TELEGRAM_API_HASH not configured — worker idle')
        self.last_beat = time.monotonic()

        runner = await self._start_http()
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        heartbeat = asyncio.ensure_future(self._beat_loop())
        reconcile = asyncio.ensure_future(self._reconcile_loop())

        await stop_event.wait()
        logger.info('shutdown requested — draining')
        self.shutting_down = True
        heartbeat.cancel()
        reconcile.cancel()

        # Stop accepting HTTP, then disconnect all clients cleanly.
        await runner.cleanup()
        await asyncio.wait_for(
            asyncio.gather(*[self._stop_client(mc) for mc in list(self.registry.values())],
                           return_exceptions=True),
            timeout=15,
        )
        logger.info('shutdown complete')

    async def _beat_loop(self):
        import time

        while True:
            self.last_beat = time.monotonic()
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _reconcile_loop(self):
        while True:
            try:
                await self.reconcile()
            except Exception:  # noqa: BLE001
                logger.exception('reconcile failed')
            await asyncio.sleep(RECONCILE_INTERVAL)

    async def reconcile(self):
        wanted = set(await sync_to_async(
            _scan_accounts_entry, thread_sensitive=False, executor=_EXECUTOR,
        )())
        current = set(self.registry.keys())
        for key in wanted - current:
            self.start_client(*key)
        for key in current - wanted:
            await self._stop_client(self.registry[key])
        if wanted:
            logger.info('reconcile: %d accounts (%d started, %d stopped)',
                        len(wanted), len(wanted - current), len(current - wanted))

    def start_client(self, schema, account_id):
        key = (schema, account_id)
        if key in self.registry:
            return
        mc = ManagedClient(self, schema, account_id)
        mc.task = asyncio.ensure_future(mc.supervise())
        self.registry[key] = mc
        logger.info('client scheduled: %s/%s', schema, account_id)

    async def _stop_client(self, mc):
        mc.stopping = True
        self.registry.pop(mc.key, None)
        if mc.task:
            mc.task.cancel()
            try:
                await mc.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if mc.client is not None:
            try:
                await mc.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        logger.info('client stopped: %s/%s', mc.schema, mc.account_id)

    # -- HTTP ----------------------------------------------------------

    async def _start_http(self):
        from aiohttp import web

        app = web.Application(client_max_size=MEDIA_MAX_BYTES + 1024 * 1024)
        app.router.add_get('/health', self._h_health)
        app.router.add_post('/send', self._h_send)
        app.router.add_post('/accounts/start', self._h_account_start)
        app.router.add_post('/accounts/stop', self._h_account_stop)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', WORKER_PORT)
        await site.start()
        logger.info('control/send API listening on :%d', WORKER_PORT)
        return runner

    async def _h_health(self, request):
        import time

        from aiohttp import web

        stale = self.last_beat is None or (time.monotonic() - self.last_beat) > HEARTBEAT_STALE_AFTER
        connected = sum(
            1 for mc in self.registry.values()
            if mc.client is not None and mc.client.is_connected()
        )
        body = {'accounts': len(self.registry), 'connected': connected}
        return web.json_response(body, status=500 if stale else 200)

    async def _h_send(self, request):
        from aiohttp import web

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({'error': 'invalid JSON'}, status=400)

        schema = payload.get('schema')
        account_id = payload.get('account_id')
        if not schema or not account_id:
            return web.json_response({'error': 'schema and account_id required'}, status=400)

        mc = self.registry.get((schema, account_id))
        if mc is None:
            # Maybe connected moments ago — try to start it, then fail fast.
            self.start_client(schema, account_id)
            return web.json_response(
                {'error': 'Account not ready yet, retry shortly', 'error_code': 'account_starting'},
                status=409,
            )
        status_code, body = await mc.send(payload)
        return web.json_response(body, status=status_code)

    async def _h_account_start(self, request):
        from aiohttp import web

        payload = await request.json()
        self.start_client(payload['schema'], payload['account_id'])
        return web.json_response({'ok': True})

    async def _h_account_stop(self, request):
        from aiohttp import web

        payload = await request.json()
        mc = self.registry.get((payload['schema'], payload['account_id']))
        if mc is not None:
            await self._stop_client(mc)
        return web.json_response({'ok': True})
