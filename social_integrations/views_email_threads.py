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

from .email_threads import find_thread_assignment, ticket_brief, user_brief
from .models import ChatAssignment, EmailConnection, EmailMessage, EmailThreadTicket
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
    _email_connection_access,
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


def _can_use_mailbox(user, conn):
    allowed = _email_connection_access(user)
    return allowed is None or conn.id in allowed


def _assignable_users(conn):
    from users.models import User
    users = User.objects.filter(is_active=True).order_by('first_name', 'last_name', 'email')
    return [u for u in users if _can_use_mailbox(u, conn)]


@extend_schema(
    summary='Users an email thread can be assigned to',
    description='Active users who can open this mailbox. ' + EMAIL_ACCESS_RULES_DOC,
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
        if target is None or not _can_use_mailbox(target, conn):
            return _error('This user cannot be assigned to this mailbox')

    with transaction.atomic():
        current = find_thread_assignment(conn.id, thread_id)
        holder_id = (
            current.assigned_user_id
            if current and current.status in ('active', 'in_session') else None
        )
        if holder_id and holder_id != request.user.id and not _is_privileged(request.user):
            return _error('Only the assigned agent or an admin can reassign this conversation',
                          status.HTTP_403_FORBIDDEN)
        if target is not None and holder_id == target.id:
            return Response({'assignee': user_brief(target)})

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
            return Response({'assignee': None})

        if current is None:
            current = ChatAssignment(
                platform='email', conversation_id=thread_id, account_id=str(conn.id),
            )
        current.assigned_user = target
        current.status = 'active'
        current.session_started_at = None
        current.session_ended_at = None
        current.save()
        assignment = current
        transaction.on_commit(lambda: _broadcast_assignment_change(request, assignment))

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


@extend_schema(
    summary='Create a ticket from an email thread',
    description=(
        'Creates a ticket from the conversation (title = subject, description = the '
        'latest customer message and its sender) and links it to the thread. '
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

    existing = EmailThreadTicket.objects.filter(connection=conn, thread_id=thread_id).select_related('ticket').first()
    if existing:
        return Response({'ticket': ticket_brief(existing.ticket), 'created': False})

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
        column = TicketColumn.objects.filter(id=column_id).first()
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
            )
            ticket._current_user = request.user
            ticket.save()
            EmailThreadTicket.objects.create(
                connection=conn, thread_id=thread_id, ticket=ticket, created_by=request.user,
            )
    except IntegrityError:
        # Someone linked the thread in parallel: return theirs.
        existing = EmailThreadTicket.objects.filter(connection=conn, thread_id=thread_id).select_related('ticket').first()
        if existing:
            return Response({'ticket': ticket_brief(existing.ticket), 'created': False})
        raise
    open_time_log(ticket, ticket.column, request.user)
    logger.info('Ticket %s created from email thread %s/%s at %s', ticket.id, conn.id, thread_id, timezone.now())
    return Response({'ticket': ticket_brief(ticket), 'created': True}, status=status.HTTP_201_CREATED)
