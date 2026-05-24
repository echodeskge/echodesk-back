"""
WebSocket consumers for real-time messaging functionality.
"""

import json
import asyncio
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from tenant_schemas.utils import schema_context
from .models import FacebookMessage, FacebookPageConnection


class MessagesConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time message updates.
    Handles new incoming messages and message status updates.
    """
    
    async def connect(self):
        self.tenant_schema = self.scope['url_route']['kwargs']['tenant_schema']
        self.user = self.scope.get('user', AnonymousUser())
        
        # For now, allow all connections to test WebSocket functionality
        # TODO: Add proper authentication when WebSocket auth is configured
        print(f"[WebSocket] Connection attempt - User: {self.user}, Tenant: {self.tenant_schema}")
        
        # Join the messages group for this tenant
        self.messages_group_name = f'messages_{self.tenant_schema}'
        
        await self.channel_layer.group_add(
            self.messages_group_name,
            self.channel_name
        )
        
        await self.accept()
        
        # Send initial connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection',
            'status': 'connected',
            'tenant': self.tenant_schema,
            'user_authenticated': not self.user.is_anonymous
        }))
        
        print(f"[WebSocket] Connected successfully for tenant: {self.tenant_schema}")
    
    async def disconnect(self, close_code):
        # Leave the messages group
        if hasattr(self, 'messages_group_name'):
            await self.channel_layer.group_discard(
                self.messages_group_name,
                self.channel_name
            )
    
    async def receive(self, text_data):
        """Handle incoming WebSocket messages from client"""
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
            
            if message_type == 'ping':
                # Respond to ping with pong for connection health check
                await self.send(text_data=json.dumps({
                    'type': 'pong',
                    'timestamp': data.get('timestamp')
                }))
            
            elif message_type == 'subscribe_conversation':
                # Subscribe to specific conversation updates
                conversation_id = data.get('conversation_id')
                if conversation_id:
                    self.conversation_group_name = f'conversation_{self.tenant_schema}_{conversation_id}'
                    await self.channel_layer.group_add(
                        self.conversation_group_name,
                        self.channel_name
                    )
                    
                    await self.send(text_data=json.dumps({
                        'type': 'subscription',
                        'status': 'subscribed',
                        'conversation_id': conversation_id
                    }))
            
            elif message_type == 'unsubscribe_conversation':
                # Unsubscribe from conversation updates
                conversation_id = data.get('conversation_id')
                if conversation_id and hasattr(self, 'conversation_group_name'):
                    await self.channel_layer.group_discard(
                        self.conversation_group_name,
                        self.channel_name
                    )
                    
                    await self.send(text_data=json.dumps({
                        'type': 'subscription',
                        'status': 'unsubscribed',
                        'conversation_id': conversation_id
                    }))
        
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON format'
            }))
    
    # Handlers for messages sent from Django views/signals
    async def new_message(self, event):
        """Send new message to WebSocket"""
        await self.send(text_data=json.dumps({
            'type': 'new_message',
            'message': event['message'],
            'conversation_id': event['conversation_id'],
            'timestamp': event['timestamp'],
            'assigned_user_id': event.get('assigned_user_id'),  # None if unassigned
        }))
    
    async def message_status_update(self, event):
        """Send a message-status (sent/delivered/read) update to the client.

        Carries a `message_ids` batch + the (platform, conversation_id,
        account_id) triple so /messages-beta can resolve the chat and flip
        the status pips on the matching bubbles. Falls back to a single
        `message_id` for any legacy caller.
        """
        message_ids = event.get('message_ids')
        if not message_ids and event.get('message_id') is not None:
            message_ids = [event['message_id']]
        await self.send(text_data=json.dumps({
            'type': 'message_status',
            'platform': event.get('platform'),
            'conversation_id': event.get('conversation_id'),
            'account_id': event.get('account_id'),
            'message_ids': message_ids or [],
            'status': event.get('status'),
            'timestamp': event.get('timestamp'),
        }))

    async def reaction_update(self, event):
        """Send a message-reaction add/remove to the client.

        `reaction_emoji` is null/empty when a reaction was removed.
        """
        await self.send(text_data=json.dumps({
            'type': 'reaction_update',
            'platform': event.get('platform'),
            'conversation_id': event.get('conversation_id'),
            'account_id': event.get('account_id'),
            'message_id': event.get('message_id'),
            'reaction_emoji': event.get('reaction_emoji'),
            'timestamp': event.get('timestamp'),
        }))

    async def conversation_update(self, event):
        """Send conversation update to WebSocket"""
        await self.send(text_data=json.dumps({
            'type': 'conversation_update',
            'conversation_id': event['conversation_id'],
            'last_message': event['last_message'],
            'timestamp': event['timestamp']
        }))

    async def session_ended(self, event):
        """Forward widget `session_ended` events to the agent dashboard.

        Both the agent-side MessagesConsumer and the visitor-side
        WidgetVisitorConsumer subscribe to the same `messages_<tenant>` group.
        When either side closes a widget chat we broadcast once and both
        consumers forward the event to their respective sockets — the agent
        UI uses this to refresh the conversation and mark the chat ended,
        the visitor iframe uses it to flip to the post-chat review form.
        """
        await self.send(text_data=json.dumps({
            'type': 'session_ended',
            'platform': 'widget',
            'session_id': event.get('session_id'),
            'connection_id': event.get('connection_id'),
            'conversation_id': event.get('conversation_id'),
            'ended_by': event.get('ended_by'),
            'ended_at': event.get('ended_at'),
        }))

    # ------------------------------------------------------------------
    # /messages-beta cross-user reactivity broadcasts (additive — the
    # legacy /messages frontend ignores these types via the switch's
    # default branch). Each handler just forwards the event payload so
    # consumers stay dumb; the heavy logic lives in the Django views that
    # call `channel_layer.group_send`.
    # ------------------------------------------------------------------
    async def assignment_update(self, event):
        """Pushed by assign/unassign/transfer/start-session/end-session views."""
        await self.send(text_data=json.dumps({
            'type': 'assignment_update',
            'platform': event.get('platform'),
            'conversation_id': event.get('conversation_id'),
            'account_id': event.get('account_id'),
            'assigned_user_id': event.get('assigned_user_id'),
            'assigned_user_name': event.get('assigned_user_name'),
            'status': event.get('status'),
            'session_started_at': event.get('session_started_at'),
            'session_ended_at': event.get('session_ended_at'),
            'by_user_id': event.get('by_user_id'),
            'timestamp': event.get('timestamp'),
        }))

    async def read_state_update(self, event):
        """Pushed by mark-read/mark-unread/mark-all-read views."""
        await self.send(text_data=json.dumps({
            'type': 'read_state_update',
            'platform': event.get('platform'),
            'conversation_id': event.get('conversation_id'),
            'account_id': event.get('account_id'),
            'unread_count': event.get('unread_count'),
            'last_read_at': event.get('last_read_at'),
            'by_user_id': event.get('by_user_id'),
            'timestamp': event.get('timestamp'),
        }))

    async def archive_update(self, event):
        """Pushed by archive/unarchive/archive-all views."""
        await self.send(text_data=json.dumps({
            'type': 'archive_update',
            'platform': event.get('platform'),
            'conversation_id': event.get('conversation_id'),
            'account_id': event.get('account_id'),
            'archived': event.get('archived'),
            'archived_at': event.get('archived_at'),
            'by_user_id': event.get('by_user_id'),
            'timestamp': event.get('timestamp'),
        }))

    async def conversation_deleted(self, event):
        """Pushed by delete-conversation + clear-platform-history views.

        Beta clients drop the conversation row from their sidebar + deselect
        the chat if it was active. Legacy clients ignore the unknown event
        type via their switch-default branch (safe-by-default contract).

        When `conversation_id` is null, the frame is the bulk variant from
        `clear_platform_history` — every conversation on the given platform
        should be removed at once.
        """
        await self.send(text_data=json.dumps({
            'type': 'conversation_deleted',
            'platform': event.get('platform'),
            'conversation_id': event.get('conversation_id'),
            'account_id': event.get('account_id'),
            'by_user_id': event.get('by_user_id'),
            'timestamp': event.get('timestamp'),
        }))


class WidgetVisitorConsumer(AsyncWebsocketConsumer):
    """
    WebSocket endpoint for a website visitor's widget chat session.

    URL: /ws/widget/<token>/<session_id>/
    - Anonymous (no JWT/session auth) — token + session_id are the credential pair.
    - Joins group `widget_visitor_<session_id>` so agent replies land in real time.
    - Messages sent by the client create a WidgetMessage row and broadcast to
      both the visitor's group AND the agent's `messages_<tenant_schema>` group
      (reused as-is from Facebook / WhatsApp / etc.).
    """

    async def connect(self):
        self.token = self.scope['url_route']['kwargs']['token']
        self.session_id = self.scope['url_route']['kwargs']['session_id']

        resolved = await self._resolve()
        if not resolved:
            await self.close(code=4004)
            return

        self.connection_id, self.tenant_schema, self.widget_connection_pk = resolved
        # Match the pattern the rest of social messages uses: one broadcast
        # group per tenant (``messages_<tenant>``). The visitor's consumer
        # filters frames by session_id in new_message so only messages for
        # this session land on the iframe. No separate widget_visitor_*
        # group, no double broadcast, no double delivery.
        self.tenant_group = f'messages_{self.tenant_schema}'
        await self.channel_layer.group_add(self.tenant_group, self.channel_name)

        # MUST accept the connection before sending any frames — Channels
        # silently drops `send()` calls until the handshake completes.
        # (A previous edit accidentally removed this and broke the entire
        # visitor WS path including the live `session_ended` delivery.)
        await self.accept()

        # Hand the visitor the session's CURRENT close state in the
        # connection handshake, so a reconnect after a missed
        # `session_ended` event (e.g. iframe was display:none, network blip,
        # host page reload) still routes them to the post-chat review.
        ended_state = await self._fetch_ended_state()
        await self.send(text_data=json.dumps({
            'type': 'connection',
            'status': 'connected',
            'session_id': self.session_id,
            'ended_at': ended_state.get('ended_at'),
            'ended_by': ended_state.get('ended_by'),
        }))

    @database_sync_to_async
    def _resolve(self):
        """Validate token + session_id. Returns (connection_id, tenant_schema, pk) or None."""
        from widget_registry.models import WidgetConnection
        from .models import WidgetSession
        from tenant_schemas.utils import schema_context, get_public_schema_name

        with schema_context(get_public_schema_name()):
            try:
                conn = WidgetConnection.objects.get(widget_token=self.token, is_active=True)
            except WidgetConnection.DoesNotExist:
                return None
            conn_id = conn.id
            conn_pk = conn.pk
            tenant_schema = conn.tenant_schema
        with schema_context(tenant_schema):
            exists = WidgetSession.objects.filter(
                session_id=self.session_id, connection_id=conn_id
            ).exists()
        if not exists:
            return None
        return conn_id, tenant_schema, conn_pk

    @database_sync_to_async
    def _fetch_ended_state(self):
        """Return `{ended_at, ended_by}` for the visitor's session.

        Both fields are null when the session is still active; when set
        the iframe uses them to immediately surface the review form
        instead of the message composer.
        """
        from .models import WidgetSession
        from tenant_schemas.utils import schema_context

        with schema_context(self.tenant_schema):
            row = (
                WidgetSession.objects
                .filter(session_id=self.session_id, connection_id=self.connection_id)
                .values('ended_at', 'ended_by')
                .first()
            )
        if not row:
            return {'ended_at': None, 'ended_by': None}
        return {
            'ended_at': row['ended_at'].isoformat() if row['ended_at'] else None,
            'ended_by': row['ended_by'],
        }

    async def disconnect(self, close_code):
        if hasattr(self, 'tenant_group'):
            await self.channel_layer.group_discard(self.tenant_group, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({'type': 'error', 'message': 'invalid_json'}))
            return

        mtype = data.get('type')
        if mtype == 'ping':
            await self.send(text_data=json.dumps({'type': 'pong', 'timestamp': data.get('timestamp')}))
            return
        if mtype == 'message':
            text = (data.get('text') or '').strip()
            attachments = data.get('attachments') or []
            if not text and not attachments:
                await self.send(text_data=json.dumps({'type': 'error', 'message': 'empty_message'}))
                return
            msg_dict = await self._persist_visitor_message(text, attachments if isinstance(attachments, list) else [])
            if not msg_dict:
                await self.send(text_data=json.dumps({'type': 'error', 'message': 'session_expired'}))
                return
            # Single broadcast to the tenant group — agent sidebar AND any
            # other visitor tabs of this session all listen there and filter
            # by session_id in new_message.
            conversation_id = f"widget_{self.connection_id}_{self.session_id}"
            await self.channel_layer.group_send(self.tenant_group, {
                'type': 'new_message',
                'message': msg_dict,
                'conversation_id': conversation_id,
                'timestamp': msg_dict.get('timestamp'),
            })
            return

    @database_sync_to_async
    def _persist_visitor_message(self, text, attachments):
        import uuid
        from django.utils import timezone
        from datetime import timedelta
        from .models import WidgetMessage, WidgetSession

        STALE = timedelta(hours=24)
        with schema_context(self.tenant_schema):
            try:
                session = WidgetSession.objects.get(
                    session_id=self.session_id, connection_id=self.connection_id
                )
            except WidgetSession.DoesNotExist:
                return None
            now = timezone.now()
            if now - session.last_seen_at > STALE:
                return None
            msg = WidgetMessage.objects.create(
                session=session,
                message_id=uuid.uuid4().hex,
                message_text=text,
                attachments=attachments,
                is_from_visitor=True,
                is_delivered=True,
                delivered_at=now,
                timestamp=now,
            )
            session.last_seen_at = now
            session.save(update_fields=['last_seen_at'])
            return {
                'message_id': msg.message_id,
                'message_text': msg.message_text,
                'attachments': msg.attachments,
                'is_from_visitor': True,
                'timestamp': msg.timestamp.isoformat(),
                'session_id': self.session_id,
                'connection_id': self.connection_id,
                'platform': 'widget',
            }

    # Outbound handlers invoked by channel_layer.group_send
    async def new_message(self, event):
        """Forward new_message events to the visitor iframe.

        The consumer listens on the tenant-wide ``messages_<tenant>`` group
        (same pattern as the agent's MessagesConsumer). Filter every frame
        so only messages for THIS widget session reach the iframe — agent
        messages to Facebook / WhatsApp / email are also on this group and
        must be ignored.
        """
        msg = event.get('message') or {}
        if (msg.get('platform') or '') != 'widget':
            return
        msg_session = msg.get('session_id') or self._session_from_conversation_id(event.get('conversation_id'))
        if msg_session != self.session_id:
            return
        await self.send(text_data=json.dumps({
            'type': 'new_message',
            'message': msg,
            'conversation_id': event.get('conversation_id'),
            'timestamp': event.get('timestamp'),
        }))

    @staticmethod
    def _session_from_conversation_id(conv_id):
        # conversation_id format: widget_<connection_id>_<session_id>
        if not conv_id or not conv_id.startswith('widget_'):
            return None
        parts = conv_id.split('_', 2)
        return parts[2] if len(parts) == 3 else None

    async def session_ended(self, event):
        """Forward `session_ended` events to the visitor iframe.

        Triggered when an agent (or future timeout job) closes the session
        on the backend. The iframe uses this signal to swap its message
        composer for the post-chat review form.
        """
        # Filter so frames for OTHER widget sessions on the same tenant
        # group don't reach this iframe.
        msg_session = event.get('session_id')
        if msg_session and msg_session != self.session_id:
            return
        await self.send(text_data=json.dumps({
            'type': 'session_ended',
            'ended_by': event.get('ended_by'),
            'message': event.get('message', ''),
            'session_id': self.session_id,
            'ended_at': event.get('ended_at'),
        }))


class TypingConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for typing indicators.
    Handles typing start/stop events for conversations.
    """

    async def connect(self):
        self.tenant_schema = self.scope['url_route']['kwargs']['tenant_schema']
        self.conversation_id = self.scope['url_route']['kwargs']['conversation_id']
        self.user = self.scope.get('user', AnonymousUser())

        print(f"[TypingWebSocket] Connection attempt - Tenant: {self.tenant_schema}, Conversation: {self.conversation_id}, User: {self.user}, Is Anonymous: {self.user.is_anonymous}")

        # Only allow authenticated users
        if self.user.is_anonymous:
            print(f"[TypingWebSocket] Rejecting connection - User not authenticated")
            # Send error message before closing
            await self.accept()
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Authentication required',
                'code': 'UNAUTHENTICATED'
            }))
            await self.close(code=4001)
            return

        # Join the typing group for this conversation
        self.typing_group_name = f'typing_{self.tenant_schema}_{self.conversation_id}'

        await self.channel_layer.group_add(
            self.typing_group_name,
            self.channel_name
        )

        await self.accept()

        print(f"[TypingWebSocket] Connection accepted for user {self.user.email}")

        # Send initial connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection',
            'status': 'connected',
            'conversation_id': self.conversation_id
        }))
    
    async def disconnect(self, close_code):
        # Leave the typing group and notify others that user stopped typing
        if hasattr(self, 'typing_group_name'):
            await self.channel_layer.group_send(
                self.typing_group_name,
                {
                    'type': 'user_stopped_typing',
                    'user_id': str(self.user.id) if not self.user.is_anonymous else 'anonymous',
                    'timestamp': asyncio.get_event_loop().time()
                }
            )
            
            await self.channel_layer.group_discard(
                self.typing_group_name,
                self.channel_name
            )
    
    async def receive(self, text_data):
        """Handle typing events from client"""
        try:
            data = json.loads(text_data)
            message_type = data.get('type')

            if message_type == 'ping':
                # Respond to ping with pong for connection health check
                await self.send(text_data=json.dumps({
                    'type': 'pong',
                    'timestamp': data.get('timestamp')
                }))
                print('[TypingWebSocket] Pong sent')

            elif message_type == 'typing_start':
                # Broadcast typing start to other users in conversation
                await self.channel_layer.group_send(
                    self.typing_group_name,
                    {
                        'type': 'user_started_typing',
                        'user_id': str(self.user.id),
                        'user_name': f"{self.user.first_name} {self.user.last_name}".strip() or self.user.email,
                        'timestamp': asyncio.get_event_loop().time()
                    }
                )

            elif message_type == 'typing_stop':
                # Broadcast typing stop to other users in conversation
                await self.channel_layer.group_send(
                    self.typing_group_name,
                    {
                        'type': 'user_stopped_typing',
                        'user_id': str(self.user.id),
                        'timestamp': asyncio.get_event_loop().time()
                    }
                )

        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON format'
            }))
    
    # Handlers for typing events
    async def user_started_typing(self, event):
        """Send typing start notification to WebSocket"""
        # Don't send the event back to the user who triggered it
        if event['user_id'] != str(self.user.id):
            await self.send(text_data=json.dumps({
                'type': 'typing_start',
                'user_id': event['user_id'],
                'user_name': event.get('user_name', 'Unknown'),
                'timestamp': event['timestamp']
            }))
    
    async def user_stopped_typing(self, event):
        """Send typing stop notification to WebSocket"""
        # Don't send the event back to the user who triggered it
        if event['user_id'] != str(self.user.id):
            await self.send(text_data=json.dumps({
                'type': 'typing_stop',
                'user_id': event['user_id'],
                'timestamp': event['timestamp']
            }))


# Utility functions for sending WebSocket messages from Django views
async def send_new_message_notification(tenant_schema, conversation_id, message_data):
    """
    Send new message notification to all connected clients.
    Call this from views when a new message is received or sent.
    """
    from channels.layers import get_channel_layer
    
    channel_layer = get_channel_layer()
    
    # Send to general messages group
    await channel_layer.group_send(
        f'messages_{tenant_schema}',
        {
            'type': 'new_message',
            'message': message_data,
            'conversation_id': conversation_id,
            'timestamp': message_data.get('timestamp')
        }
    )
    
    # Send to specific conversation group
    await channel_layer.group_send(
        f'conversation_{tenant_schema}_{conversation_id}',
        {
            'type': 'new_message',
            'message': message_data,
            'conversation_id': conversation_id,
            'timestamp': message_data.get('timestamp')
        }
    )


async def send_conversation_update(tenant_schema, conversation_id, last_message_data):
    """
    Send conversation update notification.
    Call this when a conversation's last message changes.
    """
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()

    await channel_layer.group_send(
        f'messages_{tenant_schema}',
        {
            'type': 'conversation_update',
            'conversation_id': conversation_id,
            'last_message': last_message_data,
            'timestamp': last_message_data.get('timestamp')
        }
    )


# ---------------------------------------------------------------------------
# Cross-user reactivity broadcasts for /messages-beta.
#
# These are deliberately additive — the legacy /messages frontend ignores
# unknown `type` values via the switch default, so it's safe to start
# emitting these even before any clients consume them. Each helper wraps
# `group_send` in a try/except so a Channels failure (Redis down, channel
# layer mis-config) can't break the REST mutation that called it.
# ---------------------------------------------------------------------------
import logging  # noqa: E402  (kept inline so the helpers below are self-contained)
_realtime_logger = logging.getLogger(__name__)


async def _safe_group_send(tenant_schema, payload):
    from channels.layers import get_channel_layer
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return  # Channel layer not configured (some test envs) — swallow.
        await channel_layer.group_send(f'messages_{tenant_schema}', payload)
    except Exception as exc:  # noqa: BLE001 — broadcast is best-effort.
        _realtime_logger.warning(
            'Cross-user broadcast failed for tenant=%s type=%s: %s',
            tenant_schema, payload.get('type'), exc,
        )


async def send_assignment_update(
    tenant_schema,
    *,
    platform,
    conversation_id,
    account_id,
    assigned_user_id,
    assigned_user_name=None,
    status,
    session_started_at=None,
    session_ended_at=None,
    by_user_id=None,
):
    """Broadcast a chat-assignment change. Consumed by /messages-beta clients
    so every connected agent's sidebar reorders/hides the chat live.
    """
    from datetime import datetime, timezone

    await _safe_group_send(tenant_schema, {
        'type': 'assignment_update',
        'platform': platform,
        'conversation_id': conversation_id,
        'account_id': account_id,
        'assigned_user_id': assigned_user_id,
        'assigned_user_name': assigned_user_name,
        'status': status,
        'session_started_at': session_started_at,
        'session_ended_at': session_ended_at,
        'by_user_id': by_user_id,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })


async def send_read_state_update(
    tenant_schema,
    *,
    platform,
    conversation_id,
    account_id,
    unread_count,
    last_read_at=None,
    by_user_id=None,
):
    """Broadcast a read/unread state change for one conversation."""
    from datetime import datetime, timezone

    await _safe_group_send(tenant_schema, {
        'type': 'read_state_update',
        'platform': platform,
        'conversation_id': conversation_id,
        'account_id': account_id,
        'unread_count': unread_count,
        'last_read_at': last_read_at,
        'by_user_id': by_user_id,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })


async def send_archive_update(
    tenant_schema,
    *,
    platform,
    conversation_id,
    account_id,
    archived,
    archived_at=None,
    by_user_id=None,
):
    """Broadcast that a conversation moved into / out of the archive."""
    from datetime import datetime, timezone

    await _safe_group_send(tenant_schema, {
        'type': 'archive_update',
        'platform': platform,
        'conversation_id': conversation_id,
        'account_id': account_id,
        'archived': bool(archived),
        'archived_at': archived_at,
        'by_user_id': by_user_id,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })


async def send_conversation_deleted(
    tenant_schema,
    *,
    platform,
    conversation_id,
    account_id=None,
    by_user_id=None,
):
    """Broadcast that a conversation has been deleted.

    `conversation_id=None` is the bulk variant from clear_platform_history
    — it instructs beta clients to drop every conversation on the platform
    in one frame instead of N per-conversation messages.

    Legacy /messages ignores `conversation_deleted` via the switch-default
    branch, so this stays purely additive on the wire.
    """
    from datetime import datetime, timezone

    await _safe_group_send(tenant_schema, {
        'type': 'conversation_deleted',
        'platform': platform,
        'conversation_id': conversation_id,
        'account_id': account_id,
        'by_user_id': by_user_id,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })


async def send_message_status_update(
    tenant_schema,
    *,
    platform,
    conversation_id,
    account_id,
    message_ids,
    status,
):
    """Broadcast a batch message-status change (delivered / read).

    `message_ids` are the DB ids (FB/IG) or platform message ids (WA) of
    the messages whose status flipped; `status` is 'delivered' or 'read'.
    /messages-beta resolves the chat from (platform, account_id,
    conversation_id) and upgrades the matching bubbles' status pips.

    Additive — legacy /messages ignores `message_status` via its switch
    default, so this is safe to emit unconditionally.
    """
    from datetime import datetime, timezone

    await _safe_group_send(tenant_schema, {
        'type': 'message_status_update',
        'platform': platform,
        'conversation_id': conversation_id,
        'account_id': account_id,
        'message_ids': list(message_ids or []),
        'status': status,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })


async def send_reaction_update(
    tenant_schema,
    *,
    platform,
    conversation_id,
    account_id,
    message_id,
    reaction_emoji,
):
    """Broadcast a reaction add/remove on a single message.

    `reaction_emoji` is null/empty when the reaction was removed. Beta
    patches the matching bubble's reactionEmoji; legacy ignores the
    unknown `reaction_update` type.
    """
    from datetime import datetime, timezone

    await _safe_group_send(tenant_schema, {
        'type': 'reaction_update',
        'platform': platform,
        'conversation_id': conversation_id,
        'account_id': account_id,
        'message_id': message_id,
        'reaction_emoji': reaction_emoji,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })
