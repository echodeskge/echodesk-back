"""
URL configuration for amanati_crm project.
This is the main URL configuration for tenant-specific routes.
"""
from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from django.http import JsonResponse
from ecommerce_crm.schema import EcommerceClientSchemaGenerator

def sentry_test_error(request):
    """
    Test endpoint to verify Sentry is working.
    This will trigger an error that should appear in Sentry dashboard.
    """
    division_by_zero = 1 / 0
    return JsonResponse({'status': 'error not triggered'})


def api_root_catchall(request):
    """
    Catch-all for /api without trailing slash to prevent APPEND_SLASH errors.
    Returns proper 404 instead of triggering RuntimeError for POST/PUT/PATCH.
    """
    return JsonResponse(
        {'error': 'Not found. API endpoints are under /api/'},
        status=404
    )


def websocket_diagnostic(request):
    """
    Diagnostic endpoint to check WebSocket configuration
    """
    import django
    from django.conf import settings
    
    # Check if Django Channels is installed
    try:
        import channels
        channels_version = channels.__version__
        channels_installed = True
    except ImportError:
        channels_version = None
        channels_installed = False
    
    # Check if ASGI application is configured
    asgi_application = getattr(settings, 'ASGI_APPLICATION', None)
    
    # Check if channel layers are configured
    channel_layers = getattr(settings, 'CHANNEL_LAYERS', None)
    
    # Check if this is running under ASGI or WSGI
    server_type = "Unknown"
    if hasattr(settings, 'ASGI_APPLICATION'):
        server_type = "ASGI configured"
    else:
        server_type = "WSGI only"
    
    # Check Redis connection if configured
    redis_status = "Not configured"
    if channel_layers and 'default' in channel_layers:
        backend = channel_layers['default'].get('BACKEND', '')
        if 'redis' in backend.lower():
            redis_status = "Redis configured"
    
    return JsonResponse({
        'django_version': django.get_version(),
        'channels_installed': channels_installed,
        'channels_version': channels_version,
        'asgi_application': asgi_application,
        'server_type': server_type,
        'channel_layers_configured': channel_layers is not None,
        'redis_status': redis_status,
        'websocket_urls_available': [
            '/ws/messages/<tenant_schema>/',
            '/ws/typing/<tenant_schema>/<conversation_id>/'
        ] if channels_installed else [],
        'debug_mode': settings.DEBUG,
        'message': 'WebSocket functionality requires ASGI server and Django Channels'
    })

urlpatterns = [
    path('admin/', admin.site.urls),

    # Catch-all for /api without trailing slash (prevents APPEND_SLASH errors from bots)
    path('api', api_root_catchall, name='api_root_catchall'),

    # API Documentation - Main API
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),

    # API Documentation - Ecommerce Client API (separate schema for client endpoints only)
    path('api/ecommerce-client-schema/', SpectacularAPIView.as_view(
        urlconf='ecommerce_crm.urls_client_schema'
    ), name='ecommerce-client-schema'),

    # Swagger UI - Main API
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),

    # Swagger UI - Ecommerce Client API
    path('api/ecommerce-client-docs/', SpectacularSwaggerView.as_view(url_name='ecommerce-client-schema'), name='ecommerce-client-swagger-ui'),

    # WebSocket diagnostic endpoint
    path('websocket-diagnostic/', websocket_diagnostic, name='websocket_diagnostic'),

    # Sentry test endpoint - triggers a test error
    path('sentry-test/', sentry_test_error, name='sentry_test'),
    
    # Authentication and tenant management
    path('', include('tenants.urls')),
    
    # Tenant-specific apps
    path('', include('users.urls')),
    path('', include('crm.urls')),
    path('', include('tickets.urls')),
    path('api/social/', include('social_integrations.urls')),
    path('api/widget/', include('social_integrations.widget_urls')),
    path('', include('notifications.urls')),
    path('api/ecommerce/', include('ecommerce_crm.urls')),
    path('api/help/', include('help_center.urls')),
    path('api/blog/', include('blog.urls')),
    path('api/landing/', include('landing_pages.urls')),
    path('api/marketing/', include('marketing.urls')),
    path('api/bookings/', include('booking_management.urls')),
    path('api/leave/', include('leave_management.urls')),
    path('api/invoices/', include('invoices.urls')),
    path('api/onboarding/', include('onboarding.urls')),
]
