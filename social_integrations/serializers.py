from rest_framework import serializers
from .models import (
    FacebookPageConnection, FacebookMessage,
    InstagramAccountConnection, InstagramMessage,
    WhatsAppBusinessAccount, WhatsAppMessage, WhatsAppMessageTemplate,
    WhatsAppContact, SocialIntegrationSettings, ConversationAutoReply,
    ChatAssignment, ChatRating,
    EmailConnection, EmailMessage, EmailDraft, EmailConnectionUserAssignment,
    TikTokShopAccount, TikTokMessage,
    EmailSignature, QuickReply,
    SocialClient, SocialClientCustomField, SocialClientCustomFieldValue, SocialAccount,
    AutoPostSettings, AutoPostContent,
)


class FacebookPageConnectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = FacebookPageConnection
        fields = ['id', 'page_id', 'page_name', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


def _quoted_reply_text(obj):
    """Text (or a media label) of the message a reply is quoting.

    Returned directly by the message serializers so the frontend can render
    the reply preview without having to locate the quoted message in whatever
    page of history happens to be loaded.
    """
    r = getattr(obj, 'reply_to', None)
    if not r:
        return None
    if getattr(r, 'message_text', ''):
        return r.message_text
    at = (getattr(r, 'attachment_type', '') or getattr(r, 'message_type', '') or '').lower()
    labels = {
        'image': '📷 Photo', 'sticker': '📷 Sticker', 'video': '🎬 Video',
        'audio': '🎵 Audio', 'file': '📎 File', 'document': '📄 Document',
    }
    return labels.get(at, '📎 Attachment') if at else None


def _quoted_reply_sender(obj):
    """Display name of the quoted message's sender ('You' for our own)."""
    r = getattr(obj, 'reply_to', None)
    if not r:
        return None
    if getattr(r, 'is_from_page', False) or getattr(r, 'is_from_business', False):
        return 'You'
    return getattr(r, 'sender_name', None) or None


class FacebookMessageSerializer(serializers.ModelSerializer):
    page_id = serializers.CharField(source='page_connection.page_id', read_only=True)
    page_name = serializers.CharField(source='page_connection.page_name', read_only=True)
    reply_to_id = serializers.PrimaryKeyRelatedField(source='reply_to', read_only=True)
    reply_to_text = serializers.SerializerMethodField()
    reply_to_sender_name = serializers.SerializerMethodField()
    sent_by_name = serializers.SerializerMethodField()
    recipient_name = serializers.SerializerMethodField()

    class Meta:
        model = FacebookMessage
        fields = [
            'id', 'message_id', 'sender_id', 'sender_name', 'profile_pic_url', 'message_text',
            'attachment_type', 'attachment_url', 'attachments',
            'timestamp', 'is_from_page', 'is_delivered', 'delivered_at', 'is_read', 'read_at',
            'is_read_by_staff', 'read_by_staff_at',
            'page_id', 'page_name', 'created_at',
            # Reaction fields
            'reaction', 'reaction_emoji', 'reacted_by', 'reacted_at',
            # Reply fields
            'reply_to_message_id', 'reply_to_id', 'reply_to_text', 'reply_to_sender_name',
            # Source tracking fields
            'source', 'is_echo', 'sent_by', 'sent_by_name',
            # Recipient tracking for outgoing messages
            'recipient_name',
        ]
        read_only_fields = ['id', 'is_delivered', 'delivered_at', 'is_read', 'read_at', 'created_at',
                           'reaction', 'reaction_emoji', 'reacted_by', 'reacted_at',
                           'reply_to_message_id', 'reply_to_id', 'reply_to_text', 'reply_to_sender_name',
                           'source', 'is_echo', 'sent_by', 'sent_by_name', 'recipient_name']

    def get_sent_by_name(self, obj):
        if obj.sent_by:
            return f"{obj.sent_by.first_name} {obj.sent_by.last_name}".strip() or obj.sent_by.email
        return None

    def get_reply_to_text(self, obj):
        return _quoted_reply_text(obj)

    def get_reply_to_sender_name(self, obj):
        return _quoted_reply_sender(obj)

    def get_recipient_name(self, obj):
        """Get recipient name for outgoing messages (from page to customer)"""
        if not obj.is_from_page:
            return None

        # For outgoing messages, look up the customer's name
        # First, try to get it from the reply_to message (most accurate)
        if obj.reply_to and obj.reply_to.sender_name:
            return obj.reply_to.sender_name

        # Otherwise, look at the conversation context passed via serializer context
        # The view typically passes the conversation_id (customer's sender_id)
        conversation_id = self.context.get('conversation_id') if self.context else None
        if conversation_id:
            # Use cached recipient names from view context to avoid N+1 queries
            recipient_names_cache = self.context.get('_recipient_names_cache', {})
            cache_key = f"{obj.page_connection_id}:{conversation_id}"
            if cache_key in recipient_names_cache:
                return recipient_names_cache[cache_key]

            incoming_msg = FacebookMessage.objects.filter(
                page_connection=obj.page_connection,
                sender_id=conversation_id,
                is_from_page=False
            ).exclude(sender_name__isnull=True).exclude(sender_name='').values_list('sender_name', flat=True).first()
            if incoming_msg:
                recipient_names_cache[cache_key] = incoming_msg
                return incoming_msg

        # Fallback: use cached lookup per page_connection to avoid N+1 queries
        recipient_names_cache = self.context.get('_recipient_names_cache', {})
        cache_key = f"{obj.page_connection_id}:_fallback"
        if cache_key in recipient_names_cache:
            return recipient_names_cache[cache_key]

        incoming_msg = FacebookMessage.objects.filter(
            page_connection=obj.page_connection,
            is_from_page=False
        ).exclude(sender_name__isnull=True).exclude(sender_name='').values_list('sender_name', flat=True).first()
        if incoming_msg:
            recipient_names_cache[cache_key] = incoming_msg
            return incoming_msg
        recipient_names_cache[cache_key] = None
        return None


class FacebookSendMessageSerializer(serializers.Serializer):
    recipient_id = serializers.CharField(max_length=255, help_text="Facebook user ID to send message to")
    message = serializers.CharField(required=False, allow_blank=True, default='', help_text="Message text to send (optional when sending media)")
    page_id = serializers.CharField(max_length=255, help_text="Facebook page ID to send from")
    reply_to_message_id = serializers.CharField(max_length=255, required=False, allow_blank=True, help_text="Message ID to reply to (optional)")


class InstagramAccountConnectionSerializer(serializers.ModelSerializer):
    facebook_page_name = serializers.CharField(source='facebook_page.page_name', read_only=True)

    class Meta:
        model = InstagramAccountConnection
        fields = [
            'id', 'instagram_account_id', 'username', 'profile_picture_url',
            'is_active', 'facebook_page_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class InstagramMessageSerializer(serializers.ModelSerializer):
    account_id = serializers.CharField(source='account_connection.instagram_account_id', read_only=True)
    account_username = serializers.CharField(source='account_connection.username', read_only=True)
    sent_by_name = serializers.SerializerMethodField()
    recipient_name = serializers.SerializerMethodField()
    reply_to_id = serializers.PrimaryKeyRelatedField(source='reply_to', read_only=True)
    reply_to_text = serializers.SerializerMethodField()
    reply_to_sender_name = serializers.SerializerMethodField()

    class Meta:
        model = InstagramMessage
        fields = [
            'id', 'message_id', 'sender_id', 'sender_name', 'sender_username', 'sender_profile_pic',
            'message_text', 'attachment_type', 'attachment_url', 'attachments',
            'timestamp', 'is_from_business', 'is_delivered', 'delivered_at', 'is_read', 'read_at',
            'is_read_by_staff', 'read_by_staff_at',
            'account_id', 'account_username', 'created_at',
            # Reply fields
            'reply_to_message_id', 'reply_to_id', 'reply_to_text', 'reply_to_sender_name',
            # Source tracking fields
            'source', 'is_echo', 'sent_by', 'sent_by_name',
            # Recipient tracking for outgoing messages
            'recipient_name',
        ]
        read_only_fields = ['id', 'is_delivered', 'delivered_at', 'is_read', 'read_at', 'created_at',
                           'reply_to_message_id', 'reply_to_id', 'reply_to_text', 'reply_to_sender_name',
                           'source', 'is_echo', 'sent_by', 'sent_by_name', 'recipient_name']

    def get_sent_by_name(self, obj):
        if obj.sent_by:
            return f"{obj.sent_by.first_name} {obj.sent_by.last_name}".strip() or obj.sent_by.email
        return None

    def get_reply_to_text(self, obj):
        return _quoted_reply_text(obj)

    def get_reply_to_sender_name(self, obj):
        return _quoted_reply_sender(obj)

    def get_recipient_name(self, obj):
        """Get recipient name for outgoing messages (from business to customer)"""
        if not obj.is_from_business:
            return None

        # Look at the conversation context passed via serializer context
        conversation_id = self.context.get('conversation_id') if self.context else None
        if conversation_id:
            # Use cached recipient names from view context to avoid N+1 queries
            recipient_names_cache = self.context.get('_ig_recipient_names_cache', {})
            cache_key = f"{obj.account_connection_id}:{conversation_id}"
            if cache_key in recipient_names_cache:
                return recipient_names_cache[cache_key]

            incoming_msg = InstagramMessage.objects.filter(
                account_connection=obj.account_connection,
                sender_id=conversation_id,
                is_from_business=False
            ).exclude(sender_name__isnull=True).exclude(sender_name='').values_list('sender_name', flat=True).first()
            if incoming_msg:
                recipient_names_cache[cache_key] = incoming_msg
                return incoming_msg

        # Fallback: use cached lookup per account_connection to avoid N+1 queries
        recipient_names_cache = self.context.get('_ig_recipient_names_cache', {})
        cache_key = f"{obj.account_connection_id}:_fallback"
        if cache_key in recipient_names_cache:
            return recipient_names_cache[cache_key]

        incoming_msg = InstagramMessage.objects.filter(
            account_connection=obj.account_connection,
            is_from_business=False
        ).exclude(sender_name__isnull=True).exclude(sender_name='').values_list('sender_name', flat=True).first()
        if incoming_msg:
            recipient_names_cache[cache_key] = incoming_msg
            return incoming_msg
        recipient_names_cache[cache_key] = None
        return None


class InstagramSendMessageSerializer(serializers.Serializer):
    recipient_id = serializers.CharField(max_length=255, help_text="Instagram user ID to send message to")
    message = serializers.CharField(required=False, allow_blank=True, default='', help_text="Message text to send (optional when sending media)")
    instagram_account_id = serializers.CharField(max_length=255, help_text="Instagram account ID to send from")


class WhatsAppBusinessAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = WhatsAppBusinessAccount
        fields = [
            'id', 'waba_id', 'business_name', 'phone_number_id', 'phone_number',
            'display_phone_number', 'quality_rating', 'is_active',
            # Coexistence fields
            'coex_enabled', 'is_on_biz_app', 'platform_type', 'sync_status',
            'onboarded_at', 'contacts_synced_at', 'history_synced_at', 'throughput_limit',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'coex_enabled', 'is_on_biz_app', 'platform_type', 'sync_status',
            'onboarded_at', 'contacts_synced_at', 'history_synced_at', 'throughput_limit',
            'created_at', 'updated_at'
        ]


class WhatsAppMessageSerializer(serializers.ModelSerializer):
    business_name = serializers.CharField(source='business_account.business_name', read_only=True)
    business_phone = serializers.CharField(source='business_account.display_phone_number', read_only=True)
    waba_id = serializers.CharField(source='business_account.waba_id', read_only=True)
    template_name = serializers.CharField(source='template.name', read_only=True, allow_null=True)
    sent_by_name = serializers.SerializerMethodField()
    reply_to_id = serializers.PrimaryKeyRelatedField(source='reply_to', read_only=True)
    reply_to_text = serializers.SerializerMethodField()
    reply_to_sender_name = serializers.SerializerMethodField()

    class Meta:
        model = WhatsAppMessage
        fields = [
            'id', 'message_id', 'from_number', 'to_number', 'contact_name', 'profile_pic_url',
            'message_text', 'message_type', 'media_url', 'media_mime_type', 'attachments', 'timestamp',
            'is_from_business', 'status', 'is_delivered', 'delivered_at', 'is_read', 'read_at',
            'is_read_by_staff', 'read_by_staff_at',
            'error_message', 'business_name', 'business_phone', 'waba_id', 'template', 'template_name',
            'template_parameters',
            # Coexistence fields
            'source', 'is_echo', 'is_edited', 'edited_at', 'original_text', 'is_revoked', 'revoked_at',
            # Author tracking
            'sent_by', 'sent_by_name',
            # Reply fields
            'reply_to_message_id', 'reply_to_id', 'reply_to_text', 'reply_to_sender_name',
            'created_at'
        ]
        read_only_fields = [
            'id', 'status', 'is_delivered', 'delivered_at', 'is_read', 'read_at',
            'source', 'is_echo', 'is_edited', 'edited_at', 'original_text', 'is_revoked', 'revoked_at',
            'sent_by', 'sent_by_name',
            'reply_to_message_id', 'reply_to_id', 'reply_to_text', 'reply_to_sender_name',
            'created_at'
        ]

    def get_sent_by_name(self, obj):
        if obj.sent_by:
            return f"{obj.sent_by.first_name} {obj.sent_by.last_name}".strip() or obj.sent_by.email
        return None

    def get_reply_to_text(self, obj):
        return _quoted_reply_text(obj)

    def get_reply_to_sender_name(self, obj):
        return _quoted_reply_sender(obj)


class WhatsAppSendMessageSerializer(serializers.Serializer):
    to_number = serializers.CharField(max_length=20, help_text="Recipient's phone number (E.164 format, e.g., +1234567890)")
    message = serializers.CharField(required=False, allow_blank=True, default='', help_text="Message text to send (optional when sending media)")
    waba_id = serializers.CharField(max_length=100, help_text="WhatsApp Business Account ID to send from")
    reply_to_message_id = serializers.CharField(max_length=255, required=False, allow_blank=True, default='', help_text="WhatsApp message ID (wamid) to reply to (optional)")

    def validate_to_number(self, value):
        """Ensure phone number starts with +"""
        if not value.startswith('+'):
            raise serializers.ValidationError("Phone number must be in E.164 format (start with +)")
        return value


class WhatsAppContactSerializer(serializers.ModelSerializer):
    """Serializer for WhatsApp contacts synced from Business App (Coexistence feature)"""
    account_phone = serializers.CharField(source='account.display_phone_number', read_only=True)
    account_name = serializers.CharField(source='account.business_name', read_only=True)

    class Meta:
        model = WhatsAppContact
        fields = [
            'id', 'account', 'wa_id', 'profile_name', 'is_business', 'contact_type',
            'synced_at', 'last_message_at', 'account_phone', 'account_name'
        ]
        read_only_fields = ['id', 'synced_at', 'account_phone', 'account_name']


class WhatsAppMessageTemplateSerializer(serializers.ModelSerializer):
    """Serializer for WhatsApp message templates"""
    business_name = serializers.CharField(source='business_account.business_name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True, allow_null=True)

    class Meta:
        model = WhatsAppMessageTemplate
        fields = [
            'id', 'business_account', 'template_id', 'name', 'language', 'status',
            'category', 'components', 'created_by', 'created_by_name', 'business_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'template_id', 'created_at', 'updated_at', 'business_name', 'created_by_name']


class WhatsAppTemplateCreateSerializer(serializers.Serializer):
    """Serializer for creating WhatsApp message templates"""
    waba_id = serializers.CharField(max_length=100, help_text="WhatsApp Business Account ID")
    name = serializers.CharField(
        max_length=512,
        help_text="Template name (lowercase, no spaces, use underscores)"
    )
    language = serializers.CharField(max_length=10, default='en', help_text="Language code (e.g., en, ka, ru)")
    category = serializers.ChoiceField(
        choices=['MARKETING', 'UTILITY', 'AUTHENTICATION'],
        default='UTILITY',
        help_text="Template category"
    )
    components = serializers.ListField(
        child=serializers.DictField(),
        help_text="Array of component objects (header, body, footer, buttons)"
    )

    def validate_name(self, value):
        """Validate template name format"""
        import re
        if not re.match(r'^[a-z0-9_]+$', value):
            raise serializers.ValidationError(
                "Template name must be lowercase alphanumeric with underscores only (no spaces)"
            )
        if len(value) < 1 or len(value) > 512:
            raise serializers.ValidationError("Template name must be 1-512 characters")
        return value

    def validate_components(self, value):
        """Validate components structure"""
        if not value:
            raise serializers.ValidationError("At least one component is required")

        # Check that at least one component is BODY
        has_body = any(comp.get('type') == 'BODY' for comp in value)
        if not has_body:
            raise serializers.ValidationError("Template must have at least one BODY component")

        return value


class WhatsAppTemplateSendSerializer(serializers.Serializer):
    """Serializer for sending template-based WhatsApp messages"""
    waba_id = serializers.CharField(max_length=100, help_text="WhatsApp Business Account ID")
    template_id = serializers.IntegerField(help_text="Template ID from database")
    to_number = serializers.CharField(
        max_length=20,
        help_text="Recipient's phone number (E.164 format, e.g., +1234567890)"
    )
    parameters = serializers.DictField(
        child=serializers.CharField(),
        required=False,
        allow_null=True,
        help_text="Template parameters as key-value pairs (e.g., {'name': 'John', 'order_id': '12345'})"
    )

    def validate_to_number(self, value):
        """Ensure phone number starts with +"""
        if not value.startswith('+'):
            raise serializers.ValidationError("Phone number must be in E.164 format (start with +)")
        return value


class SocialIntegrationSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = SocialIntegrationSettings
        fields = [
            'id', 'refresh_interval',
            'chat_assignment_enabled', 'session_management_enabled',
            'hide_assigned_chats', 'collect_customer_rating',
            # Link-based rating settings
            'link_based_rating_enabled',
            'rating_request_message_template_ka',
            'rating_request_message_template_en',
            'post_review_redirect_url',
            'notification_sound_facebook', 'notification_sound_instagram',
            'notification_sound_whatsapp', 'notification_sound_email',
            'notification_sound_widget',
            'notification_sound_team_chat', 'notification_sound_system',
            # Auto-reply settings
            'timezone', 'away_hours_enabled', 'away_hours_schedule',
            'auto_reply_settings',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_refresh_interval(self, value):
        """Ensure refresh interval is within acceptable range"""
        if value < 1000:
            raise serializers.ValidationError("Refresh interval must be at least 1000ms (1 second)")
        if value > 60000:
            raise serializers.ValidationError("Refresh interval must be at most 60000ms (60 seconds)")
        return value

    def validate_timezone(self, value):
        """Ensure timezone is valid"""
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, KeyError):
                raise serializers.ValidationError(f"Invalid timezone: {value}")
        except ImportError:
            import pytz
            try:
                pytz.timezone(value)
            except pytz.exceptions.UnknownTimeZoneError:
                raise serializers.ValidationError(f"Invalid timezone: {value}")
        return value

    def validate_away_hours_schedule(self, value):
        """Validate away hours schedule format"""
        valid_days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        if not isinstance(value, dict):
            raise serializers.ValidationError("Away hours schedule must be a dictionary")

        for day, hours in value.items():
            if day.lower() not in valid_days:
                raise serializers.ValidationError(f"Invalid day: {day}")
            if not isinstance(hours, list):
                raise serializers.ValidationError(f"Hours for {day} must be a list")
            for hour in hours:
                if not isinstance(hour, int) or hour < 0 or hour > 23:
                    raise serializers.ValidationError(f"Invalid hour {hour} for {day}. Hours must be integers 0-23.")
        return value

    def validate_auto_reply_settings(self, value):
        """Validate auto-reply settings format"""
        valid_platforms = ['facebook', 'instagram', 'whatsapp']
        if not isinstance(value, dict):
            raise serializers.ValidationError("Auto-reply settings must be a dictionary")

        for platform, settings in value.items():
            if platform not in valid_platforms:
                raise serializers.ValidationError(f"Invalid platform: {platform}")
            if not isinstance(settings, dict):
                raise serializers.ValidationError(f"Settings for {platform} must be a dictionary")

            # Validate structure
            allowed_keys = ['welcome_enabled', 'welcome_message', 'away_enabled', 'away_message']
            for key in settings.keys():
                if key not in allowed_keys:
                    raise serializers.ValidationError(f"Invalid setting key '{key}' for {platform}")
        return value


class ChatAssignmentSerializer(serializers.ModelSerializer):
    """Serializer for chat assignments"""
    assigned_user_name = serializers.SerializerMethodField()
    assigned_user_email = serializers.SerializerMethodField()
    full_conversation_id = serializers.CharField(read_only=True)

    class Meta:
        model = ChatAssignment
        fields = [
            'id', 'platform', 'conversation_id', 'account_id', 'full_conversation_id',
            'assigned_user', 'assigned_user_name', 'assigned_user_email',
            'status', 'session_started_at', 'session_ended_at',
            'assigned_at', 'updated_at'
        ]
        read_only_fields = ['id', 'assigned_at', 'updated_at', 'full_conversation_id']

    def get_assigned_user_name(self, obj):
        if obj.assigned_user is None:
            return None
        return obj.assigned_user.get_full_name() or obj.assigned_user.email

    def get_assigned_user_email(self, obj):
        if obj.assigned_user is None:
            return None
        return obj.assigned_user.email


class ChatAssignmentCreateSerializer(serializers.Serializer):
    """Serializer for assigning a chat"""
    platform = serializers.ChoiceField(choices=['facebook', 'instagram', 'whatsapp', 'email', 'widget'])
    conversation_id = serializers.CharField(max_length=255)
    account_id = serializers.CharField(max_length=255)


class ChatRatingSerializer(serializers.ModelSerializer):
    """Serializer for chat ratings"""
    assignment_id = serializers.IntegerField(source='assignment.id', read_only=True)
    platform = serializers.CharField(source='assignment.platform', read_only=True)
    conversation_id = serializers.CharField(source='assignment.conversation_id', read_only=True)

    class Meta:
        model = ChatRating
        fields = [
            'id', 'assignment', 'assignment_id', 'platform', 'conversation_id',
            'rating', 'comment', 'rating_request_message_id', 'rating_response_message_id',
            'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'assignment_id', 'platform', 'conversation_id']


class PublicRatingInfoSerializer(serializers.Serializer):
    """Serializer for public rating info response"""
    valid = serializers.BooleanField()
    expired = serializers.BooleanField(default=False)
    already_rated = serializers.BooleanField(default=False)
    tenant_name = serializers.CharField(allow_null=True, required=False)


class PublicRatingSubmitSerializer(serializers.Serializer):
    """Serializer for public rating submission"""
    rating = serializers.ChoiceField(choices=[1, 3, 5], help_text="Rating value: 1 (bad), 3 (good), 5 (excellent)")
    comment = serializers.CharField(max_length=1000, required=False, allow_blank=True, help_text="Optional comment")


# =============================================================================
# Email Serializers
# =============================================================================

class EmailConnectionSerializer(serializers.ModelSerializer):
    """Read-only serializer for email connections - NEVER exposes password"""

    class Meta:
        model = EmailConnection
        fields = [
            'id', 'email_address', 'display_name',
            'imap_server', 'imap_port', 'imap_use_ssl',
            'smtp_server', 'smtp_port', 'smtp_use_tls', 'smtp_use_ssl',
            'username', 'is_active',
            'last_sync_at', 'last_sync_error', 'auto_disabled_at',
            'sync_failure_count',
            'sync_folder', 'sync_days_back',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'last_sync_at', 'last_sync_error', 'auto_disabled_at',
            'sync_failure_count', 'created_at', 'updated_at',
        ]


class EmailConnectionCreateSerializer(serializers.Serializer):
    """Serializer for creating/updating email connections with password"""
    email_address = serializers.EmailField(help_text="Email address for this connection")
    display_name = serializers.CharField(max_length=200, required=False, allow_blank=True, help_text="Display name for sent emails")

    # IMAP settings
    imap_server = serializers.CharField(max_length=255, help_text="IMAP server hostname (e.g., imap.gmail.com)")
    imap_port = serializers.IntegerField(default=993, help_text="IMAP port (993 for SSL, 143 for STARTTLS)")
    imap_use_ssl = serializers.BooleanField(default=True, help_text="Use SSL for IMAP connection")

    # SMTP settings
    smtp_server = serializers.CharField(max_length=255, help_text="SMTP server hostname (e.g., smtp.gmail.com)")
    smtp_port = serializers.IntegerField(default=587, help_text="SMTP port (587 for STARTTLS, 465 for SSL)")
    smtp_use_tls = serializers.BooleanField(default=True, help_text="Use TLS/STARTTLS for SMTP")
    smtp_use_ssl = serializers.BooleanField(default=False, help_text="Use SSL for SMTP (alternative to TLS)")

    # Credentials
    username = serializers.CharField(max_length=255, help_text="Login username (usually email address)")
    password = serializers.CharField(max_length=500, write_only=True, help_text="Password or app password")

    # Sync settings
    sync_folder = serializers.CharField(max_length=100, default='INBOX', help_text="IMAP folder to sync")
    sync_days_back = serializers.IntegerField(default=30, min_value=0, max_value=3650, help_text="Number of days of history to sync (0 = all history)")

    def validate(self, data):
        """Validate that SMTP and IMAP settings are consistent"""
        # Can't use both TLS and SSL for SMTP
        if data.get('smtp_use_tls') and data.get('smtp_use_ssl'):
            raise serializers.ValidationError({
                'smtp_use_ssl': "Cannot use both TLS and SSL for SMTP. Choose one."
            })
        return data


class EmailMessageSerializer(serializers.ModelSerializer):
    """Serializer for email messages"""
    connection_id = serializers.IntegerField(source='connection.id', read_only=True)
    connection_email = serializers.EmailField(source='connection.email_address', read_only=True)
    connection_display_name = serializers.CharField(source='connection.display_name', read_only=True)

    class Meta:
        model = EmailMessage
        fields = [
            'id', 'message_id', 'thread_id', 'in_reply_to', 'references',
            'from_email', 'from_name', 'to_emails', 'cc_emails', 'bcc_emails', 'reply_to',
            'subject', 'body_text', 'body_html', 'attachments',
            'timestamp', 'folder', 'uid',
            'is_from_business', 'is_read', 'is_starred', 'is_answered', 'is_draft', 'labels',
            'is_read_by_staff', 'read_by_staff_at',
            'is_deleted', 'deleted_at',
            'connection_id', 'connection_email', 'connection_display_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'message_id', 'thread_id', 'in_reply_to', 'references',
            'timestamp', 'folder', 'uid',
            'is_from_business', 'is_answered',
            'connection_id', 'connection_email', 'connection_display_name',
            'created_at', 'updated_at'
        ]


class EmailSendSerializer(serializers.Serializer):
    """Serializer for sending email messages"""
    to_emails = serializers.ListField(
        child=serializers.EmailField(),
        min_length=1,
        help_text="List of recipient email addresses"
    )
    cc_emails = serializers.ListField(
        child=serializers.EmailField(),
        required=False,
        allow_empty=True,
        help_text="List of CC email addresses"
    )
    bcc_emails = serializers.ListField(
        child=serializers.EmailField(),
        required=False,
        allow_empty=True,
        help_text="List of BCC email addresses"
    )
    subject = serializers.CharField(max_length=1000, required=False, allow_blank=True, help_text="Email subject")
    body_text = serializers.CharField(required=False, allow_blank=True, help_text="Plain text body")
    body_html = serializers.CharField(required=False, allow_blank=True, help_text="HTML body")
    reply_to_message_id = serializers.IntegerField(required=False, allow_null=True, help_text="ID of message being replied to")

    def validate(self, data):
        """Ensure at least one body is provided"""
        if not data.get('body_text') and not data.get('body_html'):
            raise serializers.ValidationError("At least one of body_text or body_html must be provided")
        return data


class EmailDraftSerializer(serializers.ModelSerializer):
    """Serializer for email drafts"""
    created_by_name = serializers.SerializerMethodField()
    reply_to_subject = serializers.CharField(source='reply_to_message.subject', read_only=True, allow_null=True)

    class Meta:
        model = EmailDraft
        fields = [
            'id', 'connection', 'to_emails', 'cc_emails', 'bcc_emails',
            'subject', 'body_text', 'body_html', 'attachments',
            'is_reply_all', 'is_forward',
            'reply_to_message', 'reply_to_subject',
            'created_by', 'created_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_by', 'created_by_name', 'created_at', 'updated_at']

    def get_created_by_name(self, obj):
        return obj.created_by.get_full_name() or obj.created_by.email


class EmailMessageActionSerializer(serializers.Serializer):
    """Serializer for email message actions (star, read, label, move, delete)"""
    message_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="List of message IDs to perform action on"
    )
    thread_id = serializers.CharField(
        max_length=500,
        required=False,
        help_text="Thread ID to perform action on all messages in thread"
    )
    action = serializers.ChoiceField(
        choices=['mark_read', 'mark_unread', 'star', 'unstar', 'label', 'unlabel', 'move', 'delete', 'restore'],
        help_text="Action to perform"
    )
    # Optional parameters based on action
    label = serializers.CharField(max_length=100, required=False, help_text="Label name for label/unlabel actions")
    folder = serializers.CharField(max_length=100, required=False, help_text="Folder name for move action")

    def validate(self, data):
        action = data.get('action')
        # Either message_ids or thread_id must be provided
        if not data.get('message_ids') and not data.get('thread_id'):
            raise serializers.ValidationError("Either message_ids or thread_id must be provided")
        if action in ['label', 'unlabel'] and not data.get('label'):
            raise serializers.ValidationError({'label': f"Label is required for {action} action"})
        if action == 'move' and not data.get('folder'):
            raise serializers.ValidationError({'folder': "Folder is required for move action"})
        return data


class EmailFolderSerializer(serializers.Serializer):
    """Serializer for email folder information"""
    name = serializers.CharField(help_text="Folder name")
    delimiter = serializers.CharField(help_text="Folder hierarchy delimiter")
    flags = serializers.ListField(child=serializers.CharField(), help_text="IMAP folder flags")


class EmailConnectionUserAssignmentSerializer(serializers.ModelSerializer):
    """Serializer for email connection user assignments"""
    user_email = serializers.CharField(source='user.email', read_only=True)
    user_name = serializers.SerializerMethodField()
    assigned_by_email = serializers.CharField(source='assigned_by.email', read_only=True, allow_null=True)
    assigned_by_name = serializers.SerializerMethodField()
    connection_email = serializers.CharField(source='connection.email_address', read_only=True)

    class Meta:
        model = EmailConnectionUserAssignment
        fields = [
            'id', 'connection', 'user', 'user_email', 'user_name',
            'assigned_by', 'assigned_by_email', 'assigned_by_name',
            'connection_email', 'assigned_at'
        ]
        read_only_fields = ['id', 'assigned_by', 'assigned_at']

    def get_user_name(self, obj):
        if obj.user:
            return obj.user.get_full_name() or obj.user.email
        return None

    def get_assigned_by_name(self, obj):
        if obj.assigned_by:
            return obj.assigned_by.get_full_name() or obj.assigned_by.email
        return None


class EmailConnectionUserAssignmentCreateSerializer(serializers.Serializer):
    """Serializer for creating email connection user assignments"""
    connection_id = serializers.IntegerField(help_text="Email connection ID")
    user_ids = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="List of user IDs to assign"
    )


class EmailConnectionWithAssignmentsSerializer(serializers.ModelSerializer):
    """Email connection serializer with assigned users"""
    assigned_users = EmailConnectionUserAssignmentSerializer(
        source='user_assignments', many=True, read_only=True
    )

    class Meta:
        model = EmailConnection
        fields = [
            'id', 'email_address', 'display_name',
            'imap_server', 'imap_port', 'imap_use_ssl',
            'smtp_server', 'smtp_port', 'smtp_use_tls', 'smtp_use_ssl',
            'username', 'is_active',
            'last_sync_at', 'last_sync_error', 'auto_disabled_at',
            'sync_failure_count',
            'sync_folder', 'sync_days_back',
            'assigned_users',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'last_sync_at', 'last_sync_error', 'auto_disabled_at',
            'sync_failure_count', 'created_at', 'updated_at',
        ]


# =============================================================================
# TikTok Serializers
# =============================================================================

class TikTokShopAccountSerializer(serializers.ModelSerializer):
    """Read-only serializer for TikTok Shop accounts - NEVER exposes tokens"""

    class Meta:
        model = TikTokShopAccount
        fields = [
            'id', 'open_id', 'seller_name', 'seller_base_region',
            'shop_id', 'shop_cipher', 'user_type',
            'scope', 'is_active', 'token_expires_at', 'refresh_token_expires_at',
            'deactivated_at', 'deactivation_reason',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'open_id', 'seller_name', 'seller_base_region',
            'shop_id', 'shop_cipher', 'user_type',
            'scope', 'token_expires_at', 'refresh_token_expires_at',
            'deactivated_at', 'deactivation_reason',
            'created_at', 'updated_at'
        ]


class TikTokMessageSerializer(serializers.ModelSerializer):
    """Serializer for TikTok Shop messages"""
    account_seller_name = serializers.CharField(source='shop_account.seller_name', read_only=True)
    account_id = serializers.CharField(source='shop_account.open_id', read_only=True)

    class Meta:
        model = TikTokMessage
        fields = [
            'id', 'message_id', 'conversation_id', 'index',
            'sender_id', 'sender_role', 'sender_im_user_id', 'buyer_user_id',
            'message_type', 'message_text', 'media_url', 'media_mime_type', 'attachments',
            'timestamp', 'is_from_creator',
            'is_delivered', 'delivered_at', 'is_read', 'read_at',
            'is_read_by_staff', 'read_by_staff_at',
            'error_message',
            'account_seller_name', 'account_id',
            'created_at'
        ]
        read_only_fields = [
            'id', 'message_id', 'conversation_id', 'index',
            'sender_id', 'sender_role', 'sender_im_user_id', 'buyer_user_id',
            'message_type', 'media_url', 'media_mime_type', 'attachments',
            'timestamp', 'is_from_creator',
            'is_delivered', 'delivered_at', 'is_read', 'read_at',
            'error_message',
            'account_seller_name', 'account_id',
            'created_at'
        ]


class TikTokSendMessageSerializer(serializers.Serializer):
    """Serializer for sending TikTok Shop messages"""
    conversation_id = serializers.CharField(
        max_length=255,
        help_text="Conversation ID to send message in"
    )
    message = serializers.CharField(help_text="Message text to send")
    message_type = serializers.ChoiceField(
        choices=['TEXT', 'IMAGE', 'VIDEO', 'PRODUCT_CARD', 'ORDER_CARD', 'EMOTICONS'],
        default='TEXT',
        help_text="Type of message to send"
    )
    media_url = serializers.URLField(
        required=False,
        allow_null=True,
        help_text="Media URL for image/video messages"
    )

    def validate(self, data):
        """Validate message content based on type"""
        msg_type = data.get('message_type', 'TEXT')
        if msg_type == 'TEXT' and not data.get('message'):
            raise serializers.ValidationError({'message': "Message text is required for text messages"})
        if msg_type in ('IMAGE', 'VIDEO') and not data.get('media_url'):
            raise serializers.ValidationError({'media_url': f"Media URL is required for {msg_type} messages"})
        return data


class TikTokStatusSerializer(serializers.Serializer):
    """Serializer for TikTok Shop connection status response"""
    connected = serializers.BooleanField()
    account = TikTokShopAccountSerializer(allow_null=True)
    token_expires_at = serializers.DateTimeField(allow_null=True)
    is_token_expired = serializers.BooleanField()


class TikTokOAuthStartSerializer(serializers.Serializer):
    """Serializer for TikTok OAuth start response"""
    oauth_url = serializers.URLField(help_text="TikTok OAuth authorization URL")


# =============================================================================
# Email Signature Serializers
# =============================================================================

class EmailSignatureSerializer(serializers.ModelSerializer):
    """Serializer for email signature settings"""
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = EmailSignature
        fields = [
            'id', 'sender_name', 'signature_html', 'signature_text',
            'is_enabled', 'include_on_reply',
            'created_by', 'created_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_by', 'created_by_name', 'created_at', 'updated_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name() or obj.created_by.email
        return None


# =============================================================================
# Quick Reply Serializers
# =============================================================================

class QuickReplySerializer(serializers.ModelSerializer):
    """Serializer for quick reply templates"""
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = QuickReply
        fields = [
            'id', 'title', 'message', 'platforms', 'shortcut',
            'category', 'use_count', 'position',
            'created_by', 'created_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'use_count', 'created_by', 'created_by_name', 'created_at', 'updated_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name() or obj.created_by.email
        return None

    def validate_platforms(self, value):
        """Validate platforms list"""
        valid_platforms = {'all', 'facebook', 'instagram', 'whatsapp', 'email', 'tiktok'}
        if not value:
            return ['all']  # Default to all platforms
        for platform in value:
            if platform not in valid_platforms:
                raise serializers.ValidationError(
                    f"Invalid platform: {platform}. Valid options are: {', '.join(valid_platforms)}"
                )
        return value


class QuickReplyUseSerializer(serializers.Serializer):
    """Serializer for using a quick reply (replaces variables)"""
    customer_name = serializers.CharField(required=False, allow_blank=True, help_text="Customer display name")
    order_number = serializers.CharField(required=False, allow_blank=True, help_text="Most recent order number")
    agent_name = serializers.CharField(required=False, allow_blank=True, help_text="Current agent name")
    company_name = serializers.CharField(required=False, allow_blank=True, help_text="Company/business name")


class QuickReplyVariablesSerializer(serializers.Serializer):
    """Serializer for available quick reply variables"""
    name = serializers.CharField()
    description = serializers.CharField()


# =============================================================================
# Social Client Serializers
# =============================================================================

class SocialAccountSerializer(serializers.ModelSerializer):
    """Serializer for social accounts linked to a client"""

    class Meta:
        model = SocialAccount
        fields = [
            'id', 'platform', 'platform_id', 'account_connection_id',
            'display_name', 'username', 'profile_pic_url',
            'first_seen_at', 'last_seen_at', 'last_message_at',
            'is_auto_created'
        ]
        read_only_fields = ['id', 'first_seen_at', 'last_seen_at']


class SocialClientCustomFieldSerializer(serializers.ModelSerializer):
    """Serializer for custom field definitions"""
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SocialClientCustomField
        fields = [
            'id', 'name', 'label', 'field_type', 'is_required',
            'position', 'options', 'default_value', 'is_active',
            'created_by', 'created_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_by', 'created_by_name', 'created_at', 'updated_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name() or obj.created_by.email
        return None

    def validate_name(self, value):
        """Validate field name is snake_case"""
        import re
        if not re.match(r'^[a-z][a-z0-9_]*$', value):
            raise serializers.ValidationError(
                "Field name must be snake_case (lowercase letters, numbers, and underscores, starting with a letter)"
            )
        return value


class SocialClientCustomFieldValueSerializer(serializers.ModelSerializer):
    """Serializer for custom field values"""
    field_name = serializers.CharField(source='field.name', read_only=True)
    field_label = serializers.CharField(source='field.label', read_only=True)
    field_type = serializers.CharField(source='field.field_type', read_only=True)

    class Meta:
        model = SocialClientCustomFieldValue
        fields = ['id', 'field', 'field_name', 'field_label', 'field_type', 'value']
        read_only_fields = ['id', 'field_name', 'field_label', 'field_type']


class SocialClientSerializer(serializers.ModelSerializer):
    """Serializer for clients with nested social accounts and custom fields"""
    social_accounts = SocialAccountSerializer(many=True, read_only=True)
    custom_fields = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    booking_stats = serializers.SerializerMethodField()
    full_name = serializers.ReadOnlyField()

    class Meta:
        model = SocialClient
        fields = [
            'id', 'name', 'email', 'phone', 'notes', 'profile_picture',
            'social_accounts', 'custom_fields',
            'created_by', 'created_by_name',
            'created_at', 'updated_at',
            # Booking fields
            'first_name', 'last_name', 'full_name',
            'is_booking_enabled', 'is_verified', 'last_login',
            'booking_stats',
        ]
        read_only_fields = [
            'id', 'created_by', 'created_by_name', 'created_at', 'updated_at',
            'social_accounts', 'booking_stats', 'full_name'
        ]

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name() or obj.created_by.email
        return None

    def get_custom_fields(self, obj):
        """Return custom fields as a dict {field_name: value} (uses prefetched data)"""
        values = obj.custom_field_values.all()  # Uses prefetch cache including __field
        return {v.field.name: v.value for v in values}

    def get_booking_stats(self, obj):
        """Return booking statistics for booking-enabled clients (uses annotated counts)"""
        if not obj.is_booking_enabled:
            return None

        try:
            return {
                'total': getattr(obj, '_booking_count', 0),
                'completed': getattr(obj, '_booking_completed', 0),
                'upcoming': getattr(obj, '_booking_upcoming', 0),
            }
        except Exception:
            return None


# Alias for backward compatibility
ClientSerializer = SocialClientSerializer


def _build_chat_id_for_account(account):
    """Compose the frontend chatId for a given SocialAccount.

    Mirrors the composite-id format the unified conversations endpoint
    emits:

      facebook  → fb_<account_connection_id>_<platform_id>
      instagram → ig_<account_connection_id>_<platform_id>
      whatsapp  → wa_<account_connection_id>_<platform_id stripped of +>
      email     → email_<account_connection_id>_<platform_id>
      widget    → widget_<account_connection_id>_<most recent session_id
                  for this visitor_id> (returns None if no session exists)

    Returns ``None`` when we can't resolve a chatable destination (e.g.
    widget visitor with no session yet).
    """
    platform = account.platform
    conn = account.account_connection_id
    pid = account.platform_id
    if not pid or not conn:
        return None
    if platform == 'facebook':
        return f'fb_{conn}_{pid}'
    if platform == 'instagram':
        return f'ig_{conn}_{pid}'
    if platform == 'whatsapp':
        number = pid.lstrip('+')
        return f'wa_{conn}_{number}'
    if platform == 'email':
        return f'email_{conn}_{pid}'
    if platform == 'widget':
        # platform_id is the stable visitor_id; look up the most recent
        # WidgetSession for this visitor so the agent can pick up a live
        # conversation thread.
        try:
            connection_id = int(conn)
        except (TypeError, ValueError):
            return None
        try:
            from .models import WidgetSession
            session = (
                WidgetSession.objects
                .filter(connection_id=connection_id, visitor_id=pid)
                .order_by('-last_seen_at')
                .values_list('session_id', flat=True)
                .first()
            )
        except Exception:
            session = None
        if not session:
            return None
        return f'widget_{connection_id}_{session}'
    return None


class SocialAccountChatLinkSerializer(serializers.ModelSerializer):
    """Slim SocialAccount projection for the client-list row's "Open chat"
    button — just the fields the frontend needs to navigate to the right
    conversation and label the option when a client has multiple accounts."""
    chat_id = serializers.SerializerMethodField()

    class Meta:
        model = SocialAccount
        fields = ['id', 'platform', 'display_name', 'chat_id']
        read_only_fields = fields

    def get_chat_id(self, obj):
        return _build_chat_id_for_account(obj)


class SocialClientListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for client list (without all nested details)"""
    social_accounts_count = serializers.SerializerMethodField()
    platforms = serializers.SerializerMethodField()
    social_accounts = serializers.SerializerMethodField()
    booking_count = serializers.SerializerMethodField()
    full_name = serializers.ReadOnlyField()

    class Meta:
        model = SocialClient
        fields = [
            'id', 'name', 'email', 'phone', 'profile_picture',
            'social_accounts_count', 'platforms', 'social_accounts',
            'created_at', 'updated_at',
            # Booking fields
            'first_name', 'last_name', 'full_name',
            'is_booking_enabled', 'is_verified',
            'booking_count',
        ]
        read_only_fields = fields

    def get_social_accounts_count(self, obj):
        """Use prefetched data instead of .count() to avoid N+1 queries"""
        return len(obj.social_accounts.all())

    def get_platforms(self, obj):
        """Return list of unique platforms linked to this client (uses prefetch cache)"""
        return list(set(sa.platform for sa in obj.social_accounts.all()))

    def get_social_accounts(self, obj):
        """Slim per-account projection with a pre-built chatId so the
        "Open chat" action on the clients list can navigate directly to
        /social/messages/<chat_id> without a second round-trip."""
        return SocialAccountChatLinkSerializer(
            obj.social_accounts.all(), many=True
        ).data

    def get_booking_count(self, obj):
        """Return total booking count for booking-enabled clients (uses annotated count)"""
        if not obj.is_booking_enabled:
            return None
        try:
            return getattr(obj, '_booking_count', 0)
        except Exception:
            return None


# Alias for backward compatibility
ClientListSerializer = SocialClientListSerializer


class SocialClientCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating clients with custom fields and booking support"""
    custom_fields = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=False,
        help_text="Custom field values as {field_name: value}"
    )
    password = serializers.CharField(
        write_only=True,
        required=False,
        min_length=8,
        help_text="Password for booking authentication (only if is_booking_enabled=True)"
    )

    class Meta:
        model = SocialClient
        fields = [
            'name', 'email', 'phone', 'notes', 'profile_picture', 'custom_fields',
            # Booking fields
            'first_name', 'last_name', 'is_booking_enabled', 'password'
        ]

    def validate(self, attrs):
        # If enabling booking, require email and password
        is_booking_enabled = attrs.get('is_booking_enabled', False)
        if is_booking_enabled:
            if not attrs.get('email'):
                raise serializers.ValidationError({
                    'email': 'Email is required for booking-enabled clients'
                })
            # Password is only required on create when booking is enabled
            if not self.instance and not attrs.get('password'):
                raise serializers.ValidationError({
                    'password': 'Password is required for booking-enabled clients'
                })
        return attrs

    def create(self, validated_data):
        custom_fields_data = validated_data.pop('custom_fields', {})
        password = validated_data.pop('password', None)

        client = SocialClient.objects.create(**validated_data)

        # Set password if provided
        if password:
            client.set_password(password)
            client.generate_verification_token()
            client.save()

        # Create custom field values
        if custom_fields_data:
            self._save_custom_fields(client, custom_fields_data)

        return client

    def update(self, instance, validated_data):
        custom_fields_data = validated_data.pop('custom_fields', None)
        password = validated_data.pop('password', None)

        # Update basic fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        # Update password if provided
        if password:
            instance.set_password(password)

        instance.save()

        # Update custom fields if provided
        if custom_fields_data is not None:
            self._save_custom_fields(instance, custom_fields_data)

        return instance

    def _save_custom_fields(self, client, custom_fields_data):
        """Save custom field values for a client"""
        for field_name, value in custom_fields_data.items():
            try:
                field = SocialClientCustomField.objects.get(name=field_name, is_active=True)
                SocialClientCustomFieldValue.objects.update_or_create(
                    client=client,
                    field=field,
                    defaults={'value': value}
                )
            except SocialClientCustomField.DoesNotExist:
                pass  # Ignore unknown fields


# Alias for backward compatibility
ClientCreateSerializer = SocialClientCreateSerializer


class SocialAccountLinkSerializer(serializers.Serializer):
    """Serializer for linking/unlinking a social account to a client"""
    platform = serializers.ChoiceField(choices=['facebook', 'instagram', 'whatsapp', 'email', 'tiktok', 'widget'])
    platform_id = serializers.CharField(max_length=255, help_text="Platform-specific ID (sender_id, wa_id, etc.)")
    account_connection_id = serializers.CharField(max_length=255, help_text="Account connection ID (page_id, waba_id, etc.)")
    display_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    username = serializers.CharField(max_length=255, required=False, allow_blank=True)
    profile_pic_url = serializers.URLField(max_length=500, required=False, allow_null=True, allow_blank=True)


class SocialClientByAccountSerializer(serializers.Serializer):
    """Serializer for get client by account response"""
    client = SocialClientSerializer(allow_null=True)
    social_account = SocialAccountSerializer(allow_null=True)
    found = serializers.BooleanField()


class LastMessageSerializer(serializers.Serializer):
    """Serializer for last message in a conversation"""
    id = serializers.CharField()
    text = serializers.CharField(allow_null=True, allow_blank=True)
    timestamp = serializers.DateTimeField()
    is_from_business = serializers.BooleanField()
    attachment_type = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    platform_message_id = serializers.CharField()


class UnifiedConversationSerializer(serializers.Serializer):
    """Serializer for unified conversation across all platforms"""
    conversation_id = serializers.CharField()
    platform = serializers.ChoiceField(choices=['facebook', 'instagram', 'whatsapp', 'email', 'widget'])
    sender_id = serializers.CharField()
    sender_name = serializers.CharField(allow_null=True, allow_blank=True)
    profile_pic_url = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    last_message = LastMessageSerializer()
    message_count = serializers.IntegerField()
    unread_count = serializers.IntegerField()
    account_name = serializers.CharField()
    account_id = serializers.CharField()
    # Email-specific field
    subject = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    # Assignment + archive fields (added for /messages-beta bootstrap parity).
    # All nullable: legacy /messages page doesn't read them and is unaffected
    # by their presence; beta consumes them directly so the Assigned tab +
    # History view + composer gating boot correctly without a second round-trip.
    assigned_user_id = serializers.IntegerField(allow_null=True, required=False)
    assigned_user_name = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    assignment_status = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    session_started_at = serializers.DateTimeField(allow_null=True, required=False)
    session_ended_at = serializers.DateTimeField(allow_null=True, required=False)
    is_archived = serializers.BooleanField(required=False, default=False)
    archived_at = serializers.DateTimeField(allow_null=True, required=False)


class PaginatedUnifiedConversationSerializer(serializers.Serializer):
    """Paginated response for unified conversations"""
    count = serializers.IntegerField(help_text="Total number of conversations")
    next = serializers.CharField(allow_null=True, help_text="URL for next page")
    previous = serializers.CharField(allow_null=True, help_text="URL for previous page")
    results = UnifiedConversationSerializer(many=True)


class AutoPostSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = AutoPostSettings
        fields = [
            'id', 'is_enabled', 'company_description', 'posting_time', 'timezone',
            'post_to_facebook', 'post_to_instagram', 'tone', 'content_source',
            'content_language', 'require_approval', 'max_posts_per_day',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class AutoPostContentSerializer(serializers.ModelSerializer):
    featured_product_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    rejected_by_name = serializers.SerializerMethodField()

    class Meta:
        model = AutoPostContent
        fields = [
            'id', 'status', 'facebook_text', 'instagram_text', 'image_url',
            'featured_product', 'featured_product_name',
            'target_facebook', 'target_instagram', 'scheduled_for',
            'facebook_post_id', 'instagram_media_id', 'published_at',
            'error_message', 'ai_model_used',
            'approved_by', 'approved_by_name', 'approved_at',
            'rejected_by', 'rejected_by_name', 'rejected_at', 'rejection_reason',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'facebook_post_id', 'instagram_media_id', 'published_at',
            'error_message', 'ai_model_used', 'approved_by', 'approved_at',
            'rejected_by', 'rejected_at', 'created_at', 'updated_at',
        ]

    def get_featured_product_name(self, obj):
        if obj.featured_product:
            name = obj.featured_product.name
            if isinstance(name, dict):
                return name.get('en', name.get('ka', str(name)))
            return str(name)
        return None

    def get_approved_by_name(self, obj):
        if obj.approved_by:
            return obj.approved_by.get_full_name() or obj.approved_by.email
        return None

    def get_rejected_by_name(self, obj):
        if obj.rejected_by:
            return obj.rejected_by.get_full_name() or obj.rejected_by.email
        return None