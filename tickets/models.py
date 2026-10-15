from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils import timezone
import json
from amanati_crm.file_utils import sanitized_upload_to


class TicketColumn(models.Model):
    """Column model for organizing tickets in a Kanban-style board."""
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    color = models.CharField(
        max_length=7, 
        default='#6B7280',
        help_text='Hex color code for the column (e.g., #3B82F6)'
    )
    position = models.PositiveIntegerField(
        default=0,
        help_text='Position of the column in the board (lower numbers appear first)'
    )
    is_default = models.BooleanField(
        default=False,
        help_text='Whether this is the default column for new tickets'
    )
    is_closed_status = models.BooleanField(
        default=False,
        help_text='Whether tickets in this column are considered closed/completed'
    )
    track_time = models.BooleanField(
        default=False,
        help_text='Whether to track time spent by tickets in this column'
    )
    board = models.ForeignKey(
        'Board',
        on_delete=models.CASCADE,
        related_name='columns',
        help_text='Board this column belongs to'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='created_columns'
    )

    class Meta:
        ordering = ['position', 'created_at']
        unique_together = [['name', 'board']]  # Ensure unique column names per board

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Ensure only one default column exists per board
        if self.is_default and self.board:
            TicketColumn.objects.filter(
                is_default=True, 
                board=self.board
            ).exclude(id=self.id).update(is_default=False)
        super().save(*args, **kwargs)


class Tag(models.Model):
    """Tag/Label model for categorizing tickets (like Trello labels)."""
    name = models.CharField(max_length=50, unique=True)
    color = models.CharField(
        max_length=7,
        default='#6B7280',
        help_text='Hex color code for the tag (e.g., #3B82F6)'
    )
    description = models.TextField(
        blank=True,
        help_text='Optional description of what this tag represents'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='created_tags',
        null=True,  # Allow null for existing tags
        blank=True
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class TicketAssignment(models.Model):
    """Through model for ticket assignments with additional metadata."""
    ROLE_CHOICES = [
        ('primary', 'Primary Assignee'),
        ('collaborator', 'Collaborator'),
        ('reviewer', 'Reviewer'),
        ('observer', 'Observer'),
    ]
    
    ticket = models.ForeignKey('Ticket', on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='collaborator')
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='ticket_assignments_made'
    )
    
    class Meta:
        unique_together = [['ticket', 'user']]
        ordering = ['-assigned_at']
    
    def __str__(self):
        return f'{self.user.email} assigned to {self.ticket.title} as {self.role}'


class Ticket(models.Model):
    """Ticket model for managing support or internal CRM tickets."""
    PRIORITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ]

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)  # Keep for backward compatibility
    rich_description = models.JSONField(
        null=True, 
        blank=True,
        help_text='Rich text content stored as JSON (Quill Delta format or HTML)'
    )
    description_format = models.CharField(
        max_length=20,
        choices=[
            ('plain', 'Plain Text'),
            ('html', 'HTML'),
            ('delta', 'Quill Delta'),
        ],
        default='plain',
        help_text='Format of the rich_description field'
    )
    # Removed hardcoded status - now using dynamic columns
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='medium', db_index=True)
    
    # Add column field for Kanban board organization
    column = models.ForeignKey(
        TicketColumn,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tickets',
        help_text='Column in which this ticket is placed on the Kanban board'
    )
    
    # Position within the column for drag-and-drop ordering
    position_in_column = models.PositiveIntegerField(
        default=0,
        help_text='Position of the ticket within its column'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        related_name='created_tickets', 
        on_delete=models.CASCADE
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        related_name='assigned_tickets', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        help_text='Primary assignee (for backward compatibility)'
    )
    assigned_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through='TicketAssignment',
        through_fields=('ticket', 'user'),
        related_name='tickets_assigned',
        blank=True,
        help_text='All users assigned to this ticket'
    )
    assigned_groups = models.ManyToManyField(
        'users.TenantGroup',
        related_name='tickets_assigned',
        blank=True,
        help_text='Groups assigned to this ticket'
    )
    assigned_department = models.ForeignKey(
        'users.Department',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tickets_assigned',
        help_text='Department assigned to this ticket'
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name='tickets')
    
    # Field to distinguish orders from regular tickets
    is_order = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Whether this is an order (created by order users) or a regular ticket'
    )
    
    # Pricing and payment fields
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Price for this ticket (for billable work or orders)'
    )
    currency = models.CharField(
        max_length=3,
        default='USD',
        help_text='Currency code (e.g., USD, EUR, GEL)'
    )
    is_paid = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Whether this ticket has been fully paid'
    )
    amount_paid = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text='Amount that has been paid for this ticket'
    )
    payment_due_date = models.DateField(
        null=True,
        blank=True,
        help_text='Due date for payment'
    )
    metadata = models.JSONField(null=True, blank=True, help_text='Additional metadata')

    class Meta:
        ordering = ['column__position', 'position_in_column', '-created_at']
        indexes = [
            models.Index(fields=['column', 'position_in_column'], name='ticket_col_pos_idx'),
            models.Index(fields=['created_by', '-created_at'], name='ticket_creator_idx'),
            models.Index(fields=['assigned_to'], name='ticket_assignee_idx'),
        ]

    def __str__(self):
        return self.title
    
    @property
    def status(self):
        """Get ticket status from its column."""
        return self.column.name.lower().replace(' ', '_') if self.column else 'unassigned'
    
    @property
    def is_closed(self):
        """Check if ticket is in a closed status column."""
        return self.column.is_closed_status if self.column else False
    
    @property
    def remaining_balance(self):
        """Calculate remaining balance to be paid."""
        if self.price is None:
            return None
        return self.price - self.amount_paid
    
    @property
    def payment_status(self):
        """Get payment status as a string."""
        if self.price is None or self.price == 0:
            return 'no_payment_required'
        elif self.is_paid:
            return 'paid'
        elif self.amount_paid == 0:
            return 'unpaid'
        elif self.amount_paid < self.price:
            return 'partially_paid'
        else:
            return 'overpaid'
    
    @property
    def is_overdue(self):
        """Check if payment is overdue."""
        if not self.payment_due_date or self.is_paid:
            return False
        from django.utils import timezone
        return timezone.now().date() > self.payment_due_date

    def add_payment(self, amount, user=None):
        """Add a payment to this ticket."""
        if amount <= 0:
            raise ValueError("Payment amount must be positive")
        
        self.amount_paid += amount
        
        # Auto-mark as paid if full amount is reached
        if self.price and self.amount_paid >= self.price:
            self.is_paid = True
        
        self.save()
        
        # Create payment record
        TicketPayment.objects.create(
            ticket=self,
            amount=amount,
            currency=self.currency,
            payment_method='manual',
            processed_by=user
        )
        
        return self.remaining_balance

    def save(self, *args, **kwargs):
        from django.db import transaction

        # Auto-assign to default column if no column is set
        if not self.column_id:
            # Try to get default column from the default board first,
            # then fall back to any default column.
            default_board = Board.objects.filter(is_default=True).first()
            if default_board:
                default_column = TicketColumn.objects.filter(
                    board=default_board, is_default=True
                ).first()
            else:
                default_column = TicketColumn.objects.filter(is_default=True).first()
            if default_column:
                self.column = default_column

        # Auto-assign position in column on create if not set. Positions are a
        # 0-based index, so a new ticket lands after the current last one
        # (n tickets -> index n). Only on create: an update that legitimately
        # moves a ticket to index 0 must not be bounced back to the end.
        if self._state.adding and self.column and not self.position_in_column:
            with transaction.atomic():
                max_position = Ticket.objects.filter(
                    column=self.column
                ).select_for_update().aggregate(
                    models.Max('position_in_column')
                )['position_in_column__max']
                self.position_in_column = 0 if max_position is None else max_position + 1

        # Auto-update payment status
        if self.price and self.amount_paid >= self.price:
            self.is_paid = True
        elif self.price and self.amount_paid < self.price:
            self.is_paid = False

        super().save(*args, **kwargs)


class ChecklistItem(models.Model):
    """Checklist items that can be embedded in ticket descriptions."""
    ticket = models.ForeignKey(
        Ticket,
        related_name='checklist_items',
        on_delete=models.CASCADE
    )

    text = models.TextField()
    is_checked = models.BooleanField(default=False)
    position = models.PositiveIntegerField(
        default=0,
        help_text='Position of checklist item'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )

    class Meta:
        ordering = ['position', 'created_at']

    def __str__(self):
        status = "✓" if self.is_checked else "☐"
        return f'{status} {self.text} ({self.ticket})'

    def save(self, *args, **kwargs):
        # Auto-assign position if not set
        if not self.position:
            max_position = ChecklistItem.objects.filter(ticket=self.ticket).aggregate(
                models.Max('position')
            )['position__max'] or 0
            self.position = max_position + 1
        super().save(*args, **kwargs)


class TicketComment(models.Model):
    """Comment model for ticket replies and updates."""
    ticket = models.ForeignKey(Ticket, related_name='comments', on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'Comment on {self.ticket.title} by {self.user.email}'


class TicketTimeLog(models.Model):
    """Model to track time spent by tickets in columns."""
    ticket = models.ForeignKey(
        Ticket, 
        related_name='time_logs', 
        on_delete=models.CASCADE
    )
    column = models.ForeignKey(
        TicketColumn,
        related_name='time_logs',
        on_delete=models.CASCADE
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name='ticket_time_logs',
        on_delete=models.CASCADE,
        help_text='User who moved the ticket to this column'
    )
    entered_at = models.DateTimeField(
        auto_now_add=True,
        help_text='When the ticket entered this column'
    )
    exited_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the ticket left this column'
    )
    duration_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text='Total time spent in this column (in seconds)'
    )
    
    class Meta:
        ordering = ['-entered_at']
        verbose_name = 'Ticket Time Log'
        verbose_name_plural = 'Ticket Time Logs'
    
    def __str__(self):
        if self.duration_seconds:
            duration_hours = self.duration_seconds // 3600
            duration_minutes = (self.duration_seconds % 3600) // 60
            return f'{self.ticket.title} in {self.column.name}: {duration_hours}h {duration_minutes}m'
        else:
            return f'{self.ticket.title} in {self.column.name}: active'
    
    @property
    def duration_display(self):
        """Return human-readable duration."""
        if not self.duration_seconds:
            if self.exited_at:
                # Calculate duration if exited but duration not set
                from django.utils import timezone
                if self.entered_at:
                    duration = (self.exited_at - self.entered_at).total_seconds()
                    return self._format_duration(int(duration))
            else:
                # Still active in column
                from django.utils import timezone
                if self.entered_at:
                    duration = (timezone.now() - self.entered_at).total_seconds()
                    return f"{self._format_duration(int(duration))} (active)"
            return "Unknown"
        
        return self._format_duration(self.duration_seconds)
    
    def _format_duration(self, seconds):
        """Format seconds into human-readable string."""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            minutes = seconds // 60
            remaining_seconds = seconds % 60
            return f"{minutes}m {remaining_seconds}s"
        else:
            hours = seconds // 3600
            remaining_minutes = (seconds % 3600) // 60
            if remaining_minutes == 0:
                return f"{hours}h"
            return f"{hours}h {remaining_minutes}m"
    
    def calculate_duration(self):
        """Calculate and save duration if both entered_at and exited_at are set."""
        if self.entered_at and self.exited_at and not self.duration_seconds:
            duration = (self.exited_at - self.entered_at).total_seconds()
            self.duration_seconds = int(duration)
            self.save()


class TicketPayment(models.Model):
    """Model to track individual payments for tickets."""
    PAYMENT_METHOD_CHOICES = [
        ('cash', 'Cash'),
        ('card', 'Card'),
        ('bank_transfer', 'Bank Transfer'),
        ('paypal', 'PayPal'),
        ('stripe', 'Stripe'),
        ('manual', 'Manual'),
        ('other', 'Other'),
    ]
    
    ticket = models.ForeignKey(
        Ticket,
        related_name='payments',
        on_delete=models.CASCADE,
        help_text='Ticket this payment is for'
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text='Payment amount'
    )
    currency = models.CharField(
        max_length=3,
        default='USD',
        help_text='Currency code (e.g., USD, EUR, GEL)'
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        default='manual',
        help_text='How the payment was made'
    )
    payment_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text='Reference number or transaction ID'
    )
    notes = models.TextField(
        blank=True,
        help_text='Additional notes about the payment'
    )
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='processed_payments',
        help_text='User who processed this payment'
    )
    processed_at = models.DateTimeField(
        auto_now_add=True,
        help_text='When this payment was processed'
    )
    
    class Meta:
        ordering = ['-processed_at']
        verbose_name = 'Ticket Payment'
        verbose_name_plural = 'Ticket Payments'
    
    def __str__(self):
        return f'{self.amount} {self.currency} for {self.ticket.title}'


class Board(models.Model):
    """Kanban board model to group columns and tickets."""
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    is_default = models.BooleanField(default=False)

    # Users who can create orders on this board
    order_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='order_boards',
        blank=True,
        help_text='Users who can create orders on this board'
    )

    # Groups that can access this board
    board_groups = models.ManyToManyField(
        'users.TenantGroup',
        related_name='accessible_boards',
        blank=True,
        help_text='Groups that can access this board'
    )

    # Users who can access this board
    board_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='accessible_boards',
        blank=True,
        help_text='Users who can access/view this board'
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )

    class Meta:
        ordering = ['name']
        constraints = [
            # Only one default board per tenant
            models.UniqueConstraint(
                fields=['is_default'],
                condition=models.Q(is_default=True),
                name='unique_default_board'
            )
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # If this is set as default, remove default from others
        if self.is_default:
            Board.objects.filter(is_default=True).update(is_default=False)
        super().save(*args, **kwargs)

    def get_payment_summary(self):
        """Get payment summary for all tickets in this board."""
        from django.db.models import Sum, Count, Q

        tickets = Ticket.objects.filter(column__board=self)

        summary = tickets.aggregate(
            total_tickets=Count('id'),
            total_orders=Count('id', filter=Q(is_order=True)),
            total_price=Sum('price'),
            total_paid=Sum('amount_paid'),
            paid_tickets=Count('id', filter=Q(is_paid=True)),
            unpaid_tickets=Count('id', filter=Q(is_paid=False, price__gt=0)),
            overdue_tickets=Count('id', filter=Q(
                is_paid=False,
                payment_due_date__lt=timezone.now().date()
            ))
        )

        summary['remaining_balance'] = (summary['total_price'] or 0) - (summary['total_paid'] or 0)

        return summary

    def get_overdue_payments(self):
        """Get tickets with overdue payments."""
        from django.utils import timezone
        return Ticket.objects.filter(
            column__board=self,
            is_paid=False,
            payment_due_date__lt=timezone.now().date()
        ).order_by('payment_due_date')


class ItemList(models.Model):
    """
    Dynamic list model that tenants can create.
    These lists contain hierarchical items that can be attached to tickets.
    """
    title = models.CharField(
        max_length=255,
        help_text='Title of the list (e.g., "Product Categories", "Service Types")'
    )
    description = models.TextField(
        blank=True,
        help_text='Optional description of what this list is for'
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Whether this list is currently active'
    )
    is_public = models.BooleanField(
        default=False,
        help_text='Whether this list is accessible to ecommerce clients (public)'
    )

    # Parent list relationship - allows creating child lists
    parent_list = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='child_lists',
        help_text='Parent list if this is a child list (e.g., Products list can be parent of Orders list)'
    )

    # Custom fields schema - defines what additional fields items in this list should have
    # Example: [
    #   {"name": "client_name", "label": "Client Name", "type": "string", "required": true},
    #   {"name": "address", "label": "Address", "type": "text", "required": false},
    #   {"name": "id_number", "label": "ID Number", "type": "number", "required": true}
    # ]
    custom_fields_schema = models.JSONField(
        default=list,
        blank=True,
        help_text='Schema for custom fields that items in this list should have. Array of field definitions.'
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='created_item_lists'
    )

    class Meta:
        ordering = ['title', '-created_at']
        verbose_name = 'Item List'
        verbose_name_plural = 'Item Lists'

    def __str__(self):
        return self.title


class ListItem(models.Model):
    """
    Item model with recursive self-referencing for hierarchical structure.
    Each item has a label and id, and can have children.
    Can also link to parent items from other lists.
    """
    item_list = models.ForeignKey(
        ItemList,
        on_delete=models.CASCADE,
        related_name='items',
        help_text='The list this item belongs to'
    )
    label = models.CharField(
        max_length=255,
        help_text='Display label for this item'
    )
    # custom_id is optional - can be used by tenants for their own reference
    custom_id = models.CharField(
        max_length=100,
        blank=True,
        help_text='Optional custom identifier for this item'
    )

    # Recursive relationship for hierarchical structure (within same list)
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children',
        help_text='Parent item for hierarchical structure within the same list'
    )

    # Cross-list parent relationship (from parent list if this list is a child list)
    parent_list_item = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='child_list_items',
        help_text='Parent item from parent list (e.g., if this is an Order item, link to the Product item)'
    )

    # Position for ordering items at the same level
    position = models.PositiveIntegerField(
        default=0,
        help_text='Position of item within its parent or list'
    )

    is_active = models.BooleanField(
        default=True,
        help_text='Whether this item is currently active'
    )

    # Custom field values - stores the actual data for custom fields defined in ItemList
    # Example: {"client_name": "John Doe", "address": "123 Main St", "id_number": 12345}
    custom_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='Custom field values for this item based on the list schema'
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='created_list_items'
    )

    class Meta:
        ordering = ['position', 'created_at']
        verbose_name = 'List Item'
        verbose_name_plural = 'List Items'

    def __str__(self):
        if self.parent:
            return f'{self.parent.label} -> {self.label}'
        return self.label

    def save(self, *args, **kwargs):
        # Auto-assign position if not set
        if not self.position:
            if self.parent:
                max_position = ListItem.objects.filter(
                    item_list=self.item_list,
                    parent=self.parent
                ).aggregate(models.Max('position'))['position__max'] or 0
            else:
                max_position = ListItem.objects.filter(
                    item_list=self.item_list,
                    parent__isnull=True
                ).aggregate(models.Max('position'))['position__max'] or 0
            self.position = max_position + 1
        super().save(*args, **kwargs)

    def get_full_path(self):
        """Get the full hierarchical path of this item."""
        path = [self.label]
        current = self.parent
        while current:
            path.insert(0, current.label)
            current = current.parent
        return ' > '.join(path)

    def get_all_children(self):
        """Return all descendants using a single query (was a per-node recursion)."""
        all_items = list(ListItem.objects.filter(item_list_id=self.item_list_id))
        children_by_parent = {}
        for item in all_items:
            if item.parent_id is not None:
                children_by_parent.setdefault(item.parent_id, []).append(item)

        result = []
        queue = list(children_by_parent.get(self.id, []))
        while queue:
            item = queue.pop(0)
            result.append(item)
            queue.extend(children_by_parent.get(item.id, []))
        return result


class TicketForm(models.Model):
    """
    Custom ticket form that tenants can create.
    Forms can have attached ItemLists for structured data entry.
    Supports recursive child forms for multi-step workflows.
    """
    title = models.CharField(
        max_length=255,
        help_text='Title of the form (e.g., "Product Order Form", "Service Request Form")'
    )
    description = models.TextField(
        blank=True,
        help_text='Description of when to use this form'
    )

    # Parent form relationship - allows creating child forms recursively
    parent_form = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='child_forms',
        help_text='Parent form if this is a child form (e.g., main order form -> engineer resolution form)'
    )

    # Associated lists that should be used in this form
    item_lists = models.ManyToManyField(
        ItemList,
        related_name='ticket_forms',
        blank=True,
        help_text='Lists that should be available in this form'
    )

    # Form configuration stored as JSON
    # This can include field configurations, validation rules, etc.
    form_config = models.JSONField(
        default=dict,
        blank=True,
        help_text='JSON configuration for form fields and behavior'
    )

    # Custom fields schema - defines additional input fields for this form
    # Example: [
    #   {"name": "delivery_address", "label": "Delivery Address", "type": "string", "required": true},
    #   {"name": "notes", "label": "Additional Notes", "type": "text", "required": false},
    #   {"name": "quantity", "label": "Quantity", "type": "number", "required": true},
    #   {"name": "delivery_date", "label": "Delivery Date", "type": "date", "required": true},
    #   {"name": "customer_signature", "label": "Customer Signature", "type": "signature", "required": false}
    # ]
    # Supported types: string, text, number, date, signature (image upload)
    custom_fields = models.JSONField(
        default=list,
        blank=True,
        help_text='Schema for custom input fields in this form. Array of field definitions.'
    )

    is_default = models.BooleanField(
        default=False,
        help_text='Whether this is the default form for ticket creation'
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Whether this form is currently active'
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='created_ticket_forms'
    )

    class Meta:
        ordering = ['title', '-created_at']
        verbose_name = 'Ticket Form'
        verbose_name_plural = 'Ticket Forms'

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        # Ensure only one default form exists
        if self.is_default:
            TicketForm.objects.filter(is_default=True).exclude(id=self.id).update(is_default=False)
        super().save(*args, **kwargs)


class TicketFormSubmission(models.Model):
    """
    Stores the data submitted when a ticket is created using a custom form.
    Links tickets to forms and stores the selected list items.
    """
    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name='form_submissions',
        help_text='The ticket this form submission is for'
    )
    form = models.ForeignKey(
        TicketForm,
        on_delete=models.CASCADE,
        related_name='submissions',
        help_text='The form that was used'
    )

    # Selected items from the lists
    selected_items = models.ManyToManyField(
        ListItem,
        related_name='ticket_submissions',
        blank=True,
        help_text='Items selected from lists when creating this ticket'
    )

    # Store the complete form data as JSON for flexibility
    form_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='Complete form submission data including all field values'
    )

    # Metadata
    submitted_at = models.DateTimeField(auto_now_add=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='form_submissions'
    )

    class Meta:
        ordering = ['-submitted_at']
        verbose_name = 'Ticket Form Submission'
        verbose_name_plural = 'Ticket Form Submissions'

    def __str__(self):
        return f'{self.form.title} - {self.ticket.title}'


class TicketAttachment(models.Model):
    """File attachments for tickets."""
    ticket = models.ForeignKey(
        'Ticket',
        on_delete=models.CASCADE,
        related_name='attachments',
        help_text='Ticket this file is attached to'
    )
    file = models.FileField(
        upload_to=sanitized_upload_to('ticket_attachments'),
        help_text='Uploaded file'
    )
    filename = models.CharField(
        max_length=255,
        help_text='Original filename'
    )
    file_size = models.PositiveIntegerField(
        help_text='File size in bytes'
    )
    content_type = models.CharField(
        max_length=100,
        blank=True,
        help_text='MIME type of the file'
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ticket_attachments',
        help_text='User who uploaded this file'
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']
        verbose_name = 'Ticket Attachment'
        verbose_name_plural = 'Ticket Attachments'

    def __str__(self):
        return f'{self.filename} - {self.ticket.title}'


class TicketHistory(models.Model):
    """Track all changes made to a ticket."""
    ACTION_CHOICES = [
        ('created', 'Created'),
        ('updated', 'Updated'),
        ('status_changed', 'Status Changed'),
        ('assigned', 'Assigned'),
        ('unassigned', 'Unassigned'),
        ('priority_changed', 'Priority Changed'),
        ('comment_added', 'Comment Added'),
        ('tag_added', 'Tag Added'),
        ('tag_removed', 'Tag Removed'),
        ('attachment_added', 'Attachment Added'),
        ('attachment_removed', 'Attachment Removed'),
        ('checklist_updated', 'Checklist Updated'),
        ('subticket_added', 'Sub-ticket Added'),
        ('subticket_removed', 'Sub-ticket Removed'),
        ('form_submitted', 'Form Submitted'),
        ('transferred', 'Transferred to Another Board'),
        ('due_date_changed', 'Due Date Changed'),
    ]

    ticket = models.ForeignKey(
        'Ticket',
        on_delete=models.CASCADE,
        related_name='history',
        help_text='Ticket this history entry belongs to'
    )
    action = models.CharField(
        max_length=50,
        choices=ACTION_CHOICES,
        help_text='Type of action performed'
    )
    field_name = models.CharField(
        max_length=100,
        blank=True,
        help_text='Name of the field that was changed (for updates)'
    )
    old_value = models.TextField(
        blank=True,
        help_text='Previous value (JSON serialized for complex types)'
    )
    new_value = models.TextField(
        blank=True,
        help_text='New value (JSON serialized for complex types)'
    )
    description = models.TextField(
        blank=True,
        help_text='Human-readable description of the change'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='ticket_history',
        help_text='User who performed the action'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Ticket History'
        verbose_name_plural = 'Ticket Histories'
        indexes = [
            models.Index(fields=['ticket', '-created_at']),
        ]

    def __str__(self):
        return f'{self.ticket.title} - {self.action} - {self.created_at}'


class BoardTelegramConnection(models.Model):
    """Stores Telegram bot connection details for a board."""
    board = models.OneToOneField(
        Board,
        on_delete=models.CASCADE,
        related_name='telegram_connection',
        help_text='Board this Telegram connection belongs to'
    )
    bot_token = models.TextField(
        help_text='Encrypted Telegram bot token - DO NOT store plain text'
    )
    chat_id = models.CharField(
        max_length=100,
        help_text='Telegram chat ID to send notifications to'
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='telegram_connections'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Board Telegram Connection'
        verbose_name_plural = 'Board Telegram Connections'

    def __str__(self):
        return f'Telegram connection for {self.board.name}'

    def set_bot_token(self, raw_token):
        """Encrypt and store the bot token using Django's Signer"""
        from django.core.signing import Signer
        signer = Signer()
        self.bot_token = signer.sign(raw_token)

    def get_bot_token(self):
        """Decrypt and return the bot token"""
        from django.core.signing import Signer, BadSignature
        signer = Signer()
        try:
            return signer.unsign(self.bot_token)
        except BadSignature:
            return None
