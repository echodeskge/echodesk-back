from rest_framework import serializers
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.password_validation import validate_password
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field, inline_serializer
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

    @extend_schema_field(serializers.IntegerField())
    def get_employee_count(self, obj):
        if hasattr(obj, '_employee_count'):
            return obj._employee_count
        # Nested in user rows: count every department once per request
        # instead of once per row.
        context = self.context
        if isinstance(context, dict) and context.get('request') is not None:
            counts = context.get('_department_employee_counts')
            if counts is None:
                from django.db.models import Count
                counts = dict(
                    User.objects.filter(department__isnull=False)
                    .values_list('department').annotate(n=Count('id'))
                )
                context['_department_employee_counts'] = counts
            return counts.get(obj.pk, 0)
        return obj.employees.count()


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
            'id', 'name', 'description', 'is_active', 'is_booking_staff', 'created_at', 'updated_at',
            'member_count', 'features', 'feature_keys'
        ]
        read_only_fields = ['created_at', 'updated_at', 'member_count', 'feature_keys']

    @extend_schema_field(serializers.IntegerField())
    def get_member_count(self, obj):
        # Not getattr(obj, '_member_count', obj.members.count()): the default
        # is evaluated eagerly, which ran a COUNT even for annotated rows.
        if hasattr(obj, '_member_count'):
            return obj._member_count
        return obj.members.count()

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
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
        fields = ['id', 'name', 'description', 'is_active', 'is_booking_staff', 'feature_ids']
        read_only_fields = ['id']
        extra_kwargs = {
            'is_active': {'required': False},
            'is_booking_staff': {'required': False},
        }

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
        if 'is_active' in validated_data:
            instance.is_active = validated_data['is_active']
        if 'is_booking_staff' in validated_data:
            instance.is_booking_staff = validated_data['is_booking_staff']
        instance.save()

        # Update features if provided
        if feature_ids is not None:
            from tenants.feature_models import Feature
            features = Feature.objects.filter(id__in=feature_ids, is_active=True)
            instance.features.set(features)

        return instance


class DirectoryDepartmentSerializer(serializers.ModelSerializer):
    """Department reference as embedded in directory rows."""

    class Meta:
        model = Department
        fields = ['id', 'name']
        read_only_fields = fields


class DirectoryGroupSerializer(serializers.ModelSerializer):
    """Tenant group reference as embedded in directory rows."""

    class Meta:
        model = TenantGroup
        fields = ['id', 'name']
        read_only_fields = fields


class UserDirectorySerializer(serializers.ModelSerializer):
    """
    Slim, read-only user row for the people directory (``?lite=1``).

    Everything here comes from the user row itself, ``select_related``
    ``department``/``manager`` and a ``prefetch_related('tenant_groups')``;
    there is no per-row permission or feature computation, so a page of
    rows costs a constant number of queries.
    """
    full_name = serializers.SerializerMethodField()
    department = DirectoryDepartmentSerializer(read_only=True, allow_null=True)
    manager = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)
    manager_name = serializers.SerializerMethodField()
    tenant_groups = DirectoryGroupSerializer(many=True, read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'phone_number', 'job_title', 'role', 'status',
            'is_active', 'is_staff',
            'department', 'manager', 'manager_name', 'tenant_groups',
            'last_login',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField())
    def get_full_name(self, obj):
        return obj.get_full_name()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_manager_name(self, obj):
        if obj.manager_id and obj.manager:
            return obj.manager.get_full_name() or obj.manager.email
        return None


USER_ALL_PERMISSIONS_SCHEMA = inline_serializer(
    name='UserAllPermissions',
    fields={
        'individual': serializers.ListField(child=serializers.CharField()),
        'group': serializers.ListField(child=serializers.CharField()),
        'all': serializers.ListField(child=serializers.CharField()),
    },
)


class UserSerializer(serializers.ModelSerializer):
    """Serializer for User model with permissions and groups"""
    # The model exposes get_full_name(); there is no ``full_name`` attribute,
    # so a plain ReadOnlyField was silently skipped and the key never appeared.
    full_name = serializers.CharField(source='get_full_name', read_only=True)
    permissions = serializers.SerializerMethodField()
    group_permissions = serializers.SerializerMethodField()
    all_permissions = serializers.SerializerMethodField()
    feature_keys = serializers.SerializerMethodField()
    is_booking_staff = serializers.SerializerMethodField()
    # NB: no ``groups`` field. User dropped PermissionsMixin, so Django auth
    # groups do not exist on the model and the key was never in the output.
    tenant_groups = TenantGroupSerializer(many=True, read_only=True)
    department = DepartmentSerializer(read_only=True, allow_null=True)
    department_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    # The manager is written through manager_id on the create/update
    # serializers; here it is read-only so the user list/detail show who
    # the user reports to without another round trip.
    manager = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)
    manager_name = serializers.SerializerMethodField()
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
            'manager', 'manager_name', 'preferred_language',
            'is_active', 'is_staff', 'is_booking_staff', 'date_joined', 'last_login',
            'permissions', 'group_permissions', 'all_permissions', 'feature_keys',
            'group_ids', 'tenant_groups', 'tenant_group_ids', 'user_permission_ids'
        ]
        read_only_fields = [
            'id', 'date_joined', 'last_login', 'feature_keys', 'is_booking_staff',
            'manager', 'manager_name',
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_manager_name(self, obj):
        """Display name of the user's direct manager (None when unassigned)"""
        if obj.manager_id and obj.manager:
            return obj.manager.get_full_name() or obj.manager.email
        return None

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_permissions(self, obj):
        """Get user's direct permissions"""
        return obj.get_user_permissions_list()

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_group_permissions(self, obj):
        """Get permissions from groups"""
        return obj.get_group_permissions_list()

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_feature_keys(self, obj):
        """Get all feature keys this user has access to"""
        # The subscription's keys are the same for every row; look them up
        # once per serializer context instead of once per user.
        context = self.context
        keys = context.get('_subscription_feature_keys') if isinstance(context, dict) else None
        if keys is None:
            from .models import current_subscription_feature_keys
            try:
                keys = current_subscription_feature_keys()
            except Exception:
                keys = None
            if keys is not None and isinstance(context, dict):
                context['_subscription_feature_keys'] = keys
        return obj.get_feature_keys(subscription_feature_keys=keys)

    @extend_schema_field(USER_ALL_PERMISSIONS_SCHEMA)
    def get_all_permissions(self, obj):
        """Get all permissions (user + group) as a dictionary"""
        return obj.get_all_permissions_dict()

    @extend_schema_field(serializers.BooleanField())
    def get_is_booking_staff(self, obj):
        """Check if user is assigned as booking staff"""
        return hasattr(obj, 'booking_staff')

    def update(self, instance, validated_data):
        # Django auth groups / user_permissions no longer exist on User
        # (PermissionsMixin was removed); accept and ignore the legacy ids
        # instead of raising AttributeError.
        validated_data.pop('group_ids', None)
        validated_data.pop('user_permission_ids', None)
        tenant_group_ids = validated_data.pop('tenant_group_ids', None)
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

        if tenant_group_ids is not None:
            tenant_groups = TenantGroup.objects.filter(id__in=tenant_group_ids)
            instance.tenant_groups.set(tenant_groups)

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


class ManagerIdMixin:
    """Accept ``manager_id`` and resolve it to ``manager`` for the model.

    The manager must be a user in this tenant (the schema scopes the lookup)
    and cannot be the user being edited. ``null`` clears the assignment.
    """

    def validate_manager_id(self, value):
        if value is None:
            return None
        if self.instance is not None and value == self.instance.pk:
            raise serializers.ValidationError(
                "A user cannot be their own manager."
            )
        if not User.objects.filter(pk=value).exists():
            raise serializers.ValidationError("Manager not found.")
        return value

    @staticmethod
    def pop_manager(validated_data):
        """Return (present, manager_or_None) and drop manager_id from the data."""
        if 'manager_id' not in validated_data:
            return False, None
        manager_id = validated_data.pop('manager_id')
        if manager_id is None:
            return True, None
        return True, User.objects.get(pk=manager_id)


class UserCreateSerializer(ManagerIdMixin, EmailUniquenessMixin, serializers.ModelSerializer):
    """Serializer for creating users - password is auto-generated and sent via email"""
    department_id = serializers.IntegerField(required=False, allow_null=True)
    manager_id = serializers.IntegerField(
        required=False, allow_null=True,
        help_text="ID of the user's direct manager (approves their leave requests)"
    )
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
            'role', 'status', 'phone_number', 'job_title', 'department_id', 'manager_id',
            'group_ids', 'tenant_group_ids', 'user_permission_ids'
        ]
        extra_kwargs = {
            'phone_number': {'required': False},
            'job_title': {'required': False},
        }

    def validate_department_id(self, value):
        if value is not None and not Department.objects.filter(pk=value).exists():
            raise serializers.ValidationError('Department not found.')
        return value

    def create(self, validated_data):
        group_ids = validated_data.pop('group_ids', [])
        tenant_group_ids = validated_data.pop('tenant_group_ids', [])
        user_permission_ids = validated_data.pop('user_permission_ids', [])
        has_manager, manager = self.pop_manager(validated_data)
        if has_manager:
            validated_data['manager'] = manager

        # Note: Password will be set in the view's perform_create method
        # User is created without a password initially
        user = User.objects.create(**validated_data)

        # Set tenant groups (user.groups and user.user_permissions no longer exist - we removed PermissionsMixin)
        if tenant_group_ids:
            tenant_groups = TenantGroup.objects.filter(id__in=tenant_group_ids)
            user.tenant_groups.set(tenant_groups)

        return user


class UserUpdateSerializer(ManagerIdMixin, EmailUniquenessMixin, serializers.ModelSerializer):
    """Serializer for updating users"""
    department_id = serializers.IntegerField(
        required=False, allow_null=True,
        help_text="ID of the user's department (null clears it)"
    )
    manager_id = serializers.IntegerField(
        required=False, allow_null=True,
        help_text="ID of the user's direct manager (null clears it)"
    )
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
            'phone_number', 'job_title', 'is_active', 'department_id', 'manager_id',
            'preferred_language',
            'group_ids', 'tenant_group_ids', 'user_permission_ids'
        ]
        extra_kwargs = {
            # PATCH is the only update path used by the UI; keep email optional
            # so other-field updates don't have to resend it.
            'email': {'required': False},
        }

    def validate_department_id(self, value):
        if value is not None and not Department.objects.filter(pk=value).exists():
            raise serializers.ValidationError('Department not found.')
        return value

    def update(self, instance, validated_data):
        # Django auth groups / user_permissions no longer exist on User
        # (PermissionsMixin was removed): the ids are accepted for backwards
        # compatibility and ignored, as UserCreateSerializer already does.
        validated_data.pop('group_ids', None)
        validated_data.pop('user_permission_ids', None)
        tenant_group_ids = validated_data.pop('tenant_group_ids', None)
        has_manager, manager = self.pop_manager(validated_data)
        has_department = 'department_id' in validated_data
        department_id = validated_data.pop('department_id', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if has_manager:
            instance.manager = manager
        if has_department:
            instance.department_id = department_id
        instance.save()

        if tenant_group_ids is not None:
            tenant_groups = TenantGroup.objects.filter(id__in=tenant_group_ids)
            instance.tenant_groups.set(tenant_groups)

        return instance


class BulkUserActionSerializer(serializers.Serializer):
    """Serializer for bulk user actions"""
    user_ids = serializers.ListField(child=serializers.IntegerField())
    action = serializers.ChoiceField(choices=[
        ('activate', 'Activate'),
        ('deactivate', 'Deactivate'),
        ('delete', 'Delete'),
        ('change_role', 'Change Role'),
        ('reset_password', 'Send a new password'),
        ('add_to_group', 'Add to tenant group'),
        ('remove_from_group', 'Remove from tenant group'),
    ])
    role = serializers.ChoiceField(
        choices=User.ROLE_CHOICES, required=False,
        help_text='Required for change_role.',
    )
    group_id = serializers.IntegerField(
        required=False,
        help_text='Tenant group id; required for add_to_group / remove_from_group.',
    )

    def validate(self, attrs):
        action = attrs.get('action')
        # change_role without a role keeps its historical
        # {"error": "Role is required ..."} 400 from the view.
        if action in ('add_to_group', 'remove_from_group'):
            group_id = attrs.get('group_id')
            if group_id is None:
                raise serializers.ValidationError({'group_id': 'group_id is required for this action'})
            if not TenantGroup.objects.filter(pk=group_id).exists():
                raise serializers.ValidationError({'group_id': 'Tenant group not found'})
        return attrs


class BulkUserActionResponseSerializer(serializers.Serializer):
    """Result of ``POST /api/users/bulk_action/``."""
    message = serializers.CharField()
    affected = serializers.IntegerField(help_text='Number of users the action was applied to.')
    skipped_user_ids = serializers.ListField(
        child=serializers.IntegerField(),
        help_text='Requested users left untouched (yourself, or superusers when you are not one).',
    )


class UserMessageResponseSerializer(serializers.Serializer):
    message = serializers.CharField()


class AuthDeviceSerializer(serializers.Serializer):
    """A signed-in device of the current user (``AuthDeviceToken``)."""
    id = serializers.CharField(help_text='Opaque device session id (a hash prefix; never the token itself).')
    platform = serializers.ChoiceField(choices=['web', 'ios', 'android'])
    device_name = serializers.CharField(allow_blank=True)
    created_at = serializers.DateTimeField()
    last_used_at = serializers.DateTimeField(allow_null=True)
    is_current = serializers.BooleanField(help_text='True for the device making this request.')


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


def _notification_type_keys():
    """Valid notification_type values, read at call time so types added by
    other modules are accepted without touching this serializer."""
    return {key for key, _ in Notification.NOTIFICATION_TYPES}


class NotificationPreferenceBulkItemSerializer(serializers.Serializer):
    """One item of PUT /api/notification-preferences/bulk/.

    Omitted channels keep their stored value (True for a new row).
    """
    notification_type = serializers.CharField(
        max_length=50,
        help_text='A Notification.notification_type key (see GET /api/notification-preferences/types/)',
    )
    in_app = serializers.BooleanField(required=False)
    sound = serializers.BooleanField(required=False)
    push = serializers.BooleanField(required=False)

    def validate_notification_type(self, value):
        if value not in _notification_type_keys():
            raise serializers.ValidationError(f'Unknown notification type "{value}".')
        return value


class NotificationPreferenceTypeSerializer(serializers.Serializer):
    """A notification type with the user's effective delivery preference."""
    notification_type = serializers.CharField()
    label = serializers.CharField()
    in_app = serializers.BooleanField()
    sound = serializers.BooleanField()
    push = serializers.BooleanField()
    is_customized = serializers.BooleanField(
        help_text='True when the user has a stored row for this type (otherwise defaults are shown)'
    )


# Team Chat Serializers

import mimetypes
import os

from drf_spectacular.types import OpenApiTypes


# Attachment policy for POST /api/team-chat/messages/. One hard cap for every
# type; the allow-list is by extension first (React Native and browsers always
# send a filename) with the declared content type as a fallback for
# extension-less uploads.
TEAM_CHAT_MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB

TEAM_CHAT_IMAGE_EXTENSIONS = {
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'heic', 'heif', 'avif',
}
TEAM_CHAT_AUDIO_EXTENSIONS = {
    'mp3', 'm4a', 'aac', 'wav', 'ogg', 'oga', 'opus', 'webm', 'weba', '3gp', 'amr', 'caf',
}
TEAM_CHAT_DOCUMENT_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'csv', 'txt', 'rtf', 'odt', 'ods',
    'zip',
}
TEAM_CHAT_ALLOWED_EXTENSIONS = (
    TEAM_CHAT_IMAGE_EXTENSIONS | TEAM_CHAT_AUDIO_EXTENSIONS | TEAM_CHAT_DOCUMENT_EXTENSIONS
)
TEAM_CHAT_ALLOWED_MIME_TYPES = {
    'application/pdf', 'application/zip', 'application/x-zip-compressed',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-powerpoint',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/vnd.oasis.opendocument.text',
    'application/vnd.oasis.opendocument.spreadsheet',
    'text/plain', 'text/csv', 'application/rtf',
}


def _file_extension(name):
    return os.path.splitext(name or '')[1].lstrip('.').lower()


def resolve_upload_mime_type(uploaded_file):
    """
    Best-effort MIME type for a chat attachment.

    Prefers the content type the client declared for the upload (React Native
    and browsers set it from the picked file), falling back to a guess from the
    filename, then to ``application/octet-stream``.
    """
    declared = (getattr(uploaded_file, 'content_type', '') or '').split(';')[0].strip().lower()
    if declared and declared != 'application/octet-stream':
        return declared
    guessed = mimetypes.guess_type(getattr(uploaded_file, 'name', '') or '')[0]
    return guessed or declared or 'application/octet-stream'


def validate_team_chat_file(uploaded_file, message_type='file'):
    """
    Enforce the attachment policy for a chat upload.

    Raises ``serializers.ValidationError`` when the file is too large, has a
    type outside the allow-list, or does not match the declared message type
    (an ``image`` message must carry an image, a ``voice`` message an audio
    file).
    """
    if uploaded_file.size > TEAM_CHAT_MAX_FILE_SIZE:
        raise serializers.ValidationError(
            f"File size exceeds maximum allowed ({TEAM_CHAT_MAX_FILE_SIZE // (1024 * 1024)}MB)"
        )

    ext = _file_extension(uploaded_file.name)
    mime = resolve_upload_mime_type(uploaded_file)
    is_image = ext in TEAM_CHAT_IMAGE_EXTENSIONS or (not ext and mime.startswith('image/'))
    is_audio = ext in TEAM_CHAT_AUDIO_EXTENSIONS or (
        not ext and (mime.startswith('audio/') or mime == 'video/webm')
    )
    is_document = ext in TEAM_CHAT_DOCUMENT_EXTENSIONS or (
        not ext and mime in TEAM_CHAT_ALLOWED_MIME_TYPES
    )

    if not (is_image or is_audio or is_document):
        raise serializers.ValidationError(
            "File type not allowed. Allowed: images, audio, PDF, Word, Excel, "
            "PowerPoint, text/CSV and ZIP files."
        )
    if message_type == 'image' and not is_image:
        raise serializers.ValidationError("File is not a valid image")
    if message_type == 'voice' and not is_audio:
        raise serializers.ValidationError("File is not a valid audio recording")
    return uploaded_file


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

    @extend_schema_field(OpenApiTypes.STR)
    def get_full_name(self, obj):
        return obj.get_full_name()

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_is_online(self, obj):
        try:
            return obj.online_status.is_online
        except UserOnlineStatus.DoesNotExist:
            return False

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_last_seen(self, obj):
        try:
            return obj.online_status.last_seen
        except UserOnlineStatus.DoesNotExist:
            return None


class TeamChatMessageSerializer(serializers.ModelSerializer):
    """
    Serializer for team chat messages.

    Used for every message payload (REST responses, the websocket
    ``new_message`` / ``message_sent`` frames and the conversation
    ``last_message`` / ``messages`` fields) so clients see one shape.
    ``conversation`` and ``sender`` are never writable: a message cannot be
    moved to another thread or re-attributed through an update.
    """
    sender = TeamChatUserSerializer(read_only=True)
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = TeamChatMessage
        fields = [
            'id', 'conversation', 'sender',
            'message_type', 'text', 'file', 'file_url', 'file_name',
            'file_size', 'file_mime_type', 'voice_duration',
            'is_read', 'read_at', 'created_at'
        ]
        read_only_fields = ['id', 'conversation', 'sender', 'is_read', 'read_at', 'created_at']

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_file_url(self, obj):
        if obj.file:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.file.url)
            return obj.file.url
        return None

    def create(self, validated_data):
        # sender is always the authenticated user
        validated_data['sender'] = self.context['request'].user
        return super().create(validated_data)


class TeamChatConversationSerializer(serializers.ModelSerializer):
    """
    Serializer for team chat conversations.

    The viewset annotates ``unread_for_me`` and prefetches the latest message
    (``prefetched_last_messages``) and the participants; the method fields use
    those when present and fall back to the model helpers for plain instances
    (e.g. the ``with/{user_id}`` response).
    """
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

    @extend_schema_field(TeamChatMessageSerializer(allow_null=True))
    def get_last_message(self, obj):
        prefetched = getattr(obj, 'prefetched_last_messages', None)
        if prefetched is not None:
            last_msg = prefetched[0] if prefetched else None
        else:
            last_msg = obj.get_last_message()
        if last_msg:
            return TeamChatMessageSerializer(last_msg, context=self.context).data
        return None

    @extend_schema_field(OpenApiTypes.INT)
    def get_unread_count(self, obj):
        annotated = getattr(obj, 'unread_for_me', None)
        if annotated is not None:
            return annotated
        user = self.context.get('request').user
        return obj.get_unread_count(user)

    @extend_schema_field(TeamChatUserSerializer(allow_null=True))
    def get_other_participant(self, obj):
        user = self.context.get('request').user
        # participants is prefetched by the viewset; iterating the cache avoids
        # one query per row. Plain instances fall back to a single query.
        other = next(
            (p for p in obj.participants.all() if p.id != user.id),
            None,
        )
        if other:
            return TeamChatUserSerializer(other).data
        return None


class TeamChatConversationDetailSerializer(TeamChatConversationSerializer):
    """Detailed serializer including the latest 50 messages (oldest first)."""
    messages = serializers.SerializerMethodField()

    class Meta(TeamChatConversationSerializer.Meta):
        fields = TeamChatConversationSerializer.Meta.fields + ['messages']

    @extend_schema_field(TeamChatMessageSerializer(many=True))
    def get_messages(self, obj):
        latest = list(
            obj.messages.select_related('sender', 'sender__online_status')
            .order_by('-created_at', '-id')[:50]
        )
        latest.reverse()
        return TeamChatMessageSerializer(latest, many=True, context=self.context).data


class TeamChatCreateMessageSerializer(serializers.Serializer):
    """Serializer for creating a new message (multipart/form-data)."""
    recipient_id = serializers.IntegerField(
        required=False,
        help_text='Colleague to message; the 1:1 conversation is created on demand. '
                  'Either this or conversation_id is required.',
    )
    conversation_id = serializers.IntegerField(
        required=False,
        help_text='Existing conversation to post into. Either this or recipient_id is required.',
    )
    message_type = serializers.ChoiceField(
        choices=TeamChatMessage.MESSAGE_TYPE_CHOICES,
        default='text'
    )
    text = serializers.CharField(required=False, allow_blank=True)
    file = serializers.FileField(
        required=False,
        help_text='Required for image/file/voice messages. Max 25 MB; images, audio, '
                  'PDF, Word, Excel, PowerPoint, text/CSV and ZIP are accepted.',
    )
    voice_duration = serializers.IntegerField(
        required=False, min_value=0, help_text='Voice recording length in seconds',
    )

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

        uploaded = attrs.get('file')
        if uploaded is not None:
            try:
                validate_team_chat_file(uploaded, message_type)
            except serializers.ValidationError as exc:
                raise serializers.ValidationError({'file': exc.detail})

        return attrs


class TeamChatFileUploadSerializer(serializers.Serializer):
    """
    Pre-flight validator for ``POST /api/team-chat/upload/``.

    Applies ``validate_team_chat_file`` — the one attachment policy shared
    with ``TeamChatCreateMessageSerializer`` — so a file that passes here is
    guaranteed to be accepted by ``POST /api/team-chat/messages/``.
    """
    file = serializers.FileField(
        help_text='Max 25 MB; images, audio, PDF, Word, Excel, PowerPoint, text/CSV and ZIP.',
    )
    message_type = serializers.ChoiceField(
        choices=['image', 'file', 'voice'],
        default='file'
    )
    voice_duration = serializers.IntegerField(required=False, min_value=0)

    def validate_file(self, value):
        message_type = self.initial_data.get('message_type') or 'file'
        return validate_team_chat_file(value, message_type)


class TeamChatFileUploadResultSerializer(serializers.Serializer):
    """Response of the pre-flight /api/team-chat/upload/ validator."""
    file_name = serializers.CharField()
    file_size = serializers.IntegerField()
    file_mime_type = serializers.CharField()
    message = serializers.CharField()
