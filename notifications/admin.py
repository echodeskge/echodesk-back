from django.contrib import admin
from .models import DevicePushToken, PushSubscription, NotificationLog


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    """Admin for push subscriptions."""
    list_display = ('user', 'endpoint_short', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('user__email', 'endpoint')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-created_at',)

    def endpoint_short(self, obj):
        """Show shortened endpoint."""
        return obj.endpoint[:50] + '...' if len(obj.endpoint) > 50 else obj.endpoint
    endpoint_short.short_description = 'Endpoint'


@admin.register(DevicePushToken)
class DevicePushTokenAdmin(admin.ModelAdmin):
    """Admin for Expo device push tokens."""
    list_display = ('user', 'platform', 'device_name', 'app_version', 'is_active', 'last_seen_at')
    list_filter = ('platform', 'is_active', 'last_seen_at')
    search_fields = ('user__email', 'token', 'device_id', 'device_name')
    readonly_fields = ('created_at', 'last_seen_at')
    ordering = ('-last_seen_at',)


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    """Admin for notification logs."""
    list_display = ('user', 'title', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('user__email', 'title', 'body')
    readonly_fields = ('user', 'subscription', 'device_token', 'title', 'body', 'data', 'status', 'error_message', 'created_at')
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        """Don't allow manual creation."""
        return False
