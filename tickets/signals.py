"""
Signals for automatic notification creation on ticket events.
"""
from django.db.models import Q
from django.db.models.signals import post_save, post_delete, pre_save, m2m_changed
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from django.core.serializers.json import DjangoJSONEncoder
from .models import (
    Ticket,
    TicketComment,
    TicketAssignment,
    TicketAttachment,
    TicketHistory,
)
import json
import re
from asgiref.sync import async_to_sync
from django.db import connection
from tenant_schemas.utils import schema_context

User = get_user_model()


def _tenant_schema():
    """Schema name of the tenant the signal is firing in (board groups are per tenant)."""
    return getattr(connection, 'schema_name', 'public')


def _json_safe(data):
    """
    Round-trip through JSON so the payload only holds plain dicts/lists/str/
    numbers: DRF's ReturnDict, Decimal and datetime objects are not channel-
    layer (msgpack) safe.
    """
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


def _ticket_card_payload(ticket):
    """Compact card for board broadcasts — same shape as kanban_board?lite=1."""
    from .serializers import TicketCardSerializer
    return _json_safe(TicketCardSerializer(ticket).data)


def _comment_payload(comment):
    """Minimal comment dict for the ticket_commented broadcast."""
    user = comment.user
    return _json_safe({
        'id': comment.id,
        'comment': comment.comment,
        'created_at': comment.created_at,
        'user': {
            'id': user.id,
            'name': user.get_full_name(),
            'email': user.email,
        } if user else None,
    })


def create_notification(user, notification_type, title, message, ticket_id=None, metadata=None, link_url=''):
    """
    Thin wrapper that delegates to users.notification_utils.create_notification.

    Kept here so that existing call sites inside this module (and any external
    code that imports ``from tickets.signals import create_notification``) keep
    working without changes.
    """
    from users.notification_utils import create_notification as _create
    return _create(
        user=user,
        notification_type=notification_type,
        title=title,
        message=message,
        ticket_id=ticket_id,
        metadata=metadata,
        link_url=link_url,
    )


# "@john.doe@example.com" (full e-mail, tried first so the inner "@" is not
# treated as a second mention) or "@john" / "@john.doe" (name-ish token).
MENTION_RE = re.compile(r'@([\w.+-]+@[\w-]+(?:\.[\w-]+)+|[\w.-]+)')


def extract_mentions(text):
    """
    Extract @mentions from text.
    Returns the list of mentioned e-mails / name tokens, in order of appearance.
    A trailing '.' (end of sentence) is not part of the mention.
    """
    mentions = []
    for mention in MENTION_RE.findall(text or ''):
        mention = mention.rstrip('.')
        if mention:
            mentions.append(mention)
    return mentions


def get_users_from_mentions(mentions):
    """
    Get User objects from list of mentioned usernames/emails.

    Single DB query for the whole mention list (was up to 3 queries per mention).
    Email (case-insensitive exact) is preferred; falls back to first_name / last_name
    icontains matches, same as the prior per-mention behaviour.
    """
    if not mentions:
        return []

    query = Q()
    for mention in mentions:
        query |= Q(email__iexact=mention)
        query |= Q(first_name__icontains=mention)
        query |= Q(last_name__icontains=mention)

    candidates = list(User.objects.filter(query).distinct()[:200])

    resolved = []
    used_ids = set()
    for mention in mentions:
        m_lower = mention.lower()
        match = None
        for u in candidates:
            if u.id in used_ids:
                continue
            if u.email and u.email.lower() == m_lower:
                match = u
                break
        if match is None:
            for u in candidates:
                if u.id in used_ids:
                    continue
                if (u.first_name and m_lower in u.first_name.lower()) or \
                   (u.last_name and m_lower in u.last_name.lower()):
                    match = u
                    break
        if match is not None:
            resolved.append(match)
            used_ids.add(match.id)
    return resolved


def _get_bug_report_status_key(column_name):
    """
    Map a column name to a bug-report status key.
    Returns None if the column doesn't match a tracked status.
    """
    name = column_name.lower()
    if 'selected' in name and 'dev' in name:
        return 'received'
    if name == 'in progress':
        return 'in_progress'
    if 'dev' in name and 'finished' in name:
        return 'fixed'
    return None


def send_bug_report_notification(instance, metadata, status_key):
    """
    Send a cross-tenant notification to the original bug reporter.
    Switches to the reporter's tenant to create the notification there.
    """
    reporter_tenant = metadata.get('reporter_tenant')
    reporter_email = metadata.get('reporter_email')
    if not reporter_tenant or not reporter_email:
        return

    status_titles = {
        'received': 'Bug report received',
        'in_progress': 'Bug fix in progress',
        'fixed': 'Bug report fixed',
    }
    status_messages = {
        'received': 'Your bug report has been received and is scheduled for development.',
        'in_progress': 'We are currently working on fixing your reported bug.',
        'fixed': 'Your reported bug has been fixed. Thank you for the report!',
    }

    title = status_titles.get(status_key, 'Bug report update')
    message = status_messages.get(status_key, 'Your bug report status has been updated.')

    try:
        with schema_context(reporter_tenant):
            reporter = User.objects.filter(email=reporter_email).first()
            if not reporter:
                print(f"[Signals] Bug report reporter {reporter_email} not found in tenant {reporter_tenant}")
                return

            create_notification(
                user=reporter,
                notification_type='bug_report_update',
                title=title,
                message=message,
                ticket_id=None,
                metadata={
                    'status': status_key,
                    'ticket_title': instance.title,
                },
            )
            print(f"[Signals] Bug report notification ({status_key}) sent to {reporter_email} in {reporter_tenant}")
    except Exception as e:
        print(f"[Signals] Error sending bug report notification: {str(e)}")


@receiver(post_save, sender=TicketAssignment)
def notify_on_ticket_assignment(sender, instance, created, **kwargs):
    """
    Notify user when they are assigned to a ticket.
    """
    if created:
        ticket = instance.ticket
        assigned_user = instance.user
        assigned_by = instance.assigned_by

        # Don't notify if user assigned themselves
        if assigned_by and assigned_user.id == assigned_by.id:
            return

        assigner_name = assigned_by.get_full_name() if assigned_by else "Someone"

        create_notification(
            user=assigned_user,
            notification_type='ticket_assigned',
            title=f'Assigned to: {ticket.title}',
            message=f'{assigner_name} assigned you to this ticket as {instance.get_role_display()}',
            ticket_id=ticket.id,
            metadata={
                'role': instance.role,
                'assigned_by': assigned_by.email if assigned_by else None,
            }
        )


@receiver(post_save, sender=TicketComment)
def notify_on_ticket_comment(sender, instance, created, **kwargs):
    """
    Notify ticket participants when a comment is added.
    Also handles @mentions in comments.
    """
    if not created:
        return

    ticket = instance.ticket
    commenter = instance.user

    # Extract mentions from comment
    mentions = extract_mentions(instance.comment)
    mentioned_users = get_users_from_mentions(mentions)

    # Notify mentioned users
    for mentioned_user in mentioned_users:
        if mentioned_user.id != commenter.id:
            create_notification(
                user=mentioned_user,
                notification_type='ticket_mentioned',
                title=f'Mentioned in: {ticket.title}',
                message=f'{commenter.get_full_name()} mentioned you in a comment',
                ticket_id=ticket.id,
                metadata={
                    'comment_id': instance.id,
                    'commenter': commenter.email,
                }
            )

    # Notify ticket creator if they're not the commenter
    if ticket.created_by.id != commenter.id:
        create_notification(
            user=ticket.created_by,
            notification_type='ticket_commented',
            title=f'New comment on: {ticket.title}',
            message=f'{commenter.get_full_name()} added a comment',
            ticket_id=ticket.id,
            metadata={
                'comment_id': instance.id,
                'commenter': commenter.email,
            }
        )

    # Notify assigned users (excluding commenter and mentioned users)
    assigned_users = ticket.assigned_users.exclude(id=commenter.id)
    mentioned_user_ids = [u.id for u in mentioned_users]
    assigned_users = assigned_users.exclude(id__in=mentioned_user_ids)

    for assigned_user in assigned_users:
        create_notification(
            user=assigned_user,
            notification_type='ticket_commented',
            title=f'New comment on: {ticket.title}',
            message=f'{commenter.get_full_name()} added a comment',
            ticket_id=ticket.id,
            metadata={
                'comment_id': instance.id,
                'commenter': commenter.email,
            }
        )

    # Broadcast the comment to everyone viewing the board so cards can bump
    # their comment count / open threads can append it live
    if ticket.column_id and ticket.column.board_id:
        try:
            from users.consumers import broadcast_ticket_commented

            async_to_sync(broadcast_ticket_commented)(
                tenant_schema=_tenant_schema(),
                board_id=ticket.column.board_id,
                ticket_id=ticket.id,
                comment=_comment_payload(instance),
                # Fresh DB count: the view's ticket may carry a stale `comments` prefetch
                comments_count=TicketComment.objects.filter(ticket_id=ticket.id).count(),
                user=commenter,
            )
        except Exception as e:
            print(f"[Signals] Error broadcasting ticket comment: {str(e)}")


@receiver(pre_save, sender=Ticket)
def capture_ticket_prev_state(sender, instance, **kwargs):
    """
    Single pre_save receiver that fetches the old ticket once and:
      - records changed fields into TicketHistory (was record_ticket_history)
      - caches column/position/department/field diffs onto the instance for post_save
        (was track_ticket_status_change)
      - caches the assigned user IDs so post_save doesn't fire a separate query per move

    Merged to eliminate the double `Ticket.objects.get(pk=...)` on every ticket write.
    """
    if not instance.pk:
        return  # create path

    try:
        old = Ticket.objects.prefetch_related('assigned_users').select_related(
            'column', 'assigned_department'
        ).get(pk=instance.pk)
    except Ticket.DoesNotExist:
        return

    # --- TicketHistory records ---
    current_user = getattr(instance, '_current_user', None)
    tracked_fields = [
        ('title', 'updated'),
        ('description', 'updated'),
        ('priority', 'priority_changed'),
        ('column_id', 'status_changed'),
        ('assigned_to_id', 'assigned'),
        ('assigned_department_id', 'updated'),
    ]
    for field, action in tracked_fields:
        old_val = getattr(old, field)
        new_val = getattr(instance, field)
        if old_val != new_val:
            TicketHistory.objects.create(
                ticket=instance,
                action=action,
                field_name=field,
                old_value=str(old_val) if old_val is not None else '',
                new_value=str(new_val) if new_val is not None else '',
                user=current_user,
            )

    # --- Column (status) change ---
    if old.column_id != instance.column_id:
        instance._column_changed = True
        instance._old_column_id = old.column_id
        instance._old_column_name = old.column.name if old.column else 'None'
        instance._new_column_name = instance.column.name if instance.column else 'None'

    # --- Position change ---
    if old.position_in_column != instance.position_in_column:
        instance._position_changed = True
        instance._old_position = old.position_in_column

    # --- Department change ---
    if old.assigned_department_id != instance.assigned_department_id:
        instance._department_changed = True
        instance._old_department = old.assigned_department
        instance._new_department = instance.assigned_department

    # --- Field-change map for broadcasting ---
    instance._field_changes = {}
    for field in ['title', 'priority', 'assigned_department_id']:
        old_value = getattr(old, field)
        new_value = getattr(instance, field)
        if old_value != new_value:
            instance._field_changes[field] = {'old': old_value, 'new': new_value}

    # --- Assigned user IDs cached for post_save notification loop ---
    instance._prev_assigned_user_ids = [u.id for u in old.assigned_users.all()]


def _send_new_ticket_telegram_notification(ticket):
    """Send Telegram notification when a new ticket is created on a board with a connection."""
    from .models import BoardTelegramConnection
    from .telegram_utils import send_board_telegram_message

    try:
        if not ticket.column or not ticket.column.board:
            return

        board = ticket.column.board
        try:
            conn = board.telegram_connection
        except BoardTelegramConnection.DoesNotExist:
            return

        if not conn.is_active:
            return

        bot_token = conn.get_bot_token()
        if not bot_token:
            return

        # Priority emoji mapping
        priority_emojis = {
            'critical': '🔴',
            'high': '🟠',
            'medium': '🟡',
            'low': '🟢',
        }
        priority = getattr(ticket, 'priority', None) or 'medium'
        priority_emoji = priority_emojis.get(priority, '⚪')

        # Bug reports are filed FROM another tenant into the groot bug board.
        # ticket.created_by is a synthetic system superuser there — surfacing
        # it on Telegram makes every bug look like it was filed by the owner.
        # Prefer the reporter info stashed in metadata by report_bug.
        metadata = ticket.metadata or {}
        is_bug_report = bool(metadata.get('bug_report'))

        def _esc(s: str) -> str:
            # Minimal HTML escape so user-supplied <, >, & don't make Telegram
            # reject the message (parse_mode='HTML').
            return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        creator_label = 'Unknown'
        if is_bug_report:
            reporter_name = (metadata.get('reporter_name') or '').strip()
            reporter_email = (metadata.get('reporter_email') or '').strip()
            reporter_tenant = (metadata.get('reporter_tenant') or '').strip()
            if reporter_name and reporter_email:
                creator_label = f'{reporter_name} ({reporter_email})'
            else:
                creator_label = reporter_email or reporter_name or 'Unknown'
            if reporter_tenant:
                creator_label = f'{creator_label} — tenant {reporter_tenant}'
        elif ticket.created_by:
            name = f'{ticket.created_by.first_name} {ticket.created_by.last_name}'.strip()
            creator_label = name or ticket.created_by.email

        schema_name = connection.schema_name
        ticket_url = f'https://{schema_name}.echodesk.ge/tickets/{ticket.id}'

        # Description block — only added when present. Telegram's HTML parse
        # mode rejects tags it doesn't recognise (<p>, <hr>, …), so for bug
        # reports we send the raw user-written description that report_bug
        # stashed in metadata; for everything else we omit (ticket.description
        # is rich-text and would render poorly without a sanitiser).
        description_text = ''
        if is_bug_report:
            description_text = (metadata.get('description') or '').strip()
        description_block = ''
        if description_text:
            # Truncate to keep the Telegram message under the 4096-char limit;
            # the full text is always available via the View Ticket link.
            if len(description_text) > 1500:
                description_text = description_text[:1500].rstrip() + '…'
            description_block = f"\n<b>Description:</b>\n{_esc(description_text)}\n"

        page_url = (metadata.get('page_url') or '').strip() if is_bug_report else ''
        page_url_block = f"\n🌐 <b>Page:</b> {_esc(page_url)}" if page_url else ''

        message = (
            f"📋 <b>New Ticket Created</b>\n\n"
            f"<b>Title:</b> {_esc(ticket.title)}\n"
            f"{priority_emoji} <b>Priority:</b> {priority.capitalize()}\n"
            f"👤 <b>Created by:</b> {_esc(creator_label)}\n"
            f"📌 <b>Board:</b> {_esc(board.name)}\n"
            f"📊 <b>Column:</b> {_esc(ticket.column.name)}"
            f"{page_url_block}\n"
            f"{description_block}"
            f"\n🔗 <a href=\"{ticket_url}\">View Ticket</a>"
        )

        send_board_telegram_message(bot_token, conn.chat_id, message)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Failed to send Telegram notification for new ticket")


@receiver(post_save, sender=TicketAttachment)
def send_attachment_to_telegram(sender, instance, created, **kwargs):
    """Send ticket attachments to Telegram when they are uploaded."""
    if not created:
        return

    from .models import BoardTelegramConnection
    from .telegram_utils import send_board_telegram_document, send_board_telegram_photo

    try:
        ticket = instance.ticket
        if not ticket.column or not ticket.column.board:
            return

        board = ticket.column.board
        try:
            conn = board.telegram_connection
        except BoardTelegramConnection.DoesNotExist:
            return

        if not conn.is_active:
            return

        bot_token = conn.get_bot_token()
        if not bot_token:
            return

        file_url = instance.file.url
        caption = f"📎 <b>{instance.filename}</b>\nTicket: {ticket.title}"

        content_type = instance.content_type or ''
        if content_type.startswith('image/'):
            send_board_telegram_photo(bot_token, conn.chat_id, file_url, caption=caption)
        else:
            send_board_telegram_document(bot_token, conn.chat_id, file_url, caption=caption)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Failed to send Telegram attachment notification")


@receiver(post_save, sender=Ticket)
def notify_on_ticket_status_change(sender, instance, created, **kwargs):
    """
    Notify assigned users when ticket status (column) changes.
    """
    if created:
        _send_new_ticket_telegram_notification(instance)

        # Broadcast the new card to everyone viewing the board
        if instance.column_id and instance.column.board_id:
            try:
                from users.consumers import broadcast_ticket_created

                async_to_sync(broadcast_ticket_created)(
                    tenant_schema=_tenant_schema(),
                    board_id=instance.column.board_id,
                    ticket_data=_ticket_card_payload(instance),
                    created_by=instance.created_by,
                )
            except Exception as e:
                print(f"[Signals] Error broadcasting ticket creation: {str(e)}")
        return

    # Check if column was changed (set in pre_save)
    if getattr(instance, '_column_changed', False):
        old_column_name = getattr(instance, '_old_column_name', 'Unknown')
        new_column_name = getattr(instance, '_new_column_name', 'Unknown')

        # Use the assigned user IDs cached by pre_save so we don't run a fresh
        # query here on every column change. Fall back to a live read if the
        # cache isn't there (e.g. for instances that skipped pre_save).
        assigned_user_ids = getattr(instance, '_prev_assigned_user_ids', None)
        assigned_users = (
            User.objects.filter(id__in=assigned_user_ids)
            if assigned_user_ids is not None
            else instance.assigned_users.all()
        )
        for assigned_user in assigned_users:
            create_notification(
                user=assigned_user,
                notification_type='ticket_status_changed',
                title=f'Status changed: {instance.title}',
                message=f'Ticket status changed from "{old_column_name}" to "{new_column_name}"',
                ticket_id=instance.id,
                metadata={
                    'old_status': old_column_name,
                    'new_status': new_column_name,
                }
            )

        # Cross-tenant bug report notification
        ticket_metadata = getattr(instance, 'metadata', None) or {}
        if (
            ticket_metadata.get('bug_report')
            and instance.column
            and instance.column.board
            and instance.column.board.name.lower() == 'echodesk'
        ):
            status_key = _get_bug_report_status_key(new_column_name)
            if status_key:
                send_bug_report_notification(instance, ticket_metadata, status_key)

        # Broadcast ticket movement to all users viewing the board
        if instance.column and instance.column.board_id:
            try:
                from users.consumers import broadcast_ticket_moved
                old_column_id = getattr(instance, '_old_column_id', None)

                # Get tenant schema from connection
                from django.db import connection
                tenant_schema = getattr(connection, 'schema_name', 'public')

                async_to_sync(broadcast_ticket_moved)(
                    tenant_schema=tenant_schema,
                    board_id=instance.column.board_id,
                    ticket_id=instance.id,
                    from_column_id=old_column_id,
                    to_column_id=instance.column_id,
                    position=instance.position_in_column,
                    updated_by=getattr(instance, '_current_user', None),  # set by the view
                )
                print(f"[Signals] Broadcasted ticket {instance.id} movement to board {instance.column.board_id}")
            except Exception as e:
                print(f"[Signals] Error broadcasting ticket movement: {str(e)}")

        # Clean up temporary attributes
        delattr(instance, '_column_changed')
        delattr(instance, '_old_column_id')
        delattr(instance, '_old_column_name')
        delattr(instance, '_new_column_name')

    # Check if department was changed (set in pre_save)
    if getattr(instance, '_department_changed', False):
        new_department = getattr(instance, '_new_department', None)
        old_department = getattr(instance, '_old_department', None)

        # Notify all users in the newly assigned department
        if new_department:
            department_users = new_department.employees.all()
            for user in department_users:
                create_notification(
                    user=user,
                    notification_type='ticket_assigned',
                    title=f'Department assigned: {instance.title}',
                    message=f'Ticket assigned to your department "{new_department.name}"',
                    ticket_id=instance.id,
                    metadata={
                        'department_id': new_department.id,
                        'department_name': new_department.name,
                        'old_department': old_department.name if old_department else None,
                    }
                )

        # Clean up temporary attributes
        delattr(instance, '_department_changed')
        delattr(instance, '_new_department')
        delattr(instance, '_old_department')

    # Broadcast any field changes to board viewers
    field_changes = getattr(instance, '_field_changes', {})
    if field_changes and instance.column and instance.column.board_id:
        try:
            from users.consumers import broadcast_ticket_updated
            from django.db import connection
            tenant_schema = getattr(connection, 'schema_name', 'public')

            async_to_sync(broadcast_ticket_updated)(
                tenant_schema=tenant_schema,
                board_id=instance.column.board_id,
                ticket_id=instance.id,
                changes=field_changes,
                updated_by=getattr(instance, '_current_user', None),  # set by the view
            )
            print(f"[Signals] Broadcasted ticket {instance.id} field updates to board {instance.column.board_id}")
        except Exception as e:
            print(f"[Signals] Error broadcasting ticket updates: {str(e)}")


@receiver(post_delete, sender=Ticket)
def broadcast_on_ticket_delete(sender, instance, **kwargs):
    """
    Tell everyone viewing the board that a ticket is gone. Uses column_id
    (still set on the deleted instance) and never raises: a board/column
    cascade delete also fires this and the parent rows may be mid-removal.
    """
    if not instance.column_id:
        return
    try:
        from users.consumers import broadcast_ticket_deleted

        board_id = instance.column.board_id
        if not board_id:
            return
        async_to_sync(broadcast_ticket_deleted)(
            tenant_schema=_tenant_schema(),
            board_id=board_id,
            ticket_id=instance.id,
            deleted_by=getattr(instance, '_current_user', None),  # set by the view
        )
    except Exception as e:
        print(f"[Signals] Error broadcasting ticket deletion: {str(e)}")


