from django.db import connection
from django.contrib.auth import get_user_model
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework import status
from tenant_schemas.utils import schema_context

from .models import Board, TicketColumn, Ticket, TicketAttachment
from .realtime import history_muted
from .signals import record_ticket_created

User = get_user_model()

PRIORITY_SLA = {
    'high': '3 hours',
    'medium': '6 hours',
    'low': '24 hours',
}


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def report_bug(request):
    title = request.data.get('title', '').strip()
    if not title:
        return Response({'error': 'Title is required'}, status=status.HTTP_400_BAD_REQUEST)

    description = request.data.get('description', '').strip()
    priority = request.data.get('priority', 'medium').lower()
    if priority not in PRIORITY_SLA:
        priority = 'medium'

    files = request.FILES.getlist('files')
    page_url = request.data.get('url', '').strip()

    reporter_email = request.user.email
    reporter_name = request.user.first_name or request.user.email
    source_tenant = connection.schema_name

    sla = PRIORITY_SLA[priority]

    html_description = (
        f'<p>{description}</p>'
        f'<hr>'
        f'<p><strong>Reporter:</strong> {reporter_name} ({reporter_email})</p>'
        f'<p><strong>Tenant:</strong> {source_tenant}</p>'
        f'<p><strong>Page URL:</strong> <a href="{page_url}">{page_url}</a></p>'
        f'<p><strong>Priority:</strong> {priority.capitalize()} — SLA: {sla}</p>'
    )

    try:
        with schema_context('groot'):
            board = Board.objects.filter(name__iexact='echodesk').first()
            if not board:
                return Response(
                    {'error': 'Bug report board not found'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            column = TicketColumn.objects.filter(board=board, name__iexact='bugs').first()
            if not column:
                return Response(
                    {'error': 'Bugs column not found'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            system_user = User.objects.filter(is_superuser=True).first()

            # One ``created`` history row; the attachments write none (E15)
            with history_muted():
                ticket = Ticket.objects.create(
                    title=f'[Bug] {title}',
                    description=html_description,
                    column=column,
                    priority=priority,
                    created_by=system_user,
                    metadata={
                        'bug_report': True,
                        'reporter_email': reporter_email,
                        'reporter_name': reporter_name,
                        'reporter_tenant': source_tenant,
                        # Stash the raw user-written description (plain text) so the
                        # Telegram new-ticket signal can include it without having
                        # to parse the HTML wrapper above.
                        'description': description,
                        'page_url': page_url,
                    },
                )

                for f in files:
                    TicketAttachment.objects.create(
                        ticket=ticket,
                        file=f,
                        filename=f.name,
                        file_size=f.size,
                        content_type=f.content_type or 'application/octet-stream',
                        uploaded_by=system_user,
                    )
            record_ticket_created(ticket, system_user)

        return Response({'success': True, 'ticket_id': ticket.id}, status=status.HTTP_201_CREATED)

    except Exception as e:
        return Response(
            {'error': f'Failed to create bug report: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
