from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CallLogViewSet, ClientViewSet, SipConfigurationViewSet, UserPhoneAssignmentViewSet,
    pbx_settings_detail, pbx_settings_upload_sound, pbx_settings_remove_sound,
    sip_webhook, recording_webhook, call_rating_webhook, call_recording_url_webhook,
    extension_status, call_routing, send_call_review_sms,
)
from . import views_pbx, views_stats, views_pbx_install

router = DefaultRouter()
router.register(r'call-logs', CallLogViewSet, basename='call-logs')
router.register(r'clients', ClientViewSet, basename='clients')
router.register(r'sip-configurations', SipConfigurationViewSet, basename='sip-configurations')
router.register(r'phone-assignments', UserPhoneAssignmentViewSet, basename='phone-assignments')

# PBX management panel
router.register(r'trunks', views_pbx.TrunkViewSet, basename='trunks')
router.register(r'queues', views_pbx.QueueViewSet, basename='queues')
router.register(r'queue-members', views_pbx.QueueMemberViewSet, basename='queue-members')
router.register(r'inbound-routes', views_pbx.InboundRouteViewSet, basename='inbound-routes')
router.register(r'pbx-servers', views_pbx.PbxServerViewSet, basename='pbx-servers')

urlpatterns = [
    path('api/', include(router.urls)),
    path('api/webhooks/sip/', sip_webhook, name='sip-webhook'),
    path('api/webhooks/recording/', recording_webhook, name='recording-webhook'),
    path('api/extensions/status/', extension_status, name='extension-status'),
    path('api/webhooks/call-rating/', call_rating_webhook, name='call-rating-webhook'),
    path('api/webhooks/call-recording-url/', call_recording_url_webhook, name='call-recording-url-webhook'),
    path('api/pbx/call-routing/', call_routing, name='call-routing'),
    path('api/pbx/send-review-sms/', send_call_review_sms, name='send_call_review_sms'),
    # BYO Asterisk install endpoints
    path('api/pbx/install/<str:token>/', views_pbx_install.install_script, name='pbx-install-script'),
    path('api/pbx/install/<str:token>/ping/', views_pbx_install.install_ping, name='pbx-install-ping'),
    path('api/pbx/install/<str:token>/dialplan/', views_pbx_install.install_dialplan, name='pbx-install-dialplan'),
    # PBX Settings (working hours + sounds)
    path('api/pbx-settings/<int:sip_config_id>/', pbx_settings_detail, name='pbx-settings-detail'),
    path('api/pbx-settings/<int:sip_config_id>/upload-sound/', pbx_settings_upload_sound, name='pbx-settings-upload-sound'),
    path('api/pbx-settings/<int:sip_config_id>/remove-sound/', pbx_settings_remove_sound, name='pbx-settings-remove-sound'),

    # Call statistics
    path('api/call-stats/users/', views_stats.users_stats, name='call-stats-users'),
    path('api/call-stats/users/<int:user_id>/timeline/', views_stats.user_timeline, name='call-stats-user-timeline'),
    path('api/call-stats/queues/', views_stats.queue_stats, name='call-stats-queues'),
    path('api/call-stats/overview/', views_stats.overview_stats, name='call-stats-overview'),
]
