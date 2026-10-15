from rest_framework import viewsets, permissions, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Q, F, Count, Prefetch, Exists, OuterRef, Subquery, IntegerField
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse
from drf_spectacular.openapi import OpenApiTypes
from .models import (
    Ticket, Tag, TicketComment, TicketColumn, ChecklistItem,
    TicketAssignment, TicketTimeLog, Board, TicketPayment,
    ItemList, ListItem, TicketForm, TicketFormSubmission, TicketAttachment,
    BoardTelegramConnection
)
from .serializers import (
    TicketSerializer, TicketListSerializer, TagSerializer,
    TicketCommentSerializer, TicketColumnSerializer,
    TicketColumnCreateSerializer, TicketColumnUpdateSerializer,
    KanbanBoardSerializer, ChecklistItemSerializer,
    TicketAssignmentSerializer, TicketTimeLogSerializer,
    TimeTrackingSummarySerializer, BoardSerializer, TicketPaymentSerializer,
    ItemListSerializer, ItemListMinimalSerializer, ListItemSerializer, ListItemMinimalSerializer,
    TicketFormSerializer, TicketFormMinimalSerializer, TicketFormSubmissionSerializer,
    TicketAttachmentSerializer, TicketHistorySerializer,
    BoardTelegramConnectionSerializer
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
        else:
            # Default to view permission for unknown actions
            return (request.user.has_permission('view_boards') or
                   request.user.has_permission('access_orders'))

    def has_object_permission(self, request, view, obj):
        # For object-level permissions, we can add additional checks
        # For now, rely on the general permission check
        return self.has_permission(request, view)


class TicketColumnViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing ticket columns (Kanban board columns).
    """
    serializer_class = TicketColumnSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['board']
    ordering = ['position', 'created_at']

    def get_queryset(self):
        """Annotate queryset with tickets_count to avoid N+1 queries."""
        return TicketColumn.objects.select_related('created_by', 'board').annotate(
            tickets_count=Count('tickets')
        )

    def get_serializer_class(self):
        """Use different serializers for different actions."""
        if self.action == 'create':
            return TicketColumnCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return TicketColumnUpdateSerializer
        return TicketColumnSerializer

    def get_permissions(self):
        """
        Only superadmins can create, update, or delete columns.
        Anyone authenticated can view columns.
        """
        if self.action in ['create', 'update', 'partial_update', 'destroy', 'reorder']:
            permission_classes = [permissions.IsAuthenticated]
        else:
            permission_classes = [permissions.IsAuthenticated]
        
        return [permission() for permission in permission_classes]
    
    def check_superadmin_permission(self, request):
        """Check if user is superadmin."""
        if not request.user.is_superuser:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only superadmin users can manage ticket statuses.")
    
    def create(self, request, *args, **kwargs):
        """Create a new ticket column (superadmin only)."""
        self.check_superadmin_permission(request)
        return super().create(request, *args, **kwargs)
    
    def update(self, request, *args, **kwargs):
        """Update a ticket column (superadmin only)."""
        self.check_superadmin_permission(request)
        return super().update(request, *args, **kwargs)
    
    def partial_update(self, request, *args, **kwargs):
        """Partially update a ticket column (superadmin only)."""
        self.check_superadmin_permission(request)
        return super().partial_update(request, *args, **kwargs)
    
    def destroy(self, request, *args, **kwargs):
        """Delete a ticket column (superadmin only)."""
        self.check_superadmin_permission(request)
        column = self.get_object()
        if column.tickets.exists():
            return Response(
                {'error': 'Cannot delete column with tickets. Move tickets first.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'], permission_classes=[permissions.IsAuthenticated])
    def reorder(self, request, pk=None):
        """Reorder columns (superadmin only)."""
        self.check_superadmin_permission(request)
        column = self.get_object()
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
        
        with transaction.atomic():
            # Update positions of other columns
            if new_position > column.position:
                # Moving right - decrease position of columns in between
                TicketColumn.objects.filter(
                    position__gt=column.position,
                    position__lte=new_position
                ).update(position=F('position') - 1)
            else:
                # Moving left - increase position of columns in between
                TicketColumn.objects.filter(
                    position__gte=new_position,
                    position__lt=column.position
                ).update(position=F('position') + 1)
            
            # Update the column's position
            column.position = new_position
            column.save()
        
        return Response({'message': 'Column reordered successfully'})

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
        board_id = request.query_params.get('board_id')

        if board_id:
            board = Board.objects.filter(id=board_id).first()
            if not board:
                return Response({'error': 'Board not found'}, status=404)
        else:
            board = Board.objects.order_by('-is_default', 'id').first()

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


class TicketViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing tickets with filtering and permissions.
    """
    serializer_class = TicketSerializer
    permission_classes = [TicketPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['priority', 'assigned_to', 'created_by', 'tags', 'column', 'assigned_groups']
    search_fields = ['title', 'description']
    ordering_fields = ['created_at', 'updated_at', 'priority']
    ordering = ['-created_at']

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
            user_groups = self.request.user.tenant_groups.all()

            # Users can see tickets if:
            # 1. They created it, are assigned to it, or in assigned groups
            # 2. They have access to the board (via order_users, board_users, or board_groups)
            queryset = queryset.filter(
                Q(created_by=self.request.user) |
                Q(assigned_to=self.request.user) |
                Q(assigned_users=self.request.user) |
                Q(assigned_groups__in=user_groups) |
                Q(column__board__order_users=self.request.user) |  # Board order users
                Q(column__board__board_users=self.request.user) |  # Board users
                Q(column__board__board_groups__in=user_groups)     # Board groups
            ).distinct()

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
        if ticket.column and ticket.column.track_time:
            TicketTimeLog.objects.create(
                ticket=ticket,
                column=ticket.column,
                user=self.request.user
            )

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

        return super().destroy(request, *args, **kwargs)

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
        request={
            'type': 'object',
            'properties': {
                'column_id': {'type': 'integer'},
                'position_in_column': {
                    'type': 'integer', 'nullable': True, 'minimum': 0,
                    'description': '0-based index in the target column; omitted = append',
                },
            },
            'required': ['column_id']
        },
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
            # Re-fetch ticket with row lock to prevent race conditions
            ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
            old_column = ticket.column

            # Handle time tracking for old column (if enabled and ticket is leaving a column)
            if old_column and old_column.track_time:
                # Find the active time log for this ticket in the old column
                active_time_log = TicketTimeLog.objects.filter(
                    ticket=ticket,
                    column=old_column,
                    exited_at__isnull=True
                ).first()

                if active_time_log:
                    from django.utils import timezone
                    active_time_log.exited_at = timezone.now()
                    active_time_log.calculate_duration()

            # Lock both columns' tickets, then rebuild their orders in memory:
            # pull the ticket out of the source, drop it into the target at the
            # requested (clamped) index, and renumber both 0..n-1.
            column_ids = {new_column.id}
            if old_column:
                column_ids.add(old_column.id)
            orders = _locked_column_order(column_ids)

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
            if new_column.track_time:
                # Create a new time log entry for the new column
                TicketTimeLog.objects.create(
                    ticket=ticket,
                    column=new_column,
                    user=request.user
                )

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
        request={
            'type': 'object',
            'properties': {
                'position_in_column': {'type': 'integer', 'minimum': 0},
            },
            'required': ['position_in_column']
        },
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
            # Re-fetch with a row lock, then rebuild the column order in memory
            ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
            ordered_ids = [
                tid for tid in _locked_column_order({ticket.column_id})[ticket.column_id]
                if tid != ticket.id
            ]
            index = max(0, min(new_position, len(ordered_ids)))
            ordered_ids.insert(index, ticket.id)
            _renumber_column(ticket.column_id, ordered_ids, skip_id=ticket.id)

            ticket.position_in_column = index
            ticket._current_user = request.user
            ticket.save()

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
    All authenticated users can create, view, update, and delete tags.
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

        # Non-staff users can only see checklist items for tickets they have access to
        return ChecklistItem.objects.filter(
            Q(ticket__created_by=self.request.user) |
            Q(ticket__assigned_to=self.request.user) |
            Q(ticket__assigned_users=self.request.user) |
            Q(created_by=self.request.user)
        ).select_related('ticket', 'created_by')

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


@extend_schema(tags=['Time Tracking'])
class TicketTimeLogViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing ticket time logs."""
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
        ).select_related('ticket', 'column', 'user')
    
    @extend_schema(
        parameters=[
            OpenApiParameter(
                name='days',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description='Number of days to include in summary (default: 30)',
                default=30
            )
        ],
        responses={200: TimeTrackingSummarySerializer},
        tags=['Time Tracking']
    )
    @action(detail=False, methods=['get'])
    def my_time_summary(self, request):
        """Get time tracking summary for the current user."""
        from django.db.models import Sum, Count, Avg
        from datetime import datetime, timedelta
        from django.utils import timezone
        
        # Get date range from query params (default to last 30 days)
        try:
            days = int(request.query_params.get('days', 30))
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


class BoardViewSet(viewsets.ModelViewSet):
    """ViewSet for managing kanban boards."""
    serializer_class = BoardSerializer
    permission_classes = [BoardPermission]
    
    def get_queryset(self):
        """Return boards the user can access based on direct attachment or group membership."""
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

        # Superusers and staff can see all boards
        if user.is_superuser or user.is_staff:
            return base_qs.select_related('created_by').prefetch_related(
                'board_users', 'board_groups', 'order_users', 'columns'
            )

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

        return base_qs.annotate(
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
        ).select_related('created_by').prefetch_related(
            'board_users', 'board_groups', 'order_users', 'columns'
        )
    
    def perform_create(self, serializer):
        """Set the created_by field when creating a board."""
        serializer.save(created_by=self.request.user)
    
    @action(detail=True, methods=['get'])
    def kanban_board(self, request, pk=None):
        """Get kanban board data for a specific board."""
        board = self.get_object()

        # Build optimized ticket queryset with all relations prefetched
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
            tickets_serializer = TicketListSerializer(column.tickets.all(), many=True, context={'request': request})
            tickets_by_column[column.id] = tickets_serializer.data

        return Response({
            'board': BoardSerializer(board, context={'request': request}).data,
            'columns': columns_data,
            'tickets_by_column': tickets_by_column
        })
    
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

class ItemListViewSet(viewsets.ModelViewSet):
    """ViewSet for managing item lists."""
    queryset = ItemList.objects.select_related('created_by').prefetch_related('items', 'items__children').all()
    serializer_class = ItemListSerializer
    permission_classes = [permissions.IsAuthenticated]
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

    @action(detail=True, methods=['get'])
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

    @action(detail=False, methods=['get'], permission_classes=[permissions.AllowAny])
    def public(self, request):
        """Get all public item lists accessible to ecommerce clients."""
        queryset = self.get_queryset().filter(is_public=True, is_active=True)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'], permission_classes=[permissions.AllowAny])
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


class ListItemViewSet(viewsets.ModelViewSet):
    """ViewSet for managing list items."""
    queryset = ListItem.objects.select_related('item_list', 'parent').prefetch_related('children').all()
    serializer_class = ListItemSerializer
    permission_classes = [permissions.IsAuthenticated]
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

    @action(detail=True, methods=['get'])
    def children(self, request, pk=None):
        """Get direct children of this item."""
        item = self.get_object()
        children = item.children.filter(is_active=True)
        serializer = ListItemSerializer(children, many=True, context={'request': request})
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def all_descendants(self, request, pk=None):
        """Get all descendants of this item recursively."""
        item = self.get_object()
        descendants = item.get_all_children()
        serializer = ListItemMinimalSerializer(descendants, many=True, context={'request': request})
        return Response(serializer.data)

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
    permission_classes = [permissions.IsAuthenticated]
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

    @action(detail=True, methods=['post'])
    def set_default(self, request, pk=None):
        """Set this form as the default form."""
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'error': 'Only staff can set default form'}, status=status.HTTP_403_FORBIDDEN)
        form = self.get_object()
        form.is_default = True
        form.save()
        return Response({'status': 'default set'})

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
            )
        
        return queryset

    @action(detail=False, methods=['get'])
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

    @action(detail=False, methods=['get'])
    def by_ticket(self, request):
        """Get submission for a specific ticket."""
        ticket_id = request.query_params.get('ticket_id')
        if not ticket_id:
            return Response(
                {'error': 'ticket_id is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            submission = self.get_queryset().get(ticket_id=ticket_id)
            serializer = TicketFormSubmissionSerializer(submission, context={'request': request})
            return Response(serializer.data)
        except TicketFormSubmission.DoesNotExist:
            return Response({'error': 'Submission not found'}, status=status.HTTP_404_NOT_FOUND)


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
