from rest_framework import permissions


class IsAuthenticatedBookingClient(permissions.BasePermission):
    """
    Permission check for authenticated booking clients.
    Uses the unified social_integrations.Client model (not deprecated BookingClient).
    """
    def has_permission(self, request, view):
        from social_integrations.models import Client
        if not request.user:
            return False
        if isinstance(request.user, Client):
            return getattr(request.user, 'is_booking_enabled', False)
        return False


class IsBookingOwner(permissions.BasePermission):
    """
    Object-level permission to only allow booking owners to view/edit their bookings.
    """
    def has_object_permission(self, request, view, obj):
        from social_integrations.models import Client
        if isinstance(request.user, Client):
            return obj.client == request.user
        return False


class HasBookingManagementFeature(permissions.BasePermission):
    """
    Check if tenant has booking_management feature enabled.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if hasattr(request.user, 'has_feature'):
            return request.user.has_feature('booking_management')
        return False


def is_booking_admin(user):
    """Runs the business's bookings: staff flag, superuser, or role admin/manager.

    Booking staff without one of these may work bookings (view, confirm,
    check in, complete, cancel, move, take payments) but not delete them
    or change the catalog (services, categories, staff) or the settings.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    return bool(
        getattr(user, 'is_superuser', False)
        or getattr(user, 'is_staff', False)
        or getattr(user, 'role', None) in ('admin', 'manager')
    )


class IsBookingAdmin(permissions.BasePermission):
    """Always requires a booking admin (see is_booking_admin)."""
    message = 'Only admins and managers can do this.'

    def has_permission(self, request, view):
        return is_booking_admin(request.user)


class IsBookingAdminForWrites(permissions.BasePermission):
    """Reads for everyone the other permissions let in; writes for booking admins."""
    message = 'Only admins and managers can change this.'

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return is_booking_admin(request.user)


class IsBookingAdminForDestroy(permissions.BasePermission):
    """Only booking admins may hard-delete; everything else is unchanged."""
    message = 'Only admins and managers can delete bookings.'

    def has_permission(self, request, view):
        if request.method == 'DELETE':
            return is_booking_admin(request.user)
        return True


class PublicBookingEnabled(permissions.BasePermission):
    """
    Gate for the customer-facing booking API: the tenant must have the
    `booking_management` feature and must not have switched its public page
    off. Otherwise the API answers 404, as if the tenant had no booking site.
    """
    def has_permission(self, request, view):
        from django.http import Http404
        from .utils import get_booking_settings

        from tenants.subscription_service import SubscriptionService

        tenant = getattr(request, 'tenant', None)
        if tenant is None or not SubscriptionService.check_tenant_feature(tenant, 'booking_management'):
            raise Http404
        booking_settings = get_booking_settings()
        if booking_settings is not None and not booking_settings.public_page_enabled:
            raise Http404
        return True
