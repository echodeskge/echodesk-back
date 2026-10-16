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
    recent = Notification.objects.filter(
        user=user,
        notification_type=notification_type,
        ticket_id=ticket_id,
        created_at__gte=timezone.now() - timedelta(seconds=30),
    ).first()

    if recent:
        # Update the existing notification instead of creating a new one
        batch_count = recent.metadata.get('batch_count', 1) + 1
        recent.message = f"{message} (+{batch_count - 1} more)"
        recent.metadata['batch_count'] = batch_count
        recent.metadata.update(metadata)
        recent.is_read = False  # Re-mark as unread
        if link_url:
            recent.link_url = link_url
        recent.save(update_fields=['message', 'metadata', 'is_read', 'link_url'])
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
                # Chat-style notifications (team chat, social inbox) carry the
                # thread + sender so a push tap can deep-link straight into it.
                'conversation_id': notification.metadata.get('conversation_id'),
                'sender_id': notification.metadata.get('sender_id'),
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

    Returns the Notification (or None when the recipient has in-app
    notifications for this type disabled or there is no recipient).
    """
    sender = message.sender
    if recipient is None:
        recipient = message.conversation.get_other_participant(sender)
    if recipient is None or recipient.id == sender.id:
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
# Social message notification helper
# ---------------------------------------------------------------------------

def create_social_message_notification(platform, sender_name, message_text,
                                       conversation_id, sender_id,
                                       account_id=None,
                                       assigned_user_id=None):
    """
    Create a 'message_received' notification for an incoming social message.

    If the conversation is assigned to a specific user, only that user is
    notified and the link is deep-linked into the "Assigned to me" tab.
    Otherwise, up to 10 active users are notified and the link opens the
    "All chats" tab.

    `account_id` is the platform-side account identifier (Facebook page_id,
    Instagram instagram_account_id, or WhatsApp waba_id). Together with
    sender_id it's used to build the frontend chat ID (fb_<page>_<sender>,
    ig_<account>_<sender>, wa_<waba>_<phone>) so the notification click
    opens the exact chat rather than the messages index.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    title = f"New {platform} message from {sender_name}"
    msg = (message_text or '')[:100]
    meta = {
        'platform': platform,
        'conversation_id': conversation_id,
        'sender_name': sender_name,
        'sender_id': sender_id,
        'account_id': account_id,
    }

    # Build the chat ID in the exact format the frontend routes expect.
    chat_id = None
    if account_id and sender_id:
        platform_lower = (platform or '').lower()
        if platform_lower == 'facebook':
            chat_id = f'fb_{account_id}_{sender_id}'
        elif platform_lower == 'instagram':
            chat_id = f'ig_{account_id}_{sender_id}'
        elif platform_lower == 'whatsapp':
            # Frontend strips the leading + from phone numbers when building
            # the chat ID, so match that here.
            normalized = str(sender_id).lstrip('+')
            chat_id = f'wa_{account_id}_{normalized}'

    # Fallback: if we can't build a proper chat ID, keep the user at the
    # messages index rather than linking to a broken deep link.
    base_link = f'/messages/{chat_id}' if chat_id else '/messages'

    if assigned_user_id:
        try:
            user = User.objects.get(id=assigned_user_id, is_active=True)
            create_notification(
                user=user,
                notification_type='message_received',
                title=title,
                message=msg,
                ticket_id=None,
                metadata=meta,
                link_url=f'{base_link}?tab=assigned',
            )
        except User.DoesNotExist:
            pass
    else:
        # No assignment — notify up to 10 active users. For each of them the
        # chat is NOT assigned to them personally, so open the "All chats" tab.
        users = User.objects.filter(is_active=True)[:10]
        for user in users:
            create_notification(
                user=user,
                notification_type='message_received',
                title=title,
                message=msg,
                ticket_id=None,
                metadata=meta,
                link_url=base_link,
            )


def create_ai_handoff_notification(platform, conversation_id, account_id,
                                   sender_name, reason=''):
    """
    Notify agents that the AI companion escalated a conversation to a human.

    Audience mirrors create_social_message_notification: the assigned user
    if the chat is assigned, otherwise up to 10 active users. The link
    deep-links into the exact chat so the agent lands where the AI stopped.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    display_name = sender_name or 'a customer'
    title = f"AI needs a human: {platform} chat with {display_name}"
    msg = (reason or 'The AI assistant asked for a human to take over.')[:200]
    meta = {
        'platform': platform,
        'conversation_id': conversation_id,
        'account_id': account_id,
        'sender_name': sender_name,
        'reason': reason,
    }

    prefix_map = {
        'facebook': 'fb', 'instagram': 'ig', 'whatsapp': 'wa', 'telegram': 'tg',
    }
    prefix = prefix_map.get((platform or '').lower(), (platform or '').lower())
    chat_id = f'{prefix}_{account_id}_{conversation_id}' if account_id and conversation_id else None
    base_link = f'/messages/{chat_id}' if chat_id else '/messages'

    assigned_user_id = None
    try:
        from social_integrations.views import get_assignment_for_conversation
        assigned_user_id = get_assignment_for_conversation(
            platform, conversation_id, account_id
        )
    except Exception:
        pass

    if assigned_user_id:
        try:
            user = User.objects.get(id=assigned_user_id, is_active=True)
            create_notification(
                user=user,
                notification_type='ai_handoff_requested',
                title=title,
                message=msg,
                ticket_id=None,
                metadata=meta,
                link_url=f'{base_link}?tab=assigned',
            )
            return
        except User.DoesNotExist:
            pass

    for user in User.objects.filter(is_active=True)[:10]:
        create_notification(
            user=user,
            notification_type='ai_handoff_requested',
            title=title,
            message=msg,
            ticket_id=None,
            metadata=meta,
            link_url=base_link,
        )
