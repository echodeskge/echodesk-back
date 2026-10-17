"""Permission helpers for the calls / PBX endpoints.

Three levels:

* **ip_calling feature**: every calls endpoint needs the tenant's
  subscription to include ``ip_calling`` (:class:`HasIpCallingFeature`).
* **PBX admin**: writes to the PBX configuration (SIP configs, extensions,
  trunks, queues, inbound routes, PBX servers, working hours / sounds) and
  deleting call logs. A PBX admin is a staff user, a superuser, or a user
  with the ``manage_settings`` permission (``can_manage_settings`` flag or
  the ``admin`` role), so the web settings pages keep working for the
  people who already manage tenant settings.
* **Call statistics**: PBX admins plus anyone with ``view_reports``
  (managers by role). A user may always read their own timeline.

Recordings stay stricter: only staff/superusers may play them (see
``crm.serializers._is_admin_request`` and ``CallLogViewSet.stream_recording``).
"""

from rest_framework import permissions

from tenants.permissions import HasSubscriptionFeature


class HasIpCallingFeature(HasSubscriptionFeature):
    """The tenant subscription must include the ``ip_calling`` feature."""

    required_feature = 'ip_calling'


def _has_perm(user, codename):
    checker = getattr(user, 'has_permission', None)
    if checker is None:
        return False
    try:
        return bool(checker(codename))
    except Exception:  # noqa: BLE001 - defensive: never 500 on a perm check
        return False


def is_pbx_admin(user):
    """Staff, superuser, or ``manage_settings`` holder."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_staff or user.is_superuser:
        return True
    return _has_perm(user, 'manage_settings')


def can_view_call_stats(user):
    """PBX admins plus ``view_reports`` holders (managers by role)."""
    if is_pbx_admin(user):
        return True
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    return _has_perm(user, 'view_reports')


class IsPbxAdmin(permissions.BasePermission):
    message = 'Only PBX administrators can perform this action.'

    def has_permission(self, request, view):
        return is_pbx_admin(request.user)


class IsPbxAdminOrReadOnly(permissions.BasePermission):
    """Reads for everyone who passed the other checks; writes for PBX admins."""

    message = 'Only PBX administrators can change the PBX configuration.'

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return is_pbx_admin(request.user)


class CanViewCallStats(permissions.BasePermission):
    message = 'You do not have permission to view call statistics.'

    def has_permission(self, request, view):
        return can_view_call_stats(request.user)
