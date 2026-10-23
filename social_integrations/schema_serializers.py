"""Request/response shapes for drf-spectacular.

These serializers describe what the function-based social endpoints accept
and return so the generated TypeScript clients (web + mobile) are typed.
Most are documentation only; a few are also used for validation.
"""
from rest_framework import serializers

from .serializers import (
    AutoPostContentSerializer, ChatAssignmentSerializer, AssignmentSettingsFlagsSerializer,
    SocialAccountSerializer, SocialClientSerializer, TikTokShopAccountSerializer,
    WhatsAppMessageTemplateSerializer,
)

INBOX_PLATFORMS = ['facebook', 'instagram', 'whatsapp', 'email', 'widget', 'telegram']


# ── Generic ────────────────────────────────────────────────────────────────

class SocialErrorSerializer(serializers.Serializer):
    error = serializers.CharField()


class SocialErrorWithDetailsSerializer(serializers.Serializer):
    error = serializers.CharField()
    details = serializers.JSONField(required=False)
    code = serializers.CharField(
        required=False,
        help_text="'outside_messaging_window' when Meta's messaging window is closed",
    )
    window = serializers.CharField(required=False, help_text="'closed' with code=outside_messaging_window")
    meta_error = serializers.JSONField(
        required=False, help_text='{code, error_subcode, message} when Meta refused the send',
    )


class SocialMessageResponseSerializer(serializers.Serializer):
    message = serializers.CharField()


# ── Read state / archive / delete ──────────────────────────────────────────

class ConversationKeySerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=INBOX_PLATFORMS)
    conversation_id = serializers.CharField(
        help_text=(
            "Unprefixed conversation key: FB/IG sender id, WhatsApp number, email "
            "thread id, Telegram peer id, widget session id (visitor id / list id "
            "accepted for widget)."
        ),
    )
    account_id = serializers.CharField(
        required=False, allow_blank=True,
        help_text="Optional page / account / WABA / connection id to scope the change to one account.",
    )


class MarkReadResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    messages_marked_read = serializers.IntegerField()


class MarkUnreadResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    messages_marked_unread = serializers.IntegerField()


class PlatformsSerializer(serializers.Serializer):
    platform = serializers.CharField(
        required=False, default='all',
        help_text="'all' (includes EMAIL) or a comma-separated list, e.g. 'facebook,instagram,whatsapp,widget,telegram'.",
    )


class MarkAllReadResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    messages_marked_read = serializers.IntegerField()
    platform = serializers.CharField()


class ConversationRefSerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=INBOX_PLATFORMS)
    conversation_id = serializers.CharField()
    account_id = serializers.CharField()


class ConversationsSerializer(serializers.Serializer):
    conversations = ConversationRefSerializer(many=True)


class ConversationActionErrorSerializer(serializers.Serializer):
    conversation = serializers.JSONField()
    error = serializers.CharField()


class ArchiveResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    archived_count = serializers.IntegerField()
    message = serializers.CharField()
    errors = ConversationActionErrorSerializer(many=True, required=False)


class UnarchiveResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    unarchived_count = serializers.IntegerField()
    message = serializers.CharField()
    errors = ConversationActionErrorSerializer(many=True, required=False)


class ArchiveAllResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    archived_count = serializers.IntegerField()
    platform = serializers.CharField()
    message = serializers.CharField()


class DeleteConversationResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    messages_deleted = serializers.IntegerField()


class ClearHistorySerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=['facebook', 'instagram', 'whatsapp', 'email', 'widget', 'telegram', 'tiktok'])


class ClearHistoryResponseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text="'cleared'")
    platform = serializers.CharField()
    messages_deleted = serializers.IntegerField()
    archives_removed = serializers.IntegerField()
    message = serializers.CharField()


class MessagingWindowResponseSerializer(serializers.Serializer):
    window_open = serializers.BooleanField(help_text='24h customer-service window open')
    hours_remaining = serializers.FloatField(allow_null=True)
    human_agent_window_open = serializers.BooleanField(
        help_text='FB/IG only: 7-day HUMAN_AGENT window usable for this tenant',
    )
    human_agent_hours_remaining = serializers.FloatField(allow_null=True)
    can_reply = serializers.BooleanField()
    reply_mode = serializers.ChoiceField(
        choices=['response', 'human_agent', 'closed'],
        help_text='How the next human reply is sent: RESPONSE, MESSAGE_TAG+HUMAN_AGENT, or refused',
    )
    human_agent_tag_enabled = serializers.BooleanField(
        help_text='FB/IG only: this tenant may send HUMAN_AGENT-tagged replies (24h-7d)',
    )
    last_user_message_at = serializers.DateTimeField(required=False)
    hours_passed = serializers.FloatField(required=False)
    message = serializers.CharField(required=False, help_text='Set when the customer never wrote')


# ── Assignments ────────────────────────────────────────────────────────────

class ChatTransferSerializer(ConversationRefSerializer):
    target_user_id = serializers.IntegerField()


class AssignmentMessageResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    assignment = ChatAssignmentSerializer()


class EndSessionResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    assignment = ChatAssignmentSerializer()
    rating_link = serializers.CharField(allow_null=True)


class BulkAssignmentStatusResponseSerializer(serializers.Serializer):
    assignments = ChatAssignmentSerializer(many=True)
    settings = AssignmentSettingsFlagsSerializer()


# ── Sends ──────────────────────────────────────────────────────────────────

class MetaSendResponseSerializer(serializers.Serializer):
    """facebook/instagram send-message success body."""
    status = serializers.CharField(help_text="'sent'")
    message_id = serializers.CharField(allow_null=True)
    recipient_id = serializers.CharField(allow_null=True, required=False)
    messaging_type = serializers.CharField(required=False, help_text="'RESPONSE' or 'MESSAGE_TAG'")
    message_tag = serializers.CharField(required=False, allow_blank=True, help_text="'HUMAN_AGENT' or ''")
    text_part_failed = serializers.BooleanField(
        required=False, help_text='text+media send: the text half was rejected by Meta (media was sent)',
    )
    text_error = serializers.JSONField(required=False, help_text='{code, error_subcode, message} for the failed text half')


class WhatsAppSendResponseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text="'success'")
    message = serializers.CharField()
    message_id = serializers.CharField(allow_null=True)


class SendMediaFieldMixin(serializers.Serializer):
    media = serializers.FileField(required=False, help_text='One attachment (image / video / audio / document)')


class FacebookSendMultipartSerializer(SendMediaFieldMixin):
    recipient_id = serializers.CharField()
    message = serializers.CharField(required=False, allow_blank=True)
    page_id = serializers.CharField()
    reply_to_message_id = serializers.CharField(required=False, allow_blank=True)


class InstagramSendMultipartSerializer(SendMediaFieldMixin):
    recipient_id = serializers.CharField()
    message = serializers.CharField(required=False, allow_blank=True)
    instagram_account_id = serializers.CharField()
    reply_to_message_id = serializers.CharField(required=False, allow_blank=True)


class WhatsAppSendMultipartSerializer(SendMediaFieldMixin):
    to_number = serializers.CharField(help_text='E.164, must start with +')
    message = serializers.CharField(required=False, allow_blank=True)
    waba_id = serializers.CharField()
    reply_to_message_id = serializers.CharField(required=False, allow_blank=True)


class TelegramSendMultipartSerializer(SendMediaFieldMixin):
    account_id = serializers.CharField()
    peer_id = serializers.CharField()
    message = serializers.CharField(required=False, allow_blank=True)
    reply_to_message_id = serializers.CharField(required=False, allow_blank=True)


class TikTokSendResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    message = serializers.JSONField(required=False)


# ── Connections ────────────────────────────────────────────────────────────

class OAuthStartResponseSerializer(serializers.Serializer):
    oauth_url = serializers.CharField(
        help_text=(
            "Open in a browser. With ?return_to=app the callback finally redirects to "
            "echodesk://social/connections?platform=<facebook|whatsapp|tiktok>&status=<ok|error>&message=…"
        ),
    )
    redirect_uri = serializers.CharField(required=False)
    instructions = serializers.CharField(required=False)


class FacebookPageStatusSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    page_id = serializers.CharField()
    page_name = serializers.CharField()
    is_active = serializers.BooleanField()
    connected_at = serializers.DateTimeField()
    webhook_subscribed = serializers.BooleanField()
    connected_by = serializers.CharField(allow_null=True)


class FacebookStatusResponseSerializer(serializers.Serializer):
    connected = serializers.BooleanField(help_text='At least one ACTIVE page')
    pages_count = serializers.IntegerField(help_text='Active pages')
    pages = FacebookPageStatusSerializer(many=True, help_text='All pages incl. inactive (check is_active)')


class InstagramAccountStatusSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    instagram_account_id = serializers.CharField()
    username = serializers.CharField()
    profile_picture_url = serializers.CharField(allow_null=True, allow_blank=True)
    is_active = serializers.BooleanField()
    connected_at = serializers.DateTimeField()
    messaging_status = serializers.ChoiceField(
        choices=['unknown', 'ok', 'messaging_disabled', 'error'],
        help_text='messaging_disabled = the owner turned off "Allow access to messages" in Instagram',
    )
    messaging_error = serializers.CharField(allow_blank=True)
    messaging_error_code = serializers.CharField(allow_blank=True)
    messaging_checked_at = serializers.DateTimeField(allow_null=True)


class InstagramStatusResponseSerializer(serializers.Serializer):
    connected = serializers.BooleanField(help_text='Any account row exists (inactive included) — use accounts[].is_active')
    accounts_count = serializers.IntegerField()
    accounts = InstagramAccountStatusSerializer(many=True)


class WhatsAppAccountStatusSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    waba_id = serializers.CharField()
    business_name = serializers.CharField(allow_null=True, allow_blank=True)
    phone_number = serializers.CharField(allow_null=True, allow_blank=True)
    quality_rating = serializers.CharField(allow_null=True, allow_blank=True)
    is_active = serializers.BooleanField()
    connected_at = serializers.DateTimeField()


class WhatsAppStatusResponseSerializer(serializers.Serializer):
    connected = serializers.BooleanField()
    accounts_count = serializers.IntegerField()
    accounts = WhatsAppAccountStatusSerializer(many=True, help_text='Active accounts only')


class TikTokStatusResponseSerializer(serializers.Serializer):
    connected = serializers.BooleanField()
    account = TikTokShopAccountSerializer(allow_null=True)
    token_expires_at = serializers.DateTimeField(allow_null=True)
    is_token_expired = serializers.BooleanField()


class DeactivatedItemSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False)
    page_id = serializers.CharField(required=False)
    page_name = serializers.CharField(required=False)
    instagram_account_id = serializers.CharField(required=False)
    username = serializers.CharField(required=False)


class FacebookDisconnectResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    message = serializers.CharField()
    deactivated_pages = serializers.JSONField(required=False)
    facebook_pages_deactivated = serializers.IntegerField(required=False)


class FacebookPageDisconnectResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    message = serializers.CharField()
    page_id = serializers.CharField(required=False)
    page_name = serializers.CharField(required=False)


class InstagramDisconnectResponseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text="'disconnected' | 'no_accounts'")
    message = serializers.CharField()
    deactivated_accounts = serializers.JSONField(required=False)
    instagram_accounts_deactivated = serializers.IntegerField(required=False)


class WhatsAppDisconnectSerializer(serializers.Serializer):
    waba_id = serializers.CharField(required=False, help_text='Disconnect only this WABA (all when omitted)')


class WhatsAppDisconnectResponseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text="'success'")
    message = serializers.CharField()
    deactivated = serializers.JSONField(help_text='{business_name} for one WABA, {accounts: N} for all')


class StatusMessageResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    message = serializers.CharField()


class WhatsAppTemplateSyncResponseSerializer(serializers.Serializer):
    synced = serializers.IntegerField()
    templates = WhatsAppMessageTemplateSerializer(many=True)


# ── Clients ────────────────────────────────────────────────────────────────

class ClientPrefillSerializer(serializers.Serializer):
    name = serializers.CharField(allow_blank=True)
    email = serializers.CharField(allow_blank=True)
    visitor_id = serializers.CharField()
    session_id = serializers.CharField()


class ClientByAccountResponseSerializer(serializers.Serializer):
    found = serializers.BooleanField()
    client = SocialClientSerializer(allow_null=True)
    social_account = SocialAccountSerializer(allow_null=True)
    prefill = ClientPrefillSerializer(allow_null=True, help_text='Widget only, when not found: last session name/email')


class LinkAccountResponseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text="'linked' | 'updated'")
    social_account = SocialAccountSerializer()


class UnlinkAccountResponseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text="'unlinked'")


class ClientBookingHistoryResponseSerializer(serializers.Serializer):
    results = serializers.ListField(child=serializers.JSONField(), help_text='BookingList rows')
    count = serializers.IntegerField()
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()
    error = serializers.CharField(required=False, help_text='Booking not enabled for this client (results empty)')


class ClientBookingStatsResponseSerializer(serializers.Serializer):
    total = serializers.IntegerField(required=False)
    completed = serializers.IntegerField(required=False)
    upcoming = serializers.IntegerField(required=False)
    cancelled = serializers.IntegerField(required=False)
    total_spent = serializers.FloatField(required=False)
    error = serializers.CharField(required=False)
    stats = serializers.JSONField(required=False, allow_null=True)


class ReorderItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    position = serializers.IntegerField()


class ReorderSerializer(serializers.Serializer):
    items = ReorderItemSerializer(many=True)


class ReorderResponseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text="'reordered'")


# ── Quick replies ──────────────────────────────────────────────────────────

class QuickReplyUseResponseSerializer(serializers.Serializer):
    original_message = serializers.CharField()
    processed_message = serializers.CharField(help_text='Insert this into the composer')
    use_count = serializers.IntegerField()


# ── Auto-posting ───────────────────────────────────────────────────────────

class AutoPostListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True, help_text="Relative query string ('?page=2&page_size=20')")
    previous = serializers.CharField(allow_null=True)
    results = AutoPostContentSerializer(many=True)


class AutoPostRejectSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)


class AutoPostEditSerializer(serializers.Serializer):
    facebook_text = serializers.CharField(required=False, allow_blank=True)
    instagram_text = serializers.CharField(required=False, allow_blank=True)
    image_url = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    target_facebook = serializers.BooleanField(required=False)
    target_instagram = serializers.BooleanField(required=False)


class PublishingPageSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    page_id = serializers.CharField()
    page_name = serializers.CharField()
    has_publishing_permission = serializers.BooleanField()


class PublishingInstagramSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    instagram_account_id = serializers.CharField()
    username = serializers.CharField()
    has_publishing_permission = serializers.BooleanField()


class PublishingStatusResponseSerializer(serializers.Serializer):
    facebook_pages = PublishingPageSerializer(many=True)
    instagram_accounts = PublishingInstagramSerializer(many=True)


# ── Ratings ────────────────────────────────────────────────────────────────

class RatingTotalsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    average = serializers.FloatField()


class RatingBySourceSerializer(serializers.Serializer):
    social = RatingTotalsSerializer()
    calls_callback = RatingTotalsSerializer()
    calls_sms = RatingTotalsSerializer()


class RatingOverallSerializer(serializers.Serializer):
    total_ratings = serializers.IntegerField()
    average_rating = serializers.FloatField()
    by_source = RatingBySourceSerializer()


class RatingBreakdownSerializer(serializers.Serializer):
    def get_fields(self):
        return {str(i): serializers.IntegerField() for i in range(1, 6)}


class RatingUserStatSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    email = serializers.CharField()
    name = serializers.CharField()
    total_ratings = serializers.IntegerField()
    average_rating = serializers.FloatField()
    rating_breakdown = RatingBreakdownSerializer(help_text="Counts keyed '1'..'5'")
    social_ratings = serializers.IntegerField()
    call_ratings = serializers.IntegerField()


class RatingStatisticsResponseSerializer(serializers.Serializer):
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    overall = RatingOverallSerializer()
    users = RatingUserStatSerializer(many=True)


class RatingUserInfoSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    email = serializers.CharField()
    name = serializers.CharField()


class RatingSessionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    platform = serializers.CharField(help_text='facebook | instagram | whatsapp | telegram | widget | phone_sms | phone_callback')
    conversation_id = serializers.CharField(allow_null=True)
    account_id = serializers.CharField(allow_null=True)
    customer_name = serializers.CharField(allow_null=True)
    rating = serializers.IntegerField(allow_null=True, help_text='null while the customer has not rated')
    comment = serializers.CharField(allow_blank=True)
    session_started_at = serializers.DateTimeField(allow_null=True)
    session_ended_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()
    is_archived = serializers.BooleanField()


class UserRatingSessionsResponseSerializer(serializers.Serializer):
    user = RatingUserInfoSerializer()
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    sessions = RatingSessionSerializer(many=True, help_text='The requested page when page/page_size is given, else all')
    total_sessions = serializers.IntegerField()
    page = serializers.IntegerField(required=False)
    page_size = serializers.IntegerField(required=False)
    has_next = serializers.BooleanField(required=False)


# ── WhatsApp account actions ───────────────────────────────────────────────

class CoexLocalStatusSerializer(serializers.Serializer):
    coex_enabled = serializers.BooleanField()
    is_on_biz_app = serializers.BooleanField()
    platform_type = serializers.CharField(allow_null=True, allow_blank=True)
    sync_status = serializers.CharField(allow_null=True, allow_blank=True)
    onboarded_at = serializers.DateTimeField(allow_null=True)
    contacts_synced_at = serializers.DateTimeField(allow_null=True)
    history_synced_at = serializers.DateTimeField(allow_null=True)
    throughput_limit = serializers.IntegerField(allow_null=True)
    sync_window_open = serializers.BooleanField()
    sync_window_remaining_seconds = serializers.IntegerField(allow_null=True)


class CoexApiStatusSerializer(serializers.Serializer):
    is_on_biz_app = serializers.BooleanField()
    platform_type = serializers.CharField(allow_blank=True)
    verified_name = serializers.CharField(allow_blank=True)
    quality_rating = serializers.CharField(allow_blank=True)


class CoexStatusResponseSerializer(serializers.Serializer):
    local_status = CoexLocalStatusSerializer()
    api_status = CoexApiStatusSerializer(allow_null=True, help_text='Only with ?refresh=true')


class SyncHistorySerializer(serializers.Serializer):
    phase = serializers.ChoiceField(choices=['0-1', '1-90', '90-180'], required=False, default='0-1')


class SyncRequestedResponseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text="'success'")
    message = serializers.CharField()
    phase = serializers.CharField(required=False)
