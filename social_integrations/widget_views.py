"""HTTP views for the embeddable chat widget.

Public endpoints (AllowAny, rate-limited) accept a widget token in the
request body or query string; the token resolves the tenant schema so
visitors never need to know the tenant's subdomain. Authenticated
endpoints are for tenant admins/agents to manage connections and read
conversation transcripts.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.utils import timezone
from django_ratelimit.decorators import ratelimit
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from tenant_schemas.utils import schema_context

from widget_registry.models import WidgetConnection
from .models import ChatRating, ConversationArchive, WidgetMessage, WidgetSession
from .widget_serializers import (
    WidgetConnectionSerializer,
    WidgetMessageSerializer,
    WidgetSessionSerializer,
)
from .widget_utils import (
    check_origin_allowed,
    client_ip,
    is_tenant_online,
    request_origin,
    resolve_widget_connection,
)

logger = logging.getLogger(__name__)

SESSION_STALE_AFTER = timedelta(hours=24)


def _error(code: str, status_code: int, detail: str | None = None):
    payload = {'error': code}
    if detail:
        payload['detail'] = detail
    return Response(payload, status=status_code)


@api_view(['GET', 'OPTIONS'])
@permission_classes([AllowAny])
@ratelimit(key='ip', rate='60/m', block=True)
def widget_public_config(request):
    """Return widget branding + behaviour config for the given token."""
    token = request.query_params.get('token') or request.query_params.get('t')
    conn, err = resolve_widget_connection(token)
    if err == 'missing_token':
        return _error('missing_token', status.HTTP_400_BAD_REQUEST)
    if err == 'not_found':
        return _error('not_found', status.HTTP_404_NOT_FOUND)
    if err == 'disabled':
        return _error('disabled', status.HTTP_403_FORBIDDEN)

    is_setup_mode = not conn.allowed_origins
    origin_allowed = bool(conn.allowed_origins) and request_origin(request) in conn.allowed_origins

    data = {
        'widget_token': conn.widget_token,
        'brand_color': conn.brand_color,
        'position': conn.position,
        'welcome_message': conn.welcome_message or {},
        'pre_chat_form': conn.pre_chat_form or {},
        'offline_message': conn.offline_message or {},
        # Fields added in migration 0002 — defensive getattr keeps the
        # endpoint responding even if a pod still has the pre-migration
        # model imported (DO rolling restart window).
        'proactive_enabled': getattr(conn, 'proactive_enabled', False),
        'proactive_message': getattr(conn, 'proactive_message', None) or {},
        'proactive_delay_seconds': getattr(conn, 'proactive_delay_seconds', 30),
        'is_online': is_tenant_online(conn.tenant_schema),
        'is_setup_mode': is_setup_mode,
        'origin_allowed': origin_allowed or is_setup_mode,
    }
    return Response(data)


@api_view(['POST'])
@permission_classes([AllowAny])
@ratelimit(key='ip', rate='30/h', block=True)
def widget_public_sessions(request):
    """Create (or return the existing) visitor session."""
    body = request.data or {}
    token = body.get('token')
    visitor_id = (body.get('visitor_id') or '').strip()
    if not visitor_id:
        return _error('missing_visitor_id', status.HTTP_400_BAD_REQUEST)
    if len(visitor_id) > 64:
        return _error('invalid_visitor_id', status.HTTP_400_BAD_REQUEST)

    conn, err = resolve_widget_connection(token)
    if err:
        code_map = {'missing_token': 400, 'not_found': 404, 'disabled': 403}
        return _error(err, code_map[err])
    if conn.allowed_origins and not check_origin_allowed(conn, request):
        return _error('origin_not_allowed', status.HTTP_403_FORBIDDEN)

    with schema_context(conn.tenant_schema):
        # Re-use a recent session for the same visitor so a returning browser
        # keeps its conversation thread. Two filters keep this honest:
        #   - `last_seen_at__gte=cutoff`: drop dormant sessions older than
        #     SESSION_STALE_AFTER so a fresh visit feels like a fresh chat.
        #   - `ended_at__isnull=True`: never re-attach to a session the agent
        #     or visitor explicitly closed. After a close, the next message
        #     starts a brand-new chat thread on both ends.
        cutoff = timezone.now() - SESSION_STALE_AFTER
        existing = (
            WidgetSession.objects
            .filter(
                connection_id=conn.id,
                visitor_id=visitor_id,
                last_seen_at__gte=cutoff,
                ended_at__isnull=True,
            )
            .order_by('-last_seen_at')
            .first()
        )
        if existing:
            existing.last_seen_at = timezone.now()
            existing.save(update_fields=['last_seen_at'])
            return Response({
                'session_id': existing.session_id,
                'is_new': False,
                'session': WidgetSessionSerializer(existing).data,
            })

        # Carry name/email forward when a returning visitor starts a fresh
        # chat — they shouldn't have to re-enter prechat details after every
        # close. Look up the most recent prior session for this visitor_id
        # (any state — even ended ones) and inherit visitor_name /
        # visitor_email when the request body didn't supply them.
        body_name = (body.get('visitor_name') or '').strip()
        body_email = (body.get('visitor_email') or '').strip()
        if not body_name or not body_email:
            previous = (
                WidgetSession.objects
                .filter(connection_id=conn.id, visitor_id=visitor_id)
                .exclude(visitor_name='', visitor_email='')
                .order_by('-started_at')
                .only('visitor_name', 'visitor_email')
                .first()
            )
            if previous:
                if not body_name:
                    body_name = previous.visitor_name
                if not body_email:
                    body_email = previous.visitor_email

        session = WidgetSession.objects.create(
            connection_id=conn.id,
            session_id=uuid.uuid4().hex,
            visitor_id=visitor_id,
            visitor_name=body_name[:120],
            visitor_email=body_email[:254],
            referrer_url=body.get('referrer', '')[:500],
            page_url=body.get('page_url', '')[:500],
            user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:500],
            ip_address=client_ip(request),
        )
        return Response(
            {
                'session_id': session.session_id,
                'is_new': True,
                'session': WidgetSessionSerializer(session).data,
            },
            status=status.HTTP_201_CREATED,
        )


@api_view(['POST'])
@permission_classes([AllowAny])
@ratelimit(key='ip', rate='60/h', block=True)
# TODO(PR 3): switch to a custom key function that hashes session_id so
# one noisy visitor can't block another on a shared NAT.
def widget_public_messages(request):
    """Visitor sends a message."""
    body = request.data or {}
    token = body.get('token')
    session_id = (body.get('session_id') or '').strip()
    message_text = (body.get('message_text') or '').strip()
    attachments = body.get('attachments') or []

    if not session_id:
        return _error('missing_session_id', status.HTTP_400_BAD_REQUEST)
    if not message_text and not attachments:
        return _error('empty_message', status.HTTP_400_BAD_REQUEST)
    if len(message_text) > 10_000:
        return _error('message_too_long', status.HTTP_400_BAD_REQUEST)

    conn, err = resolve_widget_connection(token)
    if err:
        code_map = {'missing_token': 400, 'not_found': 404, 'disabled': 403}
        return _error(err, code_map[err])
    if conn.allowed_origins and not check_origin_allowed(conn, request):
        return _error('origin_not_allowed', status.HTTP_403_FORBIDDEN)

    with schema_context(conn.tenant_schema):
        try:
            session = WidgetSession.objects.get(session_id=session_id, connection_id=conn.id)
        except WidgetSession.DoesNotExist:
            return _error('session_not_found', status.HTTP_404_NOT_FOUND)

        if timezone.now() - session.last_seen_at > SESSION_STALE_AFTER:
            return _error('session_expired', status.HTTP_410_GONE)

        now = timezone.now()
        msg = WidgetMessage.objects.create(
            session=session,
            message_id=uuid.uuid4().hex,
            message_text=message_text,
            attachments=attachments if isinstance(attachments, list) else [],
            is_from_visitor=True,
            is_delivered=True,
            delivered_at=now,
            timestamp=now,
        )
        session.last_seen_at = now
        session.save(update_fields=['last_seen_at'])
        response_data = WidgetMessageSerializer(msg).data

    # Single broadcast to the tenant group — agents AND visitor iframes
    # both listen there. Frontend dedupes by message_id if the same frame
    # arrives twice (which happens when an agent is in both the tenant and
    # a conversation-specific group).
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
    if channel_layer:
        conversation_id = f"widget_{conn.id}_{session.session_id}"
        msg_payload = {
            'message_id': msg.message_id,
            'message_text': msg.message_text,
            'attachments': msg.attachments,
            'is_from_visitor': True,
            'timestamp': msg.timestamp.isoformat(),
            'session_id': session.session_id,
            'connection_id': conn.id,
            'platform': 'widget',
        }
        async_to_sync(channel_layer.group_send)(f'messages_{conn.tenant_schema}', {
            'type': 'new_message',
            'message': msg_payload,
            'conversation_id': conversation_id,
            'timestamp': msg_payload['timestamp'],
        })

    # Auto-unarchive: a new visitor message should always pop the conversation
    # back into the agent's active inbox if it had been moved to history.
    # Mirrors the behaviour shipped for facebook/instagram/whatsapp/email.
    with schema_context(conn.tenant_schema):
        from .views import auto_unarchive_conversation
        auto_unarchive_conversation(
            platform='widget',
            conversation_id=session.session_id,
            account_id=str(conn.id),
        )
        try:
            from .services import ai_companion
            ai_companion.schedule_ai_reply(
                'widget', str(conn.id), session.session_id
            )
        except Exception:  # noqa: BLE001
            logger.exception('widget ai companion hook failed')

    return Response(response_data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([AllowAny])
@ratelimit(key='ip', rate='120/m', block=True)
def widget_public_messages_list(request):
    """Polling fallback — return session messages after `after` timestamp."""
    token = request.query_params.get('token')
    session_id = (request.query_params.get('session_id') or '').strip()
    after_raw = request.query_params.get('after')

    if not session_id:
        return _error('missing_session_id', status.HTTP_400_BAD_REQUEST)

    conn, err = resolve_widget_connection(token)
    if err:
        code_map = {'missing_token': 400, 'not_found': 404, 'disabled': 403}
        return _error(err, code_map[err])

    with schema_context(conn.tenant_schema):
        try:
            session = WidgetSession.objects.get(session_id=session_id, connection_id=conn.id)
        except WidgetSession.DoesNotExist:
            return _error('session_not_found', status.HTTP_404_NOT_FOUND)

        qs = WidgetMessage.objects.filter(session=session, is_deleted=False).order_by('timestamp')
        if after_raw:
            try:
                after_dt = timezone.datetime.fromisoformat(after_raw.replace('Z', '+00:00'))
                qs = qs.filter(timestamp__gt=after_dt)
            except (ValueError, TypeError):
                pass  # Ignore malformed timestamps — return everything.

        # Mark agent-sent messages as read now that visitor polled them.
        now = timezone.now()
        (
            qs.filter(is_from_visitor=False, is_read_by_visitor=False)
              .update(is_read_by_visitor=True)
        )

        return Response(WidgetMessageSerializer(qs, many=True).data)


WIDGET_UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
WIDGET_UPLOAD_ALLOWED_PREFIXES = (
    'image/',
    'audio/',
    'video/',
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument',
    'application/vnd.ms-excel',
    'application/vnd.ms-powerpoint',
    'text/plain',
    'text/csv',
)


def _is_allowed_upload_type(content_type: str) -> bool:
    if not content_type:
        return False
    content_type = content_type.lower()
    return any(content_type.startswith(p) for p in WIDGET_UPLOAD_ALLOWED_PREFIXES)


@api_view(['POST'])
@permission_classes([AllowAny])
@ratelimit(key='ip', rate='30/h', block=True)
def widget_public_upload(request):
    """Visitor-side attachment upload.

    Body (multipart/form-data):
        token:       widget token
        session_id:  active session id
        file:        the file (<=10 MB, allowed content-type)
    Returns {url, filename, size, content_type}.

    We trust the DO Spaces UUID path to keep the URL unguessable; the
    attachment only becomes reachable once the visitor references it
    from a WidgetMessage, which is tied to their session.
    """
    token = request.data.get('token') or request.query_params.get('token')
    session_id = (request.data.get('session_id') or '').strip()
    upload = request.FILES.get('file')

    if not upload:
        return _error('missing_file', status.HTTP_400_BAD_REQUEST)
    if not session_id:
        return _error('missing_session_id', status.HTTP_400_BAD_REQUEST)
    if upload.size > WIDGET_UPLOAD_MAX_BYTES:
        return _error('file_too_large', status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                      detail=f'Max {WIDGET_UPLOAD_MAX_BYTES // (1024 * 1024)} MB.')
    if not _is_allowed_upload_type(upload.content_type or ''):
        return _error('unsupported_type', status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                      detail=f'Content-Type {upload.content_type!r} not allowed.')

    conn, err = resolve_widget_connection(token)
    if err:
        code_map = {'missing_token': 400, 'not_found': 404, 'disabled': 403}
        return _error(err, code_map[err])
    if conn.allowed_origins and not check_origin_allowed(conn, request):
        return _error('origin_not_allowed', status.HTTP_403_FORBIDDEN)

    with schema_context(conn.tenant_schema):
        try:
            session = WidgetSession.objects.get(session_id=session_id, connection_id=conn.id)
        except WidgetSession.DoesNotExist:
            return _error('session_not_found', status.HTTP_404_NOT_FOUND)
        if timezone.now() - session.last_seen_at > SESSION_STALE_AFTER:
            return _error('session_expired', status.HTTP_410_GONE)

    # Save to Spaces. Keep filename but prefix with a UUID so the URL can't be
    # enumerated and two visitors uploading "receipt.pdf" don't collide.
    from django.core.files.storage import default_storage
    safe_name = (upload.name or 'file').replace('/', '_').replace('\\', '_')[:120]
    storage_path = (
        f'widget/{conn.tenant_schema}/{session.session_id}/'
        f'{uuid.uuid4().hex}-{safe_name}'
    )
    saved_path = default_storage.save(storage_path, upload)
    url = default_storage.url(saved_path)
    return Response({
        'url': url,
        'filename': safe_name,
        'size': upload.size,
        'content_type': upload.content_type,
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def widget_admin_send_message(request):
    """Agent-side: send a reply into a widget conversation.

    Body: {connection_id: int, session_id: str, message_text: str, attachments?: list}
    """
    body = request.data or {}
    try:
        connection_id = int(body.get('connection_id'))
    except (TypeError, ValueError):
        return _error('invalid_connection_id', status.HTTP_400_BAD_REQUEST)
    session_id = (body.get('session_id') or '').strip()
    message_text = (body.get('message_text') or '').strip()
    attachments = body.get('attachments') or []
    if not session_id:
        return _error('missing_session_id', status.HTTP_400_BAD_REQUEST)
    if not message_text and not attachments:
        return _error('empty_message', status.HTTP_400_BAD_REQUEST)
    if len(message_text) > 10_000:
        return _error('message_too_long', status.HTTP_400_BAD_REQUEST)

    schema = getattr(request.tenant, 'schema_name', None)
    if not schema or schema == 'public':
        return _error('tenant_required', status.HTTP_400_BAD_REQUEST)

    # Verify tenant owns this connection.
    with schema_context('public'):
        try:
            conn = WidgetConnection.objects.get(id=connection_id, tenant_schema=schema)
        except WidgetConnection.DoesNotExist:
            return _error('not_found', status.HTTP_404_NOT_FOUND)

    with schema_context(schema):
        try:
            session = WidgetSession.objects.get(session_id=session_id, connection_id=connection_id)
        except WidgetSession.DoesNotExist:
            return _error('session_not_found', status.HTTP_404_NOT_FOUND)
        now = timezone.now()
        msg = WidgetMessage.objects.create(
            session=session,
            message_id=uuid.uuid4().hex,
            message_text=message_text,
            attachments=attachments if isinstance(attachments, list) else [],
            is_from_visitor=False,
            sent_by=request.user,
            is_delivered=True,
            delivered_at=now,
            timestamp=now,
        )
        payload_data = WidgetMessageSerializer(msg).data

    # Broadcast
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
    if channel_layer:
        conversation_id = f"widget_{connection_id}_{session_id}"
        msg_payload = {
            'message_id': msg.message_id,
            'message_text': msg.message_text,
            'attachments': msg.attachments,
            'is_from_visitor': False,
            'sent_by': request.user.id,
            'timestamp': msg.timestamp.isoformat(),
            'session_id': session_id,
            'connection_id': connection_id,
            'platform': 'widget',
        }
        async_to_sync(channel_layer.group_send)(f'messages_{schema}', {
            'type': 'new_message',
            'message': msg_payload,
            'conversation_id': conversation_id,
            'timestamp': msg_payload['timestamp'],
        })

    return Response(payload_data, status=status.HTTP_201_CREATED)


@extend_schema(
    request={
        'application/json': {
            'type': 'object',
            'required': ['token', 'session_id'],
            'properties': {
                'token': {'type': 'string', 'description': 'Widget token'},
                'session_id': {'type': 'string', 'description': 'Active widget session id'},
            },
        }
    },
    responses={
        200: OpenApiResponse(
            response={
                'type': 'object',
                'properties': {
                    'status': {'type': 'string', 'enum': ['ok', 'already_ended']},
                    'ended_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                    'ended_by': {'type': 'string', 'enum': ['visitor', 'agent', 'timeout'], 'nullable': True},
                },
            },
            description='Session closed (or already closed).'
        ),
        400: OpenApiResponse(description='Invalid request body.'),
        403: OpenApiResponse(description='Widget connection disabled or origin not allowed.'),
        404: OpenApiResponse(description='Token or session not found.'),
        410: OpenApiResponse(description='Session expired.'),
    },
    description='Visitor-initiated session close. Marks the WidgetSession as ended_by=visitor.',
    summary='Close widget session (visitor)',
)
@api_view(['POST'])
@permission_classes([AllowAny])
@ratelimit(key='ip', rate='30/h', block=True)
def widget_public_close_session(request):
    """Visitor closes their own widget session."""
    body = request.data or {}
    token = body.get('token')
    session_id = (body.get('session_id') or '').strip()

    if not session_id:
        return _error('missing_session_id', status.HTTP_400_BAD_REQUEST)

    conn, err = resolve_widget_connection(token)
    if err:
        code_map = {'missing_token': 400, 'not_found': 404, 'disabled': 403}
        return _error(err, code_map[err])
    if conn.allowed_origins and not check_origin_allowed(conn, request):
        return _error('origin_not_allowed', status.HTTP_403_FORBIDDEN)

    with schema_context(conn.tenant_schema):
        try:
            session = WidgetSession.objects.get(session_id=session_id, connection_id=conn.id)
        except WidgetSession.DoesNotExist:
            return _error('session_not_found', status.HTTP_404_NOT_FOUND)

        if session.ended_at is not None:
            return Response({
                'status': 'already_ended',
                'ended_at': session.ended_at.isoformat(),
                'ended_by': session.ended_by,
            })

        # Stale-session 410 — match the convention used by other widget views.
        if timezone.now() - session.last_seen_at > SESSION_STALE_AFTER:
            return _error('session_expired', status.HTTP_410_GONE)

        session.ended_at = timezone.now()
        session.ended_by = 'visitor'
        session.save(update_fields=['ended_at', 'ended_by'])
        ended_at_iso = session.ended_at.isoformat()

        # When the visitor closes their own chat, also mark any active
        # ChatAssignment as completed and unassign the agent — otherwise
        # the agent's "Assigned to me" tab keeps showing a phantom chat
        # for a session that no longer exists. Mirrors the same cleanup
        # the assignment-end view does. Idempotent.
        try:
            from .models import ChatAssignment
            ChatAssignment.objects.filter(
                platform='widget',
                conversation_id=session_id,
                account_id=str(conn.id),
                status__in=['active', 'in_session'],
            ).update(
                status='completed',
                assigned_user=None,
                session_ended_at=session.ended_at,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to complete ChatAssignment on visitor close: %s",
                exc,
            )

        # When the visitor explicitly says "I'm done", also move the
        # conversation to the agent's history. Best-effort — any failure
        # here (unique constraint race, missing tenant migration, etc.)
        # MUST NOT block the WS broadcast below, which is the only signal
        # the visitor's iframe has to flip to the review form.
        try:
            ConversationArchive.objects.get_or_create(
                platform='widget',
                conversation_id=session_id,
                account_id=str(conn.id),
                defaults={'archived_by': None},
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "Failed to archive widget conversation on visitor close: %s",
                exc,
            )

    # Broadcast so the agent dashboard sees the visitor-initiated close in
    # real time. Mirrors the agent-close path which broadcasts the same
    # event the other direction. Both MessagesConsumer (agent) and
    # WidgetVisitorConsumer (visitor) live in `messages_<tenant>` — but
    # WidgetVisitorConsumer filters by session_id so only the active
    # visitor iframe receives it (and on the visitor side we expect a
    # no-op anyway: they're the one who clicked "End conversation").
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(f'messages_{conn.tenant_schema}', {
            'type': 'session_ended',
            'session_id': session_id,
            'connection_id': conn.id,
            'conversation_id': f'widget_{conn.id}_{session_id}',
            'ended_by': 'visitor',
            'ended_at': ended_at_iso,
            'message': 'The visitor ended this conversation.',
        })

    return Response({
        'status': 'ok',
        'ended_at': ended_at_iso,
        'ended_by': 'visitor',
    })


@extend_schema(
    request={
        'application/json': {
            'type': 'object',
            'required': ['token', 'session_id', 'rating'],
            'properties': {
                'token': {'type': 'string', 'description': 'Widget token'},
                'session_id': {'type': 'string', 'description': 'Widget session id'},
                'rating': {'type': 'integer', 'minimum': 1, 'maximum': 5},
                'comment': {'type': 'string', 'description': 'Optional free-form comment'},
            },
        }
    },
    responses={
        200: OpenApiResponse(
            response={
                'type': 'object',
                'properties': {
                    'status': {'type': 'string', 'enum': ['ok']},
                    'rating': {'type': 'integer'},
                },
            },
            description='Rating recorded (created or updated).'
        ),
        400: OpenApiResponse(description='Invalid rating or missing fields.'),
        403: OpenApiResponse(description='Origin not allowed or widget disabled.'),
        404: OpenApiResponse(description='Token or session not found.'),
    },
    description=(
        'Visitor submits a 1-5 star rating for a widget session. Idempotent — '
        'a second submission for the same session updates the existing row.'
    ),
    summary='Rate widget session (visitor)',
)
@api_view(['POST'])
@permission_classes([AllowAny])
@ratelimit(key='ip', rate='30/h', block=True)
def widget_public_rate_session(request):
    """Visitor submits a post-chat rating + optional comment."""
    body = request.data or {}
    token = body.get('token')
    session_id = (body.get('session_id') or '').strip()
    rating_raw = body.get('rating')
    comment = (body.get('comment') or '').strip()

    if not session_id:
        return _error('missing_session_id', status.HTTP_400_BAD_REQUEST)

    # Validate rating: must be an int 1..5. Reject floats / strings outright
    # so we don't silently round.
    try:
        rating = int(rating_raw)
    except (TypeError, ValueError):
        return _error('invalid_rating', status.HTTP_400_BAD_REQUEST)
    if rating < 1 or rating > 5:
        return _error('invalid_rating', status.HTTP_400_BAD_REQUEST)
    if len(comment) > 5000:
        return _error('comment_too_long', status.HTTP_400_BAD_REQUEST)

    conn, err = resolve_widget_connection(token)
    if err:
        code_map = {'missing_token': 400, 'not_found': 404, 'disabled': 403}
        return _error(err, code_map[err])
    if conn.allowed_origins and not check_origin_allowed(conn, request):
        return _error('origin_not_allowed', status.HTTP_403_FORBIDDEN)

    with schema_context(conn.tenant_schema):
        try:
            session = WidgetSession.objects.get(session_id=session_id, connection_id=conn.id)
        except WidgetSession.DoesNotExist:
            return _error('session_not_found', status.HTTP_404_NOT_FOUND)

        # ChatRating's identity field is `conversation_id` (not `chat_id`).
        # Match the convention the rest of the widget pipeline uses for
        # tying social-style records back to a widget session:
        # conversation_id = "widget_<connection_id>_<session_id>".
        conversation_id = f"widget_{conn.id}_{session.session_id}"
        session_ended_at = session.ended_at or timezone.now()

        # Attribute the rating to whichever agent actually handled the chat
        # so the per-user rating-statistics breakdown picks it up. Best
        # effort: most recent assignment wins; fall back to the latest
        # agent-side message; otherwise leave null (still counts in the
        # aggregate totals, just not in the per-agent table).
        rated_user = None
        try:
            from .models import ChatAssignment
            assignment = (
                ChatAssignment.objects
                .filter(
                    platform='widget',
                    conversation_id=session.session_id,
                    account_id=str(conn.id),
                )
                .order_by('-assigned_at')
                .first()
            )
            if assignment and assignment.assigned_user_id:
                rated_user = assignment.assigned_user
            else:
                last_agent_msg = (
                    WidgetMessage.objects
                    .filter(
                        session=session,
                        is_from_visitor=False,
                        sent_by__isnull=False,
                    )
                    .order_by('-timestamp')
                    .first()
                )
                if last_agent_msg:
                    rated_user = last_agent_msg.sent_by
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to resolve rated_user for widget rating: %s", exc
            )

        ChatRating.objects.update_or_create(
            platform='widget',
            conversation_id=conversation_id,
            defaults={
                'rating': rating,
                'comment': comment,
                'account_id': str(conn.id),
                'session_started_at': session.started_at,
                'session_ended_at': session_ended_at,
                'rated_user': rated_user,
            },
        )

    return Response({'status': 'ok', 'rating': rating})


@extend_schema(
    request={
        'application/json': {
            'type': 'object',
            'required': ['connection_id', 'session_id'],
            'properties': {
                'connection_id': {'type': 'integer'},
                'session_id': {'type': 'string'},
            },
        }
    },
    responses={
        200: OpenApiResponse(
            response={
                'type': 'object',
                'properties': {
                    'status': {'type': 'string', 'enum': ['ok', 'already_ended']},
                    'ended_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                    'ended_by': {'type': 'string', 'enum': ['visitor', 'agent', 'timeout'], 'nullable': True},
                },
            },
            description='Session closed (or already closed).'
        ),
        400: OpenApiResponse(description='Invalid request body.'),
        404: OpenApiResponse(description='Connection or session not found.'),
    },
    description=(
        'Agent-initiated session close from the dashboard. Marks the '
        'WidgetSession ended_by=agent and pushes a `session_ended` WS '
        'event so the visitor iframe can surface the post-chat review.'
    ),
    summary='Close widget session (agent)',
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def widget_admin_close_session(request):
    """Agent ends a widget session from the tenant dashboard."""
    body = request.data or {}
    try:
        connection_id = int(body.get('connection_id'))
    except (TypeError, ValueError):
        return _error('invalid_connection_id', status.HTTP_400_BAD_REQUEST)
    session_id = (body.get('session_id') or '').strip()
    if not session_id:
        return _error('missing_session_id', status.HTTP_400_BAD_REQUEST)

    schema = getattr(request.tenant, 'schema_name', None)
    if not schema or schema == 'public':
        return _error('tenant_required', status.HTTP_400_BAD_REQUEST)

    # Verify the tenant owns this connection (registry lives in public).
    with schema_context('public'):
        try:
            conn = WidgetConnection.objects.get(id=connection_id, tenant_schema=schema)
        except WidgetConnection.DoesNotExist:
            return _error('not_found', status.HTTP_404_NOT_FOUND)

    already_ended = False
    with schema_context(schema):
        try:
            session = WidgetSession.objects.get(session_id=session_id, connection_id=connection_id)
        except WidgetSession.DoesNotExist:
            return _error('session_not_found', status.HTTP_404_NOT_FOUND)

        if session.ended_at is not None:
            already_ended = True
            ended_at_iso = session.ended_at.isoformat()
            ended_by_value = session.ended_by or 'agent'
        else:
            session.ended_at = timezone.now()
            session.ended_by = 'agent'
            session.save(update_fields=['ended_at', 'ended_by'])
            ended_at_iso = session.ended_at.isoformat()
            ended_by_value = 'agent'

            # An agent-initiated end implies the chat is done — clear any
            # active ChatAssignment and move the conversation to history
            # so the agent inbox stays tidy. Both are idempotent and
            # best-effort: failures log a warning, don't swallow the
            # broadcast below.
            try:
                from .models import ChatAssignment
                ChatAssignment.objects.filter(
                    platform='widget',
                    conversation_id=session_id,
                    account_id=str(connection_id),
                    status__in=['active', 'in_session'],
                ).update(
                    status='completed',
                    assigned_user=None,
                    session_ended_at=session.ended_at,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to complete ChatAssignment on agent close: %s",
                    exc,
                )
            try:
                ConversationArchive.objects.get_or_create(
                    platform='widget',
                    conversation_id=session_id,
                    account_id=str(connection_id),
                    defaults={'archived_by': request.user},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to archive widget conversation on agent close: %s",
                    exc,
                )

    # Broadcast every time — even if the session was already ended on the
    # backend, the visitor's iframe might have missed the original event
    # (network blip, iframe was hidden, etc.). Re-firing is idempotent on
    # the visitor side: setEndedBy('agent') is a no-op if it's already
    # 'agent', and the iframe stays on the review form.
    logger.info(
        "Broadcasting widget session_ended: schema=%s connection_id=%s "
        "session_id=%s ended_by=%s already_ended=%s",
        conn.tenant_schema, connection_id, session_id, ended_by_value, already_ended,
    )
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(f'messages_{conn.tenant_schema}', {
            'type': 'session_ended',
            'session_id': session_id,
            'connection_id': connection_id,
            'conversation_id': f'widget_{connection_id}_{session_id}',
            'ended_by': ended_by_value,
            'ended_at': ended_at_iso,
            'message': 'The agent has ended this conversation.',
        })
    else:
        logger.warning(
            "No channel layer available — agent end broadcast skipped for "
            "session %s (tenant %s)",
            session_id, conn.tenant_schema,
        )

    return Response({
        'status': 'already_ended' if already_ended else 'ok',
        'ended_at': ended_at_iso,
        'ended_by': ended_by_value,
    })


class WidgetConnectionViewSet(viewsets.ModelViewSet):
    """Tenant-scoped admin CRUD for widget connections (lives in public schema)."""
    serializer_class = WidgetConnectionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        schema = getattr(self.request.tenant, 'schema_name', None)
        if not schema or schema == 'public':
            return WidgetConnection.objects.none()
        with schema_context('public'):
            return WidgetConnection.objects.filter(tenant_schema=schema).order_by('-created_at')

    def perform_create(self, serializer):
        schema = self.request.tenant.schema_name
        with schema_context('public'):
            serializer.save(
                tenant_schema=schema,
                widget_token=WidgetConnection.generate_token(),
            )

    def perform_update(self, serializer):
        with schema_context('public'):
            serializer.save()

    def perform_destroy(self, instance):
        with schema_context('public'):
            instance.delete()


class WidgetMessageViewSet(viewsets.ReadOnlyModelViewSet):
    """Tenant-scoped read-only view of widget messages. Agents use this when
    surfing a single visitor's transcript."""
    serializer_class = WidgetMessageSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        schema = getattr(self.request.tenant, 'schema_name', None)
        if not schema or schema == 'public':
            return WidgetMessage.objects.none()
        # WidgetMessage lives in the tenant schema already — the tenant middleware
        # set the connection. Filter by session_id query param when provided.
        qs = WidgetMessage.objects.filter(is_deleted=False).order_by('timestamp')
        session_id = self.request.query_params.get('session_id')
        if session_id:
            qs = qs.filter(session__session_id=session_id)
        return qs
