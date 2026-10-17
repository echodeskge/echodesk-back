from rest_framework import serializers
from widget_registry.models import WidgetConnection
from .models import WidgetMessage, WidgetSession


class WidgetConnectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WidgetConnection
        fields = [
            'id', 'tenant_schema', 'widget_token', 'label', 'is_active',
            'allowed_origins', 'brand_color', 'position',
            'welcome_message', 'pre_chat_form', 'offline_message',
            'business_hours_schedule',
            'proactive_enabled', 'proactive_message', 'proactive_delay_seconds',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'tenant_schema', 'widget_token', 'created_at', 'updated_at']


class WidgetSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WidgetSession
        fields = [
            'id', 'connection_id', 'session_id', 'visitor_id',
            'visitor_name', 'visitor_email', 'referrer_url', 'page_url',
            'started_at', 'last_seen_at',
            'ended_at', 'ended_by',
        ]
        read_only_fields = fields


class WidgetMessageSerializer(serializers.ModelSerializer):
    session_id = serializers.CharField(source='session.session_id', read_only=True)

    class Meta:
        model = WidgetMessage
        fields = [
            'id', 'session_id', 'message_id', 'message_text', 'attachments',
            'is_from_visitor', 'sent_by', 'is_delivered', 'delivered_at',
            'is_read_by_visitor', 'is_read_by_staff', 'timestamp', 'created_at',
        ]
        read_only_fields = [
            'id', 'session_id', 'message_id', 'is_delivered', 'delivered_at',
            'is_read_by_visitor', 'is_read_by_staff', 'created_at',
        ]


class WidgetAdminSendMessageSerializer(serializers.Serializer):
    """Body of POST /api/widget/admin/messages/send/ (agent reply)."""
    connection_id = serializers.IntegerField(help_text='Widget connection id (the widget_<conn>_<session> middle part)')
    session_id = serializers.CharField(help_text='Widget session id (NOT the visitor id)')
    message_text = serializers.CharField(required=False, allow_blank=True, max_length=10000)
    attachments = serializers.ListField(
        child=serializers.DictField(), required=False,
        help_text='Already-hosted attachments [{url, name, content_type, size}]',
    )
