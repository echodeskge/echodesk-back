"""
Security API Views for login/logout logs and IP whitelist management.
"""
from rest_framework import status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from datetime import timedelta
from tenant_schemas.utils import get_public_schema_name
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse, inline_serializer
from rest_framework import serializers
from drf_spectacular.openapi import OpenApiTypes

from .models import SecurityLog, TenantIPWhitelist, Tenant
from .serializers import SecurityLogSerializer, TenantIPWhitelistSerializer
from .security_service import SecurityService

import logging

logger = logging.getLogger(__name__)


class SecurityLogPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200


class IsSuperUser(permissions.BasePermission):
    """Only allow superusers to access"""
    def has_permission(self, request, view):
        return request.user and request.user.is_superuser


class IsStaffOrSuperUser(permissions.BasePermission):
    """
    Staff or superusers: the same rule the dashboard uses to show the
    Security settings page (``is_staff || is_superuser``).
    """
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


ERROR_SCHEMA = inline_serializer(name='SecurityErrorResponse', fields={'error': serializers.CharField()})

PAGINATED_SECURITY_LOGS = inline_serializer(
    name='PaginatedSecurityLogList',
    fields={
        'count': serializers.IntegerField(),
        'next': serializers.URLField(allow_null=True),
        'previous': serializers.URLField(allow_null=True),
        'results': SecurityLogSerializer(many=True),
    },
)

IP_WHITELIST_STATE = inline_serializer(
    name='IPWhitelistState',
    fields={
        'ip_whitelist_enabled': serializers.BooleanField(),
        'superadmin_bypass_whitelist': serializers.BooleanField(),
        'message': serializers.CharField(),
    },
)

IP_WHITELIST_LOCKOUT = inline_serializer(
    name='IPWhitelistLockoutError',
    fields={
        'error': serializers.CharField(),
        'code': serializers.ChoiceField(choices=['no_active_entries', 'would_lock_out']),
        'ip_address': serializers.CharField(required=False),
    },
)


def _would_lock_out(request, tenant, enabled, bypass, exclude_entry_id=None, entry_overrides=None):
    """
    True when the whitelist would end up enabled and the caller's own IP
    would no longer be let through (and no superuser bypass covers them).
    """
    if not enabled:
        return False
    if bypass and request.user.is_superuser:
        return False
    client_ip = SecurityService.get_client_ip(request)
    return not SecurityService.ip_matches_whitelist(
        tenant, client_ip, exclude_entry_id=exclude_entry_id, entry_overrides=entry_overrides,
    )


def _lockout_response(request):
    client_ip = SecurityService.get_client_ip(request)
    return Response(
        {
            'error': (
                f'Your current IP address ({client_ip}) is not in the active whitelist, so this '
                'change would lock you out. Add your IP (or a range that contains it) first, '
                'or turn on the superadmin bypass if you are a superuser.'
            ),
            'code': 'would_lock_out',
            'ip_address': client_ip,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


# ============================================================
# Security Logs Endpoints
# ============================================================

@extend_schema(
    operation_id='list_security_logs',
    summary='List Security Logs',
    description='Get paginated list of security logs with filtering options. Staff or superuser only.',
    parameters=[
        OpenApiParameter('event_type', OpenApiTypes.STR, description='Filter by event type (login_success, login_failed, logout, token_expired)'),
        OpenApiParameter('user_id', OpenApiTypes.INT, description='Filter by user ID'),
        OpenApiParameter('ip_address', OpenApiTypes.STR, description='Filter by IP address'),
        OpenApiParameter('date_from', OpenApiTypes.DATE, description='Filter logs from this date'),
        OpenApiParameter('date_to', OpenApiTypes.DATE, description='Filter logs until this date'),
        OpenApiParameter('search', OpenApiTypes.STR, description='Search in email, IP, browser, city'),
        OpenApiParameter('page', OpenApiTypes.INT, description='Page number'),
        OpenApiParameter('page_size', OpenApiTypes.INT, description='Items per page (max 200)'),
    ],
    responses={
        200: PAGINATED_SECURITY_LOGS,
        403: OpenApiResponse(description='Not staff/superuser or not from tenant domain')
    },
    tags=['Security']
)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsStaffOrSuperUser])
def list_security_logs(request):
    """List all security logs with filtering and pagination"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Filter by current tenant
    queryset = SecurityLog.objects.filter(tenant=request.tenant)

    # Apply filters
    event_type = request.GET.get('event_type')
    if event_type:
        queryset = queryset.filter(event_type=event_type)

    user_id = request.GET.get('user_id')
    if user_id:
        queryset = queryset.filter(user_id=user_id)

    ip_address = request.GET.get('ip_address')
    if ip_address:
        queryset = queryset.filter(ip_address__icontains=ip_address)

    date_from = request.GET.get('date_from')
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)

    date_to = request.GET.get('date_to')
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)

    search = request.GET.get('search')
    if search:
        queryset = queryset.filter(
            Q(attempted_email__icontains=search) |
            Q(ip_address__icontains=search) |
            Q(browser__icontains=search) |
            Q(city__icontains=search) |
            Q(country__icontains=search)
        )

    # Paginate
    paginator = SecurityLogPagination()
    page = paginator.paginate_queryset(queryset, request)

    serializer = SecurityLogSerializer(page, many=True)
    return paginator.get_paginated_response(serializer.data)


@extend_schema(
    operation_id='security_logs_stats',
    summary='Get Security Logs Statistics',
    description='Get security event statistics for the dashboard. Staff or superuser only.',
    parameters=[
        OpenApiParameter('days', OpenApiTypes.INT, description='Number of days to include (default 30)'),
    ],
    responses={
        200: inline_serializer(
            name='SecurityLogsStats',
            fields={
                'total_logins': serializers.IntegerField(),
                'failed_logins': serializers.IntegerField(),
                'unique_ips': serializers.IntegerField(),
                'unique_users': serializers.IntegerField(),
                'by_event_type': serializers.DictField(child=serializers.IntegerField()),
                'by_device_type': serializers.DictField(child=serializers.IntegerField()),
                'by_date': inline_serializer(
                    name='SecurityLogsStatsDay', many=True,
                    fields={
                        'date': serializers.DateField(),
                        'login_success': serializers.IntegerField(),
                        'login_failed': serializers.IntegerField(),
                        'logout': serializers.IntegerField(),
                    },
                ),
                'recent_failed_logins': SecurityLogSerializer(many=True),
                'top_ips': inline_serializer(
                    name='SecurityLogsStatsTopIp', many=True,
                    fields={
                        'ip_address': serializers.CharField(),
                        'count': serializers.IntegerField(),
                    },
                ),
                'period_days': serializers.IntegerField(),
            },
        ),
        400: ERROR_SCHEMA,
        403: OpenApiResponse(description='Not staff/superuser or not from tenant domain')
    },
    tags=['Security']
)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsStaffOrSuperUser])
def security_logs_stats(request):
    """Get security logs statistics for dashboard"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    try:
        days = int(request.GET.get('days', 30))
    except (TypeError, ValueError):
        return Response({'error': 'days must be an integer'}, status=status.HTTP_400_BAD_REQUEST)
    if days < 1 or days > 3650:
        return Response({'error': 'days must be between 1 and 3650'}, status=status.HTTP_400_BAD_REQUEST)
    since_date = timezone.now() - timedelta(days=days)

    # Filter by current tenant
    logs = SecurityLog.objects.filter(tenant=request.tenant, created_at__gte=since_date)

    # Basic counts
    total_logins = logs.filter(event_type='login_success').count()
    failed_logins = logs.filter(event_type='login_failed').count()
    unique_ips = logs.values('ip_address').distinct().count()
    unique_users = logs.filter(user_id__isnull=False).values('user_id').distinct().count()

    # Count by event type
    by_event_type = dict(logs.values('event_type').annotate(count=Count('id')).values_list('event_type', 'count'))

    # Count by device type
    by_device_type = dict(logs.values('device_type').annotate(count=Count('id')).values_list('device_type', 'count'))

    # Daily counts for chart
    by_date = list(
        logs.annotate(date=TruncDate('created_at'))
        .values('date')
        .annotate(
            login_success=Count('id', filter=Q(event_type='login_success')),
            login_failed=Count('id', filter=Q(event_type='login_failed')),
            logout=Count('id', filter=Q(event_type='logout'))
        )
        .order_by('date')
        .values('date', 'login_success', 'login_failed', 'logout')
    )

    # Recent failed logins
    recent_failed = logs.filter(event_type='login_failed').order_by('-created_at')[:10]
    recent_failed_data = SecurityLogSerializer(recent_failed, many=True).data

    # Top IPs by activity
    top_ips = list(
        logs.values('ip_address')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )

    return Response({
        'total_logins': total_logins,
        'failed_logins': failed_logins,
        'unique_ips': unique_ips,
        'unique_users': unique_users,
        'by_event_type': by_event_type,
        'by_device_type': by_device_type,
        'by_date': by_date,
        'recent_failed_logins': recent_failed_data,
        'top_ips': top_ips,
        'period_days': days
    })


@extend_schema(
    operation_id='my_security_logs',
    summary='Get My Security Logs',
    description='Get security logs for the current user.',
    parameters=[
        OpenApiParameter('page', OpenApiTypes.INT, description='Page number'),
        OpenApiParameter('page_size', OpenApiTypes.INT, description='Items per page (max 100)'),
    ],
    responses={
        200: PAGINATED_SECURITY_LOGS,
        403: OpenApiResponse(description='Not from tenant domain')
    },
    tags=['Security']
)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def my_security_logs(request):
    """Get security logs for the current user"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Filter by current tenant and user
    queryset = SecurityLog.objects.filter(tenant=request.tenant, user_id=request.user.id).order_by('-created_at')

    paginator = SecurityLogPagination()
    paginator.page_size = 20
    paginator.max_page_size = 100
    page = paginator.paginate_queryset(queryset, request)

    serializer = SecurityLogSerializer(page, many=True)
    return paginator.get_paginated_response(serializer.data)


# ============================================================
# IP Whitelist Endpoints
# ============================================================

@extend_schema(
    operation_id='list_ip_whitelist',
    summary='List IP Whitelist',
    description='Get all IP whitelist entries for the tenant. Staff or superuser only.',
    responses={
        200: OpenApiResponse(
            description='IP whitelist entries with tenant settings',
            response=inline_serializer(
                name='IPWhitelistList',
                fields={
                    'ip_whitelist_enabled': serializers.BooleanField(),
                    'superadmin_bypass_whitelist': serializers.BooleanField(),
                    'entries': TenantIPWhitelistSerializer(many=True),
                },
            ),
        ),
        403: OpenApiResponse(description='Not staff/superuser or not from tenant domain')
    },
    tags=['Security']
)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsStaffOrSuperUser])
def list_ip_whitelist(request):
    """List all IP whitelist entries for the tenant"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    entries = TenantIPWhitelist.objects.filter(tenant=request.tenant).select_related('created_by')
    serializer = TenantIPWhitelistSerializer(entries, many=True)

    return Response({
        'ip_whitelist_enabled': request.tenant.ip_whitelist_enabled,
        'superadmin_bypass_whitelist': request.tenant.superadmin_bypass_whitelist,
        'entries': serializer.data
    })


@extend_schema(
    operation_id='create_ip_whitelist',
    summary='Create IP Whitelist Entry',
    description='Add a new IP address or range to the whitelist. Staff or superuser only.',
    request=TenantIPWhitelistSerializer,
    responses={
        201: TenantIPWhitelistSerializer,
        400: OpenApiResponse(description='Validation error'),
        403: OpenApiResponse(description='Not staff/superuser or not from tenant domain')
    },
    tags=['Security']
)
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsStaffOrSuperUser])
def create_ip_whitelist(request):
    """Create a new IP whitelist entry"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    serializer = TenantIPWhitelistSerializer(data=request.data)
    if serializer.is_valid():
        # Check for duplicates
        ip_address = serializer.validated_data['ip_address']
        cidr_notation = serializer.validated_data.get('cidr_notation', '')

        existing = TenantIPWhitelist.objects.filter(
            tenant=request.tenant,
            ip_address=ip_address,
            cidr_notation=cidr_notation
        ).exists()

        if existing:
            return Response(
                {'error': 'This IP address/range is already in the whitelist'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer.save(tenant=request.tenant, created_by=request.user)
        logger.info(f"IP whitelist entry created by {request.user.email}: {ip_address}/{cidr_notation}")
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    operation_id='manage_ip_whitelist',
    summary='Update or Delete IP Whitelist Entry',
    description=(
        'Update or delete an IP whitelist entry. Staff or superuser only. While the whitelist '
        "is enabled, a change that would stop letting the caller's current IP through "
        '(deleting, deactivating or re-addressing the entry that covers it) is refused with '
        '400 `code: would_lock_out`, unless the caller is a superuser and the bypass is on.'
    ),
    request=TenantIPWhitelistSerializer,
    responses={
        200: TenantIPWhitelistSerializer,
        204: OpenApiResponse(description='Entry deleted'),
        400: IP_WHITELIST_LOCKOUT,
        403: OpenApiResponse(description='Not staff/superuser or not from tenant domain'),
        404: OpenApiResponse(description='Entry not found')
    },
    tags=['Security']
)
@api_view(['PUT', 'PATCH', 'DELETE'])
@permission_classes([permissions.IsAuthenticated, IsStaffOrSuperUser])
def manage_ip_whitelist(request, pk):
    """Update or delete an IP whitelist entry"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    try:
        entry = TenantIPWhitelist.objects.get(pk=pk, tenant=request.tenant)
    except TenantIPWhitelist.DoesNotExist:
        return Response(
            {'error': 'IP whitelist entry not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    tenant = request.tenant
    if request.method == 'DELETE':
        if _would_lock_out(request, tenant, tenant.ip_whitelist_enabled,
                           tenant.superadmin_bypass_whitelist, exclude_entry_id=entry.pk):
            return _lockout_response(request)
        ip_info = f"{entry.ip_address}/{entry.cidr_notation}" if entry.cidr_notation else entry.ip_address
        entry.delete()
        logger.info(f"IP whitelist entry deleted by {request.user.email}: {ip_info}")
        return Response(status=status.HTTP_204_NO_CONTENT)

    # PUT or PATCH
    serializer = TenantIPWhitelistSerializer(entry, data=request.data, partial=(request.method == 'PATCH'))
    if serializer.is_valid():
        if _would_lock_out(request, tenant, tenant.ip_whitelist_enabled,
                           tenant.superadmin_bypass_whitelist,
                           entry_overrides={entry.pk: serializer.validated_data}):
            return _lockout_response(request)
        serializer.save()
        return Response(serializer.data)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    operation_id='toggle_ip_whitelist',
    summary='Toggle IP Whitelist',
    description=(
        'Enable or disable the IP whitelist and/or the superadmin bypass. Staff or superuser '
        'only. Both fields are optional; omitted ones keep their value. Enabling needs at '
        'least one active entry (400 `code: no_active_entries`). A change that would leave '
        "the whitelist on without letting the caller's current IP through is refused with "
        '400 `code: would_lock_out` (the bypass only counts for superusers).'
    ),
    request=inline_serializer(
        name='ToggleIPWhitelist',
        fields={
            'enabled': serializers.BooleanField(required=False, help_text='Whether to enable IP whitelist'),
            'superadmin_bypass': serializers.BooleanField(
                required=False, help_text='Whether superadmins can bypass whitelist'),
        },
    ),
    responses={
        200: IP_WHITELIST_STATE,
        400: IP_WHITELIST_LOCKOUT,
        403: OpenApiResponse(description='Not staff/superuser or not from tenant domain')
    },
    tags=['Security']
)
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsStaffOrSuperUser])
def toggle_ip_whitelist(request):
    """Toggle IP whitelist enabled/disabled for the tenant"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    tenant = request.tenant
    enabled = request.data.get('enabled')
    superadmin_bypass = request.data.get('superadmin_bypass')

    # If enabling whitelist, ensure there's at least one entry
    if enabled is True:
        active_entries = TenantIPWhitelist.objects.filter(
            tenant=tenant,
            is_active=True
        ).count()

        if active_entries == 0:
            return Response(
                {'error': 'Cannot enable IP whitelist without any active whitelist entries. Add at least one IP address first.',
                 'code': 'no_active_entries'},
                status=status.HTTP_400_BAD_REQUEST
            )

    final_enabled = tenant.ip_whitelist_enabled if enabled is None else bool(enabled)
    final_bypass = tenant.superadmin_bypass_whitelist if superadmin_bypass is None else bool(superadmin_bypass)
    if _would_lock_out(request, tenant, final_enabled, final_bypass):
        return _lockout_response(request)

    # Update settings
    if enabled is not None:
        tenant.ip_whitelist_enabled = enabled

    if superadmin_bypass is not None:
        tenant.superadmin_bypass_whitelist = superadmin_bypass

    tenant.save()

    logger.info(f"IP whitelist settings updated by {request.user.email}: enabled={tenant.ip_whitelist_enabled}, bypass={tenant.superadmin_bypass_whitelist}")

    return Response({
        'ip_whitelist_enabled': tenant.ip_whitelist_enabled,
        'superadmin_bypass_whitelist': tenant.superadmin_bypass_whitelist,
        'message': 'IP whitelist settings updated successfully'
    })


@extend_schema(
    operation_id='get_current_ip',
    summary='Get Current IP Address',
    description='Get the current request IP address and location. Useful for adding to whitelist.',
    responses={
        200: OpenApiResponse(
            description='Current IP information',
            response=inline_serializer(
                name='CurrentIPInfo',
                fields={
                    'ip_address': serializers.CharField(),
                    'city': serializers.CharField(),
                    'country': serializers.CharField(),
                    'country_code': serializers.CharField(),
                    'is_whitelisted': serializers.BooleanField(
                        help_text='Whether this IP matches an active whitelist entry.'),
                },
            ),
        ),
        403: OpenApiResponse(description='Not from tenant domain')
    },
    tags=['Security']
)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_current_ip(request):
    """Get the current request's IP address and location"""
    if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    ip_address = SecurityService.get_client_ip(request)
    location = SecurityService.get_ip_location(ip_address)

    return Response({
        'ip_address': ip_address,
        'city': location.get('city', ''),
        'country': location.get('country', ''),
        'country_code': location.get('country_code', ''),
        'is_whitelisted': bool(SecurityService.ip_matches_whitelist(request.tenant, ip_address)),
    })
