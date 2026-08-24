from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views, admin_views, ai_views, telegram_views

# Create a router and register our viewsets with it
router = DefaultRouter()
router.register(r'facebook-pages', views.FacebookPageConnectionViewSet, basename='facebook_pages')
router.register(r'facebook-messages', views.FacebookMessageViewSet, basename='facebook_messages')
router.register(r'instagram-accounts', views.InstagramAccountConnectionViewSet, basename='instagram_accounts')
router.register(r'instagram-messages', views.InstagramMessageViewSet, basename='instagram_messages')
router.register(r'whatsapp-accounts', views.WhatsAppBusinessAccountViewSet, basename='whatsapp_accounts')
router.register(r'whatsapp-messages', views.WhatsAppMessageViewSet, basename='whatsapp_messages')
router.register(r'whatsapp-contacts', views.WhatsAppContactViewSet, basename='whatsapp_contacts')
router.register(r'email-messages', views.EmailMessageViewSet, basename='email_messages')
router.register(r'email-drafts', views.EmailDraftViewSet, basename='email_drafts')
router.register(r'tiktok-messages', views.TikTokMessageViewSet, basename='tiktok_messages')
router.register(r'telegram-messages', telegram_views.TelegramMessageViewSet, basename='telegram_messages')
router.register(r'quick-replies', views.QuickReplyViewSet, basename='quick_replies')
router.register(r'clients', views.SocialClientViewSet, basename='social_clients')
router.register(r'clients/custom-fields', views.SocialClientCustomFieldViewSet, basename='social_client_custom_fields')

# URL patterns for the social integrations app
urlpatterns = [
    # Include router URLs for ViewSets
    path('', include(router.urls)),
    
    # Facebook OAuth endpoints
    path('facebook/oauth/start/', views.facebook_oauth_start, name='facebook_oauth_start'),
    path('facebook/oauth/callback/', views.facebook_oauth_callback, name='facebook_oauth_callback'),
    path('facebook/oauth/debug/', views.facebook_debug_callback, name='facebook_debug_callback'),
    path('facebook/api/test/', views.test_facebook_api_access, name='facebook_api_test'),
    path('facebook/webhook/test/', views.webhook_test_endpoint, name='webhook_test'),
    path('facebook/database/test/', views.test_database_save, name='database_test'),
    path('facebook/database/debug/', views.debug_database_status, name='database_debug'),
    path('facebook/pages/debug/', views.debug_facebook_pages, name='facebook_pages_debug'),
    path('facebook/status/', views.facebook_connection_status, name='facebook_status'),
    path('facebook/disconnect/', views.facebook_disconnect, name='facebook_disconnect'),
    path('facebook/pages/<str:page_id>/disconnect/', views.facebook_page_disconnect, name='facebook_page_disconnect'),
    path('facebook/send-message/', views.facebook_send_message, name='facebook_send_message'),
    path('facebook/webhook/', views.facebook_webhook, name='facebook_webhook'),

    # Platform-wide endpoints
    path('clear-history/', views.clear_platform_history, name='clear_platform_history'),
    path('messaging-window/', views.check_messaging_window, name='check_messaging_window'),

    # Instagram endpoints
    path('instagram/status/', views.instagram_connection_status, name='instagram_status'),
    path('instagram/disconnect/', views.instagram_disconnect, name='instagram_disconnect'),
    path('instagram/send-message/', views.instagram_send_message, name='instagram_send_message'),
    path('instagram/webhook/', views.instagram_webhook, name='instagram_webhook'),

    # WhatsApp endpoints
    path('whatsapp/oauth/start/', views.whatsapp_oauth_start, name='whatsapp_oauth_start'),
    path('whatsapp/embedded-signup/callback/', views.whatsapp_embedded_signup_callback, name='whatsapp_embedded_signup_callback'),
    path('whatsapp/status/', views.whatsapp_connection_status, name='whatsapp_status'),
    path('whatsapp/disconnect/', views.whatsapp_disconnect, name='whatsapp_disconnect'),
    path('whatsapp/send-message/', views.whatsapp_send_message, name='whatsapp_send_message'),
    path('whatsapp/webhook/', views.whatsapp_webhook, name='whatsapp_webhook'),

    # WhatsApp Media Proxy (avoids browser ORB/CORS blocking on Meta URLs)
    path('whatsapp-media/<str:media_id>/', views.whatsapp_media_proxy, name='whatsapp_media_proxy'),

    # WhatsApp Template Management
    path('whatsapp/<str:waba_id>/templates/', views.whatsapp_list_templates, name='whatsapp_list_templates'),
    path('whatsapp/<str:waba_id>/templates/sync/', views.whatsapp_sync_templates, name='whatsapp_sync_templates'),
    path('whatsapp/templates/create/', views.whatsapp_create_template, name='whatsapp_create_template'),
    path('whatsapp/templates/<int:template_id>/delete/', views.whatsapp_delete_template, name='whatsapp_delete_template'),
    path('whatsapp/templates/send/', views.whatsapp_send_template_message, name='whatsapp_send_template_message'),

    # Email endpoints
    path('email/status/', views.email_connection_status, name='email_status'),
    path('email/connect/', views.email_connect, name='email_connect'),
    path('email/disconnect/', views.email_disconnect, name='email_disconnect'),
    path('email/update/', views.email_update_connection, name='email_update_connection'),
    path('email/reactivate/', views.email_reactivate, name='email_reactivate'),
    path('email/send/', views.email_send, name='email_send'),
    path('email/action/', views.email_action, name='email_action'),
    path('email/folders/', views.email_folders, name='email_folders'),
    path('email/sync/', views.email_sync, name='email_sync'),
    path('email/sync/debug/', views.email_sync_debug, name='email_sync_debug'),
    path('email/sync/settings/', views.email_update_sync_days, name='email_update_sync_days'),
    path('email/signature/', views.email_signature_view, name='email_signature'),

    # Email User Assignment endpoints
    path('email/assignments/', views.email_assignments_list, name='email_assignments_list'),
    path('email/assignments/create/', views.email_assignment_create, name='email_assignment_create'),
    path('email/assignments/<int:assignment_id>/', views.email_assignment_delete, name='email_assignment_delete'),
    path('email/<int:connection_id>/assigned-users/', views.email_connection_assigned_users, name='email_connection_assigned_users'),

    # TikTok endpoints
    path('tiktok/oauth/start/', views.tiktok_oauth_start, name='tiktok_oauth_start'),
    path('tiktok/oauth/callback/', views.tiktok_oauth_callback, name='tiktok_oauth_callback'),
    path('tiktok/webhook/', views.tiktok_webhook, name='tiktok_webhook'),
    path('tiktok/status/', views.tiktok_status, name='tiktok_status'),
    path('tiktok/disconnect/', views.tiktok_disconnect, name='tiktok_disconnect'),

    # Telegram (MTProto) endpoints
    path('telegram/connect/', telegram_views.telegram_connect, name='telegram_connect'),
    path('telegram/verify/', telegram_views.telegram_verify, name='telegram_verify'),
    path('telegram/status/', telegram_views.telegram_status, name='telegram_status'),
    path('telegram/disconnect/', telegram_views.telegram_disconnect, name='telegram_disconnect'),
    path('telegram/send-message/', telegram_views.telegram_send_message, name='telegram_send_message'),
    path('tiktok/send-message/', views.tiktok_send_message, name='tiktok_send_message'),

    # Webhook debugging endpoints
    path('webhook-status/', views.webhook_status, name='webhook_status'),
    path('webhook-logs/', views.webhook_debug_logs, name='webhook_debug_logs'),
    path('webhook-test/', views.webhook_test_receiver, name='webhook_test'),

    # Settings endpoint
    path('settings/', views.social_settings, name='social_settings'),

    # Chat Assignment endpoints
    path('assignments/', views.my_assignments, name='my_assignments'),
    path('assignments/all/', views.all_assignments, name='all_assignments'),
    path('assignments/assign/', views.assign_chat, name='assign_chat'),
    path('assignments/unassign/', views.unassign_chat, name='unassign_chat'),
    path('assignments/transfer/', views.transfer_chat, name='transfer_chat'),
    path('assignments/status/', views.get_assignment_status, name='assignment_status'),
    path('assignments/bulk-status/', views.bulk_assignment_status, name='bulk_assignment_status'),
    path('assignments/start-session/', views.start_session, name='start_session'),
    path('assignments/end-session/', views.end_session, name='end_session'),

    # Rating statistics endpoint (superadmin only)
    path('rating-statistics/', views.rating_statistics, name='rating_statistics'),
    path('rating-statistics/user/<int:user_id>/', views.user_rating_sessions, name='user_rating_sessions'),

    # Public rating endpoints (no authentication required)
    path('public/rating/<str:token>/', views.get_rating_info, name='public_rating_info'),
    path('public/rating/<str:token>/submit/', views.submit_public_rating, name='public_rating_submit'),

    # Unified conversations endpoint
    path('conversations/', views.unified_conversations, name='unified_conversations'),

    # Unread messages count endpoint
    path('unread-count/', views.unread_messages_count, name='unread_messages_count'),

    # Mark conversation as read/unread endpoint
    path('mark-read/', views.mark_conversation_read, name='mark_conversation_read'),
    path('mark-unread/', views.mark_conversation_unread, name='mark_conversation_unread'),
    path('mark-all-read/', views.mark_all_conversations_read, name='mark_all_conversations_read'),

    # Archive/History endpoints
    path('conversations/archive/', views.archive_conversation, name='archive_conversation'),
    path('conversations/unarchive/', views.unarchive_conversation, name='unarchive_conversation'),
    path('conversations/archive-all/', views.archive_all_conversations, name='archive_all_conversations'),

    # Delete conversation endpoint (superadmin only)
    path('delete-conversation/', views.delete_conversation, name='delete_conversation'),

    # Publishing OAuth (opt-in for auto-posting)
    path('facebook/oauth/start-publishing/', views.facebook_oauth_start_publishing, name='facebook_oauth_start_publishing'),

    # Auto-posting endpoints
    path('auto-post/publishing-status/', views.auto_post_publishing_status, name='auto_post_publishing_status'),
    path('auto-post/settings/', views.auto_post_settings, name='auto_post_settings'),
    path('auto-post/list/', views.auto_post_list, name='auto_post_list'),
    path('auto-post/<int:pk>/', views.auto_post_detail, name='auto_post_detail'),
    path('auto-post/<int:pk>/approve/', views.auto_post_approve, name='auto_post_approve'),
    path('auto-post/<int:pk>/reject/', views.auto_post_reject, name='auto_post_reject'),
    path('auto-post/<int:pk>/edit/', views.auto_post_edit, name='auto_post_edit'),
    path('auto-post/generate/', views.auto_post_generate, name='auto_post_generate'),
    path('auto-post/<int:pk>/publish/', views.auto_post_publish, name='auto_post_publish'),

    # Admin OAuth endpoints
    path('admin/facebook/oauth/start/', admin_views.facebook_oauth_admin_start, name='admin_facebook_oauth_start'),
    path('admin/facebook/oauth/callback/', admin_views.facebook_oauth_admin_callback, name='admin_facebook_oauth_callback'),

    # AI companion
    path('ai/settings/', ai_views.ai_companion_settings, name='ai_companion_settings'),
    path('ai/summarize/', ai_views.ai_summarize_conversation, name='ai_summarize_conversation'),
    path('ai/summaries/', ai_views.ai_conversation_summaries, name='ai_conversation_summaries'),
    path('ai/state/', ai_views.ai_conversation_state, name='ai_conversation_state'),
    path('ai/test/', ai_views.ai_companion_test, name='ai_companion_test'),
]

app_name = 'social_integrations'
