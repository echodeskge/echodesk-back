"""
Security Service for handling IP detection, user agent parsing, geolocation, and security logging.
"""
import ipaddress
import logging
import requests
from typing import Dict, Optional
from django.utils import timezone

logger = logging.getLogger(__name__)


class SecurityService:
    """
    Service class for security-related operations.
    """

    @staticmethod
    def get_client_ip(request) -> str:
        """
        Extract the client's real IP address from the request.
        Handles proxies and load balancers by checking X-Forwarded-For header.
        """
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            # X-Forwarded-For can contain multiple IPs, first one is the client's
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR', '0.0.0.0')
        return ip

    @staticmethod
    def parse_user_agent(user_agent_string: str) -> Dict:
        """
        Parse user agent string to extract device type, browser, and OS.
        Uses simple parsing without external library.
        """
        result = {
            'device_type': 'unknown',
            'browser': '',
            'operating_system': ''
        }

        if not user_agent_string:
            return result

        ua_lower = user_agent_string.lower()

        # Detect device type
        if any(mobile in ua_lower for mobile in ['iphone', 'android', 'mobile', 'blackberry', 'windows phone']):
            if 'ipad' in ua_lower or 'tablet' in ua_lower:
                result['device_type'] = 'tablet'
            else:
                result['device_type'] = 'mobile'
        else:
            result['device_type'] = 'desktop'

        # Detect browser
        if 'edg/' in ua_lower or 'edge' in ua_lower:
            result['browser'] = 'Edge'
        elif 'opr/' in ua_lower or 'opera' in ua_lower:
            result['browser'] = 'Opera'
        elif 'chrome' in ua_lower and 'chromium' not in ua_lower:
            result['browser'] = 'Chrome'
        elif 'safari' in ua_lower and 'chrome' not in ua_lower:
            result['browser'] = 'Safari'
        elif 'firefox' in ua_lower:
            result['browser'] = 'Firefox'
        elif 'msie' in ua_lower or 'trident' in ua_lower:
            result['browser'] = 'Internet Explorer'
        else:
            result['browser'] = 'Unknown'

        # Detect operating system
        if 'windows' in ua_lower:
            result['operating_system'] = 'Windows'
        elif 'mac os' in ua_lower or 'macintosh' in ua_lower:
            result['operating_system'] = 'macOS'
        elif 'linux' in ua_lower and 'android' not in ua_lower:
            result['operating_system'] = 'Linux'
        elif 'android' in ua_lower:
            result['operating_system'] = 'Android'
        elif 'iphone' in ua_lower or 'ipad' in ua_lower:
            result['operating_system'] = 'iOS'
        else:
            result['operating_system'] = 'Unknown'

        return result

    @staticmethod
    def get_ip_location(ip_address: str) -> Dict:
        """
        Get geolocation information from IP address using ip-api.com.
        Free tier allows 45 requests/minute.
        """
        result = {
            'city': '',
            'country': '',
            'country_code': ''
        }

        # Skip geolocation for private/local IPs
        try:
            ip = ipaddress.ip_address(ip_address)
            if ip.is_private or ip.is_loopback or ip.is_reserved:
                result['city'] = 'Local'
                result['country'] = 'Local Network'
                result['country_code'] = 'LO'
                return result
        except ValueError:
            return result

        try:
            # Use ip-api.com free tier (no API key required)
            response = requests.get(
                f'http://ip-api.com/json/{ip_address}',
                timeout=3,
                params={'fields': 'status,city,country,countryCode'}
            )
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'success':
                    result['city'] = data.get('city', '')
                    result['country'] = data.get('country', '')
                    result['country_code'] = data.get('countryCode', '')
        except Exception as e:
            logger.warning(f"Failed to get IP location for {ip_address}: {e}")

        return result

    @classmethod
    def log_security_event(
        cls,
        event_type: str,
        request,
        user=None,
        attempted_email: str = '',
        failure_reason: str = '',
        tenant=None
    ):
        """
        Create a SecurityLog entry with all metadata.
        Tenant can be passed explicitly or extracted from request.
        """
        from .models import SecurityLog

        # Get tenant from request if not explicitly provided
        if tenant is None and hasattr(request, 'tenant'):
            tenant = request.tenant

        ip_address = cls.get_client_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', '')
        ua_info = cls.parse_user_agent(user_agent)
        location = cls.get_ip_location(ip_address)

        try:
            log = SecurityLog.objects.create(
                tenant=tenant,
                user_id=user.id if user else None,
                attempted_email=attempted_email or (user.email if user else ''),
                event_type=event_type,
                ip_address=ip_address,
                user_agent=user_agent,
                device_type=ua_info['device_type'],
                browser=ua_info['browser'],
                operating_system=ua_info['operating_system'],
                city=location['city'],
                country=location['country'],
                country_code=location['country_code'],
                failure_reason=failure_reason
            )
            logger.info(f"Security event logged: {event_type} for {user.email if user else attempted_email} from {ip_address}")
            return log
        except Exception as e:
            logger.error(f"Failed to log security event: {e}")
            return None

    @classmethod
    def is_ip_whitelisted(cls, tenant, ip_address: str, is_superuser: bool = False) -> bool:
        """
        Check if an IP address is allowed to access the tenant.

        Returns True if:
        - IP whitelist is disabled for the tenant
        - User is superuser and superadmin_bypass_whitelist is enabled
        - IP is in the whitelist (exact match or CIDR range)
        """
        from .models import TenantIPWhitelist

        # If whitelist is disabled, all IPs are allowed
        if not tenant.ip_whitelist_enabled:
            return True

        # If superadmin bypass is enabled and user is superuser
        if tenant.superadmin_bypass_whitelist and is_superuser:
            return True

        return cls.ip_matches_whitelist(tenant, ip_address)

    @staticmethod
    def ip_matches_whitelist(tenant, ip_address: str, exclude_entry_id=None, entry_overrides=None) -> bool:
        """
        Whether ``ip_address`` matches one of the tenant's active whitelist
        entries, whatever the enabled flag says.

        ``exclude_entry_id`` / ``entry_overrides`` ({entry_id: {field: value}})
        evaluate a pending delete or edit before it is saved.
        """
        from .models import TenantIPWhitelist

        try:
            client_ip = ipaddress.ip_address(ip_address)
        except ValueError:
            logger.warning(f"Invalid IP address format: {ip_address}")
            return False

        overrides = entry_overrides or {}
        for entry in TenantIPWhitelist.objects.filter(tenant=tenant):
            if exclude_entry_id is not None and entry.pk == exclude_entry_id:
                continue
            values = overrides.get(entry.pk, {})
            is_active = values.get('is_active', entry.is_active)
            if not is_active:
                continue
            address = values.get('ip_address', entry.ip_address)
            cidr = values.get('cidr_notation', entry.cidr_notation)
            try:
                if cidr:
                    # Check CIDR range
                    network = ipaddress.ip_network(f"{address}/{cidr}", strict=False)
                    if client_ip in network:
                        return True
                else:
                    # Check exact IP match
                    if client_ip == ipaddress.ip_address(address):
                        return True
            except ValueError as e:
                logger.warning(f"Invalid whitelist entry: {address}/{cidr}: {e}")
                continue

        return False

    @classmethod
    def get_client_location_summary(cls, request) -> str:
        """
        Get a human-readable location summary for the current request.
        """
        ip_address = cls.get_client_ip(request)
        location = cls.get_ip_location(ip_address)

        if location['city'] and location['country']:
            return f"{location['city']}, {location['country']}"
        elif location['country']:
            return location['country']
        else:
            return 'Unknown Location'
