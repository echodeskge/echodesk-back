"""
Reusable notification utilities.

Centralises `create_notification` (previously in tickets/signals.py) so that
any module can import it without circular-import issues.  Also provides Redis-
backed caching helpers for unread counts (Phase 2).
"""

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from datetime import timedelta

# ---------------------------------------------------------------------------
# Redis cache helpers (Phase 2)
# ---------------------------------------------------------------------------

UNREAD_CACHE_KEY = 'notif_unread:{tenant}:{user_id}'
UNREAD_CACHE_TTL = 300  # 5 minutes


def _resolve_tenant(tenant_schema):
    """Return an explicit tenant schema or fall back to the current connection."""
    if tenant_schema:
        return tenant_schema
    return getattr(connection, 'schema_name', 'public')


def get_unread_count(user, tenant_schema=None):
    """Get unread count from Redis cache, falling back to DB."""
    tenant_schema = _resolve_tenant(tenant_schema)
    cache_key = UNREAD_CACHE_KEY.format(tenant=tenant_schema, user_id=user.id)
    count = cache.get(cache_key)
    if count is None:
        from users.models import Notification
        count = Notification.objects.filter(user=user, is_read=False).count()
        cache.set(cache_key, count, UNREAD_CACHE_TTL)
    return count


def increment_unread(user, tenant_schema=None):
    """Increment cached unread count."""
    tenant_schema = _resolve_tenant(tenant_schema)
    cache_key = UNREAD_CACHE_KEY.format(tenant=tenant_schema, user_id=user.id)
    try:
        cache.incr(cache_key)
    except ValueError:
        # Key doesn't exist yet — seed from DB
        get_unread_count(user, tenant_schema)


def decrement_unread(user, tenant_schema=None):
    """Decrement cached unread count."""
    tenant_schema = _resolve_tenant(tenant_schema)
    cache_key = UNREAD_CACHE_KEY.format(tenant=tenant_schema, user_id=user.id)
    try:
        new_val = cache.decr(cache_key)
        if new_val < 0:
            cache.set(cache_key, 0, UNREAD_CACHE_TTL)
    except ValueError:
        get_unread_count(user, tenant_schema)


def reset_unread(user, tenant_schema=None):
    """Reset cached unread count to 0 (e.g. mark-all-read)."""
    tenant_schema = _resolve_tenant(tenant_schema)
    cache_key = UNREAD_CACHE_KEY.format(tenant=tenant_schema, user_id=user.id)
    cache.set(cache_key, 0, UNREAD_CACHE_TTL)


# ---------------------------------------------------------------------------
# Core notification creator (moved from tickets/signals.py)
# ---------------------------------------------------------------------------

def create_notification(user, notification_type, title, message, ticket_id=None,
                        metadata=None, link_url=''):
    """
    Create a Notification row, broadcast it over WebSocket, and send a Web Push.

    Includes duplicate / batching logic (Phase 5): within a 30-second window for
    the same user + type + ticket, the existing notification is updated rather
    than creating a duplicate.
    """
    from users.models import Notification, NotificationPreference
    from users.consumers import send_notification_to_user
    from asgiref.sync import async_to_sync
    from django.utils.timesince import timesince

    if metadata is None:
        metadata = {}

    tenant_schema = getattr(connection, 'schema_name', 'public')

    # --- Check user notification preferences ---
    pref = NotificationPreference.objects.filter(
        user=user, notification_type=notification_type
    ).first()

    # Default: all enabled
    should_create_in_app = pref.in_app if pref else True
    should_push = pref.push if pref else True
    sound_enabled = pref.sound if pref else True

    if not should_create_in_app:
        return None  # User disabled this notification type

    # --- Phase 5: batching / debounce (30-second window) ---
    recent_filter = {
        'user': user,
        'notification_type': notification_type,
        'ticket_id': ticket_id,
        'created_at__gte': timezone.now() - timedelta(seconds=30),
    }
    # Chat-style notifications batch per thread, not per type: a message from
    # a second colleague inside the window must not be folded into the first
    # colleague's row (it would keep the first title while message, metadata
    # and link moved to the second one).
    if metadata.get('conversation_id') is not None:
        recent_filter['metadata__conversation_id'] = metadata['conversation_id']
    # Likewise two different missed calls must stay two notifications.
    if metadata.get('call_log_id') is not None:
        recent_filter['metadata__call_log_id'] = metadata['call_log_id']
    # Same for bookings: two different bookings changing inside the window
    # are two notifications, each linking to its own booking.
    if metadata.get('booking_id') is not None:
        recent_filter['metadata__booking_id'] = metadata['booking_id']
    recent = Notification.objects.filter(**recent_filter).first()

    if recent:
        # Update the existing notification instead of creating a new one
        batch_count = recent.metadata.get('batch_count', 1) + 1
        recent.title = title
        recent.message = f"{message} (+{batch_count - 1} more)"
        recent.metadata['batch_count'] = batch_count
        recent.metadata.update(metadata)
        recent.is_read = False  # Re-mark as unread
        if link_url:
            recent.link_url = link_url
        recent.save(update_fields=['title', 'message', 'metadata', 'is_read', 'link_url'])
        notification = recent
    else:
        notification = Notification.objects.create(
            user=user,
            notification_type=notification_type,
            title=title,
            message=message,
            ticket_id=ticket_id,
            link_url=link_url,
            metadata=metadata,
        )
        # Increment cached unread count
        increment_unread(user, tenant_schema)

    # --- Build unread count (from cache) ---
    unread_count = get_unread_count(user, tenant_schema)

    # --- WebSocket broadcast ---
    notification_data = {
        'id': notification.id,
        'notification_type': notification.notification_type,
        'title': notification.title,
        'message': notification.message,
        'ticket_id': notification.ticket_id,
        'link_url': notification.link_url,
        'metadata': notification.metadata,
        'is_read': notification.is_read,
        'created_at': notification.created_at.isoformat(),
        'read_at': notification.read_at.isoformat() if notification.read_at else None,
        'time_ago': timesince(notification.created_at),
        'user': notification.user.id,
        'user_name': notification.user.get_full_name() or notification.user.email,
        'sound_enabled': sound_enabled,
    }

    try:
        async_to_sync(send_notification_to_user)(
            tenant_schema=tenant_schema,
            user_id=user.id,
            notification_data=notification_data,
            unread_count=unread_count,
        )
    except Exception as e:
        print(f"[notification_utils] Error broadcasting via WebSocket: {e}")

    # --- Push (Web Push + Expo, only if user has push enabled) ---
    if should_push:
        nav_url = link_url or (f'/tickets/{ticket_id}' if ticket_id else '/')
        _dispatch_push(
            user=user,
            tenant_schema=tenant_schema,
            title=title,
            body=message,
            data={
                'notification_id': notification.id,
                'notification_type': notification_type,
                'link_url': notification.link_url,
                'url': nav_url,
                'ticket_id': ticket_id,
                'leave_request_id': notification.metadata.get('leave_request_id'),
                'booking_id': notification.metadata.get('booking_id'),
                # Chat-style notifications (team chat, social inbox) carry the
                # thread + sender so a push tap can deep-link straight into it.
                'conversation_id': notification.metadata.get('conversation_id'),
                'sender_id': notification.metadata.get('sender_id'),
                # Missed-call pushes open the call detail.
                'call_log_id': notification.metadata.get('call_log_id'),
                # Social inbox pushes: lowercase platform key, platform-side
                # account id and the prefixed inbox chat id (fb_/ig_/wa_/
                # tg_/widget_<account>_<key>) so a tap needs no URL parsing.
                'platform': (
                    notification.metadata.get('platform_key')
                    or notification.metadata.get('platform')
                ),
                'account_id': notification.metadata.get('account_id'),
                'chat_id': notification.metadata.get('chat_id'),
            },
            url=nav_url,
            tag=f'echodesk-{ticket_id or notification.id}',
            badge=unread_count,
        )

    return notification


def _dispatch_push(user, tenant_schema, title, body, data, url, tag, badge):
    """
    Hand push delivery (Web Push + Expo) to the Celery task.

    Skips the enqueue entirely when the user has nothing to deliver to, so
    the broker is not hit for every notification of users without a browser
    subscription or a registered phone. If the broker is unreachable the task
    is executed inline so a Redis blip never loses a push.
    """
    from notifications.models import DevicePushToken, PushSubscription
    from notifications.tasks import deliver_push_notification_task

    has_web = PushSubscription.objects.filter(user=user, is_active=True).exists()
    has_device = DevicePushToken.objects.filter(user=user, is_active=True).exists()
    if not has_web and not has_device:
        return

    kwargs = dict(
        tenant_schema=tenant_schema,
        user_id=user.id,
        title=title,
        body=body,
        data=data,
        url=url,
        tag=tag,
        badge=badge,
    )

    try:
        deliver_push_notification_task.delay(**kwargs)
    except Exception as e:
        if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
            # Eager mode already ran the task inline; the exception is the
            # task's own failure (propagates=True), not a broker problem.
            print(f"[notification_utils] Push delivery failed: {e}")
            return
        # Broker down — deliver now rather than silently dropping the push.
        print(f"[notification_utils] Push enqueue failed ({e}); delivering inline")
        try:
            deliver_push_notification_task.apply(kwargs=kwargs)
        except Exception as inline_err:
            print(f"[notification_utils] Inline push delivery failed: {inline_err}")


# ---------------------------------------------------------------------------
# Team chat notification helper
# ---------------------------------------------------------------------------

TEAM_CHAT_PREVIEW_LENGTH = 120

# A recipient with an open team-chat socket gets the message live over that
# socket (the dashboard widget plays its own sound, the mobile app updates the
# thread), so no in-app/push notification is created for them. Presence is
# ``UserOnlineStatus.is_online``, which the consumer sets on connect/disconnect
# and refreshes (``last_seen``) on the client's periodic ping. A row that has
# not been refreshed within this window is treated as stale — e.g. a server
# restart that skipped the disconnect handler — so the notification is sent
# rather than lost.
TEAM_CHAT_PRESENCE_STALE_AFTER = timedelta(minutes=2)


def has_open_team_chat_socket(user):
    """
    True when ``user`` currently has a live team-chat websocket (one query).

    See ``TEAM_CHAT_PRESENCE_STALE_AFTER`` for how a stale "online" row is
    handled.
    """
    from users.models import UserOnlineStatus

    return UserOnlineStatus.objects.filter(
        user=user,
        is_online=True,
        last_seen__gte=timezone.now() - TEAM_CHAT_PRESENCE_STALE_AFTER,
    ).exists()


def team_chat_message_preview(message):
    """Short, human-readable preview of a team chat message for notifications."""
    text = (message.text or '').strip()
    if message.message_type == 'image':
        return text[:TEAM_CHAT_PREVIEW_LENGTH] if text else 'Sent a photo'
    if message.message_type == 'voice':
        return 'Sent a voice message'
    if message.message_type == 'file':
        name = message.file_name or ''
        return f'Sent a file: {name}'[:TEAM_CHAT_PREVIEW_LENGTH] if name else 'Sent a file'
    return text[:TEAM_CHAT_PREVIEW_LENGTH]


def create_team_chat_message_notification(message, recipient=None):
    """
    Notify the other participant of a new team chat message.

    Goes through ``create_notification`` so the recipient gets the in-app row,
    the notifications websocket event and the push (Web Push + Expo) with the
    same preference handling as every other notification type
    (``NotificationPreference`` for ``team_chat_message``). The notification
    links to the sender's thread (``/team-chat?user=<sender_id>``) and the
    metadata/push data carry ``conversation_id`` and ``sender_id`` so a mobile
    push tap can open the exact conversation.

    Skipped when the recipient has an open team-chat socket: they receive the
    ``new_message`` frame live and the dashboard widget already alerts on it,
    so a notification would only duplicate the sound/toast/bell row.

    Returns the Notification (or None when the recipient is online, has
    in-app notifications for this type disabled, or there is no recipient).
    """
    sender = message.sender
    if recipient is None:
        recipient = message.conversation.get_other_participant(sender)
    if recipient is None or recipient.id == sender.id:
        return None

    if has_open_team_chat_socket(recipient):
        return None

    title = sender.get_full_name() or sender.email
    return create_notification(
        user=recipient,
        notification_type='team_chat_message',
        title=title,
        message=team_chat_message_preview(message),
        ticket_id=None,
        metadata={
            'conversation_id': message.conversation_id,
            'sender_id': sender.id,
            'sender_name': title,
            'message_id': message.id,
            'message_type': message.message_type,
        },
        link_url=f'/team-chat?user={sender.id}',
    )


# ---------------------------------------------------------------------------
# Social message notification helpers
# ---------------------------------------------------------------------------

SOCIAL_CHAT_PREFIXES = {
    'facebook': 'fb',
    'instagram': 'ig',
    'whatsapp': 'wa',
    'telegram': 'tg',
    'widget': 'widget',
    'email': 'email',
    'tiktok': 'tiktok',
}


def users_with_feature(feature_key):
    """
    Active users of the current tenant who have ``feature_key``.

    Same rule as ``User.has_feature`` (the tenant's subscription must include
    the feature; superusers get every subscribed feature, everyone else needs
    an active group that grants it), resolved in two queries instead of
    several per user.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    from tenants.models import Tenant

    User = get_user_model()
    try:
        tenant = Tenant.objects.get(schema_name=connection.schema_name)
        subscription = tenant.current_subscription
        if not subscription or not subscription.is_active:
            return User.objects.none()
        if not subscription.selected_features.filter(key=feature_key, is_active=True).exists():
            return User.objects.none()
    except Exception:
        return User.objects.none()

    return User.objects.filter(is_active=True).filter(
        Q(is_superuser=True)
        | Q(
            tenant_groups__is_active=True,
            tenant_groups__features__key=feature_key,
            tenant_groups__features__is_active=True,
        )
    ).distinct()


def build_social_chat_id(platform, account_id, conversation_key):
    """Prefixed inbox chat id (``fb_<page>_<psid>``, ``wa_<waba>_<phone>``,
    ``tg_<account>_<peer>``, ``widget_<conn>_<session>`` …) or None."""
    platform_lower = (platform or '').lower()
    prefix = SOCIAL_CHAT_PREFIXES.get(platform_lower)
    if not prefix or not account_id or not conversation_key:
        return None
    key = str(conversation_key)
    if platform_lower == 'whatsapp':
        # The inbox strips the leading + from phone numbers in chat ids.
        key = key.lstrip('+')
    return f'{prefix}_{account_id}_{key}'


def social_message_recipients(assigned_user_id=None):
    """
    Who is told about a new customer message.

    An assigned chat notifies only its agent. An unassigned chat is visible
    to every agent (hide_assigned_chats only hides chats assigned to someone
    else), so it notifies every active user who has the social inbox
    (``social_integrations``) — no arbitrary cap, no users without access.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    if assigned_user_id:
        return list(User.objects.filter(id=assigned_user_id, is_active=True))
    return list(users_with_feature('social_integrations'))


def create_social_message_notification(platform, sender_name, message_text,
                                       conversation_id, sender_id,
                                       account_id=None,
                                       assigned_user_id=None,
                                       chat_conversation_key=None):
    """
    Create a 'message_received' notification for an incoming social message.

    Recipients: see ``social_message_recipients``. The link deep-links into
    the exact chat: ``/messages/<chat_id>`` (``?tab=assigned`` for the
    assigned agent) where chat_id is the inbox's prefixed id built from
    ``account_id`` and ``chat_conversation_key`` (defaults to
    ``sender_id``; widget passes the session id).

    Metadata (also copied into the push ``data``): platform (display name,
    e.g. 'Facebook'), platform_key (lowercase), conversation_id (unprefixed
    key mark-read clears by), chat_id, sender_id, sender_name, account_id.
    """
    title = f"New {platform} message from {sender_name}"
    msg = (message_text or '')[:100]
    chat_id = build_social_chat_id(platform, account_id, chat_conversation_key or sender_id)
    meta = {
        'platform': platform,
        'platform_key': (platform or '').lower(),
        'conversation_id': conversation_id,
        'chat_id': chat_id,
        'sender_name': sender_name,
        'sender_id': sender_id,
        'account_id': account_id,
    }

    # Fallback: if we can't build a proper chat ID, keep the user at the
    # messages index rather than linking to a broken deep link.
    base_link = f'/messages/{chat_id}' if chat_id else '/messages'
    link = f'{base_link}?tab=assigned' if assigned_user_id else base_link

    created = []
    for user in social_message_recipients(assigned_user_id):
        notification = create_notification(
            user=user,
            notification_type='message_received',
            title=title,
            message=msg,
            ticket_id=None,
            metadata=dict(meta),
            link_url=link,
        )
        if notification is not None:
            created.append(notification)
    return created


def create_ai_handoff_notification(platform, conversation_id, account_id,
                                   sender_name, reason=''):
    """
    Notify agents that the AI companion escalated a conversation to a human.

    Audience mirrors create_social_message_notification: the assigned user
    if the chat is assigned, otherwise every active user with the social
    inbox. The link deep-links into the exact chat so the agent lands where
    the AI stopped.
    """
    display_name = sender_name or 'a customer'
    title = f"AI needs a human: {platform} chat with {display_name}"
    msg = (reason or 'The AI assistant asked for a human to take over.')[:200]
    chat_id = build_social_chat_id(platform, account_id, conversation_id)
    meta = {
        'platform': platform,
        'platform_key': (platform or '').lower(),
        'conversation_id': conversation_id,
        'chat_id': chat_id,
        'account_id': account_id,
        'sender_name': sender_name,
        'reason': reason,
    }
    base_link = f'/messages/{chat_id}' if chat_id else '/messages'

    assigned_user_id = None
    try:
        from social_integrations.views import get_assignment_for_conversation
        assigned_user_id = get_assignment_for_conversation(
            platform, conversation_id, account_id
        )
    except Exception:
        pass

    recipients = social_message_recipients(assigned_user_id)
    if assigned_user_id and not recipients:
        # Assigned agent deactivated: fall back to everyone with the inbox.
        recipients = social_message_recipients(None)
        assigned_user_id = None
    link = f'{base_link}?tab=assigned' if assigned_user_id else base_link

    for user in recipients:
        create_notification(
            user=user,
            notification_type='ai_handoff_requested',
            title=title,
            message=msg,
            ticket_id=None,
            metadata=dict(meta),
            link_url=link,
        )


def social_admin_recipients(permission='manage_social_settings'):
    """Users who administer social settings/connections: active users with
    the social inbox who are staff, superuser, role admin, or hold
    ``permission``."""
    return [
        user for user in users_with_feature('social_integrations')
        if user.is_staff or user.is_superuser or user.role == 'admin'
        or user.has_permission(permission)
    ]
