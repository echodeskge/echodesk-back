"""
IP Whitelist Middleware - Blocks all requests from non-whitelisted IPs
when IP whitelist is enabled for a tenant.
"""
import logging
from django.http import JsonResponse
from tenant_schemas.utils import get_public_schema_name
from rest_framework.authtoken.models import Token
from .security_service import SecurityService

logger = logging.getLogger(__name__)


class IPWhitelistMiddleware:
    """
    Middleware that enforces IP whitelist for all tenant requests.

    This middleware:
    - Only applies to tenant subdomains (not public schema)
    - Checks if IP whitelist is enabled for the tenant
    - Allows requests from whitelisted IPs
    - Allows superadmin bypass if configured
    - Blocks all other requests with 403 Forbidden
    """

    # Paths that should be excluded from IP whitelist check
    # These are public endpoints that don't require authentication
    EXCLUDED_PATHS = [
        '/api/auth/login/',  # Login endpoint (has its own IP check)
        '/api/auth/logout/',
        '/api/auth/refresh/',
        '/api/tenant/info/',  # Public tenant info
        '/api/security/current-ip/',  # Need this to show current IP even when blocked
        '/api/help/',  # Help center is public
        # Public booking site: used by the tenant's customers from anywhere,
        # and the payment gateway's callback.
        '/api/bookings/client/',
        '/api/bookings/clients/',
        '/api/bookings/payment-webhook/',
        '/health/',  # Health check
        '/api/schema/',  # API schema
        '/admin/',  # Django admin
        '/static/',  # Static files
        '/media/',  # Media files
    ]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Skip for non-tenant requests (public schema)
        if not hasattr(request, 'tenant') or request.tenant.schema_name == get_public_schema_name():
            return self.get_response(request)

        # Skip for excluded paths
        if self._is_excluded_path(request.path):
            return self.get_response(request)

        # Check if IP whitelist is enabled for this tenant
        tenant = request.tenant
        if not tenant.ip_whitelist_enabled:
            return self.get_response(request)

        # Get client IP
        client_ip = SecurityService.get_client_ip(request)

        # Check if user is superuser (for bypass check)
        # Need to manually check token auth since DRF hasn't processed it yet
        is_superuser = self._check_superuser_from_token(request)

        # Check if IP is whitelisted
        if SecurityService.is_ip_whitelisted(tenant, client_ip, is_superuser):
            return self.get_response(request)

        # IP is not whitelisted - block the request
        logger.warning(
            f"IP whitelist block: {client_ip} attempted to access {request.path} "
            f"on tenant {tenant.schema_name}"
        )

        return JsonResponse(
            {
                'error': 'Access denied',
                'message': 'Your IP address is not allowed to access this resource.',
                'ip_address': client_ip,
            },
            status=403
        )

    def _is_excluded_path(self, path: str) -> bool:
        """Check if the path should be excluded from IP whitelist check."""
        for excluded in self.EXCLUDED_PATHS:
            if path.startswith(excluded):
                return True
        return False

    def _check_superuser_from_token(self, request) -> bool:
        """
        Check if the request has a valid token belonging to a superuser.
        This is needed because DRF token auth happens at the view level,
        not in middleware.
        """
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header.startswith('Token '):
            return False

        token_key = auth_header[6:]  # Remove 'Token ' prefix
        try:
            token = Token.objects.select_related('user').get(key=token_key)
            return token.user.is_superuser
        except Token.DoesNotExist:
            return False
        except Exception as e:
            logger.error(f"Error checking token for superuser: {e}")
            return False
