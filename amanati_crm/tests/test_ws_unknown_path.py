"""Unknown WebSocket paths are closed quietly instead of raising.

Sentry ECHODESK-BACKEND-8S: a scanner opened ``wss://api.echodesk.ge/websocket``
and ``URLRouter`` raised ``ValueError("No route found for path 'websocket'")``,
which Daphne logs at ERROR level. The catch-all route in ``amanati_crm.asgi``
closes the handshake with 4404 and the real routes keep matching first.
"""
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase


def _connect(path):
    from channels.testing import WebsocketCommunicator
    from amanati_crm.asgi import application

    async def run():
        comm = WebsocketCommunicator(application, path)
        try:
            return await comm.connect(timeout=5)
        finally:
            await comm.disconnect()

    return async_to_sync(run)()


class TestUnknownWebsocketPath(SimpleTestCase):

    def test_unknown_path_is_closed_with_4404(self):
        for path in ('/websocket', '/', '/ws/', '/ws/nope/x/', '/socket.io/?EIO=4&transport=websocket'):
            with self.subTest(path=path):
                accepted, code = _connect(path)
                self.assertFalse(accepted)
                self.assertEqual(code, 4404)

    def test_known_routes_match_before_the_catch_all(self):
        from amanati_crm.asgi import application, close_unknown_websocket
        router = application.application_mapping['websocket']
        self.assertIs(router.routes[-1].callback, close_unknown_websocket)
        for path in ('ws/messages/acme/', 'ws/typing/acme/42/', 'ws/notifications/acme/',
                     'ws/boards/acme/7/', 'ws/team-chat/acme/', 'ws/widget/tok-1/abc123/'):
            with self.subTest(path=path):
                first = next(r for r in router.routes if r.pattern.match(path))
                self.assertIsNot(first.callback, close_unknown_websocket)
