"""WebSocket frames on the board socket (``ws/boards/<schema>/<board_id>/``).

Runs the real routing + ``JWTAuthMiddlewareStack`` + ``TicketBoardConsumer``
over an in-memory channel layer, authenticating with a real per-tenant DRF
token. BE-08 (F1): every handler forwards its new keys.
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


def _reset_channel_layers():
    from channels.layers import channel_layers
    channel_layers.backends = {}


class BoardSocketTestCase(TicketTestCase):

    def setUp(self):
        # Per test, not class level: EchoDeskTenantTestCase.setUpClass does
        # not call super(), so a class-level override_settings never applied
        # (this file used to run on Redis). Enabled before super().setUp() so
        # it nests with the base class's middleware override; the cached
        # layers are dropped on both sides.
        override = override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYERS)
        override.enable()
        _reset_channel_layers()
        self.addCleanup(_reset_channel_layers)
        self.addCleanup(override.disable)
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


class TestTicketActivityFrame(BoardSocketTestCase):

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


class TestBoardEventFrames(BoardSocketTestCase):
    """BE-08 forwarding: card / client_mutation_id / actor_id / reason /
    to_board_id / comment.is_internal reach the client frame."""

    def _api(self, fn):
        """Run an API call with on-commit callbacks executed (the broadcast
        is sent after commit) from inside the async scenario."""
        def call():
            with self.captureOnCommitCallbacks(execute=True):
                return fn()
        from asgiref.sync import sync_to_async
        return sync_to_async(call)()

    def test_ticket_updated_frame_from_a_real_patch(self):
        ticket_id, admin = self.ticket.id, self.admin

        async def scenario(comm):
            resp = await self._api(lambda: self.api_patch(
                f'/api/tickets/{ticket_id}/', {'title': 'Renamed'}, user=admin,
                HTTP_X_CLIENT_MUTATION='mut-ws-1'))
            assert resp.status_code == 200, resp.data
            return await comm.receive_json_from(timeout=5)

        frame = self._run(scenario)
        self.assertEqual(frame['type'], 'ticket_updated')
        self.assertEqual(frame['changes']['title'], {'old': 'WS', 'new': 'Renamed'})
        self.assertEqual(frame['card']['id'], self.ticket.id)
        self.assertEqual(frame['card']['title'], 'Renamed')
        self.assertEqual(frame['client_mutation_id'], 'mut-ws-1')
        self.assertEqual(frame['actor_id'], self.admin.id)
        self.assertEqual(frame['updated_by_id'], self.admin.id)

    def test_ticket_commented_frame_carries_is_internal(self):
        ticket_id, admin = self.ticket.id, self.admin

        async def scenario(comm):
            resp = await self._api(lambda: self.api_post(
                '/api/comments/', {'ticket': ticket_id, 'comment': 'hi', 'is_internal': False}, user=admin))
            assert resp.status_code == 201, resp.data
            first = await comm.receive_json_from(timeout=5)
            second = await comm.receive_json_from(timeout=5)
            return first, second

        commented, activity = self._run(scenario)
        self.assertEqual(commented['type'], 'ticket_commented')
        self.assertIs(commented['comment']['is_internal'], False)
        self.assertEqual(commented['comments_count'], 1)
        self.assertEqual(activity, {
            'type': 'ticket_activity', 'ticket_id': ticket_id, 'kind': 'comment',
            'actor_id': admin.id, 'client_mutation_id': None,
        })

    def test_moved_created_deleted_frames(self):
        from users.consumers import broadcast_ticket_created, broadcast_ticket_deleted, broadcast_ticket_moved
        schema, board_id, ticket_id = self.schema, self.board.id, self.ticket.id
        admin = self.admin
        card = {'id': ticket_id, 'title': 'WS'}

        async def scenario(comm):
            await broadcast_ticket_moved(
                schema, board_id, ticket_id, 1, 2, 0, admin,
                card=card, actor_id=admin.id, client_mutation_id='m-1')
            moved = await comm.receive_json_from(timeout=5)
            await broadcast_ticket_created(
                schema, board_id, card, admin, actor_id=admin.id, client_mutation_id='m-2')
            created = await comm.receive_json_from(timeout=5)
            await broadcast_ticket_deleted(
                schema, board_id, ticket_id, admin, reason='transferred', to_board_id=99,
                client_mutation_id='m-3')
            deleted = await comm.receive_json_from(timeout=5)
            return moved, created, deleted

        moved, created, deleted = self._run(scenario)
        self.assertEqual(moved, {
            'type': 'ticket_moved', 'ticket_id': ticket_id, 'from_column_id': 1, 'to_column_id': 2,
            'position': 0, 'updated_by_id': admin.id, 'updated_by_name': admin.get_full_name() or admin.email,
            'card': card, 'actor_id': admin.id, 'client_mutation_id': 'm-1',
        })
        self.assertEqual(created['ticket'], card)
        self.assertEqual(created['client_mutation_id'], 'm-2')
        self.assertEqual(created['actor_id'], admin.id)
        self.assertEqual(deleted['reason'], 'transferred')
        self.assertEqual(deleted['to_board_id'], 99)
        self.assertEqual(deleted['client_mutation_id'], 'm-3')
        self.assertEqual(deleted['deleted_by_id'], admin.id)

    def test_bare_group_messages_default_new_keys(self):
        """Group messages from an older sender still forward every key."""
        group = f'board_{self.schema}_{self.board.id}'
        ticket_id = self.ticket.id

        async def scenario(comm):
            from channels.layers import get_channel_layer
            layer = get_channel_layer()
            await layer.group_send(group, {'type': 'ticket_updated', 'ticket_id': ticket_id})
            updated = await comm.receive_json_from(timeout=5)
            await layer.group_send(group, {'type': 'ticket_deleted', 'ticket_id': ticket_id})
            deleted = await comm.receive_json_from(timeout=5)
            return updated, deleted

        updated, deleted = self._run(scenario)
        self.assertEqual(updated, {
            'type': 'ticket_updated', 'ticket_id': ticket_id, 'changes': {},
            'updated_by_id': None, 'updated_by_name': None,
            'card': None, 'actor_id': None, 'client_mutation_id': None,
        })
        self.assertEqual(deleted['reason'], 'deleted')
        self.assertIsNone(deleted['to_board_id'])
