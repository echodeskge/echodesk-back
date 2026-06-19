from rest_framework import viewsets, status, permissions, serializers as drf_serializers
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import transaction, IntegrityError
from django.db.models import Count
from django.utils import timezone
from django.conf import settings
import secrets
import string
import logging
from tenants.email_service import email_service
from .models import Department, TenantGroup, Notification, NotificationPreference, UserOnlineStatus, TeamChatConversation, TeamChatMessage, HiddenTeamChatConversation
from .serializers import (
    UserSerializer, UserCreateSerializer, UserUpdateSerializer,
    GroupSerializer, GroupCreateSerializer, PermissionSerializer,
    BulkUserActionSerializer, PasswordChangeSerializer, DepartmentSerializer,
    TenantGroupSerializer, TenantGroupCreateSerializer, NotificationSerializer,
    TeamChatUserSerializer, TeamChatConversationSerializer, TeamChatConversationDetailSerializer,
    TeamChatMessageSerializer, TeamChatCreateMessageSerializer, TeamChatFileUploadSerializer,
    NotificationPreferenceSerializer
)

logger = logging.getLogger(__name__)

User = get_user_model()


class DepartmentViewSet(viewsets.ModelViewSet):
    """ViewSet for managing departments"""
    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Only show active departments by default, unless explicitly requested
        if self.request.query_params.get('include_inactive') == 'true':
            qs = Department.objects.all()
        else:
            qs = Department.objects.filter(is_active=True)
        return qs.annotate(_employee_count=Count('employees'))


class PermissionViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing Django permissions"""
    queryset = Permission.objects.select_related('content_type').order_by('content_type__app_label', 'content_type__model', 'codename')
    serializer_class = PermissionSerializer
    permission_classes = [permissions.IsAuthenticated]


class GroupViewSet(viewsets.ModelViewSet):
    """ViewSet for managing Django Groups with permissions"""
    queryset = Group.objects.prefetch_related('permissions__content_type').all()
    serializer_class = GroupSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return GroupCreateSerializer
        return GroupSerializer
    
    def perform_create(self, serializer):
        # Check if user has permission to manage groups
        if not self.request.user.has_permission('manage_groups') and not self.request.user.is_staff:
            raise PermissionDenied("You don't have permission to manage groups")
        serializer.save()
    
    def perform_update(self, serializer):
        # Check if user has permission to manage groups
        if not self.request.user.has_permission('manage_groups') and not self.request.user.is_staff:
            raise PermissionDenied("You don't have permission to manage groups")
        serializer.save()
    
    def perform_destroy(self, instance):
        # Check if user has permission to manage groups
        if not self.request.user.has_permission('manage_groups') and not self.request.user.is_staff:
            raise PermissionDenied("You don't have permission to manage groups")
        instance.delete()
    
    @action(detail=True, methods=['post'])
    def add_users(self, request, pk=None):
        """Add users to this group — deprecated, use /api/tenant-groups/ instead."""
        return Response(
            {'error': 'Django auth groups are deprecated. Use /api/tenant-groups/ instead.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    @action(detail=True, methods=['post'])
    def remove_users(self, request, pk=None):
        """Remove users from this group — deprecated, use /api/tenant-groups/ instead."""
        return Response(
            {'error': 'Django auth groups are deprecated. Use /api/tenant-groups/ instead.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=False, methods=['get'])
    def available_permissions(self, request):
        """Get available permissions for groups"""
        # Get permissions from the User model's Meta.permissions
        user_content_type = ContentType.objects.get_for_model(User)
        user_permissions = Permission.objects.filter(content_type=user_content_type)
        
        # Also include some common auth permissions
        auth_content_type = ContentType.objects.get(app_label='auth', model='permission')
        
        all_permissions = user_permissions
        serializer = PermissionSerializer(all_permissions, many=True)
        return Response(serializer.data)


class UserViewSet(viewsets.ModelViewSet):
    """ViewSet for managing users"""
    queryset = User.objects.prefetch_related('tenant_groups', 'tenant_groups__features').all()
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return UserCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return UserUpdateSerializer
        return UserSerializer
    
    def get_queryset(self):
        queryset = User.objects.select_related('department', 'booking_staff').prefetch_related('tenant_groups', 'tenant_groups__features').all()

        # Filter by role if specified
        role = self.request.query_params.get('role', None)
        if role is not None:
            queryset = queryset.filter(role=role)

        # Filter by status if specified
        status_filter = self.request.query_params.get('status', None)
        if status_filter is not None:
            queryset = queryset.filter(status=status_filter)

        # Filter by group if specified
        group_id = self.request.query_params.get('group', None)
        if group_id is not None:
            queryset = queryset.filter(tenant_groups__id=group_id)

        return queryset.distinct()
    
    def perform_create(self, serializer):
        # Check if user has permission to manage users
        if not self.request.user.has_permission('manage_users') and not self.request.user.is_staff:
            raise PermissionDenied("You don't have permission to manage users")

        # Generate temporary password (12 characters: letters, digits, special chars)
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        temporary_password = ''.join(secrets.choice(alphabet) for _ in range(12))

        # Save user with temporary password and require password change on first login
        user = serializer.save(
            invited_by=self.request.user,
            invitation_sent_at=timezone.now(),
            temporary_password=temporary_password,
            password_change_required=True
        )

        # Set the password (hashed)
        user.set_password(temporary_password)
        user.save()

        # Send invitation email
        try:
            # Get tenant info and language
            tenant = self.request.tenant if hasattr(self.request, 'tenant') else None
            tenant_name = tenant.name if tenant else "EchoDesk"
            frontend_url = tenant.frontend_url if tenant else settings.MAIN_DOMAIN
            language = tenant.preferred_language if tenant else 'en'

            email_sent = email_service.send_user_invitation_email(
                user_email=user.email,
                user_name=user.get_full_name(),
                tenant_name=tenant_name,
                temporary_password=temporary_password,
                frontend_url=frontend_url,
                invited_by=self.request.user.get_full_name(),
                language=language
            )

            if email_sent:
                logger.info(f'Invitation email sent to {user.email}')
            else:
                logger.warning(f'Failed to send invitation email to {user.email}')
        except Exception as e:
            logger.error(f'Error sending invitation email to {user.email}: {str(e)}')
    
    def _reset_password_and_notify(self, user):
        """Generate a fresh temporary password for ``user``, store it, require a
        change on next login, and email it to the user's current address.

        Returns True if the email was sent. Used both by the send_new_password
        action and by perform_update when an admin changes a user's email.
        """
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        new_password = ''.join(secrets.choice(alphabet) for _ in range(12))

        user.set_password(new_password)
        user.temporary_password = new_password
        user.password_change_required = True
        user.save()

        tenant = self.request.tenant if hasattr(self.request, 'tenant') else None
        tenant_name = tenant.name if tenant else "EchoDesk"
        frontend_url = tenant.frontend_url if tenant else settings.MAIN_DOMAIN
        language = tenant.preferred_language if tenant else 'en'

        try:
            email_sent = email_service.send_new_password_email(
                user_email=user.email,
                user_name=user.get_full_name(),
                tenant_name=tenant_name,
                new_password=new_password,
                frontend_url=frontend_url,
                language=language
            )
            if email_sent:
                logger.info(f'New password email sent to {user.email}')
            else:
                logger.warning(f'Failed to send new password email to {user.email}')
            return email_sent
        except Exception as e:
            logger.error(f'Error sending new password email to {user.email}: {str(e)}')
            return False

    def perform_update(self, serializer):
        # Check if user has permission to manage users or is updating their own profile
        user_being_updated = self.get_object()
        is_privileged = (
            self.request.user.has_permission('manage_users') or
            self.request.user.is_staff
        )
        if not is_privileged and user_being_updated != self.request.user:
            raise PermissionDenied("You don't have permission to manage users")

        # If user is updating themselves and is not privileged, restrict to safe
        # fields (email is intentionally excluded, so only admins can change it).
        if not is_privileged and user_being_updated == self.request.user:
            safe_fields = {'first_name', 'last_name', 'phone_number', 'job_title'}
            for field in list(serializer.validated_data.keys()):
                if field not in safe_fields:
                    serializer.validated_data.pop(field)

        old_email = user_being_updated.email
        user = serializer.save()

        # When an admin changes a user's email, the email is their login
        # identity, so issue a fresh password and send it to the NEW address.
        if user.email and user.email != old_email:
            self._reset_password_and_notify(user)
    
    def perform_destroy(self, instance):
        # Check if user has permission to manage users
        if not self.request.user.has_permission('manage_users') and not self.request.user.is_staff:
            raise PermissionDenied("You don't have permission to manage users")
        
        # Don't allow users to delete themselves
        if instance == self.request.user:
            raise PermissionDenied("You cannot delete your own account")
        
        instance.delete()
    
    @action(detail=False, methods=['post'])
    def bulk_action(self, request):
        """Perform bulk actions on users"""
        if not request.user.has_permission('manage_users') and not request.user.is_staff:
            raise PermissionDenied('You do not have permission to manage users')
        
        serializer = BulkUserActionSerializer(data=request.data)
        if serializer.is_valid():
            user_ids = serializer.validated_data['user_ids']
            action_type = serializer.validated_data['action']
            
            users = User.objects.filter(id__in=user_ids)
            
            if action_type == 'activate':
                count = users.count()
                users.update(is_active=True, status='active')
                message = f'Activated {count} users'

            elif action_type == 'deactivate':
                count = users.count()
                users.update(is_active=False, status='inactive')
                message = f'Deactivated {count} users'

            elif action_type == 'delete':
                # Don't allow deletion of current user
                users = users.exclude(id=request.user.id)
                count = users.count()
                users.delete()
                message = f'Deleted {count} users'

            elif action_type == 'change_role':
                role = serializer.validated_data.get('role')
                if role:
                    count = users.count()
                    users.update(role=role)
                    message = f'Changed role to {role} for {count} users'
                else:
                    return Response(
                        {'error': 'Role is required for change_role action'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            return Response({'message': message})
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['post'])
    def change_password(self, request, pk=None):
        """Change user password"""
        user = self.get_object()

        # Users can only change their own password unless they have manage_users permission
        if (user != request.user and
            not request.user.has_permission('manage_users') and
            not request.user.is_staff):
            raise PermissionDenied('You can only change your own password')

        serializer = PasswordChangeSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            user.set_password(serializer.validated_data['new_password'])
            user.save()
            return Response({'message': 'Password changed successfully'})

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def send_new_password(self, request, pk=None):
        """Generate new password and email it to user"""
        # Check permission
        if not request.user.has_permission('manage_users') and not request.user.is_staff:
            raise PermissionDenied('You do not have permission to manage users')

        user = self.get_object()
        email_sent = self._reset_password_and_notify(user)

        if email_sent:
            return Response({'message': 'New password sent successfully'})
        return Response(
            {'error': 'Failed to send email'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


class TenantGroupViewSet(viewsets.ModelViewSet):
    """ViewSet for managing TenantGroups with custom permissions"""
    queryset = TenantGroup.objects.prefetch_related('members', 'features').annotate(_member_count=Count('members')).all()
    serializer_class = TenantGroupSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return TenantGroupCreateSerializer
        return TenantGroupSerializer
    
    def perform_create(self, serializer):
        # Check if user has permission to manage groups
        if not self.request.user.has_permission('manage_groups') and not self.request.user.is_staff:
            raise PermissionDenied("You don't have permission to manage groups")
        try:
            serializer.save()
        except IntegrityError:
            raise drf_serializers.ValidationError({'error': 'A group with this name already exists'})

    def perform_update(self, serializer):
        # Check if user has permission to manage groups
        if not self.request.user.has_permission('manage_groups') and not self.request.user.is_staff:
            raise PermissionDenied("You don't have permission to manage groups")
        serializer.save()

    def perform_destroy(self, instance):
        # Check if user has permission to manage groups
        if not self.request.user.has_permission('manage_groups') and not self.request.user.is_staff:
            raise PermissionDenied("You don't have permission to manage groups")
        instance.delete()

    @action(detail=True, methods=['post'])
    def add_users(self, request, pk=None):
        """Add users to this tenant group"""
        if not request.user.has_permission('manage_groups') and not request.user.is_staff:
            raise PermissionDenied('You do not have permission to manage groups')
        
        tenant_group = self.get_object()
        user_ids = request.data.get('user_ids', [])
        
        if not user_ids:
            return Response(
                {'error': 'user_ids is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        users = User.objects.filter(id__in=user_ids)
        for user in users:
            user.tenant_groups.add(tenant_group)
        
        return Response({
            'message': f'Added {len(users)} users to tenant group {tenant_group.name}',
            'added_users': [user.email for user in users]
        })
    
    @action(detail=True, methods=['post'])
    def remove_users(self, request, pk=None):
        """Remove users from this tenant group"""
        if not request.user.has_permission('manage_groups') and not request.user.is_staff:
            raise PermissionDenied('You do not have permission to manage groups')
        
        tenant_group = self.get_object()
        user_ids = request.data.get('user_ids', [])
        
        if not user_ids:
            return Response(
                {'error': 'user_ids is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        users = User.objects.filter(id__in=user_ids)
        for user in users:
            user.tenant_groups.remove(tenant_group)
        
        return Response({
            'message': f'Removed {len(users)} users from tenant group {tenant_group.name}',
            'removed_users': [user.email for user in users]
        })
    
    @action(detail=True, methods=['get'])
    def members(self, request, pk=None):
        """Get all members of this tenant group"""
        tenant_group = self.get_object()
        members = tenant_group.members.select_related('department').prefetch_related(
            'tenant_groups', 'tenant_groups__features',
        ).all()

        # Use the UserSerializer to return user data
        from .serializers import UserSerializer
        serializer = UserSerializer(members, many=True)

        return Response({
            'count': len(members),
            'group': tenant_group.name,
            'members': serializer.data
        })

    @action(detail=False, methods=['get'])
    def available_features(self, request):
        """Get all available features that can be assigned to groups based on tenant's subscription"""
        from tenants.models import TenantSubscription
        from tenants.feature_models import Feature

        # Get current tenant's available features
        try:
            # Get tenant from request
            tenant = request.tenant if hasattr(request, 'tenant') else None
            # Get optional group_id to include features already assigned to that group
            group_id = request.query_params.get('group_id')

            if tenant and hasattr(tenant, 'schema_name') and tenant.schema_name != 'public':
                # Get features available to this tenant through their subscription's selected_features
                try:
                    subscription = TenantSubscription.objects.get(tenant=tenant, is_active=True)
                    subscription_feature_ids = list(
                        subscription.selected_features.filter(is_active=True).values_list('id', flat=True)
                    )
                except TenantSubscription.DoesNotExist:
                    subscription_feature_ids = []

                # Start with subscription features
                feature_ids = subscription_feature_ids

                # If editing a group, also include features already assigned to it
                if group_id:
                    try:
                        group = TenantGroup.objects.get(id=group_id)
                        group_feature_ids = group.features.values_list('id', flat=True)
                        # Combine subscription features and group features (avoiding duplicates)
                        feature_ids = list(set(feature_ids) | set(group_feature_ids))
                    except TenantGroup.DoesNotExist:
                        pass

                # Return only the features this tenant has access to or already has in the group
                features = Feature.objects.filter(id__in=feature_ids, is_active=True)
            else:
                # For public schema or no tenant, return all active features
                features = Feature.objects.filter(is_active=True)

            # Group by category
            from tenants.feature_models import FeatureCategory
            categories_dict = {}

            for feature in features:
                if feature.category not in categories_dict:
                    categories_dict[feature.category] = {
                        'category': feature.category,
                        'category_display': feature.get_category_display(),
                        'features': []
                    }

                categories_dict[feature.category]['features'].append({
                    'id': feature.id,
                    'key': feature.key,
                    'name': feature.name,
                    'description': feature.description,
                    'icon': feature.icon,
                    'sort_order': feature.sort_order
                })

            categories_list = list(categories_dict.values())

            return Response({
                'categories': categories_list
            })

        except Exception as e:
            return Response(
                {'error': f'Failed to fetch available features: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class NotificationViewSet(viewsets.ModelViewSet):
    """ViewSet for managing user notifications"""
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """Only show notifications for the current user"""
        return Notification.objects.select_related('user').filter(user=self.request.user)

    @action(detail=False, methods=['get'])
    def unread_count(self, request):
        """Get count of unread notifications"""
        count = self.get_queryset().filter(is_read=False).count()
        return Response({'count': count})

    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """Mark a single notification as read"""
        notification = self.get_object()
        notification.mark_as_read()
        serializer = self.get_serializer(notification)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        """Mark all notifications as read"""
        updated = self.get_queryset().filter(is_read=False).update(
            is_read=True,
            read_at=timezone.now()
        )
        return Response({'updated': updated})

    @action(detail=False, methods=['delete'])
    def clear_all(self, request):
        """Clear all read notifications"""
        deleted_count, _ = self.get_queryset().filter(is_read=True).delete()
        return Response({'deleted': deleted_count})


from rest_framework.decorators import api_view, permission_classes as perm_classes


@api_view(['GET'])
@perm_classes([permissions.IsAuthenticated])
def notification_preferences_list(request):
    """List all notification preferences for the current user."""
    prefs = NotificationPreference.objects.filter(user=request.user)
    return Response(NotificationPreferenceSerializer(prefs, many=True).data)


@api_view(['PUT'])
@perm_classes([permissions.IsAuthenticated])
def notification_preferences_bulk_update(request):
    """Bulk update notification preferences. Accepts a list of {notification_type, in_app, sound, push}."""
    prefs_data = request.data  # list of {notification_type, in_app, sound, push}
    if not isinstance(prefs_data, list):
        return Response(
            {'error': 'Request body must be a list of preference objects'},
            status=status.HTTP_400_BAD_REQUEST
        )
    results = []
    for pref_data in prefs_data:
        notification_type = pref_data.get('notification_type')
        if not notification_type:
            continue
        obj, _ = NotificationPreference.objects.update_or_create(
            user=request.user,
            notification_type=notification_type,
            defaults={
                'in_app': pref_data.get('in_app', True),
                'sound': pref_data.get('sound', True),
                'push': pref_data.get('push', True),
            }
        )
        results.append(NotificationPreferenceSerializer(obj).data)
    return Response(results)


def tenant_homepage(request):
    """Simple homepage view for tenants"""
    from django.http import JsonResponse
    return JsonResponse({
        'message': 'Welcome to EchoDesk API',
        'tenant': getattr(request, 'tenant', None),
        'version': '1.0.0'
    })


# Team Chat Views

from rest_framework.views import APIView
from rest_framework.decorators import api_view
from rest_framework.parsers import MultiPartParser, FormParser
from django.db.models import Q, Max
import mimetypes


class TeamChatUserListView(APIView):
    """List all users in tenant with online status for team chat"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # Get all active users except the current user
        # Use select_related to avoid N+1 query on online_status
        users = User.objects.filter(
            is_active=True,
            status='active'
        ).select_related(
            'online_status'
        ).exclude(id=request.user.id).order_by('first_name', 'last_name', 'email')

        serializer = TeamChatUserSerializer(users, many=True)
        return Response(serializer.data)


class TeamChatConversationViewSet(viewsets.ModelViewSet):
    """ViewSet for team chat conversations"""
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """Conversations the user is part of — scoped to today's activity.

        "Recent Chats" in the sidebar shows only conversations whose last
        message was sent today (server TZ). Brand-new conversations without
        messages are also included if created/updated today so a freshly
        opened chat doesn't immediately vanish.
        """
        # Get IDs of conversations hidden by this user
        hidden_conversation_ids = HiddenTeamChatConversation.objects.filter(
            user=self.request.user
        ).values_list('conversation_id', flat=True)

        from django.utils import timezone
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

        return TeamChatConversation.objects.filter(
            participants=self.request.user
        ).exclude(
            id__in=hidden_conversation_ids
        ).prefetch_related(
            'participants', 'participants__online_status',
            'messages', 'messages__sender', 'messages__sender__online_status'
        ).annotate(
            latest_message_time=Max('messages__created_at')
        ).filter(
            Q(latest_message_time__gte=today_start)
            | Q(latest_message_time__isnull=True, updated_at__gte=today_start)
        ).order_by('-latest_message_time', '-updated_at')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return TeamChatConversationDetailSerializer
        return TeamChatConversationSerializer

    @action(detail=False, methods=['get', 'post'], url_path='with/(?P<user_id>[^/.]+)')
    def with_user(self, request, user_id=None):
        """Get or create a conversation with a specific user"""
        try:
            other_user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response(
                {'error': 'User not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Find existing conversation between these two users (atomic to prevent race condition)
        with transaction.atomic():
            conversation = TeamChatConversation.objects.filter(
                participants=request.user
            ).filter(
                participants=other_user
            ).select_for_update().first()

            if not conversation:
                # Create new conversation
                conversation = TeamChatConversation.objects.create()
                conversation.participants.add(request.user, other_user)
            else:
                # If conversation exists but was hidden by this user, unhide it
                # since user is explicitly choosing to start a chat again
                HiddenTeamChatConversation.objects.filter(
                    user=request.user,
                    conversation=conversation
                ).delete()

        serializer = TeamChatConversationDetailSerializer(
            conversation,
            context={'request': request}
        )
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """Mark all messages in conversation as read"""
        conversation = self.get_object()

        # Mark all unread messages from other participants as read
        updated = conversation.messages.filter(
            is_read=False
        ).exclude(
            sender=request.user
        ).update(
            is_read=True,
            read_at=timezone.now()
        )

        return Response({'marked_read': updated})

    @action(detail=True, methods=['delete', 'post'])
    def clear_history(self, request, pk=None):
        """Clear all messages in a conversation (affects all users)"""
        conversation = self.get_object()

        # Authorization check - only participants can clear history
        if not conversation.participants.filter(id=request.user.id).exists():
            raise PermissionDenied('Not authorized to clear this conversation')

        # Delete all messages in the conversation
        deleted_count, _ = conversation.messages.all().delete()

        return Response({
            'message': f'Cleared {deleted_count} messages',
            'deleted_count': deleted_count
        })

    @action(detail=True, methods=['post'])
    def hide_for_me(self, request, pk=None):
        """
        Hide a conversation from the current user's view only.
        This does NOT delete messages - the other user can still see the conversation.
        """
        conversation = self.get_object()

        # Authorization check - only participants can hide
        if not conversation.participants.filter(id=request.user.id).exists():
            raise PermissionDenied('Not authorized to hide this conversation')

        # Create or get the hidden record
        hidden, created = HiddenTeamChatConversation.objects.get_or_create(
            user=request.user,
            conversation=conversation
        )

        return Response({
            'success': True,
            'message': 'Conversation hidden from your view',
            'created': created
        })

    @action(detail=True, methods=['post'])
    def unhide(self, request, pk=None):
        """
        Unhide a previously hidden conversation.
        """
        conversation = self.get_object()

        # Delete the hidden record
        deleted_count, _ = HiddenTeamChatConversation.objects.filter(
            user=request.user,
            conversation=conversation
        ).delete()

        return Response({
            'success': True,
            'message': 'Conversation restored to your view',
            'was_hidden': deleted_count > 0
        })


class TeamChatMessageViewSet(viewsets.ModelViewSet):
    """ViewSet for team chat messages"""
    serializer_class = TeamChatMessageSerializer
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        """Only show messages from conversations the user is part of"""
        return TeamChatMessage.objects.filter(
            conversation__participants=self.request.user
        ).select_related('sender', 'sender__online_status', 'conversation')

    def create(self, request, *args, **kwargs):
        """Create a new message"""
        serializer = TeamChatCreateMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        recipient_id = data.get('recipient_id')
        conversation_id = data.get('conversation_id')

        # Get or create conversation
        if conversation_id:
            try:
                conversation = TeamChatConversation.objects.get(
                    id=conversation_id,
                    participants=request.user
                )
            except TeamChatConversation.DoesNotExist:
                return Response(
                    {'error': 'Conversation not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
        else:
            # Get recipient and find/create conversation
            try:
                recipient = User.objects.get(id=recipient_id)
            except User.DoesNotExist:
                return Response(
                    {'error': 'Recipient not found'},
                    status=status.HTTP_404_NOT_FOUND
                )

            conversation = TeamChatConversation.objects.filter(
                participants=request.user
            ).filter(
                participants=recipient
            ).first()

            if not conversation:
                conversation = TeamChatConversation.objects.create()
                conversation.participants.add(request.user, recipient)

        # Create message
        message_data = {
            'conversation': conversation,
            'sender': request.user,
            'message_type': data.get('message_type', 'text'),
            'text': data.get('text', ''),
        }

        # Handle file upload
        file = data.get('file')
        if file:
            message_data['file'] = file
            message_data['file_name'] = file.name
            message_data['file_size'] = file.size
            message_data['file_mime_type'] = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'

        # Handle voice duration
        voice_duration = data.get('voice_duration')
        if voice_duration:
            message_data['voice_duration'] = voice_duration

        message = TeamChatMessage.objects.create(**message_data)

        # Update conversation timestamp
        conversation.save()  # This triggers auto_now on updated_at

        # Return serialized message
        response_serializer = TeamChatMessageSerializer(
            message,
            context={'request': request}
        )

        # Send WebSocket notification
        self._notify_recipient(conversation, message)

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    def _notify_recipient(self, conversation, message):
        """Send WebSocket notification to recipient"""
        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync

            channel_layer = get_channel_layer()
            if not channel_layer:
                return

            # Get recipient (other participant)
            recipient = conversation.get_other_participant(message.sender)
            if not recipient:
                return

            # Get tenant schema
            from django.db import connection
            tenant_schema = connection.schema_name

            # Send to recipient's channel group
            group_name = f"team_chat_{tenant_schema}_{recipient.id}"

            message_data = TeamChatMessageSerializer(message).data

            async_to_sync(channel_layer.group_send)(
                group_name,
                {
                    'type': 'chat_message',
                    'message': message_data,
                    'conversation_id': conversation.id,
                }
            )
        except (ConnectionError, OSError) as e:
            # Connection reset by peer (errno 104) and similar transient errors
            # are expected when clients disconnect before receiving the message
            logger.warning(f"WebSocket notification could not be delivered (client may have disconnected): {e}")
        except Exception as e:
            logger.error(f"Failed to send WebSocket notification: {e}")

    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """Mark a single message as read"""
        message = self.get_object()

        # Only mark if user is not the sender
        if message.sender != request.user:
            message.mark_as_read()

        serializer = self.get_serializer(message)
        return Response(serializer.data)


@api_view(['POST'])
def upload_team_chat_file(request):
    """Upload a file for team chat"""
    serializer = TeamChatFileUploadSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    file = serializer.validated_data['file']
    message_type = serializer.validated_data.get('message_type', 'file')

    # Determine content type
    content_type = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'

    # For images, verify it's actually an image
    if message_type == 'image' and not content_type.startswith('image/'):
        return Response(
            {'error': 'File is not a valid image'},
            status=status.HTTP_400_BAD_REQUEST
        )

    return Response({
        'file_name': file.name,
        'file_size': file.size,
        'file_mime_type': content_type,
        'message': 'File validated successfully. Include this file in your message creation request.'
    })


@api_view(['GET'])
def team_chat_unread_count(request):
    """Get total unread message count for team chat"""
    count = TeamChatMessage.objects.filter(
        conversation__participants=request.user,
        is_read=False
    ).exclude(
        sender=request.user
    ).count()

    return Response({'count': count})
