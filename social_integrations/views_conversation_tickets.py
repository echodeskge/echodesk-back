"""Tickets created from chat conversations (Messages inbox profile rail).

``POST /api/social/conversations/create-ticket/`` and
``GET  /api/social/conversations/tickets/``. Email threads have their own
endpoint (``views_email_threads``); the rules here mirror it: the
ticket_management feature plus the create_tickets permission, columns limited
to boards the user can access, one ticket per conversation.
"""
import html
import logging

from django.db import IntegrityError, transaction
from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from tenants.permissions import has_subscription_feature

from .conversation_keys import assignment_conversation_id, assignment_lookup_ids, session_conversation_id
from .email_threads import TicketVisibility, ticket_brief
from .models import ConversationTicket
from .permissions import CanViewSocialMessages
from .serializers import (
    ConversationCreateTicketResponseSerializer,
    ConversationCreateTicketSerializer,
    LinkedTicketSerializer,
)

logger = logging.getLogger(__name__)

TAGS = ['Social - Conversations']
PLATFORM_LABELS = {
    'facebook': 'Facebook', 'instagram': 'Instagram', 'whatsapp': 'WhatsApp',
    'telegram': 'Telegram', 'widget': 'Website chat',
}
CUSTOMER_MESSAGES_IN_TICKET = 10


def _error(message, code=status.HTTP_400_BAD_REQUEST):
    return Response({'error': message}, status=code)


def _lookup_ids(platform, conversation_id, account_id):
    """Every key form a conversation may be stored or sent under. WhatsApp
    numbers arrive with or without '+' (and a '+' in a query string decodes to
    a space, stripped by the caller)."""
    keys = set(assignment_lookup_ids(platform, conversation_id, account_id))
    if platform == 'whatsapp':
        digits = str(conversation_id or '').strip().lstrip('+')
        if digits:
            keys.update({digits, f'+{digits}'})
    return sorted(keys)


def _find_link(platform, conversation_id, account_id):
    return (
        ConversationTicket.objects.filter(
            platform=platform, account_id=str(account_id),
            conversation_id__in=_lookup_ids(platform, conversation_id, account_id),
        ).select_related('ticket', 'ticket__column').first()
    )


def _customer_id(platform, conversation_id, account_id):
    """Id of the ``Client`` linked to this conversation's ``SocialAccount``, or
    None. Matches every key form the conversation may be stored under (the same
    set the ``client_id`` listing uses); more than one distinct client is
    ambiguous and also gives None."""
    from .models import SocialAccount

    client_ids = set(
        SocialAccount.objects.filter(
            platform=platform, account_connection_id=str(account_id),
            platform_id__in=_lookup_ids(platform, conversation_id, account_id),
        ).values_list('client_id', flat=True)
    )
    return client_ids.pop() if len(client_ids) == 1 else None


def _ticket_content(platform, conversation_id, account_id):
    """(customer name, description html, transcript) from the conversation."""
    from .services.conversation_transcript import build_transcript, get_customer_display_name

    candidates = [session_conversation_id(platform, conversation_id, account_id)]
    if platform == 'whatsapp':
        candidates += [k for k in _lookup_ids(platform, conversation_id, account_id) if k not in candidates]
    name, transcript = '', []
    try:
        for key in candidates:
            transcript = build_transcript(platform, account_id, key, limit=50)
            if transcript:
                name = get_customer_display_name(platform, account_id, key)
                break
    except ValueError:
        return '', '', []
    customer = [e for e in transcript if e['role'] == 'customer'][-CUSTOMER_MESSAGES_IN_TICKET:]
    name = name or next((e['sender_name'] for e in reversed(customer) if e['sender_name']), '')
    label = PLATFORM_LABELS.get(platform, platform)
    who = html.escape(name or conversation_id)
    parts = [f'<p><strong>{html.escape(label)}:</strong> {who}</p><hr>']
    for entry in customer:
        when = entry['timestamp'].strftime('%Y-%m-%d %H:%M') if entry.get('timestamp') else ''
        text = '<br>'.join(html.escape(line) for line in entry['text'].splitlines())
        parts.append(f'<p><small>{when}</small><br>{text}</p>')
    return name, ''.join(parts), transcript


@extend_schema(
    summary='Create a ticket from a chat conversation',
    description=(
        'Creates a ticket (title = "<customer> · <channel>", description = the latest '
        'customer messages) and links it to the conversation. The ticket\'s customer '
        'is the client linked to the conversation\'s social account, when there is one. '
        'Returns 200 with the '
        'existing ticket when the conversation already has one. Needs the '
        'ticket_management feature and the create_tickets permission; `column_id` '
        'must belong to a board the user can access.'
    ),
    request=ConversationCreateTicketSerializer,
    responses={200: ConversationCreateTicketResponseSerializer, 201: ConversationCreateTicketResponseSerializer},
    tags=TAGS,
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def conversation_create_ticket(request):
    from tickets.models import Ticket, TicketColumn
    from tickets.views import accessible_boards, open_time_log

    if not has_subscription_feature(request, 'ticket_management'):
        return _error('Tickets are not included in your plan', status.HTTP_403_FORBIDDEN)
    if not request.user.has_permission('create_tickets'):
        return _error('You do not have permission to create tickets', status.HTTP_403_FORBIDDEN)

    serializer = ConversationCreateTicketSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    platform, raw_id, account_id = data['platform'], data['conversation_id'].strip(), data['account_id'].strip()
    visible = TicketVisibility(request.user)

    existing = _find_link(platform, raw_id, account_id)
    if existing:
        return Response({'ticket': ticket_brief(existing.ticket, visible(existing.ticket)), 'created': False})

    name, description, transcript = _ticket_content(platform, raw_id, account_id)
    if not transcript:
        return _error('Conversation not found', status.HTTP_404_NOT_FOUND)

    column = None
    if data.get('column_id'):
        column = TicketColumn.objects.filter(
            id=data['column_id'], board__in=accessible_boards(request.user),
        ).first()
        if column is None:
            return _error('Column not found')

    title = f'{name or raw_id} · {PLATFORM_LABELS.get(platform, platform)}'
    try:
        with transaction.atomic():
            ticket = Ticket(
                title=title[:255],
                description=description,
                rich_description=description,
                description_format='html',
                column=column,
                created_by=request.user,
                customer_id=_customer_id(platform, raw_id, account_id),
            )
            ticket._current_user = request.user
            # One ``created`` history row, nothing else (E15)
            from tickets.realtime import history_muted
            from tickets.signals import record_ticket_created
            with history_muted():
                ticket.save()
            record_ticket_created(ticket, request.user)
            ConversationTicket.objects.create(
                platform=platform,
                conversation_id=assignment_conversation_id(platform, raw_id, account_id),
                account_id=account_id,
                ticket=ticket,
                created_by=request.user,
            )
    except IntegrityError:
        existing = _find_link(platform, raw_id, account_id)
        if existing:
            return Response({'ticket': ticket_brief(existing.ticket, visible(existing.ticket)), 'created': False})
        raise
    open_time_log(ticket, ticket.column, request.user)
    logger.info('Ticket %s created from %s conversation %s/%s', ticket.id, platform, account_id, raw_id)
    return Response({'ticket': ticket_brief(ticket), 'created': True}, status=status.HTTP_201_CREATED)


@extend_schema(
    summary='Tickets linked to a conversation or client',
    description=(
        'Either `platform` + `conversation_id` + `account_id` (one conversation) or '
        '`client_id` (every conversation of the client\'s linked social accounts). '
        'Newest first. `title` and `column` are null for tickets on boards the '
        'viewer cannot see.'
    ),
    parameters=[
        OpenApiParameter('platform', OpenApiTypes.STR, OpenApiParameter.QUERY),
        OpenApiParameter('conversation_id', OpenApiTypes.STR, OpenApiParameter.QUERY),
        OpenApiParameter('account_id', OpenApiTypes.STR, OpenApiParameter.QUERY),
        OpenApiParameter('client_id', OpenApiTypes.INT, OpenApiParameter.QUERY),
    ],
    responses={200: LinkedTicketSerializer(many=True)},
    tags=TAGS,
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def conversation_tickets(request):
    params = request.query_params
    client_id = params.get('client_id')
    if client_id:
        from .models import SocialAccount
        try:
            client_id = int(client_id)
        except ValueError:
            return _error('client_id must be a number')
        q = Q()
        for platform, platform_id, account in SocialAccount.objects.filter(client_id=client_id).values_list(
            'platform', 'platform_id', 'account_connection_id',
        ):
            q |= Q(platform=platform, account_id=str(account),
                   conversation_id__in=_lookup_ids(platform, platform_id, account))
        links = ConversationTicket.objects.filter(q) if q else ConversationTicket.objects.none()
    else:
        platform = params.get('platform')
        conversation_id = (params.get('conversation_id') or '').strip()
        account_id = (params.get('account_id') or '').strip()
        if not (platform and conversation_id and account_id):
            return _error('Pass client_id, or platform, conversation_id and account_id')
        links = ConversationTicket.objects.filter(
            platform=platform, account_id=account_id,
            conversation_id__in=_lookup_ids(platform, conversation_id, account_id),
        )

    visible = TicketVisibility(request.user)
    out, seen = [], set()
    for link in links.select_related('ticket', 'ticket__column').order_by('-created_at'):
        if link.ticket_id in seen:
            continue
        seen.add(link.ticket_id)
        ok = visible(link.ticket)
        out.append({
            'id': link.ticket_id,
            'title': link.ticket.title if ok else None,
            'column': (link.ticket.column.name if link.ticket.column else None) if ok else None,
            'platform': link.platform,
            'created_at': link.created_at,
        })
    return Response(LinkedTicketSerializer(out, many=True).data)
