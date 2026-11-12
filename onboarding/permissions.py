"""Onboarding is for tenant admins: the same check the workspace settings
endpoints use (``tenants.views._can_manage_tenant_settings`` →
``tickets.permissions.user_can_manage(user, 'manage_settings')``): staff,
superusers and holders of ``manage_settings`` (own flag or the admin role)."""
from rest_framework import permissions

from tickets.permissions import user_can_manage

SETTINGS_PERMISSION = 'manage_settings'


def is_onboarding_admin(user):
    return user_can_manage(user, SETTINGS_PERMISSION)


class IsTenantAdmin(permissions.BasePermission):
    message = 'You need the manage_settings permission (or staff) to run onboarding.'

    def has_permission(self, request, view):
        return is_onboarding_admin(request.user)
