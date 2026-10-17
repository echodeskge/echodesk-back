from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .password_reset import password_reset_confirm, password_reset_request
from .views import (
    UserViewSet, GroupViewSet, PermissionViewSet, DepartmentViewSet,
    TenantGroupViewSet, NotificationViewSet, tenant_homepage,
    TeamChatUserListView, TeamChatConversationViewSet, TeamChatMessageViewSet,
    upload_team_chat_file, team_chat_unread_count,
    notification_preferences_list, notification_preferences_bulk_update, notification_preferences_types
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

    # Notification Preferences endpoints
    path('api/notification-preferences/', notification_preferences_list, name='notification-preferences-list'),
    path('api/notification-preferences/bulk/', notification_preferences_bulk_update, name='notification-preferences-bulk'),
    path('api/notification-preferences/types/', notification_preferences_types, name='notification-preferences-types'),

    # Team Chat endpoints
    path('api/team-chat/users/', TeamChatUserListView.as_view(), name='team-chat-users'),
    path('api/team-chat/upload/', upload_team_chat_file, name='team-chat-upload'),
    path('api/team-chat/unread-count/', team_chat_unread_count, name='team-chat-unread-count'),
]
