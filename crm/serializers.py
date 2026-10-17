from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from .models import (
    CallLog, Client, SipConfiguration, CallEvent, CallRecording,
    UserPhoneAssignment, PbxSettings, CallRating,
    Trunk, Queue, QueueMember, InboundRoute, PbxServer,
)


class MaskedSecretField(serializers.CharField):
    """A writable secret that is never echoed back.

    Responses always carry ``''`` in its place, so the response shape (and the
    generated TypeScript types the web dashboard compiles against) stays the
    same while the secret itself never leaves the server. Use the companion
    ``has_<field>`` flag to know whether a value is stored.
    """

    def to_representation(self, value):
        return ''


class SecretFieldsMixin:
    """Secret handling for ModelSerializers.

    * every name in ``secret_fields`` is a :class:`MaskedSecretField`
      (responses show ``''``);
    * on update the secrets are optional, and a blank / null value means
      "keep the stored secret", so edit forms that cannot show the current
      value do not wipe it.
    """

    secret_fields = ()

    def get_fields(self):
        fields = super().get_fields()
        for name in self.secret_fields:
            field = fields.get(name)
            if field is None:
                continue
            if not isinstance(field, MaskedSecretField):
                masked = MaskedSecretField(
                    max_length=getattr(field, 'max_length', None),
                    required=field.required,
                    allow_blank=getattr(field, 'allow_blank', False),
                    help_text=field.help_text,
                )
                fields[name] = field = masked
            if self.instance is not None:
                field.required = False
                field.allow_blank = True
                field.allow_null = True
        return fields

    def update(self, instance, validated_data):
        for name in self.secret_fields:
            if name in validated_data and validated_data[name] in (None, ''):
                validated_data.pop(name)
        return super().update(instance, validated_data)


class UserPhoneAssignmentSerializer(SecretFieldsMixin, serializers.ModelSerializer):
    """User <-> extension assignment. ``extension_password`` is write-only in
    effect: required on create, always ``''`` in responses, blank on update
    keeps the stored one."""
    secret_fields = ('extension_password',)

    user_name = serializers.SerializerMethodField()
    user_email = serializers.SerializerMethodField()
    has_extension_password = serializers.SerializerMethodField()

    class Meta:
        model = UserPhoneAssignment
        fields = [
            'id', 'user', 'user_name', 'user_email', 'sip_configuration',
            'extension', 'extension_password', 'has_extension_password',
            'phone_number', 'display_name',
            'is_primary', 'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    @extend_schema_field(serializers.CharField)
    def get_user_name(self, obj):
        return obj.user.get_full_name() or obj.user.email

    @extend_schema_field(serializers.CharField)
    def get_user_email(self, obj):
        return obj.user.email

    @extend_schema_field(serializers.BooleanField)
    def get_has_extension_password(self, obj):
        return bool(obj.extension_password)


class SipConfigurationSerializer(SecretFieldsMixin, serializers.ModelSerializer):
    """SIP configuration create/update. ``password`` and ``turn_password``
    are never returned (``''``); blank on update keeps the stored value."""
    secret_fields = ('password', 'turn_password')

    # Optional on create too: the web form creates the config first and
    # sets the password in a later edit.
    password = MaskedSecretField(max_length=255, required=False, allow_blank=True)
    turn_password = MaskedSecretField(max_length=255, required=False, allow_blank=True)
    user_assignments = UserPhoneAssignmentSerializer(many=True, read_only=True)
    has_password = serializers.SerializerMethodField()
    has_turn_password = serializers.SerializerMethodField()

    class Meta:
        model = SipConfiguration
        fields = [
            'id', 'name', 'sip_server', 'sip_port', 'username', 'password',
            'realm', 'proxy', 'phone_number', 'websocket_path', 'stun_server', 'turn_server',
            'turn_username', 'turn_password', 'has_password', 'has_turn_password',
            'is_active', 'is_default',
            'max_concurrent_calls', 'created_at', 'updated_at', 'user_assignments'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    @extend_schema_field(serializers.BooleanField)
    def get_has_password(self, obj):
        return bool(obj.password)

    @extend_schema_field(serializers.BooleanField)
    def get_has_turn_password(self, obj):
        return bool(obj.turn_password)

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class SipConfigurationListSerializer(serializers.ModelSerializer):
    """Simplified serializer for listing SIP configurations"""
    assignment_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = SipConfiguration
        fields = ['id', 'name', 'sip_server', 'phone_number', 'is_active', 'is_default', 'assignment_count']


SOFTPHONE_CONTEXT_KEY = 'softphone_credentials'


class SipConfigurationDetailSerializer(serializers.ModelSerializer):
    """SIP configuration detail (read-only).

    By default it carries no secrets: ``password`` and ``turn_password`` are
    ``''`` and every nested ``user_assignments[].extension_password`` is
    ``''`` (see the ``has_*`` flags).

    The softphone endpoints (``my_config``, ``webrtc_config``) put
    ``{'username': ..., 'password': ...}`` under ``softphone_credentials`` in
    the context: then ``username``/``password`` are the caller's own SIP
    credentials, ``turn_password`` is filled (ICE needs it) and
    ``user_assignments`` is ``[]``.
    """
    username = serializers.SerializerMethodField()
    password = serializers.SerializerMethodField()
    turn_password = serializers.SerializerMethodField()
    user_assignments = UserPhoneAssignmentSerializer(many=True, read_only=True)
    has_password = serializers.SerializerMethodField()
    has_turn_password = serializers.SerializerMethodField()

    class Meta:
        model = SipConfiguration
        fields = [
            'id', 'name', 'sip_server', 'sip_port', 'username', 'password',
            'realm', 'proxy', 'phone_number', 'websocket_path', 'stun_server', 'turn_server',
            'turn_username', 'turn_password', 'has_password', 'has_turn_password',
            'is_active', 'is_default',
            'max_concurrent_calls', 'user_assignments'
        ]

    def _softphone(self):
        return self.context.get(SOFTPHONE_CONTEXT_KEY)

    @extend_schema_field(serializers.CharField)
    def get_username(self, obj):
        creds = self._softphone()
        return creds['username'] if creds else obj.username

    @extend_schema_field(serializers.CharField)
    def get_password(self, obj):
        creds = self._softphone()
        return (creds['password'] or '') if creds else ''

    @extend_schema_field(serializers.CharField)
    def get_turn_password(self, obj):
        return obj.turn_password if self._softphone() else ''

    @extend_schema_field(serializers.BooleanField)
    def get_has_password(self, obj):
        return bool(obj.password)

    @extend_schema_field(serializers.BooleanField)
    def get_has_turn_password(self, obj):
        return bool(obj.turn_password)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if self._softphone():
            data['user_assignments'] = []
        return data


class UserPhoneAssignmentDetailSerializer(SecretFieldsMixin, serializers.ModelSerializer):
    """Assignment detail with the (secret-free) SIP config it belongs to."""
    secret_fields = ('extension_password',)

    sip_configuration = serializers.SerializerMethodField()
    has_extension_password = serializers.SerializerMethodField()

    class Meta:
        model = UserPhoneAssignment
        fields = [
            'id', 'user', 'sip_configuration', 'extension', 'extension_password',
            'has_extension_password',
            'phone_number', 'display_name', 'is_primary', 'is_active'
        ]

    @extend_schema_field(SipConfigurationDetailSerializer)
    def get_sip_configuration(self, obj):
        context = {k: v for k, v in self.context.items() if k != SOFTPHONE_CONTEXT_KEY}
        return SipConfigurationDetailSerializer(obj.sip_configuration, context=context).data

    @extend_schema_field(serializers.BooleanField)
    def get_has_extension_password(self, obj):
        return bool(obj.extension_password)


class MySoftphoneConfigSerializer(serializers.Serializer):
    """Response of ``GET /api/sip-configurations/my_config/``: the caller's
    own extension and credentials (``sip_configuration.username/password``
    are the same credentials). ``id`` is null when the caller has no
    extension and the tenant's default (shared) SIP account is returned."""
    id = serializers.IntegerField(allow_null=True)
    user = serializers.IntegerField()
    sip_configuration = SipConfigurationDetailSerializer()
    extension = serializers.CharField()
    extension_password = serializers.CharField()
    phone_number = serializers.CharField(allow_blank=True)
    display_name = serializers.CharField(allow_blank=True)
    is_primary = serializers.BooleanField()
    is_active = serializers.BooleanField()


def _is_admin_request(serializer):
    """Tenant admin = is_staff or is_superuser. Recordings + review ratings
    are gated to admins only — agents see neither the URL nor the score."""
    request = serializer.context.get('request')
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return False
    return bool(user.is_staff or user.is_superuser)


def _format_duration(duration):
    if not duration:
        return None
    total_seconds = int(duration.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _user_display_name(user):
    if user is None:
        return None
    return f"{user.first_name} {user.last_name}".strip()


class CallLogSerializer(serializers.ModelSerializer):
    """Enhanced serializer for CallLog model with SIP support"""

    handled_by_name = serializers.SerializerMethodField()
    transferred_to_user_name = serializers.SerializerMethodField()
    duration_display = serializers.SerializerMethodField()
    client_name = serializers.SerializerMethodField()
    sip_config_name = serializers.CharField(
        source='sip_configuration.name', read_only=True, allow_null=True,
    )
    recording_url = serializers.SerializerMethodField()
    call_quality_score = serializers.SerializerMethodField()

    class Meta:
        model = CallLog
        fields = [
            'id', 'call_id', 'caller_number', 'recipient_number',
            'direction', 'call_type', 'started_at', 'answered_at',
            'ended_at', 'duration', 'duration_display', 'status',
            'notes', 'sip_call_id', 'client', 'social_client', 'client_name',
            'handled_by', 'handled_by_name', 'sip_configuration',
            'sip_config_name', 'recording_url', 'call_quality_score',
            'transferred_to', 'transferred_to_user', 'transferred_to_user_name',
            'transferred_at', 'parent_call', 'transfer_type',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'call_id', 'created_at', 'updated_at', 'client', 'social_client', 'parent_call']

    @extend_schema_field(serializers.CharField)
    def get_recording_url(self, obj):
        """Return the recording URL, falling back to the related CallRecording.

        Historical recordings were written to CallLog.recording_url; the newer
        webhook pipeline writes to CallRecording.file_url instead and doesn't
        always backfill the legacy field. Restricted to tenant admins only —
        agents get an empty string so the recording UI hides itself.
        """
        if not _is_admin_request(self):
            return ''
        if obj.recording_url:
            return obj.recording_url
        # Reverse OneToOne raises RelatedObjectDoesNotExist when no recording
        # row exists — guard with try/except rather than hasattr so a
        # select_related hit doesn't spuriously fail.
        try:
            recording = obj.recording
        except CallRecording.DoesNotExist:
            return ''
        return recording.file_url or ''

    @extend_schema_field(serializers.FloatField(allow_null=True))
    def get_call_quality_score(self, obj):
        """Customer review rating (1-5 from IVR). Admin-only — agents
        shouldn't see how individual customers rated them."""
        if not _is_admin_request(self):
            return None
        return obj.call_quality_score

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_client_name(self, obj):
        """Get client name from social_client first, then CRM client"""
        if obj.social_client:
            return obj.social_client.name
        if obj.client:
            return obj.client.name
        return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_handled_by_name(self, obj):
        """Get the name of the user who handled the call"""
        return _user_display_name(obj.handled_by)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_transferred_to_user_name(self, obj):
        return _user_display_name(obj.transferred_to_user)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_duration_display(self, obj):
        """Display duration in human-readable format (null when 0/empty)."""
        return _format_duration(obj.duration)


class ConsultationInitiateSerializer(serializers.Serializer):
    """Serializer for initiating an attended transfer consultation call"""
    target_number = serializers.CharField(max_length=30)
    target_user_id = serializers.IntegerField(required=False)


class MergeConferenceSerializer(serializers.Serializer):
    """Serializer for merging an attended transfer into a 3-way conference"""
    consultation_log_id = serializers.IntegerField(help_text="ID of the consultation CallLog")


class CallLogCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating new call logs"""

    class Meta:
        model = CallLog
        fields = [
            'caller_number', 'recipient_number', 'direction',
            'call_type', 'sip_call_id', 'sip_configuration', 'notes'
        ]


class CallInitiateSerializer(serializers.Serializer):
    """Serializer for initiating outbound calls"""
    recipient_number = serializers.CharField(max_length=20)
    call_type = serializers.ChoiceField(
        choices=CallLog.CALL_TYPE_CHOICES, 
        default='voice'
    )
    sip_configuration = serializers.IntegerField(required=False)
    
    def validate_recipient_number(self, value):
        """Validate phone number format"""
        # Remove common formatting characters
        clean_number = value.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
        if not clean_number.isdigit() or len(clean_number) < 7:
            raise serializers.ValidationError("Please enter a valid phone number")
        return value


class CallStatusUpdateSerializer(serializers.Serializer):
    """Serializer for updating call status"""
    status = serializers.ChoiceField(choices=CallLog.STATUS_CHOICES)
    notes = serializers.CharField(required=False, allow_blank=True)
    call_quality_score = serializers.FloatField(min_value=0, max_value=5, required=False)
    recording_url = serializers.URLField(required=False, allow_blank=True)


class CallEndRequestSerializer(serializers.Serializer):
    """Body of ``POST /api/call-logs/{id}/end_call/``. Everything is
    optional and ignored (the server decides ended vs missed); send ``{}``.
    ``status`` is accepted only for older clients that still send it."""
    status = serializers.CharField(required=False, help_text='Ignored.')


class ClientSerializer(serializers.ModelSerializer):
    """Serializer for Client model"""
    
    class Meta:
        model = Client
        fields = (
            'id', 'name', 'email', 'phone', 'company', 
            'created_at', 'updated_at', 'is_active'
        )
        read_only_fields = ('id', 'created_at', 'updated_at')


class CallEventSerializer(serializers.ModelSerializer):
    """Serializer for call events"""
    
    user_name = serializers.SerializerMethodField()
    
    class Meta:
        model = CallEvent
        fields = [
            'id', 'event_type', 'timestamp', 'metadata', 
            'user', 'user_name'
        ]
        read_only_fields = ['id', 'timestamp']
    
    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_user_name(self, obj):
        """Get the name of the user who triggered the event"""
        if obj.user:
            return f"{obj.user.first_name} {obj.user.last_name}".strip()
        return None


class CallRecordingSerializer(serializers.ModelSerializer):
    """Serializer for call recordings"""
    
    duration_display = serializers.SerializerMethodField()
    file_size_display = serializers.SerializerMethodField()
    
    class Meta:
        model = CallRecording
        fields = [
            'id', 'recording_id', 'file_path', 'file_url', 
            'file_size', 'file_size_display', 'duration', 'duration_display',
            'format', 'status', 'started_at', 'completed_at',
            'transcript', 'transcript_confidence', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'recording_id', 'created_at', 'updated_at']
    
    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_duration_display(self, obj):
        """Display duration in human-readable format"""
        if obj.duration:
            total_seconds = int(obj.duration.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60
            if hours > 0:
                return f"{hours}:{minutes:02d}:{seconds:02d}"
            else:
                return f"{minutes}:{seconds:02d}"
        return None
    
    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_file_size_display(self, obj):
        """Display file size in human-readable format"""
        if obj.file_size:
            # Convert bytes to MB
            size_mb = obj.file_size / (1024 * 1024)
            if size_mb < 1:
                size_kb = obj.file_size / 1024
                return f"{size_kb:.1f} KB"
            else:
                return f"{size_mb:.1f} MB"
        return None


class CallLogDetailSerializer(CallLogSerializer):
    """Call log detail: every list field plus the event timeline and the
    recording (admin-only; null for agents and calls without one)."""

    events = CallEventSerializer(many=True, read_only=True)
    recording = serializers.SerializerMethodField()

    class Meta(CallLogSerializer.Meta):
        fields = CallLogSerializer.Meta.fields + ['events', 'recording']

    @extend_schema_field(CallRecordingSerializer(allow_null=True))
    def get_recording(self, obj):
        """Nested CallRecording — admin-only."""
        if not _is_admin_request(self):
            return None
        try:
            recording = obj.recording
        except CallRecording.DoesNotExist:
            return None
        return CallRecordingSerializer(recording, context=self.context).data


class PbxSettingsSerializer(serializers.ModelSerializer):
    """Serializer for PBX working hours and sound management."""

    sound_greeting_url = serializers.SerializerMethodField()
    sound_after_hours_url = serializers.SerializerMethodField()
    sound_queue_hold_url = serializers.SerializerMethodField()
    sound_voicemail_prompt_url = serializers.SerializerMethodField()
    sound_thank_you_url = serializers.SerializerMethodField()
    sound_transfer_hold_url = serializers.SerializerMethodField()
    sound_review_prompt_url = serializers.SerializerMethodField()
    sound_review_invalid_url = serializers.SerializerMethodField()
    sound_review_thanks_url = serializers.SerializerMethodField()
    sound_queue_position_1_url = serializers.SerializerMethodField()
    sound_queue_position_2_url = serializers.SerializerMethodField()
    sound_queue_position_3_url = serializers.SerializerMethodField()
    sound_queue_position_4_url = serializers.SerializerMethodField()
    sound_queue_position_5_url = serializers.SerializerMethodField()
    sound_queue_position_6_url = serializers.SerializerMethodField()
    sound_queue_position_7_url = serializers.SerializerMethodField()
    sound_queue_position_8_url = serializers.SerializerMethodField()
    sound_queue_position_9_url = serializers.SerializerMethodField()
    sound_queue_position_10_url = serializers.SerializerMethodField()

    class Meta:
        model = PbxSettings
        fields = [
            'id', 'sip_configuration',
            'working_hours_enabled', 'working_hours_schedule', 'timezone', 'holidays',
            'after_hours_action', 'forward_number', 'voicemail_enabled',
            'review_method', 'sms_api_key', 'sms_rating_template_ka', 'sms_rating_template_en',
            'review_delay_hours', 'review_cooldown_hours',
            'sound_greeting', 'sound_after_hours', 'sound_queue_hold',
            'sound_voicemail_prompt', 'sound_thank_you', 'sound_transfer_hold',
            'sound_review_prompt', 'sound_review_invalid', 'sound_review_thanks',
            'sound_queue_position_1', 'sound_queue_position_2', 'sound_queue_position_3',
            'sound_queue_position_4', 'sound_queue_position_5', 'sound_queue_position_6',
            'sound_queue_position_7', 'sound_queue_position_8', 'sound_queue_position_9',
            'sound_queue_position_10',
            'sound_greeting_url', 'sound_after_hours_url', 'sound_queue_hold_url',
            'sound_voicemail_prompt_url', 'sound_thank_you_url', 'sound_transfer_hold_url',
            'sound_review_prompt_url', 'sound_review_invalid_url', 'sound_review_thanks_url',
            'sound_queue_position_1_url', 'sound_queue_position_2_url', 'sound_queue_position_3_url',
            'sound_queue_position_4_url', 'sound_queue_position_5_url', 'sound_queue_position_6_url',
            'sound_queue_position_7_url', 'sound_queue_position_8_url', 'sound_queue_position_9_url',
            'sound_queue_position_10_url',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'sip_configuration', 'created_at', 'updated_at']
        extra_kwargs = {
            'sms_api_key': {'write_only': True},
            'sound_greeting': {'write_only': True, 'required': False},
            'sound_after_hours': {'write_only': True, 'required': False},
            'sound_queue_hold': {'write_only': True, 'required': False},
            'sound_voicemail_prompt': {'write_only': True, 'required': False},
            'sound_thank_you': {'write_only': True, 'required': False},
            'sound_transfer_hold': {'write_only': True, 'required': False},
            'sound_review_prompt': {'write_only': True, 'required': False},
            'sound_review_invalid': {'write_only': True, 'required': False},
            'sound_review_thanks': {'write_only': True, 'required': False},
            'sound_queue_position_1': {'write_only': True, 'required': False},
            'sound_queue_position_2': {'write_only': True, 'required': False},
            'sound_queue_position_3': {'write_only': True, 'required': False},
            'sound_queue_position_4': {'write_only': True, 'required': False},
            'sound_queue_position_5': {'write_only': True, 'required': False},
            'sound_queue_position_6': {'write_only': True, 'required': False},
            'sound_queue_position_7': {'write_only': True, 'required': False},
            'sound_queue_position_8': {'write_only': True, 'required': False},
            'sound_queue_position_9': {'write_only': True, 'required': False},
            'sound_queue_position_10': {'write_only': True, 'required': False},
        }

    def _get_url(self, obj, field_name):
        f = getattr(obj, field_name)
        if f and f.name:
            return f.url
        return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_greeting_url(self, obj):
        return self._get_url(obj, 'sound_greeting')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_after_hours_url(self, obj):
        return self._get_url(obj, 'sound_after_hours')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_queue_hold_url(self, obj):
        return self._get_url(obj, 'sound_queue_hold')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_voicemail_prompt_url(self, obj):
        return self._get_url(obj, 'sound_voicemail_prompt')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_thank_you_url(self, obj):
        return self._get_url(obj, 'sound_thank_you')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_transfer_hold_url(self, obj):
        return self._get_url(obj, 'sound_transfer_hold')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_review_prompt_url(self, obj):
        return self._get_url(obj, 'sound_review_prompt')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_review_invalid_url(self, obj):
        return self._get_url(obj, 'sound_review_invalid')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_review_thanks_url(self, obj):
        return self._get_url(obj, 'sound_review_thanks')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_queue_position_1_url(self, obj):
        return self._get_url(obj, 'sound_queue_position_1')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_queue_position_2_url(self, obj):
        return self._get_url(obj, 'sound_queue_position_2')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_queue_position_3_url(self, obj):
        return self._get_url(obj, 'sound_queue_position_3')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_queue_position_4_url(self, obj):
        return self._get_url(obj, 'sound_queue_position_4')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_queue_position_5_url(self, obj):
        return self._get_url(obj, 'sound_queue_position_5')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_queue_position_6_url(self, obj):
        return self._get_url(obj, 'sound_queue_position_6')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_queue_position_7_url(self, obj):
        return self._get_url(obj, 'sound_queue_position_7')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_queue_position_8_url(self, obj):
        return self._get_url(obj, 'sound_queue_position_8')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_queue_position_9_url(self, obj):
        return self._get_url(obj, 'sound_queue_position_9')

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sound_queue_position_10_url(self, obj):
        return self._get_url(obj, 'sound_queue_position_10')


class CallRatingSerializer(serializers.ModelSerializer):
    """Serializer for call ratings collected via SMS or phone callback."""

    rated_user_name = serializers.SerializerMethodField()

    class Meta:
        model = CallRating
        fields = [
            'id', 'call_log', 'caller_number', 'rated_user', 'rated_user_name',
            'rating', 'rating_token', 'token_expires_at', 'comment',
            'sms_message_id', 'review_method', 'sip_configuration',
            'created_at', 'rated_at',
        ]
        read_only_fields = ['id', 'created_at', 'rated_at']

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_rated_user_name(self, obj):
        if obj.rated_user:
            return f"{obj.rated_user.first_name} {obj.rated_user.last_name}".strip()
        return None


# ---------------------------------------------------------------------------
# PBX management panel serializers
# ---------------------------------------------------------------------------


class TrunkSerializer(SecretFieldsMixin, serializers.ModelSerializer):
    """Full serializer for ``Trunk`` — used on retrieve/create/update.
    ``password`` is never returned (``''``); blank on update keeps the stored value."""
    secret_fields = ('password',)

    has_password = serializers.SerializerMethodField()

    class Meta:
        model = Trunk
        fields = [
            'id', 'name', 'provider',
            'sip_server', 'sip_port', 'username', 'password', 'has_password',
            'realm', 'proxy', 'register',
            'codecs', 'caller_id_number', 'phone_numbers',
            'is_active', 'is_default',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    @extend_schema_field(serializers.BooleanField)
    def get_has_password(self, obj):
        return bool(obj.password)


class TrunkListSerializer(serializers.ModelSerializer):
    """Lighter serializer for trunk lists."""

    class Meta:
        model = Trunk
        fields = ['id', 'name', 'provider', 'phone_numbers', 'is_active']


def _queue_member_count(queue):
    """Use the ``active_member_count`` annotation from the viewset when
    present (one query for the whole page); fall back to a COUNT."""
    annotated = getattr(queue, 'active_member_count', None)
    if annotated is not None:
        return annotated
    return queue.members.filter(is_active=True).count()


class QueueSerializer(serializers.ModelSerializer):
    """Full serializer for ``Queue`` — used on retrieve/create/update."""

    group_name = serializers.CharField(source='group.name', read_only=True)
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Queue
        fields = [
            'id', 'name', 'slug', 'strategy',
            'group', 'group_name',
            'timeout_seconds', 'max_wait_seconds', 'max_len', 'wrapup_time',
            'music_on_hold', 'announce_position', 'announce_holdtime',
            'joinempty', 'leavewhenempty',
            'is_active', 'is_default',
            'member_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    @extend_schema_field(serializers.IntegerField)
    def get_member_count(self, obj):
        """Active QueueMember rows for this queue (materialised by sync layer)."""
        return _queue_member_count(obj)


class QueueListSerializer(serializers.ModelSerializer):
    """Lighter serializer for queue lists."""

    group_name = serializers.CharField(source='group.name', read_only=True)
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Queue
        fields = [
            'id', 'name', 'slug', 'strategy',
            'group', 'group_name', 'member_count',
            'is_active', 'is_default',
        ]

    @extend_schema_field(serializers.IntegerField)
    def get_member_count(self, obj):
        return _queue_member_count(obj)


class QueueMemberSerializer(serializers.ModelSerializer):
    """Read-only serializer for QueueMember.

    Rows are materialised by the sync layer from ``Queue.group``'s members
    that have an active ``UserPhoneAssignment``.
    """

    queue_slug = serializers.CharField(source='queue.slug', read_only=True)
    queue_name = serializers.CharField(source='queue.name', read_only=True)
    extension = serializers.CharField(source='user_phone_assignment.extension', read_only=True)
    phone_number = serializers.CharField(source='user_phone_assignment.phone_number', read_only=True)
    user_id = serializers.IntegerField(source='user_phone_assignment.user_id', read_only=True)
    user_email = serializers.CharField(source='user_phone_assignment.user.email', read_only=True)
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = QueueMember
        fields = [
            'id', 'queue', 'queue_slug', 'queue_name',
            'user_phone_assignment', 'extension', 'phone_number',
            'user_id', 'user_email', 'user_name',
            'penalty', 'paused', 'is_active', 'synced_at',
        ]
        read_only_fields = fields  # Entire serializer is read-only.

    @extend_schema_field(serializers.CharField)
    def get_user_name(self, obj):
        user = obj.user_phone_assignment.user
        return f"{user.first_name} {user.last_name}".strip() or user.email


class InboundRouteSerializer(serializers.ModelSerializer):
    """Serializer for ``InboundRoute`` with destination-consistency validation."""

    trunk_name = serializers.CharField(source='trunk.name', read_only=True)
    destination_queue_slug = serializers.CharField(source='destination_queue.slug', read_only=True)
    destination_extension_display = serializers.SerializerMethodField()

    class Meta:
        model = InboundRoute
        fields = [
            'id', 'did',
            'trunk', 'trunk_name',
            'destination_type',
            'destination_queue', 'destination_queue_slug',
            'destination_extension', 'destination_extension_display',
            'ivr_custom_context', 'working_hours_override',
            'priority', 'is_active',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_destination_extension_display(self, obj):
        if obj.destination_extension:
            return f"ext {obj.destination_extension.extension} ({obj.destination_extension.phone_number})"
        return None

    def validate(self, attrs):
        """Mirror ``InboundRoute.clean()`` — DRF-side destination consistency."""
        # Merge incoming attrs with the instance's current values so partial
        # updates still get sensible validation.
        merged = {}
        if self.instance is not None:
            for field in ('destination_type', 'destination_queue', 'destination_extension', 'ivr_custom_context'):
                merged[field] = getattr(self.instance, field)
        merged.update(attrs)

        destination_type = merged.get('destination_type')
        errors = {}

        if destination_type == 'queue' and not merged.get('destination_queue'):
            errors['destination_queue'] = "destination_queue is required when destination_type='queue'."
        if destination_type == 'extension' and not merged.get('destination_extension'):
            errors['destination_extension'] = "destination_extension is required when destination_type='extension'."
        if destination_type == 'ivr_custom' and not merged.get('ivr_custom_context'):
            errors['ivr_custom_context'] = "ivr_custom_context is required when destination_type='ivr_custom'."

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class PbxServerSerializer(serializers.ModelSerializer):
    """PbxServer CRUD serializer. Passwords are write-only — never returned
    in responses. The ``install_command`` is computed from the enrollment
    token so the UI can show the curl one-liner.
    """
    install_command = serializers.SerializerMethodField()
    realtime_db_password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    ami_password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = PbxServer
        fields = [
            'id', 'name', 'fqdn', 'public_ip',
            'realtime_db_host', 'realtime_db_port', 'realtime_db_name',
            'realtime_db_user', 'realtime_db_password', 'realtime_db_sslmode',
            'ami_host', 'ami_port', 'ami_username', 'ami_password',
            'wss_url', 'recording_base_url',
            'status', 'asterisk_version', 'last_seen_at',
            'enrollment_token', 'enrollment_expires_at', 'install_command',
            'use_tenant_prefix', 'notes',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'status', 'asterisk_version', 'last_seen_at',
            'enrollment_token', 'enrollment_expires_at', 'install_command',
            'created_at', 'updated_at',
        ]

    @extend_schema_field(serializers.CharField)
    def get_install_command(self, obj):
        request = self.context.get('request') if self.context else None
        if request is None:
            base = ''
        else:
            base = request.build_absolute_uri('/').rstrip('/')
        return f"curl -sSL {base}/api/pbx/install/{obj.enrollment_token}/ | sudo bash"
