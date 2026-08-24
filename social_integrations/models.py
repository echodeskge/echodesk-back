from django.conf import settings
from django.db import models
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password, check_password
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
import secrets

from crm.fields import EncryptedCharField

User = get_user_model()


class FacebookPageConnection(models.Model):
    """Stores Facebook page connection details for a tenant"""

    DEACTIVATION_REASONS = [
        ('manual', 'Manually Disconnected'),
        ('token_expired', 'Access Token Expired'),
        ('permission_revoked', 'Permissions Revoked'),
        ('oauth_error', 'OAuth Error'),
        ('api_error', 'API Error'),
    ]

    page_id = models.CharField(max_length=100, unique=True)  # Make unique directly
    page_name = models.CharField(max_length=200)
    page_access_token = models.TextField()
    is_active = models.BooleanField(default=True)
    has_publishing_permission = models.BooleanField(
        default=False,
        help_text='Whether this connection has been granted publishing scopes (pages_manage_posts, instagram_content_publish)'
    )

    # Who granted this connection — used for auditing and to scope the OAuth
    # stale-page sweep to the same Facebook user (so one person's OAuth can
    # never deactivate pages someone else connected).
    connected_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='connected_facebook_pages',
        help_text='EchoDesk user whose session initiated the OAuth connect',
    )
    connected_by_fb_user_id = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text='App-scoped Facebook user id that granted the page token',
    )
    webhook_subscribed = models.BooleanField(
        default=True,
        help_text='Whether the subscribed_apps webhook subscription succeeded at connect time',
    )

    # Deactivation tracking
    deactivated_at = models.DateTimeField(null=True, blank=True, help_text='When the page was deactivated')
    deactivation_reason = models.CharField(
        max_length=50,
        choices=DEACTIVATION_REASONS,
        null=True,
        blank=True,
        help_text='Reason why the page was deactivated'
    )
    deactivation_error_code = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text='Facebook error code if deactivated due to error'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.page_name} - Tenant Page"


class FacebookMessage(models.Model):
    """Stores Facebook page messages"""
    ATTACHMENT_TYPE_CHOICES = [
        ('image', 'Image'),
        ('video', 'Video'),
        ('audio', 'Audio'),
        ('file', 'File'),
        ('location', 'Location'),
        ('fallback', 'Fallback'),
    ]

    # Message source choices for tracking where messages originate
    SOURCE_CHOICES = [
        ('echodesk', 'EchoDesk'),
        ('facebook_app', 'Facebook App'),
        ('messenger_app', 'Messenger App'),
    ]

    page_connection = models.ForeignKey(FacebookPageConnection, on_delete=models.CASCADE, related_name='messages')
    message_id = models.CharField(max_length=100, unique=True)
    sender_id = models.CharField(max_length=100)
    sender_name = models.CharField(max_length=200, blank=True)
    profile_pic_url = models.URLField(max_length=500, blank=True, null=True)  # User's profile picture
    message_text = models.TextField(blank=True)  # Can be empty for attachment-only messages
    # Attachment fields
    attachment_type = models.CharField(max_length=20, choices=ATTACHMENT_TYPE_CHOICES, blank=True)
    attachment_url = models.URLField(max_length=1000, blank=True, null=True)  # URL for media attachments
    attachments = models.JSONField(default=list, blank=True)  # Array for multiple attachments
    timestamp = models.DateTimeField()
    is_from_page = models.BooleanField(default=False)  # True if message is from page to user
    is_delivered = models.BooleanField(default=False)  # True if message was delivered to customer
    delivered_at = models.DateTimeField(null=True, blank=True)  # When the message was delivered
    is_read = models.BooleanField(default=False)  # True if customer has read the message
    read_at = models.DateTimeField(null=True, blank=True)  # When the message was read
    is_read_by_staff = models.BooleanField(default=False)  # True if staff has read this incoming message
    read_by_staff_at = models.DateTimeField(null=True, blank=True)  # When staff read the message
    created_at = models.DateTimeField(auto_now_add=True)

    # Soft delete fields for admin investigation
    is_deleted = models.BooleanField(default=False, help_text='Soft deleted by staff')
    deleted_at = models.DateTimeField(null=True, blank=True, help_text='When the message was deleted')
    deleted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deleted_facebook_messages',
        help_text='Staff member who deleted this message'
    )

    # Reaction fields (from message_reactions webhook)
    reaction = models.CharField(max_length=20, blank=True, null=True, help_text='Reaction type: love, smile, angry, sad, wow, like')
    reaction_emoji = models.CharField(max_length=10, blank=True, null=True, help_text='Emoji representation of the reaction')
    reacted_by = models.CharField(max_length=100, blank=True, null=True, help_text='ID of the user who reacted')
    reacted_at = models.DateTimeField(null=True, blank=True, help_text='When the reaction was added')

    # Reply fields (for message replies)
    reply_to_message_id = models.CharField(max_length=100, blank=True, null=True, help_text='Message ID this is a reply to')
    reply_to = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='replies',
        help_text='Reference to the message this is replying to'
    )

    # Message source tracking
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default='echodesk',
        help_text="Source of the message: EchoDesk, Facebook App, or Messenger App"
    )
    is_echo = models.BooleanField(
        default=False,
        help_text="Message echoed from external app (sent by user on app, echoed to API)"
    )
    sent_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_facebook_messages',
        help_text='Staff member who sent this message via EchoDesk'
    )

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            # Supports the DISTINCT ON (sender_id) ORDER BY sender_id, timestamp DESC
            # used by unified_conversations to find the latest message per thread.
            models.Index(fields=['page_connection', 'sender_id', '-timestamp']),
        ]

    def __str__(self):
        if self.message_text:
            return f"Message from {self.sender_name} - {self.message_text[:50]}"
        elif self.attachment_type:
            return f"Message from {self.sender_name} - [{self.attachment_type}]"
        return f"Message from {self.sender_name}"


class OrphanedFacebookMessage(models.Model):
    """
    Stores Facebook messages that couldn't be matched to any tenant.
    This happens when:
    1. A page is deactivated but still receives messages
    2. A webhook is received for an unknown page_id
    3. The tenant connection was deleted

    These messages are saved in the PUBLIC schema for admin review.
    """
    page_id = models.CharField(max_length=100, db_index=True, help_text="Facebook page ID that sent the message")
    sender_id = models.CharField(max_length=100, help_text="ID of the person who sent the message")
    sender_name = models.CharField(max_length=200, blank=True, help_text="Name of the sender if available")
    message_id = models.CharField(max_length=100, blank=True, help_text="Facebook message ID if available")
    message_text = models.TextField(help_text="Content of the message")
    timestamp = models.DateTimeField(help_text="When the message was sent")
    raw_webhook_data = models.JSONField(help_text="Full webhook payload for debugging")
    error_reason = models.CharField(
        max_length=255,
        default='page_not_found',
        help_text="Why this message was orphaned"
    )
    reviewed = models.BooleanField(default=False, help_text="Whether an admin has reviewed this message")
    reviewed_at = models.DateTimeField(null=True, blank=True, help_text="When the message was reviewed")
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_orphaned_messages',
        help_text="Admin who reviewed this message"
    )
    notes = models.TextField(blank=True, help_text="Admin notes about this orphaned message")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When this record was created")

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['page_id', 'timestamp']),
            models.Index(fields=['reviewed', 'created_at']),
        ]
        verbose_name = "Orphaned Facebook Message"
        verbose_name_plural = "Orphaned Facebook Messages"

    def __str__(self):
        return f"Orphaned message from {self.sender_name or self.sender_id} to page {self.page_id} - {self.message_text[:50]}"


class InstagramAccountConnection(models.Model):
    """Stores Instagram Business account connection details for a tenant"""

    instagram_account_id = models.CharField(max_length=100, unique=True)
    username = models.CharField(max_length=200)
    profile_picture_url = models.URLField(max_length=500, blank=True, null=True)
    access_token = models.TextField()
    # Instagram accounts are connected through Facebook Pages
    facebook_page = models.ForeignKey(
        FacebookPageConnection,
        on_delete=models.CASCADE,
        related_name='instagram_accounts',
        null=True,
        blank=True
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"@{self.username} - Instagram"


class InstagramMessage(models.Model):
    """Stores Instagram Direct messages"""
    ATTACHMENT_TYPE_CHOICES = [
        ('image', 'Image'),
        ('video', 'Video'),
        ('audio', 'Audio'),
        ('file', 'File'),
        ('share', 'Share'),  # Instagram story/post shares
        ('story_mention', 'Story Mention'),
        ('story_reply', 'Story Reply'),
    ]

    # Message source choices for tracking where messages originate
    SOURCE_CHOICES = [
        ('echodesk', 'EchoDesk'),
        ('instagram_app', 'Instagram App'),
    ]

    account_connection = models.ForeignKey(
        InstagramAccountConnection,
        on_delete=models.CASCADE,
        related_name='messages'
    )
    message_id = models.CharField(max_length=255, unique=True)  # Instagram IDs are base64 encoded, need more space
    sender_id = models.CharField(max_length=100)
    sender_name = models.CharField(max_length=200, blank=True)  # Display name
    sender_username = models.CharField(max_length=200, blank=True)  # @username
    sender_profile_pic = models.URLField(max_length=500, blank=True, null=True)
    message_text = models.TextField(blank=True)  # Can be empty for media-only messages
    # Attachment fields
    attachment_type = models.CharField(max_length=20, choices=ATTACHMENT_TYPE_CHOICES, blank=True)
    attachment_url = models.URLField(max_length=1000, blank=True, null=True)  # URL for media attachments
    attachments = models.JSONField(default=list, blank=True)  # Array for multiple attachments
    timestamp = models.DateTimeField()
    is_from_business = models.BooleanField(default=False)  # True if sent by business
    is_delivered = models.BooleanField(default=False)  # True if message was delivered to customer
    delivered_at = models.DateTimeField(null=True, blank=True)  # When the message was delivered
    is_read = models.BooleanField(default=False)  # True if customer has read the message
    read_at = models.DateTimeField(null=True, blank=True)  # When the message was read
    is_read_by_staff = models.BooleanField(default=False)  # True if staff has read this incoming message
    read_by_staff_at = models.DateTimeField(null=True, blank=True)  # When staff read the message
    created_at = models.DateTimeField(auto_now_add=True)

    # Soft delete fields for admin investigation
    is_deleted = models.BooleanField(default=False, help_text='Soft deleted by staff')
    deleted_at = models.DateTimeField(null=True, blank=True, help_text='When the message was deleted')
    deleted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deleted_instagram_messages',
        help_text='Staff member who deleted this message'
    )

    # Message source tracking
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default='echodesk',
        help_text="Source of the message: EchoDesk or Instagram App"
    )
    is_echo = models.BooleanField(
        default=False,
        help_text="Message echoed from external app (sent by user on app, echoed to API)"
    )
    sent_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_instagram_messages',
        help_text='Staff member who sent this message via EchoDesk'
    )

    # Reply fields (for message replies) — Instagram sends reply_to.mid in the
    # webhook, same shape as Facebook.
    reply_to_message_id = models.CharField(max_length=255, blank=True, null=True, help_text='Message ID this is a reply to')
    reply_to = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='replies',
        help_text='Reference to the message this is replying to'
    )

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            # Supports the DISTINCT ON (sender_id) ORDER BY sender_id, timestamp DESC
            # used by unified_conversations to find the latest message per thread.
            models.Index(fields=['account_connection', 'sender_id', '-timestamp']),
        ]

    def __str__(self):
        if self.message_text:
            return f"Instagram DM from @{self.sender_username} - {self.message_text[:50]}"
        elif self.attachment_type:
            return f"Instagram DM from @{self.sender_username} - [{self.attachment_type}]"
        return f"Instagram DM from @{self.sender_username}"


class WhatsAppBusinessAccount(models.Model):
    """Stores WhatsApp Business Account connection details for a tenant"""
    waba_id = models.CharField(max_length=100, unique=True)  # WhatsApp Business Account ID
    business_name = models.CharField(max_length=200)
    phone_number_id = models.CharField(max_length=100)  # Phone number ID for sending messages
    phone_number = models.CharField(max_length=20)  # Display phone number (e.g., +1234567890)
    display_phone_number = models.CharField(max_length=30, blank=True)  # Formatted display number
    access_token = models.TextField()  # Long-lived access token
    quality_rating = models.CharField(max_length=50, blank=True)  # Quality rating from WhatsApp
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # WhatsApp Business App Coexistence fields
    SYNC_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('syncing', 'Syncing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    PLATFORM_TYPE_CHOICES = [
        ('CLOUD_API', 'Cloud API'),
        ('ON_PREMISE', 'On-Premise'),
        ('SMB', 'Small/Medium Business App'),
    ]

    is_on_biz_app = models.BooleanField(
        default=False,
        help_text="Whether this number is linked to WhatsApp Business App"
    )
    platform_type = models.CharField(
        max_length=50,
        choices=PLATFORM_TYPE_CHOICES,
        blank=True,
        null=True,
        help_text="Platform type: CLOUD_API, ON_PREMISE, or SMB"
    )
    coex_enabled = models.BooleanField(
        default=False,
        help_text="Whether coexistence mode is enabled for this account"
    )
    sync_status = models.CharField(
        max_length=20,
        choices=SYNC_STATUS_CHOICES,
        default='pending',
        help_text="Status of data sync from Business App"
    )
    contacts_synced_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When contacts were last synced from Business App"
    )
    history_synced_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When message history was last synced from Business App"
    )
    onboarded_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When coexistence onboarding was completed (24hr sync window starts)"
    )
    throughput_limit = models.IntegerField(
        default=80,
        help_text="Messages per second limit (20 for coex accounts, 80 for standard)"
    )

    def __str__(self):
        return f"{self.business_name} - {self.display_phone_number or self.phone_number}"


class WhatsAppMessageTemplate(models.Model):
    """Stores WhatsApp message templates for a business account"""
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]

    CATEGORY_CHOICES = [
        ('MARKETING', 'Marketing'),
        ('UTILITY', 'Utility'),
        ('AUTHENTICATION', 'Authentication'),
    ]

    business_account = models.ForeignKey(
        WhatsAppBusinessAccount,
        on_delete=models.CASCADE,
        related_name='templates'
    )
    template_id = models.CharField(max_length=100, blank=True)  # Meta's template ID (assigned after creation)
    name = models.CharField(max_length=512)  # Template name (lowercase, no spaces, underscores allowed)
    language = models.CharField(max_length=10, default='en')  # Language code (en, ka, ru, etc.)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='UTILITY')
    components = models.JSONField(default=list)  # Array of component objects (header, body, footer, buttons)
    created_by = models.ForeignKey(
        'users.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_whatsapp_templates'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = [['business_account', 'name', 'language']]
        indexes = [
            models.Index(fields=['business_account', 'status']),
            models.Index(fields=['name']),
        ]

    def __str__(self):
        return f"{self.name} ({self.language}) - {self.status}"


class WhatsAppMessage(models.Model):
    """Stores WhatsApp messages"""
    MESSAGE_TYPE_CHOICES = [
        ('text', 'Text'),
        ('image', 'Image'),
        ('video', 'Video'),
        ('audio', 'Audio'),
        ('document', 'Document'),
        ('sticker', 'Sticker'),
        ('location', 'Location'),
        ('contacts', 'Contacts'),
        ('interactive', 'Interactive'),
    ]

    STATUS_CHOICES = [
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('read', 'Read'),
        ('failed', 'Failed'),
    ]

    # Message source choices for coexistence
    SOURCE_CHOICES = [
        ('cloud_api', 'Cloud API'),
        ('business_app', 'Business App'),
        ('synced', 'Synced from History'),
    ]

    business_account = models.ForeignKey(
        WhatsAppBusinessAccount,
        on_delete=models.CASCADE,
        related_name='messages'
    )
    message_id = models.CharField(max_length=255, unique=True)  # WhatsApp message ID (wamid)
    from_number = models.CharField(max_length=20)  # Sender's phone number
    to_number = models.CharField(max_length=20)  # Recipient's phone number
    contact_name = models.CharField(max_length=200, blank=True)  # Contact name if available
    profile_pic_url = models.URLField(max_length=500, blank=True, null=True)  # Contact's profile picture
    message_text = models.TextField(blank=True)  # Message text content
    message_type = models.CharField(max_length=20, choices=MESSAGE_TYPE_CHOICES, default='text')
    media_url = models.URLField(max_length=1000, blank=True, null=True)  # URL for media messages
    media_mime_type = models.CharField(max_length=100, blank=True)  # MIME type for media
    attachments = models.JSONField(default=list, blank=True)  # Array for multiple attachments
    # Template-related fields
    template = models.ForeignKey(
        WhatsAppMessageTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='messages'
    )  # Template used for this message (if template-based)
    template_parameters = models.JSONField(null=True, blank=True)  # Parameters used to fill template variables
    timestamp = models.DateTimeField()  # Message timestamp from WhatsApp
    is_from_business = models.BooleanField(default=False)  # True if sent by business
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='sent')
    is_delivered = models.BooleanField(default=False)  # True if message was delivered
    delivered_at = models.DateTimeField(null=True, blank=True)  # When the message was delivered
    is_read = models.BooleanField(default=False)  # True if customer has read the message
    read_at = models.DateTimeField(null=True, blank=True)  # When the message was read
    is_read_by_staff = models.BooleanField(default=False)  # True if staff has read this incoming message
    read_by_staff_at = models.DateTimeField(null=True, blank=True)  # When staff read the message
    error_message = models.TextField(blank=True)  # Error message if failed
    created_at = models.DateTimeField(auto_now_add=True)

    # WhatsApp Business App Coexistence fields
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default='cloud_api',
        help_text="Source of the message: Cloud API, Business App, or Synced from History"
    )
    is_echo = models.BooleanField(
        default=False,
        help_text="Message echoed from Business App (sent by user on app, echoed to API)"
    )
    sent_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_whatsapp_messages',
        help_text='Staff member who sent this message via EchoDesk'
    )

    # Edit support
    is_edited = models.BooleanField(
        default=False,
        help_text="Whether this message has been edited"
    )
    edited_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the message was edited"
    )
    original_text = models.TextField(
        blank=True,
        null=True,
        help_text="Original message text before edit"
    )

    # Revoke (delete for everyone) support
    is_revoked = models.BooleanField(
        default=False,
        help_text="Whether this message has been revoked (deleted for everyone)"
    )
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the message was revoked"
    )

    # Reaction fields
    reaction_emoji = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        help_text='Emoji reaction on this message'
    )
    reacted_by = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text='Phone number of user who reacted'
    )
    reacted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the reaction was added'
    )

    # Reply fields (for message replies / quote-reply)
    reply_to_message_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text='WhatsApp message ID (wamid) this is a reply to'
    )
    reply_to = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='replies',
        help_text='Reference to the message this is replying to'
    )

    # Soft delete fields for admin investigation
    is_deleted = models.BooleanField(default=False, help_text='Soft deleted by staff')
    deleted_at = models.DateTimeField(null=True, blank=True, help_text='When the message was deleted')
    deleted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deleted_whatsapp_messages',
        help_text='Staff member who deleted this message'
    )

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['from_number', 'to_number']),
            models.Index(fields=['business_account', 'timestamp']),
            # Supports DISTINCT ON (from_number) ORDER BY from_number, -timestamp
            # used by unified_conversations to find the latest customer-sent message
            # per customer (is_from_business=False branch of the split query).
            models.Index(fields=['business_account', 'from_number', '-timestamp']),
            # Supports DISTINCT ON (to_number) ORDER BY to_number, -timestamp used
            # for the latest business-sent message per customer (is_from_business=True).
            models.Index(fields=['business_account', 'to_number', '-timestamp']),
        ]

    def __str__(self):
        direction = "to" if self.is_from_business else "from"
        contact = self.contact_name or self.from_number
        return f"WhatsApp {direction} {contact} - {self.message_text[:50]}"


class WhatsAppContact(models.Model):
    """
    Stores contacts synced from WhatsApp Business App during coexistence onboarding.
    These contacts represent users who have previously interacted with the business
    on the WhatsApp Business App.
    """
    CONTACT_TYPE_CHOICES = [
        ('USER', 'User'),
        ('BUSINESS', 'Business'),
    ]

    account = models.ForeignKey(
        WhatsAppBusinessAccount,
        on_delete=models.CASCADE,
        related_name='contacts'
    )
    wa_id = models.CharField(
        max_length=50,
        help_text="WhatsApp ID (phone number in international format without +)"
    )
    profile_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Contact's WhatsApp profile name"
    )
    is_business = models.BooleanField(
        default=False,
        help_text="Whether this contact is a business account"
    )
    contact_type = models.CharField(
        max_length=50,
        choices=CONTACT_TYPE_CHOICES,
        default='USER',
        help_text="Type of contact: USER or BUSINESS"
    )
    synced_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When this contact was synced from Business App"
    )
    last_message_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the last message was exchanged with this contact"
    )

    class Meta:
        unique_together = ['account', 'wa_id']
        ordering = ['-last_message_at', '-synced_at']
        indexes = [
            models.Index(fields=['account', 'wa_id']),
            models.Index(fields=['account', 'last_message_at']),
        ]

    def __str__(self):
        return f"{self.profile_name or self.wa_id} - {self.account.business_name}"


class WhatsAppOutboundConsentLog(models.Model):
    """
    Append-only audit record for business-initiated WhatsApp sends (template
    messages an agent starts toward a number, including cold outreach to people
    who never messaged us).

    Exactly one row is written per send attempt — on success, when blocked by
    the opt-in guard, and on send failure — capturing who confirmed opt-in,
    when, to whom, with which template, and the outcome. This is the compliance
    trail WhatsApp policy effectively requires for opt-in. Never updated/deleted.
    """
    OUTCOME_CHOICES = [
        ('sent', 'Sent'),
        ('blocked', 'Blocked'),
        ('failed', 'Failed'),
    ]

    business_account = models.ForeignKey(
        WhatsAppBusinessAccount,
        on_delete=models.CASCADE,
        related_name='outbound_consent_logs'
    )
    agent = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='whatsapp_outbound_consent_logs',
        help_text="Agent who initiated the send"
    )
    to_number = models.CharField(max_length=20, help_text="Recipient phone number")
    template = models.ForeignKey(
        WhatsAppMessageTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='outbound_consent_logs'
    )
    opt_in_confirmed = models.BooleanField(
        default=False,
        help_text="Whether the agent confirmed the recipient opted in"
    )
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES)
    error_code = models.CharField(
        max_length=50,
        blank=True,
        help_text="Stable error code when outcome is blocked/failed"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['business_account', '-created_at']),
            models.Index(fields=['to_number']),
        ]

    def __str__(self):
        return f"{self.outcome} -> {self.to_number} ({self.created_at:%Y-%m-%d})"


class SocialIntegrationSettings(models.Model):
    """Stores tenant-specific settings for social integrations"""
    # Singleton pattern - only one settings object per tenant
    refresh_interval = models.IntegerField(
        default=5000,
        help_text="Auto-refresh interval in milliseconds for messages page (min: 1000, max: 60000)"
    )

    # Chat assignment mode - allows users to claim/assign chats to themselves
    chat_assignment_enabled = models.BooleanField(
        default=False,
        help_text="When enabled, users can claim chats and assign them to themselves."
    )

    # Session management - allows start/end session functionality
    session_management_enabled = models.BooleanField(
        default=False,
        help_text="When enabled, users can start and end chat sessions."
    )

    # Hide assigned chats from other users
    hide_assigned_chats = models.BooleanField(
        default=False,
        help_text="When enabled, assigned chats are hidden from other users (except admins)."
    )

    # Collect customer rating after session ends
    collect_customer_rating = models.BooleanField(
        default=False,
        help_text="When enabled, customers will be asked to rate the session after it ends."
    )

    # Stale assignment reminders
    stale_assignment_reminder_enabled = models.BooleanField(
        default=False,
        help_text="When enabled, agents get a popup reminder about assigned chats with no recent activity."
    )
    stale_assignment_reminder_minutes = models.PositiveIntegerField(
        default=60,
        validators=[MinValueValidator(5), MaxValueValidator(10080)],
        help_text="Minutes of inactivity before an assigned chat is considered stale (5–10080)."
    )

    # Link-based rating settings
    link_based_rating_enabled = models.BooleanField(
        default=False,
        help_text="When enabled, customers receive a link to rate their experience instead of replying to a message."
    )
    rating_request_message_template_ka = models.TextField(
        default="გმადლობთ ჩვენთან საუბრისთვის! გთხოვთ შეაფასოთ თქვენი გამოცდილება: {link}",
        help_text="Georgian template for rating request message. Use {link} as placeholder."
    )
    rating_request_message_template_en = models.TextField(
        default="Thank you for chatting with us! Please rate your experience: {link}",
        help_text="English template for rating request message. Use {link} as placeholder."
    )

    post_review_redirect_url = models.URLField(
        max_length=500,
        blank=True,
        default='',
        help_text="URL to redirect customers to after they submit a review (e.g. your website)."
    )

    # Notification sound settings (per platform)
    notification_sound_facebook = models.CharField(
        max_length=255,
        default='mixkit-bubble-pop-up-alert-notification-2357.wav',
        help_text="Sound file for Facebook notifications"
    )
    notification_sound_instagram = models.CharField(
        max_length=255,
        default='mixkit-magic-notification-ring-2344.wav',
        help_text="Sound file for Instagram notifications"
    )
    notification_sound_whatsapp = models.CharField(
        max_length=255,
        default='mixkit-positive-notification-951.wav',
        help_text="Sound file for WhatsApp notifications"
    )
    notification_sound_email = models.CharField(
        max_length=255,
        default='mixkit-bell-notification-933.wav',
        help_text="Sound file for Email notifications"
    )
    notification_sound_team_chat = models.CharField(
        max_length=255,
        default='mixkit-happy-bells-notification-937.wav',
        help_text="Sound file for Team Chat notifications"
    )
    notification_sound_system = models.CharField(
        max_length=255,
        default='mixkit-confirmation-tone-2867.wav',
        help_text="Sound file for System notifications"
    )

    # Embeddable chat widget settings
    widget_enabled = models.BooleanField(
        default=False,
        help_text="Enable the embeddable chat widget for this tenant."
    )
    notification_sound_widget = models.CharField(
        max_length=64,
        default='default',
        help_text="Sound identifier for widget notifications."
    )
    notification_sound_telegram = models.CharField(
        max_length=64,
        default='default',
        help_text="Sound identifier for Telegram notifications."
    )

    # Auto-Reply Settings - Timezone & Away Hours
    timezone = models.CharField(
        max_length=50,
        default='UTC',
        help_text="Business timezone (e.g., 'Asia/Tbilisi', 'Europe/London')"
    )
    away_hours_enabled = models.BooleanField(
        default=False,
        help_text="Enable away hours schedule for auto-replies"
    )
    # Store as JSON: {"monday": [0,1,2,3,4,5,6,7,8,18,19,20,21,22,23], "tuesday": [...], ...}
    # Each day contains array of hours (0-23) when business is AWAY
    away_hours_schedule = models.JSONField(
        default=dict,
        blank=True,
        help_text="Weekly away schedule - each day maps to array of away hours (0-23)"
    )

    # Per-Platform Auto-Reply Settings (stored as JSON)
    # Format: {
    #   "facebook": {"welcome_enabled": true, "welcome_message": "...", "away_enabled": true, "away_message": "..."},
    #   "instagram": {"welcome_enabled": true, "welcome_message": "...", "away_enabled": false, "away_message": ""},
    #   "whatsapp": {"welcome_enabled": false, "welcome_message": "", "away_enabled": true, "away_message": "..."}
    # }
    auto_reply_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-platform auto-reply settings (welcome/away messages)"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Social Integration Settings"
        verbose_name_plural = "Social Integration Settings"

    def __str__(self):
        return f"Social Settings (Refresh: {self.refresh_interval}ms)"

    def save(self, *args, **kwargs):
        # Enforce min/max limits
        if self.refresh_interval < 1000:
            self.refresh_interval = 1000
        elif self.refresh_interval > 60000:
            self.refresh_interval = 60000
        super().save(*args, **kwargs)


class ConversationAutoReply(models.Model):
    """Tracks auto-reply history per conversation for welcome/away message cooldowns"""
    PLATFORM_CHOICES = [
        ('facebook', 'Facebook'),
        ('instagram', 'Instagram'),
        ('whatsapp', 'WhatsApp'),
        ('telegram', 'Telegram'),
    ]

    platform = models.CharField(
        max_length=20,
        choices=PLATFORM_CHOICES,
        help_text="Messaging platform"
    )
    account_id = models.CharField(
        max_length=100,
        help_text="Account identifier (page_id for FB, instagram_account_id for IG, waba_id for WhatsApp)"
    )
    conversation_id = models.CharField(
        max_length=100,
        help_text="Customer identifier (sender_id for FB/IG, from_number for WhatsApp)"
    )
    last_welcome_sent = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the last welcome message was sent to this conversation"
    )
    last_away_sent = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the last away message was sent to this conversation"
    )

    class Meta:
        unique_together = ['platform', 'account_id', 'conversation_id']
        indexes = [
            models.Index(fields=['platform', 'account_id', 'conversation_id']),
        ]
        verbose_name = "Conversation Auto-Reply"
        verbose_name_plural = "Conversation Auto-Replies"

    def __str__(self):
        return f"{self.platform} - {self.conversation_id}"


class ChatAssignment(models.Model):
    """Tracks chat assignments to users for session management"""
    PLATFORM_CHOICES = [
        ('facebook', 'Facebook'),
        ('instagram', 'Instagram'),
        ('whatsapp', 'WhatsApp'),
        ('email', 'Email'),
        ('tiktok', 'TikTok'),
        ('widget', 'Website widget'),
        ('telegram', 'Telegram'),
    ]

    STATUS_CHOICES = [
        ('active', 'Active'),           # Chat is assigned, session not started
        ('in_session', 'In Session'),   # User has started a session
        ('completed', 'Completed'),     # Session ended, waiting for rating or rated
    ]

    # Composite key for conversation: platform + conversation_id + account_id
    platform = models.CharField(
        max_length=20,
        choices=PLATFORM_CHOICES,
        help_text="Messaging platform"
    )
    conversation_id = models.CharField(
        max_length=255,
        help_text="Customer identifier (sender_id for FB/IG, from_number for WhatsApp)"
    )
    account_id = models.CharField(
        max_length=255,
        help_text="Account identifier (page_id for FB, account_id for IG, waba_id for WhatsApp)"
    )

    # Assignment details
    assigned_user = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='chat_assignments',
        null=True,
        blank=True,
        help_text="User this chat is assigned to (null when session completed)"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='active',
        help_text="Current status of the assignment"
    )

    # Session tracking
    session_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the session was started"
    )
    session_ended_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the session was ended"
    )

    # Timestamps
    assigned_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Only one active assignment per conversation at a time
        unique_together = [['platform', 'conversation_id', 'account_id']]
        indexes = [
            models.Index(fields=['assigned_user', 'status']),
            models.Index(fields=['platform', 'conversation_id']),
            models.Index(fields=['platform', 'account_id']),
        ]
        verbose_name = "Chat Assignment"
        verbose_name_plural = "Chat Assignments"

    def __str__(self):
        # assigned_user is nulled out by end_session (see views.py) when a
        # chat is released back to the queue, so guard the attribute access.
        who = self.assigned_user.email if self.assigned_user_id else "(unassigned)"
        return f"{self.platform} - {self.conversation_id} -> {who}"

    @property
    def full_conversation_id(self):
        """Returns the full conversation ID as used in frontend"""
        # Platforms whose chat ID prefix matches the platform name verbatim:
        # email -> email_<account>_<conv>, widget -> widget_<account>_<conv>,
        # tiktok -> tiktok_<account>_<conv>.
        if self.platform in ('email', 'widget', 'tiktok'):
            return f"{self.platform}_{self.account_id}_{self.conversation_id}"
        prefix = {'facebook': 'fb', 'instagram': 'ig', 'whatsapp': 'wa', 'telegram': 'tg'}.get(self.platform)
        if not prefix:
            # Defensive: future-proof against new platforms slipping in.
            return f"{self.platform}_{self.account_id}_{self.conversation_id}"
        return f"{prefix}_{self.account_id}_{self.conversation_id}"


class ChatRating(models.Model):
    """Stores customer ratings for chat sessions"""

    # Store assignment reference (nullable - assignment deleted after rating)
    assignment = models.ForeignKey(
        ChatAssignment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ratings',
        help_text="The assignment this rating is for (may be null after completion)"
    )

    # Store user directly so rating persists after assignment deletion
    rated_user = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        null=True,  # Nullable for migration, will always be set in code
        blank=True,
        related_name='chat_ratings',
        help_text="The user who handled this chat session"
    )

    # Store conversation info for historical reference
    platform = models.CharField(
        max_length=20,
        choices=[('facebook', 'Facebook'), ('instagram', 'Instagram'), ('whatsapp', 'WhatsApp'), ('email', 'Email'), ('tiktok', 'TikTok'), ('widget', 'Website widget')],
        default='facebook',  # Default for migration
        help_text="Platform where the chat occurred"
    )
    conversation_id = models.CharField(
        max_length=255,
        default='',  # Default for migration
        help_text="Conversation identifier"
    )
    account_id = models.CharField(
        max_length=255,
        default='',  # Default for migration
        help_text="Account identifier (page_id, account_id, or waba_id)"
    )

    rating = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        default=0,
        help_text="Customer rating from 1-5 (0 = pending response)"
    )

    # Link-based rating fields
    rating_token = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text="Secure token for link-based rating"
    )
    token_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the rating token expires"
    )
    comment = models.TextField(
        blank=True,
        default='',
        help_text="Optional comment from customer"
    )

    # Session timing
    session_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the session started"
    )
    session_ended_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the session ended"
    )

    # Message tracking for the rating flow
    rating_request_message_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Message ID of the rating request sent to customer"
    )
    rating_response_message_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Message ID of the customer's rating response"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['rating', 'created_at']),
            models.Index(fields=['rated_user', 'created_at']),
            models.Index(fields=['platform', 'created_at']),
        ]
        verbose_name = "Chat Rating"
        verbose_name_plural = "Chat Ratings"

    def __str__(self):
        if self.rating == 0:
            return f"Pending rating for {self.rated_user}"
        return f"Rating {self.rating}/5 for {self.rated_user}"


class EmailConnection(models.Model):
    """Stores Email (IMAP/SMTP) connection details for a tenant - supports multiple connections per tenant"""

    # Identity
    email_address = models.EmailField(help_text="Email address for this connection")
    display_name = models.CharField(max_length=200, blank=True, help_text="Display name for sent emails")

    # IMAP Settings (incoming)
    imap_server = models.CharField(max_length=255, help_text="IMAP server hostname (e.g., imap.gmail.com)")
    imap_port = models.IntegerField(default=993, help_text="IMAP port (993 for SSL, 143 for STARTTLS)")
    imap_use_ssl = models.BooleanField(default=True, help_text="Use SSL for IMAP connection")

    # SMTP Settings (outgoing)
    smtp_server = models.CharField(max_length=255, help_text="SMTP server hostname (e.g., smtp.gmail.com)")
    smtp_port = models.IntegerField(default=587, help_text="SMTP port (587 for STARTTLS, 465 for SSL)")
    smtp_use_tls = models.BooleanField(default=True, help_text="Use TLS/STARTTLS for SMTP")
    smtp_use_ssl = models.BooleanField(default=False, help_text="Use SSL for SMTP (alternative to TLS)")

    # Credentials (encrypted using Django's Signer)
    username = models.CharField(max_length=255, help_text="Login username (usually email address)")
    encrypted_password = models.TextField(help_text="Encrypted password - DO NOT store plain text")

    # Status
    is_active = models.BooleanField(default=True)
    last_sync_at = models.DateTimeField(null=True, blank=True, help_text="Last successful IMAP sync")
    last_sync_error = models.TextField(blank=True, help_text="Last sync error message if any")
    auto_disabled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Set when the connection was disabled automatically by the sync "
            "worker after a non-transient error. Non-null means `is_active` was "
            "flipped to False by the system (not by a human); the user must go "
            "to email settings and hit Reactivate to resume sync."
        ),
    )
    sync_failure_count = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Number of consecutive sync attempts that have failed. Reset to 0 "
            "on a successful sync or on manual reactivate. Once this crosses "
            "the sync worker's threshold the connection is auto-disabled, "
            "even for 'transient' errors (e.g. repeated connection resets)."
        ),
    )

    # Sync settings
    sync_folder = models.CharField(max_length=100, default='INBOX', help_text="IMAP folder to sync")
    sync_days_back = models.IntegerField(default=365, help_text="Number of days of history to sync")

    # Email signature settings (per connection)
    signature_enabled = models.BooleanField(default=False, help_text="Whether to append signature to outgoing emails")
    signature_html = models.TextField(blank=True, help_text="HTML signature for outgoing emails")
    signature_text = models.TextField(blank=True, help_text="Plain text signature for outgoing emails")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Email Connection"
        verbose_name_plural = "Email Connections"

    def __str__(self):
        return f"{self.display_name or self.email_address}"

    def set_password(self, raw_password):
        """Encrypt and store the password using Django's Signer"""
        from django.core.signing import Signer
        signer = Signer()
        self.encrypted_password = signer.sign(raw_password)

    def get_password(self):
        """Decrypt and return the password"""
        from django.core.signing import Signer, BadSignature
        signer = Signer()
        try:
            return signer.unsign(self.encrypted_password)
        except BadSignature:
            return None


class EmailMessage(models.Model):
    """Stores email messages - both sent and received"""

    connection = models.ForeignKey(
        EmailConnection,
        on_delete=models.CASCADE,
        related_name='messages'
    )

    # Email identifiers
    message_id = models.CharField(max_length=500, unique=True, help_text="RFC 2822 Message-ID header")
    thread_id = models.CharField(max_length=500, blank=True, db_index=True, help_text="Thread grouping identifier")
    in_reply_to = models.CharField(max_length=500, blank=True, help_text="In-Reply-To header for threading")
    references = models.TextField(blank=True, help_text="References header for threading")

    # Sender/Recipients
    from_email = models.EmailField(help_text="Sender email address")
    from_name = models.CharField(max_length=200, blank=True, help_text="Sender display name")
    to_emails = models.JSONField(default=list, help_text="List of To recipients [{email, name}]")
    cc_emails = models.JSONField(default=list, blank=True, help_text="List of CC recipients")
    bcc_emails = models.JSONField(default=list, blank=True, help_text="List of BCC recipients")
    reply_to = models.EmailField(blank=True, help_text="Reply-To address if different from from_email")

    # Content
    subject = models.CharField(max_length=1000, blank=True)
    body_text = models.TextField(blank=True, help_text="Plain text body")
    body_html = models.TextField(blank=True, help_text="HTML body")
    attachments = models.JSONField(default=list, help_text="Array of {filename, content_type, url, size}")

    # Email metadata
    timestamp = models.DateTimeField(help_text="Email Date header")
    folder = models.CharField(max_length=100, default='INBOX', help_text="IMAP folder this email is in")
    uid = models.CharField(max_length=50, blank=True, help_text="IMAP UID for this message in folder")

    # Status flags (from IMAP)
    is_from_business = models.BooleanField(default=False, help_text="True if sent by business")
    is_read = models.BooleanField(default=False, help_text="IMAP SEEN flag")
    is_starred = models.BooleanField(default=False, help_text="IMAP FLAGGED flag")
    is_answered = models.BooleanField(default=False, help_text="IMAP ANSWERED flag")
    is_draft = models.BooleanField(default=False, help_text="True if this is a draft")

    # Labels/Folders (for Gmail-like label support)
    labels = models.JSONField(default=list, help_text="Array of label names")

    # Staff interaction tracking
    is_read_by_staff = models.BooleanField(default=False)
    read_by_staff_at = models.DateTimeField(null=True, blank=True)

    # Soft delete
    is_deleted = models.BooleanField(default=False, help_text='Soft deleted by staff')
    deleted_at = models.DateTimeField(null=True, blank=True, help_text='When the message was deleted')
    deleted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deleted_email_messages',
        help_text='Staff member who deleted this message'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['connection', 'thread_id']),
            models.Index(fields=['connection', 'folder', 'timestamp']),
            models.Index(fields=['from_email', 'timestamp']),
        ]
        verbose_name = "Email Message"
        verbose_name_plural = "Email Messages"

    def __str__(self):
        return f"Email: {self.subject[:50]} from {self.from_name or self.from_email}"


class EmailDraft(models.Model):
    """Stores email drafts before sending"""

    connection = models.ForeignKey(
        EmailConnection,
        on_delete=models.CASCADE,
        related_name='drafts'
    )

    # Draft content
    to_emails = models.JSONField(default=list)
    cc_emails = models.JSONField(default=list, blank=True)
    bcc_emails = models.JSONField(default=list, blank=True)
    subject = models.CharField(max_length=1000, blank=True)
    body_text = models.TextField(blank=True)
    body_html = models.TextField(blank=True)
    attachments = models.JSONField(default=list, help_text="Pending attachments")

    # Reply context
    reply_to_message = models.ForeignKey(
        EmailMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='draft_replies',
        help_text="The message this draft is replying to"
    )
    is_reply_all = models.BooleanField(default=False)
    is_forward = models.BooleanField(default=False)

    # Ownership
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='email_drafts')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = "Email Draft"
        verbose_name_plural = "Email Drafts"

    def __str__(self):
        return f"Draft: {self.subject[:50] or 'No subject'}"


class TikTokShopAccount(models.Model):
    """Stores TikTok Shop seller account connection details for a tenant"""

    DEACTIVATION_REASONS = [
        ('expired_token', 'Access Token Expired'),
        ('revoked_access', 'User Revoked Access'),
        ('manual', 'Manually Disconnected'),
        ('api_error', 'API Error'),
    ]

    USER_TYPE_CHOICES = [
        (0, 'Seller'),
        (1, 'Creator'),
        (3, 'Partner'),
    ]

    # TikTok Shop identifiers
    open_id = models.CharField(
        max_length=255,
        unique=True,
        help_text="TikTok user unique ID (open_id)"
    )
    seller_name = models.CharField(max_length=200, blank=True, help_text="Seller name from token response")
    seller_base_region = models.CharField(max_length=10, blank=True, help_text="Seller base region (e.g. GB)")
    shop_id = models.CharField(max_length=255, blank=True, help_text="Shop ID from webhook payloads")
    shop_cipher = models.CharField(max_length=255, blank=True, help_text="Shop cipher required for API calls")
    user_type = models.IntegerField(choices=USER_TYPE_CHOICES, default=0, help_text="0=Seller, 1=Creator, 3=Partner")

    # OAuth tokens (encrypted using Django's Signer)
    access_token = models.TextField(help_text="Encrypted access token")
    refresh_token = models.TextField(help_text="Encrypted refresh token")
    token_expires_at = models.DateTimeField(help_text="When the access token expires")
    refresh_token_expires_at = models.DateTimeField(null=True, blank=True, help_text="When the refresh token expires")
    scope = models.TextField(blank=True, help_text="Granted OAuth scopes")

    # Status
    is_active = models.BooleanField(default=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivation_reason = models.CharField(
        max_length=50,
        choices=DEACTIVATION_REASONS,
        blank=True
    )
    deactivation_error = models.TextField(blank=True, help_text="Error details if deactivated due to error")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TikTok Shop Account"
        verbose_name_plural = "TikTok Shop Accounts"

    def __str__(self):
        return f"{self.seller_name or self.open_id} - TikTok Shop"

    def set_tokens(self, access_token, refresh_token):
        """Encrypt and store tokens using Django's Signer"""
        from django.core.signing import Signer
        signer = Signer()
        self.access_token = signer.sign(access_token)
        self.refresh_token = signer.sign(refresh_token)

    def get_access_token(self):
        """Decrypt and return the access token"""
        from django.core.signing import Signer, BadSignature
        signer = Signer()
        try:
            return signer.unsign(self.access_token)
        except BadSignature:
            return None

    def get_refresh_token(self):
        """Decrypt and return the refresh token"""
        from django.core.signing import Signer, BadSignature
        signer = Signer()
        try:
            return signer.unsign(self.refresh_token)
        except BadSignature:
            return None


class TikTokMessage(models.Model):
    """Stores TikTok Shop Customer Service Messages"""

    MESSAGE_TYPE_CHOICES = [
        ('TEXT', 'Text'),
        ('IMAGE', 'Image'),
        ('VIDEO', 'Video'),
        ('PRODUCT_CARD', 'Product Card'),
        ('ORDER_CARD', 'Order Card'),
        ('EMOTICONS', 'Emoticons'),
        ('COUPON_CARD', 'Coupon Card'),
        ('LOGISTICS_CARD', 'Logistics Card'),
        ('RETURN_REFUND_CARD', 'Return/Refund Card'),
        ('NOTIFICATION', 'Notification'),
        ('ALLOCATED_SERVICE', 'Allocated Service'),
        ('OTHER', 'Other'),
    ]

    SENDER_ROLE_CHOICES = [
        ('BUYER', 'Buyer'),
        ('CUSTOMER_SERVICE', 'Customer Service'),
        ('SHOP', 'Shop'),
        ('SYSTEM', 'System'),
        ('ROBOT', 'Robot'),
    ]

    shop_account = models.ForeignKey(
        TikTokShopAccount,
        on_delete=models.CASCADE,
        related_name='messages'
    )

    # Message identifiers
    message_id = models.CharField(max_length=255, unique=True, help_text="TikTok message ID")
    conversation_id = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Conversation ID for grouping messages"
    )
    index = models.CharField(max_length=255, blank=True, help_text="Message index for ordering")

    # Sender info
    sender_id = models.CharField(max_length=255, help_text="Sender identifier")
    sender_role = models.CharField(
        max_length=20,
        choices=SENDER_ROLE_CHOICES,
        blank=True,
        help_text="Role of the sender: BUYER, CUSTOMER_SERVICE, SHOP, SYSTEM, ROBOT"
    )
    sender_im_user_id = models.CharField(max_length=255, blank=True, help_text="Internal CS participant ID")
    buyer_user_id = models.CharField(max_length=255, blank=True, help_text="Buyer user ID for querying orders")

    # Content
    message_type = models.CharField(max_length=30, choices=MESSAGE_TYPE_CHOICES, default='TEXT')
    message_text = models.TextField(blank=True)
    media_url = models.URLField(max_length=1000, blank=True, null=True)
    media_mime_type = models.CharField(max_length=100, blank=True)
    attachments = models.JSONField(default=list, blank=True)

    # Metadata
    timestamp = models.DateTimeField(help_text="Message timestamp from TikTok")
    is_from_creator = models.BooleanField(default=False, help_text="True if sent by shop/customer service")

    # Status tracking
    is_delivered = models.BooleanField(default=False)
    delivered_at = models.DateTimeField(null=True, blank=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    is_read_by_staff = models.BooleanField(default=False)
    read_by_staff_at = models.DateTimeField(null=True, blank=True)

    # Error tracking for sent messages
    error_message = models.TextField(blank=True)

    # Soft delete fields
    is_deleted = models.BooleanField(default=False, help_text='Soft deleted by staff')
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deleted_tiktok_messages',
        help_text='Staff member who deleted this message'
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['shop_account', 'conversation_id']),
            models.Index(fields=['shop_account', 'timestamp']),
            models.Index(fields=['sender_id', 'timestamp']),
        ]
        verbose_name = "TikTok Message"
        verbose_name_plural = "TikTok Messages"

    def __str__(self):
        direction = "to" if self.is_from_creator else "from"
        sender = self.sender_role or self.sender_id
        if self.message_text:
            return f"TikTok Shop msg {direction} {sender} - {self.message_text[:50]}"
        elif self.message_type:
            return f"TikTok Shop msg {direction} {sender} - [{self.message_type}]"
        return f"TikTok Shop msg {direction} {sender}"


class EmailConnectionUserAssignment(models.Model):
    """Assigns specific email accounts to specific users."""
    connection = models.ForeignKey(
        EmailConnection,
        on_delete=models.CASCADE,
        related_name='user_assignments'
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='email_assignments'
    )
    assigned_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='email_assignments_created'
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('connection', 'user')
        verbose_name = "Email Connection User Assignment"
        verbose_name_plural = "Email Connection User Assignments"

    def __str__(self):
        return f"{self.user.email} -> {self.connection.email_address}"


class EmailSignature(models.Model):
    """Stores email signature configuration for a tenant - singleton per tenant"""

    # Sender name for the signature
    sender_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Name to display in the signature (e.g., 'John Doe' or 'Support Team')"
    )

    # Signature content
    signature_html = models.TextField(
        blank=True,
        help_text="HTML signature content"
    )
    signature_text = models.TextField(
        blank=True,
        help_text="Plain text signature content (fallback for non-HTML clients)"
    )

    # Settings
    is_enabled = models.BooleanField(
        default=True,
        help_text="Whether to append signature to outgoing emails"
    )
    include_on_reply = models.BooleanField(
        default=True,
        help_text="Whether to include signature when replying to emails"
    )

    # Timestamps and ownership
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_email_signatures',
        help_text="User who created/last updated the signature"
    )

    class Meta:
        verbose_name = "Email Signature"
        verbose_name_plural = "Email Signatures"

    def __str__(self):
        status = "enabled" if self.is_enabled else "disabled"
        return f"Email Signature ({status})"


class QuickReply(models.Model):
    """
    Stores quick reply messages that can be used across all messaging platforms.
    Supports variable placeholders like {{customer_name}}, {{order_number}}, etc.
    """

    PLATFORM_CHOICES = [
        ('all', 'All Platforms'),
        ('facebook', 'Facebook'),
        ('instagram', 'Instagram'),
        ('whatsapp', 'WhatsApp'),
        ('email', 'Email'),
        ('tiktok', 'TikTok'),
        ('widget', 'Website widget'),
        ('telegram', 'Telegram'),
    ]

    # Content
    title = models.CharField(
        max_length=100,
        help_text="Short title for identifying the quick reply"
    )
    message = models.TextField(
        help_text="Message content with optional variables like {{customer_name}}, {{order_number}}"
    )

    # Platform targeting (empty list means all platforms)
    platforms = models.JSONField(
        default=list,
        blank=True,
        help_text="List of platforms this quick reply is available on (empty = all platforms)"
    )

    # Optional metadata
    shortcut = models.CharField(
        max_length=20,
        blank=True,
        help_text="Optional shortcut command (e.g., /thanks, /hello)"
    )
    category = models.CharField(
        max_length=50,
        blank=True,
        help_text="Optional category for grouping (e.g., 'Greetings', 'Support', 'Orders')"
    )

    # Usage tracking
    use_count = models.IntegerField(
        default=0,
        help_text="Number of times this quick reply has been used"
    )

    # Ordering
    position = models.IntegerField(
        default=0,
        help_text="Order position for display (lower = higher priority)"
    )

    # Ownership
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_quick_replies',
        help_text="User who created this quick reply"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position', '-use_count', 'title']
        verbose_name = "Quick Reply"
        verbose_name_plural = "Quick Replies"

    def __str__(self):
        return f"{self.title}"

    @staticmethod
    def get_available_variables():
        """Returns list of available template variables with descriptions"""
        return [
            {'name': 'customer_name', 'description': 'Customer display name'},
            {'name': 'order_number', 'description': 'Most recent order number'},
            {'name': 'agent_name', 'description': 'Current agent/staff name'},
            {'name': 'company_name', 'description': 'Company/business name'},
            {'name': 'current_date', 'description': 'Current date (YYYY-MM-DD)'},
            {'name': 'current_time', 'description': 'Current time (HH:MM)'},
        ]


class Client(models.Model):
    """
    Unified client entity for social/chat contacts and booking clients.
    One client can have multiple social accounts across different platforms
    and can also be enabled for booking services.
    """
    # Basic info
    name = models.CharField(max_length=255, help_text="Client's display name")
    email = models.EmailField(blank=True, null=True, help_text="Optional email address")
    phone = models.CharField(max_length=50, blank=True, help_text="Optional phone number")
    notes = models.TextField(blank=True, help_text="Internal notes about this client")
    profile_picture = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        help_text="URL to profile picture (auto-populated from linked accounts)"
    )

    # Booking authentication fields
    first_name = models.CharField(max_length=100, blank=True, help_text="First name for booking")
    last_name = models.CharField(max_length=100, blank=True, help_text="Last name for booking")
    password_hash = models.CharField(max_length=255, blank=True, null=True, help_text="Hashed password for booking auth")
    is_booking_enabled = models.BooleanField(default=False, help_text="Whether this client can use booking system")

    # Email verification (for booking)
    is_verified = models.BooleanField(default=False, help_text="Email verified for booking")
    verification_token = models.CharField(max_length=100, blank=True, null=True)
    verification_sent_at = models.DateTimeField(blank=True, null=True)

    # Password reset (for booking)
    reset_token = models.CharField(max_length=100, blank=True, null=True)
    reset_token_expires = models.DateTimeField(blank=True, null=True)

    # Login tracking
    last_login = models.DateTimeField(blank=True, null=True)

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_clients'
    )

    class Meta:
        ordering = ['-updated_at']
        verbose_name = "Client"
        verbose_name_plural = "Clients"
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['email']),
            models.Index(fields=['phone']),
            models.Index(fields=['is_booking_enabled']),
        ]

    def __str__(self):
        return self.name

    @property
    def full_name(self):
        """Return full name for booking, or display name if not set"""
        if self.first_name or self.last_name:
            return f"{self.first_name} {self.last_name}".strip()
        return self.name

    def set_password(self, raw_password):
        """Hash and set password for booking authentication"""
        self.password_hash = make_password(raw_password)

    def check_password(self, raw_password):
        """Check password against hash"""
        if not self.password_hash:
            return False
        return check_password(raw_password, self.password_hash)

    def generate_verification_token(self):
        """Generate email verification token"""
        self.verification_token = secrets.token_urlsafe(32)
        self.verification_sent_at = timezone.now()
        return self.verification_token

    def generate_reset_token(self):
        """Generate password reset token (valid for 1 hour)"""
        self.reset_token = secrets.token_urlsafe(32)
        self.reset_token_expires = timezone.now() + timezone.timedelta(hours=1)
        return self.reset_token

    def verify_reset_token(self, token):
        """Check if reset token is valid"""
        if not self.reset_token or not self.reset_token_expires:
            return False
        if self.reset_token != token:
            return False
        if timezone.now() > self.reset_token_expires:
            return False
        return True


# Backwards compatibility alias
SocialClient = Client


class SocialClientCustomField(models.Model):
    """
    Tenant-configurable custom field definitions for social clients.
    Defines the schema for custom fields that can be added to clients.
    """
    FIELD_TYPE_CHOICES = [
        ('string', 'Single Line Text'),
        ('text', 'Multi-line Text'),
        ('number', 'Number'),
        ('date', 'Date'),
        ('boolean', 'Yes/No'),
        ('select', 'Dropdown Select'),
        ('multiselect', 'Multi-select'),
    ]

    name = models.CharField(max_length=100, help_text="Internal field name (snake_case)")
    label = models.CharField(max_length=255, help_text="Display label for the field")
    field_type = models.CharField(max_length=20, choices=FIELD_TYPE_CHOICES, default='string')
    is_required = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0, help_text="Display order")

    # For select/multiselect types - JSON array of options
    options = models.JSONField(
        default=list,
        blank=True,
        help_text="Options for select/multiselect fields (e.g., ['Option 1', 'Option 2'])"
    )

    # Default value
    default_value = models.CharField(max_length=500, blank=True, help_text="Default value for new clients")

    # Metadata
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_social_client_fields'
    )

    class Meta:
        ordering = ['position', 'created_at']
        verbose_name = "Social Client Custom Field"
        verbose_name_plural = "Social Client Custom Fields"
        constraints = [
            models.UniqueConstraint(fields=['name'], name='unique_social_client_field_name')
        ]

    def __str__(self):
        return f"{self.label} ({self.field_type})"


class SocialClientCustomFieldValue(models.Model):
    """
    Stores the actual custom field values for each client.
    Each client can have multiple field values.
    """
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name='custom_field_values'
    )
    field = models.ForeignKey(
        SocialClientCustomField,
        on_delete=models.CASCADE,
        related_name='values'
    )
    value = models.TextField(blank=True, help_text="Value stored as string (JSON for complex types)")

    class Meta:
        unique_together = [['client', 'field']]
        verbose_name = "Social Client Custom Field Value"
        verbose_name_plural = "Social Client Custom Field Values"

    def __str__(self):
        return f"{self.client.name} - {self.field.label}: {self.value[:50] if self.value else ''}"


class SocialAccount(models.Model):
    """
    Links a Client to a specific social platform account.
    One client can have multiple accounts across different platforms.
    """
    PLATFORM_CHOICES = [
        ('facebook', 'Facebook'),
        ('instagram', 'Instagram'),
        ('whatsapp', 'WhatsApp'),
        ('email', 'Email'),
        ('tiktok', 'TikTok'),
        ('widget', 'Website widget'),
        ('telegram', 'Telegram'),
    ]

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name='social_accounts'
    )

    # Platform identification
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    platform_id = models.CharField(
        max_length=255,
        help_text="Platform-specific ID (sender_id, wa_id, email address, etc.)"
    )

    # Connection reference - links to the tenant's platform connection
    account_connection_id = models.CharField(
        max_length=255,
        help_text="ID of the tenant's account connection (page_id for FB, account_id for IG, waba_id for WhatsApp)"
    )

    # Display info (cached from messages)
    display_name = models.CharField(max_length=255, blank=True)
    username = models.CharField(
        max_length=255,
        blank=True,
        help_text="Username if available (e.g., @instagram_handle)"
    )
    profile_pic_url = models.URLField(max_length=500, blank=True, null=True)

    # Metadata
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    last_message_at = models.DateTimeField(null=True, blank=True)

    # Auto-created vs manually linked
    is_auto_created = models.BooleanField(
        default=True,
        help_text="True if auto-created from incoming message, False if manually linked"
    )

    class Meta:
        ordering = ['-last_message_at']
        verbose_name = "Social Account"
        verbose_name_plural = "Social Accounts"
        unique_together = [['platform', 'platform_id', 'account_connection_id']]
        indexes = [
            models.Index(fields=['platform', 'platform_id']),
            models.Index(fields=['client', 'platform']),
        ]

    def __str__(self):
        return f"{self.client.name} - {self.platform}: {self.display_name or self.platform_id}"


class CachedProfilePicture(models.Model):
    """
    Local cache of customer profile pictures fetched from social platforms.

    One row per (platform, platform_id). The image bytes are copied to our
    own storage so conversations never lose avatars when Meta's CDN URLs
    expire, and so webhooks don't spend Graph API rate budget re-fetching
    the same picture on every incoming message.

    An empty image_url with a recent fetched_at is a negative cache entry:
    the platform reported no visible picture, so don't ask again until the
    entry goes stale.
    """
    platform = models.CharField(max_length=20)
    platform_id = models.CharField(max_length=255)
    image_url = models.URLField(max_length=500, blank=True)  # URL on our storage
    storage_path = models.CharField(max_length=500, blank=True)  # path in default_storage
    display_name = models.CharField(max_length=255, blank=True)  # cached IG name
    username = models.CharField(max_length=255, blank=True)  # cached IG username
    fetched_at = models.DateTimeField(null=True, blank=True)  # null = never fetched
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [['platform', 'platform_id']]
        verbose_name = "Cached Profile Picture"
        verbose_name_plural = "Cached Profile Pictures"

    def __str__(self):
        return f"{self.platform}:{self.platform_id}"


class TelegramAccount(models.Model):
    """A tenant's connected Telegram account (personal/business, via MTProto).

    The session_string is a full account credential (bypasses login + 2FA),
    so it is stored with real Fernet encryption — never the Signer-based
    pseudo-encryption used for legacy token fields in this app.
    """

    DEACTIVATION_REASONS = [
        ('manual', 'Manually Disconnected'),
        ('session_revoked', 'Session Revoked'),
        ('flood_wait', 'Flood Wait'),
        ('auth_error', 'Authentication Error'),
        ('api_error', 'API Error'),
    ]

    telegram_user_id = models.BigIntegerField(unique=True, help_text="The account's own Telegram user id")
    phone_number = models.CharField(max_length=20)
    first_name = models.CharField(max_length=128, blank=True)
    last_name = models.CharField(max_length=128, blank=True)
    username = models.CharField(max_length=128, blank=True)
    profile_pic_url = models.URLField(max_length=500, blank=True, null=True)

    # MTProto StringSession, Fernet-encrypted at rest.
    session_string = EncryptedCharField()

    is_active = models.BooleanField(default=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivation_reason = models.CharField(
        max_length=50, choices=DEACTIVATION_REASONS, null=True, blank=True,
    )
    deactivation_error = models.TextField(blank=True)

    # Worker health (mirrors EmailConnection's auto-disable machinery)
    last_seen_at = models.DateTimeField(null=True, blank=True, help_text='Last worker heartbeat for this account')
    last_sync_error = models.TextField(blank=True)
    auto_disabled_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Set when the system (not a human) disabled the account; requires re-login',
    )
    failure_count = models.PositiveIntegerField(default=0)

    connected_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='connected_telegram_accounts',
        help_text='EchoDesk user who performed the login',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Telegram Account"
        verbose_name_plural = "Telegram Accounts"

    def __str__(self):
        label = self.username or self.first_name or self.phone_number
        return f"Telegram @{label} ({self.telegram_user_id})"


class TelegramMessage(models.Model):
    """Telegram DM, mirroring WhatsAppMessage's field set.

    conversation key = peer_id (the customer's Telegram user id).
    message_id is the composite '{telegram_user_id}_{telegram_msg_id}'
    because Telegram message ids are only sequential per account.
    """

    MESSAGE_TYPE_CHOICES = [
        ('text', 'Text'),
        ('image', 'Image'),
        ('video', 'Video'),
        ('audio', 'Audio'),
        ('voice', 'Voice'),
        ('document', 'Document'),
        ('sticker', 'Sticker'),
    ]

    STATUS_CHOICES = [
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('read', 'Read'),
        ('failed', 'Failed'),
    ]

    SOURCE_CHOICES = [
        ('echodesk', 'EchoDesk'),
        ('telegram_app', 'Telegram App'),
        ('synced', 'Synced from History'),
    ]

    account = models.ForeignKey(TelegramAccount, on_delete=models.CASCADE, related_name='messages')
    message_id = models.CharField(max_length=100, unique=True)
    telegram_msg_id = models.BigIntegerField(db_index=True, help_text='Raw Telegram message id (per-account sequential)')

    peer_id = models.BigIntegerField(db_index=True, help_text="Customer's Telegram user id — the conversation key")
    peer_access_hash = models.BigIntegerField(
        null=True, blank=True,
        help_text='Telegram access_hash for the peer; required to send after worker restarts',
    )
    peer_name = models.CharField(max_length=255, blank=True)
    peer_username = models.CharField(max_length=128, blank=True)
    profile_pic_url = models.URLField(max_length=500, blank=True, null=True)

    message_text = models.TextField(blank=True)
    message_type = models.CharField(max_length=20, choices=MESSAGE_TYPE_CHOICES, default='text')
    media_url = models.URLField(max_length=1000, blank=True, null=True)
    media_mime_type = models.CharField(max_length=100, blank=True)
    attachments = models.JSONField(default=list, blank=True)

    timestamp = models.DateTimeField()
    is_from_business = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='sent')
    is_delivered = models.BooleanField(default=False)
    delivered_at = models.DateTimeField(null=True, blank=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    is_read_by_staff = models.BooleanField(default=False)
    read_by_staff_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default='telegram_app',
        help_text='echodesk = sent from EchoDesk; telegram_app = echo of a message the owner sent from a Telegram client',
    )
    is_echo = models.BooleanField(default=False)
    sent_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sent_telegram_messages',
    )

    is_edited = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)
    original_text = models.TextField(blank=True, null=True)
    is_revoked = models.BooleanField(default=False)
    revoked_at = models.DateTimeField(null=True, blank=True)

    reaction_emoji = models.CharField(max_length=10, blank=True, null=True)
    reacted_by = models.CharField(max_length=50, blank=True, null=True)
    reacted_at = models.DateTimeField(null=True, blank=True)

    reply_to_message_id = models.CharField(max_length=100, blank=True, null=True)
    reply_to = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='replies',
    )

    is_deleted = models.BooleanField(default=False, help_text='Soft deleted by staff')
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='deleted_telegram_messages',
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['account', 'timestamp']),
            # Supports DISTINCT ON (peer_id) ORDER BY peer_id, -timestamp used
            # by unified_conversations for the latest message per conversation.
            models.Index(fields=['account', 'peer_id', '-timestamp']),
        ]

    def __str__(self):
        direction = "to" if self.is_from_business else "from"
        contact = self.peer_name or self.peer_username or self.peer_id
        return f"Telegram {direction} {contact} - {self.message_text[:50]}"


class ConversationArchive(models.Model):
    """
    Tracks archived conversations across all platforms.
    When a conversation is archived, it's hidden from the main list
    and shown in the history/archive tab.
    """
    PLATFORM_CHOICES = [
        ('facebook', 'Facebook'),
        ('instagram', 'Instagram'),
        ('whatsapp', 'WhatsApp'),
        ('email', 'Email'),
        ('tiktok', 'TikTok'),
        ('widget', 'Website widget'),
        ('telegram', 'Telegram'),
    ]

    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    conversation_id = models.CharField(
        max_length=255,
        help_text="Platform-specific conversation ID (sender_id, from_number, thread_id)"
    )
    account_id = models.CharField(
        max_length=255,
        help_text="Account connection ID (page_id, instagram_account_id, waba_id, connection_id)"
    )

    # Archive metadata
    archived_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='archived_conversations'
    )
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-archived_at']
        verbose_name = "Conversation Archive"
        verbose_name_plural = "Conversation Archives"
        unique_together = [['platform', 'conversation_id', 'account_id']]
        indexes = [
            models.Index(fields=['platform', 'account_id']),
            models.Index(fields=['archived_at']),
        ]

    def __str__(self):
        return f"{self.platform} - {self.conversation_id} (archived {self.archived_at})"


class AutoPostSettings(models.Model):
    """Singleton per tenant — AI auto-posting configuration"""

    TONE_CHOICES = [
        ('professional', 'Professional'),
        ('casual', 'Casual'),
        ('friendly', 'Friendly'),
        ('promotional', 'Promotional'),
        ('informative', 'Informative'),
    ]

    CONTENT_SOURCE_CHOICES = [
        ('products', 'Products'),
        ('company', 'Company Info'),
        ('both', 'Both'),
    ]

    is_enabled = models.BooleanField(default=False, help_text='Master toggle for auto-posting')
    company_description = models.TextField(blank=True, default='', help_text='AI context about the company')
    posting_time = models.TimeField(default='10:00', help_text='Daily posting time')
    timezone = models.CharField(max_length=50, default='UTC', help_text='Timezone for schedule calculation')
    post_to_facebook = models.BooleanField(default=True)
    post_to_instagram = models.BooleanField(default=True)
    tone = models.CharField(max_length=20, choices=TONE_CHOICES, default='professional')
    content_source = models.CharField(max_length=20, choices=CONTENT_SOURCE_CHOICES, default='both')
    content_language = models.CharField(max_length=10, default='en', help_text='Language for generated content')
    require_approval = models.BooleanField(default=True, help_text='If True, posts need manual approval before publishing')
    max_posts_per_day = models.IntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(10)])

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Auto Post Settings"
        verbose_name_plural = "Auto Post Settings"

    def __str__(self):
        return f"AutoPostSettings (enabled={self.is_enabled})"


class AutoPostContent(models.Model):
    """Post queue and history for AI-generated social media posts"""

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('published', 'Published'),
        ('failed', 'Failed'),
        ('rejected', 'Rejected'),
    ]

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    facebook_text = models.TextField(blank=True, default='', help_text='Post text for Facebook')
    instagram_text = models.TextField(blank=True, default='', help_text='Post text for Instagram (with hashtags)')
    image_url = models.URLField(max_length=2000, blank=True, null=True, help_text='Product image or AI-generated image URL')
    featured_product = models.ForeignKey(
        'ecommerce_crm.Product',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='auto_posts',
        help_text='Which product was featured'
    )

    # Targeting
    target_facebook = models.BooleanField(default=True)
    target_instagram = models.BooleanField(default=True)

    # Scheduling
    scheduled_for = models.DateTimeField(help_text='When to publish')

    # Publishing results
    facebook_post_id = models.CharField(max_length=255, blank=True, null=True)
    instagram_media_id = models.CharField(max_length=255, blank=True, null=True)
    published_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default='')

    # AI metadata
    ai_model_used = models.CharField(max_length=50, blank=True, default='')

    # Approval tracking
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_auto_posts'
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    # Rejection tracking
    rejected_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rejected_auto_posts'
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Auto Post Content"
        verbose_name_plural = "Auto Post Contents"
        indexes = [
            models.Index(fields=['status', 'scheduled_for']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        product_name = self.featured_product.name if self.featured_product else 'Company'
        return f"AutoPost [{self.status}] - {product_name} ({self.scheduled_for})"


class WidgetSession(models.Model):
    """A single website visitor's chat session with the tenant."""
    # No FK to WidgetConnection because that model lives in the public schema
    # and this one lives in each tenant schema — cross-schema FKs aren't
    # possible. Store the WidgetConnection.id by value.
    connection_id = models.IntegerField(db_index=True)
    session_id = models.CharField(max_length=64, unique=True, db_index=True)
    visitor_id = models.CharField(max_length=64, db_index=True)
    visitor_name = models.CharField(max_length=120, blank=True)
    visitor_email = models.EmailField(blank=True)
    referrer_url = models.CharField(max_length=500, blank=True)
    page_url = models.CharField(max_length=500, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.CharField(
        max_length=16,
        choices=[
            ('visitor', 'visitor'),
            ('agent', 'agent'),
            ('timeout', 'timeout'),
        ],
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['connection_id', '-started_at']),
            models.Index(fields=['connection_id', 'visitor_id']),
        ]

    def __str__(self):
        return f"WidgetSession({self.session_id}) visitor={self.visitor_name or self.visitor_id}"

    @property
    def is_ended(self):
        return self.ended_at is not None


class WidgetMessage(models.Model):
    session = models.ForeignKey(
        WidgetSession, on_delete=models.CASCADE, related_name='messages'
    )
    message_id = models.CharField(max_length=64, unique=True)
    message_text = models.TextField(blank=True)
    attachments = models.JSONField(default=list)
    is_from_visitor = models.BooleanField(default=True, db_index=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='widget_messages_sent',
    )
    is_delivered = models.BooleanField(default=False)
    delivered_at = models.DateTimeField(null=True, blank=True)
    is_read_by_visitor = models.BooleanField(default=False)
    is_read_by_staff = models.BooleanField(default=False, db_index=True)
    read_by_staff_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    timestamp = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp']
        indexes = [
            models.Index(fields=['session', '-timestamp']),
        ]

    def __str__(self):
        direction = 'visitor' if self.is_from_visitor else 'agent'
        return f"WidgetMessage({self.message_id}) from={direction}"


# ============================================================================
# AI COMPANION
# ============================================================================

class AICompanionSettings(models.Model):
    """
    Tenant-wide AI companion configuration (singleton, like AutoPostSettings).

    Per-channel enablement lives in AICompanionChannel; this holds the
    provider choice, key override, default guidance, and safety caps.
    """
    PROVIDER_CHOICES = [
        ('anthropic', 'Anthropic (Claude)'),
        ('openai', 'OpenAI'),
    ]

    is_enabled = models.BooleanField(
        default=False, help_text='Master toggle for all AI companion behavior'
    )
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES, default='anthropic')
    model = models.CharField(
        max_length=80, blank=True, default='',
        help_text='Model id; empty = provider default',
    )
    api_key = EncryptedCharField(
        blank=True, default='',
        help_text='Tenant API key override; empty = platform default key',
    )
    guidance_prompt = models.TextField(
        blank=True, default='',
        help_text='Tenant instructions for how the AI should answer customers',
    )
    escalation_instructions = models.TextField(
        blank=True, default='',
        help_text='Extra instructions for when the AI should hand off to a human',
    )
    language = models.CharField(
        max_length=10, default='ka',
        help_text='Default reply language code (customer language still wins)',
    )
    max_replies_per_conversation_per_day = models.PositiveIntegerField(default=30)
    max_replies_per_day = models.PositiveIntegerField(
        default=500, help_text='Tenant-wide daily AI reply cap'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'AI Companion Settings'
        verbose_name_plural = 'AI Companion Settings'

    def __str__(self):
        return f"AICompanionSettings(enabled={self.is_enabled}, provider={self.provider})"


class AICompanionChannel(models.Model):
    """
    Enables the AI companion for one platform account.
    account_id='' means "every account of this platform".
    """
    platform = models.CharField(max_length=20)
    account_id = models.CharField(max_length=255, blank=True, default='')
    enabled = models.BooleanField(default=False)
    guidance_prompt = models.TextField(
        blank=True, default='',
        help_text='Channel-specific guidance; empty = inherit tenant default',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [['platform', 'account_id']]
        verbose_name = 'AI Companion Channel'
        verbose_name_plural = 'AI Companion Channels'

    def __str__(self):
        scope = self.account_id or 'all accounts'
        return f"AICompanionChannel({self.platform}/{scope}, enabled={self.enabled})"


class AIConversationState(models.Model):
    """
    Per-conversation AI mode. Absence of a row means mode 'ai'.

    After a handoff (needs_human) or a manual pause (off) the AI never
    resumes by itself — an agent must switch the mode back to 'ai'.
    """
    MODE_CHOICES = [
        ('ai', 'AI Active'),
        ('needs_human', 'Needs Human'),
        ('off', 'Paused'),
    ]

    platform = models.CharField(max_length=20)
    conversation_id = models.CharField(max_length=255)
    account_id = models.CharField(max_length=255)
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default='ai')
    reason = models.TextField(
        blank=True, default='',
        help_text='Why the AI escalated (includes call requests)',
    )
    total_ai_replies = models.PositiveIntegerField(default=0)
    daily_reply_count = models.PositiveIntegerField(default=0)
    daily_count_date = models.DateField(null=True, blank=True)
    last_ai_reply_at = models.DateTimeField(null=True, blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [['platform', 'conversation_id', 'account_id']]
        indexes = [
            models.Index(fields=['platform', 'account_id', 'mode']),
        ]
        verbose_name = 'AI Conversation State'
        verbose_name_plural = 'AI Conversation States'

    def __str__(self):
        return f"AIConversationState({self.platform}/{self.conversation_id}: {self.mode})"


class ConversationSummary(models.Model):
    """An AI-generated summary of one conversation, generated on demand."""
    platform = models.CharField(max_length=20)
    account_id = models.CharField(max_length=255)
    conversation_id = models.CharField(max_length=255)
    summary_text = models.TextField()
    provider = models.CharField(max_length=20, blank=True, default='')
    model = models.CharField(max_length=80, blank=True, default='')
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveIntegerField(null=True, blank=True)
    requested_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='conversation_summaries',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['platform', 'account_id', 'conversation_id', '-created_at']),
        ]
        verbose_name = 'Conversation Summary'
        verbose_name_plural = 'Conversation Summaries'

    def __str__(self):
        return f"ConversationSummary({self.platform}/{self.conversation_id} @ {self.created_at:%Y-%m-%d %H:%M})"


class AICompanionRun(models.Model):
    """
    Audit row for every AI provider invocation (reply decision, summary,
    settings test) — success or failure. Doubles as the tenant daily-cap
    counter and the future billing source.
    """
    KIND_CHOICES = [
        ('reply', 'Reply Decision'),
        ('summary', 'Summary'),
        ('test', 'Settings Test'),
    ]

    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    platform = models.CharField(max_length=20, blank=True, default='')
    account_id = models.CharField(max_length=255, blank=True, default='')
    conversation_id = models.CharField(max_length=255, blank=True, default='')
    provider = models.CharField(max_length=20, blank=True, default='')
    model = models.CharField(max_length=80, blank=True, default='')
    action = models.CharField(
        max_length=30, blank=True, default='',
        help_text='Decision outcome for reply runs: reply|handoff|ignore',
    )
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveIntegerField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    success = models.BooleanField(default=False)
    error_message = models.TextField(blank=True, default='')
    raw_response = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['-started_at']),
            models.Index(fields=['kind', 'started_at']),
        ]
        verbose_name = 'AI Companion Run'
        verbose_name_plural = 'AI Companion Runs'

    def __str__(self):
        return f"AICompanionRun({self.kind}, success={self.success}, {self.started_at:%Y-%m-%d %H:%M})"