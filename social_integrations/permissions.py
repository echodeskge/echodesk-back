"""
Custom permission classes for social media integrations
"""
from rest_framework import permissions


class CanManageSocialConnections(permissions.BasePermission):
    """
    Permission to manage social media connections (connect/disconnect pages)

    - Read access (GET): Users with social_integrations feature
    - Write access (POST/PUT/DELETE): Admins only or users with manage_social_connections permission
    """
    message = "You do not have permission to manage social media connections."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # Read-only permissions: Check if user has social_integrations feature
        if request.method in permissions.SAFE_METHODS:
            return request.user.has_feature('social_integrations')

        # Write permissions: feature check + admin role or explicit permission
        if not request.user.has_feature('social_integrations'):
            return False
        return (
            request.user.role == 'admin' or
            request.user.has_permission('manage_social_connections')
        )


class CanViewSocialMessages(permissions.BasePermission):
    """
    Permission to view social media messages

    - Users with social_integrations feature can view messages
    """
    message = "You do not have permission to view social media messages."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # Check if user has social_integrations feature through their group
        return request.user.has_feature('social_integrations')


class CanSendSocialMessages(permissions.BasePermission):
    """
    Permission to send social media messages

    - Users with social_integrations feature can send messages
    """
    message = "You do not have permission to send social media messages."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # All users with social_integrations feature can send messages
        return request.user.has_feature('social_integrations')


class CanManageSocialSettings(permissions.BasePermission):
    """
    Permission to manage social media integration settings

    - Read access (GET): Users with social_integrations feature
    - Write access (POST/PUT/PATCH/DELETE): Admins only or users with manage_social_settings permission
    """
    message = "You do not have permission to manage social media settings."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # Read-only permissions: Check if user has social_integrations feature
        if request.method in permissions.SAFE_METHODS:
            return request.user.has_feature('social_integrations')

        # Write permissions: feature check + admin role or explicit permission
        if not request.user.has_feature('social_integrations'):
            return False
        return (
            request.user.role == 'admin' or
            request.user.has_permission('manage_social_settings')
        )


class CanAccessSocial(permissions.BasePermission):
    """
    Broad social-access gate for shared, low-risk team resources (e.g. quick
    reply templates) that any user involved in social messaging should be able
    to manage.

    Grants access when the user is staff/superuser, OR has the
    `social_integrations` subscription feature, OR has any explicit social
    permission toggle (view/send messages, manage settings/connections).

    Unlike CanSendSocialMessages — which checks ONLY the subscription feature
    (the intersection of the tenant's plan and the user's group feature-keys) —
    this honors the per-user/group permission toggles an admin actually sets, so
    an agent granted social-message permissions isn't blocked just because their
    group's feature-key set omits `social_integrations`.
    """
    message = "You do not have permission to manage social features."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False

        if user.is_staff or user.is_superuser:
            return True

        if user.has_feature('social_integrations'):
            return True

        return (
            user.has_permission('view_social_messages') or
            user.has_permission('send_social_messages') or
            user.has_permission('manage_social_settings') or
            user.has_permission('manage_social_connections')
        )


class IsSuperAdmin(permissions.BasePermission):
    """
    Permission that only allows superadmins (is_superuser=True)
    """
    message = "This action is restricted to superadmins only."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        return request.user.is_superuser


class IsStaffUser(permissions.BasePermission):
    """
    Permission that only allows staff users (is_staff=True)
    """
    message = "This action is restricted to staff users only."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        return request.user.is_staff


class CanUseAICompanion(permissions.BasePermission):
    """
    Permission to use AI companion features (summaries, per-conversation
    AI state) — any user whose group grants the ai_companion feature.
    """
    message = "You do not have permission to use the AI companion."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return request.user.has_feature('ai_companion')


class CanManageAICompanion(permissions.BasePermission):
    """
    Permission to manage AI companion settings.

    - Read access (GET): Users with the ai_companion feature
    - Write access: feature + admin role (mirrors CanManageSocialSettings)
    """
    message = "You do not have permission to manage AI companion settings."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if not request.user.has_feature('ai_companion'):
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        # Write access: admin role, or an explicit settings permission.
        # `has_permission` returns True for superusers, matching how
        # CanManageSocialSettings gates the rest of social settings.
        return (
            request.user.role == 'admin' or
            request.user.has_permission('manage_social_settings')
        )
