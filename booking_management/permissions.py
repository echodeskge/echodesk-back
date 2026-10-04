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
