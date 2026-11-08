from django.contrib import admin
from django.utils.html import format_html
from django.shortcuts import redirect
from django.contrib import messages
from django.http import HttpResponse
from django.urls import path
from django.utils.safestring import mark_safe
from django.db.models import Sum, Q
import csv
from decimal import Decimal
from .models import (
    Ticket, Tag, TicketComment, TicketColumn, ChecklistItem, Board, TicketTimeLog, TicketPayment,
    ItemList, ListItem, TicketForm, TicketFormSubmission, TicketAttachment, TicketHistory,
    TicketTimeEntry,
)


@admin.register(Board)
class BoardAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_default', 'columns_count', 'tickets_count', 'payment_summary', 'order_users_count', 'board_users_count', 'board_groups_count', 'created_by', 'created_at')
    list_filter = ('is_default', 'created_at', 'updated_at')
    search_fields = ('name', 'description')
    filter_horizontal = ('order_users', 'board_users', 'board_groups')
    readonly_fields = ('created_at', 'updated_at', 'payment_summary_detailed')
    ordering = ('-is_default', 'name')

    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'description', 'is_default', 'color', 'position')
        }),
        ('Payment Summary', {
            'fields': ('payment_summary_detailed',),
            'classes': ('collapse',)
        }),
        ('Access Control', {
            'fields': ('order_users', 'board_users', 'board_groups'),
            'description': 'Control which users and groups can access this board. Order Users can create orders, Board Users can view/access the board, and Board Groups provide group-based access.'
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def columns_count(self, obj):
        count = obj.columns.count()
        if count > 0:
            return format_html(
                '<a href="/admin/tickets/ticketcolumn/?board__id__exact={}">{} columns</a>',
                obj.id, count
            )
        return '0 columns'
    columns_count.short_description = 'Columns'
    
    def tickets_count(self, obj):
        count = sum(column.tickets.count() for column in obj.columns.all())
        if count > 0:
            return format_html(
                '<a href="/admin/tickets/ticket/?column__board__id__exact={}">{} tickets</a>',
                obj.id, count
            )
        return '0 tickets'
    tickets_count.short_description = 'Tickets'
    
    def order_users_count(self, obj):
        count = obj.order_users.count()
        if count > 0:
            return f'{count} users'
        return '0 users'
    order_users_count.short_description = 'Order Users'

    def board_users_count(self, obj):
        count = obj.board_users.count()
        if count > 0:
            return f'{count} users'
        return '0 users'
    board_users_count.short_description = 'Board Users'

    def board_groups_count(self, obj):
        count = obj.board_groups.count()
        if count > 0:
            return f'{count} groups'
        return '0 groups'
    board_groups_count.short_description = 'Board Groups'

    def payment_summary(self, obj):
        """Display payment summary for the board."""
        summary = obj.get_payment_summary()
        total_price = summary.get('total_price') or 0
        total_paid = summary.get('total_paid') or 0
        remaining = summary.get('remaining_balance') or 0
        overdue = summary.get('overdue_tickets') or 0

        if total_price > 0:
            payment_rate = (total_paid / total_price) * 100 if total_price > 0 else 0
            status_color = '#28a745' if payment_rate >= 90 else '#ffc107' if payment_rate >= 50 else '#dc3545'

            result = format_html(
                '<span style="color: {}; font-weight: bold;">{:.1f}% paid</span><br>'
                '<small>{:.2f} / {:.2f}</small>',
                status_color, payment_rate, total_paid, total_price
            )

            if overdue > 0:
                result += format_html('<br><span style="color: #dc3545; font-size: 10px;">{} overdue</span>', overdue)

            return result
        return format_html('<span style="color: #6c757d;">No payments</span>')
    payment_summary.short_description = 'Payment Status'

    def payment_summary_detailed(self, obj):
        """Display detailed payment summary in the form view."""
        summary = obj.get_payment_summary()

        html = '<div style="background: #f8f9fa; padding: 15px; border-radius: 5px; margin: 10px 0;">'
        html += '<h4 style="margin-top: 0;">Payment Summary</h4>'
        html += f'<p><strong>Total Tickets:</strong> {summary.get("total_tickets", 0)}</p>'
        html += f'<p><strong>Orders:</strong> {summary.get("total_orders", 0)}</p>'
        html += f'<p><strong>Total Value:</strong> {summary.get("total_price", 0):.2f}</p>'
        html += f'<p><strong>Total Paid:</strong> {summary.get("total_paid", 0):.2f}</p>'
        html += f'<p><strong>Remaining Balance:</strong> {summary.get("remaining_balance", 0):.2f}</p>'
        html += f'<p><strong>Paid Tickets:</strong> {summary.get("paid_tickets", 0)}</p>'
        html += f'<p><strong>Unpaid Tickets:</strong> {summary.get("unpaid_tickets", 0)}</p>'

        overdue = summary.get("overdue_tickets", 0)
        if overdue > 0:
            html += f'<p style="color: #dc3545;"><strong>Overdue Tickets:</strong> {overdue}</p>'

        html += '</div>'
        return mark_safe(html)
    payment_summary_detailed.short_description = 'Payment Summary'
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('created_by').prefetch_related('columns', 'order_users')
    
    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(TicketColumn)
class TicketColumnAdmin(admin.ModelAdmin):
    """Admin configuration for TicketColumn model."""
    list_display = ('name', 'board', 'color_badge', 'position', 'is_default', 'is_closed_status', 'track_time', 'tickets_count', 'created_at')
    list_filter = ('board', 'is_default', 'is_closed_status', 'track_time', 'created_at')
    search_fields = ('name', 'description', 'board__name')
    ordering = ('board', 'position', 'name')
    readonly_fields = ('created_at', 'updated_at', 'created_by')
    raw_id_fields = ('board',)
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('board', 'name', 'description', 'color', 'position')
        }),
        ('Status Settings', {
            'fields': ('is_default', 'is_closed_status', 'track_time')
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def color_badge(self, obj):
        """Display color as a badge."""
        return format_html(
            '<span style="background-color: {}; color: white; padding: 2px 8px; border-radius: 3px; font-size: 11px;">{}</span>',
            obj.color, obj.color
        )
    color_badge.short_description = 'Color'
    
    def tickets_count(self, obj):
        """Display the number of tickets in this column."""
        count = obj.tickets.count()
        if count > 0:
            return format_html(
                '<a href="/admin/tickets/ticket/?column__id__exact={}">{} tickets</a>',
                obj.id, count
            )
        return '0 tickets'
    tickets_count.short_description = 'Tickets Count'
    
    def save_model(self, request, obj, form, change):
        """Set created_by to current user if creating new column."""
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    """Admin configuration for Tag/Label model."""
    list_display = ('name', 'color_badge', 'description_preview', 'created_by', 'created_at', 'tickets_count')
    search_fields = ('name', 'description')
    list_filter = ('created_at',)
    ordering = ('name',)
    readonly_fields = ('created_at', 'created_by')

    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'color', 'description')
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at'),
            'classes': ('collapse',)
        }),
    )

    def color_badge(self, obj):
        """Display color as a badge."""
        return format_html(
            '<span style="background-color: {}; color: white; padding: 4px 12px; border-radius: 4px; font-size: 11px; font-weight: bold;">{}</span>',
            obj.color, obj.name
        )
    color_badge.short_description = 'Preview'

    def description_preview(self, obj):
        """Display a preview of the description."""
        if obj.description:
            if len(obj.description) > 50:
                return obj.description[:47] + '...'
            return obj.description
        return '-'
    description_preview.short_description = 'Description'

    def tickets_count(self, obj):
        """Display the number of tickets with this tag."""
        count = obj.tickets.count()
        if count > 0:
            return format_html(
                '<a href="/admin/tickets/ticket/?tags__id__exact={}">{} tickets</a>',
                obj.id, count
            )
        return '0 tickets'
    tickets_count.short_description = 'Tickets Count'

    def save_model(self, request, obj, form, change):
        """Set created_by to current user if creating new tag."""
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


class TicketCommentInline(admin.TabularInline):
    """Inline admin for ticket comments."""
    model = TicketComment
    extra = 0
    readonly_fields = ('created_at', 'updated_at')
    fields = ('user', 'comment', 'created_at')

    def get_readonly_fields(self, request, obj=None):
        """Make user field readonly if editing existing comment."""
        if obj and obj.pk:
            return self.readonly_fields + ('user',)
        return self.readonly_fields


class ChecklistItemInline(admin.TabularInline):
    """Inline admin for checklist items."""
    model = ChecklistItem
    extra = 0
    readonly_fields = ('created_at', 'updated_at')
    fields = ('text', 'is_checked', 'position', 'created_by', 'created_at')

    def get_readonly_fields(self, request, obj=None):
        """Make created_by field readonly if editing existing item."""
        if obj and obj.pk:
            return self.readonly_fields + ('created_by',)
        return self.readonly_fields


class TicketPaymentInline(admin.TabularInline):
    """Inline admin for ticket payments."""
    model = TicketPayment
    extra = 0
    readonly_fields = ('processed_at', 'processed_by')
    fields = ('amount', 'currency', 'payment_method', 'payment_reference', 'processed_by', 'processed_at')
    
    def get_readonly_fields(self, request, obj=None):
        """Make processed_by field readonly if editing existing payment."""
        if obj and obj.pk:
            return self.readonly_fields + ('processed_by',)
        return self.readonly_fields


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    """Admin configuration for Ticket model."""
    list_display = (
        'title', 'board_name', 'status_badge', 'priority_badge', 'payment_badge', 'is_order',
        'created_by', 'assigned_to', 'comments_count', 'created_at', 'updated_at'
    )
    list_filter = (
        'column__board', 'column', 'priority', 'is_order', 'is_paid', 'currency',
        'created_at', 'updated_at', 'assigned_to', 'tags'
    )
    search_fields = (
        'title', 'description', 'created_by__email',
        'created_by__first_name', 'created_by__last_name',
        'assigned_to__email', 'assigned_to__first_name',
        'assigned_to__last_name'
    )
    raw_id_fields = ('created_by', 'assigned_to', 'customer')
    filter_horizontal = ('tags',)
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at')
    actions = ['mark_as_paid', 'mark_as_unpaid', 'export_payment_report']

    fieldsets = (
        ('Basic Information', {
            'fields': ('title', 'description', 'rich_description', 'description_format', 'column', 'priority', 'position_in_column', 'is_order', 'due_date')
        }),
        ('Payment Information', {
            'fields': ('price', 'currency', 'is_paid', 'amount_paid', 'payment_due_date'),
            'classes': ('collapse',)
        }),
        ('Assignment', {
            'fields': ('created_by', 'assigned_to', 'customer', 'tags')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    inlines = [TicketCommentInline, ChecklistItemInline, TicketPaymentInline]

    def mark_as_paid(self, request, queryset):
        """Mark selected tickets as paid."""
        updated = 0
        for ticket in queryset:
            if ticket.price and not ticket.is_paid:
                ticket.amount_paid = ticket.price
                ticket.is_paid = True
                ticket.save()

                TicketPayment.objects.create(
                    ticket=ticket,
                    amount=ticket.price - (ticket.payments.aggregate(Sum('amount'))['amount__sum'] or 0),
                    currency=ticket.currency,
                    payment_method='manual',
                    payment_reference=f'Admin bulk action - {request.user.email}',
                    notes='Marked as paid through admin bulk action',
                    processed_by=request.user
                )
                updated += 1

        self.message_user(request, f'{updated} tickets marked as paid.')
    mark_as_paid.short_description = "Mark selected tickets as paid"

    def mark_as_unpaid(self, request, queryset):
        """Mark selected tickets as unpaid."""
        updated = queryset.filter(is_paid=True).update(is_paid=False)
        self.message_user(request, f'{updated} tickets marked as unpaid.')
    mark_as_unpaid.short_description = "Mark selected tickets as unpaid"

    def export_payment_report(self, request, queryset):
        """Export payment report for selected tickets."""
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="payment_report.csv"'

        writer = csv.writer(response)
        writer.writerow([
            'Ticket ID', 'Title', 'Board', 'Status', 'Priority', 'Price', 'Currency',
            'Amount Paid', 'Remaining Balance', 'Payment Status', 'Is Paid',
            'Payment Due Date', 'Is Overdue', 'Created By', 'Created At'
        ])

        for ticket in queryset:
            writer.writerow([
                ticket.id,
                ticket.title,
                ticket.column.board.name if ticket.column and ticket.column.board else 'No Board',
                ticket.column.name if ticket.column else 'No Status',
                ticket.get_priority_display(),
                ticket.price or 0,
                ticket.currency,
                ticket.amount_paid,
                ticket.remaining_balance or 0,
                ticket.payment_status,
                ticket.is_paid,
                ticket.payment_due_date,
                ticket.is_overdue,
                ticket.created_by.email if ticket.created_by else '',
                ticket.created_at.strftime('%Y-%m-%d %H:%M:%S')
            ])

        return response
    export_payment_report.short_description = "Export payment report for selected tickets"

    def board_name(self, obj):
        """Display board name with link."""
        if obj.column and obj.column.board:
            return format_html(
                '<a href="/admin/tickets/board/{}/change/">{}</a>',
                obj.column.board.id, obj.column.board.name
            )
        return format_html('<span style="color: #6c757d; font-style: italic;">No Board</span>')
    board_name.short_description = 'Board'

    def status_badge(self, obj):
        """Display status with color coding."""
        if obj.column:
            return format_html(
                '<span style="background-color: {}; color: white; padding: 2px 6px; border-radius: 3px; font-size: 11px;">{}</span>',
                obj.column.color, obj.column.name
            )
        return format_html(
            '<span style="color: #6c757d; font-style: italic;">No Status</span>'
        )
    status_badge.short_description = 'Status'

    def priority_badge(self, obj):
        """Display priority with color coding."""
        colors = {
            'low': '#28a745',       # Green
            'medium': '#ffc107',    # Yellow
            'high': '#fd7e14',      # Orange
            'critical': '#dc3545'   # Red
        }
        color = colors.get(obj.priority, '#6c757d')
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color, obj.get_priority_display()
        )
    priority_badge.short_description = 'Priority'

    def payment_badge(self, obj):
        """Display payment status with color coding."""
        if not obj.price:
            return format_html('<span style="color: #6c757d;">N/A</span>')
        
        status_colors = {
            'paid': '#28a745',           # Green
            'partially_paid': '#ffc107', # Yellow
            'unpaid': '#dc3545',         # Red
            'overpaid': '#17a2b8',       # Blue
            'no_payment_required': '#6c757d'  # Gray
        }
        
        status = obj.payment_status
        color = status_colors.get(status, '#6c757d')
        
        # Add overdue indication
        badge_text = status.replace('_', ' ').title()
        if obj.is_overdue and status != 'paid':
            badge_text += ' (OVERDUE)'
            color = '#dc3545'  # Force red for overdue
        
        # Show amount info
        if obj.price:
            amount_info = f'{obj.amount_paid}/{obj.price} {obj.currency}'
            return format_html(
                '<span style="color: {}; font-weight: bold;">{}</span><br>'
                '<small style="color: #6c757d;">{}</small>',
                color, badge_text, amount_info
            )
        
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color, badge_text
        )
    payment_badge.short_description = 'Payment Status'

    def comments_count(self, obj):
        """Display the number of comments."""
        count = obj.comments.count()
        if count > 0:
            return format_html(
                '<span title="{} comments">{}</span>',
                count, count
            )
        return '0'
    comments_count.short_description = 'Comments'

    def get_queryset(self, request):
        """Optimize queries by prefetching related objects."""
        return super().get_queryset(request).select_related(
            'created_by', 'assigned_to', 'column', 'column__board'
        ).prefetch_related('tags', 'comments')

    def save_model(self, request, obj, form, change):
        """Set created_by to current user if creating new ticket."""
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(TicketComment)
class TicketCommentAdmin(admin.ModelAdmin):
    """Admin configuration for TicketComment model."""
    list_display = ('ticket_title', 'user', 'comment_preview', 'created_at')
    list_filter = ('created_at', 'ticket__column', 'ticket__priority')
    search_fields = (
        'comment', 'ticket__title', 'user__email',
        'user__first_name', 'user__last_name'
    )
    raw_id_fields = ('ticket', 'user')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        (None, {
            'fields': ('ticket', 'user', 'comment')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def ticket_title(self, obj):
        """Display ticket title with link."""
        return format_html(
            '<a href="/admin/tickets/ticket/{}/change/">{}</a>',
            obj.ticket.id, obj.ticket.title
        )
    ticket_title.short_description = 'Ticket'

    def comment_preview(self, obj):
        """Display a preview of the comment."""
        if len(obj.comment) > 50:
            return obj.comment[:47] + '...'
        return obj.comment
    comment_preview.short_description = 'Comment Preview'

    def get_queryset(self, request):
        """Optimize queries by selecting related objects."""
        return super().get_queryset(request).select_related('ticket', 'user')


@admin.register(ChecklistItem)
class ChecklistItemAdmin(admin.ModelAdmin):
    """Admin configuration for ChecklistItem model."""
    list_display = (
        'text_preview', 'ticket_title', 'is_checked',
        'position', 'created_by', 'created_at'
    )
    list_filter = ('is_checked', 'created_at', 'ticket__column', 'ticket__priority')
    search_fields = (
        'text', 'ticket__title',
        'created_by__email', 'created_by__first_name', 'created_by__last_name'
    )
    raw_id_fields = ('ticket', 'created_by')
    date_hierarchy = 'created_at'
    ordering = ('ticket', 'position', '-created_at')
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        ('Basic Information', {
            'fields': ('text', 'is_checked', 'position')
        }),
        ('Ticket', {
            'fields': ('ticket',)
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def text_preview(self, obj):
        """Display a preview of the checklist item text."""
        if len(obj.text) > 50:
            return obj.text[:47] + '...'
        return obj.text
    text_preview.short_description = 'Text'

    def ticket_title(self, obj):
        """Display the title of the parent ticket with link."""
        if obj.ticket:
            return format_html(
                '<a href="/admin/tickets/ticket/{}/change/">{}</a>',
                obj.ticket.id, obj.ticket.title
            )
        return 'None'
    ticket_title.short_description = 'Ticket'

    def get_queryset(self, request):
        """Optimize queries by selecting related objects."""
        return super().get_queryset(request).select_related(
            'ticket', 'created_by'
        )

    def save_model(self, request, obj, form, change):
        """Set created_by to current user if creating new checklist item."""
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(TicketTimeLog)
class TicketTimeLogAdmin(admin.ModelAdmin):
    """Admin configuration for TicketTimeLog model."""
    list_display = (
        'ticket_title', 'board_name', 'column_name', 'user', 
        'duration_display_formatted', 'entered_at', 'exited_at'
    )
    list_filter = (
        'column__board', 'column', 'entered_at', 'exited_at', 
        'user', 'ticket__priority'
    )
    search_fields = (
        'ticket__title', 'column__name', 'column__board__name',
        'user__email', 'user__first_name', 'user__last_name'
    )
    raw_id_fields = ('ticket', 'user')
    date_hierarchy = 'entered_at'
    ordering = ('-entered_at',)
    readonly_fields = ('duration_display', 'entered_at', 'exited_at', 'duration_seconds')
    
    fieldsets = (
        ('Time Log Information', {
            'fields': ('ticket', 'column', 'user')
        }),
        ('Time Tracking', {
            'fields': ('entered_at', 'exited_at', 'duration_seconds', 'duration_display'),
            'classes': ('collapse',)
        }),
    )
    
    def ticket_title(self, obj):
        """Display ticket title with link."""
        return format_html(
            '<a href="/admin/tickets/ticket/{}/change/">{}</a>',
            obj.ticket.id, obj.ticket.title
        )
    ticket_title.short_description = 'Ticket'
    
    def board_name(self, obj):
        """Display board name."""
        if obj.column and obj.column.board:
            return format_html(
                '<a href="/admin/tickets/board/{}/change/">{}</a>',
                obj.column.board.id, obj.column.board.name
            )
        return 'No Board'
    board_name.short_description = 'Board'
    
    def column_name(self, obj):
        """Display column name with color badge."""
        return format_html(
            '<span style="background-color: {}; color: white; padding: 2px 6px; border-radius: 3px; font-size: 11px;">{}</span>',
            obj.column.color, obj.column.name
        )
    column_name.short_description = 'Column'
    
    def duration_display_formatted(self, obj):
        """Display formatted duration."""
        return obj.duration_display
    duration_display_formatted.short_description = 'Duration'
    
    def get_queryset(self, request):
        """Optimize queries by selecting related objects."""
        return super().get_queryset(request).select_related(
            'ticket', 'column', 'column__board', 'user'
        )


@admin.register(TicketPayment)
class TicketPaymentAdmin(admin.ModelAdmin):
    """Admin configuration for TicketPayment model."""
    list_display = (
        'ticket_title', 'amount_display', 'payment_method', 'payment_status_display',
        'processed_by', 'processed_at'
    )
    list_filter = (
        'payment_method', 'currency', 'processed_at',
        'ticket__column__board', 'ticket__is_paid'
    )
    search_fields = (
        'ticket__title', 'payment_reference', 'notes',
        'processed_by__email', 'processed_by__first_name', 'processed_by__last_name'
    )
    raw_id_fields = ('ticket', 'processed_by')
    date_hierarchy = 'processed_at'
    ordering = ('-processed_at',)
    readonly_fields = ('processed_at',)
    actions = ['export_payment_details', 'mark_tickets_as_paid']
    
    fieldsets = (
        ('Payment Information', {
            'fields': ('ticket', 'amount', 'currency', 'payment_method', 'payment_reference')
        }),
        ('Additional Information', {
            'fields': ('notes',),
            'classes': ('collapse',)
        }),
        ('Processing Information', {
            'fields': ('processed_by', 'processed_at'),
            'classes': ('collapse',)
        }),
    )
    
    def ticket_title(self, obj):
        """Display ticket title with link."""
        return format_html(
            '<a href="/admin/tickets/ticket/{}/change/">{}</a>',
            obj.ticket.id, obj.ticket.title
        )
    ticket_title.short_description = 'Ticket'
    
    def amount_display(self, obj):
        """Display amount with currency."""
        return f'{obj.amount} {obj.currency}'
    amount_display.short_description = 'Amount'
    
    def payment_status_display(self, obj):
        """Display ticket's payment status."""
        if obj.ticket.is_paid:
            return format_html('<span style="color: #28a745; font-weight: bold;">Paid</span>')
        else:
            remaining = obj.ticket.remaining_balance
            if remaining:
                return format_html(
                    '<span style="color: #ffc107; font-weight: bold;">Partial</span><br>'
                    '<small style="color: #6c757d;">Remaining: {} {}</small>',
                    remaining, obj.ticket.currency
                )
            return format_html('<span style="color: #dc3545; font-weight: bold;">Unpaid</span>')
    payment_status_display.short_description = 'Status'
    
    def get_queryset(self, request):
        """Optimize queries by selecting related objects."""
        return super().get_queryset(request).select_related(
            'ticket', 'ticket__column', 'ticket__column__board', 'processed_by'
        )
    
    def export_payment_details(self, request, queryset):
        """Export detailed payment information."""
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="payment_details.csv"'

        writer = csv.writer(response)
        writer.writerow([
            'Payment ID', 'Ticket ID', 'Ticket Title', 'Board', 'Amount', 'Currency',
            'Payment Method', 'Payment Reference', 'Notes', 'Processed By',
            'Processed At', 'Ticket Price', 'Ticket Total Paid', 'Ticket Remaining',
            'Ticket Payment Status'
        ])

        for payment in queryset:
            writer.writerow([
                payment.id,
                payment.ticket.id,
                payment.ticket.title,
                payment.ticket.column.board.name if payment.ticket.column and payment.ticket.column.board else 'No Board',
                payment.amount,
                payment.currency,
                payment.get_payment_method_display(),
                payment.payment_reference,
                payment.notes,
                payment.processed_by.email if payment.processed_by else '',
                payment.processed_at.strftime('%Y-%m-%d %H:%M:%S'),
                payment.ticket.price or 0,
                payment.ticket.amount_paid,
                payment.ticket.remaining_balance or 0,
                payment.ticket.payment_status
            ])

        return response
    export_payment_details.short_description = "Export payment details"

    def mark_tickets_as_paid(self, request, queryset):
        """Mark tickets of selected payments as fully paid."""
        updated = 0
        for payment in queryset:
            ticket = payment.ticket
            if ticket.price and not ticket.is_paid:
                if ticket.amount_paid >= ticket.price:
                    ticket.is_paid = True
                    ticket.save()
                    updated += 1

        self.message_user(request, f'{updated} tickets marked as paid.')
    mark_tickets_as_paid.short_description = "Mark tickets as paid if fully funded"

    def save_model(self, request, obj, form, change):
        """Set processed_by to current user if creating new payment."""
        if not change and not obj.processed_by_id:
            obj.processed_by = request.user
        super().save_model(request, obj, form, change)


class ListItemInline(admin.TabularInline):
    """Inline admin for list items."""
    model = ListItem
    extra = 0
    readonly_fields = ('created_at', 'updated_at', 'created_by')
    fields = ('label', 'custom_id', 'parent', 'parent_list_item', 'position', 'is_active', 'custom_data', 'created_by', 'created_at')
    raw_id_fields = ('parent', 'parent_list_item')
    ordering = ('position',)

    def get_readonly_fields(self, request, obj=None):
        """Make created_by field readonly if editing existing item."""
        if obj and obj.pk:
            return self.readonly_fields + ('created_by',)
        return self.readonly_fields

    def save_model(self, request, obj, form, change):
        """Set created_by to current user if creating new list item."""
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(ItemList)
class ItemListAdmin(admin.ModelAdmin):
    """Admin configuration for ItemList model."""
    list_display = ('title', 'parent_list', 'is_active', 'items_count', 'root_items_count', 'created_by', 'created_at')
    list_filter = ('is_active', 'parent_list', 'created_at', 'updated_at')
    search_fields = ('title', 'description', 'created_by__email', 'created_by__first_name', 'created_by__last_name')
    raw_id_fields = ('created_by', 'parent_list')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        ('Basic Information', {
            'fields': ('title', 'description', 'parent_list', 'is_active')
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    inlines = [ListItemInline]

    def items_count(self, obj):
        """Display the total number of items in this list."""
        count = obj.items.filter(is_active=True).count()
        if count > 0:
            return format_html(
                '<a href="/admin/tickets/listitem/?item_list__id__exact={}">{} items</a>',
                obj.id, count
            )
        return '0 items'
    items_count.short_description = 'Total Items'

    def root_items_count(self, obj):
        """Display the number of root-level items (without parents)."""
        count = obj.items.filter(parent__isnull=True, is_active=True).count()
        return f'{count} root items'
    root_items_count.short_description = 'Root Items'

    def get_queryset(self, request):
        """Optimize queries by selecting related objects."""
        return super().get_queryset(request).select_related('created_by').prefetch_related('items')

    def save_model(self, request, obj, form, change):
        """Set created_by to current user if creating new item list."""
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(ListItem)
class ListItemAdmin(admin.ModelAdmin):
    """Admin configuration for ListItem model."""
    list_display = ('label', 'custom_id', 'item_list', 'parent', 'parent_list_item', 'full_path_display', 'position', 'is_active', 'children_count', 'created_at')
    list_filter = ('is_active', 'item_list', 'created_at', 'updated_at')
    search_fields = ('label', 'custom_id', 'item_list__title')
    raw_id_fields = ('item_list', 'parent', 'parent_list_item')
    date_hierarchy = 'created_at'
    ordering = ('item_list', 'parent', 'position', 'label')
    readonly_fields = ('created_at', 'updated_at', 'full_path_display')

    fieldsets = (
        ('Basic Information', {
            'fields': ('item_list', 'label', 'custom_id', 'parent', 'parent_list_item', 'position', 'is_active')
        }),
        ('Hierarchy', {
            'fields': ('full_path_display',),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def full_path_display(self, obj):
        """Display the full hierarchical path."""
        return obj.get_full_path()
    full_path_display.short_description = 'Full Path'

    def children_count(self, obj):
        """Display the number of direct children."""
        count = obj.children.filter(is_active=True).count()
        if count > 0:
            return format_html(
                '<a href="/admin/tickets/listitem/?parent__id__exact={}">{} children</a>',
                obj.id, count
            )
        return '0 children'
    children_count.short_description = 'Children'

    def get_queryset(self, request):
        """Optimize queries by selecting related objects."""
        return super().get_queryset(request).select_related('item_list', 'parent').prefetch_related('children')

    def save_model(self, request, obj, form, change):
        """Set created_by to current user if creating new list item."""
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(TicketForm)
class TicketFormAdmin(admin.ModelAdmin):
    """Admin configuration for TicketForm model."""
    list_display = ('title', 'is_default', 'is_active', 'item_lists_count', 'submissions_count', 'created_by', 'created_at')
    list_filter = ('is_default', 'is_active', 'created_at', 'updated_at')
    search_fields = ('title', 'description', 'created_by__email', 'created_by__first_name', 'created_by__last_name')
    filter_horizontal = ('item_lists',)
    raw_id_fields = ('created_by',)
    date_hierarchy = 'created_at'
    ordering = ('-is_default', '-created_at')
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        ('Basic Information', {
            'fields': ('title', 'description', 'is_default', 'is_active')
        }),
        ('Form Configuration', {
            'fields': ('form_config',),
            'description': 'JSON configuration for form fields and layout'
        }),
        ('Attached Lists', {
            'fields': ('item_lists',),
            'description': 'Lists that will be available in this form'
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def item_lists_count(self, obj):
        """Display the number of attached item lists."""
        count = obj.item_lists.filter(is_active=True).count()
        if count > 0:
            return f'{count} lists'
        return '0 lists'
    item_lists_count.short_description = 'Item Lists'

    def submissions_count(self, obj):
        """Display the number of form submissions."""
        count = obj.submissions.count()
        if count > 0:
            return format_html(
                '<a href="/admin/tickets/ticketformsubmission/?form__id__exact={}">{} submissions</a>',
                obj.id, count
            )
        return '0 submissions'
    submissions_count.short_description = 'Submissions'

    def get_queryset(self, request):
        """Optimize queries by selecting related objects."""
        return super().get_queryset(request).select_related('created_by').prefetch_related('item_lists', 'submissions')

    def save_model(self, request, obj, form, change):
        """Set created_by to current user if creating new ticket form."""
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(TicketFormSubmission)
class TicketFormSubmissionAdmin(admin.ModelAdmin):
    """Admin configuration for TicketFormSubmission model."""
    list_display = ('ticket_title', 'form', 'selected_items_count', 'submitted_by', 'submitted_at')
    list_filter = ('form', 'submitted_at', 'ticket__column', 'ticket__priority')
    search_fields = ('ticket__title', 'form__title', 'submitted_by__email')
    filter_horizontal = ('selected_items',)
    raw_id_fields = ('ticket', 'form', 'submitted_by')
    date_hierarchy = 'submitted_at'
    ordering = ('-submitted_at',)
    readonly_fields = ('submitted_at', 'submitted_by')

    fieldsets = (
        ('Basic Information', {
            'fields': ('ticket', 'form', 'submitted_by')
        }),
        ('Form Data', {
            'fields': ('form_data',),
            'description': 'JSON data submitted with this form',
            'classes': ('collapse',)
        }),
        ('Selected Items', {
            'fields': ('selected_items',),
            'description': 'Items selected from the attached lists'
        }),
        ('Metadata', {
            'fields': ('submitted_at',),
            'classes': ('collapse',)
        }),
    )

    def ticket_title(self, obj):
        """Display ticket title with link."""
        return format_html(
            '<a href="/admin/tickets/ticket/{}/change/">{}</a>',
            obj.ticket.id, obj.ticket.title
        )
    ticket_title.short_description = 'Ticket'

    def selected_items_count(self, obj):
        """Display the number of selected items."""
        count = obj.selected_items.count()
        if count > 0:
            return f'{count} items'
        return '0 items'
    selected_items_count.short_description = 'Selected Items'

    def get_queryset(self, request):
        """Optimize queries by selecting related objects."""
        return super().get_queryset(request).select_related('ticket', 'form').prefetch_related('selected_items')


@admin.register(TicketAttachment)
class TicketAttachmentAdmin(admin.ModelAdmin):
    """Admin for ticket file attachments."""
    list_display = ('filename', 'ticket_link', 'file_size_display', 'content_type', 'uploaded_by', 'uploaded_at')
    list_filter = ('uploaded_at', 'content_type')
    search_fields = ('filename', 'ticket__title', 'uploaded_by__email')
    readonly_fields = ('uploaded_at', 'file_size', 'content_type', 'uploaded_by')
    ordering = ('-uploaded_at',)

    def ticket_link(self, obj):
        """Display clickable link to ticket."""
        if obj.ticket:
            return format_html(
                '<a href="/admin/tickets/ticket/{}/change/">{}</a>',
                obj.ticket.id, obj.ticket.title
            )
        return '-'
    ticket_link.short_description = 'Ticket'

    def file_size_display(self, obj):
        """Display file size in human-readable format."""
        size = obj.file_size
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f'{size:.1f} {unit}'
            size /= 1024.0
        return f'{size:.1f} TB'
    file_size_display.short_description = 'File Size'

    def save_model(self, request, obj, form, change):
        """Set uploaded_by to current user if creating."""
        if not change:
            obj.uploaded_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(TicketHistory)
class TicketHistoryAdmin(admin.ModelAdmin):
    """Admin for ticket history entries."""
    list_display = ('ticket_link', 'action', 'field_name', 'user', 'created_at')
    list_filter = ('action', 'created_at')
    search_fields = ('ticket__title', 'description', 'user__email')
    readonly_fields = ('ticket', 'action', 'field_name', 'old_value', 'new_value', 'description', 'user', 'created_at')
    ordering = ('-created_at',)

    fieldsets = (
        ('History Entry', {
            'fields': ('ticket', 'action', 'field_name', 'user', 'created_at')
        }),
        ('Changes', {
            'fields': ('old_value', 'new_value', 'description')
        }),
    )

    def ticket_link(self, obj):
        """Display clickable link to ticket."""
        if obj.ticket:
            return format_html(
                '<a href="/admin/tickets/ticket/{}/change/">{}</a>',
                obj.ticket.id, obj.ticket.title
            )
        return '-'
    ticket_link.short_description = 'Ticket'

    def has_add_permission(self, request):
        """Don't allow manual creation of history entries."""
        return False

    def has_change_permission(self, request, obj=None):
        """Don't allow editing history entries."""
        return False


@admin.register(TicketTimeEntry)
class TicketTimeEntryAdmin(admin.ModelAdmin):
    """Read-only admin for personal timer entries."""
    list_display = ('ticket', 'user', 'started_at', 'ended_at', 'duration_seconds')
    list_filter = ('started_at', 'ended_at')
    search_fields = ('ticket__title', 'user__email')
    raw_id_fields = ('ticket', 'user')
    readonly_fields = ('ticket', 'user', 'started_at', 'ended_at', 'duration_seconds', 'created_at')
    ordering = ('-started_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
