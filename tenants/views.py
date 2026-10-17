from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action, api_view, permission_classes, throttle_classes
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from rest_framework.throttling import ScopedRateThrottle
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.db import transaction
from django.views.decorators.csrf import ensure_csrf_cookie
from django.contrib.auth import authenticate
from tenant_schemas.utils import get_public_schema_name, schema_context
from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse, inline_serializer
from rest_framework import serializers as drf_serializers
from drf_spectacular.openapi import OpenApiTypes
from .models import Tenant, PendingRegistration, PaymentOrder, TenantDomain, DashboardAppearanceSettings
from .feature_models import Feature
from .serializers import (
    TenantSerializer, TenantCreateSerializer, TenantRegistrationSerializer,
    TenantLoginSerializer, TenantDashboardDataSerializer, DashboardAppearanceSettingsSerializer,
    TenantSettingsSerializer,
)
from .services import SingleFrontendDeploymentService, TenantConfigAPI
from .bog_payment import bog_service
from .permissions import get_subscription_info
from .security_service import SecurityService
from users.models import AuthDeviceToken
from django.contrib.auth.hashers import make_password
import logging
import uuid

logger = logging.getLogger(__name__)
User = get_user_model()


class AuthRateThrottle(ScopedRateThrottle):
    scope = 'auth'


MOBILE_PLATFORMS = ('ios', 'android')


def _issue_login_token(user, data):
    """
    Return ``(key, token_type)`` for a freshly authenticated user.

    Mobile clients (``platform`` ios/android) get a per-device
    ``AuthDeviceToken`` so each phone can be logged out on its own; everything
    else keeps the single shared DRF ``Token`` the web dashboard relies on.
    """
    platform = data.get('platform') or 'web'
    if platform in MOBILE_PLATFORMS:
        device = AuthDeviceToken.issue(
            user,
            platform=platform,
            device_id=(data.get('device_id') or '')[:255],
            device_name=(data.get('device_name') or '')[:150],
        )
        return device.key, 'device'

    token, _created = Token.objects.get_or_create(user=user)
    return token.key, 'shared'


@extend_schema(
    operation_id='tenant_login',
    summary='Tenant Login',
    description='Authenticate a user within a specific tenant and get dashboard data. This endpoint only works from tenant subdomains (*.api.echodesk.ge).',
    request=TenantLoginSerializer,
    responses={
        200: OpenApiResponse(
            description='Login successful',
            response={
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'},
                    'token': {'type': 'string'},
                    'token_type': {'type': 'string', 'enum': ['shared', 'device']},
                    'dashboard_data': {'type': 'object'}
                }
            }
        ),
        400: OpenApiResponse(description='Invalid credentials or validation errors'),
        403: OpenApiResponse(description='Not available from main domain')
    },
    tags=['Authentication']
)
@api_view(['POST'])
@permission_classes([])  # No authentication required for login
@throttle_classes([AuthRateThrottle])
def tenant_login(request):
    """
    Tenant-specific login endpoint for dashboard access
    """
    from django.utils import timezone

    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Get the attempted email for logging
    attempted_email = request.data.get('email', '')
    client_ip = SecurityService.get_client_ip(request)

    # Try to find the user first to check if they're superuser (for IP bypass check)
    potential_user = None
    try:
        potential_user = User.objects.get(email=attempted_email)
    except User.DoesNotExist:
        pass

    # Check IP whitelist BEFORE authentication
    is_superuser = potential_user.is_superuser if potential_user else False
    if not SecurityService.is_ip_whitelisted(request.tenant, client_ip, is_superuser):
        # Log failed login due to IP block
        SecurityService.log_security_event(
            event_type='login_failed',
            request=request,
            user=potential_user,
            attempted_email=attempted_email,
            failure_reason=f'IP address {client_ip} is not whitelisted'
        )
        return Response(
            {'error': 'Access denied. Your IP address is not allowed.'},
            status=status.HTTP_403_FORBIDDEN
        )

    serializer = TenantLoginSerializer(data=request.data, context={'request': request})
    if serializer.is_valid():
        user = serializer.validated_data['user']

        # Check if user must change password
        if user.password_change_required:
            return Response({
                'message': 'Password change required',
                'password_change_required': True,
                'user_id': user.id,
                'email': user.email
            }, status=status.HTTP_403_FORBIDDEN)

        # Shared web token, or a per-device token for mobile clients
        token_key, token_type = _issue_login_token(user, serializer.validated_data)

        # Update last_login
        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])

        # Log successful login
        SecurityService.log_security_event(
            event_type='login_success',
            request=request,
            user=user
        )

        # Get dashboard data
        dashboard_serializer = TenantDashboardDataSerializer(user, context={'request': request})

        return Response({
            'message': 'Login successful',
            'token': token_key,
            'token_type': token_type,
            'dashboard_data': dashboard_serializer.data
        }, status=status.HTTP_200_OK)

    # Log failed login (invalid credentials)
    SecurityService.log_security_event(
        event_type='login_failed',
        request=request,
        user=potential_user,
        attempted_email=attempted_email,
        failure_reason='Invalid email or password'
    )

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    operation_id='tenant_logout',
    summary='Tenant Logout',
    description='Logout the current user and invalidate their authentication token. This endpoint only works from tenant subdomains.',
    responses={
        200: OpenApiResponse(
            description='Logout successful',
            response={
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'}
                }
            }
        ),
        400: OpenApiResponse(description='Error during logout'),
        403: OpenApiResponse(description='Not available from main domain')
    },
    tags=['Authentication']
)
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def tenant_logout(request):
    """
    Tenant-specific logout endpoint
    """
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    try:
        # Log logout event before deleting token
        SecurityService.log_security_event(
            event_type='logout',
            request=request,
            user=request.user
        )

        # A device token logs out just that device; the shared web token
        # logs out every web session.
        if isinstance(request.auth, AuthDeviceToken):
            request.auth.delete()
        else:
            request.user.auth_token.delete()
        return Response({
            'message': 'Successfully logged out'
        }, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f"Error during logout: {str(e)}")
        return Response(
            {'error': 'Error during logout'},
            status=status.HTTP_400_BAD_REQUEST
        )


@extend_schema(
    operation_id='tenant_dashboard',
    summary='Get Tenant Dashboard Data',
    description='Get comprehensive dashboard data including tenant information, user details, and statistics. Requires authentication.',
    responses={
        200: TenantDashboardDataSerializer,
        403: OpenApiResponse(description='Not available from main domain')
    },
    tags=['Dashboard']
)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def tenant_dashboard(request):
    """
    Get tenant dashboard data for authenticated users
    """
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'}, 
            status=status.HTTP_403_FORBIDDEN
        )
    
    serializer = TenantDashboardDataSerializer(request.user, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@extend_schema(
    operation_id='tenant_profile',
    summary='Get User Profile',
    description='Get current user profile information including groups and permissions within the tenant context.',
    responses={
        200: OpenApiResponse(
            description='User profile data with groups and permissions',
            response={
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer'},
                    'email': {'type': 'string'},
                    'first_name': {'type': 'string'},
                    'last_name': {'type': 'string'},
                    'is_staff': {'type': 'boolean'},
                    'is_superuser': {'type': 'boolean'},
                    'is_leave_manager': {
                        'type': 'boolean',
                        'description': 'True when the user can open the leave management manager views (staff, manager role, or has direct reports)'
                    },
                    'date_joined': {'type': 'string', 'format': 'date-time'},
                    'last_login': {'type': 'string', 'format': 'date-time'},
                    'is_active': {'type': 'boolean'},
                    'groups': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'id': {'type': 'integer'},
                                'name': {'type': 'string'},
                                'feature_keys': {
                                    'type': 'array',
                                    'items': {'type': 'string'},
                                    'description': 'Active feature keys granted by this group',
                                },
                                'permissions': {
                                    'description': 'Legacy: not returned by this endpoint',
                                    'type': 'array',
                                    'items': {
                                        'type': 'object',
                                        'properties': {
                                            'id': {'type': 'integer'},
                                            'codename': {'type': 'string'},
                                            'name': {'type': 'string'},
                                            'app_label': {'type': 'string'},
                                            'model': {'type': 'string'}
                                        }
                                    }
                                }
                            }
                        }
                    },
                    'all_permissions': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'List of all permission codenames user has'
                    },
                    'permissions_list': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': (
                            'Sorted flat list of every tenant permission codename the user '
                            'holds: individual can_* flags, role grants (manager: '
                            'view_all_tickets, edit_all_tickets, delete_tickets, assign_tickets, '
                            'view_reports, export_data, make_calls, manage_tags, manage_columns, '
                            'view_boards, create_boards, edit_boards, view_social_messages, '
                            'send_social_messages; admin additionally manage_users, manage_groups, '
                            'manage_settings, delete_boards, manage_social_connections, '
                            'manage_social_settings). Superusers get all. Same values as '
                            'all_permissions, sorted. is_staff is NOT folded in: endpoints that accept staff check '
                            'is_staff separately.'
                        ),
                    },
                    'feature_keys': {'type': 'array', 'items': {'type': 'string'}},
                }
            }
        ),
        403: OpenApiResponse(description='Not available from main domain')
    },
    tags=['User Profile']
)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def tenant_profile(request):
    """
    Get current user profile information including groups and permissions
    """
    from django.db.models import Prefetch
    from django.db import connection

    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    user = request.user

    # Prefetch tenant_groups with features to avoid N+1 queries
    groups_data = []
    group_feature_keys = set()
    prefetched_groups = []
    try:
        # Single query with prefetched features
        tenant_groups = user.tenant_groups.filter(
            is_active=True
        ).prefetch_related(
            Prefetch(
                'features',
                queryset=Feature.objects.filter(is_active=True),
                to_attr='prefetched_features'
            )
        )

        for group in tenant_groups:
            prefetched_groups.append(group)
            # Use prefetched features instead of calling get_feature_keys()
            feature_keys_list = [f.key for f in group.prefetched_features]
            groups_data.append({
                'id': group.id,
                'name': group.name,
                'feature_keys': feature_keys_list
            })
            group_feature_keys.update(feature_keys_list)
    except Exception:
        # In case of any tenant_groups query issues
        pass

    # Calculate permissions efficiently without N+1 queries
    # Use the already-fetched groups instead of querying again
    all_permissions = _compute_permissions_efficiently(user, prefetched_groups)

    # Calculate feature_keys efficiently (avoid redundant queries)
    feature_keys = []
    try:
        tenant = Tenant.objects.get(schema_name=connection.schema_name)
        subscription = tenant.current_subscription
        if subscription and subscription.is_active:
            subscription_feature_keys = set(
                subscription.selected_features.filter(
                    is_active=True
                ).values_list('key', flat=True)
            )

            if user.is_superuser:
                feature_keys = list(subscription_feature_keys)
            elif group_feature_keys:
                # Intersection of subscription features and group features
                feature_keys = list(group_feature_keys & subscription_feature_keys)
    except Exception:
        pass

    # Whether to show the leave "team" views (mobile reads this on login)
    from leave_management.utils import is_leave_manager

    return Response({
        'id': user.id,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'is_staff': user.is_staff,
        'is_superuser': user.is_superuser,
        'is_leave_manager': is_leave_manager(user),
        'date_joined': user.date_joined,
        'last_login': user.last_login,
        'is_active': user.is_active,
        'groups': groups_data,
        'all_permissions': list(all_permissions),
        'permissions_list': sorted(all_permissions),
        'feature_keys': feature_keys
    }, status=status.HTTP_200_OK)


def _compute_permissions_efficiently(user, prefetched_groups):
    """
    Compute user permissions without N+1 queries.
    Uses already-fetched groups instead of querying for each permission check.
    """
    if user.is_superuser:
        # Superuser has all permissions
        return [
            'view_all_tickets', 'manage_users', 'make_calls', 'manage_groups',
            'manage_settings', 'create_tickets', 'edit_own_tickets',
            'edit_all_tickets', 'delete_tickets', 'assign_tickets',
            'view_reports', 'export_data', 'manage_tags', 'manage_columns',
            'view_boards', 'create_boards', 'edit_boards', 'delete_boards',
            'access_orders', 'manage_social_connections', 'view_social_messages',
            'send_social_messages', 'manage_social_settings'
        ]

    permissions = set()

    # Permission fields to check
    permission_fields = [
        'view_all_tickets', 'manage_users', 'make_calls', 'manage_groups',
        'manage_settings', 'create_tickets', 'edit_own_tickets',
        'edit_all_tickets', 'delete_tickets', 'assign_tickets',
        'view_reports', 'export_data', 'manage_tags', 'manage_columns',
        'view_boards', 'create_boards', 'edit_boards', 'delete_boards',
        'access_orders', 'manage_social_connections', 'view_social_messages',
        'send_social_messages', 'manage_social_settings'
    ]

    # Individual user permissions (direct attributes)
    individual_permissions = {
        'view_all_tickets': getattr(user, 'can_view_all_tickets', False),
        'manage_users': getattr(user, 'can_manage_users', False),
        'make_calls': getattr(user, 'can_make_calls', False),
        'manage_groups': getattr(user, 'can_manage_groups', False),
        'manage_settings': getattr(user, 'can_manage_settings', False),
        'create_tickets': getattr(user, 'can_create_tickets', False),
        'edit_own_tickets': getattr(user, 'can_edit_own_tickets', False),
        'edit_all_tickets': getattr(user, 'can_edit_all_tickets', False),
        'delete_tickets': getattr(user, 'can_delete_tickets', False),
        'assign_tickets': getattr(user, 'can_assign_tickets', False),
        'view_reports': getattr(user, 'can_view_reports', False),
        'export_data': getattr(user, 'can_export_data', False),
        'manage_tags': getattr(user, 'can_manage_tags', False),
        'manage_columns': getattr(user, 'can_manage_columns', False),
        'view_boards': getattr(user, 'can_view_boards', False),
        'create_boards': getattr(user, 'can_create_boards', False),
        'edit_boards': getattr(user, 'can_edit_boards', False),
        'delete_boards': getattr(user, 'can_delete_boards', False),
        'access_orders': getattr(user, 'can_access_orders', False),
        'manage_social_connections': getattr(user, 'can_manage_social_connections', False),
        'view_social_messages': getattr(user, 'can_view_social_messages', False),
        'send_social_messages': getattr(user, 'can_send_social_messages', False),
        'manage_social_settings': getattr(user, 'can_manage_social_settings', False),
    }

    # Role-based permissions
    role_permissions = {
        'view_all_tickets': getattr(user, 'is_manager', False),
        'manage_users': getattr(user, 'is_admin', False),
        'make_calls': getattr(user, 'is_manager', False),
        'manage_groups': getattr(user, 'is_admin', False),
        'manage_settings': getattr(user, 'is_admin', False),
        'edit_all_tickets': getattr(user, 'is_manager', False),
        'delete_tickets': getattr(user, 'is_manager', False),
        'assign_tickets': getattr(user, 'is_manager', False),
        'view_reports': getattr(user, 'is_manager', False),
        'export_data': getattr(user, 'is_manager', False),
        'manage_tags': getattr(user, 'is_manager', False),
        'manage_columns': getattr(user, 'is_manager', False),
        'view_boards': getattr(user, 'is_manager', False),
        'create_boards': getattr(user, 'is_manager', False),
        'edit_boards': getattr(user, 'is_manager', False),
        'delete_boards': getattr(user, 'is_admin', False),
        'manage_social_connections': getattr(user, 'is_admin', False),
        'view_social_messages': getattr(user, 'is_manager', False),
        'send_social_messages': getattr(user, 'is_manager', False),
        'manage_social_settings': getattr(user, 'is_admin', False),
    }

    for perm in permission_fields:
        # Check individual permission
        if individual_permissions.get(perm, False):
            permissions.add(perm)
            continue

        # Check role-based permission
        if role_permissions.get(perm, False):
            permissions.add(perm)
            continue

        # Check group permissions (using already-fetched groups, no new queries)
        group_perm_field = f'can_{perm}'
        for group in prefetched_groups:
            if getattr(group, group_perm_field, False):
                permissions.add(perm)
                break

    return list(permissions)


@extend_schema(
    operation_id='get_subscription_me',
    summary='Get Current Subscription',
    description='Get the current tenant subscription information including features, limits, and usage.',
    responses={
        200: OpenApiResponse(
            description='Subscription information',
            response={
                'type': 'object',
                'properties': {
                    'has_subscription': {'type': 'boolean'},
                    'subscription': {
                        'type': 'object',
                        'properties': {
                            'is_active': {'type': 'boolean'},
                            'starts_at': {'type': 'string', 'format': 'date-time'},
                            'expires_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                            'monthly_cost': {'type': 'number'},
                            'agent_count': {'type': 'integer'},
                            'subscription_type': {'type': 'string'},
                            'is_trial': {'type': 'boolean'},
                            'trial_ends_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                            'next_billing_date': {'type': 'string', 'format': 'date-time', 'nullable': True},
                        }
                    },
                    'features': {'type': 'object'},
                    'selected_features': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'id': {'type': 'integer'},
                                'key': {'type': 'string'},
                                'name': {'type': 'string'},
                                'price_per_user_gel': {'type': 'number'},
                                'category': {'type': 'string'},
                                'description': {'type': 'string'},
                            }
                        }
                    },
                    'limits': {
                        'type': 'object',
                        'properties': {
                            'max_users': {'type': 'integer', 'nullable': True},
                            'max_whatsapp_messages': {'type': 'integer'},
                            'max_storage_gb': {'type': 'integer'},
                        }
                    },
                    'usage': {
                        'type': 'object',
                        'properties': {
                            'current_users': {'type': 'integer'},
                            'whatsapp_messages_used': {'type': 'integer'},
                            'storage_used_gb': {'type': 'number'},
                        }
                    },
                    'usage_limits': {'type': 'object'},
                }
            }
        ),
        403: OpenApiResponse(description='Not available from main domain')
    },
    tags=['Subscription']
)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_subscription_me(request):
    """
    Get the current tenant's subscription information.

    Returns complete subscription details including:
    - Subscription status (active, trial, expires_at)
    - Available features
    - Usage limits and current usage
    """
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    subscription_info = get_subscription_info(request)
    return Response(subscription_info, status=status.HTTP_200_OK)


@extend_schema(
    operation_id='update_tenant_profile',
    summary='Update User Profile',
    description='Update current user profile information (first_name and last_name only).',
    # Raw schema dicts must sit under a media type, or drf-spectacular reads
    # their keys ('type', 'properties') as media types.
    request={'application/json': {
        'type': 'object',
        'properties': {
            'first_name': {'type': 'string'},
            'last_name': {'type': 'string'}
        }
    }},
    responses={
        200: OpenApiResponse(
            description='Profile updated successfully',
            response={
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'},
                    'user': {'type': 'object'}
                }
            }
        ),
        403: OpenApiResponse(description='Not available from main domain')
    },
    tags=['User Profile']
)
@api_view(['PUT', 'PATCH'])
@permission_classes([permissions.IsAuthenticated])
def update_tenant_profile(request):
    """
    Update current user profile information
    """
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'}, 
            status=status.HTTP_403_FORBIDDEN
        )
    
    user = request.user
    data = request.data
    
    # Update allowed fields
    if 'first_name' in data:
        user.first_name = data['first_name']
    if 'last_name' in data:
        user.last_name = data['last_name']
    
    user.save()
    
    return Response({
        'message': 'Profile updated successfully',
        'user': {
            'id': user.id,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'is_staff': user.is_staff,
            'is_superuser': user.is_superuser
        }
    }, status=status.HTTP_200_OK)


@extend_schema(
    operation_id='change_tenant_password',
    summary='Change User Password',
    description=(
        'Change password for the current user. Requires old password for verification. '
        'All existing tokens (web and every device) are invalidated, so sign in again. '
        '`old_password` and `new_password` are both required (400 otherwise); they are '
        'optional in the schema only so the dashboard\'s existing call keeps compiling.'
    ),
    request=inline_serializer(
        name='ChangeTenantPassword',
        fields={
            'old_password': drf_serializers.CharField(required=False),
            'new_password': drf_serializers.CharField(required=False, min_length=8),
        },
    ),
    responses={
        200: OpenApiResponse(
            description='Password changed successfully',
            response={
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'}
                }
            }
        ),
        400: OpenApiResponse(description='Invalid old password or validation errors'),
        403: OpenApiResponse(description='Not available from main domain')
    },
    tags=['User Profile']
)
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
@throttle_classes([AuthRateThrottle])
def change_tenant_password(request):
    """
    Change password for current user in tenant
    """
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'}, 
            status=status.HTTP_403_FORBIDDEN
        )
    
    old_password = request.data.get('old_password')
    new_password = request.data.get('new_password')
    
    if not old_password or not new_password:
        return Response(
            {'error': 'Both old_password and new_password are required'}, 
            status=status.HTTP_400_BAD_REQUEST
        )
    
    user = request.user
    
    # Check old password
    if not user.check_password(old_password):
        return Response(
            {'error': 'Invalid old password'}, 
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Validate new password using Django's password validators
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError
    try:
        validate_password(new_password, user=user)
    except ValidationError as e:
        return Response(
            {'error': e.messages[0]},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Set new password
    user.set_password(new_password)
    user.save()
    
    # Delete all existing tokens (web + every device) to force re-login
    Token.objects.filter(user=user).delete()
    AuthDeviceToken.objects.filter(user=user).delete()
    
    return Response({
        'message': 'Password changed successfully. Please login again.'
    }, status=status.HTTP_200_OK)


@extend_schema(
    operation_id='get_tenant_language',
    summary='Get Tenant Language',
    description='Get the preferred language setting for the current tenant.',
    responses={
        200: OpenApiResponse(
            description='Tenant language information',
            response={
                'type': 'object',
                'properties': {
                    'preferred_language': {'type': 'string'},
                    'tenant_name': {'type': 'string'},
                    'schema_name': {'type': 'string'}
                }
            }
        ),
        403: OpenApiResponse(description='Not available from main domain')
    },
    tags=['Tenant Configuration']
)
@api_view(['GET'])
@permission_classes([])  # No authentication required
def get_tenant_language(request):
    """
    Get the preferred language for the current tenant
    """
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'}, 
            status=status.HTTP_403_FORBIDDEN
        )
    
    return Response({
        'preferred_language': request.tenant.preferred_language,
        'tenant_name': request.tenant.name,
        'schema_name': request.tenant.schema_name
    })


@api_view(['PUT', 'PATCH'])
@permission_classes([permissions.IsAuthenticated])
def update_tenant_language(request):
    """
    Update the preferred language for the current tenant.

    The language is tenant-wide, so this needs the same permission as
    PATCH /api/tenant-settings/ (staff, superuser or ``manage_settings``).
    """
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'}, 
            status=status.HTTP_403_FORBIDDEN
        )

    if not _can_manage_tenant_settings(request.user):
        return Response(_SETTINGS_FORBIDDEN, status=status.HTTP_403_FORBIDDEN)

    language = request.data.get('preferred_language')
    
    # Validate language choice
    valid_languages = ['en', 'ru', 'ka']
    if language not in valid_languages:
        return Response(
            {'error': f'Invalid language. Must be one of: {", ".join(valid_languages)}'}, 
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Update tenant language
    request.tenant.preferred_language = language
    request.tenant.save()
    
    return Response({
        'message': 'Language preference updated successfully',
        'preferred_language': request.tenant.preferred_language,
        'tenant_name': request.tenant.name
    })


def public_homepage(request):
    """
    Homepage view for the public schema (main domain)
    Redirects directly to the admin panel
    """
    return redirect('/admin/')


@ensure_csrf_cookie
def register_tenant_form(request):
    """
    Serve the tenant registration form
    """
    # Only allow access from public schema
    if hasattr(request, 'tenant') and request.tenant.schema_name != get_public_schema_name():
        return JsonResponse(
            {'error': 'Registration is only available from the main domain'}, 
            status=403
        )
    
    return render(request, 'tenants/register.html')


@extend_schema(
    operation_id='register_tenant_with_payment',
    summary='Register Tenant with Payment',
    description='Initiate tenant registration with payment. Creates a pending registration and returns BOG payment URL.',
    request=TenantRegistrationSerializer,
    responses={
        200: OpenApiResponse(
            description='Payment initiated successfully',
            response={
                'type': 'object',
                'properties': {
                    'payment_url': {'type': 'string'},
                    'order_id': {'type': 'string'},
                    'amount': {'type': 'number'},
                    'currency': {'type': 'string'}
                }
            }
        ),
        400: OpenApiResponse(description='Validation errors'),
        403: OpenApiResponse(description='Only available from main domain')
    },
    tags=['Tenant Management']
)
@api_view(['POST'])
@permission_classes([])
def register_tenant_with_payment(request):
    """
    Register a new tenant with payment requirement.
    Creates a pending registration and initiates BOG payment.
    """
    # Only allow access from public schema
    if hasattr(request, 'tenant') and request.tenant.schema_name != get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from the main domain'},
            status=status.HTTP_403_FORBIDDEN
        )

    serializer = TenantRegistrationSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    validated_data = serializer.validated_data

    try:
        import uuid
        from decimal import Decimal
        from .payment_providers import get_payment_provider

        with transaction.atomic():
            # Generate schema name
            domain_name = validated_data['domain']
            schema_name = domain_name.lower().replace('-', '_')

            # Check if schema name already exists
            if Tenant.objects.filter(schema_name=schema_name).exists():
                return Response(
                    {'error': 'A tenant with this domain already exists'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Check if pending registration exists
            if PendingRegistration.objects.filter(schema_name=schema_name, is_processed=False).exists():
                return Response(
                    {'error': 'A registration for this domain is already pending'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Feature-based pricing
            agent_count = validated_data.get('agent_count', 10)
            feature_ids = validated_data.get('feature_ids', [])
            selected_features = Feature.objects.filter(id__in=feature_ids, is_active=True)

            if not selected_features.exists():
                return Response(
                    {'error': 'At least one valid feature must be selected'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Calculate monthly subscription amount from features
            subscription_amount = sum(
                float(feature.price_per_user_gel) * agent_count
                for feature in selected_features
            )

            # Determine payment provider (from request or default to 'bog')
            payment_provider_name = validated_data.get('payment_provider', request.data.get('payment_provider', 'bog'))
            if payment_provider_name not in ('bog', 'paddle'):
                payment_provider_name = 'bog'

            provider = get_payment_provider(provider_name=payment_provider_name)

            # Generate unique order ID
            order_id = f"REG-{uuid.uuid4().hex[:12].upper()}"

            # Create pending registration
            pending_registration = PendingRegistration.objects.create(
                schema_name=schema_name,
                name=validated_data['company_name'],
                admin_email=validated_data['admin_email'],
                admin_password=make_password(validated_data['admin_password']),
                admin_first_name=validated_data['admin_first_name'],
                admin_last_name=validated_data['admin_last_name'],
                preferred_language=validated_data.get('preferred_language', 'en'),
                agent_count=agent_count,
                order_id=order_id,
                payment_provider=payment_provider_name,
            )

            # Add selected features
            if selected_features:
                pending_registration.selected_features.set(selected_features)

            # Determine currency based on provider
            currency = 'USD' if payment_provider_name == 'paddle' else 'GEL'

            # Create payment order for first month subscription (without tenant)
            payment_order = PaymentOrder.objects.create(
                order_id=order_id,
                tenant=None,  # No tenant yet
                amount=subscription_amount,  # Charge first month upfront
                currency=currency,
                agent_count=agent_count,
                status='pending',
                is_trial_payment=False,
                payment_provider=payment_provider_name,
                metadata={
                    'registration': True,
                    'schema_name': schema_name,
                    'company_name': validated_data['company_name'],
                    'admin_email': validated_data['admin_email'],
                    'subscription_amount': subscription_amount,
                    'is_custom': True,  # Feature-based pricing
                    'feature_ids': list(selected_features.values_list('id', flat=True)),
                    'agent_count': agent_count,
                }
            )

            # Determine callback URL based on provider
            if payment_provider_name == 'paddle':
                callback_url = f"https://api.echodesk.ge/api/payments/paddle-webhook/"
            else:
                callback_url = f"https://api.echodesk.ge/api/payments/webhook/"

            # Build Paddle feature items if applicable
            feature_items = None
            if payment_provider_name == 'paddle':
                from .models import PaddlePriceMapping
                feature_items = []
                for feature in selected_features:
                    try:
                        mapping = feature.paddle_price_mapping
                        feature_items.append({
                            'paddle_price_id': mapping.paddle_price_id,
                            'quantity': agent_count,
                        })
                    except PaddlePriceMapping.DoesNotExist:
                        pass
                # If no mappings found, fall back to ad-hoc pricing
                if not feature_items:
                    feature_items = None

            # Create subscription payment via the selected provider
            payment_result = provider.create_subscription_payment(
                amount=Decimal(str(subscription_amount)),
                currency=currency,
                external_order_id=order_id,
                customer_email=validated_data['admin_email'],
                customer_name=f"{validated_data['admin_first_name']} {validated_data['admin_last_name']}",
                company_name=validated_data['company_name'],
                return_url_success=f"https://echodesk.ge/registration/success",
                return_url_fail=f"https://echodesk.ge/registration/failed",
                callback_url=callback_url,
                feature_items=feature_items,
                metadata={
                    'registration': True,
                    'schema_name': schema_name,
                },
            )

            # Update payment order with payment details
            payment_order.provider_order_id = payment_result.provider_order_id
            if payment_result.payment_url:
                payment_order.payment_url = payment_result.payment_url
            if payment_provider_name == 'bog':
                payment_order.bog_order_id = payment_result.provider_order_id
            payment_order.card_saved = payment_result.card_saving_enabled
            payment_order.save()

            logger.info(f"Registration payment initiated for {schema_name}: {order_id}, provider={payment_provider_name}, features={len(selected_features)}, agents={agent_count}, amount={subscription_amount}")

            # Build response based on provider type
            response_data = {
                'order_id': order_id,
                'amount': subscription_amount,
                'currency': currency,
                'payment_provider': payment_provider_name,
                'requires_redirect': payment_result.requires_redirect,
            }

            if payment_result.requires_redirect and payment_result.payment_url:
                # BOG: frontend should redirect to payment_url
                response_data['payment_url'] = payment_result.payment_url
                response_data['message'] = 'Subscription payment initiated - card will be saved for automatic recurring billing'
            elif payment_result.checkout_data:
                # Paddle: frontend should open Paddle.js overlay with checkout_data
                response_data['checkout_data'] = payment_result.checkout_data
                response_data['message'] = 'Paddle checkout session created'

            return Response(response_data, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Error initiating registration payment: {str(e)}")
        return Response(
            {'error': f'Failed to initiate payment: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    operation_id='register_tenant',
    summary='Register New Tenant',
    description='Register a new tenant with admin user creation and package selection. Only available from the main domain.',
    request=TenantRegistrationSerializer,
    responses={
        201: OpenApiResponse(
            description='Tenant created successfully',
            response={
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'},
                    'tenant': {'type': 'object'},
                    'subscription': {'type': 'object'},
                    'frontend_url': {'type': 'string'},
                    'api_url': {'type': 'string'}
                }
            }
        ),
        400: OpenApiResponse(description='Validation errors'),
        403: OpenApiResponse(description='Only available from main domain')
    },
    tags=['Tenant Management']
)
@api_view(['POST'])
@permission_classes([])  # No authentication required
def register_tenant(request):
    """
    Public endpoint for tenant registration with admin user creation and package selection
    """
    # Only allow access from public schema
    if hasattr(request, 'tenant') and request.tenant.schema_name != get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from the main domain'}, 
            status=status.HTTP_403_FORBIDDEN
        )
    
    serializer = TenantRegistrationSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    validated_data = serializer.validated_data
    
    try:
        with transaction.atomic():
            # Get selected features
            from .models import TenantSubscription
            from datetime import datetime, timedelta
            from django.utils import timezone

            feature_ids = validated_data.get('feature_ids', [])
            selected_features = Feature.objects.filter(id__in=feature_ids, is_active=True)
            agent_count = validated_data.get('agent_count', 10)

            # Create tenant
            domain_name = validated_data['domain']
            schema_name = domain_name.lower().replace('-', '_')

            tenant = Tenant.objects.create(
                schema_name=schema_name,
                domain_url=f"{domain_name}.api.echodesk.ge",  # Backend API domain
                name=validated_data['company_name'],
                description=validated_data.get('description', ''),
                admin_email=validated_data['admin_email'],
                admin_name=f"{validated_data['admin_first_name']} {validated_data['admin_last_name']}",
                preferred_language=validated_data.get('preferred_language', 'en'),
                plan='basic',  # Legacy field
                max_users=1000,  # Default limit
                max_storage=100 * 1024,  # 100GB in MB
                deployment_status='deploying'  # Set initial status
            )

            # Create subscription
            subscription = TenantSubscription.objects.create(
                tenant=tenant,
                agent_count=agent_count,
                is_active=True,
                starts_at=timezone.now(),
                expires_at=timezone.now() + timedelta(days=30),  # 30-day trial
                current_users=1,  # Admin user will be created
                whatsapp_messages_used=0,
                storage_used_gb=0,
                next_billing_date=timezone.now() + timedelta(days=30)
            )

            # Add selected features to subscription
            if selected_features:
                subscription.selected_features.set(selected_features)

            # Create admin user in tenant schema
            with schema_context(tenant.schema_name):
                admin_user = User.objects.create_user(
                    email=validated_data['admin_email'],
                    password=validated_data['admin_password'],
                    first_name=validated_data['admin_first_name'],
                    last_name=validated_data['admin_last_name'],
                    is_staff=True,
                    is_superuser=True,
                    is_active=True
                )
            
            # Setup frontend access (immediate, no deployment needed)
            deployment_service = SingleFrontendDeploymentService()
            deployment_result = deployment_service.setup_tenant_frontend(tenant)
            
            if deployment_result:
                return Response({
                    'message': 'Tenant created successfully! Your frontend is ready.',
                    'tenant': {
                        'id': tenant.id,
                        'name': tenant.name,
                        'domain_url': tenant.domain_url,
                        'schema': tenant.schema_name,
                        'admin_email': tenant.admin_email,
                        'preferred_language': tenant.preferred_language,
                        'deployment_status': 'deployed',
                        'frontend_url': tenant.frontend_url
                    },
                    'subscription': {
                        'package_name': 'Feature-based',  # Package system removed
                        'pricing_model': 'feature',
                        'monthly_cost': float(subscription.monthly_cost),
                        'agent_count': subscription.agent_count,
                        'trial_expires': subscription.expires_at,
                        'limits': {
                            'max_users': None,
                            'max_whatsapp_messages': None,
                            'max_storage_gb': None
                        }
                    },
                    'domain_url': tenant.domain_url,
                    'admin_email': validated_data['admin_email'],
                    'preferred_language': tenant.preferred_language,
                    'login_url': f"https://{tenant.domain_url}/admin/",  # API domain admin
                    'api_url': f"https://{tenant.domain_url}/api/",      # API domain
                    'frontend_url': tenant.frontend_url,                 # Frontend domain
                    'deployment_status': 'deployed',
                    'credentials': {
                        'email': validated_data['admin_email'],
                        'note': 'Use the password you provided to login'
                    }
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'error': 'Tenant created but frontend setup failed'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
    except Exception as e:
        return Response(
            {'error': f'Failed to create tenant: {str(e)}'}, 
            status=status.HTTP_400_BAD_REQUEST
        )


# Add API endpoint for frontend to get tenant configuration
@api_view(['GET'])
@permission_classes([])
def get_tenant_config(request):
    """
    Get tenant configuration for frontend
    Can be called by subdomain or domain parameter
    """
    subdomain = request.GET.get('subdomain')
    domain = request.GET.get('domain')
    
    if subdomain:
        config = TenantConfigAPI.get_tenant_by_subdomain(subdomain)
    elif domain:
        config = TenantConfigAPI.get_tenant_by_domain(domain)
    else:
        return Response({'error': 'subdomain or domain parameter required'}, status=400)
    
    if config:
        return Response(config)
    else:
        return Response({'error': 'Tenant not found'}, status=404)


@api_view(['GET'])
@permission_classes([])
def get_all_tenants(request):
    """
    Get list of all active tenants for frontend routing
    """
    tenants = TenantConfigAPI.get_all_tenants()
    return Response({
        'tenants': tenants,
        'count': len(tenants)
    })


@api_view(['GET'])
@permission_classes([])
def check_deployment_status(request, tenant_id):
    """Check the deployment status of a tenant's frontend"""
    try:
        tenant = Tenant.objects.get(id=tenant_id)
        return Response({
            'deployment_status': tenant.deployment_status,
            'frontend_url': tenant.frontend_url,
            'message': f'Deployment status: {tenant.get_deployment_status_display()}'
        })
    except Tenant.DoesNotExist:
        return Response({'error': 'Tenant not found'}, status=404)


class TenantViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing tenants.
    Only accessible from the public schema.
    """
    queryset = Tenant.objects.all()
    serializer_class = TenantSerializer
    permission_classes = [permissions.IsAdminUser]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return TenantCreateSerializer
        return TenantSerializer
    
    def get_queryset(self):
        # Only allow access from public schema
        if not hasattr(self.request, 'tenant') or self.request.tenant.schema_name != get_public_schema_name():
            return Tenant.objects.none()
        return super().get_queryset()
    
    @action(detail=True, methods=['post'])
    def create_admin_user(self, request, pk=None):
        """Create an admin user for a specific tenant"""
        tenant = self.get_object()
        
        # Get user data from request
        email = request.data.get('email')
        password = request.data.get('password')
        first_name = request.data.get('first_name', '')
        last_name = request.data.get('last_name', '')
        
        if not email or not password:
            return Response(
                {'error': 'Email and password are required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create user in tenant's schema
        with schema_context(tenant.schema_name):
            try:
                user = User.objects.create_user(
                    email=email,
                    password=password,
                    first_name=first_name,
                    last_name=last_name,
                    is_staff=True,
                    is_superuser=True
                )
                return Response({
                    'message': f'Admin user created successfully for tenant {tenant.name}',
                    'user': {
                        'id': user.id,
                        'email': user.email,
                        'first_name': user.first_name,
                        'last_name': user.last_name
                    }
                })
            except Exception as e:
                return Response(
                    {'error': f'Failed to create user: {str(e)}'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
    
    @action(detail=True, methods=['get'])
    def users(self, request, pk=None):
        """Get all users for a specific tenant"""
        tenant = self.get_object()

        with schema_context(tenant.schema_name):
            users = User.objects.all()
            user_data = [{
                'id': user.id,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'is_active': user.is_active,
                'is_staff': user.is_staff,
                'date_joined': user.date_joined
            } for user in users]

        return Response({'users': user_data})


# Tenant Settings API
def _can_manage_tenant_settings(user):
    """Staff, superusers and users with ``manage_settings`` (their own
    ``can_manage_settings`` flag or the admin role) may change workspace
    settings and the logo. Groups carry no ``can_*`` flags."""
    from tickets.permissions import user_can_manage
    return user_can_manage(user, 'manage_settings')


_SETTINGS_FORBIDDEN = {'error': 'You need the manage_settings permission (or staff) to change workspace settings.'}


@extend_schema(
    methods=['GET'],
    operation_id='tenant_settings_get',
    summary='Get Tenant Settings',
    description=(
        'Workspace settings: logo URL, company name, ticket rules and the '
        'dashboard language. Readable by every authenticated tenant user.'
    ),
    responses={
        200: TenantSettingsSerializer,
        403: OpenApiResponse(description='Not available from main domain'),
    },
    tags=['Tenant Settings'],
)
@extend_schema(
    methods=['PATCH'],
    operation_id='tenant_settings_update',
    summary='Update Tenant Settings',
    description=(
        'Partial update of min_users_per_ticket, only_superadmin_can_delete_tickets '
        'and preferred_language. Needs staff, superuser or manage_settings. '
        'Returns the full settings object.'
    ),
    request=TenantSettingsSerializer,
    responses={
        200: TenantSettingsSerializer,
        400: OpenApiResponse(description='Validation error'),
        403: OpenApiResponse(description='Needs manage_settings or staff, or called from the main domain'),
    },
    tags=['Tenant Settings'],
)
@api_view(['GET', 'PATCH'])
@permission_classes([permissions.IsAuthenticated])
def tenant_settings(request):
    """Get or update tenant settings"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    tenant = request.tenant
    if request.method == 'PATCH':
        if not _can_manage_tenant_settings(request.user):
            return Response(_SETTINGS_FORBIDDEN, status=status.HTTP_403_FORBIDDEN)
        serializer = TenantSettingsSerializer(
            tenant, data=request.data, partial=True, context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        changed = []
        for field, value in serializer.validated_data.items():
            setattr(tenant, field, value)
            changed.append(field)
        if changed:
            tenant.save(update_fields=changed)
    return Response(TenantSettingsSerializer(tenant, context={'request': request}).data)


@extend_schema(
    operation_id='tenant_public_branding',
    summary='Get Public Tenant Branding',
    description=(
        'Public (unauthenticated) read of the current tenant\'s logo and '
        'display name. Used by the frontend\'s dynamic PWA manifest route '
        'so the install dialog and home-screen icon are tenant-branded '
        'before the user has signed in. Returns 403 from the public schema '
        '— there is no fallback "EchoDesk" tenant on the bare domain.'
    ),
    responses={
        200: OpenApiResponse(
            description='Branding retrieved successfully',
            response={
                'type': 'object',
                'properties': {
                    'logo': {'type': 'string', 'nullable': True},
                    'company_name': {'type': 'string'},
                    'schema_name': {'type': 'string'},
                },
            },
        ),
        403: OpenApiResponse(description='Not available from main domain'),
    },
    tags=['Tenant Settings'],
)
@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def tenant_public_branding(request):
    """Public, no-auth read of tenant logo + display name for PWA manifest."""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    tenant = request.tenant
    return Response({
        'logo': request.build_absolute_uri(tenant.logo.url) if tenant.logo else None,
        'company_name': tenant.name,
        'schema_name': tenant.schema_name,
    })


@extend_schema(
    operation_id='tenant_settings_upload_logo',
    summary='Upload Tenant Logo',
    description=(
        'Upload a company logo (JPG, PNG, GIF or WebP, max 2MB) as multipart '
        'field `logo`. Needs staff, superuser or manage_settings.'
    ),
    request={
        'multipart/form-data': {
            'type': 'object',
            'properties': {
                'logo': {
                    'type': 'string',
                    'format': 'binary'
                }
            }
        }
    },
    responses={
        200: OpenApiResponse(
            description='Logo uploaded successfully',
            response={
                'type': 'object',
                'properties': {
                    'logo_url': {'type': 'string'},
                    'message': {'type': 'string'}
                }
            }
        ),
        400: OpenApiResponse(description='Invalid file or upload error'),
        403: OpenApiResponse(description='Needs manage_settings or staff, or called from the main domain')
    },
    tags=['Tenant Settings']
)
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def upload_logo(request):
    """Upload tenant logo (staff / manage_settings)"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )
    if not _can_manage_tenant_settings(request.user):
        return Response(_SETTINGS_FORBIDDEN, status=status.HTTP_403_FORBIDDEN)

    if 'logo' not in request.FILES:
        return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

    logo_file = request.FILES['logo']

    # Validate file type
    allowed_types = ['image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp']
    if logo_file.content_type not in allowed_types:
        return Response(
            {'error': 'Invalid file type. Only JPG, PNG, GIF and WebP are allowed.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Validate file size (2MB max)
    if logo_file.size > 2 * 1024 * 1024:
        return Response(
            {'error': 'File size must be less than 2MB'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        # Delete old logo if exists
        tenant = request.tenant
        if tenant.logo:
            tenant.logo.delete(save=False)

        # Save new logo
        tenant.logo = logo_file
        tenant.save()

        return Response({
            'logo_url': request.build_absolute_uri(tenant.logo.url),
            'message': 'Logo uploaded successfully'
        })
    except Exception as e:
        logger.error(f'Failed to upload logo: {str(e)}')
        return Response(
            {'error': 'Failed to upload logo'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    operation_id='tenant_settings_remove_logo',
    summary='Remove Tenant Logo',
    description='Remove the current tenant logo. Needs staff, superuser or manage_settings.',
    responses={
        200: OpenApiResponse(
            description='Logo removed successfully',
            response={
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'}
                }
            }
        ),
        403: OpenApiResponse(description='Needs manage_settings or staff, or called from the main domain')
    },
    tags=['Tenant Settings']
)
@api_view(['DELETE'])
@permission_classes([permissions.IsAuthenticated])
def remove_logo(request):
    """Remove tenant logo (staff / manage_settings)"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )
    if not _can_manage_tenant_settings(request.user):
        return Response(_SETTINGS_FORBIDDEN, status=status.HTTP_403_FORBIDDEN)

    try:
        tenant = request.tenant
        if tenant.logo:
            tenant.logo.delete(save=True)
            return Response({'message': 'Logo removed successfully'})
        else:
            return Response({'message': 'No logo to remove'})
    except Exception as e:
        logger.error(f'Failed to remove logo: {str(e)}')
        return Response(
            {'error': 'Failed to remove logo'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    operation_id='forced_password_change',
    summary='Forced Password Change',
    description='Change password for user with temporary password (first login)',
    request={
        'application/json': {
            'type': 'object',
            'properties': {
                'email': {'type': 'string', 'format': 'email'},
                'current_password': {'type': 'string'},
                'new_password': {'type': 'string'}
            },
            'required': ['email', 'current_password', 'new_password']
        }
    },
    responses={
        200: OpenApiResponse(
            description='Password changed successfully',
            response={
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'},
                    'token': {'type': 'string'}
                }
            }
        ),
        400: OpenApiResponse(description='Invalid credentials or validation error'),
        403: OpenApiResponse(description='Not available from main domain')
    },
    tags=['Authentication']
)
@api_view(['POST'])
@permission_classes([])  # No authentication required
@throttle_classes([AuthRateThrottle])
def forced_password_change(request):
    """
    Endpoint for users to change their password on first login
    """
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    email = request.data.get('email')
    current_password = request.data.get('current_password')
    new_password = request.data.get('new_password')

    if not email or not current_password or not new_password:
        return Response(
            {'error': 'Email, current password, and new password are required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        # Get user from tenant schema
        user = User.objects.get(email=email)

        # Validate new password using Django's password validators
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError
        try:
            validate_password(new_password, user=user)
        except ValidationError as e:
            return Response(
                {'error': e.messages[0]},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Verify current password
        if not user.check_password(current_password):
            return Response(
                {'error': 'Invalid credentials'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Disabled and suspended accounts cannot sign in through this door
        # either (same message as the login endpoint).
        if not user.is_active or user.status == 'suspended':
            return Response(
                {'error': 'User account is disabled'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if password change is required
        if not user.password_change_required:
            return Response(
                {'error': 'Password change is not required for this user'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Update password
        user.set_password(new_password)
        user.password_change_required = False
        user.temporary_password = None
        user.save()

        # Generate token for immediate login (per-device for mobile clients)
        token_key, token_type = _issue_login_token(user, request.data)

        logger.info(f'Password changed successfully for user: {email}')

        return Response({
            'message': 'Password changed successfully',
            'token': token_key,
            'token_type': token_type
        }, status=status.HTTP_200_OK)

    except User.DoesNotExist:
        return Response(
            {'error': 'Invalid credentials'},
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        logger.error(f'Error changing password for {email}: {str(e)}')
        return Response(
            {'error': 'Failed to change password'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    operation_id='upload_image',
    summary='Upload Image',
    description='Upload an image file and get back the URL. Used for gallery fields in item lists.',
    request={
        'multipart/form-data': {
            'type': 'object',
            'properties': {
                'image': {
                    'type': 'string',
                    'format': 'binary'
                }
            }
        }
    },
    responses={
        200: OpenApiResponse(
            description='Image uploaded successfully',
            response={
                'type': 'object',
                'properties': {
                    'url': {'type': 'string'},
                    'message': {'type': 'string'}
                }
            }
        ),
        400: OpenApiResponse(description='Invalid file or missing file'),
        403: OpenApiResponse(description='Not available from main domain')
    },
    tags=['Uploads']
)
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def upload_image(request):
    """Upload an image and return its URL"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    if 'image' not in request.FILES:
        return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

    image_file = request.FILES['image']

    # Validate file type
    allowed_types = ['image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp']
    if image_file.content_type not in allowed_types:
        return Response(
            {'error': 'Invalid file type. Only JPG, PNG, GIF, and WebP are allowed.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Validate file size (5MB max)
    if image_file.size > 5 * 1024 * 1024:
        return Response(
            {'error': 'File size must be less than 5MB'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        from django.core.files.storage import default_storage
        from datetime import datetime
        import os
        import re

        # Generate unique filename with sanitization
        # Remove special characters that DigitalOcean Spaces doesn't like
        ext = os.path.splitext(image_file.name)[1]
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', image_file.name)
        filename = f'gallery/{request.tenant.schema_name}/{datetime.now().strftime("%Y%m%d_%H%M%S")}_{safe_name}'

        # Save file using Django's storage backend
        path = default_storage.save(filename, image_file)
        url = default_storage.url(path)

        # Make URL absolute if it's relative
        if not url.startswith('http'):
            url = request.build_absolute_uri(url)

        return Response({
            'url': url,
            'message': 'Image uploaded successfully'
        })
    except Exception as e:
        logger.error(f'Failed to upload image: {str(e)}')
        return Response(
            {'error': f'Failed to upload image: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ==============================================================================
# MULTI-TENANT ECOMMERCE - Domain Resolution
# ==============================================================================

@extend_schema(
    operation_id='resolve_ecommerce_domain',
    summary='Resolve Ecommerce Domain to Tenant',
    description='''
    Public endpoint for the multi-tenant ecommerce frontend to resolve a hostname to tenant configuration.

    The frontend middleware calls this endpoint on every request to determine which tenant to serve.

    Supports two domain patterns:
    1. Subdomain pattern: {schema}.ecommerce.echodesk.ge
    2. Custom domains: mystore.com (stored in TenantDomain table)

    Returns tenant configuration including API URL, store name, theme, and features.
    Responses are cached by the frontend for 5 minutes.
    ''',
    parameters=[
        OpenApiParameter(
            name='domain',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=True,
            description='The hostname to resolve (e.g., store1.ecommerce.echodesk.ge or mystore.com)'
        )
    ],
    responses={
        200: OpenApiResponse(
            description='Tenant found - returns configuration',
            response={
                'type': 'object',
                'properties': {
                    'tenant_id': {'type': 'integer'},
                    'schema': {'type': 'string'},
                    'api_url': {'type': 'string'},
                    'store_name': {'type': 'string'},
                    'store_logo': {'type': 'string', 'nullable': True},
                    'primary_color': {'type': 'string'},
                    'currency': {'type': 'string'},
                    'locale': {'type': 'string'},
                    'features': {
                        'type': 'object',
                        'properties': {
                            'ecommerce': {'type': 'boolean'},
                            'wishlist': {'type': 'boolean'},
                            'reviews': {'type': 'boolean'}
                        }
                    }
                }
            }
        ),
        400: OpenApiResponse(description='Missing domain parameter'),
        404: OpenApiResponse(description='Domain not found or tenant inactive')
    },
    tags=['Ecommerce - Public']
)
@api_view(['GET'])
@permission_classes([])  # Public endpoint - no authentication required
def resolve_ecommerce_domain(request):
    """
    Resolve a domain/subdomain to tenant configuration for the multi-tenant ecommerce frontend.

    This endpoint is called by the Next.js middleware to determine which tenant to serve.

    Supported patterns:
    1. Subdomain: {schema}.ecommerce.echodesk.ge → extracts schema from subdomain
    2. Custom domain: mystore.com → looks up in TenantDomain table

    Returns tenant configuration including API URL, store settings, and theme.
    """
    domain = request.GET.get('domain', '').lower().strip()

    if not domain:
        return Response(
            {'error': 'domain parameter is required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    tenant = None

    # Pattern 1: Subdomain - {schema}.ecommerce.echodesk.ge
    if domain.endswith('.ecommerce.echodesk.ge'):
        schema = domain.replace('.ecommerce.echodesk.ge', '')
        # Validate schema name (alphanumeric and underscores only)
        if schema and schema.replace('_', '').replace('-', '').isalnum():
            # Convert dashes to underscores for schema lookup
            schema_name = schema.replace('-', '_')
            tenant = Tenant.objects.filter(
                schema_name=schema_name,
                is_active=True
            ).first()

    # Pattern 2: Custom domain - look up in TenantDomain table
    if not tenant:
        custom_domain = TenantDomain.objects.filter(
            domain=domain,
            is_verified=True
        ).select_related('tenant').first()

        if custom_domain and custom_domain.tenant.is_active:
            tenant = custom_domain.tenant

    # Pattern 3: Custom domain in EcommerceSettings.custom_domain field
    # EcommerceSettings is in tenant schemas, so we need to check each active tenant
    if not tenant:
        try:
            from ecommerce_crm.models import EcommerceSettings
            # Get all active tenants and check their EcommerceSettings
            for candidate_tenant in Tenant.objects.filter(is_active=True).exclude(schema_name='public'):
                try:
                    with schema_context(candidate_tenant.schema_name):
                        settings = EcommerceSettings.objects.filter(
                            custom_domain=domain,
                            tenant=candidate_tenant
                        ).first()
                        if settings:
                            tenant = candidate_tenant
                            break
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"Error checking EcommerceSettings.custom_domain: {e}")

    # Not found
    if not tenant:
        logger.debug(f"Ecommerce domain resolution failed for: {domain}")
        return Response(
            {'error': 'Domain not found or tenant inactive'},
            status=status.HTTP_404_NOT_FOUND
        )

    # Build tenant configuration response
    try:
        # Get ecommerce settings if available. EcommerceSettings is a
        # tenant-scoped model — querying from the public schema never
        # returns rows, which previously meant the resolve-domain
        # response always fell back to `tenant.name` ("groot") instead
        # of the configured `EcommerceSettings.store_name` ("Refurb").
        # Wrap the lookup in schema_context so the storefront sees the
        # right brand on every meta tag, OG card, and JSON-LD entity.
        ecommerce_settings = None
        try:
            from ecommerce_crm.models import EcommerceSettings
            with schema_context(tenant.schema_name):
                ecommerce_settings = EcommerceSettings.objects.first()
        except Exception:
            pass

        # Build response
        config = {
            'tenant_id': tenant.id,
            'schema': tenant.schema_name,
            'api_url': f'https://{tenant.schema_name}.api.echodesk.ge',
            'store_name': tenant.name,
            'store_logo': None,
            'primary_color': None,
            'secondary_color': None,
            'accent_color': None,
            'currency': 'GEL',
            'locale': tenant.preferred_language or 'en',
            'features': {
                'ecommerce': True,
                'wishlist': True,
                'reviews': False,
                'compare': False
            },
            'contact': {
                'email': tenant.admin_email or '',
                'phone': '',
                'address': ''
            },
            'social': {
                'facebook': '',
                'instagram': '',
                'twitter': ''
            }
        }

        # Add ecommerce-specific settings if available. The model
        # exposes theme colours as `theme_primary_color` etc.; the
        # original code referenced bare `primary_color` which only
        # didn't crash because `ecommerce_settings` was always None
        # (querying from the public schema). With the schema_context
        # fix above, the rows now load and we have to use the real
        # field names. Defensive getattr so a tenant on an older
        # migration with missing columns degrades gracefully instead
        # of bringing down the whole resolve-domain endpoint.
        if ecommerce_settings:
            if getattr(ecommerce_settings, 'store_name', None):
                config['store_name'] = ecommerce_settings.store_name
            if getattr(ecommerce_settings, 'store_email', None):
                config['contact']['email'] = ecommerce_settings.store_email
            if getattr(ecommerce_settings, 'store_phone', None):
                config['contact']['phone'] = ecommerce_settings.store_phone

            # Theme colors
            if getattr(ecommerce_settings, 'theme_primary_color', None):
                config['primary_color'] = ecommerce_settings.theme_primary_color
            if getattr(ecommerce_settings, 'theme_secondary_color', None):
                config['secondary_color'] = ecommerce_settings.theme_secondary_color
            if getattr(ecommerce_settings, 'theme_accent_color', None):
                config['accent_color'] = ecommerce_settings.theme_accent_color

        # Logo
        if tenant.logo:
            config['store_logo'] = request.build_absolute_uri(tenant.logo.url)

        logger.debug(f"Ecommerce domain resolved: {domain} → {tenant.schema_name}")
        return Response(config, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Error building tenant config for {domain}: {str(e)}")
        return Response(
            {'error': 'Failed to build tenant configuration'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ============================================================
# Dashboard Appearance Settings
# ============================================================

@extend_schema(
    operation_id='get_dashboard_appearance',
    summary='Get Dashboard Appearance Settings',
    description='Get the current tenant dashboard appearance customization settings. Available to all authenticated users.',
    responses={
        200: DashboardAppearanceSettingsSerializer,
        403: OpenApiResponse(description='Not available from main domain')
    },
    tags=['Dashboard Appearance']
)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_dashboard_appearance(request):
    """Get tenant dashboard appearance settings"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Get or create appearance settings for this tenant
    appearance, created = DashboardAppearanceSettings.objects.get_or_create(
        tenant=request.tenant
    )

    serializer = DashboardAppearanceSettingsSerializer(appearance)
    return Response(serializer.data)


@extend_schema(
    operation_id='update_dashboard_appearance',
    summary='Update Dashboard Appearance Settings',
    description='Update the tenant dashboard appearance customization settings. Only superadmins can update.',
    request=DashboardAppearanceSettingsSerializer,
    responses={
        200: DashboardAppearanceSettingsSerializer,
        400: OpenApiResponse(description='Validation error'),
        403: OpenApiResponse(description='Permission denied or not from tenant domain')
    },
    tags=['Dashboard Appearance']
)
@api_view(['PATCH', 'PUT'])
@permission_classes([permissions.IsAuthenticated])
def update_dashboard_appearance(request):
    """Update tenant dashboard appearance settings - superadmin only"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Only superadmins can update appearance settings
    if not request.user.is_superuser:
        return Response(
            {'error': 'Only superadmins can update dashboard appearance settings'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Get or create appearance settings
    appearance, created = DashboardAppearanceSettings.objects.get_or_create(
        tenant=request.tenant
    )

    serializer = DashboardAppearanceSettingsSerializer(
        appearance,
        data=request.data,
        partial=True
    )

    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    operation_id='reset_dashboard_appearance',
    summary='Reset Dashboard Appearance Settings',
    description='Reset the tenant dashboard appearance to default values. Only superadmins can reset.',
    responses={
        200: DashboardAppearanceSettingsSerializer,
        403: OpenApiResponse(description='Permission denied or not from tenant domain')
    },
    tags=['Dashboard Appearance']
)
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def reset_dashboard_appearance(request):
    """Reset tenant dashboard appearance to defaults - superadmin only"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    if not request.user.is_superuser:
        return Response(
            {'error': 'Only superadmins can reset dashboard appearance settings'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Delete existing and create fresh with defaults
    DashboardAppearanceSettings.objects.filter(tenant=request.tenant).delete()
    appearance = DashboardAppearanceSettings.objects.create(tenant=request.tenant)

    serializer = DashboardAppearanceSettingsSerializer(appearance)
    return Response(serializer.data)
