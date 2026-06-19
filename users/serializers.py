from rest_framework import serializers
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.password_validation import validate_password
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from .models import Department, TenantGroup, Notification, NotificationPreference, UserOnlineStatus, TeamChatConversation, TeamChatMessage
from tenants.feature_models import Feature

User = get_user_model()


class PermissionSerializer(serializers.ModelSerializer):
    """Serializer for Django permissions"""
    app_label = serializers.CharField(source='content_type.app_label', read_only=True)
    model = serializers.CharField(source='content_type.model', read_only=True)
    
    class Meta:
        model = Permission
        fields = ['id', 'name', 'codename', 'content_type', 'app_label', 'model']


class DepartmentSerializer(serializers.ModelSerializer):
    """Serializer for Department"""
    employee_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Department
        fields = ['id', 'name', 'description', 'is_active', 'employee_count', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']
    
    def get_employee_count(self, obj):
        return getattr(obj, '_employee_count', obj.employees.count())


class GroupSerializer(serializers.ModelSerializer):
    """Serializer for Django Groups with permissions"""
    permissions = PermissionSerializer(many=True, read_only=True)
    permission_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        help_text="List of permission IDs to assign to this group"
    )
    user_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Group
        fields = ['id', 'name', 'permissions', 'permission_ids', 'user_count']
    
    def get_user_count(self, obj):
        return getattr(obj, '_user_count', obj.user_set.count())
    
    def create(self, validated_data):
        permission_ids = validated_data.pop('permission_ids', [])
        group = Group.objects.create(**validated_data)
        
        if permission_ids:
            permissions = Permission.objects.filter(id__in=permission_ids)
            group.permissions.set(permissions)
        
        return group
    
    def update(self, instance, validated_data):
        permission_ids = validated_data.pop('permission_ids', None)
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        if permission_ids is not None:
            permissions = Permission.objects.filter(id__in=permission_ids)
            instance.permissions.set(permissions)
        
        return instance


class GroupCreateSerializer(serializers.ModelSerializer):
    """Simplified serializer for creating groups"""
    permission_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="List of permission IDs to assign to this group"
    )
    
    class Meta:
        model = Group
        fields = ['name', 'permission_ids']
    
    def create(self, validated_data):
        permission_ids = validated_data.pop('permission_ids', [])
        group = Group.objects.create(**validated_data)
        
        if permission_ids:
            permissions = Permission.objects.filter(id__in=permission_ids)
            group.permissions.set(permissions)
        
        return group


class FeatureMinimalSerializer(serializers.ModelSerializer):
    """Minimal serializer for Feature model"""
    class Meta:
        model = Feature
        fields = ['id', 'key', 'name', 'description', 'category', 'icon']


class TenantGroupSerializer(serializers.ModelSerializer):
    """Serializer for TenantGroup model with feature-based permissions"""
    member_count = serializers.SerializerMethodField()
    features = FeatureMinimalSerializer(many=True, read_only=True)
    feature_keys = serializers.SerializerMethodField()

    class Meta:
        model = TenantGroup
        fields = [
            'id', 'name', 'description', 'is_active', 'created_at', 'updated_at',
            'member_count', 'features', 'feature_keys'
        ]
        read_only_fields = ['created_at', 'updated_at', 'member_count', 'feature_keys']

    def get_member_count(self, obj):
        return getattr(obj, '_member_count', obj.members.count())

    def get_feature_keys(self, obj):
        """Return list of feature keys for easy checking"""
        return obj.get_feature_keys()


class TenantGroupCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating TenantGroup"""
    feature_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        help_text="List of feature IDs to assign to this group"
    )

    class Meta:
        model = TenantGroup
        fields = ['name', 'description', 'feature_ids']

    def create(self, validated_data):
        feature_ids = validated_data.pop('feature_ids', [])
        group = TenantGroup.objects.create(**validated_data)

        if feature_ids:
            from tenants.feature_models import Feature
            features = Feature.objects.filter(id__in=feature_ids, is_active=True)
            group.features.set(features)

        return group

    def update(self, instance, validated_data):
        feature_ids = validated_data.pop('feature_ids', None)

        # Update basic fields
        instance.name = validated_data.get('name', instance.name)
        instance.description = validated_data.get('description', instance.description)
        instance.save()

        # Update features if provided
        if feature_ids is not None:
            from tenants.feature_models import Feature
            features = Feature.objects.filter(id__in=feature_ids, is_active=True)
            instance.features.set(features)

        return instance


class UserSerializer(serializers.ModelSerializer):
    """Serializer for User model with permissions and groups"""
    full_name = serializers.ReadOnlyField()
    permissions = serializers.SerializerMethodField()
    group_permissions = serializers.SerializerMethodField()
    all_permissions = serializers.SerializerMethodField()
    feature_keys = serializers.SerializerMethodField()
    is_booking_staff = serializers.SerializerMethodField()
    groups = GroupSerializer(many=True, read_only=True)
    tenant_groups = TenantGroupSerializer(many=True, read_only=True)
    department = DepartmentSerializer(read_only=True)
    department_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    group_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        help_text="List of group IDs to assign to this user"
    )
    tenant_group_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        help_text="List of tenant group IDs to assign to this user"
    )
    user_permission_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        help_text="List of permission IDs to assign directly to this user"
    )

    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'role', 'status', 'phone_number', 'job_title', 'department', 'department_id',
            'is_active', 'is_staff', 'is_booking_staff', 'date_joined', 'last_login',
            'permissions', 'group_permissions', 'all_permissions', 'feature_keys',
            'groups', 'group_ids', 'tenant_groups', 'tenant_group_ids', 'user_permission_ids'
        ]
        read_only_fields = ['id', 'date_joined', 'last_login', 'feature_keys', 'is_booking_staff']

    def get_permissions(self, obj):
        """Get user's direct permissions"""
        return obj.get_user_permissions_list()

    def get_group_permissions(self, obj):
        """Get permissions from groups"""
        return obj.get_group_permissions_list()

    def get_feature_keys(self, obj):
        """Get all feature keys this user has access to"""
        return obj.get_feature_keys()

    def get_all_permissions(self, obj):
        """Get all permissions (user + group) as a dictionary"""
        return obj.get_all_permissions_dict()

    def get_is_booking_staff(self, obj):
        """Check if user is assigned as booking staff"""
        return hasattr(obj, 'booking_staff')

    def update(self, instance, validated_data):
        group_ids = validated_data.pop('group_ids', None)
        tenant_group_ids = validated_data.pop('tenant_group_ids', None)
        user_permission_ids = validated_data.pop('user_permission_ids', None)
        department_id = validated_data.pop('department_id', None)
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        # Handle department assignment
        if department_id is not None:
            if department_id:
                try:
                    department = Department.objects.get(id=department_id)
                    instance.department = department
                except Department.DoesNotExist:
                    raise serializers.ValidationError({'department_id': 'Department not found'})
            else:
                instance.department = None
        
        instance.save()
        
        if group_ids is not None:
            groups = Group.objects.filter(id__in=group_ids)
            instance.groups.set(groups)
        
        if tenant_group_ids is not None:
            tenant_groups = TenantGroup.objects.filter(id__in=tenant_group_ids)
            instance.tenant_groups.set(tenant_groups)
        
        if user_permission_ids is not None:
            permissions = Permission.objects.filter(id__in=user_permission_ids)
            instance.user_permissions.set(permissions)
        
        return instance


class EmailUniquenessMixin:
    """Reject an email that already belongs to another user in this tenant.

    The DB already enforces exact uniqueness (User.email is unique=True, scoped
    to the tenant schema), but the check here is case-insensitive and returns a
    clean 400 instead of an IntegrityError, and excludes the instance being
    updated so an admin can save a user without changing their email.
    """

    def validate_email(self, value):
        qs = User.objects.filter(email__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                "A user with this email already exists."
            )
        return value


class UserCreateSerializer(EmailUniquenessMixin, serializers.ModelSerializer):
    """Serializer for creating users - password is auto-generated and sent via email"""
    department_id = serializers.IntegerField(required=False, allow_null=True)
    group_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="List of Django group IDs to assign to this user"
    )
    tenant_group_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="List of tenant group IDs to assign to this user"
    )
    user_permission_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="List of permission IDs to assign directly to this user"
    )

    class Meta:
        model = User
        fields = [
            'email', 'first_name', 'last_name',
            'role', 'status', 'phone_number', 'department_id',
            'group_ids', 'tenant_group_ids', 'user_permission_ids'
        ]
        extra_kwargs = {
            'phone_number': {'required': False},
        }

    def create(self, validated_data):
        group_ids = validated_data.pop('group_ids', [])
        tenant_group_ids = validated_data.pop('tenant_group_ids', [])
        user_permission_ids = validated_data.pop('user_permission_ids', [])

        # Note: Password will be set in the view's perform_create method
        # User is created without a password initially
        user = User.objects.create(**validated_data)

        # Set tenant groups (user.groups and user.user_permissions no longer exist - we removed PermissionsMixin)
        if tenant_group_ids:
            tenant_groups = TenantGroup.objects.filter(id__in=tenant_group_ids)
            user.tenant_groups.set(tenant_groups)

        return user


class UserUpdateSerializer(EmailUniquenessMixin, serializers.ModelSerializer):
    """Serializer for updating users"""
    group_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="List of group IDs to assign to this user"
    )
    tenant_group_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="List of tenant group IDs to assign to this user"
    )
    user_permission_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="List of permission IDs to assign directly to this user"
    )
    
    class Meta:
        model = User
        fields = [
            'email', 'first_name', 'last_name', 'role', 'status',
            'phone_number', 'job_title', 'is_active',
            'group_ids', 'tenant_group_ids', 'user_permission_ids'
        ]
        extra_kwargs = {
            # PATCH is the only update path used by the UI; keep email optional
            # so other-field updates don't have to resend it.
            'email': {'required': False},
        }
    
    def update(self, instance, validated_data):
        group_ids = validated_data.pop('group_ids', None)
        tenant_group_ids = validated_data.pop('tenant_group_ids', None)
        user_permission_ids = validated_data.pop('user_permission_ids', None)
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        if group_ids is not None:
            groups = Group.objects.filter(id__in=group_ids)
            instance.groups.set(groups)
        
        if tenant_group_ids is not None:
            tenant_groups = TenantGroup.objects.filter(id__in=tenant_group_ids)
            instance.tenant_groups.set(tenant_groups)
        
        if user_permission_ids is not None:
            permissions = Permission.objects.filter(id__in=user_permission_ids)
            instance.user_permissions.set(permissions)
        
        return instance


class BulkUserActionSerializer(serializers.Serializer):
    """Serializer for bulk user actions"""
    user_ids = serializers.ListField(child=serializers.IntegerField())
    action = serializers.ChoiceField(choices=[
        ('activate', 'Activate'),
        ('deactivate', 'Deactivate'),
        ('delete', 'Delete'),
        ('change_role', 'Change Role'),
    ])
    role = serializers.ChoiceField(choices=User.ROLE_CHOICES, required=False)
    group_id = serializers.IntegerField(required=False)


class PasswordChangeSerializer(serializers.Serializer):
    """Serializer for password change"""
    old_password = serializers.CharField(required=True)
    new_password = serializers.CharField(required=True, validators=[validate_password])
    new_password_confirm = serializers.CharField(required=True)
    
    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password_confirm']:
            raise serializers.ValidationError("New passwords don't match")
        return attrs
    
    def validate_old_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError("Old password is incorrect")
        return value


class UserLoginSerializer(serializers.Serializer):
    """Serializer for user login"""
    email = serializers.EmailField()
    password = serializers.CharField()
    
    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')
        
        if email and password:
            user = authenticate(email=email, password=password)
            if not user:
                raise serializers.ValidationError('Invalid email or password.')
            if not user.is_active:
                raise serializers.ValidationError('User account is disabled.')
            attrs['user'] = user
            return attrs
        else:
            raise serializers.ValidationError('Must include email and password.')


class NotificationSerializer(serializers.ModelSerializer):
    """Serializer for user notifications"""
    user_name = serializers.SerializerMethodField()
    time_ago = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            'id', 'user', 'user_name', 'notification_type', 'title',
            'message', 'ticket_id', 'link_url', 'metadata', 'is_read', 'read_at',
            'created_at', 'time_ago'
        ]
        read_only_fields = ['user', 'created_at', 'read_at']

    def get_user_name(self, obj):
        """Get the full name of the user"""
        return obj.user.get_full_name() if obj.user else None

    def get_time_ago(self, obj):
        """Get human-readable time ago"""
        from django.utils.timesince import timesince
        return timesince(obj.created_at)


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = ['id', 'notification_type', 'in_app', 'sound', 'push']
        read_only_fields = ['id']


# Team Chat Serializers

class UserOnlineStatusSerializer(serializers.ModelSerializer):
    """Serializer for user online status"""
    class Meta:
        model = UserOnlineStatus
        fields = ['is_online', 'last_seen']


class TeamChatUserSerializer(serializers.ModelSerializer):
    """Lightweight user serializer for team chat"""
    full_name = serializers.SerializerMethodField()
    is_online = serializers.SerializerMethodField()
    last_seen = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'full_name', 'is_online', 'last_seen']

    def get_full_name(self, obj):
        return obj.get_full_name()

    def get_is_online(self, obj):
        try:
            return obj.online_status.is_online
        except UserOnlineStatus.DoesNotExist:
            return False

    def get_last_seen(self, obj):
        try:
            return obj.online_status.last_seen
        except UserOnlineStatus.DoesNotExist:
            return None


class TeamChatMessageSerializer(serializers.ModelSerializer):
    """Serializer for team chat messages"""
    sender = TeamChatUserSerializer(read_only=True)
    sender_id = serializers.IntegerField(write_only=True, required=False)
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = TeamChatMessage
        fields = [
            'id', 'conversation', 'sender', 'sender_id',
            'message_type', 'text', 'file', 'file_url', 'file_name',
            'file_size', 'file_mime_type', 'voice_duration',
            'is_read', 'read_at', 'created_at'
        ]
        read_only_fields = ['id', 'sender', 'created_at']

    def get_file_url(self, obj):
        if obj.file:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.file.url)
            return obj.file.url
        return None

    def create(self, validated_data):
        # Remove sender_id if present, sender is set from request.user
        validated_data.pop('sender_id', None)
        validated_data['sender'] = self.context['request'].user
        return super().create(validated_data)


class TeamChatConversationSerializer(serializers.ModelSerializer):
    """Serializer for team chat conversations"""
    participants = TeamChatUserSerializer(many=True, read_only=True)
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    other_participant = serializers.SerializerMethodField()

    class Meta:
        model = TeamChatConversation
        fields = [
            'id', 'participants', 'other_participant',
            'last_message', 'unread_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_last_message(self, obj):
        last_msg = obj.get_last_message()
        if last_msg:
            return TeamChatMessageSerializer(last_msg, context=self.context).data
        return None

    def get_unread_count(self, obj):
        user = self.context.get('request').user
        return obj.get_unread_count(user)

    def get_other_participant(self, obj):
        user = self.context.get('request').user
        other = obj.get_other_participant(user)
        if other:
            return TeamChatUserSerializer(other).data
        return None


class TeamChatConversationDetailSerializer(TeamChatConversationSerializer):
    """Detailed serializer including messages"""
    messages = serializers.SerializerMethodField()

    class Meta(TeamChatConversationSerializer.Meta):
        fields = TeamChatConversationSerializer.Meta.fields + ['messages']

    def get_messages(self, obj):
        # Get messages with pagination
        messages = obj.messages.all()[:50]  # Default last 50 messages
        return TeamChatMessageSerializer(messages, many=True, context=self.context).data


class TeamChatCreateMessageSerializer(serializers.Serializer):
    """Serializer for creating a new message"""
    recipient_id = serializers.IntegerField(required=False)
    conversation_id = serializers.IntegerField(required=False)
    message_type = serializers.ChoiceField(
        choices=TeamChatMessage.MESSAGE_TYPE_CHOICES,
        default='text'
    )
    text = serializers.CharField(required=False, allow_blank=True)
    file = serializers.FileField(required=False)
    voice_duration = serializers.IntegerField(required=False)

    def validate(self, attrs):
        if not attrs.get('recipient_id') and not attrs.get('conversation_id'):
            raise serializers.ValidationError(
                "Either recipient_id or conversation_id is required"
            )

        message_type = attrs.get('message_type', 'text')
        if message_type == 'text' and not attrs.get('text'):
            raise serializers.ValidationError("Text is required for text messages")

        if message_type in ['file', 'image', 'voice'] and not attrs.get('file'):
            raise serializers.ValidationError(f"File is required for {message_type} messages")

        return attrs


class TeamChatFileUploadSerializer(serializers.Serializer):
    """Serializer for file uploads"""
    file = serializers.FileField()
    message_type = serializers.ChoiceField(
        choices=['image', 'file', 'voice'],
        default='file'
    )
    voice_duration = serializers.IntegerField(required=False)

    def validate_file(self, value):
        # File size limits
        max_sizes = {
            'image': 10 * 1024 * 1024,  # 10MB
            'file': 25 * 1024 * 1024,   # 25MB
            'voice': 5 * 1024 * 1024,   # 5MB
        }

        message_type = self.initial_data.get('message_type', 'file')
        max_size = max_sizes.get(message_type, max_sizes['file'])

        if value.size > max_size:
            raise serializers.ValidationError(
                f"File size exceeds maximum allowed ({max_size // (1024 * 1024)}MB)"
            )

        return value
