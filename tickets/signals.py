"""
Signals for ticket notifications, history and board broadcasts.

- Notifications (in-app / push / Telegram) are sent inline, as before.
- Board WebSocket events go through ``tickets.realtime.queue_event``: they
  are sent after commit and coalesced per request (E12).
- TicketHistory rows are written for field changes, board transfers, tags,
  assignees, groups, attachments, checklist items and form submissions
  (E15). ``tickets.realtime.history_muted()`` suppresses them while a ticket
  is being created; the creating code then writes one ``created`` row with
  ``record_ticket_created``. A plain ``Ticket.objects.create`` writes no
  history at all.
"""
from django.db.models import Q
from django.db.models.signals import post_save, post_delete, pre_save, pre_delete, m2m_changed
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from .models import (
    ChecklistItem,
    Tag,
    Ticket,
    TicketComment,
    TicketAssignment,
    TicketAttachment,
    TicketFormSubmission,
    TicketHistory,
)
import re
# Not used here any more; kept so existing patch('tickets.signals.async_to_sync') targets resolve.
from asgiref.sync import async_to_sync  # noqa: F401
from django.db import connection
from tenant_schemas.utils import schema_context
from users.notification_texts import CATALOG as _TEXTS, T
from .realtime import (
    actor_for,
    card_payload,
    is_history_muted,
    json_safe,
    queue_event,
)

User = get_user_model()


def _tenant_schema():
    """Schema name of the tenant the signal is firing in (board groups are per tenant)."""
    return getattr(connection, 'schema_name', 'public')


# Back-compat names (BE-08 moved the implementations to tickets.realtime)
_json_safe = json_safe
_ticket_card_payload = card_payload


def _user_label(user):
    if user is None:
        return ''
    return user.get_full_name() or user.email


def _board_id_of_ticket(ticket_id):
    return (
        Ticket.objects.filter(pk=ticket_id)
        .values_list('column__board_id', flat=True)
        .first()
    )


def _board_id_for(ticket):
    """Board of an instance without a query when its column is cached."""
    if not ticket.column_id:
        return None
    cached = ticket._state.fields_cache.get('column')
    if cached is not None and cached.pk == ticket.column_id:
        return cached.board_id
    return _board_id_of_ticket(ticket.pk)


def _deleted_with_parent(kwargs, model):
    """True when a post_delete fires because a parent (ticket, user, ...) is
    being deleted, i.e. the delete did not start on ``model`` itself."""
    origin = kwargs.get('origin')
    if origin is None:
        return False
    origin_model = getattr(origin, 'model', None) or type(origin)
    return origin_model is not model


def _ticket_exists(ticket_id):
    return Ticket.objects.filter(pk=ticket_id).exists()


def _history(ticket_id, action, *, user=None, field_name='', old_value='', new_value='', description=''):
    """Write one TicketHistory row unless history is muted."""
    if is_history_muted():
        return None
    return TicketHistory.objects.create(
        ticket_id=ticket_id,
        action=action,
        field_name=field_name,
        old_value=old_value if old_value is not None else '',
        new_value=new_value if new_value is not None else '',
        description=description,
        user=user,
    )


def record_ticket_created(ticket, user=None):
    """The single ``created`` history row of a ticket made through the API,
    the social create paths or ``report_bug`` (always written, muted or not)."""
    return TicketHistory.objects.create(
        ticket=ticket,
        action='created',
        new_value=ticket.title or '',
        description='Created',
        user=user,
    )


def broadcast_new_ticket_card(ticket):
    """
    Queue a ``ticket_created`` event for a freshly created ticket.

    Called from the post_save hook for plain ``Ticket.objects.create`` paths,
    and from ``TicketSerializer.create`` once tags / assignees are applied
    (post_save fires before those M2M writes). The card is built when the
    event is sent, from the committed row. Never raises.
    """
    if not ticket.column_id:
        return
    try:
        queue_event(
            'ticket_created',
            board_id=_board_id_for(ticket),
            ticket_id=ticket.pk,
            created_by=ticket.created_by if ticket.created_by_id else None,
            instance=ticket,
        )
    except Exception as e:
        print(f"[Signals] Error broadcasting ticket creation: {str(e)}")


def _comment_payload(comment):
    """Minimal comment dict for the ticket_commented broadcast."""
    user = comment.user
    return json_safe({
        'id': comment.id,
        'comment': comment.comment,
        'created_at': comment.created_at,
        'is_internal': comment.is_internal,
        'user': {
            'id': user.id,
            'name': user.get_full_name(),
            'email': user.email,
        } if user else None,
    })


def _queue_activity(ticket_id, kind, actor=None, board_id=None):
    """``ticket_activity`` for something inside a ticket. Never raises."""
    try:
        if board_id is None:
            board_id = _board_id_of_ticket(ticket_id)
        queue_event(
            'ticket_activity', board_id=board_id, ticket_id=ticket_id,
            kind=kind, actor_id=getattr(actor, 'pk', None),
        )
    except Exception as e:
        print(f"[Signals] Error broadcasting ticket activity: {str(e)}")


# ── card-shaped M2M values (E14) ────────────────────────────────────────────

def _tag_refs(tag_ids):
    return [
        {'id': t.id, 'name': t.name, 'color': t.color}
        for t in Tag.objects.filter(pk__in=list(tag_ids)).only('id', 'name', 'color')
    ]


def _user_refs(users):
    return [{'id': u.id, 'name': u.get_full_name(), 'email': u.email} for u in users]


def _assignee_refs(assigned_to_id, user_ids):
    """The card's ``assignees``: ``assigned_to`` first, then the assigned
    users (User ordering), de-duplicated."""
    ids = set(user_ids)
    if assigned_to_id:
        ids.add(assigned_to_id)
    users = {u.id: u for u in User.objects.filter(pk__in=ids).only('id', 'first_name', 'last_name', 'email')}
    ordered = []
    if assigned_to_id and assigned_to_id in users:
        ordered.append(users[assigned_to_id])
    for u in User.objects.filter(pk__in=set(user_ids)).only('id', 'first_name', 'last_name', 'email'):
        if u.id != assigned_to_id:
            ordered.append(u)
    return _user_refs(ordered)


def _queue_ticket_updated(ticket_id, changes, updated_by, board_id=None):
    if not changes:
        return
    try:
        if board_id is None:
            board_id = _board_id_of_ticket(ticket_id)
        queue_event(
            'ticket_updated', board_id=board_id, ticket_id=ticket_id,
            changes=changes, updated_by=updated_by,
        )
    except Exception as e:
        print(f"[Signals] Error broadcasting ticket updates: {str(e)}")


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

    # Rendered in the reporter's language (their tenant's default when they
    # have not chosen one).
    text_key = status_key if f'bug_report.{status_key}.title' in _TEXTS else 'update'
    title = T(f'bug_report.{text_key}.title')
    message = T(f'bug_report.{text_key}.message')

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




def _creating(ticket):
    """True while TicketSerializer.create is still writing the ticket's M2M
    rows (its single ticket_created broadcast follows and carries them)."""
    return ticket is not None and getattr(ticket, '_defer_created_broadcast', False)


def _on_assignment_change(instance, added):
    """History row + ``ticket_updated{assignees}`` for one assignment row
    added or removed."""
    user = instance.user
    actor = actor_for(instance) or (instance.assigned_by if added else None)
    label = _user_label(user)
    if added:
        _history(instance.ticket_id, 'assigned', user=actor, field_name='assigned_users',
                 new_value=label, description=f'Assigned {label}')
    else:
        _history(instance.ticket_id, 'unassigned', user=actor, field_name='assigned_users',
                 old_value=label, description=f'Unassigned {label}')

    if _creating(instance._state.fields_cache.get('ticket')):
        return
    row = (
        Ticket.objects.filter(pk=instance.ticket_id)
        .values('assigned_to_id', 'column__board_id')
        .first()
    )
    if not row or not row['column__board_id'] or user.id == row['assigned_to_id']:
        return  # no board, or the card's assignees are unchanged
    current = list(TicketAssignment.objects.filter(ticket_id=instance.ticket_id).values_list('user_id', flat=True))
    if added:
        old_ids = [uid for uid in current if uid != user.id]
    else:
        # A queryset delete removes every row before the first post_delete,
        # so the "before" set comes from pre_delete (see capture_assignees_before_delete).
        old_ids = getattr(instance, '_assignees_before_delete', None) or current + [user.id]
    assigned_to_id = row['assigned_to_id']
    _queue_ticket_updated(instance.ticket_id, {
        'assignees': {
            'old': _assignee_refs(assigned_to_id, old_ids),
            'new': _assignee_refs(assigned_to_id, current),
        },
    }, actor, board_id=row['column__board_id'])


@receiver(post_save, sender=TicketAssignment)
def notify_on_ticket_assignment(sender, instance, created, raw=False, **kwargs):
    """
    Notify user when they are assigned to a ticket; record the assignment in
    the ticket's history and broadcast the card's new assignees.
    """
    if not created or raw:
        return
    ticket = instance.ticket
    assigned_user = instance.user
    assigned_by = instance.assigned_by

    # Don't notify if user assigned themselves
    if not (assigned_by and assigned_user.id == assigned_by.id):
        assigner_name = assigned_by.get_full_name() if assigned_by else T('common.someone')
        role_key = f'ticket.role.{instance.role}'
        role = T(role_key) if role_key in _TEXTS else instance.get_role_display()

        create_notification(
            user=assigned_user,
            notification_type='ticket_assigned',
            title=T('ticket.assigned.title', ticket=ticket.title),
            message=T('ticket.assigned.message', assigner=assigner_name, role=role),
            ticket_id=ticket.id,
            metadata={
                'role': instance.role,
                'assigned_by': assigned_by.email if assigned_by else None,
            }
        )

    _on_assignment_change(instance, added=True)


@receiver(pre_delete, sender=TicketAssignment)
def capture_assignees_before_delete(sender, instance, **kwargs):
    """The ticket's assigned user ids before this delete (all pre_delete
    signals of a queryset delete run before its SQL DELETE)."""
    if _deleted_with_parent(kwargs, TicketAssignment):
        return
    instance._assignees_before_delete = list(
        TicketAssignment.objects.filter(ticket_id=instance.ticket_id).values_list('user_id', flat=True)
    )


@receiver(post_delete, sender=TicketAssignment)
def record_ticket_unassignment(sender, instance, **kwargs):
    """One ``unassigned`` row (and an assignees update) per removed row; not
    when the ticket or the user itself is being deleted."""
    if _deleted_with_parent(kwargs, TicketAssignment):
        return
    _on_assignment_change(instance, added=False)


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
                title=T('ticket.mentioned.title', ticket=ticket.title),
                message=T('ticket.mentioned.message', commenter=commenter.get_full_name()),
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
            title=T('ticket.commented.title', ticket=ticket.title),
            message=T('ticket.commented.message', commenter=commenter.get_full_name()),
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
            title=T('ticket.commented.title', ticket=ticket.title),
            message=T('ticket.commented.message', commenter=commenter.get_full_name()),
            ticket_id=ticket.id,
            metadata={
                'comment_id': instance.id,
                'commenter': commenter.email,
            }
        )

    # Broadcast the comment to everyone viewing the board so cards can bump
    # their comment count / open threads can append it live
    if ticket.column_id:
        try:
            board_id = _board_id_for(ticket)
            queue_event(
                'ticket_commented',
                board_id=board_id,
                ticket_id=ticket.id,
                comment=_comment_payload(instance),
                # Fresh DB count: the view's ticket may carry a stale `comments` prefetch
                comments_count=TicketComment.objects.filter(ticket_id=ticket.id).count(),
                user=commenter,
            )
            _queue_activity(ticket.id, 'comment', commenter, board_id=board_id)
        except Exception as e:
            print(f"[Signals] Error broadcasting ticket comment: {str(e)}")


@receiver(post_save, sender=TicketComment)
def broadcast_comment_edited(sender, instance, created, raw=False, **kwargs):
    if created or raw:
        return
    _queue_activity(instance.ticket_id, 'comment_edited', actor_for(instance))


@receiver(post_delete, sender=TicketComment)
def broadcast_comment_deleted(sender, instance, **kwargs):
    if _deleted_with_parent(kwargs, TicketComment):
        return
    _queue_activity(instance.ticket_id, 'comment_deleted', actor_for(instance))


def _date_label(value):
    return value.isoformat() if value else ''


@receiver(pre_save, sender=Ticket)
def capture_ticket_prev_state(sender, instance, **kwargs):
    """
    Single pre_save receiver that fetches the old ticket once and:
      - records changed fields into TicketHistory (title, description,
        priority, column, assignee, department, due date, customer, and a
        ``transferred`` row when the ticket changes boards)
      - caches column/position/department/board diffs onto the instance for
        post_save
      - caches the assigned user IDs so post_save doesn't fire a separate query per move
    """
    if not instance.pk or kwargs.get('raw'):
        return  # create path

    try:
        old = Ticket.objects.prefetch_related('assigned_users').select_related(
            'column', 'column__board', 'assigned_department', 'customer',
        ).get(pk=instance.pk)
    except Ticket.DoesNotExist:
        return

    current_user = actor_for(instance)

    # --- Board transfer (E13) ---
    old_board = old.column.board if old.column_id and old.column else None
    new_column = instance.column if instance.column_id else None
    new_board_id = new_column.board_id if new_column else None
    old_board_id = old_board.id if old_board else None
    if old.column_id != instance.column_id and old_board_id != new_board_id:
        instance._board_transfer = (old_board_id, new_board_id)
    else:
        instance._board_transfer = None

    # --- TicketHistory records ---
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
            _history(
                instance.pk, action, user=current_user, field_name=field,
                old_value=str(old_val) if old_val is not None else '',
                new_value=str(new_val) if new_val is not None else '',
            )

    if old.due_date != instance.due_date:
        old_label, new_label = _date_label(old.due_date), _date_label(instance.due_date)
        _history(
            instance.pk, 'due_date_changed', user=current_user, field_name='due_date',
            old_value=old_label, new_value=new_label,
            description=f'Due date: {old_label or "none"} → {new_label or "none"}',
        )

    if old.customer_id != instance.customer_id:
        old_name = old.customer.name if old.customer_id and old.customer else ''
        new_customer = instance.customer if instance.customer_id else None
        new_name = new_customer.name if new_customer else ''
        _history(
            instance.pk, 'updated', user=current_user, field_name='customer',
            old_value=old_name, new_value=new_name,
            description=f'Customer: {old_name or "none"} → {new_name or "none"}',
        )

    if instance._board_transfer and old_board and new_column and new_column.board_id:
        new_board = new_column.board
        _history(
            instance.pk, 'transferred', user=current_user, field_name='board',
            old_value=old_board.name, new_value=new_board.name,
            description=f'{old_board.name} → {new_board.name}',
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

    # --- Assigned user IDs cached for post_save notification loop ---
    prev_ids = [u.id for u in old.assigned_users.all()]
    instance._prev_assigned_user_ids = prev_ids

    # --- Field-change map for broadcasting ---
    instance._field_changes = {}
    for field in ['title', 'priority', 'assigned_department_id', 'due_date']:
        old_value = getattr(old, field)
        new_value = getattr(instance, field)
        if old_value != new_value:
            instance._field_changes[field] = {'old': old_value, 'new': new_value}
    if old.assigned_to_id != instance.assigned_to_id:
        old_refs = _assignee_refs(old.assigned_to_id, prev_ids)
        new_refs = _assignee_refs(instance.assigned_to_id, prev_ids)
        if old_refs != new_refs:
            instance._field_changes['assignees'] = {'old': old_refs, 'new': new_refs}


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
    Notify assigned users when ticket status (column) changes, and queue the
    board events: ``ticket_created`` on create, ``ticket_moved`` within a
    board, ``ticket_deleted{reason:'transferred'}`` + ``ticket_created`` on
    a board transfer (E13), ``ticket_updated`` for field changes.
    """
    if created:
        _send_new_ticket_telegram_notification(instance)

        # Broadcast the new card to everyone viewing the board, unless the
        # creator (TicketSerializer.create) will do it itself once the card's
        # tags / assignees are written
        if not getattr(instance, '_defer_created_broadcast', False):
            broadcast_new_ticket_card(instance)
        return

    actor = actor_for(instance)
    transfer = getattr(instance, '_board_transfer', None)
    instance._board_transfer = None

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
                title=T('ticket.status_changed.title', ticket=instance.title),
                message=T('ticket.status_changed.message', old=old_column_name, new=new_column_name),
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

        try:
            if transfer:
                # Board transfer: the old board drops the card, the new board
                # gets it as a new ticket; no cross-board ticket_moved.
                old_board_id, new_board_id = transfer
                queue_event(
                    'ticket_deleted', board_id=old_board_id, ticket_id=instance.id,
                    deleted_by=actor, reason='transferred', to_board_id=new_board_id,
                )
                queue_event(
                    'ticket_created', board_id=new_board_id, ticket_id=instance.id,
                    created_by=actor, instance=instance,
                )
            elif instance.column and instance.column.board_id:
                queue_event(
                    'ticket_moved', board_id=instance.column.board_id, ticket_id=instance.id,
                    from_column_id=getattr(instance, '_old_column_id', None),
                    to_column_id=instance.column_id,
                    position=instance.position_in_column,
                    updated_by=actor,
                )
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
                    title=T('ticket.department_assigned.title', ticket=instance.title),
                    message=T('ticket.department_assigned.message', department=new_department.name),
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
    instance._field_changes = {}
    if field_changes and instance.column and instance.column.board_id:
        _queue_ticket_updated(instance.id, field_changes, actor, board_id=instance.column.board_id)


@receiver(post_delete, sender=Ticket)
def broadcast_on_ticket_delete(sender, instance, **kwargs):
    """
    Tell everyone viewing the board that a ticket is gone. Uses column_id
    (still set on the deleted instance) and never raises.

    Skipped when the delete cascades from another model (e.g. removing a
    user cascades to every ticket they created): that would cost one column
    query plus one group_send per ticket. Column / board deletes SET_NULL
    the tickets instead, so they never reach this hook.
    """
    if not instance.column_id:
        return
    if _deleted_with_parent(kwargs, Ticket):
        return
    try:
        board_id = instance.column.board_id
        if not board_id:
            return
        queue_event(
            'ticket_deleted', board_id=board_id, ticket_id=instance.id,
            deleted_by=getattr(instance, '_current_user', None),  # set by the view
            reason='deleted', to_board_id=None,
        )
    except Exception as e:
        print(f"[Signals] Error broadcasting ticket deletion: {str(e)}")


# ── tags and groups (m2m_changed) ───────────────────────────────────────────

def _tickets_for_m2m(instance, reverse, pk_set):
    """(ticket, ids) pairs: forward → [(instance, pk_set)]; reverse (e.g.
    ``tag.tickets.add(t)``) → one pair per ticket with ``{instance.pk}``."""
    if not reverse:
        return [(instance, set(pk_set or ()))]
    return [(t, {instance.pk}) for t in Ticket.objects.filter(pk__in=list(pk_set or ()))]


def _m2m_cleared_ids(instance, reverse, field):
    """Ids captured in pre_clear, for post_clear."""
    return getattr(instance, f'_{field}_before_clear', set())


def _capture_before_clear(instance, reverse, field, related_name):
    if reverse:
        ids = set(getattr(instance, related_name).values_list('id', flat=True))
    else:
        ids = set(getattr(instance, field).values_list('id', flat=True))
    setattr(instance, f'_{field}_before_clear', ids)


def _tags_changed(ticket, added, removed):
    if not (added or removed):
        return
    actor = actor_for(ticket)
    names = dict(Tag.objects.filter(pk__in=added | removed).values_list('id', 'name'))
    for tag_id in sorted(added):
        name = names.get(tag_id, str(tag_id))
        _history(ticket.pk, 'tag_added', user=actor, field_name='tags',
                 new_value=name, description=f'Added label {name}')
    for tag_id in sorted(removed):
        name = names.get(tag_id, str(tag_id))
        _history(ticket.pk, 'tag_removed', user=actor, field_name='tags',
                 old_value=name, description=f'Removed label {name}')
    if _creating(ticket):
        return
    current = set(Ticket.tags.through.objects.filter(ticket_id=ticket.pk).values_list('tag_id', flat=True))
    old = (current - added) | removed
    _queue_ticket_updated(ticket.pk, {
        'tags': {'old': _tag_refs(old), 'new': _tag_refs(current)},
    }, actor, board_id=_board_id_for(ticket))


@receiver(m2m_changed, sender=Ticket.tags.through)
def track_ticket_tags(sender, instance, action, reverse, model, pk_set, **kwargs):
    if action == 'pre_clear':
        _capture_before_clear(instance, reverse, 'tags', 'tickets')
        return
    if action == 'post_clear':
        cleared = _m2m_cleared_ids(instance, reverse, 'tags')
        if reverse:
            for ticket in Ticket.objects.filter(pk__in=cleared):
                _tags_changed(ticket, set(), {instance.pk})
        else:
            _tags_changed(instance, set(), cleared)
        return
    if action not in ('post_add', 'post_remove'):
        return
    for ticket, ids in _tickets_for_m2m(instance, reverse, pk_set):
        if action == 'post_add':
            _tags_changed(ticket, ids, set())
        else:
            _tags_changed(ticket, set(), ids)


def _groups_changed(ticket, added, removed):
    if not (added or removed):
        return
    from users.models import TenantGroup
    actor = actor_for(ticket)
    names = dict(TenantGroup.objects.filter(pk__in=added | removed).values_list('id', 'name'))
    for group_id in sorted(added):
        name = names.get(group_id, str(group_id))
        _history(ticket.pk, 'assigned', user=actor, field_name='assigned_groups',
                 new_value=name, description=f'Assigned group {name}')
    for group_id in sorted(removed):
        name = names.get(group_id, str(group_id))
        _history(ticket.pk, 'unassigned', user=actor, field_name='assigned_groups',
                 old_value=name, description=f'Unassigned group {name}')
    if _creating(ticket):
        return
    _queue_activity(ticket.pk, 'history', actor, board_id=_board_id_for(ticket))


@receiver(m2m_changed, sender=Ticket.assigned_groups.through)
def track_ticket_groups(sender, instance, action, reverse, model, pk_set, **kwargs):
    if action == 'pre_clear':
        _capture_before_clear(instance, reverse, 'assigned_groups', 'tickets_assigned')
        return
    if action == 'post_clear':
        cleared = _m2m_cleared_ids(instance, reverse, 'assigned_groups')
        if reverse:
            for ticket in Ticket.objects.filter(pk__in=cleared):
                _groups_changed(ticket, set(), {instance.pk})
        else:
            _groups_changed(instance, set(), cleared)
        return
    if action not in ('post_add', 'post_remove'):
        return
    for ticket, ids in _tickets_for_m2m(instance, reverse, pk_set):
        if action == 'post_add':
            _groups_changed(ticket, ids, set())
        else:
            _groups_changed(ticket, set(), ids)


# ── attachments, checklist, form submissions ────────────────────────────────

@receiver(post_save, sender=TicketAttachment)
def track_attachment_added(sender, instance, created, raw=False, **kwargs):
    if not created or raw:
        return
    actor = actor_for(instance) or instance.uploaded_by
    _history(instance.ticket_id, 'attachment_added', user=actor, field_name='attachments',
             new_value=instance.filename, description=f'Attached {instance.filename}')
    _queue_activity(instance.ticket_id, 'attachment', actor)


@receiver(post_delete, sender=TicketAttachment)
def track_attachment_removed(sender, instance, **kwargs):
    if _deleted_with_parent(kwargs, TicketAttachment):
        return
    actor = actor_for(instance)
    _history(instance.ticket_id, 'attachment_removed', user=actor, field_name='attachments',
             old_value=instance.filename, description=f'Removed attachment {instance.filename}')
    _queue_activity(instance.ticket_id, 'attachment', actor)


@receiver(pre_save, sender=ChecklistItem)
def capture_checklist_prev_state(sender, instance, raw=False, **kwargs):
    instance._checklist_prev = None
    if instance.pk and not raw:
        instance._checklist_prev = (
            ChecklistItem.objects.filter(pk=instance.pk).values('text', 'is_checked').first()
        )


@receiver(post_save, sender=ChecklistItem)
def track_checklist_saved(sender, instance, created, raw=False, **kwargs):
    """``checklist_updated`` for create / toggle / rename; a save that only
    moves the item (reorder) writes no row. Always a ``checklist`` activity."""
    if raw:
        return
    actor = actor_for(instance)
    text = instance.text
    description = None
    if created:
        description = f'Added checklist item: {text}'
    else:
        prev = getattr(instance, '_checklist_prev', None)
        if prev is not None:
            if prev['is_checked'] != instance.is_checked:
                actor = actor or (instance.checked_by if instance.is_checked else None)
                description = f'{"Checked" if instance.is_checked else "Unchecked"}: {text}'
            elif prev['text'] != text:
                description = f'Renamed checklist item: {prev["text"]} → {text}'
    if description:
        _history(instance.ticket_id, 'checklist_updated', user=actor, field_name='checklist',
                 new_value=text, description=description)
    _queue_activity(instance.ticket_id, 'checklist', actor)


@receiver(post_delete, sender=ChecklistItem)
def track_checklist_deleted(sender, instance, **kwargs):
    if _deleted_with_parent(kwargs, ChecklistItem):
        return
    actor = actor_for(instance)
    _history(instance.ticket_id, 'checklist_updated', user=actor, field_name='checklist',
             old_value=instance.text, description=f'Removed checklist item: {instance.text}')
    _queue_activity(instance.ticket_id, 'checklist', actor)


@receiver(post_save, sender=TicketFormSubmission)
def track_form_submitted(sender, instance, created, raw=False, **kwargs):
    if not created or raw:
        return
    actor = actor_for(instance) or instance.submitted_by
    title = instance.form.title if instance.form_id else ''
    _history(instance.ticket_id, 'form_submitted', user=actor, field_name='form',
             new_value=title, description=f'Submitted form {title}'.strip())
    _queue_activity(instance.ticket_id, 'form', actor)
