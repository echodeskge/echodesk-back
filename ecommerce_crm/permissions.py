"""
Permission classes for the ecommerce admin API (``/api/ecommerce/admin/``).

Three levels:

* ``HasEcommerceFeature``: the caller can use the e-commerce module at all.
  Same rule as the web sidebar (``user.feature_keys`` contains
  ``ecommerce_crm``), plus staff of a tenant whose plan includes the feature,
  which is what the web settings sidebar shows them.
* ``IsEcommerceManager``: money moves and destructive actions (refund, mark
  paid, delete orders/customers/products, edit shop customers). Superuser,
  staff, the admin or manager role, or the ``access_orders`` permission.
* ``IsEcommerceAdmin``: store configuration (settings with payment
  credentials, languages, homepage, deployment, domains, courier keys).
  Superuser, staff, the admin role, or the ``manage_settings`` permission.

The two higher levels always include the feature check.
"""
from django.db import connection
from rest_framework import permissions

FEATURE_KEY = 'ecommerce_crm'
MANAGER_ROLES = ('admin', 'manager')


def _current_tenant(request=None):
    tenant = getattr(request, 'tenant', None) if request is not None else None
    if tenant is None:
        tenant = getattr(connection, 'tenant', None)
    return tenant


def tenant_has_ecommerce(request=None):
    """True when the current tenant's active subscription includes the feature."""
    from tenants.subscription_service import SubscriptionService

    tenant = _current_tenant(request)
    if tenant is None or getattr(tenant, 'pk', None) is None:
        return False
    try:
        return SubscriptionService.check_tenant_feature(tenant, FEATURE_KEY)
    except Exception:
        return False


def user_has_ecommerce_feature(user, request=None):
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    # Ecommerce shop clients authenticate on the storefront API with their
    # own token type; they are never dashboard users.
    if not hasattr(user, 'has_feature'):
        return False
    if request is not None and hasattr(request, '_ecommerce_feature_ok'):
        return request._ecommerce_feature_ok
    ok = bool(user.has_feature(FEATURE_KEY))
    if not ok and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False)):
        ok = tenant_has_ecommerce(request)
    if request is not None:
        request._ecommerce_feature_ok = ok
    return ok


def is_ecommerce_manager(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return True
    if getattr(user, 'role', None) in MANAGER_ROLES:
        return True
    has_perm = getattr(user, 'has_permission', None)
    return bool(has_perm and has_perm('access_orders'))


def is_ecommerce_admin(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return True
    if getattr(user, 'role', None) == 'admin':
        return True
    has_perm = getattr(user, 'has_permission', None)
    return bool(has_perm and has_perm('manage_settings'))


class HasEcommerceFeature(permissions.BasePermission):
    message = 'Your account does not have access to the e-commerce module.'

    def has_permission(self, request, view):
        return user_has_ecommerce_feature(request.user, request)


class IsEcommerceManager(permissions.BasePermission):
    message = ('Only staff, administrators, managers or users with the '
               '"access orders" permission can do this.')

    def has_permission(self, request, view):
        return (user_has_ecommerce_feature(request.user, request)
                and is_ecommerce_manager(request.user))


class IsEcommerceAdmin(permissions.BasePermission):
    message = 'Only staff or administrators can change the store configuration.'

    def has_permission(self, request, view):
        return (user_has_ecommerce_feature(request.user, request)
                and is_ecommerce_admin(request.user))


class EcommerceAdminWritePermission(permissions.BasePermission):
    """Feature for reads; store-admin level for any write."""
    message = IsEcommerceAdmin.message

    def has_permission(self, request, view):
        if not user_has_ecommerce_feature(request.user, request):
            self.message = HasEcommerceFeature.message
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        self.message = IsEcommerceAdmin.message
        return is_ecommerce_admin(request.user)


class EcommercePermissionMixin:
    """Per-action permission levels for the admin viewsets.

    * every action requires ``HasEcommerceFeature``
    * actions in ``manager_actions`` also require ``is_ecommerce_manager``
    * actions in ``admin_actions`` also require ``is_ecommerce_admin``
    """
    manager_actions = ()
    admin_actions = ()

    def get_permissions(self):
        action = getattr(self, 'action', None)
        if action in self.admin_actions:
            return [IsEcommerceAdmin()]
        if action in self.manager_actions:
            return [IsEcommerceManager()]
        return [HasEcommerceFeature()]
