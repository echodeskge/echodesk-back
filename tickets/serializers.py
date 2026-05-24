from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import (
    Ticket, Tag, TicketComment, TicketColumn, ChecklistItem,
    TicketAssignment, TicketTimeLog, Board, TicketPayment,
    ItemList, ListItem, TicketForm, TicketFormSubmission, TicketAttachment, TicketHistory,
    BoardTelegramConnection
)
from users.models import TenantGroup, Department

User = get_user_model()


class UserMinimalSerializer(serializers.ModelSerializer):
    """Minimal user serializer for ticket relationships."""

    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name']
        read_only_fields = ['id', 'email', 'first_name', 'last_name']


class TenantGroupMinimalSerializer(serializers.ModelSerializer):
    """Minimal tenant group serializer for ticket relationships."""

    class Meta:
        model = TenantGroup
        fields = ['id', 'name', 'description']
        read_only_fields = ['id', 'name', 'description']


class DepartmentMinimalSerializer(serializers.ModelSerializer):
    """Minimal department serializer for ticket relationships."""

    class Meta:
        model = Department
        fields = ['id', 'name', 'description']
        read_only_fields = ['id', 'name', 'description']


class BoardSerializer(serializers.ModelSerializer):
    """Serializer for Board model."""
    created_by = serializers.StringRelatedField(read_only=True)
    columns_count = serializers.IntegerField(read_only=True)  # From annotation
    order_users = UserMinimalSerializer(many=True, read_only=True)
    order_user_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        allow_empty=True,
        help_text='List of user IDs who can create orders on this board'
    )
    board_groups = TenantGroupMinimalSerializer(many=True, read_only=True)
    board_group_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        allow_empty=True,
        help_text='List of group IDs that can access this board'
    )
    board_users = UserMinimalSerializer(many=True, read_only=True)
    board_user_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        allow_empty=True,
        help_text='List of user IDs who can access this board'
    )
    payment_summary = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Board
        fields = [
            'id', 'name', 'description', 'is_default',
            'created_at', 'updated_at', 'created_by', 'columns_count',
            'order_users', 'order_user_ids', 'board_groups', 'board_group_ids',
            'board_users', 'board_user_ids', 'payment_summary'
        ]
        read_only_fields = ['created_at', 'updated_at', 'created_by']
    
    def get_payment_summary(self, obj):
        """Get payment summary for this board.

        Skipped on list responses to avoid a per-board aggregation N+1;
        callers that need it should hit the detail endpoint.
        """
        view = self.context.get('view')
        if view is not None and getattr(view, 'action', None) == 'list':
            return None
        return obj.get_payment_summary()
    
    def create(self, validated_data):
        order_user_ids = validated_data.pop('order_user_ids', [])
        board_group_ids = validated_data.pop('board_group_ids', [])
        board_user_ids = validated_data.pop('board_user_ids', [])
        validated_data['created_by'] = self.context['request'].user
        board = super().create(validated_data)

        if order_user_ids:
            board.order_users.set(order_user_ids)

        if board_group_ids:
            board.board_groups.set(board_group_ids)

        if board_user_ids:
            board.board_users.set(board_user_ids)

        return board

    def update(self, instance, validated_data):
        order_user_ids = validated_data.pop('order_user_ids', None)
        board_group_ids = validated_data.pop('board_group_ids', None)
        board_user_ids = validated_data.pop('board_user_ids', None)
        board = super().update(instance, validated_data)

        if order_user_ids is not None:
            board.order_users.set(order_user_ids)

        if board_group_ids is not None:
            board.board_groups.set(board_group_ids)

        if board_user_ids is not None:
            board.board_users.set(board_user_ids)

        return board


class TicketColumnSerializer(serializers.ModelSerializer):
    """Serializer for TicketColumn model."""
    created_by = serializers.StringRelatedField(read_only=True)
    tickets_count = serializers.IntegerField(read_only=True)  # From annotation
    
    class Meta:
        model = TicketColumn
        fields = [
            'id', 'name', 'description', 'color', 'position', 
            'is_default', 'is_closed_status', 'track_time', 'board', 'created_at', 'updated_at',
            'created_by', 'tickets_count'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']

    def create(self, validated_data):
        # Set created_by from request context
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)

    def validate_color(self, value):
        """Validate that color is a valid hex color code."""
        import re
        if not re.match(r'^#[0-9A-Fa-f]{6}$', value):
            raise serializers.ValidationError('Color must be a valid hex color code (e.g., #3B82F6)')
        return value


class TicketColumnCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating ticket columns."""
    
    class Meta:
        model = TicketColumn
        fields = ['name', 'description', 'color', 'position', 'is_default', 'is_closed_status', 'track_time', 'board']
        
    def create(self, validated_data):
        # Set created_by from request context
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class TicketColumnUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating ticket columns."""
    
    class Meta:
        model = TicketColumn
        fields = ['name', 'description', 'color', 'position', 'is_default', 'is_closed_status', 'track_time', 'board']


class TagSerializer(serializers.ModelSerializer):
    """Serializer for Tag/Label model."""
    created_by = UserMinimalSerializer(read_only=True)

    class Meta:
        model = Tag
        fields = ['id', 'name', 'color', 'description', 'created_at', 'created_by']
        read_only_fields = ['id', 'created_at', 'created_by']

    def create(self, validated_data):
        # Set created_by from request context
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)

    def validate_color(self, value):
        """Validate that color is a valid hex color code."""
        import re
        if not re.match(r'^#[0-9A-Fa-f]{6}$', value):
            raise serializers.ValidationError('Color must be a valid hex color code (e.g., #3B82F6)')
        return value


class TicketPaymentSerializer(serializers.ModelSerializer):
    """Serializer for TicketPayment model."""
    processed_by = UserMinimalSerializer(read_only=True)
    
    class Meta:
        model = TicketPayment
        fields = [
            'id', 'ticket', 'amount', 'currency', 'payment_method',
            'payment_reference', 'notes', 'processed_by', 'processed_at'
        ]
        read_only_fields = ['id', 'processed_at', 'processed_by']
    
    def create(self, validated_data):
        # Set processed_by from request context
        validated_data['processed_by'] = self.context['request'].user
        return super().create(validated_data)



class TicketAssignmentSerializer(serializers.ModelSerializer):
    """Serializer for TicketAssignment model."""
    user = UserMinimalSerializer(read_only=True)
    assigned_by = UserMinimalSerializer(read_only=True)
    
    class Meta:
        model = TicketAssignment
        fields = ['id', 'user', 'role', 'assigned_at', 'assigned_by']
        read_only_fields = ['id', 'assigned_at', 'assigned_by']


class TicketCommentSerializer(serializers.ModelSerializer):
    """Serializer for TicketComment model."""
    user = UserMinimalSerializer(read_only=True)
    user_id = serializers.IntegerField(write_only=True, required=False)
    
    class Meta:
        model = TicketComment
        fields = ['id', 'ticket', 'user', 'user_id', 'comment', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        # Set user from request context if not provided
        if 'user_id' not in validated_data:
            validated_data['user'] = self.context['request'].user
        else:
            user_id = validated_data.pop('user_id')
            validated_data['user'] = User.objects.get(id=user_id)
        return super().create(validated_data)


class ChecklistItemSerializer(serializers.ModelSerializer):
    """Serializer for ChecklistItem model."""
    created_by = UserMinimalSerializer(read_only=True)

    class Meta:
        model = ChecklistItem
        fields = [
            'id', 'ticket', 'text', 'is_checked', 'position',
            'created_at', 'updated_at', 'created_by'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']

    def create(self, validated_data):
        # Set created_by from request context
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class TicketSerializer(serializers.ModelSerializer):
    """Serializer for Ticket model."""
    created_by = UserMinimalSerializer(read_only=True)
    assigned_to = UserMinimalSerializer(read_only=True)
    assigned_to_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    assigned_users = UserMinimalSerializer(many=True, read_only=True)
    assignments = TicketAssignmentSerializer(source='ticketassignment_set', many=True, read_only=True)
    assigned_user_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        allow_empty=True,
        help_text='List of user IDs to assign to this ticket'
    )
    assignment_roles = serializers.DictField(
        child=serializers.CharField(max_length=20),
        write_only=True,
        required=False,
        help_text='Dictionary mapping user IDs to roles (e.g., {"1": "primary", "2": "collaborator"})'
    )
    assigned_groups = TenantGroupMinimalSerializer(many=True, read_only=True)
    assigned_group_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        allow_empty=True,
        help_text='List of group IDs to assign to this ticket'
    )
    assigned_department = DepartmentMinimalSerializer(read_only=True)
    assigned_department_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    column = TicketColumnSerializer(read_only=True)
    column_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    tags = TagSerializer(many=True, read_only=True)
    tag_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        allow_empty=True
    )
    comments = TicketCommentSerializer(many=True, read_only=True)
    comments_count = serializers.IntegerField(read_only=True)  # From annotation
    # Add new fields for rich content
    checklist_items = ChecklistItemSerializer(many=True, read_only=True)
    checklist_items_count = serializers.IntegerField(read_only=True)  # From annotation
    completed_checklist_items_count = serializers.IntegerField(read_only=True)  # From annotation
    status = serializers.ReadOnlyField()  # Dynamic status from column
    is_closed = serializers.ReadOnlyField()  # Dynamic closed status from column
    
    # Payment fields
    payments = TicketPaymentSerializer(many=True, read_only=True)
    remaining_balance = serializers.ReadOnlyField()
    payment_status = serializers.ReadOnlyField()
    is_overdue = serializers.ReadOnlyField()

    # Form submissions field - returns all form submissions for this ticket
    form_submissions = serializers.SerializerMethodField(read_only=True)

    # Attachments field - returns all attachments for this ticket
    attachments = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Ticket
        fields = [
            'id', 'title', 'description', 'rich_description', 'description_format',
            'status', 'priority', 'is_closed', 'is_order', 'column', 'column_id', 'position_in_column',
            'created_at', 'updated_at', 'created_by', 'assigned_to', 'assigned_to_id',
            'assigned_users', 'assignments', 'assigned_user_ids', 'assignment_roles',
            'assigned_groups', 'assigned_group_ids',
            'assigned_department', 'assigned_department_id',
            'tags', 'tag_ids', 'comments', 'comments_count',
            'checklist_items', 'checklist_items_count', 'completed_checklist_items_count',
            'price', 'currency', 'is_paid', 'amount_paid', 'payment_due_date',
            'payments', 'remaining_balance', 'payment_status', 'is_overdue', 'form_submissions',
            'attachments'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by', 'status', 'is_closed']

    def get_form_submissions(self, obj):
        """Get all form submissions for this ticket (uses prefetched data)."""
        submissions = obj.form_submissions.all()  # Uses prefetch, already ordered
        if submissions:
            return TicketFormSubmissionSerializer(submissions, many=True, context=self.context).data
        return []

    def get_attachments(self, obj):
        """Get all attachments for this ticket (uses prefetched data)."""
        attachments = obj.attachments.all()  # Uses prefetch, already ordered
        return TicketAttachmentSerializer(attachments, many=True, context=self.context).data

    def create(self, validated_data):
        tag_ids = validated_data.pop('tag_ids', [])
        assigned_to_id = validated_data.pop('assigned_to_id', None)
        assigned_user_ids = validated_data.pop('assigned_user_ids', [])
        assigned_group_ids = validated_data.pop('assigned_group_ids', [])
        assigned_department_id = validated_data.pop('assigned_department_id', None)
        assignment_roles = validated_data.pop('assignment_roles', {})
        column_id = validated_data.pop('column_id', None)
        is_order = validated_data.get('is_order', False)

        # Set created_by from request context
        validated_data['created_by'] = self.context['request'].user
        current_user = self.context['request'].user

        # Set assigned_to if provided
        if assigned_to_id:
            validated_data['assigned_to'] = User.objects.get(id=assigned_to_id)

        # Set assigned_department if provided
        if assigned_department_id:
            validated_data['assigned_department'] = Department.objects.get(id=assigned_department_id)

        # Handle order-specific logic
        if is_order and column_id:
            # For orders, automatically assign to the first column of the board
            column = TicketColumn.objects.get(id=column_id)
            board = column.board
            first_column = board.columns.order_by('position').first()
            if first_column:
                validated_data['column'] = first_column
            else:
                validated_data['column'] = column  # fallback to provided column
        elif column_id:
            # Set column if provided (regular tickets)
            validated_data['column'] = TicketColumn.objects.get(id=column_id)

        ticket = Ticket.objects.create(**validated_data)

        # Set tags
        if tag_ids:
            ticket.tags.set(tag_ids)

        # Set assigned groups
        if assigned_group_ids:
            ticket.assigned_groups.set(assigned_group_ids)

        # Handle multiple user assignments
        if assigned_user_ids:
            for user_id in assigned_user_ids:
                role = assignment_roles.get(str(user_id), 'collaborator')
                TicketAssignment.objects.create(
                    ticket=ticket,
                    user_id=user_id,
                    role=role,
                    assigned_by=current_user
                )

        return ticket

    def update(self, instance, validated_data):
        tag_ids = validated_data.pop('tag_ids', None)
        assigned_to_id = validated_data.pop('assigned_to_id', None)
        assigned_user_ids = validated_data.pop('assigned_user_ids', None)
        assigned_group_ids = validated_data.pop('assigned_group_ids', None)
        assigned_department_id = validated_data.pop('assigned_department_id', None)
        assignment_roles = validated_data.pop('assignment_roles', {})
        column_id = validated_data.pop('column_id', None)

        current_user = self.context['request'].user

        # Handle assigned_to field
        if assigned_to_id is not None:
            if assigned_to_id:
                validated_data['assigned_to'] = User.objects.get(id=assigned_to_id)
            else:
                validated_data['assigned_to'] = None

        # Handle assigned_department field
        if assigned_department_id is not None:
            if assigned_department_id:
                validated_data['assigned_department'] = Department.objects.get(id=assigned_department_id)
            else:
                validated_data['assigned_department'] = None

        # Handle column field
        if column_id is not None:
            if column_id:
                validated_data['column'] = TicketColumn.objects.get(id=column_id)
            else:
                validated_data['column'] = None

        ticket = super().update(instance, validated_data)

        # Update tags if provided
        if tag_ids is not None:
            ticket.tags.set(tag_ids)

        # Update assigned groups if provided
        if assigned_group_ids is not None:
            ticket.assigned_groups.set(assigned_group_ids)

        # Handle multiple user assignments update
        if assigned_user_ids is not None:
            # Check minimum users requirement — cache tenant on the request
            # so repeated updates in the same request don't re-query.
            from .views import get_current_tenant
            tenant = get_current_tenant(self.context['request'])
            min_users_required = tenant.min_users_per_ticket

            # Get current assignment count
            current_assignment_count = ticket.assigned_users.count()
            new_assignment_count = len(assigned_user_ids)

            # Only enforce minimum if:
            # 1. There's a minimum set (> 0)
            # 2. Current count is >= minimum (i.e., we've already reached the threshold)
            # 3. User is not superadmin
            # 4. New count would be less than minimum
            if (min_users_required > 0 and
                current_assignment_count >= min_users_required and
                not current_user.is_staff and
                new_assignment_count < min_users_required):
                raise serializers.ValidationError({
                    'assigned_user_ids': f'Cannot reduce assigned users below {min_users_required}. Only superadmins can do this.'
                })

            # Clear existing assignments
            TicketAssignment.objects.filter(ticket=ticket).delete()

            # Add new assignments
            for user_id in assigned_user_ids:
                role = assignment_roles.get(str(user_id), 'collaborator')
                TicketAssignment.objects.create(
                    ticket=ticket,
                    user_id=user_id,
                    role=role,
                    assigned_by=current_user
                )

        return ticket


class TicketListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for ticket lists."""
    created_by = UserMinimalSerializer(read_only=True)
    assigned_to = UserMinimalSerializer(read_only=True)
    assigned_users = UserMinimalSerializer(many=True, read_only=True)
    assigned_groups = TenantGroupMinimalSerializer(many=True, read_only=True)
    assignments = TicketAssignmentSerializer(source='ticketassignment_set', many=True, read_only=True)
    column = TicketColumnSerializer(read_only=True)
    tags = TagSerializer(many=True, read_only=True)
    comments_count = serializers.IntegerField(read_only=True)  # From annotation
    status = serializers.ReadOnlyField()  # Dynamic status from column
    is_closed = serializers.ReadOnlyField()  # Dynamic closed status from column

    class Meta:
        model = Ticket
        fields = [
            'id', 'title', 'description', 'description_format',
            'status', 'priority', 'is_closed', 'column', 'position_in_column',
            'created_at', 'updated_at', 'created_by', 'assigned_to', 'assigned_users',
            'assigned_groups', 'assignments', 'tags', 'comments_count'
        ]
        read_only_fields = fields


class KanbanBoardSerializer(serializers.Serializer):
    """Serializer for Kanban board data with columns and tickets."""
    columns = TicketColumnSerializer(many=True, read_only=True)
    tickets_by_column = serializers.SerializerMethodField()
    
    def get_tickets_by_column(self, obj):
        """Get tickets organized by column using prefetched data."""
        columns = obj.get('columns', [])
        tickets_data = {}

        for column in columns:
            # Use prefetched tickets (set by Prefetch in the view) instead of a separate query per column
            tickets = column.tickets.all()
            tickets_data[column.id] = TicketListSerializer(tickets, many=True, context=self.context).data

        return tickets_data


class TicketTimeLogSerializer(serializers.ModelSerializer):
    """Serializer for TicketTimeLog model."""
    ticket = serializers.StringRelatedField(read_only=True)
    column = TicketColumnSerializer(read_only=True)
    user = UserMinimalSerializer(read_only=True)
    duration_display = serializers.ReadOnlyField()
    
    class Meta:
        model = TicketTimeLog
        fields = [
            'id', 'ticket', 'column', 'user', 'entered_at', 'exited_at',
            'duration_seconds', 'duration_display'
        ]
        read_only_fields = fields


class TimeTrackingSummarySerializer(serializers.Serializer):
    """Serializer for time tracking summary data."""
    period_days = serializers.IntegerField()
    start_date = serializers.DateTimeField()
    total_time_seconds = serializers.IntegerField()
    total_sessions = serializers.IntegerField()
    time_by_column = serializers.ListField()
    daily_breakdown = serializers.ListField()
    recent_activity = TicketTimeLogSerializer(many=True)
    active_sessions = TicketTimeLogSerializer(many=True)


# ============================================================================
# New Serializers for ItemList, ListItem, TicketForm, and TicketFormSubmission
# ============================================================================

class ListItemSerializer(serializers.ModelSerializer):
    """Serializer for ListItem model with recursive children support."""
    created_by = UserMinimalSerializer(read_only=True)
    children = serializers.SerializerMethodField()
    full_path = serializers.ReadOnlyField(source='get_full_path')

    class Meta:
        model = ListItem
        fields = [
            'id', 'item_list', 'label', 'custom_id', 'parent', 'parent_list_item', 'position',
            'is_active', 'custom_data', 'created_at', 'updated_at', 'created_by',
            'children', 'full_path'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']

    def get_children(self, obj):
        """Return this node's active children.

        If the caller has pre-grouped children by parent_id into
        ``context['_children_by_parent']`` (done by ItemListSerializer and the
        root_items / public_items actions), we read from that map instead of
        firing a fresh query per node. Otherwise fall back to a DB fetch.
        """
        children_by_parent = self.context.get('_children_by_parent')
        if children_by_parent is not None:
            children = children_by_parent.get(obj.id, [])
        else:
            children = obj.children.filter(is_active=True)
        return ListItemSerializer(children, many=True, context=self.context).data

    def create(self, validated_data):
        # Set created_by from request context
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class ListItemMinimalSerializer(serializers.ModelSerializer):
    """Minimal serializer for ListItem without children (to avoid deep recursion)."""
    full_path = serializers.ReadOnlyField(source='get_full_path')
    item_list = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = ListItem
        fields = ['id', 'item_list', 'label', 'custom_id', 'parent', 'position', 'is_active', 'full_path']
        read_only_fields = fields


class ItemListSerializer(serializers.ModelSerializer):
    """Serializer for ItemList model."""
    created_by = UserMinimalSerializer(read_only=True)
    items = ListItemSerializer(many=True, read_only=True)
    items_count = serializers.SerializerMethodField()
    root_items = serializers.SerializerMethodField()

    class Meta:
        model = ItemList
        fields = [
            'id', 'title', 'description', 'is_active', 'is_public', 'parent_list', 'custom_fields_schema',
            'created_at', 'updated_at', 'created_by',
            'items', 'items_count', 'root_items'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']

    def to_representation(self, instance):
        """Prebuild children-by-parent map from the prefetched items cache so
        every nested ListItemSerializer resolves children from memory instead
        of hitting the DB. Shared via context so both ``items`` and
        ``root_items`` benefit, and counts come out of the same cache.
        """
        prefetched = list(instance.items.all())
        active_items = [i for i in prefetched if i.is_active]
        children_by_parent = {}
        for item in active_items:
            if item.parent_id is not None:
                children_by_parent.setdefault(item.parent_id, []).append(item)
        self.context['_children_by_parent'] = children_by_parent
        self.context['_active_items'] = active_items
        return super().to_representation(instance)

    def get_items_count(self, obj):
        """Get the number of active items in this list (uses prefetched cache)."""
        active_items = self.context.get('_active_items')
        if active_items is not None:
            return len(active_items)
        if hasattr(obj, '_active_items_count'):
            return obj._active_items_count
        return obj.items.filter(is_active=True).count()

    def get_root_items(self, obj):
        """Root-level active items (without parents)."""
        active_items = self.context.get('_active_items')
        if active_items is None:
            active_items = [i for i in obj.items.all() if i.is_active]
        roots = [i for i in active_items if i.parent_id is None]
        return ListItemSerializer(roots, many=True, context=self.context).data

    def create(self, validated_data):
        # Set created_by from request context
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class ItemListMinimalSerializer(serializers.ModelSerializer):
    """Minimal serializer for ItemList without nested items."""
    created_by = UserMinimalSerializer(read_only=True)
    items_count = serializers.SerializerMethodField()

    class Meta:
        model = ItemList
        fields = [
            'id', 'title', 'description', 'is_active', 'is_public', 'parent_list', 'custom_fields_schema',
            'created_at', 'updated_at', 'created_by', 'items_count'
        ]
        read_only_fields = fields

    def get_items_count(self, obj):
        """Get the number of active items in this list."""
        if hasattr(obj, '_active_items_count'):
            return obj._active_items_count
        return obj.items.filter(is_active=True).count()


class TicketFormMinimalSerializer(serializers.ModelSerializer):
    """Minimal serializer for TicketForm (to avoid circular references)."""
    created_by = UserMinimalSerializer(read_only=True)
    # Add child_forms as a self-reference to show child forms that need to be filled
    # We use the same minimal serializer to avoid deep nesting
    child_forms = serializers.SerializerMethodField()
    # Add parent_form as a simple dict to avoid recursion
    parent_form = serializers.SerializerMethodField()
    item_lists = ItemListMinimalSerializer(many=True, read_only=True)
    submissions_count = serializers.SerializerMethodField()

    class Meta:
        model = TicketForm
        fields = [
            'id', 'title', 'description', 'is_default', 'is_active',
            'created_at', 'created_by', 'child_forms', 'parent_form',
            'item_lists', 'submissions_count', 'custom_fields'
        ]
        read_only_fields = fields

    def get_child_forms(self, obj):
        """Get child forms without deep nesting."""
        child_forms = obj.child_forms.filter(is_active=True)
        # Return simple dict to avoid recursion
        return [
            {
                'id': child.id,
                'title': child.title,
                'description': child.description,
                'is_active': child.is_active,
            }
            for child in child_forms
        ]

    def get_parent_form(self, obj):
        """Get parent form without deep nesting."""
        if obj.parent_form:
            return {
                'id': obj.parent_form.id,
                'title': obj.parent_form.title,
                'description': obj.parent_form.description,
                'is_active': obj.parent_form.is_active,
            }
        return None

    def get_submissions_count(self, obj):
        """Get the number of submissions for this form."""
        if hasattr(obj, '_submissions_count'):
            return obj._submissions_count
        return obj.submissions.count()


class TicketFormSerializer(serializers.ModelSerializer):
    """Serializer for TicketForm model."""
    created_by = UserMinimalSerializer(read_only=True)
    item_lists = ItemListMinimalSerializer(many=True, read_only=True)
    item_list_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        allow_empty=True,
        help_text='List of ItemList IDs to attach to this form'
    )
    parent_form = TicketFormMinimalSerializer(read_only=True)
    parent_form_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    child_forms = TicketFormMinimalSerializer(many=True, read_only=True)
    child_forms_count = serializers.SerializerMethodField()
    submissions_count = serializers.SerializerMethodField()

    class Meta:
        model = TicketForm
        fields = [
            'id', 'title', 'description', 'parent_form', 'parent_form_id',
            'child_forms', 'child_forms_count', 'item_lists', 'item_list_ids',
            'form_config', 'custom_fields', 'is_default', 'is_active',
            'created_at', 'updated_at', 'created_by', 'submissions_count'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']

    def get_child_forms_count(self, obj):
        """Get the number of child forms."""
        if hasattr(obj, '_child_forms_count'):
            return obj._child_forms_count
        return obj.child_forms.count()

    def get_submissions_count(self, obj):
        """Get the number of submissions for this form."""
        if hasattr(obj, '_submissions_count'):
            return obj._submissions_count
        return obj.submissions.count()

    def create(self, validated_data):
        item_list_ids = validated_data.pop('item_list_ids', [])
        parent_form_id = validated_data.pop('parent_form_id', None)
        validated_data['created_by'] = self.context['request'].user

        # Set parent_form if provided
        if parent_form_id:
            validated_data['parent_form_id'] = parent_form_id

        form = super().create(validated_data)

        if item_list_ids:
            form.item_lists.set(item_list_ids)

        return form

    def update(self, instance, validated_data):
        item_list_ids = validated_data.pop('item_list_ids', None)
        parent_form_id = validated_data.pop('parent_form_id', None)

        # Handle parent_form field
        if parent_form_id is not None:
            if parent_form_id:
                validated_data['parent_form_id'] = parent_form_id
            else:
                validated_data['parent_form'] = None

        form = super().update(instance, validated_data)

        if item_list_ids is not None:
            form.item_lists.set(item_list_ids)

        return form


class TicketFormSubmissionSerializer(serializers.ModelSerializer):
    """Serializer for TicketFormSubmission model."""
    submitted_by = UserMinimalSerializer(read_only=True)
    form = TicketFormMinimalSerializer(read_only=True)
    form_id = serializers.IntegerField(write_only=True)
    selected_items = ListItemMinimalSerializer(many=True, read_only=True)
    selected_item_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        allow_empty=True,
        help_text='List of ListItem IDs selected in this submission'
    )
    ticket_data = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = TicketFormSubmission
        fields = [
            'id', 'ticket', 'form', 'form_id', 'selected_items',
            'selected_item_ids', 'form_data', 'submitted_at', 'submitted_by',
            'ticket_data'
        ]
        read_only_fields = ['id', 'submitted_at', 'submitted_by']

    def get_ticket_data(self, obj):
        """Get minimal ticket data."""
        from .models import Ticket
        if obj.ticket:
            return {
                'id': obj.ticket.id,
                'title': obj.ticket.title,
                'status': obj.ticket.status
            }
        return None

    def create(self, validated_data):
        selected_item_ids = validated_data.pop('selected_item_ids', [])
        form_id = validated_data.pop('form_id')

        validated_data['submitted_by'] = self.context['request'].user
        validated_data['form_id'] = form_id

        submission = super().create(validated_data)

        if selected_item_ids:
            submission.selected_items.set(selected_item_ids)

        return submission

    def update(self, instance, validated_data):
        selected_item_ids = validated_data.pop('selected_item_ids', None)
        form_id = validated_data.pop('form_id', None)

        if form_id:
            validated_data['form_id'] = form_id

        submission = super().update(instance, validated_data)

        if selected_item_ids is not None:
            submission.selected_items.set(selected_item_ids)

        return submission


class TicketAttachmentSerializer(serializers.ModelSerializer):
    """Serializer for ticket file attachments."""
    uploaded_by = UserMinimalSerializer(read_only=True)
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = TicketAttachment
        fields = ['id', 'ticket', 'file', 'file_url', 'filename', 'file_size', 'content_type', 'uploaded_by', 'uploaded_at']
        read_only_fields = ['id', 'file_url', 'filename', 'file_size', 'content_type', 'uploaded_by', 'uploaded_at']

    def get_file_url(self, obj):
        """Get the full URL of the uploaded file."""
        if obj.file:
            return obj.file.url
        return None

    def create(self, validated_data):
        """Handle file upload and metadata extraction."""
        file = validated_data.get('file')
        if file:
            validated_data['filename'] = file.name
            validated_data['file_size'] = file.size
            validated_data['content_type'] = file.content_type or ''
        validated_data['uploaded_by'] = self.context['request'].user
        return super().create(validated_data)


class TicketHistorySerializer(serializers.ModelSerializer):
    """Serializer for ticket history entries."""
    user = UserMinimalSerializer(read_only=True)

    class Meta:
        model = TicketHistory
        fields = [
            'id', 'ticket', 'action', 'field_name', 'old_value',
            'new_value', 'description', 'user', 'created_at'
        ]
        read_only_fields = ['id', 'ticket', 'action', 'field_name', 'old_value',
                           'new_value', 'description', 'user', 'created_at']


class BoardTelegramConnectionSerializer(serializers.ModelSerializer):
    """Serializer for BoardTelegramConnection model."""
    bot_token_raw = serializers.CharField(write_only=True, required=False)
    has_token = serializers.SerializerMethodField()
    created_by = UserMinimalSerializer(read_only=True)

    class Meta:
        model = BoardTelegramConnection
        fields = [
            'id', 'board', 'chat_id', 'is_active',
            'bot_token_raw', 'has_token',
            'created_by', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'board', 'created_by', 'created_at', 'updated_at']

    def get_has_token(self, obj):
        return bool(obj.bot_token)

    def create(self, validated_data):
        raw_token = validated_data.pop('bot_token_raw', None)
        if not raw_token:
            raise serializers.ValidationError({'bot_token_raw': 'Bot token is required.'})
        instance = BoardTelegramConnection(**validated_data)
        instance.set_bot_token(raw_token)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        raw_token = validated_data.pop('bot_token_raw', None)
        if raw_token:
            instance.set_bot_token(raw_token)
        instance.chat_id = validated_data.get('chat_id', instance.chat_id)
        instance.is_active = validated_data.get('is_active', instance.is_active)
        instance.save()
        return instance
