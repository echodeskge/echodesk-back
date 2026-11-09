"""BE-06 / E7: board WebSocket access check.

Runs the real ``JWTAuthMiddlewareStack`` + ``TicketBoardConsumer`` over an
in-memory channel layer with real per-tenant DRF tokens. A denied connection
is accepted, gets ``{type:'error', code:'FORBIDDEN'}`` and is closed with
4403 (accept first, so browsers see 4403 and not 1006).
"""
from asgiref.sync import async_to_sync
from django.db import connection
from django.test import override_settings
from rest_framework.authtoken.models import Token

from tickets.tests.conftest import TicketTestCase

IN_MEMORY_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}


def _application():
    from channels.routing import URLRouter
    from django.urls import re_path
    from amanati_crm.websocket_auth import JWTAuthMiddlewareStack
    from users.consumers import TicketBoardConsumer
    return URLRouter([
        re_path(r'ws/boards/(?P<tenant_schema>\w+)/(?P<board_id>\w+)/$',
                JWTAuthMiddlewareStack(TicketBoardConsumer.as_asgi())),
    ])


@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYERS)
class TestBoardSocketAccess(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.schema = connection.schema_name
        self.admin = self.create_admin(email='wsa-admin@test.com')
        self.agent = self.create_user(email='wsa-agent@test.com')
        self.member = self.create_user(email='wsa-member@test.com')
        self.restricted = self.create_board('Restricted', created_by=self.admin, users=[self.member])
        self.open_board = self.create_board('Open', created_by=self.admin, open=True)
        self.tokens = {
            user.id: Token.objects.create(user=user).key
            for user in (self.admin, self.agent, self.member)
        }

    def _connect(self, user, board_id):
        """Connect and return (accepted, first_frame, close_code_or_None)."""
        from channels.testing import WebsocketCommunicator
        url = f'/ws/boards/{self.schema}/{board_id}/?token={self.tokens[user.id]}'

        async def run():
            comm = WebsocketCommunicator(_application(), url)
            accepted, _ = await comm.connect()
            first = await comm.receive_json_from(timeout=5) if accepted else None
            close_code = None
            if first and first.get('type') == 'error':
                closing = await comm.receive_output(timeout=5)
                self.assertEqual(closing['type'], 'websocket.close')
                close_code = closing.get('code')
            else:
                await comm.disconnect()
            return accepted, first, close_code

        return async_to_sync(run)()

    def test_inaccessible_board_is_accepted_then_closed_4403(self):
        accepted, frame, code = self._connect(self.agent, self.restricted.id)
        self.assertIs(accepted, True)
        self.assertEqual(frame['type'], 'error')
        self.assertEqual(frame['code'], 'FORBIDDEN')
        self.assertEqual(code, 4403)

    def test_non_numeric_board_id_is_4403(self):
        accepted, frame, code = self._connect(self.agent, 'default')
        self.assertIs(accepted, True)
        self.assertEqual(frame['code'], 'FORBIDDEN')
        self.assertEqual(code, 4403)

    def test_missing_board_is_4403(self):
        self.assertEqual(self._connect(self.admin, 999999)[2], 4403)

    def test_member_connects_to_restricted_board(self):
        accepted, frame, code = self._connect(self.member, self.restricted.id)
        self.assertIs(accepted, True)
        self.assertEqual(frame['type'], 'connection')
        self.assertEqual(frame['board_id'], str(self.restricted.id))
        self.assertIsNone(code)

    def test_agent_connects_to_open_board(self):
        accepted, frame, code = self._connect(self.agent, self.open_board.id)
        self.assertIs(accepted, True)
        self.assertEqual(frame['type'], 'connection')
        self.assertIsNone(code)

    def test_staff_connects_to_any_board(self):
        accepted, frame, _ = self._connect(self.admin, self.restricted.id)
        self.assertEqual(frame['type'], 'connection')

    def _connect_from_public(self, user, board_id):
        """Connect while the connection points at the public schema (no
        tickets tables there): the check must still run in the tenant schema
        from the URL. (A full second tenant schema would need a ~20 s
        migration; this exercises the same schema_context code path.)"""
        connection.set_schema_to_public()
        try:
            return self._connect(user, board_id)
        finally:
            connection.set_tenant(self.tenant)

    def test_access_check_runs_in_url_schema_allows_member(self):
        self.assertEqual(self._connect_from_public(self.member, self.restricted.id)[1]['type'], 'connection')

    def test_access_check_runs_in_url_schema_denies_agent(self):
        self.assertEqual(self._connect_from_public(self.agent, self.restricted.id)[2], 4403)
