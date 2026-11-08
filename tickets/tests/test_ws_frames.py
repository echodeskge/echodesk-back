"""WebSocket frames on the board socket (``ws/boards/<schema>/<board_id>/``).

Runs the real routing + ``JWTAuthMiddlewareStack`` + ``TicketBoardConsumer``
over an in-memory channel layer, authenticating with a real per-tenant DRF
token.
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
class TestTicketActivityFrame(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='ws-admin@test.com')
        self.board, self.col, _, _ = self.setup_board_with_columns(admin=self.admin)
        self.ticket = self.create_ticket('WS', column=self.col, created_by=self.admin)
        self.token = Token.objects.create(user=self.admin).key
        self.schema = connection.schema_name

    def _run(self, scenario):
        from channels.testing import WebsocketCommunicator
        url = f'/ws/boards/{self.schema}/{self.board.id}/?token={self.token}'

        async def wrapper():
            comm = WebsocketCommunicator(_application(), url)
            connected, _ = await comm.connect()
            assert connected
            hello = await comm.receive_json_from(timeout=5)
            assert hello['type'] == 'connection', hello
            try:
                return await scenario(comm)
            finally:
                await comm.disconnect()

        return async_to_sync(wrapper)()

    def test_ticket_activity_is_forwarded_and_socket_stays_open(self):
        from users.consumers import broadcast_ticket_activity
        schema, board_id, ticket_id, actor_id = self.schema, self.board.id, self.ticket.id, self.admin.id

        async def scenario(comm):
            await broadcast_ticket_activity(schema, board_id, {
                'ticket_id': ticket_id, 'kind': 'timer', 'actor_id': actor_id,
                'client_mutation_id': None,
            })
            first = await comm.receive_json_from(timeout=5)
            await broadcast_ticket_activity(schema, board_id, {
                'ticket_id': ticket_id, 'kind': 'comment', 'actor_id': actor_id,
                'client_mutation_id': 'abc-123',
            })
            second = await comm.receive_json_from(timeout=5)
            return first, second

        first, second = self._run(scenario)
        self.assertEqual(first, {
            'type': 'ticket_activity', 'ticket_id': ticket_id, 'kind': 'timer',
            'actor_id': actor_id, 'client_mutation_id': None,
        })
        self.assertEqual(second['kind'], 'comment')
        self.assertEqual(second['client_mutation_id'], 'abc-123')

    def test_raw_group_message_missing_optional_keys(self):
        """A bare group message (e.g. from a future sender) still forwards
        every key, defaulting the optional ones to null."""
        group = f'board_{self.schema}_{self.board.id}'
        ticket_id = self.ticket.id

        async def scenario(comm):
            from channels.layers import get_channel_layer
            await get_channel_layer().group_send(group, {
                'type': 'ticket_activity', 'ticket_id': ticket_id, 'kind': 'checklist',
            })
            return await comm.receive_json_from(timeout=5)

        frame = self._run(scenario)
        self.assertEqual(frame, {
            'type': 'ticket_activity', 'ticket_id': ticket_id, 'kind': 'checklist',
            'actor_id': None, 'client_mutation_id': None,
        })

    def test_timer_start_reaches_the_board_socket(self):
        """End to end: the timer's on-commit broadcast reaches a viewer."""
        agent = self.create_user(email='ws-agent@test.com')
        self.ticket.assigned_to = agent
        self.ticket.save()
        ticket_id = self.ticket.id

        def do_start():
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.api_post(f'/api/tickets/{ticket_id}/timer/start/', {}, user=agent)
            assert resp.status_code == 201, resp.data

        async def scenario(comm):
            from asgiref.sync import sync_to_async
            await sync_to_async(do_start)()
            return await comm.receive_json_from(timeout=5)

        frame = self._run(scenario)
        self.assertEqual(frame, {
            'type': 'ticket_activity', 'ticket_id': ticket_id, 'kind': 'timer',
            'actor_id': agent.id, 'client_mutation_id': None,
        })
