from django.db import models
from django.conf import settings
import uuid

from amanati_crm.file_utils import SanitizedUploadTo
from crm.fields import EncryptedCharField


class SipConfiguration(models.Model):
    """SIP configuration for tenant-specific calling"""
    
    name = models.CharField(max_length=100, help_text="Configuration name")
    sip_server = models.CharField(max_length=255, help_text="SIP server hostname/IP")
    sip_port = models.IntegerField(default=5060, help_text="SIP server port")
    username = models.CharField(max_length=100, help_text="SIP username")
    password = EncryptedCharField(help_text="SIP password (encrypted at rest)")
    realm = models.CharField(max_length=255, blank=True, help_text="SIP realm/domain")
    proxy = models.CharField(max_length=255, blank=True, help_text="Outbound proxy")
    phone_number = models.CharField(max_length=30, blank=True, help_text="Trunk phone number (e.g., +995322421219)")

    # WebRTC/STUN/TURN settings
    stun_server = models.CharField(
        max_length=255, 
        blank=True, 
        default="stun:stun.l.google.com:19302",
        help_text="STUN server for NAT traversal"
    )
    websocket_path = models.CharField(
        max_length=255,
        blank=True,
        default="/ws",
        help_text="WebSocket path on the SIP server (e.g., /ws)"
    )
    turn_server = models.CharField(max_length=255, blank=True, help_text="TURN server")
    turn_username = models.CharField(max_length=100, blank=True)
    turn_password = EncryptedCharField(blank=True, default='', help_text="Static TURN credential (encrypted at rest)")
    turn_auth_secret = EncryptedCharField(
        blank=True, default='',
        help_text="coturn static-auth-secret; when set the softphone gets ephemeral TURN credentials instead of turn_password",
    )
    
    # Settings
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    max_concurrent_calls = models.IntegerField(default=5)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_sip_configs'
    )
    
    class Meta:
        ordering = ['-is_default', 'name']
        
    def __str__(self):
        return f"{self.name} ({self.sip_server})"
    
    def save(self, *args, **kwargs):
        # Ensure only one default configuration per tenant
        if self.is_default:
            SipConfiguration.objects.filter(is_default=True).exclude(id=self.id).update(is_default=False)
        super().save(*args, **kwargs)


class UserPhoneAssignment(models.Model):
    """Assigns a PBX extension + phone number to a specific user"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='phone_assignments',
        help_text="User assigned to this phone number"
    )
    sip_configuration = models.ForeignKey(
        SipConfiguration,
        on_delete=models.CASCADE,
        related_name='user_assignments',
        help_text="The SIP trunk/server this extension belongs to"
    )
    extension = models.CharField(max_length=20, help_text="PBX extension number (e.g., 100)")
    extension_password = EncryptedCharField(help_text="PBX extension password (encrypted at rest)")
    phone_number = models.CharField(max_length=30, help_text="Display phone number (e.g., +995322421219)")
    display_name = models.CharField(max_length=100, blank=True, help_text="Caller ID display name")
    is_primary = models.BooleanField(default=True, help_text="Primary number for this user")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_primary', 'extension']
        unique_together = [
            ('sip_configuration', 'extension'),  # Each extension unique per SIP config
        ]

    def __str__(self):
        return f"{self.user} - ext {self.extension} ({self.phone_number})"

    def save(self, *args, **kwargs):
        # Ensure only one primary per user
        if self.is_primary:
            UserPhoneAssignment.objects.filter(
                user=self.user, is_primary=True
            ).exclude(id=self.id).update(is_primary=False)
        super().save(*args, **kwargs)


class Client(models.Model):
    """Client/Customer model"""
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True)
    company = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.name} ({self.email})"


class CallLog(models.Model):
    """Enhanced call log model for tracking phone calls with SIP integration"""
    
    STATUS_CHOICES = [
        ('initiated', 'Initiated'),
        ('ringing', 'Ringing'),
        ('answered', 'Answered'),
        ('missed', 'Missed'), 
        ('busy', 'Busy'),
        ('no_answer', 'No Answer'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
        ('transferred', 'Transferred'),
        ('ended', 'Ended'),
        ('recording', 'Recording'),
        ('on_hold', 'On Hold'),
    ]
    
    DIRECTION_CHOICES = [
        ('inbound', 'Inbound'),
        ('outbound', 'Outbound'),
    ]
    
    CALL_TYPE_CHOICES = [
        ('voice', 'Voice Call'),
        ('video', 'Video Call'),
        ('conference', 'Conference Call'),
    ]
    
    # Basic call information
    call_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    caller_number = models.CharField(max_length=64)
    recipient_number = models.CharField(max_length=64)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, default='inbound')
    call_type = models.CharField(max_length=15, choices=CALL_TYPE_CHOICES, default='voice')
    
    # Call timing
    started_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration = models.DurationField(null=True, blank=True)
    
    # Call status and metadata
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ringing')
    notes = models.TextField(blank=True)
    
    # SIP-specific fields
    sip_call_id = models.CharField(max_length=255, blank=True, help_text="SIP Call-ID header")
    pbx_call_id = models.CharField(
        max_length=64, blank=True, default='', db_index=True,
        help_text="Asterisk LINKEDID shared by every leg of one call (from the X-EchoDesk-Call header)",
    )
    sip_configuration = models.ForeignKey(
        SipConfiguration, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        help_text="SIP configuration used for this call"
    )
    
    # User and client relationships
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='handled_calls'
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='calls',
        help_text="Legacy CRM client (deprecated)"
    )
    social_client = models.ForeignKey(
        'social_integrations.Client',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='calls',
        db_constraint=False,
        help_text="Associated social client (auto-detected by phone number)"
    )
    
    # Transfer tracking
    transferred_to = models.CharField(max_length=30, blank=True, help_text="Number/extension the call was transferred to")
    transferred_to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='received_transfers',
        help_text="User the call was transferred to"
    )
    transferred_at = models.DateTimeField(null=True, blank=True)
    parent_call = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='consultation_calls',
        help_text="Original call this consultation was created for"
    )
    transfer_type = models.CharField(
        max_length=10, blank=True, default='',
        choices=[('', 'None'), ('blind', 'Blind'), ('attended', 'Attended')],
        help_text="Type of transfer"
    )

    # Recording and quality
    recording_url = models.URLField(blank=True, help_text="Call recording file URL")
    call_quality_score = models.FloatField(null=True, blank=True, help_text="Call quality (0-5)")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['caller_number']),
            models.Index(fields=['recipient_number']),
            models.Index(fields=['status']),
            models.Index(fields=['direction']),
            models.Index(fields=['started_at']),
        ]
    
    def __str__(self):
        direction_symbol = "→" if self.direction == "outbound" else "←"
        return f"{self.caller_number} {direction_symbol} {self.recipient_number} ({self.status})"
    
    # A phone number needs at least this many digits before it is matched
    # against clients; shorter values (extensions, "anonymous") never are.
    CLIENT_MATCH_MIN_DIGITS = 7

    def _should_match_client(self, update_fields):
        if self.client_id or self.social_client_id:
            return False
        if update_fields is not None and 'client' not in update_fields:
            # The caller is writing other columns; the lookup would cost a
            # query whose result could not even be saved.
            return False
        return True

    def _match_client(self):
        """The CRM client whose phone ends with the other party's number
        (last 7 digits), or ``None``."""
        number = self.caller_number if self.direction == 'inbound' else self.recipient_number
        digits = ''.join(ch for ch in (number or '') if ch.isdigit())
        if len(digits) < self.CLIENT_MATCH_MIN_DIGITS:
            return None
        return Client.objects.filter(phone__endswith=digits[-7:]).order_by('id').first()

    def save(self, *args, **kwargs):
        # Auto-associate with a CRM client by phone number, unless a
        # client (CRM or social) is already set or this save does not
        # write ``client`` at all.
        update_fields = kwargs.get('update_fields')
        if self._should_match_client(update_fields):
            try:
                matched = self._match_client()
            except Exception:  # noqa: BLE001 - never block a call log write
                matched = None
            if matched is not None:
                self.client = matched
        super().save(*args, **kwargs)


class CallEvent(models.Model):
    """Track detailed events during a call"""
    
    EVENT_TYPES = [
        ('initiated', 'Call Initiated'),
        ('ringing', 'Ringing Started'),
        ('answered', 'Call Answered'),
        ('hold', 'Call On Hold'),
        ('unhold', 'Call Resumed'),
        ('transfer_initiated', 'Transfer Initiated'),
        ('transfer_completed', 'Transfer Completed'),
        ('recording_started', 'Recording Started'),
        ('recording_stopped', 'Recording Stopped'),
        ('muted', 'Call Muted'),
        ('unmuted', 'Call Unmuted'),
        ('dtmf', 'DTMF Pressed'),
        ('quality_change', 'Call Quality Changed'),
        ('ended', 'Call Ended'),
        ('failed', 'Call Failed'),
        ('error', 'Error Occurred'),
        ('conference_started', 'Conference Started'),
    ]
    
    call_log = models.ForeignKey(CallLog, on_delete=models.CASCADE, related_name='events')
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)
    timestamp = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(blank=True, default=dict, help_text="Additional event data")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        help_text="User who triggered this event"
    )
    
    class Meta:
        ordering = ['timestamp']
        indexes = [
            models.Index(fields=['call_log', 'event_type']),
            models.Index(fields=['timestamp']),
        ]
    
    def __str__(self):
        return f"{self.call_log.call_id} - {self.event_type} at {self.timestamp}"


class CallRecording(models.Model):
    """Track call recordings separately for better management"""
    
    RECORDING_STATUS = [
        ('pending', 'Pending'),
        ('recording', 'Recording'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('deleted', 'Deleted'),
    ]
    
    call_log = models.OneToOneField(CallLog, on_delete=models.CASCADE, related_name='recording')
    recording_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    file_path = models.CharField(max_length=500, blank=True, help_text="Local file path")
    file_url = models.URLField(blank=True, help_text="External URL for recording")
    file_size = models.BigIntegerField(null=True, blank=True, help_text="File size in bytes")
    duration = models.DurationField(null=True, blank=True, help_text="Recording duration")
    format = models.CharField(max_length=10, default='wav', help_text="Audio format")
    status = models.CharField(max_length=15, choices=RECORDING_STATUS, default='pending')
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    # Transcription
    transcript = models.TextField(blank=True, help_text="Call transcript")
    transcript_confidence = models.FloatField(null=True, blank=True, help_text="Transcript confidence (0-1)")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Recording for {self.call_log.call_id} ({self.status})"


class PbxSettings(models.Model):
    """PBX working hours and sound management per SIP configuration."""

    AFTER_HOURS_ACTIONS = [
        ('announcement', 'Play announcement'),
        ('voicemail', 'Voicemail'),
        ('forward', 'Forward to number'),
    ]

    sip_configuration = models.OneToOneField(
        SipConfiguration, on_delete=models.CASCADE, related_name='pbx_settings'
    )

    # Working hours
    working_hours_enabled = models.BooleanField(
        default=False, help_text="Enforce working hours for incoming calls"
    )
    working_hours_schedule = models.JSONField(
        default=dict, blank=True,
        help_text='Hours when business is OPEN. Format: {"monday": [9,10,...,17], ...}'
    )
    timezone = models.CharField(
        max_length=50, default='Asia/Tbilisi', help_text="Business timezone"
    )

    # Holidays (override working hours as closed)
    holidays = models.JSONField(
        default=list, blank=True,
        help_text='List of holiday objects: [{"date": "2026-01-01", "name": "New Year"}, ...]'
    )

    # After-hours behaviour
    after_hours_action = models.CharField(
        max_length=20, choices=AFTER_HOURS_ACTIONS, default='announcement'
    )
    forward_number = models.CharField(
        max_length=30, blank=True, default='', help_text="Forward-to number"
    )
    voicemail_enabled = models.BooleanField(
        default=False, help_text="Enable voicemail recording after hours"
    )

    # Sound files (uploaded to DO Spaces)
    sound_greeting = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Welcome greeting during working hours"
    )
    sound_after_hours = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="After-hours announcement"
    )
    sound_queue_hold = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Queue hold music"
    )
    sound_voicemail_prompt = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Voicemail prompt"
    )
    sound_thank_you = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Post-rating thank you"
    )
    sound_transfer_hold = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Transfer hold music"
    )
    sound_review_prompt = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Post-call review prompt (rate 1-5)"
    )
    sound_review_invalid = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Invalid review input prompt"
    )
    sound_review_thanks = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Review thank you message"
    )
    sound_queue_position_1 = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Sound for queue position 1"
    )
    sound_queue_position_2 = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Sound for queue position 2"
    )
    sound_queue_position_3 = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Sound for queue position 3"
    )
    sound_queue_position_4 = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Sound for queue position 4"
    )
    sound_queue_position_5 = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Sound for queue position 5"
    )
    sound_queue_position_6 = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Sound for queue position 6"
    )
    sound_queue_position_7 = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Sound for queue position 7"
    )
    sound_queue_position_8 = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Sound for queue position 8"
    )
    sound_queue_position_9 = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Sound for queue position 9"
    )
    sound_queue_position_10 = models.FileField(
        upload_to=SanitizedUploadTo('pbx_sounds', date_based=False),
        blank=True, null=True, help_text="Sound for queue position 10"
    )

    # Review settings
    review_method = models.CharField(
        max_length=10, default='callback',
        choices=[('callback', 'Phone Callback'), ('sms', 'SMS Link'), ('both', 'Both'), ('none', 'Disabled')],
        help_text="How to collect post-call ratings"
    )
    sms_api_key = EncryptedCharField(blank=True, default='', help_text="sender.ge API key (encrypted at rest)")
    sms_rating_template_ka = models.TextField(
        blank=True,
        default='გმადლობთ დაკავშირებისთვის! გთხოვთ შეაფასეთ ჩვენი მომსახურება: {link}',
        help_text="SMS template Georgian with {link} placeholder"
    )
    sms_rating_template_en = models.TextField(
        blank=True,
        default='Thank you for calling! Please rate our service: {link}',
        help_text="SMS template English with {link} placeholder"
    )
    review_delay_hours = models.IntegerField(default=3, help_text="Hours to wait after last call before review request")
    review_cooldown_hours = models.IntegerField(default=24, help_text="Hours between review requests per caller")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "PBX Settings"
        verbose_name_plural = "PBX Settings"

    def __str__(self):
        return f"PBX Settings for {self.sip_configuration.name}"

    def is_working_hours_now(self):
        """Check if current time falls within working hours (holidays override as closed)."""
        if not self.working_hours_enabled or not self.working_hours_schedule:
            return True  # No restriction = always open
        from zoneinfo import ZoneInfo
        from django.utils import timezone as tz
        try:
            biz_tz = ZoneInfo(self.timezone)
        except Exception:
            return True
        local_dt = tz.now().astimezone(biz_tz)

        # Check holidays first — if today is a holiday, it's closed
        today_str = local_dt.strftime('%Y-%m-%d')
        if self.holidays:
            holiday_dates = [h.get('date') if isinstance(h, dict) else h for h in self.holidays]
            if today_str in holiday_dates:
                return False

        day_name = local_dt.strftime('%A').lower()
        current_hour = local_dt.hour
        day_hours = self.working_hours_schedule.get(day_name, [])
        return current_hour in day_hours

    def get_sound_urls(self):
        """Return dict of sound type → public URL (or None)."""
        urls = {}
        for field_name in [
            'sound_greeting', 'sound_after_hours', 'sound_queue_hold',
            'sound_voicemail_prompt', 'sound_thank_you', 'sound_transfer_hold',
            'sound_review_prompt', 'sound_review_invalid', 'sound_review_thanks',
            'sound_queue_position_1', 'sound_queue_position_2', 'sound_queue_position_3',
            'sound_queue_position_4', 'sound_queue_position_5', 'sound_queue_position_6',
            'sound_queue_position_7', 'sound_queue_position_8', 'sound_queue_position_9',
            'sound_queue_position_10',
        ]:
            field_file = getattr(self, field_name)
            if field_file and field_file.name:
                urls[field_name.replace('sound_', '')] = field_file.url
            else:
                urls[field_name.replace('sound_', '')] = None
        return urls


class CallRating(models.Model):
    """Tracks call ratings collected via SMS link or phone callback."""

    call_log = models.ForeignKey(
        'CallLog', on_delete=models.SET_NULL, null=True, blank=True, related_name='call_ratings'
    )
    caller_number = models.CharField(max_length=30, db_index=True)
    rated_user = models.ForeignKey(
        'users.User', on_delete=models.SET_NULL, null=True, blank=True
    )
    rating = models.IntegerField(default=0, help_text="0=pending, 1-5=rated")
    rating_token = models.CharField(max_length=100, unique=True, null=True, blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    comment = models.TextField(blank=True, default='')
    sms_message_id = models.CharField(max_length=100, blank=True, default='')
    review_method = models.CharField(
        max_length=10, default='sms',
        choices=[('sms', 'SMS'), ('callback', 'Callback')]
    )
    sip_configuration = models.ForeignKey(
        'SipConfiguration', on_delete=models.CASCADE, related_name='call_ratings'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    rated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"CallRating {self.caller_number} - {self.rating}/5"


# ---------------------------------------------------------------------------
# PBX management panel models
#
# These models back the full settings panel described in the PBX plan:
# tenants own SIP trunks (inbound DID ownership + outbound egress), define
# call queues tied to tenant groups, and route inbound DIDs to queues,
# extensions, voicemail or custom IVR contexts. The realtime sync layer
# (Django → Asterisk realtime DB) is built in a follow-up step; here we
# just establish the product schema.
# ---------------------------------------------------------------------------


class Trunk(models.Model):
    """A tenant-owned SIP trunk to a provider.

    Superset of the outbound portion of ``SipConfiguration`` — coexists with
    it during the MVP and eventually replaces it for DID ownership + outbound
    egress. One tenant may have many trunks (e.g. Magti + Silknet).
    """

    name = models.CharField(max_length=100, unique=True, help_text="Trunk name (unique per tenant)")
    provider = models.CharField(max_length=100, blank=True, help_text='Free-text provider label (e.g. "Magti", "Silknet")')

    # SIP connection
    sip_server = models.CharField(max_length=255, help_text="SIP server hostname/IP")
    sip_port = models.IntegerField(default=5060, help_text="SIP server port")
    username = models.CharField(max_length=100, help_text="SIP username")
    password = EncryptedCharField(help_text="SIP password (encrypted at rest)")
    realm = models.CharField(max_length=255, blank=True, help_text="SIP realm/domain")
    proxy = models.CharField(max_length=255, blank=True, help_text="Outbound proxy")
    register = models.BooleanField(default=True, help_text="Whether Asterisk should register against this trunk")

    # Codecs and outbound caller ID
    codecs = models.JSONField(
        default=list, blank=True,
        help_text='Preferred codec order, e.g. ["g722", "alaw", "ulaw"]'
    )
    caller_id_number = models.CharField(
        max_length=30, blank=True,
        help_text="Default caller ID number for outbound calls over this trunk"
    )

    # Inbound DIDs owned by this trunk (simple string list for MVP)
    phone_numbers = models.JSONField(
        default=list, blank=True,
        help_text='DIDs owned by this trunk, e.g. ["+995322421219", "+995322421220"]'
    )

    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False, help_text="Default trunk for outbound calls (one per tenant)")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', 'name']

    def __str__(self):
        return f"{self.name} ({self.sip_server})"

    def save(self, *args, **kwargs):
        # Ensure only one default trunk per tenant (mirrors SipConfiguration.save)
        if self.is_default:
            Trunk.objects.filter(is_default=True).exclude(id=self.id).update(is_default=False)
        super().save(*args, **kwargs)


class Queue(models.Model):
    """A call queue (Asterisk ``queues``-table analog).

    Members are derived from ``group``'s members who have an active
    ``UserPhoneAssignment`` — the sync layer materialises this into
    ``QueueMember`` rows and into Asterisk's realtime ``queue_members`` table.
    """

    STRATEGY_CHOICES = [
        ('ringall', 'Ring All'),
        ('rrmemory', 'Round-Robin Memory'),
        ('leastrecent', 'Least Recent'),
        ('fewestcalls', 'Fewest Calls'),
        ('random', 'Random'),
        ('linear', 'Linear'),
        ('wrandom', 'Weighted Random'),
    ]

    JOINEMPTY_CHOICES = [
        ('yes', 'Yes'),
        ('no', 'No'),
        ('strict', 'Strict'),
        ('loose', 'Loose'),
    ]

    LEAVEWHENEMPTY_CHOICES = [
        ('yes', 'Yes'),
        ('no', 'No'),
        ('strict', 'Strict'),
        ('loose', 'Loose'),
    ]

    name = models.CharField(max_length=100, help_text="Human-readable queue name")
    slug = models.SlugField(
        max_length=100, unique=True,
        help_text="Queue slug (unique per tenant, used in the tenant-prefixed Asterisk queue name)"
    )
    strategy = models.CharField(
        max_length=20, choices=STRATEGY_CHOICES, default='rrmemory',
        help_text="Asterisk queue ring strategy"
    )
    group = models.ForeignKey(
        'users.TenantGroup',
        on_delete=models.PROTECT,
        related_name='queues',
        help_text="Queue members are pulled from this group's members with an active UserPhoneAssignment"
    )

    # Ring behaviour
    timeout_seconds = models.IntegerField(default=30, help_text="Per-agent ring timeout in seconds")
    max_wait_seconds = models.IntegerField(default=300, help_text="Caller is abandoned after this many seconds in the queue")
    max_len = models.IntegerField(default=0, help_text="Maximum callers in queue (0 = unlimited)")
    wrapup_time = models.IntegerField(default=10, help_text="Seconds between calls for an agent (wrap-up time)")

    # Announcements / hold music
    music_on_hold = models.CharField(max_length=100, default='queue-hold', help_text="Music-on-hold class name")
    announce_position = models.BooleanField(default=True, help_text="Announce position in queue to callers")
    announce_holdtime = models.BooleanField(default=False, help_text="Announce expected hold time to callers")

    # Edge-case behaviour (Asterisk-native values)
    joinempty = models.CharField(
        max_length=10, choices=JOINEMPTY_CHOICES, default='yes',
        help_text="Whether callers can join when no agents are available"
    )
    leavewhenempty = models.CharField(
        max_length=10, choices=LEAVEWHENEMPTY_CHOICES, default='no',
        help_text="Whether queued callers are kicked when all agents become unavailable"
    )

    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False, help_text="Default queue for tenant (one per tenant)")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', 'name']

    def __str__(self):
        return f"{self.name} ({self.slug})"

    def save(self, *args, **kwargs):
        # Ensure only one default queue per tenant (mirrors SipConfiguration.save)
        if self.is_default:
            Queue.objects.filter(is_default=True).exclude(id=self.id).update(is_default=False)
        super().save(*args, **kwargs)


class QueueMember(models.Model):
    """Derived agent row — one per (Queue, UserPhoneAssignment).

    Kept in sync with ``Queue.group``'s members that have an active
    ``UserPhoneAssignment`` by the (future) sync layer. This table is
    read-only from the UI's perspective; its purpose is to give us a
    1:1 shadow of what Asterisk's realtime ``queue_members`` table should
    look like.
    """

    queue = models.ForeignKey(Queue, on_delete=models.CASCADE, related_name='members')
    user_phone_assignment = models.ForeignKey(
        UserPhoneAssignment,
        on_delete=models.CASCADE,
        related_name='queue_memberships',
    )
    penalty = models.IntegerField(default=0, help_text="Agent penalty (higher = rings last in penalty-aware strategies)")
    paused = models.BooleanField(default=False, help_text="Agent paused (not receiving new calls)")
    is_active = models.BooleanField(default=True)
    synced_at = models.DateTimeField(auto_now=True, help_text="Updated on every sync write")

    class Meta:
        ordering = ['queue', 'penalty', 'user_phone_assignment']
        unique_together = [('queue', 'user_phone_assignment')]

    def __str__(self):
        return f"{self.queue.slug} ← ext {self.user_phone_assignment.extension}"


class InboundRoute(models.Model):
    """A DID-to-destination rule.

    Rules are matched in ``priority`` order (lower first) and route an
    incoming call to a queue, a specific extension, voicemail, a custom
    IVR context, or simply hang up.
    """

    DESTINATION_CHOICES = [
        ('queue', 'Queue'),
        ('extension', 'Extension'),
        ('voicemail', 'Voicemail'),
        ('ivr_custom', 'Custom IVR context'),
        ('hangup', 'Hang up'),
    ]

    did = models.CharField(
        max_length=30,
        help_text="Matched DID (not unique — a tenant can have overlapping rules prioritised by priority)"
    )
    trunk = models.ForeignKey(
        Trunk, on_delete=models.CASCADE, null=True, blank=True,
        related_name='inbound_routes',
        help_text="Trunk this DID belongs to (optional — a null trunk matches any trunk)"
    )
    destination_type = models.CharField(
        max_length=20, choices=DESTINATION_CHOICES, default='queue',
        help_text="Where calls matching this DID should be routed"
    )
    destination_queue = models.ForeignKey(
        Queue, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='inbound_routes',
        help_text="Target queue (required when destination_type='queue')"
    )
    destination_extension = models.ForeignKey(
        UserPhoneAssignment, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='inbound_routes',
        help_text="Target extension (required when destination_type='extension')"
    )
    ivr_custom_context = models.CharField(
        max_length=100, blank=True,
        help_text="Context name in extensions_custom.conf (required when destination_type='ivr_custom')"
    )
    working_hours_override = models.ForeignKey(
        PbxSettings, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='inbound_route_overrides',
        help_text="Optional working-hours override for this route"
    )

    priority = models.IntegerField(default=100, help_text="Lower priority is checked first")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['priority', 'did']

    def __str__(self):
        return f"{self.did} → {self.destination_type} (priority {self.priority})"

    def clean(self):
        """Enforce destination-type/destination-field consistency."""
        from django.core.exceptions import ValidationError

        errors = {}
        if self.destination_type == 'queue' and not self.destination_queue_id:
            errors['destination_queue'] = "destination_queue is required when destination_type='queue'."
        if self.destination_type == 'extension' and not self.destination_extension_id:
            errors['destination_extension'] = "destination_extension is required when destination_type='extension'."
        if self.destination_type == 'ivr_custom' and not self.ivr_custom_context:
            errors['ivr_custom_context'] = "ivr_custom_context is required when destination_type='ivr_custom'."
        if errors:
            raise ValidationError(errors)


class PbxServer(models.Model):
    """Per-tenant Asterisk server that EchoDesk manages via realtime config.

    Each tenant registers one (MVP: enforce 1-per-tenant by service code)
    Asterisk instance — hostname, credentials for its dedicated Postgres
    realtime DB, and AMI credentials for runtime ops (merge_conference,
    extension_status). Provisioning flow generates ``enrollment_token``
    and the tenant runs a one-line install script on their box that
    fetches config from ``/api/pbx/install/<token>/``.
    """

    STATUS_PENDING = 'pending'
    STATUS_PROVISIONING = 'provisioning'
    STATUS_ACTIVE = 'active'
    STATUS_ERROR = 'error'
    STATUS_REVOKED = 'revoked'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_PROVISIONING, 'Provisioning'),
        (STATUS_ACTIVE, 'Active'),
        (STATUS_ERROR, 'Error'),
        (STATUS_REVOKED, 'Revoked'),
    ]

    # Identity
    name = models.CharField(max_length=100, help_text="Friendly label (e.g. 'Main PBX')")
    fqdn = models.CharField(
        max_length=255,
        help_text="FQDN of the Asterisk server (e.g. pbx.acme.com). Used to derive wss_url.",
    )
    public_ip = models.GenericIPAddressField(
        null=True, blank=True,
        help_text="Public IP — needed so DO Postgres firewall can allow this host.",
    )

    # Realtime DB connection (Django writes, Asterisk reads)
    realtime_db_host = models.CharField(max_length=255)
    realtime_db_port = models.IntegerField(default=25060)
    realtime_db_name = models.CharField(
        max_length=63,
        help_text="Tenant-dedicated Postgres database (e.g. asterisk_acme).",
    )
    realtime_db_user = models.CharField(max_length=63)
    realtime_db_password = EncryptedCharField(
        blank=True, default='',
        help_text="Encrypted at rest with Fernet.",
    )
    realtime_db_sslmode = models.CharField(max_length=16, default='require')

    # AMI (Asterisk Manager Interface) for runtime channel ops
    ami_host = models.CharField(
        max_length=255, blank=True,
        help_text="Usually same as fqdn; override if AMI is bound to a different interface.",
    )
    ami_port = models.IntegerField(default=5038)
    ami_username = models.CharField(max_length=80, blank=True)
    ami_password = EncryptedCharField(blank=True, default='')

    # SIP / WebRTC transport endpoints for softphones + recordings
    wss_url = models.URLField(
        blank=True,
        help_text="WebSocket SIP endpoint (e.g. wss://pbx.acme.com:8089/ws). "
                  "Derived from fqdn if blank.",
    )
    recording_base_url = models.URLField(
        blank=True,
        help_text="Public HTTPS base where recording files live "
                  "(e.g. https://pbx.acme.com:8443/recordings/).",
    )

    # Onboarding / health
    enrollment_token = models.CharField(
        max_length=128, unique=True, db_index=True,
        help_text="Bearer token embedded in the install script + AGI callbacks. "
                  "Used to identify this PbxServer on inbound API calls.",
    )
    enrollment_expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Install token TTL; after this the install endpoint returns 410.",
    )
    api_token = models.CharField(
        max_length=64, unique=True, db_index=True,
        help_text="Long-lived token the PBX sends as X-PBX-Token on webhooks + AGI "
                  "callbacks. Separate from the 24h enrollment_token.",
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True,
    )
    asterisk_version = models.CharField(max_length=64, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    # Naming scheme — legacy deployments on the shared public schema
    # need prefixed IDs (amanati_100) to avoid collisions. Fresh per-tenant
    # DBs don't need the prefix.
    use_tenant_prefix = models.BooleanField(
        default=False,
        help_text="Legacy shim: prefix realtime row IDs with the tenant schema "
                  "name. Leave False for new BYO PBXs that use a dedicated DB.",
    )

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'PBX Server'

    def __str__(self):
        return f"{self.name} ({self.fqdn}, {self.status})"

    def save(self, *args, **kwargs):
        if not self.enrollment_token:
            import secrets
            self.enrollment_token = secrets.token_urlsafe(48)
        if not self.api_token:
            self.api_token = self._mint_api_token()
        if not self.ami_host:
            self.ami_host = self.fqdn
        if not self.wss_url and self.fqdn:
            self.wss_url = f"wss://{self.fqdn}:8089/ws"
        if not self.recording_base_url and self.fqdn:
            self.recording_base_url = f"https://{self.fqdn}:8443/recordings/"
        super().save(*args, **kwargs)

    @property
    def is_active(self) -> bool:
        return self.status == self.STATUS_ACTIVE

    @staticmethod
    def _mint_api_token() -> str:
        import secrets
        return secrets.token_urlsafe(32)

    def regenerate_api_token(self):
        """Mint a fresh long-lived PBX token; the old one stops authenticating."""
        self.api_token = self._mint_api_token()

    def regenerate_enrollment_token(self, ttl_hours: int = 24):
        """Mint a fresh install token; invalidates any previous one."""
        import secrets
        from django.utils import timezone
        from datetime import timedelta
        self.enrollment_token = secrets.token_urlsafe(48)
        self.enrollment_expires_at = timezone.now() + timedelta(hours=ttl_hours)
        self.status = self.STATUS_PENDING
        self.save(update_fields=[
            'enrollment_token', 'enrollment_expires_at', 'status', 'updated_at',
        ])
