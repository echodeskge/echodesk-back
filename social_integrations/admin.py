from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.contrib import messages
from django.db import connection
from tenant_schemas.utils import get_public_schema_name
import requests
from .models import (
    FacebookPageConnection, FacebookMessage,
    InstagramAccountConnection, InstagramMessage,
    WhatsAppBusinessAccount, WhatsAppMessage,
    SocialIntegrationSettings, OrphanedFacebookMessage,
    SocialClient, SocialClientCustomField, SocialClientCustomFieldValue, SocialAccount,
    WidgetSession, WidgetMessage,
)


class TenantAwareAdminMixin:
    """Mixin to restrict admin models to tenant schemas only"""

    def has_module_permission(self, request):
        """Only show this admin in tenant schemas, not public schema"""
        if hasattr(connection, 'schema_name'):
            schema_name = connection.schema_name
        else:
            schema_name = get_public_schema_name()

        # Hide from public schema admin
        if schema_name == get_public_schema_name():
            return False

        return super().has_module_permission(request)


@admin.register(FacebookPageConnection)
class FacebookPageConnectionAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ['page_name', 'page_id', 'is_active', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['page_name', 'page_id']
    fields = ['page_name', 'page_id', 'page_access_token', 'is_active']
    
    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        
        # Add "Connect to Facebook" button
        connect_url = reverse('social_integrations:admin_facebook_oauth_start')
        extra_context['connect_facebook_button'] = format_html(
            '<a class="button" href="{}" style="background-color: #1877f2; color: white; margin-bottom: 10px;">'
            'Connect to Facebook</a>',
            connect_url
        )
        
        return super().changelist_view(request, extra_context=extra_context)
    
    def save_model(self, request, obj, form, change):
        # If page_access_token was manually entered, validate it
        if 'page_access_token' in form.changed_data and obj.page_access_token:
            try:
                # Validate the token by making a request to Facebook API
                response = requests.get(
                    f"https://graph.facebook.com/v23.0/me?access_token={obj.page_access_token}"
                )
                if response.status_code == 200:
                    data = response.json()
                    # If page_id is not set, try to get it from the API response
                    if not obj.page_id and 'id' in data:
                        obj.page_id = data['id']
                    # If page_name is not set, try to get it from the API response
                    if not obj.page_name and 'name' in data:
                        obj.page_name = data['name']
                    messages.success(request, f"Facebook token validated successfully for page: {data.get('name', 'Unknown')}")
                else:
                    messages.error(request, f"Invalid Facebook token. API returned: {response.status_code}")
            except Exception as e:
                messages.error(request, f"Error validating Facebook token: {str(e)}")
        
        super().save_model(request, obj, form, change)


@admin.register(FacebookMessage)
class FacebookMessageAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ['sender_name', 'page_connection', 'message_preview', 'timestamp', 'is_from_page', 'is_delivered', 'is_read']
    list_filter = ['is_from_page', 'is_delivered', 'is_read', 'timestamp', 'page_connection']
    search_fields = ['sender_name', 'message_text', 'sender_id']
    readonly_fields = ['message_id', 'sender_id', 'timestamp', 'delivered_at', 'read_at', 'created_at']

    def message_preview(self, obj):
        """Show first 50 characters of message"""
        return obj.message_text[:50] + '...' if len(obj.message_text) > 50 else obj.message_text
    message_preview.short_description = 'Message Preview'


@admin.register(InstagramAccountConnection)
class InstagramAccountConnectionAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ['username', 'instagram_account_id', 'facebook_page', 'is_active', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['username', 'instagram_account_id']
    fields = ['username', 'instagram_account_id', 'profile_picture_url', 'facebook_page', 'access_token', 'is_active']
    readonly_fields = ['created_at', 'updated_at']

    def get_readonly_fields(self, request, obj=None):
        if obj:  # Editing an existing object
            return self.readonly_fields + ['created_at', 'updated_at']
        return self.readonly_fields


@admin.register(InstagramMessage)
class InstagramMessageAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ['sender_username', 'account_connection', 'message_preview', 'timestamp', 'is_from_business', 'is_delivered', 'is_read']
    list_filter = ['is_from_business', 'is_delivered', 'is_read', 'timestamp', 'account_connection']
    search_fields = ['sender_username', 'message_text', 'sender_id']
    readonly_fields = ['message_id', 'sender_id', 'timestamp', 'delivered_at', 'read_at', 'created_at']

    def message_preview(self, obj):
        """Show first 50 characters of message"""
        return obj.message_text[:50] + '...' if len(obj.message_text) > 50 else obj.message_text
    message_preview.short_description = 'Message Preview'


@admin.register(WhatsAppBusinessAccount)
class WhatsAppBusinessAccountAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ['business_name', 'phone_number', 'display_phone_number', 'quality_rating', 'is_active', 'connected_at']
    list_filter = ['is_active', 'quality_rating', 'created_at']
    search_fields = ['business_name', 'waba_id', 'phone_number', 'display_phone_number']
    readonly_fields = ['waba_id', 'phone_number_id', 'created_at', 'updated_at']

    fieldsets = (
        ('Business Information', {
            'fields': ('business_name', 'waba_id', 'phone_number_id')
        }),
        ('Phone Details', {
            'fields': ('phone_number', 'display_phone_number')
        }),
        ('Status', {
            'fields': ('quality_rating', 'is_active')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def connected_at(self, obj):
        """Display created_at as connected_at"""
        return obj.created_at
    connected_at.short_description = 'Connected At'
    connected_at.admin_order_field = 'created_at'

    def get_readonly_fields(self, request, obj=None):
        """Make access_token readonly to prevent accidental exposure"""
        if obj:  # Editing an existing object
            return list(self.readonly_fields) + ['access_token']
        return self.readonly_fields


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ['contact_name_or_number', 'business_account_name', 'message_preview', 'message_type', 'timestamp', 'is_from_business', 'status']
    list_filter = ['is_from_business', 'status', 'message_type', 'timestamp', 'business_account']
    search_fields = ['contact_name', 'from_number', 'to_number', 'message_text', 'message_id']
    readonly_fields = ['message_id', 'from_number', 'to_number', 'timestamp', 'delivered_at', 'read_at', 'created_at']
    date_hierarchy = 'timestamp'

    fieldsets = (
        ('Message Information', {
            'fields': ('message_id', 'business_account', 'message_type', 'status')
        }),
        ('Contact Details', {
            'fields': ('contact_name', 'from_number', 'to_number')
        }),
        ('Message Content', {
            'fields': ('message_text',)
        }),
        ('Media Information', {
            'fields': ('media_url', 'media_mime_type'),
            'classes': ('collapse',)
        }),
        ('Status Tracking', {
            'fields': ('timestamp', 'delivered_at', 'read_at', 'error_message'),
            'classes': ('collapse',)
        }),
    )

    def contact_name_or_number(self, obj):
        """Display contact name or phone number"""
        if obj.contact_name:
            return f"{obj.contact_name} ({obj.from_number if not obj.is_from_business else obj.to_number})"
        return obj.from_number if not obj.is_from_business else obj.to_number
    contact_name_or_number.short_description = 'Contact'

    def business_account_name(self, obj):
        """Display business account name"""
        return obj.business_account.business_name
    business_account_name.short_description = 'WhatsApp Account'
    business_account_name.admin_order_field = 'business_account__business_name'

    def message_preview(self, obj):
        """Show first 50 characters of message"""
        if obj.message_text:
            return obj.message_text[:50] + '...' if len(obj.message_text) > 50 else obj.message_text
        elif obj.media_url:
            return f"[{obj.message_type.upper()}] Media message"
        return "[No content]"
    message_preview.short_description = 'Message Preview'


class PublicSchemaOnlyAdminMixin:
    """Mixin to restrict admin models to public schema only"""

    def has_module_permission(self, request):
        """Only show this admin in public schema, not tenant schemas"""
        if hasattr(connection, 'schema_name'):
            schema_name = connection.schema_name
        else:
            schema_name = get_public_schema_name()

        # Only show in public schema
        if schema_name != get_public_schema_name():
            return False

        return super().has_module_permission(request)


@admin.register(OrphanedFacebookMessage)
class OrphanedFacebookMessageAdmin(PublicSchemaOnlyAdminMixin, admin.ModelAdmin):
    """
    Admin interface for orphaned Facebook messages.
    These are messages that couldn't be matched to any tenant.
    """
    list_display = [
        'created_at',
        'page_id',
        'sender_name_or_id',
        'message_preview',
        'timestamp',
        'reviewed_status',
        'error_reason'
    ]
    list_filter = [
        'reviewed',
        'error_reason',
        'created_at',
        'timestamp'
    ]
    search_fields = [
        'page_id',
        'sender_id',
        'sender_name',
        'message_text',
        'message_id'
    ]
    readonly_fields = [
        'page_id',
        'sender_id',
        'sender_name',
        'message_id',
        'message_text',
        'timestamp',
        'raw_webhook_data',
        'error_reason',
        'created_at'
    ]
    date_hierarchy = 'created_at'

    fieldsets = (
        ('Message Information', {
            'fields': ('page_id', 'sender_id', 'sender_name', 'message_id')
        }),
        ('Message Content', {
            'fields': ('message_text', 'timestamp')
        }),
        ('Error Details', {
            'fields': ('error_reason', 'raw_webhook_data'),
            'classes': ('collapse',)
        }),
        ('Review Status', {
            'fields': ('reviewed', 'reviewed_at', 'reviewed_by', 'notes')
        }),
        ('Metadata', {
            'fields': ('created_at',),
            'classes': ('collapse',)
        }),
    )

    actions = ['mark_as_reviewed', 'mark_as_unreviewed']

    def sender_name_or_id(self, obj):
        """Display sender name or ID"""
        return obj.sender_name or obj.sender_id
    sender_name_or_id.short_description = 'Sender'
    sender_name_or_id.admin_order_field = 'sender_name'

    def message_preview(self, obj):
        """Show first 50 characters of message"""
        return obj.message_text[:50] + '...' if len(obj.message_text) > 50 else obj.message_text
    message_preview.short_description = 'Message Preview'

    def reviewed_status(self, obj):
        """Display reviewed status with color"""
        if obj.reviewed:
            return format_html(
                '<span style="color: green;">✓ Reviewed</span>'
            )
        return format_html(
            '<span style="color: orange;">⚠ Needs Review</span>'
        )
    reviewed_status.short_description = 'Status'
    reviewed_status.admin_order_field = 'reviewed'

    def mark_as_reviewed(self, request, queryset):
        """Mark selected messages as reviewed"""
        from django.utils import timezone
        updated = queryset.update(
            reviewed=True,
            reviewed_at=timezone.now(),
            reviewed_by=request.user
        )
        self.message_user(request, f"{updated} message(s) marked as reviewed.", messages.SUCCESS)
    mark_as_reviewed.short_description = "Mark selected messages as reviewed"

    def mark_as_unreviewed(self, request, queryset):
        """Mark selected messages as unreviewed"""
        updated = queryset.update(
            reviewed=False,
            reviewed_at=None,
            reviewed_by=None
        )
        self.message_user(request, f"{updated} message(s) marked as unreviewed.", messages.SUCCESS)
    mark_as_unreviewed.short_description = "Mark selected messages as unreviewed"

    def save_model(self, request, obj, form, change):
        """Automatically set reviewed_by and reviewed_at when reviewed is checked"""
        if obj.reviewed and not obj.reviewed_at:
            from django.utils import timezone
            obj.reviewed_at = timezone.now()
            obj.reviewed_by = request.user
        elif not obj.reviewed:
            obj.reviewed_at = None
            obj.reviewed_by = None
        super().save_model(request, obj, form, change)


@admin.register(SocialIntegrationSettings)
class SocialIntegrationSettingsAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ['refresh_interval_display', 'updated_at']
    fields = ['refresh_interval']

    def refresh_interval_display(self, obj):
        """Display refresh interval in a more readable format"""
        seconds = obj.refresh_interval / 1000
        return f"{obj.refresh_interval}ms ({seconds}s)"
    refresh_interval_display.short_description = 'Refresh Interval'

    def has_add_permission(self, request):
        """Only allow one settings object per tenant"""
        try:
            return not SocialIntegrationSettings.objects.exists()
        except Exception:
            # Table doesn't exist yet, allow creation
            return True

    def has_delete_permission(self, request, obj=None):
        """Prevent deletion of settings"""
        return False


class SocialAccountInline(admin.TabularInline):
    """Inline for viewing social accounts linked to a client"""
    model = SocialAccount
    extra = 0
    fields = ['platform', 'platform_id', 'display_name', 'username', 'last_message_at']
    readonly_fields = ['platform', 'platform_id', 'display_name', 'username', 'last_message_at']
    can_delete = True
    show_change_link = True


class SocialClientCustomFieldValueInline(admin.TabularInline):
    """Inline for viewing custom field values for a client"""
    model = SocialClientCustomFieldValue
    extra = 0
    fields = ['field', 'value']
    readonly_fields = ['field']
    can_delete = True


@admin.register(SocialClient)
class SocialClientAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ['name', 'email', 'phone', 'social_accounts_count', 'created_at', 'updated_at']
    list_filter = ['created_at', 'updated_at']
    search_fields = ['name', 'email', 'phone', 'notes']
    readonly_fields = ['created_at', 'updated_at', 'created_by']
    inlines = [SocialAccountInline, SocialClientCustomFieldValueInline]

    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'email', 'phone', 'profile_picture')
        }),
        ('Notes', {
            'fields': ('notes',)
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def social_accounts_count(self, obj):
        """Display count of linked social accounts"""
        return obj.social_accounts.count()
    social_accounts_count.short_description = 'Linked Accounts'

    def save_model(self, request, obj, form, change):
        if not change:  # Creating new object
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(SocialClientCustomField)
class SocialClientCustomFieldAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ['label', 'name', 'field_type', 'is_required', 'position', 'is_active']
    list_filter = ['field_type', 'is_required', 'is_active']
    search_fields = ['name', 'label']
    list_editable = ['position', 'is_active']
    ordering = ['position']

    fieldsets = (
        ('Field Information', {
            'fields': ('name', 'label', 'field_type')
        }),
        ('Configuration', {
            'fields': ('is_required', 'position', 'default_value', 'is_active')
        }),
        ('Options', {
            'fields': ('options',),
            'classes': ('collapse',),
            'description': 'Only for select/multiselect field types'
        }),
    )

    def save_model(self, request, obj, form, change):
        if not change:  # Creating new object
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(SocialAccount)
class SocialAccountAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ['display_name_or_id', 'platform', 'client', 'last_message_at', 'is_auto_created']
    list_filter = ['platform', 'is_auto_created', 'first_seen_at']
    search_fields = ['display_name', 'username', 'platform_id', 'client__name']
    readonly_fields = ['first_seen_at', 'last_seen_at']

    fieldsets = (
        ('Client', {
            'fields': ('client',)
        }),
        ('Platform Details', {
            'fields': ('platform', 'platform_id', 'account_connection_id')
        }),
        ('Display Information', {
            'fields': ('display_name', 'username', 'profile_pic_url')
        }),
        ('Metadata', {
            'fields': ('is_auto_created', 'first_seen_at', 'last_seen_at', 'last_message_at'),
            'classes': ('collapse',)
        }),
    )

    def display_name_or_id(self, obj):
        """Display name or platform ID"""
        return obj.display_name or obj.platform_id
    display_name_or_id.short_description = 'Name/ID'


@admin.register(WidgetSession)
class WidgetSessionAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ('session_id', 'connection_id', 'visitor_name', 'visitor_email', 'started_at', 'last_seen_at')
    list_filter = ('connection_id',)
    search_fields = ('session_id', 'visitor_id', 'visitor_name', 'visitor_email')
    readonly_fields = ('started_at', 'last_seen_at')


@admin.register(WidgetMessage)
class WidgetMessageAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ('message_id', 'session', 'is_from_visitor', 'timestamp', 'is_delivered', 'is_read_by_staff')
    list_filter = ('is_from_visitor', 'is_delivered', 'is_read_by_staff', 'is_deleted')
    search_fields = ('message_id', 'message_text')
    readonly_fields = ('created_at',)

from .models import (  # noqa: E402
    AICompanionChannel, AICompanionRun, AICompanionSettings,
    AIConversationState, ConversationSummary,
)


@admin.register(AICompanionSettings)
class AICompanionSettingsAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ('is_enabled', 'provider', 'model', 'language', 'updated_at')


@admin.register(AICompanionChannel)
class AICompanionChannelAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ('platform', 'account_id', 'enabled', 'updated_at')
    list_filter = ('platform', 'enabled')


@admin.register(AIConversationState)
class AIConversationStateAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ('platform', 'conversation_id', 'account_id', 'mode',
                    'total_ai_replies', 'last_ai_reply_at', 'escalated_at')
    list_filter = ('platform', 'mode')
    search_fields = ('conversation_id', 'account_id')


@admin.register(ConversationSummary)
class ConversationSummaryAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ('platform', 'conversation_id', 'provider', 'model',
                    'requested_by', 'created_at')
    list_filter = ('platform', 'provider')
    search_fields = ('conversation_id', 'summary_text')
    readonly_fields = ('created_at',)


@admin.register(AICompanionRun)
class AICompanionRunAdmin(TenantAwareAdminMixin, admin.ModelAdmin):
    list_display = ('kind', 'platform', 'conversation_id', 'action', 'success',
                    'prompt_tokens', 'completion_tokens', 'started_at')
    list_filter = ('kind', 'success', 'platform')
    readonly_fields = ('started_at',)
