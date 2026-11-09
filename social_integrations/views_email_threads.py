"""Email inbox thread actions: assign a conversation, create a ticket from it.

``/api/social/email/threads/assign/``, ``…/assignable-users/`` and
``…/create-ticket/``. Mailbox visibility follows ``EMAIL_ACCESS_RULES_DOC``.
"""
import html
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from tenants.permissions import has_subscription_feature

from .email_threads import TicketVisibility, ticket_brief, user_brief
from .models import (
    ChatAssignment, EmailConnection, EmailConnectionUserAssignment, EmailMessage, EmailThreadTicket,
)
from .permissions import CanViewSocialMessages
from .serializers import (
    EmailThreadAssigneeSerializer,
    EmailThreadAssignSerializer,
    EmailThreadAssignResponseSerializer,
    EmailThreadCreateTicketSerializer,
    EmailThreadCreateTicketResponseSerializer,
)
from .views import (
    EMAIL_ACCESS_RULES_DOC,
    _broadcast_assignment_change,
    _can_access_email_connection,
)

logger = logging.getLogger(__name__)

TAGS = ['Social - Email']


def _error(message, code=status.HTTP_400_BAD_REQUEST):
    return Response({'error': message}, status=code)


def _connection_for(user, connection_id):
    conn = EmailConnection.objects.filter(id=connection_id).first()
    if conn is None or not _can_access_email_connection(user, conn.id):
        return None
    return conn


def _thread_messages(conn, thread_id):
    return EmailMessage.objects.filter(connection=conn, thread_id=thread_id, is_deleted=False)


def _is_privileged(user):
    return bool(user.is_staff or user.is_superuser or getattr(user, 'role', None) == 'admin')


def _mailbox_assignee_ids(conn):
    return set(
        EmailConnectionUserAssignment.objects.filter(connection=conn).values_list('user_id', flat=True)
    )


def _can_be_assigned(user, subscription_keys, mailbox_assignee_ids):
    """May ``user`` hold a thread of this mailbox?

    They need the social_integrations feature (what ``CanViewSocialMessages``
    checks, so they can open the inbox the notification links to) and access
    to the mailbox (``EMAIL_ACCESS_RULES_DOC``: privileged users see every
    mailbox; a mailbox with no user assignments is shared).
    """
    if not user.has_feature('social_integrations', subscription_keys):
        return False
    if (
        user.is_superuser or user.is_staff or getattr(user, 'role', None) == 'admin'
        or user.has_permission('manage_social_connections')
    ):
        return True
    return not mailbox_assignee_ids or user.id in mailbox_assignee_ids


def _assignable_users(conn):
    """Every eligible user in a fixed number of queries (no per-user lookups)."""
    from users.models import User, current_subscription_feature_keys
    subscription_keys = set(current_subscription_feature_keys())
    assignee_ids = _mailbox_assignee_ids(conn)
    users = (
        User.objects.filter(is_active=True)
        .prefetch_related('tenant_groups__features')
        .order_by('first_name', 'last_name', 'email')
    )
    return [u for u in users if _can_be_assigned(u, subscription_keys, assignee_ids)]


def _is_assignable(user, conn):
    from users.models import current_subscription_feature_keys
    return _can_be_assigned(user, set(current_subscription_feature_keys()), _mailbox_assignee_ids(conn))


@extend_schema(
    summary='Users an email thread can be assigned to',
    description='Active users who can open the inbox (social_integrations feature) and this mailbox. ' + EMAIL_ACCESS_RULES_DOC,
    parameters=[OpenApiParameter('connection_id', OpenApiTypes.INT, OpenApiParameter.QUERY, required=True)],
    responses={200: EmailThreadAssigneeSerializer(many=True)},
    tags=TAGS,
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def email_thread_assignable_users(request):
    try:
        connection_id = int(request.query_params.get('connection_id') or 0)
    except ValueError:
        connection_id = 0
    conn = _connection_for(request.user, connection_id)
    if conn is None:
        return _error('Mailbox not found', status.HTTP_404_NOT_FOUND)
    return Response([user_brief(u) for u in _assignable_users(conn)])


def _notify_assignee(assignee, actor, conn, thread_id):
    from users.notification_texts import T, truncate
    from users.notification_utils import create_notification

    latest = _thread_messages(conn, thread_id).order_by('-timestamp').only('id', 'subject').first()
    subject = (latest.subject if latest else '') or '—'
    try:
        create_notification(
            user=assignee,
            notification_type='message_assigned',
            title=T('email.assigned.title'),
            message=T(
                'email.assigned.message',
                actor=actor.get_full_name() or actor.email,
                subject=truncate(subject, 120),
            ),
            metadata={'connection_id': conn.id, 'thread_id': thread_id},
            link_url=f'/email/INBOX/{latest.id}' if latest else '/email/INBOX',
        )
    except Exception:  # never fail the assignment over a notification
        logger.exception('email thread assignment notification failed')


def _apply_assignment(request, conn, thread_id, target):
    """Write the assignment under a row lock.

    Returns a Response on refusal, ``False`` when nothing changed and ``True``
    otherwise. A thread already ``in_session`` (a running chat session from
    the unified inbox) keeps its status and session timestamps when it is
    handed to someone else; only a fresh row starts as ``active``.
    """
    with transaction.atomic():
        current = (
            ChatAssignment.objects.select_for_update()
            .filter(platform='email', account_id=str(conn.id), conversation_id=thread_id)
            .order_by('-updated_at').first()
        )
        holder_id = (
            current.assigned_user_id
            if current and current.status in ('active', 'in_session') else None
        )
        if holder_id and holder_id != request.user.id and not _is_privileged(request.user):
            return _error('Only the assigned agent or an admin can reassign this conversation',
                          status.HTTP_403_FORBIDDEN)

        if target is None:
            if current is not None:
                snapshot = {
                    'platform': 'email', 'conversation_id': current.conversation_id,
                    'account_id': current.account_id, 'status': None,
                    'session_started_at': None, 'session_ended_at': None,
                }
                ChatAssignment.objects.filter(
                    platform='email', account_id=str(conn.id), conversation_id=thread_id,
                ).delete()
                transaction.on_commit(lambda: _broadcast_assignment_change(request, snapshot))
            return True

        if holder_id == target.id:
            return False

        if current is None:
            current = ChatAssignment(
                platform='email', conversation_id=thread_id, account_id=str(conn.id), status='active',
            )
        elif current.status != 'in_session':
            current.status = 'active'
            current.session_started_at = None
            current.session_ended_at = None
        current.assigned_user = target
        with transaction.atomic():  # savepoint: a duplicate insert raises cleanly
            current.save()
        assignment = current
        transaction.on_commit(lambda: _broadcast_assignment_change(request, assignment))
    return True


@extend_schema(
    summary='Assign an email thread',
    description=(
        'Assigns the conversation to `user_id` (null unassigns). A thread held by '
        'someone else can only be reassigned by its holder or staff. The target '
        'must be an active user who can open the mailbox; they get a notification. '
        'Stored as a ChatAssignment (platform email), independent of the chat '
        'assignment mode. ' + EMAIL_ACCESS_RULES_DOC
    ),
    request=EmailThreadAssignSerializer,
    responses={200: EmailThreadAssignResponseSerializer},
    tags=TAGS,
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def email_thread_assign(request):
    serializer = EmailThreadAssignSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    conn = _connection_for(request.user, data['connection_id'])
    if conn is None:
        return _error('Mailbox not found', status.HTTP_404_NOT_FOUND)
    thread_id = data['thread_id']
    if not _thread_messages(conn, thread_id).exists():
        return _error('Conversation not found', status.HTTP_404_NOT_FOUND)

    target = None
    user_id = data.get('user_id')
    if user_id is not None:
        from users.models import User
        target = User.objects.filter(id=user_id, is_active=True).first()
        if target is None or not _is_assignable(target, conn):
            return _error('This user cannot be assigned to this mailbox')

    try:
        result = _apply_assignment(request, conn, thread_id, target)
    except IntegrityError:
        # A parallel assign inserted the row first: retry against it.
        result = _apply_assignment(request, conn, thread_id, target)
    if isinstance(result, Response):
        return result
    if target is None:
        return Response({'assignee': None})
    if not result:  # already held by the target
        return Response({'assignee': user_brief(target)})

    if target.id != request.user.id:
        _notify_assignee(target, request.user, conn, thread_id)
    return Response({'assignee': user_brief(target)})


def _ticket_description(message):
    from .html_sanitizer import sanitize_email_html
    sender = message.from_name or message.from_email
    header = (
        f'<p><strong>From:</strong> {html.escape(sender)} '
        f'&lt;{html.escape(message.from_email)}&gt;</p>'
    )
    if message.body_html:
        body = sanitize_email_html(message.body_html)
    else:
        body = ''.join(
            f'<p>{html.escape(line)}</p>' for line in (message.body_text or '').splitlines() if line.strip()
        )
    return header + '<hr>' + body


def _customer_id_for_sender(message):
    """Id of the one ``Client`` whose email matches the customer message's
    sender (case-insensitive), or None when none or several match. A thread
    with only business messages has no customer sender."""
    from .models import Client

    email = (message.from_email or '').strip()
    if message.is_from_business or not email:
        return None
    ids = list(Client.objects.filter(email__iexact=email).values_list('id', flat=True)[:2])
    return ids[0] if len(ids) == 1 else None


@extend_schema(
    summary='Create a ticket from an email thread',
    description=(
        'Creates a ticket from the conversation (title = subject, description = the '
        'latest customer message and its sender) and links it to the thread. The '
        'ticket\'s customer is the client whose email matches that sender, when exactly '
        'one does. '
        'Returns 200 with the existing ticket when the thread already has one. '
        'Needs the ticket_management feature and the create_tickets permission.'
    ),
    request=EmailThreadCreateTicketSerializer,
    responses={200: EmailThreadCreateTicketResponseSerializer, 201: EmailThreadCreateTicketResponseSerializer},
    tags=TAGS,
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def email_thread_create_ticket(request):
    from tickets.models import Ticket, TicketColumn
    from tickets.views import open_time_log

    if not has_subscription_feature(request, 'ticket_management'):
        return _error('Tickets are not included in your plan', status.HTTP_403_FORBIDDEN)
    if not request.user.has_permission('create_tickets'):
        return _error('You do not have permission to create tickets', status.HTTP_403_FORBIDDEN)

    serializer = EmailThreadCreateTicketSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    conn = _connection_for(request.user, data['connection_id'])
    if conn is None:
        return _error('Mailbox not found', status.HTTP_404_NOT_FOUND)
    thread_id = data['thread_id']

    existing = EmailThreadTicket.objects.filter(connection=conn, thread_id=thread_id).select_related('ticket', 'ticket__column').first()
    if existing:
        return Response({'ticket': ticket_brief(existing.ticket, TicketVisibility(request.user)(existing.ticket)), 'created': False})

    messages = _thread_messages(conn, thread_id)
    source = (
        messages.filter(is_from_business=False).order_by('-timestamp').first()
        or messages.order_by('-timestamp').first()
    )
    if source is None:
        return _error('Conversation not found', status.HTTP_404_NOT_FOUND)

    column = None
    column_id = data.get('column_id')
    if column_id:
        from tickets.views import accessible_boards
        column = TicketColumn.objects.filter(
            id=column_id, board__in=accessible_boards(request.user),
        ).first()
        if column is None:
            return _error('Column not found')

    subject = (source.subject or '').strip() or '(no subject)'
    try:
        with transaction.atomic():
            description = _ticket_description(source)
            ticket = Ticket(
                title=subject[:255],
                description=description,
                rich_description=description,
                description_format='html',
                column=column,
                created_by=request.user,
                customer_id=_customer_id_for_sender(source),
            )
            ticket._current_user = request.user
            # One ``created`` history row, nothing else (E15)
            from tickets.realtime import history_muted
            from tickets.signals import record_ticket_created
            with history_muted():
                ticket.save()
            record_ticket_created(ticket, request.user)
            EmailThreadTicket.objects.create(
                connection=conn, thread_id=thread_id, ticket=ticket, created_by=request.user,
            )
    except IntegrityError:
        # Someone linked the thread in parallel: return theirs.
        existing = EmailThreadTicket.objects.filter(connection=conn, thread_id=thread_id).select_related('ticket', 'ticket__column').first()
        if existing:
            return Response({'ticket': ticket_brief(existing.ticket, TicketVisibility(request.user)(existing.ticket)), 'created': False})
        raise
    open_time_log(ticket, ticket.column, request.user)
    logger.info('Ticket %s created from email thread %s/%s at %s', ticket.id, conn.id, thread_id, timezone.now())
    return Response({'ticket': ticket_brief(ticket), 'created': True}, status=status.HTTP_201_CREATED)
