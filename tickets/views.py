from rest_framework import viewsets, permissions, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from django_filters.rest_framework import DjangoFilterBackend
import django_filters
from django.db.models import Q, F, Max, Count, Prefetch, Exists, OuterRef, Subquery, IntegerField
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import (
    extend_schema, extend_schema_view, OpenApiParameter, OpenApiResponse, inline_serializer,
)
from rest_framework import serializers as drf_serializers
from drf_spectacular.openapi import OpenApiTypes
from .models import (
    Ticket, Tag, TicketComment, TicketColumn, ChecklistItem,
    TicketAssignment, TicketTimeLog, Board, TicketPayment,
    ItemList, ListItem, TicketForm, TicketFormSubmission, TicketAttachment,
    BoardTelegramConnection
)
from .permissions import CanManageColumns, CanManageSettings
from .serializers import (
    TicketSerializer, TicketListSerializer, TagSerializer,
    TicketCommentSerializer, TicketColumnSerializer,
    TicketColumnCreateSerializer, TicketColumnUpdateSerializer,
    KanbanBoardSerializer, ChecklistItemSerializer,
    TicketAssignmentSerializer, TicketTimeLogSerializer,
    TimeTrackingSummarySerializer, TeamSummarySerializer, TicketTimeSummarySerializer,
    BoardSerializer, TicketPaymentSerializer,
    ItemListSerializer, ItemListMinimalSerializer, ListItemSerializer, ListItemMinimalSerializer,
    TicketFormSerializer, TicketFormMinimalSerializer, TicketFormSubmissionSerializer,
    TicketAttachmentSerializer, TicketHistorySerializer,
    BoardTelegramConnectionSerializer, BoardTelegramConnectionCreateSerializer,
    TicketCardSerializer
)


def get_current_tenant(request):
    """Resolve the current tenant once per request (was a fresh DB read each call)."""
    cached = getattr(request, '_current_tenant', None)
    if cached is not None:
        return cached
    from django.db import connection
    from tenants.models import Tenant
    cached = Tenant.objects.get(schema_name=connection.schema_name)
    request._current_tenant = cached
    return cached


def _renumber_column(column_id, ordered_ids, skip_id=None):
    """
    Rewrite position_in_column for one column so it is exactly 0..n-1 in the
    order given. ``skip_id`` is left to its own ``save()`` (the ticket being
    moved) so that its pre_save history/broadcast hooks still see the change.
    Single UPDATE via bulk_update; no signals fire for the renumbered rows.
    """
    rows = [
        Ticket(pk=ticket_id, position_in_column=index)
        for index, ticket_id in enumerate(ordered_ids)
        if ticket_id != skip_id
    ]
    if rows:
        Ticket.objects.bulk_update(rows, ['position_in_column'])


def _locked_column_order(column_ids):
    """
    Lock every ticket in the given columns and return their ids grouped by
    column, in board display order. Must be called inside transaction.atomic().
    """
    by_column = {column_id: [] for column_id in column_ids}
    rows = (
        Ticket.objects.select_for_update()
        .filter(column_id__in=column_ids)
        .order_by('position_in_column', '-created_at')
        .values_list('id', 'column_id')
    )
    for ticket_id, column_id in rows:
        by_column[column_id].append(ticket_id)
    return by_column


def _parse_position(raw):
    """
    Parse an optional 0-based position from the request body.
    Returns (position_or_None, error_message_or_None).
    """
    if raw is None or raw == '':
        return None, None
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, 'position_in_column must be a number'


def ticket_visibility_q(user):
    """Q limiting tickets to what a non-staff user may see (shared by every
    endpoint that resolves a ticket on the user's behalf)."""
    user_groups = user.tenant_groups.all()
    return (
        Q(created_by=user) |
        Q(assigned_to=user) |
        Q(assigned_users=user) |
        Q(assigned_groups__in=user_groups) |
        Q(column__board__order_users=user) |  # Board order users
        Q(column__board__board_users=user) |  # Board users
        Q(column__board__board_groups__in=user_groups)  # Board groups
    )


def close_active_time_log(ticket, column):
    """Close the open time log of ``ticket`` in ``column`` (no-op unless the
    column tracks time and an open log exists). Returns the closed log or None."""
    if not column or not column.track_time:
        return None
    active_time_log = TicketTimeLog.objects.filter(
        ticket=ticket,
        column=column,
        exited_at__isnull=True
    ).first()
    if active_time_log:
        active_time_log.exited_at = timezone.now()
        active_time_log.calculate_duration()
    return active_time_log


def open_time_log(ticket, column, user):
    """Start a time log for ``ticket`` in ``column`` when the column tracks time."""
    if not column or not column.track_time:
        return None
    return TicketTimeLog.objects.create(ticket=ticket, column=column, user=user)


class BoardPermission(permissions.BasePermission):
    """
    Custom permission class for Board operations.
    """
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False

        user = request.user

        # Staff and superusers always have access
        if user.is_staff or user.is_superuser:
            return True

        # Check permissions based on action
        if view.action == 'list' or view.action == 'retrieve' or view.action == 'kanban_board':
            # Allow access if:
            # 1. User has full board permissions OR order access permissions
            # 2. User is assigned to at least one board (directly or via group)
            has_permission = (user.has_permission('view_boards') or
                            user.has_permission('access_orders'))

            if has_permission:
                return True

            # Single query: does this user have access to any board either
            # directly (board_users) or through one of their active groups?
            user_group_ids = list(
                user.tenant_groups.filter(is_active=True).values_list('id', flat=True)
            )
            access_filter = Q(board_users=user)
            if user_group_ids:
                access_filter |= Q(board_groups__id__in=user_group_ids)
            return Board.objects.filter(access_filter).exists()

        elif view.action == 'create':
            return request.user.has_permission('create_boards')
        elif view.action in ['update', 'partial_update']:
            return request.user.has_permission('edit_boards')
        elif view.action == 'destroy':
            return request.user.has_permission('delete_boards')
        elif view.action == 'set_default':
            return request.user.is_staff or request.user.is_superuser
        elif (
            view.action in ('telegram_connection', 'telegram_connection_test')
            and request.method not in permissions.SAFE_METHODS
        ):
            # Writing the bot token / chat id (or sending a test message with
            # them) is board configuration, not viewing.
            return request.user.has_permission('edit_boards')
        else:
            # Default to view permission for unknown actions
            return (request.user.has_permission('view_boards') or
                   request.user.has_permission('access_orders'))

    def has_object_permission(self, request, view, obj):
        # For object-level permissions, we can add additional checks
        # For now, rely on the general permission check
        return self.has_permission(request, view)


def accessible_boards(user, base_qs=None):
    """
    Boards ``user`` may see: all of them for staff/superusers; otherwise the
    boards they are attached to directly (board_users / order_users), through
    an active group (board_groups), or boards open to everyone (no users and
    no groups attached). ``BoardViewSet`` and the column endpoints share this.
    """
    qs = Board.objects.all() if base_qs is None else base_qs
    if not user or not user.is_authenticated:
        return qs.none()
    if user.is_superuser or user.is_staff:
        return qs

    # Access check via Exists subqueries — avoids Cartesian join across 3+ M2Ms
    BoardUsers = Board.board_users.through
    BoardGroups = Board.board_groups.through
    OrderUsers = Board.order_users.through

    user_group_ids = list(
        user.tenant_groups.filter(is_active=True).values_list('id', flat=True)
    )

    has_users_sq = BoardUsers.objects.filter(board_id=OuterRef('pk'))
    has_groups_sq = BoardGroups.objects.filter(board_id=OuterRef('pk'))
    user_in_board_sq = BoardUsers.objects.filter(board_id=OuterRef('pk'), user_id=user.id)
    user_in_orders_sq = OrderUsers.objects.filter(board_id=OuterRef('pk'), user_id=user.id)
    group_in_board_sq = (
        BoardGroups.objects.filter(board_id=OuterRef('pk'), tenantgroup_id__in=user_group_ids)
        if user_group_ids else BoardGroups.objects.none()
    )

    return qs.annotate(
        _has_users=Exists(has_users_sq),
        _has_groups=Exists(has_groups_sq),
        _user_in_board=Exists(user_in_board_sq),
        _user_in_orders=Exists(user_in_orders_sq),
        _group_in_board=Exists(group_in_board_sq),
    ).filter(
        Q(_user_in_board=True)
        | Q(_user_in_orders=True)
        | Q(_group_in_board=True)
        | Q(_has_users=False, _has_groups=False)
    )


class TicketColumnViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing ticket columns (Kanban board columns).

    Anyone authenticated can read columns. Create, update, delete and reorder
    need staff/superuser or the ``manage_columns`` permission (individual
    flag or manager/admin role). Every action only sees columns of boards the
    caller can access (``accessible_boards``; staff/superusers see all), and a
    column's ``board`` cannot be changed after creation.
    """
    serializer_class = TicketColumnSerializer
    permission_classes = [CanManageColumns]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['board']
    ordering = ['position', 'created_at']

    def get_queryset(self):
        """
        Columns of the boards the caller can access (all boards for
        staff/superusers), annotated with tickets_count to avoid N+1 queries.
        Columns of other boards are 404 for every action, reads included.
        """
        if getattr(self, 'swagger_fake_view', False):
            return TicketColumn.objects.none()
        qs = TicketColumn.objects.select_related('created_by', 'board')
        user = self.request.user
        if not (user.is_superuser or user.is_staff):
            qs = qs.filter(board__in=accessible_boards(user).values('pk'))
        return qs.annotate(tickets_count=Count('tickets'))

    def perform_create(self, serializer):
        """Only allow creating columns on boards the caller can access."""
        board = serializer.validated_data.get('board')
        if board is not None and not accessible_boards(self.request.user).filter(pk=board.pk).exists():
            raise PermissionDenied('You do not have access to this board.')
        serializer.save()

    def get_serializer_class(self):
        """Use different serializers for different actions."""
        if self.action == 'create':
            return TicketColumnCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return TicketColumnUpdateSerializer
        return TicketColumnSerializer

    def destroy(self, request, *args, **kwargs):
        """Delete a ticket column (refused while it still holds tickets)."""
        column = self.get_object()
        if column.tickets.exists():
            return Response(
                {'error': 'Cannot delete column with tickets. Move tickets first.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        summary='Move a column to a new position on its board',
        description=(
            'Moves the column to the 0-based index `position` among the columns '
            'of its own board (ordered by position, then creation time) and '
            "renumbers that board's columns 0..n-1. Columns on other boards are "
            'never touched. Positions past the end are clamped. Returns the '
            "board's columns in their new order."
        ),
        request=inline_serializer(
            name='TicketColumnReorder',
            fields={
                'position': drf_serializers.IntegerField(
                    min_value=0, help_text='0-based target index on the board'
                ),
            },
        ),
        responses={
            200: inline_serializer(
                name='TicketColumnReorderResponse',
                fields={
                    'message': drf_serializers.CharField(),
                    'board': drf_serializers.IntegerField(),
                    'columns': TicketColumnSerializer(many=True),
                },
            ),
            400: OpenApiResponse(description='position missing, not a number or negative'),
            403: OpenApiResponse(description='Needs manage_columns or staff'),
        },
    )
    @action(detail=True, methods=['post'])
    def reorder(self, request, pk=None):
        """Reorder a column within its own board."""
        column = self.get_object()
        new_position = request.data.get('position')

        if new_position is None or new_position == '':
            return Response(
                {'error': 'Position is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            new_position = int(new_position)
        except (TypeError, ValueError):
            return Response(
                {'error': 'Position must be a number'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if new_position < 0:
            return Response(
                {'error': 'Position cannot be negative'},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            ordered_ids = list(
                TicketColumn.objects.select_for_update()
                .filter(board_id=column.board_id)
                .order_by('position', 'created_at', 'id')
                .values_list('id', flat=True)
            )
            ordered_ids.remove(column.id)
            new_position = min(new_position, len(ordered_ids))
            ordered_ids.insert(new_position, column.id)
            TicketColumn.objects.bulk_update(
                [TicketColumn(pk=col_id, position=index) for index, col_id in enumerate(ordered_ids)],
                ['position'],
            )

        columns = (
            self.get_queryset()
            .filter(board_id=column.board_id)
            .order_by('position', 'created_at', 'id')
        )
        return Response({
            'message': 'Column reordered successfully',
            'board': column.board_id,
            'columns': TicketColumnSerializer(columns, many=True, context={'request': request}).data,
        })

    @extend_schema(
        operation_id='kanban_board',
        summary='Get Kanban Board',
        description='Get all columns with their tickets organized for Kanban board display.',
        responses={
            200: KanbanBoardSerializer,
        },
        tags=['Kanban Board']
    )
    @action(detail=False, methods=['get'])
    def kanban_board(self, request):
        """Get all columns with their tickets for Kanban board view."""
        # Resolve the target board in a single query: explicit id if provided,
        # otherwise prefer the default board and fall back to the first one.
        # Only boards the caller can access (all for staff/superusers); an
        # inaccessible board_id is reported as not found.
        board_id = request.query_params.get('board_id')
        boards = accessible_boards(request.user)

        if board_id:
            try:
                board = boards.filter(id=int(board_id)).first()
            except (TypeError, ValueError):
                board = None
            if not board:
                return Response({'error': 'Board not found'}, status=404)
        else:
            board = boards.order_by('-is_default', 'id').first()

        columns_qs = (
            TicketColumn.objects.filter(board=board)
            if board is not None
            else TicketColumn.objects.filter(board__isnull=True)  # legacy fallback
        )

        # Prefetch all ticket relations to avoid N+1 queries
        from django.contrib.auth import get_user_model
        User = get_user_model()
        ticket_qs = (
            Ticket.objects
            .order_by('position_in_column', '-created_at')
            .select_related(
                'created_by', 'assigned_to', 'assigned_department',
                'column', 'column__board', 'column__created_by',
            )
            .prefetch_related(
                Prefetch('assigned_users', queryset=User.objects.only('id', 'first_name', 'last_name', 'email')),
                'assigned_groups',
                'tags',
                'attachments',
                Prefetch(
                    'ticketassignment_set',
                    queryset=TicketAssignment.objects.select_related('user', 'assigned_by'),
                ),
            )
            .annotate(comments_count=Count('comments'))
        )

        columns = (
            columns_qs
            .order_by('position')
            .select_related('board', 'created_by')
            .annotate(tickets_count=Count('tickets'))
            .prefetch_related(Prefetch('tickets', queryset=ticket_qs))
        )

        board_data = {
            'columns': columns
        }
        serializer = KanbanBoardSerializer(board_data, context={'request': request})
        return Response(serializer.data)


class TicketPermission(permissions.BasePermission):
    """
    Custom permission for tickets:
    - Anyone authenticated can create tickets
    - Only staff can assign tickets or close them
    - Users can view their own tickets or tickets assigned to them
    """
    
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated
    
    def has_object_permission(self, request, view, obj):
        # Staff users have full access
        if request.user.is_staff:
            return True

        # Check if user belongs to any group assigned to the ticket
        user_groups = request.user.tenant_groups.all()
        is_in_assigned_group = obj.assigned_groups.filter(id__in=user_groups).exists()

        # Board-level access — mirrors the TicketViewSet.get_queryset() rules
        # so a user who sees a ticket in the list can also open its detail.
        board = obj.column.board if obj.column_id else None
        has_board_access = bool(
            board and (
                board.board_users.filter(id=request.user.id).exists() or
                board.order_users.filter(id=request.user.id).exists() or
                board.board_groups.filter(id__in=user_groups).exists()
            )
        )

        # Users can view tickets they created, are assigned to, belong to an
        # assigned group, OR have access to the board the ticket lives on.
        if request.method in permissions.SAFE_METHODS:
            return (obj.created_by == request.user or
                   obj.assigned_to == request.user or
                   obj.assigned_users.filter(id=request.user.id).exists() or
                   is_in_assigned_group or
                   has_board_access)

        # Only staff can assign tickets or move to closed columns
        if request.method in ['PUT', 'PATCH']:
            # Check if trying to assign ticket or move to closed column
            if 'assigned_to_id' in request.data or 'assigned_group_ids' in request.data:
                return request.user.is_staff
            # Check if trying to move to a closed status column
            if 'column_id' in request.data:
                try:
                    column = TicketColumn.objects.get(id=request.data['column_id'])
                    if column.is_closed_status:
                        return request.user.is_staff
                except TicketColumn.DoesNotExist:
                    pass
            # Users can edit tickets they created, are assigned to, or belong to an assigned group (but not assign or close)
            return (obj.created_by == request.user or
                   obj.assigned_to == request.user or
                   obj.assigned_users.filter(id=request.user.id).exists() or
                   is_in_assigned_group)

        # Only staff can delete tickets
        if request.method == 'DELETE':
            return request.user.is_staff

        return False


class TicketFilter(django_filters.FilterSet):
    """
    Query-param filters for the ticket list. ``board`` filters through the
    column so a client can list every ticket on one board without first
    resolving its column ids; the remaining fields are the pre-existing
    exact-match filters.
    """
    board = django_filters.NumberFilter(field_name='column__board_id')

    class Meta:
        model = Ticket
        fields = ['priority', 'assigned_to', 'created_by', 'tags', 'column', 'assigned_groups']


class TicketViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing tickets with filtering and permissions.
    """
    serializer_class = TicketSerializer
    permission_classes = [TicketPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = TicketFilter
    search_fields = ['title', 'description']
    ordering_fields = ['created_at', 'updated_at', 'priority']
    ordering = ['-created_at']
    # The dashboard's generated client passes `{id}` as a string; keep the
    # documented type now that get_queryset is visible to schema generation.
    schema_path_param_types = {'id': OpenApiTypes.STR}

    def get_serializer_class(self):
        """Use list serializer for list action."""
        if self.action == 'list':
            return TicketListSerializer
        return TicketSerializer

    def get_queryset(self):
        """
        Filter queryset based on user permissions and query parameters.
        Annotate with counts to avoid N+1 queries.
        """
        if getattr(self, 'swagger_fake_view', False):
            # Schema generation runs with AnonymousUser; return an empty queryset
            # so drf-spectacular can still introspect the filterset parameters
            return Ticket.objects.none()

        queryset = Ticket.objects.annotate(
            comments_count=Count('comments', distinct=True),
            checklist_items_count=Count('checklist_items', distinct=True),
            completed_checklist_items_count=Count(
                'checklist_items',
                filter=Q(checklist_items__is_checked=True),
                distinct=True,
            ),
        ).select_related(
            'created_by', 'assigned_to', 'column', 'column__board',
            'assigned_department',
        ).prefetch_related(
            'tags',
            'assigned_groups',
            'assigned_users',
            Prefetch(
                'ticketassignment_set',
                queryset=TicketAssignment.objects.select_related('user', 'assigned_by'),
            ),
            Prefetch(
                'comments',
                queryset=TicketComment.objects.select_related('user').order_by('created_at'),
            ),
            Prefetch(
                'checklist_items',
                queryset=ChecklistItem.objects.select_related('created_by'),
            ),
            Prefetch(
                'attachments',
                queryset=TicketAttachment.objects.select_related('uploaded_by').order_by('-uploaded_at'),
            ),
            Prefetch(
                'payments',
                queryset=TicketPayment.objects.select_related('processed_by'),
            ),
            Prefetch(
                'form_submissions',
                queryset=TicketFormSubmission.objects.select_related(
                    'submitted_by', 'form',
                ).prefetch_related('selected_items').order_by('submitted_at'),
            ),
        )

        # Non-staff users can only see their tickets or tickets on boards they have access to
        if not self.request.user.is_staff:
            # Users can see tickets if:
            # 1. They created it, are assigned to it, or in assigned groups
            # 2. They have access to the board (via order_users, board_users, or board_groups)
            queryset = queryset.filter(ticket_visibility_q(self.request.user)).distinct()

        # Additional filtering by query parameters
        priority_filter = self.request.query_params.get('priority')
        if priority_filter:
            queryset = queryset.filter(priority=priority_filter)

        assigned_to_filter = self.request.query_params.get('assigned_to')
        if assigned_to_filter:
            queryset = queryset.filter(assigned_to_id=assigned_to_filter)

        created_by_filter = self.request.query_params.get('created_by')
        if created_by_filter:
            queryset = queryset.filter(created_by_id=created_by_filter)

        assigned_group_filter = self.request.query_params.get('assigned_group')
        if assigned_group_filter:
            queryset = queryset.filter(assigned_groups__id=assigned_group_filter)

        return queryset

    def perform_create(self, serializer):
        """Create a ticket and start time tracking if applicable."""
        # Set the user who created the ticket
        ticket = serializer.save(created_by=self.request.user)

        # If the assigned column has time tracking enabled, create initial time log
        open_time_log(ticket, ticket.column, self.request.user)

    def perform_update(self, serializer):
        """Save the ticket via PUT/PATCH.

        Tags the instance with the acting user so TicketHistory and the board
        broadcast can attribute the change, then runs a column change through
        the same open/close time-log logic as ``move_to_column``.
        """
        old_column = serializer.instance.column
        serializer.instance._current_user = self.request.user
        with transaction.atomic():
            ticket = serializer.save()
            new_column = ticket.column
            old_id = old_column.id if old_column else None
            new_id = new_column.id if new_column else None
            if old_id != new_id:
                close_active_time_log(ticket, old_column)
                open_time_log(ticket, new_column, self.request.user)

    def destroy(self, request, *args, **kwargs):
        """Delete a ticket with permission check based on tenant settings."""
        ticket = self.get_object()
        tenant = get_current_tenant(request)

        # Check if only superadmin can delete tickets
        if tenant.only_superadmin_can_delete_tickets:
            # Only superadmin can delete
            if not request.user.is_staff:
                return Response(
                    {'error': 'Only superadmins can delete tickets based on tenant settings.'},
                    status=status.HTTP_403_FORBIDDEN
                )
        else:
            # Both ticket owner and superadmin can delete
            if not request.user.is_staff and ticket.created_by != request.user:
                return Response(
                    {'error': 'Only the ticket owner or superadmins can delete this ticket.'},
                    status=status.HTTP_403_FORBIDDEN
                )

        # Delete the instance we already fetched (super().destroy would re-fetch
        # and lose the _current_user tag the post_delete broadcast reads).
        ticket._current_user = request.user
        self.perform_destroy(ticket)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def add_comment(self, request, pk=None):
        """Add a comment to a ticket."""
        ticket = self.get_object()
        serializer = TicketCommentSerializer(
            data=request.data, 
            context={'request': request}
        )
        if serializer.is_valid():
            serializer.save(ticket=ticket, user=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def comments(self, request, pk=None):
        """Get all comments for a ticket."""
        ticket = self.get_object()
        comments = ticket.comments.select_related('user').order_by('created_at')
        serializer = TicketCommentSerializer(comments, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def my_tickets(self, request):
        """Get tickets created by the current user."""
        queryset = self.get_queryset().filter(created_by=request.user)
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = TicketListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = TicketListSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def assigned_to_me(self, request):
        """Get tickets assigned to the current user."""
        queryset = self.get_queryset().filter(
            Q(assigned_to=request.user) |
            Q(assigned_users=request.user)
        )
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = TicketListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = TicketListSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['patch'], permission_classes=[permissions.IsAuthenticated])
    def assign(self, request, pk=None):
        """Assign a ticket to a user (staff only)."""
        if not request.user.is_staff:
            return Response(
                {'error': 'Only staff can assign tickets.'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        ticket = self.get_object()
        assigned_to_id = request.data.get('assigned_to_id')
        
        if assigned_to_id:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            try:
                assigned_user = User.objects.get(id=assigned_to_id)
                ticket.assigned_to = assigned_user
                ticket.save()
                serializer = self.get_serializer(ticket)
                return Response(serializer.data)
            except User.DoesNotExist:
                return Response(
                    {'error': 'User not found.'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
        else:
            ticket.assigned_to = None
            ticket.save()
            serializer = self.get_serializer(ticket)
            return Response(serializer.data)

    @extend_schema(
        operation_id='move_ticket_to_column',
        summary='Move Ticket to Column',
        description=(
            'Move a ticket to a different column in the Kanban board. '
            '`position_in_column` is the 0-based index the ticket should occupy in the '
            'target column (0 = top); values past the end are clamped to the end. '
            'Omit it (or send null) to append at the bottom. Both the source and the '
            'target column are renumbered so positions are always exactly 0..n-1.'
        ),
        request=inline_serializer(
            name='MoveTicketToColumnRequest',
            fields={
                'column_id': drf_serializers.IntegerField(),
                'position_in_column': drf_serializers.IntegerField(
                    required=False, allow_null=True, min_value=0,
                    help_text='0-based index in the target column; omitted = append',
                ),
            },
        ),
        responses={
            200: TicketSerializer,
            400: OpenApiResponse(description='Invalid column, missing column_id or non-integer position'),
        },
        tags=['Kanban Board']
    )
    @action(detail=True, methods=['patch'], permission_classes=[permissions.IsAuthenticated])
    def move_to_column(self, request, pk=None):
        """Move a ticket to a different column, inserting at a 0-based index."""
        ticket = self.get_object()
        column_id = request.data.get('column_id')
        new_position, position_error = _parse_position(request.data.get('position_in_column'))

        if not column_id:
            return Response(
                {'error': 'column_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if position_error:
            return Response({'error': position_error}, status=status.HTTP_400_BAD_REQUEST)

        try:
            new_column = TicketColumn.objects.get(id=column_id)
        except TicketColumn.DoesNotExist:
            return Response(
                {'error': 'Column not found'},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            # Lock both columns' tickets first (one query, deterministic order)
            # and only then the moving row. Taking the single-row lock first
            # lets two opposite-direction moves (A->B and B->A) deadlock.
            column_ids = {new_column.id}
            if ticket.column_id:
                column_ids.add(ticket.column_id)
            orders = _locked_column_order(column_ids)

            # Re-fetch the moving row (already locked above unless it was moved
            # to a third column concurrently -- then lock that column too).
            ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
            if ticket.column_id and ticket.column_id not in column_ids:
                column_ids.add(ticket.column_id)
                orders = _locked_column_order(column_ids)
            old_column = ticket.column

            # Handle time tracking for old column (if enabled and ticket is leaving a column)
            close_active_time_log(ticket, old_column)

            # Rebuild both orders in memory: pull the ticket out of the source,
            # drop it into the target at the requested (clamped) index, and
            # renumber both 0..n-1.
            target_ids = [tid for tid in orders[new_column.id] if tid != ticket.id]
            if new_position is None:
                index = len(target_ids)
            else:
                index = max(0, min(new_position, len(target_ids)))
            target_ids.insert(index, ticket.id)

            if old_column and old_column.id != new_column.id:
                source_ids = [tid for tid in orders[old_column.id] if tid != ticket.id]
                _renumber_column(old_column.id, source_ids)
            _renumber_column(new_column.id, target_ids, skip_id=ticket.id)

            ticket.column = new_column
            ticket.position_in_column = index
            ticket._current_user = request.user  # for TicketHistory + broadcast attribution
            ticket.save()

            # Handle time tracking for new column (if enabled)
            open_time_log(ticket, new_column, request.user)

        serializer = self.get_serializer(ticket)
        return Response(serializer.data)

    @extend_schema(
        operation_id='reorder_ticket_in_column',
        summary='Reorder Ticket in Column',
        description=(
            'Move a ticket to a 0-based index within its current column (0 = top); '
            'values past the end are clamped to the end. The column is renumbered so '
            'positions are always exactly 0..n-1.'
        ),
        request=inline_serializer(
            name='ReorderTicketInColumnRequest',
            fields={
                'position_in_column': drf_serializers.IntegerField(
                    min_value=0, help_text='0-based index in the current column',
                ),
            },
        ),
        responses={
            200: TicketSerializer,
            400: OpenApiResponse(description='Missing/invalid position or ticket has no column'),
        },
        tags=['Kanban Board']
    )
    @action(detail=True, methods=['patch'], permission_classes=[permissions.IsAuthenticated])
    def reorder_in_column(self, request, pk=None):
        """Reorder a ticket within its current column (0-based index)."""
        ticket = self.get_object()
        new_position, position_error = _parse_position(request.data.get('position_in_column'))

        if position_error:
            return Response({'error': position_error}, status=status.HTTP_400_BAD_REQUEST)
        if new_position is None:
            return Response(
                {'error': 'position_in_column is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not ticket.column_id:
            return Response(
                {'error': 'Ticket is not assigned to any column'},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            # Lock the whole column first (see move_to_column), then re-fetch
            # the row; if it was moved to another column meanwhile, reorder it
            # there instead.
            column_id = ticket.column_id
            orders = _locked_column_order({column_id})
            ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
            if not ticket.column_id:
                return Response(
                    {'error': 'Ticket is not assigned to any column'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if ticket.column_id != column_id:
                column_id = ticket.column_id
                orders = _locked_column_order({column_id})

            ordered_ids = [tid for tid in orders[column_id] if tid != ticket.id]
            index = max(0, min(new_position, len(ordered_ids)))
            ordered_ids.insert(index, ticket.id)
            _renumber_column(column_id, ordered_ids, skip_id=ticket.id)

            ticket.position_in_column = index
            ticket._current_user = request.user
            ticket.save()

        # Re-load through get_queryset() so the response carries the count
        # annotations (comments_count, checklist_items_count, ...); FOR UPDATE
        # and those aggregates cannot be combined, so this runs after the lock.
        ticket = self.get_queryset().get(pk=ticket.pk)
        serializer = self.get_serializer(ticket)
        return Response(serializer.data)

    @action(detail=True, methods=['get'], permission_classes=[permissions.IsAuthenticated])
    def history(self, request, pk=None):
        """Get the complete history of changes for a ticket."""
        ticket = self.get_object()
        history_entries = ticket.history.all().select_related('user')
        serializer = TicketHistorySerializer(history_entries, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'], permission_classes=[permissions.IsAuthenticated])
    def activity(self, request, pk=None):
        """Get unified activity timeline: comments + history events sorted chronologically."""
        ticket = self.get_object()

        # Fetch comments
        comments = list(ticket.comments.select_related('user').order_by('created_at').values(
            'id', 'comment', 'created_at', 'user__id', 'user__first_name', 'user__last_name', 'user__email'
        ))

        # Fetch history
        history = list(ticket.history.select_related('user').order_by('created_at').values(
            'id', 'action', 'field_name', 'old_value', 'new_value', 'description', 'created_at',
            'user__id', 'user__first_name', 'user__last_name', 'user__email'
        ))

        # Build unified timeline
        timeline = []

        for c in comments:
            timeline.append({
                'type': 'comment',
                'id': f'comment-{c["id"]}',
                'comment_id': c['id'],
                'text': c['comment'],
                'created_at': c['created_at'].isoformat(),
                'user': {
                    'id': c['user__id'],
                    'name': f"{c['user__first_name'] or ''} {c['user__last_name'] or ''}".strip() or c['user__email'],
                    'email': c['user__email'],
                },
            })

        for h in history:
            timeline.append({
                'type': 'event',
                'id': f'event-{h["id"]}',
                'action': h['action'],
                'field_name': h['field_name'],
                'old_value': h['old_value'],
                'new_value': h['new_value'],
                'description': h['description'],
                'created_at': h['created_at'].isoformat(),
                'user': {
                    'id': h['user__id'],
                    'name': f"{h['user__first_name'] or ''} {h['user__last_name'] or ''}".strip() or (h['user__email'] or 'System'),
                    'email': h['user__email'],
                } if h['user__id'] else {'id': None, 'name': 'System', 'email': ''},
            })

        # Sort chronologically (oldest first for chat-style)
        timeline.sort(key=lambda x: x['created_at'])

        return Response(timeline)


class TagViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing tags.
    All authenticated users can create, view, update, and delete tags. The web
    label dialog offers these to every user, so they are not gated by
    ``manage_tags`` (unchanged from before the parity work).
    """
    queryset = Tag.objects.select_related('created_by').all()
    serializer_class = TagSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name']
    ordering = ['name']


class TicketCommentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing ticket comments.
    """
    serializer_class = TicketCommentSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.OrderingFilter]
    ordering = ['created_at']

    def get_queryset(self):
        """Get comments for tickets the user has access to."""
        if self.request.user.is_staff:
            return TicketComment.objects.all().select_related('user', 'ticket')

        # Non-staff users can see comments on tickets they created, are assigned to, or whose group is assigned
        user_groups = self.request.user.tenant_groups.filter(is_active=True)
        return TicketComment.objects.filter(
            Q(ticket__created_by=self.request.user) |
            Q(ticket__assigned_to=self.request.user) |
            Q(ticket__assigned_users=self.request.user) |
            Q(ticket__assigned_groups__in=user_groups)
        ).select_related('user', 'ticket').distinct()

    def perform_create(self, serializer):
        """Set the user when creating a comment."""
        serializer.save(user=self.request.user)


class ChecklistItemViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing checklist items.
    """
    serializer_class = ChecklistItemSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['ticket', 'is_checked', 'created_by']
    ordering_fields = ['created_at', 'position']
    ordering = ['position', 'created_at']

    def get_queryset(self):
        """Filter checklist items based on user permissions."""
        if self.request.user.is_staff:
            return ChecklistItem.objects.all().select_related(
                'ticket', 'created_by'
            )

        # Non-staff users can only see checklist items for tickets they have access to.
        # The assigned_users join yields one row per assignment, so without
        # distinct() an item on a multi-assignee ticket appears several times
        # (duplicates in the list, MultipleObjectsReturned on the detail routes).
        return ChecklistItem.objects.filter(
            Q(ticket__created_by=self.request.user) |
            Q(ticket__assigned_to=self.request.user) |
            Q(ticket__assigned_users=self.request.user) |
            Q(created_by=self.request.user)
        ).select_related('ticket', 'created_by').distinct()

    @action(detail=True, methods=['patch'])
    def toggle_check(self, request, pk=None):
        """Toggle the checked status of a checklist item."""
        item = self.get_object()
        item.is_checked = not item.is_checked
        item.save()
        serializer = self.get_serializer(item)
        return Response(serializer.data)

    @action(detail=True, methods=['patch'])
    def reorder(self, request, pk=None):
        """Reorder checklist item within its parent ticket."""
        item = self.get_object()
        new_position = request.data.get('position')

        if new_position is None:
            return Response(
                {'error': 'Position is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            new_position = int(new_position)
        except ValueError:
            return Response(
                {'error': 'Position must be a number'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if new_position < 0:
            return Response(
                {'error': 'Position cannot be negative'},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            # Filter other items in the same ticket
            other_items = ChecklistItem.objects.filter(ticket=item.ticket)

            # Update positions
            if new_position > item.position:
                # Moving down
                other_items.filter(
                    position__gt=item.position,
                    position__lte=new_position
                ).update(position=F('position') - 1)
            else:
                # Moving up
                other_items.filter(
                    position__gte=new_position,
                    position__lt=item.position
                ).update(position=F('position') + 1)

            # Update the item's position
            item.position = new_position
            item.save()

        serializer = self.get_serializer(item)
        return Response(serializer.data)


class TicketAssignmentViewSet(viewsets.ModelViewSet):
    """ViewSet for managing ticket assignments."""
    serializer_class = TicketAssignmentSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Return assignments for a specific ticket."""
        ticket_pk = self.kwargs.get('ticket_pk')
        if ticket_pk:
            return TicketAssignment.objects.filter(ticket_id=ticket_pk).select_related('ticket', 'user', 'assigned_by')
        return TicketAssignment.objects.none()
    
    def perform_create(self, serializer):
        """Create a new ticket assignment.

        Writes ``ticket_id`` directly instead of loading the full ticket row
        (the FK constraint validates the id); keeps a cheap existence check
        so a bad URL still returns 404 instead of a 500 on IntegrityError.
        """
        from django.http import Http404
        ticket_pk = self.kwargs.get('ticket_pk')
        if not Ticket.objects.filter(pk=ticket_pk).only('id').exists():
            raise Http404('Ticket not found')
        serializer.save(ticket_id=ticket_pk, assigned_by=self.request.user)
    
    @action(detail=False, methods=['post'])
    def bulk_assign(self, request, ticket_pk=None):
        """Bulk assign users to a ticket."""
        try:
            ticket = Ticket.objects.get(pk=ticket_pk)
        except Ticket.DoesNotExist:
            return Response({'error': 'Ticket not found'}, status=status.HTTP_404_NOT_FOUND)

        # Permission check: only staff or ticket creator can bulk assign
        if not (request.user.is_staff or ticket.created_by == request.user):
            return Response(
                {'error': 'Only staff or the ticket owner can manage assignments.'},
                status=status.HTTP_403_FORBIDDEN
            )

        user_ids = request.data.get('user_ids', [])
        roles = request.data.get('roles', {})

        # Validate all users exist and are active before assigning
        if user_ids:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            valid_users = User.objects.filter(id__in=user_ids, is_active=True)
            if valid_users.count() != len(user_ids):
                return Response(
                    {'error': 'Some users not found or inactive'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        with transaction.atomic():
            # Clear existing assignments if replace is True
            if request.data.get('replace', False):
                TicketAssignment.objects.filter(ticket=ticket).delete()

            assignments = []
            for user_id in user_ids:
                role = roles.get(str(user_id), 'collaborator')
                assignment, created = TicketAssignment.objects.get_or_create(
                    ticket=ticket,
                    user_id=user_id,
                    defaults={'role': role, 'assigned_by': request.user}
                )
                assignments.append(assignment)

        serializer = TicketAssignmentSerializer(assignments, many=True)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=False, methods=['delete'])
    def bulk_unassign(self, request, ticket_pk=None):
        """Bulk remove user assignments from a ticket."""
        try:
            ticket = Ticket.objects.get(pk=ticket_pk)
        except Ticket.DoesNotExist:
            return Response({'error': 'Ticket not found'}, status=status.HTTP_404_NOT_FOUND)

        # Permission check: only staff or ticket creator can bulk unassign
        if not (request.user.is_staff or ticket.created_by == request.user):
            return Response(
                {'error': 'Only staff or the ticket owner can manage assignments.'},
                status=status.HTTP_403_FORBIDDEN
            )

        user_ids = request.data.get('user_ids', [])
        
        deleted_count, _ = TicketAssignment.objects.filter(
            ticket=ticket,
            user_id__in=user_ids
        ).delete()
        
        return Response(
            {'message': f'Removed {deleted_count} assignments'},
            status=status.HTTP_204_NO_CONTENT
        )


TIME_SUMMARY_MAX_DAYS = 365


def _parse_days(raw, default=30):
    """Parse ``?days=`` and clamp it to 1..TIME_SUMMARY_MAX_DAYS. Raises ValueError."""
    days = int(default if raw in (None, '') else raw)
    return max(1, min(days, TIME_SUMMARY_MAX_DAYS))


def _parse_boundary(raw, end_of_day=False):
    """Parse an ISO date (YYYY-MM-DD) or datetime query value into an aware datetime.

    A bare date is treated as the start of that day, or as the *exclusive* end
    boundary (start of the next day) when ``end_of_day`` is set, so
    ``end_date=2026-10-16`` includes the whole 16th.
    """
    import datetime as dt
    from django.utils.dateparse import parse_date, parse_datetime

    # parse_date first: parse_datetime also accepts a bare YYYY-MM-DD and would
    # swallow the end-of-day handling.
    day = parse_date(raw)
    if day is not None:
        if end_of_day:
            day = day + dt.timedelta(days=1)
        value = dt.datetime.combine(day, dt.time.min)
    else:
        value = parse_datetime(raw)
        if value is None:
            raise ValueError(raw)
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt.timezone.utc)
    return value


@extend_schema_view(
    retrieve=extend_schema(
        parameters=[OpenApiParameter('id', OpenApiTypes.STR, OpenApiParameter.PATH)],
    ),
)
@extend_schema(tags=['Time Tracking'])
class TicketTimeLogViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing ticket time logs."""
    # ``queryset`` is only here so drf-spectacular can resolve the model (and
    # therefore the ``ticket``/``column``/``user`` filter params); the real
    # scope comes from get_queryset().
    queryset = TicketTimeLog.objects.none()
    serializer_class = TicketTimeLogSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['ticket', 'column', 'user']
    ordering_fields = ['entered_at', 'exited_at', 'duration_seconds']
    ordering = ['-entered_at']

    def get_queryset(self):
        """Return time logs for tickets the user has access to."""
        if self.request.user.is_staff:
            return TicketTimeLog.objects.all().select_related(
                'ticket', 'column', 'user'
            )

        # Non-staff users can only see time logs for tickets they have access to
        return TicketTimeLog.objects.filter(
            Q(ticket__created_by=self.request.user) |
            Q(ticket__assigned_to=self.request.user) |
            Q(ticket__assigned_users=self.request.user)
        ).distinct().select_related('ticket', 'column', 'user')

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name='days',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description='Number of days to include in summary (default: 30, clamped to 1..365)',
                default=30
            )
        ],
        responses={
            200: TimeTrackingSummarySerializer,
            400: OpenApiResponse(description='days is not an integer'),
        },
        tags=['Time Tracking']
    )
    @action(detail=False, methods=['get'])
    def my_time_summary(self, request):
        """Get time tracking summary for the current user."""
        from django.db.models import Sum, Count, Avg
        from datetime import timedelta

        # Get date range from query params (default to last 30 days)
        try:
            days = _parse_days(request.query_params.get('days'))
        except (ValueError, TypeError):
            return Response(
                {'error': 'days must be a valid integer'},
                status=status.HTTP_400_BAD_REQUEST
            )
        start_date = timezone.now() - timedelta(days=days)
        
        # Base queryset for user's time logs
        user_logs = TicketTimeLog.objects.filter(
            user=request.user,
            entered_at__gte=start_date
        ).select_related('ticket', 'column')
        
        # Total time tracked
        total_time = user_logs.filter(
            duration_seconds__isnull=False
        ).aggregate(
            total_seconds=Sum('duration_seconds'),
            total_sessions=Count('id')
        )
        
        # Time by column
        time_by_column = user_logs.filter(
            duration_seconds__isnull=False
        ).values(
            'column__name', 'column__color'
        ).annotate(
            total_seconds=Sum('duration_seconds'),
            session_count=Count('id'),
            avg_seconds=Avg('duration_seconds')
        ).order_by('-total_seconds')
        
        # Recent activity
        recent_logs = user_logs.order_by('-entered_at')[:10]
        recent_logs_data = TicketTimeLogSerializer(recent_logs, many=True).data
        
        # Currently active sessions
        active_sessions = user_logs.filter(
            exited_at__isnull=True
        )
        active_sessions_data = TicketTimeLogSerializer(active_sessions, many=True).data
        
        # Daily breakdown for the period — single aggregate query instead of
        # materialising every log into Python.
        from django.db.models.functions import TruncDate
        daily_breakdown_qs = (
            user_logs.filter(duration_seconds__isnull=False)
            .annotate(date=TruncDate('entered_at'))
            .values('date')
            .annotate(
                total_seconds=Sum('duration_seconds'),
                session_count=Count('id'),
            )
            .order_by('-date')
        )
        daily_breakdown = [
            {
                'date': row['date'].isoformat() if row['date'] else None,
                'total_seconds': row['total_seconds'] or 0,
                'session_count': row['session_count'] or 0,
            }
            for row in daily_breakdown_qs
        ]
        
        return Response({
            'period_days': days,
            'start_date': start_date,
            'total_time_seconds': total_time['total_seconds'] or 0,
            'total_sessions': total_time['total_sessions'] or 0,
            'time_by_column': list(time_by_column),
            'daily_breakdown': daily_breakdown,
            'recent_activity': recent_logs_data,
            'active_sessions': active_sessions_data
        })

    @extend_schema(
        operation_id='time_logs_team_summary',
        summary='Team time summary',
        description=(
            'Per-user aggregate of ticket time logs over a period, computed in the '
            'database. Only users with at least one log in the period are listed. '
            'Requires staff or the `advanced_analytics` feature. Note that the feature '
            'is granted tenant-wide (subscription feature intersected with the caller\'s '
            'group features), so every non-staff member of a group holding '
            '`advanced_analytics` sees every colleague\'s hours, email and last '
            'activity -- the same scope as the dashboard\'s user statistics page. '
            'The window is `[start_date, end_date)` on `entered_at`; defaults to the '
            'last `days` days.'
        ),
        parameters=[
            OpenApiParameter(
                name='days', type=OpenApiTypes.INT, location=OpenApiParameter.QUERY,
                description='Window length in days ending at end_date (default 30, clamped to 1..365). '
                            'Ignored when start_date is given.',
                default=30,
            ),
            OpenApiParameter(
                name='start_date', type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY,
                description='ISO date (YYYY-MM-DD, start of day UTC) or ISO datetime. Inclusive.',
            ),
            OpenApiParameter(
                name='end_date', type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY,
                description='ISO date (whole day included) or ISO datetime (exclusive). Defaults to now.',
            ),
        ],
        responses={
            200: TeamSummarySerializer,
            400: OpenApiResponse(description='Invalid days/start_date/end_date, or start_date >= end_date'),
            403: OpenApiResponse(description='Not staff and no advanced_analytics feature'),
        },
        tags=['Time Tracking'],
    )
    @action(detail=False, methods=['get'])
    def team_summary(self, request):
        """DB-side per-user time aggregate for the team statistics table.

        Authorisation is deliberately "staff OR advanced_analytics" (plan Part C
        F4 / mobile WP12). ``User.has_feature`` resolves tenant-wide (the
        tenant's subscription features intersected with the user's group
        features), so a non-staff user in a group that holds
        ``advanced_analytics`` can read every colleague's totals. Tighten to
        ``is_staff and has_feature`` here if that ever needs to be narrower.
        """
        from datetime import timedelta
        from django.db.models import Sum
        from django.db.models.functions import Coalesce

        user = request.user
        if not (user.is_staff or user.has_feature('advanced_analytics')):
            return Response(
                {'error': 'You do not have permission to view team statistics.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            days = _parse_days(request.query_params.get('days'))
        except (ValueError, TypeError):
            return Response(
                {'error': 'days must be a valid integer'},
                status=status.HTTP_400_BAD_REQUEST
            )

        now = timezone.now()
        try:
            raw_end = request.query_params.get('end_date')
            end = _parse_boundary(raw_end, end_of_day=True) if raw_end else now
            raw_start = request.query_params.get('start_date')
            start = _parse_boundary(raw_start) if raw_start else end - timedelta(days=days)
        except ValueError:
            return Response(
                {'error': 'start_date and end_date must be ISO dates (YYYY-MM-DD) or datetimes'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if start >= end:
            return Response(
                {'error': 'start_date must be before end_date'},
                status=status.HTTP_400_BAD_REQUEST
            )

        rows = (
            TicketTimeLog.objects
            .filter(entered_at__gte=start, entered_at__lt=end)
            .values('user_id', 'user__first_name', 'user__last_name', 'user__email')
            .annotate(
                # Coalesce: Sum() is NULL for a user whose only logs in the
                # window are still open, and Postgres sorts NULLs first on
                # DESC -- such users must sort last (0 seconds), not first.
                total_seconds=Coalesce(Sum('duration_seconds'), 0),
                sessions=Count('id', filter=Q(duration_seconds__isnull=False)),
                active_sessions=Count('id', filter=Q(exited_at__isnull=True)),
                last_activity=Max('entered_at'),
            )
            .order_by('-total_seconds', 'user__email')
        )

        users = []
        total_seconds = 0
        total_sessions = 0
        for row in rows:
            seconds = row['total_seconds'] or 0
            total_seconds += seconds
            total_sessions += row['sessions']
            full_name = f"{row['user__first_name'] or ''} {row['user__last_name'] or ''}".strip()
            users.append({
                'user': {
                    'id': row['user_id'],
                    'full_name': full_name or row['user__email'],
                    'email': row['user__email'],
                },
                'total_seconds': seconds,
                'sessions': row['sessions'],
                'active_sessions': row['active_sessions'],
                'last_activity': row['last_activity'],
            })

        return Response({
            'period': {
                'start_date': start,
                'end_date': end,
                'days': max(1, round((end - start).total_seconds() / 86400)),
            },
            'users': users,
            'totals': {
                'total_seconds': total_seconds,
                'sessions': total_sessions,
                'users': len(users),
            },
        })

    @extend_schema(
        operation_id='time_logs_ticket_summary',
        summary='Ticket time summary',
        description=(
            'Time spent by one ticket: completed total, the currently open log (if any), '
            'per-column history and the last 20 logs. Visible to anyone who can view the ticket.'
        ),
        parameters=[
            OpenApiParameter(
                name='ticket', type=OpenApiTypes.INT, location=OpenApiParameter.QUERY,
                required=True, description='Ticket id',
            ),
        ],
        responses={
            200: TicketTimeSummarySerializer,
            400: OpenApiResponse(description='ticket missing or not an integer'),
            404: OpenApiResponse(description='Ticket not found or not visible to the user'),
        },
        tags=['Time Tracking'],
    )
    @action(detail=False, methods=['get'])
    def ticket_summary(self, request):
        """Per-ticket time panel data (total, active log, per-column history)."""
        from django.db.models import Sum

        raw_ticket = request.query_params.get('ticket')
        try:
            ticket_id = int(raw_ticket)
        except (TypeError, ValueError):
            return Response(
                {'error': 'ticket must be a valid integer'},
                status=status.HTTP_400_BAD_REQUEST
            )

        tickets = Ticket.objects.all()
        if not request.user.is_staff:
            tickets = tickets.filter(ticket_visibility_q(request.user)).distinct()
        if not tickets.filter(pk=ticket_id).exists():
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        logs = TicketTimeLog.objects.filter(ticket_id=ticket_id)
        completed = logs.filter(duration_seconds__isnull=False)

        total_seconds = completed.aggregate(total=Sum('duration_seconds'))['total'] or 0

        by_column = [
            {
                'column_id': row['column_id'],
                'column_name': row['column__name'],
                'column_color': row['column__color'],
                'total_seconds': row['total_seconds'] or 0,
                'sessions': row['sessions'],
            }
            for row in (
                completed
                .values('column_id', 'column__name', 'column__color')
                .annotate(total_seconds=Sum('duration_seconds'), sessions=Count('id'))
                .order_by('-total_seconds', 'column_id')
            )
        ]

        active_log = (
            logs.filter(exited_at__isnull=True)
            .select_related('column')
            .order_by('-entered_at')
            .first()
        )
        active = None
        if active_log:
            active = {
                'log_id': active_log.id,
                'column': {
                    'id': active_log.column_id,
                    'name': active_log.column.name,
                    'color': active_log.column.color,
                },
                'entered_at': active_log.entered_at,
                'elapsed_seconds': max(0, int((timezone.now() - active_log.entered_at).total_seconds())),
            }

        recent = logs.select_related('ticket', 'column', 'user').order_by('-entered_at')[:20]

        return Response({
            'ticket_id': ticket_id,
            'total_seconds': total_seconds,
            'active': active,
            'by_column': by_column,
            'logs': TicketTimeLogSerializer(recent, many=True).data,
        })


class BoardViewSet(viewsets.ModelViewSet):
    """ViewSet for managing kanban boards."""
    serializer_class = BoardSerializer
    permission_classes = [BoardPermission]
    # Same as TicketViewSet: the generated web client sends `{id}` as a string.
    schema_path_param_types = {'id': OpenApiTypes.STR}

    def get_queryset(self):
        """Return boards the user can access based on direct attachment or group membership."""
        if getattr(self, 'swagger_fake_view', False):
            return Board.objects.none()  # schema generation runs as AnonymousUser

        user = self.request.user

        # Scalar subquery so columns_count doesn't fan out the main row set
        from django.db.models.functions import Coalesce
        columns_count_sq = (
            TicketColumn.objects
            .filter(board=OuterRef('pk'))
            .order_by()
            .values('board')
            .annotate(c=Count('*'))
            .values('c')
        )
        base_qs = Board.objects.annotate(
            columns_count=Coalesce(
                Subquery(columns_count_sq, output_field=IntegerField()),
                0,
                output_field=IntegerField(),
            ),
        )

        return accessible_boards(user, base_qs).select_related('created_by').prefetch_related(
            'board_users', 'board_groups', 'order_users', 'columns'
        )

    def perform_create(self, serializer):
        """Set the created_by field when creating a board."""
        serializer.save(created_by=self.request.user)
    
    @extend_schema(
        # No operation_id / responses override: the generated clients depend on
        # `boards_kanban_board_retrieve` returning the viewset's Board schema.
        summary='Kanban Board',
        description=(
            'Columns and tickets for one board, with tickets grouped by column in '
            'display order. Pass `lite=1` to get compact ticket cards (no description, '
            'no nested column/creator, no comment bodies) with `comments_count` and '
            '`attachments_count` — the same response keys, much smaller payload, '
            'suited to the mobile board.'
        ),
        parameters=[
            OpenApiParameter(
                name='lite',
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                required=False,
                description='1|true: return compact ticket cards (TicketCard) instead of full ticket objects.',
            ),
        ],
        tags=['Kanban Board'],
    )
    @action(detail=True, methods=['get'])
    def kanban_board(self, request, pk=None):
        """Get kanban board data for a specific board."""
        board = self.get_object()
        lite = str(request.query_params.get('lite', '')).lower() in ('1', 'true', 'yes')

        # Build optimized ticket queryset with all relations prefetched
        from django.contrib.auth import get_user_model
        User = get_user_model()
        assignee_prefetch = Prefetch(
            'assigned_users', queryset=User.objects.only('id', 'first_name', 'last_name', 'email'),
        )
        if lite:
            # Card payload only needs assignees, tags and two counts
            ticket_serializer_class = TicketCardSerializer
            ticket_qs = (
                Ticket.objects
                .order_by('position_in_column', '-created_at')
                .select_related('assigned_to', 'column')
                .prefetch_related(assignee_prefetch, 'tags')
                .annotate(
                    comments_count=Count('comments', distinct=True),
                    attachments_count=Count('attachments', distinct=True),
                )
            )
        else:
            ticket_serializer_class = TicketListSerializer
            ticket_qs = (
                Ticket.objects
                .order_by('position_in_column', '-created_at')
                .select_related(
                    'created_by', 'assigned_to', 'assigned_department',
                    'column', 'column__board', 'column__created_by',
                )
                .prefetch_related(
                    assignee_prefetch,
                    'assigned_groups',
                    'tags',
                    'attachments',
                    Prefetch(
                        'ticketassignment_set',
                        queryset=TicketAssignment.objects.select_related('user', 'assigned_by'),
                    ),
                )
                .annotate(comments_count=Count('comments'))
            )

        # Get columns with prefetched tickets in a single query
        columns = (
            TicketColumn.objects
            .filter(board=board)
            .order_by('position')
            .select_related('board', 'created_by')
            .annotate(tickets_count=Count('tickets'))
            .prefetch_related(Prefetch('tickets', queryset=ticket_qs))
        )

        # Build response
        columns_data = []
        tickets_by_column = {}

        for column in columns:
            column_serializer = TicketColumnSerializer(column, context={'request': request})
            columns_data.append(column_serializer.data)

            # Use prefetched tickets instead of a separate query per column
            tickets_serializer = ticket_serializer_class(
                column.tickets.all(), many=True, context={'request': request}
            )
            tickets_by_column[column.id] = tickets_serializer.data

        return Response({
            'board': BoardSerializer(board, context={'request': request}).data,
            'columns': columns_data,
            'tickets_by_column': tickets_by_column
        })
    
    @extend_schema(
        summary='Make this board the default board (staff only)',
        request=None,
        responses={
            200: inline_serializer(
                name='BoardSetDefaultResponse',
                fields={'status': drf_serializers.CharField()},
            ),
            403: OpenApiResponse(description='Only staff can set the default board'),
        },
    )
    @action(detail=True, methods=['post'])
    def set_default(self, request, pk=None):
        """Set this board as the default board."""
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'error': 'Only staff can set default board'}, status=status.HTTP_403_FORBIDDEN)
        board = self.get_object()
        board.is_default = True
        board.save()
        return Response({'status': 'default set'})
    
    @action(detail=False, methods=['get'])
    def default(self, request):
        """Get the default board."""
        default_board = Board.objects.filter(is_default=True).first()
        if default_board:
            return Response(BoardSerializer(default_board, context={'request': request}).data)
        return Response({'error': 'No default board found'}, status=status.HTTP_404_NOT_FOUND)

    @extend_schema(
        methods=['GET'],
        summary="Get the board's Telegram connection",
        description='Readable by anyone who can view boards. 404 when none is configured.',
        request=None,
        responses={
            200: BoardTelegramConnectionSerializer,
            404: OpenApiResponse(description='No Telegram connection configured'),
        },
    )
    @extend_schema(
        methods=['POST'],
        summary='Create the Telegram connection (needs edit_boards or staff)',
        request=BoardTelegramConnectionCreateSerializer,
        responses={
            201: BoardTelegramConnectionSerializer,
            400: OpenApiResponse(description='Connection already exists or bot_token_raw missing'),
            403: OpenApiResponse(description='Needs edit_boards or staff'),
        },
    )
    @extend_schema(
        methods=['PUT'],
        summary='Update the Telegram connection (partial; needs edit_boards or staff)',
        request=BoardTelegramConnectionSerializer(partial=True),
        responses={
            200: BoardTelegramConnectionSerializer,
            403: OpenApiResponse(description='Needs edit_boards or staff'),
            404: OpenApiResponse(description='No connection to update'),
        },
    )
    @extend_schema(
        methods=['DELETE'],
        summary='Delete the Telegram connection (needs edit_boards or staff)',
        request=None,
        responses={
            204: OpenApiResponse(description='Deleted'),
            403: OpenApiResponse(description='Needs edit_boards or staff'),
            404: OpenApiResponse(description='No connection to delete'),
        },
    )
    @action(detail=True, methods=['get', 'post', 'put', 'delete'], url_path='telegram-connection')
    def telegram_connection(self, request, pk=None):
        """Manage Telegram bot connection for a board."""
        board = self.get_object()

        if request.method == 'GET':
            try:
                conn = board.telegram_connection
                return Response(BoardTelegramConnectionSerializer(conn).data)
            except BoardTelegramConnection.DoesNotExist:
                return Response({'detail': 'No Telegram connection configured.'}, status=status.HTTP_404_NOT_FOUND)

        elif request.method == 'POST':
            if hasattr(board, 'telegram_connection'):
                try:
                    board.telegram_connection
                    return Response({'detail': 'Connection already exists. Use PUT to update.'}, status=status.HTTP_400_BAD_REQUEST)
                except BoardTelegramConnection.DoesNotExist:
                    pass
            serializer = BoardTelegramConnectionSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save(board=board, created_by=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        elif request.method == 'PUT':
            try:
                conn = board.telegram_connection
            except BoardTelegramConnection.DoesNotExist:
                return Response({'detail': 'No connection to update.'}, status=status.HTTP_404_NOT_FOUND)
            serializer = BoardTelegramConnectionSerializer(conn, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)

        elif request.method == 'DELETE':
            try:
                conn = board.telegram_connection
                conn.delete()
                return Response(status=status.HTTP_204_NO_CONTENT)
            except BoardTelegramConnection.DoesNotExist:
                return Response({'detail': 'No connection to delete.'}, status=status.HTTP_404_NOT_FOUND)

    @extend_schema(
        summary='Send a Telegram test message (needs edit_boards or staff)',
        description=(
            'Uses bot_token_raw/chat_id from the body when given, otherwise the '
            "board's saved connection."
        ),
        request=inline_serializer(
            name='BoardTelegramTest',
            fields={
                'bot_token_raw': drf_serializers.CharField(required=False),
                'chat_id': drf_serializers.CharField(required=False),
            },
        ),
        responses={
            200: inline_serializer(
                name='BoardTelegramTestResponse',
                fields={
                    'status': drf_serializers.CharField(),
                    'message': drf_serializers.CharField(),
                },
            ),
            400: OpenApiResponse(description='Missing credentials or the message could not be sent'),
            403: OpenApiResponse(description='Needs edit_boards or staff'),
        },
    )
    @action(detail=True, methods=['post'], url_path='telegram-connection/test')
    def telegram_connection_test(self, request, pk=None):
        """Test Telegram bot connection by sending a test message."""
        from .telegram_utils import send_board_telegram_message

        board = self.get_object()
        bot_token_raw = request.data.get('bot_token_raw')
        chat_id = request.data.get('chat_id')

        # Use provided values or fall back to saved connection
        if not bot_token_raw or not chat_id:
            try:
                conn = board.telegram_connection
                bot_token_raw = bot_token_raw or conn.get_bot_token()
                chat_id = chat_id or conn.chat_id
            except BoardTelegramConnection.DoesNotExist:
                return Response(
                    {'detail': 'Provide bot_token_raw and chat_id, or save a connection first.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        if not bot_token_raw or not chat_id:
            return Response({'detail': 'bot_token_raw and chat_id are required.'}, status=status.HTTP_400_BAD_REQUEST)

        message = f"✅ <b>Telegram Bot Connected!</b>\n\nNotifications for board <b>{board.name}</b> are now active."
        success = send_board_telegram_message(bot_token_raw, chat_id, message)

        if success:
            return Response({'status': 'ok', 'message': 'Test message sent successfully.'})
        return Response({'status': 'error', 'message': 'Failed to send test message. Check your bot token and chat ID.'}, status=status.HTTP_400_BAD_REQUEST)


class TicketPaymentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing ticket payments.
    """
    queryset = TicketPayment.objects.all()
    serializer_class = TicketPaymentSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['ticket', 'payment_method', 'currency']
    ordering = ['-processed_at']

    def get_queryset(self):
        """Filter payments based on user permissions."""
        queryset = super().get_queryset().select_related('processed_by')

        # If user has ticket permissions or is superuser, return all
        if (self.request.user.is_superuser or
            self.request.user.has_permission('view_tickets')):
            return queryset

        # Otherwise, scope directly via joins instead of a correlated
        # subquery over Ticket — single query, uses existing indexes.
        user = self.request.user
        return queryset.filter(
            Q(ticket__created_by=user) |
            Q(ticket__assigned_to=user) |
            Q(ticket__assigned_users=user)
        ).distinct()

    @action(detail=False, methods=['post'])
    def process_payment(self, request):
        """Process a payment for a ticket."""
        ticket_id = request.data.get('ticket_id')
        amount = request.data.get('amount')
        
        if not ticket_id or not amount:
            return Response(
                {'error': 'ticket_id and amount are required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from decimal import Decimal, InvalidOperation
        try:
            ticket = Ticket.objects.get(id=ticket_id)
            amount = Decimal(str(amount))
        except (Ticket.DoesNotExist, ValueError, InvalidOperation):
            return Response(
                {'error': 'Invalid ticket or amount'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if user can modify this ticket
        if not (request.user.is_superuser or 
                request.user.has_permission('edit_tickets') or
                ticket.created_by == request.user):
            return Response(
                {'error': 'Permission denied'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        try:
            remaining_balance = ticket.add_payment(amount, request.user)
            
            # Get updated ticket data
            ticket.refresh_from_db()
            ticket_serializer = TicketSerializer(ticket, context={'request': request})
            
            return Response({
                'message': 'Payment processed successfully',
                'remaining_balance': remaining_balance,
                'ticket': ticket_serializer.data
            })
        except ValueError as e:
            return Response(
                {'error': str(e)}, 
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=False, methods=['get'])
    def payment_summary(self, request):
        """Get payment summary across all accessible tickets."""
        from django.db.models import Sum, Count, Q
        from decimal import Decimal
        
        # Get accessible tickets
        accessible_tickets = Ticket.objects.all()
        if not (request.user.is_superuser or request.user.has_permission('view_tickets')):
            accessible_tickets = accessible_tickets.filter(
                Q(created_by=request.user) |
                Q(assigned_to=request.user) |
                Q(assigned_users=request.user)
            ).distinct()
        
        # Filter by board if provided
        board_id = request.query_params.get('board_id')
        if board_id:
            accessible_tickets = accessible_tickets.filter(column__board_id=board_id)
        
        summary = accessible_tickets.aggregate(
            total_tickets=Count('id'),
            billable_tickets=Count('id', filter=Q(price__gt=0)),
            total_value=Sum('price'),
            total_paid=Sum('amount_paid'),
            paid_tickets=Count('id', filter=Q(is_paid=True)),
            unpaid_tickets=Count('id', filter=Q(is_paid=False, price__gt=0)),
            overdue_tickets=Count('id', filter=Q(
                is_paid=False, 
                payment_due_date__lt=timezone.now().date()
            ))
        )
        
        # Calculate remaining balance
        total_value = summary['total_value'] or Decimal('0.00')
        total_paid = summary['total_paid'] or Decimal('0.00')
        summary['remaining_balance'] = total_value - total_paid
        
        return Response(summary)


# ============================================================================
# New ViewSets for ItemList, ListItem, TicketForm, and TicketFormSubmission
# ============================================================================

@extend_schema_view(
    list=extend_schema(parameters=[OpenApiParameter(
                name='is_active', type=OpenApiTypes.BOOL, location=OpenApiParameter.QUERY,
                required=False, description='true|false: only active / only inactive rows',
            )]),
)
class ItemListViewSet(viewsets.ModelViewSet):
    """ViewSet for managing item lists.

    Reads are open to every authenticated user; create, update and delete
    need staff/superuser or ``manage_settings``.
    """
    queryset = ItemList.objects.select_related('created_by').prefetch_related('items', 'items__children').all()
    serializer_class = ItemListSerializer
    permission_classes = [CanManageSettings]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['title', 'description']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter lists based on user permissions."""
        from django.db.models import Count, Q as DQ
        queryset = super().get_queryset().annotate(
            _active_items_count=Count('items', filter=DQ(items__is_active=True))
        )

        # Filter by is_active if specified
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')

        return queryset

    def get_serializer_class(self):
        """Use minimal serializer for list action."""
        if self.action == 'list':
            return ItemListMinimalSerializer
        return ItemListSerializer

    @extend_schema(
        summary='Active root-level items of this list, with nested active children',
        operation_id='item_lists_root_items_retrieve',
        filters=False,
        responses={200: ListItemSerializer(many=True)},
    )
    @action(detail=True, methods=['get'], pagination_class=None)
    def root_items(self, request, pk=None):
        """Get only root-level items for this list.

        Loads all items in a single query, then groups children in memory so
        the nested ListItemSerializer does not recurse back to the DB.
        """
        item_list = self.get_object()
        all_items = list(item_list.items.filter(is_active=True))
        children_by_parent = {}
        roots = []
        for item in all_items:
            if item.parent_id is None:
                roots.append(item)
            else:
                children_by_parent.setdefault(item.parent_id, []).append(item)
        serializer = ListItemSerializer(
            roots,
            many=True,
            context={'request': request, '_children_by_parent': children_by_parent},
        )
        return Response(serializer.data)

    @extend_schema(
        summary='Public, active item lists (no auth)',
        operation_id='item_lists_public_retrieve',
        filters=False,
        responses={200: ItemListSerializer(many=True)},
    )
    @action(detail=False, methods=['get'], permission_classes=[permissions.AllowAny], pagination_class=None)
    def public(self, request):
        """Get all public item lists accessible to ecommerce clients."""
        queryset = self.get_queryset().filter(is_public=True, is_active=True)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary='Active items of a public list (no auth)',
        operation_id='item_lists_public_items_retrieve',
        filters=False,
        responses={200: ListItemSerializer(many=True), 404: OpenApiResponse(description='Public list not found')},
    )
    @action(detail=True, methods=['get'], permission_classes=[permissions.AllowAny], pagination_class=None)
    def public_items(self, request, pk=None):
        """Get items from a public list (only if the list is public).

        Loads all active items in a single query and passes an in-memory
        parent→children map through context so the recursive serializer
        resolves nested children without additional DB hits.
        """
        try:
            item_list = ItemList.objects.get(pk=pk, is_public=True, is_active=True)
        except ItemList.DoesNotExist:
            return Response(
                {'error': 'Public list not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        all_items = list(item_list.items.filter(is_active=True))
        children_by_parent = {}
        for item in all_items:
            if item.parent_id is not None:
                children_by_parent.setdefault(item.parent_id, []).append(item)
        serializer = ListItemSerializer(
            all_items,
            many=True,
            context={'request': request, '_children_by_parent': children_by_parent},
        )
        return Response(serializer.data)


@extend_schema_view(
    list=extend_schema(parameters=[
        OpenApiParameter(
            name='item_list_id', type=OpenApiTypes.INT, location=OpenApiParameter.QUERY,
            required=False, description='Only items of this list (same as item_list)',
        ),
        OpenApiParameter(
            name='root_only', type=OpenApiTypes.BOOL, location=OpenApiParameter.QUERY,
            required=False, description='true: only items without a parent',
        ),
    ]),
)
class ListItemViewSet(viewsets.ModelViewSet):
    """ViewSet for managing list items.

    Reads are open to every authenticated user; create, update, delete and
    reorder need staff/superuser or ``manage_settings``.
    """
    queryset = ListItem.objects.select_related('item_list', 'parent').prefetch_related('children').all()
    serializer_class = ListItemSerializer
    permission_classes = [CanManageSettings]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['item_list', 'parent', 'is_active']
    search_fields = ['label', 'custom_id']
    ordering = ['position', 'created_at']

    def get_queryset(self):
        """Filter items based on user permissions."""
        queryset = super().get_queryset()
        
        # Filter by list if specified
        item_list_id = self.request.query_params.get('item_list_id')
        if item_list_id:
            queryset = queryset.filter(item_list_id=item_list_id)
        
        # Filter by is_active if specified
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        
        # Filter only root items if specified
        root_only = self.request.query_params.get('root_only')
        if root_only and root_only.lower() == 'true':
            queryset = queryset.filter(parent__isnull=True)
        
        return queryset

    def get_serializer_class(self):
        """Use minimal serializer for list action to avoid deep nesting."""
        if self.action == 'list':
            return ListItemMinimalSerializer
        return ListItemSerializer

    @extend_schema(
        summary='Active direct children of this item (plain array)',
        operation_id='list_items_children_retrieve',
        filters=False,
        responses={200: ListItemSerializer(many=True)},
    )
    @action(detail=True, methods=['get'], pagination_class=None)
    def children(self, request, pk=None):
        """Get direct children of this item."""
        item = self.get_object()
        children = item.children.filter(is_active=True)
        serializer = ListItemSerializer(children, many=True, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary='All descendants of this item, flattened (plain array)',
        operation_id='list_items_all_descendants_retrieve',
        filters=False,
        responses={200: ListItemMinimalSerializer(many=True)},
    )
    @action(detail=True, methods=['get'], pagination_class=None)
    def all_descendants(self, request, pk=None):
        """Get all descendants of this item recursively."""
        item = self.get_object()
        descendants = item.get_all_children()
        serializer = ListItemMinimalSerializer(descendants, many=True, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary='Move an item among its siblings',
        request=inline_serializer(
            name='ListItemReorder',
            fields={'position': drf_serializers.IntegerField(min_value=0)},
        ),
        responses={200: ListItemSerializer, 400: OpenApiResponse(description='Invalid position')},
    )
    @action(detail=True, methods=['patch'])
    def reorder(self, request, pk=None):
        """Reorder item within its parent or list."""
        item = self.get_object()
        new_position = request.data.get('position')
        
        if new_position is None:
            return Response(
                {'error': 'Position is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            new_position = int(new_position)
        except ValueError:
            return Response(
                {'error': 'Position must be a number'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if new_position < 0:
            return Response(
                {'error': 'Position cannot be negative'},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            # Determine the siblings
            if item.parent:
                siblings = ListItem.objects.filter(
                    item_list=item.item_list,
                    parent=item.parent
                )
            else:
                siblings = ListItem.objects.filter(
                    item_list=item.item_list,
                    parent__isnull=True
                )
            
            # Update positions
            if new_position > item.position:
                # Moving down
                siblings.filter(
                    position__gt=item.position,
                    position__lte=new_position
                ).update(position=F('position') - 1)
            else:
                # Moving up
                siblings.filter(
                    position__gte=new_position,
                    position__lt=item.position
                ).update(position=F('position') + 1)
            
            # Update the item's position
            item.position = new_position
            item.save()
        
        serializer = self.get_serializer(item)
        return Response(serializer.data)


@extend_schema_view(
    list=extend_schema(parameters=[
        OpenApiParameter(
                name='is_active', type=OpenApiTypes.BOOL, location=OpenApiParameter.QUERY,
                required=False, description='true|false: only active / only inactive rows',
            ),
        OpenApiParameter(
            name='is_default', type=OpenApiTypes.BOOL, location=OpenApiParameter.QUERY,
            required=False, description='true|false: only the default form / only non-default forms',
        ),
    ]),
)
class TicketFormViewSet(viewsets.ModelViewSet):
    """ViewSet for managing ticket forms."""
    queryset = TicketForm.objects.all().select_related(
        'created_by', 'parent_form'
    ).prefetch_related(
        Prefetch(
            'item_lists',
            queryset=ItemList.objects.annotate(
                _active_items_count=Count('items', filter=Q(items__is_active=True))
            ),
        ),
        'child_forms',
        'submissions',
    )
    serializer_class = TicketFormSerializer
    # Reads for everyone authenticated; writes (incl. set_default) need
    # staff/superuser or manage_settings.
    permission_classes = [CanManageSettings]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['title', 'description']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter forms based on user permissions."""
        from django.db.models import Count
        queryset = super().get_queryset().annotate(
            _submissions_count=Count('submissions', distinct=True),
            _child_forms_count=Count('child_forms', distinct=True),
        )

        # Filter by is_active if specified
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')

        # Filter by is_default if specified
        is_default = self.request.query_params.get('is_default')
        if is_default is not None:
            queryset = queryset.filter(is_default=is_default.lower() == 'true')

        return queryset

    def get_serializer_class(self):
        """Use minimal serializer for list action."""
        if self.action == 'list':
            return TicketFormMinimalSerializer
        return TicketFormSerializer

    @extend_schema(
        summary='The default active form (or the first active one)',
        responses={200: TicketFormSerializer, 404: OpenApiResponse(description='No forms found')},
    )
    @action(detail=False, methods=['get'])
    def default(self, request):
        """Get the default form (uses the shared prefetched queryset)."""
        qs = self.get_queryset()
        default_form = qs.filter(is_default=True, is_active=True).first()
        if not default_form:
            default_form = qs.filter(is_active=True).first()

        if default_form:
            serializer = TicketFormSerializer(default_form, context={'request': request})
            return Response(serializer.data)
        return Response({'error': 'No forms found'}, status=status.HTTP_404_NOT_FOUND)

    @extend_schema(
        summary='Make this form the default form (staff or manage_settings)',
        description='The request body is ignored.',
        responses={
            200: inline_serializer(
                name='TicketFormSetDefaultResponse',
                fields={'status': drf_serializers.CharField()},
            ),
            403: OpenApiResponse(description='Needs manage_settings or staff'),
        },
    )
    @action(detail=True, methods=['post'])
    def set_default(self, request, pk=None):
        """Set this form as the default form (permission: CanManageSettings)."""
        form = self.get_object()
        form.is_default = True
        form.save()
        return Response({'status': 'default set'})

    @extend_schema(responses={200: TicketFormSerializer})
    @action(detail=True, methods=['get'])
    def with_lists(self, request, pk=None):
        """Get form with full list details (including items)."""
        form = self.get_object()
        serializer = TicketFormSerializer(form, context={'request': request})
        return Response(serializer.data)


class TicketFormSubmissionViewSet(viewsets.ModelViewSet):
    """ViewSet for managing ticket form submissions."""
    queryset = TicketFormSubmission.objects.select_related(
        'form', 'ticket', 'ticket__column', 'submitted_by'
    ).prefetch_related('selected_items').all()
    serializer_class = TicketFormSubmissionSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['form', 'ticket', 'submitted_by']
    ordering = ['-submitted_at']

    def get_queryset(self):
        """Filter submissions based on user permissions."""
        queryset = super().get_queryset()
        
        # Non-staff users can only see submissions for tickets they have access to
        if not self.request.user.is_staff:
            queryset = queryset.filter(
                Q(ticket__created_by=self.request.user) | 
                Q(ticket__assigned_to=self.request.user) |
                Q(ticket__assigned_users=self.request.user) |
                Q(submitted_by=self.request.user)
            ).distinct()
        
        return queryset

    @extend_schema(
        summary='Submissions of one form',
        description='`form_id` is required (400 without it). Returns a plain array (not paginated).',
        operation_id='form_submissions_by_form_retrieve',
        filters=False,
        parameters=[OpenApiParameter(
            name='form_id', type=OpenApiTypes.INT, location=OpenApiParameter.QUERY,
            required=False, description='Form id (required; validated by the view)',
        )],
        responses={200: TicketFormSubmissionSerializer(many=True), 400: OpenApiResponse(description='form_id is required')},
    )
    @action(detail=False, methods=['get'], pagination_class=None)
    def by_form(self, request):
        """Get submissions grouped by form."""
        form_id = request.query_params.get('form_id')
        if not form_id:
            return Response(
                {'error': 'form_id is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        queryset = self.get_queryset().filter(form_id=form_id)
        serializer = TicketFormSubmissionSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary='The latest submission for one ticket',
        description='`ticket_id` is required (400 without it).',
        parameters=[OpenApiParameter(
            name='ticket_id', type=OpenApiTypes.INT, location=OpenApiParameter.QUERY,
            required=False, description='Ticket id (required; validated by the view)',
        )],
        responses={
            200: TicketFormSubmissionSerializer,
            400: OpenApiResponse(description='ticket_id is required'),
            404: OpenApiResponse(description='Submission not found'),
        },
    )
    @action(detail=False, methods=['get'])
    def by_ticket(self, request):
        """Get submission for a specific ticket."""
        ticket_id = request.query_params.get('ticket_id')
        if not ticket_id:
            return Response(
                {'error': 'ticket_id is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # A ticket can hold several submissions; .get() raised
        # MultipleObjectsReturned (500). Return the newest one.
        submission = self.get_queryset().filter(ticket_id=ticket_id).order_by('-submitted_at', '-id').first()
        if submission is None:
            return Response({'error': 'Submission not found'}, status=status.HTTP_404_NOT_FOUND)
        serializer = TicketFormSubmissionSerializer(submission, context={'request': request})
        return Response(serializer.data)


class TicketAttachmentViewSet(viewsets.ModelViewSet):
    """ViewSet for managing ticket file attachments."""
    queryset = TicketAttachment.objects.select_related('ticket', 'uploaded_by').all()
    serializer_class = TicketAttachmentSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['ticket']

    def get_queryset(self):
        """Filter attachments based on user permissions."""
        user = self.request.user
        if user.is_staff:
            return TicketAttachment.objects.select_related('ticket', 'uploaded_by').all()

        # Users can see attachments for tickets they have access to
        user_groups = user.tenant_groups.all()
        return TicketAttachment.objects.filter(
            Q(ticket__created_by=user) |
            Q(ticket__assigned_to=user) |
            Q(ticket__assigned_users=user) |
            Q(ticket__assigned_groups__in=user_groups) |
            Q(ticket__column__board__order_users=user) |
            Q(ticket__column__board__board_users=user) |
            Q(ticket__column__board__board_groups__in=user_groups)
        ).select_related('ticket', 'uploaded_by').distinct()

    def perform_create(self, serializer):
        """Save attachment with uploaded_by set to current user."""
        serializer.save(uploaded_by=self.request.user)
