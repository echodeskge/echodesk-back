"""
Permission helpers for the ticket admin endpoints (columns, tags, item lists,
ticket forms).

Reads stay open to every authenticated user (the kanban, ticket create sheet
and ticket forms all need them). Writes need the matching tenant permission
flag, checked through ``User.has_permission`` so individual ``can_*`` flags,
role grants (manager/admin) and group ``can_*`` flags all count. Staff and
superusers always pass.
"""
from rest_framework import permissions


def user_can_manage(user, codename):
    """True when ``user`` may write objects guarded by ``codename``."""
    if not user or not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    return user.has_permission(codename)


class TenantPermissionForWrites(permissions.BasePermission):
    """
    Authenticated reads; writes need ``required_permission``.

    Subclass and set ``required_permission``, or use
    :func:`write_permission` to build one inline. Read-only actions declared
    with ``@action(methods=['get'])`` are safe-method requests and pass.
    """
    required_permission = None
    message = 'You do not have permission to manage these settings.'

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return user_can_manage(request.user, self.required_permission)


def write_permission(codename):
    """Build a permission class that guards writes with ``codename``."""
    return type(
        f'Can{"".join(p.title() for p in codename.split("_"))}',
        (TenantPermissionForWrites,),
        {
            'required_permission': codename,
            'message': f'You need the "{codename}" permission (or staff) to change this.',
        },
    )


CanManageColumns = write_permission('manage_columns')
CanManageTags = write_permission('manage_tags')
CanManageSettings = write_permission('manage_settings')
