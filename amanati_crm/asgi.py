"""
ASGI config for amanati_crm project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""

import os
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application
from django.urls import path, re_path

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'amanati_crm.settings')

# Initialize Django ASGI application early to ensure the AppRegistry
# is populated before importing code that may import ORM models.
django_asgi_app = get_asgi_application()

# Import consumers and custom auth middleware AFTER Django initialization
from social_integrations import consumers
from users import consumers as users_consumers
from amanati_crm.websocket_auth import JWTAuthMiddlewareStack


async def close_unknown_websocket(scope, receive, send):
    """Catch-all for WebSocket paths no consumer owns.

    Without it ``URLRouter`` raises ``ValueError("No route found ...")``,
    Daphne logs "Exception inside application" at ERROR level and every
    scanner probing ``wss://api.../websocket`` lands in Sentry. Closing the
    handshake instead makes Daphne answer 403 and log nothing.
    """
    await receive()  # websocket.connect
    await send({"type": "websocket.close", "code": 4404})


application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": URLRouter([
        path('ws/messages/<str:tenant_schema>/', JWTAuthMiddlewareStack(consumers.MessagesConsumer.as_asgi())),
        path('ws/typing/<str:tenant_schema>/<str:conversation_id>/', JWTAuthMiddlewareStack(consumers.TypingConsumer.as_asgi())),
        path('ws/notifications/<str:tenant_schema>/', JWTAuthMiddlewareStack(users_consumers.NotificationConsumer.as_asgi())),
        path('ws/boards/<str:tenant_schema>/<str:board_id>/', JWTAuthMiddlewareStack(users_consumers.TicketBoardConsumer.as_asgi())),
        path('ws/team-chat/<str:tenant_schema>/', JWTAuthMiddlewareStack(users_consumers.TeamChatConsumer.as_asgi())),
        # Widget visitor WebSocket — anonymous (no JWT). Token + session_id
        # in the URL identify the tenant + session; the consumer resolves
        # the tenant schema internally via the widget token.
        re_path(
            r'^ws/widget/(?P<token>[\w\-]+)/(?P<session_id>\w+)/$',
            consumers.WidgetVisitorConsumer.as_asgi(),
        ),
        # Must stay last: any path not matched above is closed quietly.
        re_path(r'^.*$', close_unknown_websocket),
    ]),
})
