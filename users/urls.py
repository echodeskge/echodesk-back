from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .password_reset import password_reset_confirm, password_reset_request
from .views import (
    UserViewSet, GroupViewSet, PermissionViewSet, DepartmentViewSet,
    TenantGroupViewSet, NotificationViewSet, tenant_homepage,
    TeamChatUserListView, TeamChatConversationViewSet, TeamChatMessageViewSet,
    upload_team_chat_file, team_chat_unread_count,
    notification_preferences_list, notification_preferences_bulk_update, notification_preferences_types,
    auth_devices_list, auth_device_revoke, auth_devices_revoke_others,
)

router = DefaultRouter()
router.register(r'users', UserViewSet)
router.register(r'groups', GroupViewSet)
router.register(r'tenant-groups', TenantGroupViewSet)
router.register(r'permissions', PermissionViewSet)
router.register(r'departments', DepartmentViewSet)
router.register(r'notifications', NotificationViewSet, basename='notification')

# Team Chat routes
router.register(r'team-chat/conversations', TeamChatConversationViewSet, basename='team-chat-conversation')
router.register(r'team-chat/messages', TeamChatMessageViewSet, basename='team-chat-message')

urlpatterns = [
    path('', tenant_homepage, name='tenant_homepage'),
    # Before the router: otherwise users/<pk>/ swallows "password-reset"
    path('api/users/password-reset/', password_reset_request, name='password-reset-request'),
    path('api/users/password-reset/confirm/', password_reset_confirm, name='password-reset-confirm'),
    path('api/', include(router.urls)),

    # Signed-in devices of the current user (mobile AuthDeviceToken sessions)
    path('api/auth/devices/', auth_devices_list, name='auth-devices-list'),
    path('api/auth/devices/revoke-others/', auth_devices_revoke_others, name='auth-devices-revoke-others'),
    path('api/auth/devices/<str:id>/', auth_device_revoke, name='auth-devices-revoke'),

    # Notification Preferences endpoints
    path('api/notification-preferences/', notification_preferences_list, name='notification-preferences-list'),
    path('api/notification-preferences/bulk/', notification_preferences_bulk_update, name='notification-preferences-bulk'),
    path('api/notification-preferences/types/', notification_preferences_types, name='notification-preferences-types'),

    # Team Chat endpoints
    path('api/team-chat/users/', TeamChatUserListView.as_view(), name='team-chat-users'),
    path('api/team-chat/upload/', upload_team_chat_file, name='team-chat-upload'),
    path('api/team-chat/unread-count/', team_chat_unread_count, name='team-chat-unread-count'),
]
