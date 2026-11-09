"""
Tests for the board WebSocket broadcasts fired from tickets/signals.py.

Since BE-08 every board event goes through ``tickets.realtime.queue_event``:
it is sent after commit and coalesced per request. TestCase wraps each test
in a transaction, so every action that should broadcast runs inside
``captureOnCommitCallbacks(execute=True)``.

The users.consumers.broadcast_* coroutines are patched with AsyncMock (the
sender calls them through async_to_sync, which rejects a plain Mock) so we
assert on what is sent without a channel layer.
"""
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.db import transaction

from tickets.models import ChecklistItem, Ticket, TicketAttachment, TicketFormSubmission, TicketHistory
from tickets.querysets import lite_card_queryset
from tickets.realtime import build_card, card_payload, json_safe
from tickets.serializers import TicketCardSerializer
from tickets.tests.conftest import TicketTestCase

BROADCASTS = ('created', 'updated', 'moved', 'deleted', 'commented', 'activity')

# The ticket_moved keys as of migration 0040 (the mobile contract, C7).
MOVED_KEYS_0040 = {
    'type', 'ticket_id', 'from_column_id', 'to_column_id', 'position',
    'updated_by_id', 'updated_by_name',
}


class BroadcastTestCase(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bc-admin@test.com')
        self.board, self.col1, self.col2, _ = self.setup_board_with_columns(admin=self.admin)

    @contextmanager
    def broadcasts(self, execute=True):
        """Patch every board broadcast helper and run on-commit callbacks at
        the end of the block. Yields a namespace of the AsyncMocks."""
        patchers = [
            patch(f'users.consumers.broadcast_ticket_{name}', new_callable=AsyncMock)
            for name in BROADCASTS
        ]
        mocks = SimpleNamespace(**{name: p.start() for name, p in zip(BROADCASTS, patchers)})
        try:
            with self.captureOnCommitCallbacks(execute=execute) as callbacks:
                mocks.callbacks = callbacks
                yield mocks
        finally:
            for p in patchers:
                p.stop()

    @staticmethod
    def activity_kinds(mock):
        return [c.args[2]['kind'] for c in mock.await_args_list]

    def expected_card(self, ticket_id):
        return json_safe(TicketCardSerializer(lite_card_queryset().get(pk=ticket_id)).data)


class TestTicketCreatedBroadcast(BroadcastTestCase):

    def test_create_broadcasts_card(self):
        with self.broadcasts() as m:
            resp = self.api_post('/api/tickets/', {
                'title': 'Fresh', 'description': 'body', 'column_id': self.col1.id,
            }, user=self.admin)
        self.assertEqual(resp.status_code, 201)

        m.created.assert_awaited_once()
        kwargs = m.created.await_args.kwargs
        self.assertEqual(kwargs['board_id'], self.board.id)
        self.assertEqual(kwargs['created_by'], self.admin)
        card = kwargs['ticket_data']
        self.assertIs(type(card), dict)
        self.assertEqual(card['id'], resp.data['id'])
        self.assertEqual(card['title'], 'Fresh')
        self.assertEqual(card['column_id'], self.col1.id)
        self.assertEqual(card['comments_count'], 0)
        self.assertEqual(card['attachments_count'], 0)
        self.assertNotIn('description', card)
        self.assertEqual(card, self.expected_card(resp.data['id']))
        # Channel-layer safe: plain JSON types only
        json.dumps(card)
        # Creating also writes tags / assignments: no separate updates
        m.updated.assert_not_awaited()

    def test_create_card_includes_tags_and_assignees(self):
        # post_save fires before the serializer writes tags / assignments, so
        # the API path must broadcast the card only once those are in place
        tag = self.create_tag(name='urgent')
        assignee = self.create_user(email='bc-assignee@test.com')
        with self.broadcasts() as m:
            resp = self.api_post('/api/tickets/', {
                'title': 'Tagged', 'description': 'body', 'column_id': self.col1.id,
                'tag_ids': [tag.id], 'assigned_user_ids': [assignee.id],
            }, user=self.admin)
        self.assertEqual(resp.status_code, 201)

        m.created.assert_awaited_once()
        card = m.created.await_args.kwargs['ticket_data']
        self.assertEqual([t['id'] for t in card['tags']], [tag.id])
        self.assertEqual([u['id'] for u in card['assignees']], [assignee.id])
        m.updated.assert_not_awaited()

    def test_model_create_still_broadcasts(self):
        # Non-API creates (e.g. support bug reports) broadcast from post_save
        with self.broadcasts() as m:
            ticket = self.create_ticket(title='Plain', column=self.col1, created_by=self.admin)
        m.created.assert_awaited_once()
        self.assertEqual(m.created.await_args.kwargs['ticket_data']['id'], ticket.id)
        self.assertIsNone(m.created.await_args.kwargs['client_mutation_id'])

    def test_update_does_not_broadcast_created(self):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        with self.broadcasts() as m:
            ticket.title = 'T2'
            ticket.save()
        m.created.assert_not_awaited()

    def test_card_from_bare_instance_has_defaults(self):
        ticket = Ticket(id=999999, title='Bare', column=self.col1, created_by=self.admin)
        card = card_payload(ticket)
        self.assertEqual(card['comments_count'], 0)
        self.assertEqual(card['attachments_count'], 0)
        self.assertEqual(card['checklist_items_count'], 0)
        self.assertEqual(card['completed_checklist_items_count'], 0)
        self.assertEqual(card['description_preview'], '')
        self.assertEqual(card['tags'], [])
        # build_card falls back to the instance when the row does not exist
        self.assertEqual(build_card(999999, fallback=ticket)['title'], 'Bare')
        self.assertIsNone(build_card(999999))


class TestTicketDeletedBroadcast(BroadcastTestCase):

    def test_delete_broadcasts_with_deleting_user(self):
        ticket = self.create_ticket(title='Gone', column=self.col1, created_by=self.admin)
        with self.broadcasts() as m:
            resp = self.api_delete(f'/api/tickets/{ticket.id}/', user=self.admin)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Ticket.objects.filter(pk=ticket.pk).exists())

        m.deleted.assert_awaited_once()
        kwargs = m.deleted.await_args.kwargs
        self.assertEqual(kwargs['board_id'], self.board.id)
        self.assertEqual(kwargs['ticket_id'], ticket.id)
        self.assertEqual(kwargs['deleted_by'], self.admin)
        self.assertEqual(kwargs['reason'], 'deleted')
        self.assertIsNone(kwargs['to_board_id'])

    def test_model_delete_broadcasts_without_user(self):
        ticket = self.create_ticket(title='Gone', column=self.col1, created_by=self.admin)
        ticket_id = ticket.id
        with self.broadcasts() as m:
            ticket.delete()
        m.deleted.assert_awaited_once()
        kwargs = m.deleted.await_args.kwargs
        self.assertEqual(kwargs['ticket_id'], ticket_id)
        self.assertIsNone(kwargs['deleted_by'])

    def test_queryset_delete_broadcasts_each_ticket(self):
        t1 = self.create_ticket(title='A', column=self.col1, created_by=self.admin)
        t2 = self.create_ticket(title='B', column=self.col1, created_by=self.admin)
        with self.broadcasts() as m:
            Ticket.objects.filter(pk__in=[t1.pk, t2.pk]).delete()
        self.assertEqual(m.deleted.await_count, 2)
        self.assertEqual(
            {c.kwargs['ticket_id'] for c in m.deleted.await_args_list}, {t1.id, t2.id},
        )

    def test_user_cascade_does_not_broadcast_per_ticket(self):
        # Ticket.created_by cascades: deleting a user removes their tickets in
        # bulk and must not fan out one group_send per ticket
        author = self.create_user(email='bc-author@test.com')
        t1 = self.create_ticket(title='A', column=self.col1, created_by=author)
        t2 = self.create_ticket(title='B', column=self.col2, created_by=author)
        with self.broadcasts() as m:
            author.delete()
        self.assertFalse(Ticket.objects.filter(pk__in=[t1.pk, t2.pk]).exists())
        m.deleted.assert_not_awaited()
        m.activity.assert_not_awaited()
        m.updated.assert_not_awaited()

    def test_receiver_honours_delete_origin(self):
        from tickets.signals import broadcast_on_ticket_delete
        ticket = self.create_ticket(title='A', column=self.col1, created_by=self.admin)

        with self.broadcasts() as m:
            broadcast_on_ticket_delete(Ticket, instance=ticket, origin=self.col1)
        m.deleted.assert_not_awaited()

        with self.broadcasts() as m:
            broadcast_on_ticket_delete(Ticket, instance=ticket, origin=ticket)
        with self.broadcasts() as m2:
            broadcast_on_ticket_delete(Ticket, instance=ticket, origin=Ticket.objects.all())
        with self.broadcasts() as m3:
            broadcast_on_ticket_delete(Ticket, instance=ticket)  # no origin: pre-4.1 style call
        self.assertEqual(m.deleted.await_count + m2.deleted.await_count + m3.deleted.await_count, 3)


class TestTicketCommentedConsumerHandler(BroadcastTestCase):
    """The consumer-side handler that relays a ticket_commented group event."""

    def _consumer(self, channel_name='chan-1'):
        from users.consumers import TicketBoardConsumer
        consumer = TicketBoardConsumer()
        consumer.channel_name = channel_name
        consumer.send = AsyncMock()
        return consumer

    def test_handler_sends_comment_payload(self):
        from asgiref.sync import async_to_sync
        consumer = self._consumer()
        event = {
            'type': 'ticket_commented', 'ticket_id': 7,
            'comment': {'id': 3, 'comment': 'hi', 'is_internal': True}, 'comments_count': 4,
            'user_id': self.admin.id, 'user_name': 'Admin',
            'exclude_channel': 'someone-else',
        }
        async_to_sync(consumer.ticket_commented)(event)
        consumer.send.assert_awaited_once()
        sent = json.loads(consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(sent, {
            'type': 'ticket_commented', 'ticket_id': 7,
            'comment': {'id': 3, 'comment': 'hi', 'is_internal': True}, 'comments_count': 4,
            'user_id': self.admin.id, 'user_name': 'Admin',
        })

    def test_handler_skips_excluded_channel(self):
        from asgiref.sync import async_to_sync
        consumer = self._consumer(channel_name='me')
        async_to_sync(consumer.ticket_commented)({
            'type': 'ticket_commented', 'ticket_id': 7, 'exclude_channel': 'me',
        })
        consumer.send.assert_not_awaited()

    def test_moved_frame_keeps_0040_keys(self):
        from asgiref.sync import async_to_sync
        consumer = self._consumer()
        async_to_sync(consumer.ticket_moved)({
            'type': 'ticket_moved', 'ticket_id': 1, 'from_column_id': 2, 'to_column_id': 3,
            'position': 0, 'updated_by_id': 4, 'updated_by_name': 'A',
        })
        sent = json.loads(consumer.send.await_args.kwargs['text_data'])
        self.assertTrue(MOVED_KEYS_0040 <= set(sent))
        self.assertEqual(set(sent) - MOVED_KEYS_0040, {'card', 'actor_id', 'client_mutation_id'})


class TestTicketCommentedBroadcast(BroadcastTestCase):

    def test_comment_broadcasts_comment_and_count(self):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        self.create_comment(ticket, self.admin, 'first')

        with self.broadcasts() as m:
            resp = self.api_post('/api/comments/', {
                'ticket': ticket.id, 'comment': 'second', 'is_internal': False,
            }, user=self.admin)
        self.assertEqual(resp.status_code, 201)

        m.commented.assert_awaited_once()
        kwargs = m.commented.await_args.kwargs
        self.assertEqual(kwargs['board_id'], self.board.id)
        self.assertEqual(kwargs['ticket_id'], ticket.id)
        self.assertEqual(kwargs['comments_count'], 2)
        self.assertEqual(kwargs['user'], self.admin)
        comment = kwargs['comment']
        self.assertEqual(comment['id'], resp.data['id'])
        self.assertEqual(comment['comment'], 'second')
        self.assertIs(comment['is_internal'], False)
        self.assertEqual(comment['user']['id'], self.admin.id)
        self.assertEqual(comment['user']['email'], 'bc-admin@test.com')
        self.assertIsInstance(comment['created_at'], str)
        json.dumps(comment)
        self.assertEqual(self.activity_kinds(m.activity), ['comment'])
        self.assertEqual(m.activity.await_args.args[2]['actor_id'], self.admin.id)

    def test_comment_payload_defaults_internal(self):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        with self.broadcasts() as m:
            self.api_post('/api/comments/', {'ticket': ticket.id, 'comment': 'x'}, user=self.admin)
        self.assertIs(m.commented.await_args.kwargs['comment']['is_internal'], True)

    def test_add_comment_action_broadcasts(self):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        with self.broadcasts() as m:
            resp = self.api_post(f'/api/tickets/{ticket.id}/add_comment/', {
                'ticket': ticket.id, 'comment': 'via action',
            }, user=self.admin)
        self.assertEqual(resp.status_code, 201)
        m.commented.assert_awaited_once()
        self.assertEqual(m.commented.await_args.kwargs['comments_count'], 1)

    def test_edit_and_delete_send_activity(self):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        comment = self.create_comment(ticket, self.admin, 'first')
        with self.broadcasts() as m:
            resp = self.api_patch(f'/api/comments/{comment.id}/', {'comment': 'edited'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.activity_kinds(m.activity), ['comment_edited'])
        m.commented.assert_not_awaited()

        with self.broadcasts() as m:
            resp = self.api_delete(f'/api/comments/{comment.id}/', user=self.admin)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(self.activity_kinds(m.activity), ['comment_deleted'])


class TestUpdatedByAttribution(BroadcastTestCase):

    def test_move_broadcast_carries_user_position_and_card(self):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        self.create_ticket(title='Other', column=self.col2, created_by=self.admin)
        with self.broadcasts() as m:
            resp = self.api_patch(f'/api/tickets/{ticket.id}/move_to_column/', {
                'column_id': self.col2.id, 'position_in_column': 0,
            }, user=self.admin)
        self.assertEqual(resp.status_code, 200)

        m.moved.assert_awaited_once()
        kwargs = m.moved.await_args.kwargs
        self.assertEqual(kwargs['updated_by'], self.admin)
        self.assertEqual(kwargs['from_column_id'], self.col1.id)
        self.assertEqual(kwargs['to_column_id'], self.col2.id)
        self.assertEqual(kwargs['position'], 0)
        self.assertEqual(kwargs['actor_id'], self.admin.id)
        self.assertEqual(kwargs['card'], self.expected_card(ticket.id))
        self.assertEqual(kwargs['card']['column_id'], self.col2.id)
        m.deleted.assert_not_awaited()
        m.created.assert_not_awaited()

    def test_patch_broadcast_carries_user_and_records_history_user(self):
        ticket = self.create_ticket(title='Old', column=self.col1, created_by=self.admin)
        with self.broadcasts() as m:
            resp = self.api_patch(f'/api/tickets/{ticket.id}/', {'title': 'New'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)

        m.updated.assert_awaited_once()
        kwargs = m.updated.await_args.kwargs
        self.assertEqual(kwargs['updated_by'], self.admin)
        self.assertEqual(kwargs['changes']['title'], {'old': 'Old', 'new': 'New'})

        entry = TicketHistory.objects.get(ticket=ticket, field_name='title')
        self.assertEqual(entry.user_id, self.admin.id)


class TestCoalescing(BroadcastTestCase):

    def test_patch_title_tags_assignees_is_one_update_with_card(self):
        old_tag = self.create_tag(name='Old', color='#000000')
        new_tag = self.create_tag(name='VIP', color='#ff0000')
        alice = self.create_user(email='bc-alice@test.com', first_name='Alice', last_name='A')
        ticket = self.create_ticket(title='Before', column=self.col1, created_by=self.admin)
        ticket.tags.add(old_tag)

        with self.broadcasts() as m:
            resp = self.api_patch(f'/api/tickets/{ticket.id}/', {
                'title': 'After', 'tag_ids': [new_tag.id], 'assigned_user_ids': [alice.id],
            }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)

        m.updated.assert_awaited_once()
        kwargs = m.updated.await_args.kwargs
        changes = kwargs['changes']
        self.assertEqual(changes['title'], {'old': 'Before', 'new': 'After'})
        self.assertEqual(changes['tags'], {
            'old': [{'id': old_tag.id, 'name': 'Old', 'color': '#000000'}],
            'new': [{'id': new_tag.id, 'name': 'VIP', 'color': '#ff0000'}],
        })
        self.assertEqual(changes['assignees'], {
            'old': [],
            'new': [{'id': alice.id, 'name': 'Alice A', 'email': 'bc-alice@test.com'}],
        })
        self.assertNotIn('assigned_users', changes)
        self.assertEqual(kwargs['card'], self.expected_card(ticket.id))
        self.assertEqual(kwargs['actor_id'], self.admin.id)
        json.dumps(kwargs['changes'])

    def test_bulk_assign_three_users_is_one_update(self):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        users = [self.create_user(email=f'bc-bulk{i}@test.com') for i in range(3)]
        with self.broadcasts() as m:
            resp = self.api_post(f'/api/tickets/{ticket.id}/assignments/bulk_assign/', {
                'user_ids': [u.id for u in users],
            }, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        m.updated.assert_awaited_once()
        change = m.updated.await_args.kwargs['changes']['assignees']
        self.assertEqual(change['old'], [])
        self.assertEqual({u['id'] for u in change['new']}, {u.id for u in users})

    def test_bulk_unassign_two_users_is_one_update(self):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        users = [self.create_user(email=f'bc-unbulk{i}@test.com') for i in range(3)]
        for u in users:
            self.create_assignment(ticket, u, assigned_by=self.admin)
        with self.broadcasts() as m:
            resp = self.client_delete_with_body(
                f'/api/tickets/{ticket.id}/assignments/bulk_unassign/',
                {'user_ids': [users[0].id, users[1].id]},
            )
        self.assertEqual(resp.status_code, 204)
        m.updated.assert_awaited_once()
        change = m.updated.await_args.kwargs['changes']['assignees']
        self.assertEqual({u['id'] for u in change['old']}, {u.id for u in users})
        self.assertEqual([u['id'] for u in change['new']], [users[2].id])

    def client_delete_with_body(self, url, data):
        client = self.authenticated_client(self.admin)
        return client.delete(url, data, format='json', HTTP_HOST='tenant.test.com')

    def test_rolled_back_transaction_sends_nothing(self):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        with self.broadcasts() as m:
            try:
                with transaction.atomic():
                    ticket.title = 'Rolled back'
                    ticket.save()
                    ChecklistItem.objects.create(ticket=ticket, text='x', created_by=self.admin)
                    raise RuntimeError('boom')
            except RuntimeError:
                pass
        for name in BROADCASTS:
            getattr(m, name).assert_not_awaited()

    def test_no_request_path_sends_on_commit(self):
        """Celery / shell / admin code (no request, no batch) sends each event
        once the transaction commits, not before."""
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        with self.broadcasts(execute=False) as m:
            ticket.title = 'From a task'
            ticket.save()
            m.updated.assert_not_awaited()
        self.assertEqual(len(m.callbacks), 1)
        with self.broadcasts() as m2:
            m.callbacks[0]()
        m2.updated.assert_awaited_once()
        sent = m2.updated.await_args.kwargs
        self.assertEqual(sent['changes']['title']['new'], 'From a task')
        self.assertIsNone(sent['client_mutation_id'])
        self.assertIsNone(sent['updated_by'])


class TestClientMutationEcho(BroadcastTestCase):

    def test_header_echoed_on_updated_moved_created(self):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        with self.broadcasts() as m:
            self.api_patch(f'/api/tickets/{ticket.id}/', {'title': 'T2'}, user=self.admin,
                           HTTP_X_CLIENT_MUTATION='mut-updated-1')
            self.api_patch(f'/api/tickets/{ticket.id}/move_to_column/', {'column_id': self.col2.id},
                           user=self.admin, HTTP_X_CLIENT_MUTATION='mut-moved-2')
            self.api_post('/api/tickets/', {'title': 'N', 'description': 'b', 'column_id': self.col1.id},
                          user=self.admin, HTTP_X_CLIENT_MUTATION='mut-created-3')
        self.assertEqual(m.updated.await_args.kwargs['client_mutation_id'], 'mut-updated-1')
        self.assertEqual(m.moved.await_args.kwargs['client_mutation_id'], 'mut-moved-2')
        self.assertEqual(m.created.await_args.kwargs['client_mutation_id'], 'mut-created-3')

    def test_null_when_header_absent_or_invalid(self):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        with self.broadcasts() as m:
            self.api_patch(f'/api/tickets/{ticket.id}/', {'title': 'T2'}, user=self.admin)
        self.assertIsNone(m.updated.await_args.kwargs['client_mutation_id'])
        with self.broadcasts() as m:
            self.api_patch(f'/api/tickets/{ticket.id}/', {'title': 'T3'}, user=self.admin,
                           HTTP_X_CLIENT_MUTATION='bad value <script>')
        self.assertIsNone(m.updated.await_args.kwargs['client_mutation_id'])

    def test_header_echoed_on_ticket_activity(self):
        """Leftover (a): ticket_activity carries X-Client-Mutation (here via
        a comment and via the timer)."""
        agent = self.create_user(email='bc-timer@test.com')
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin, assigned_to=agent)
        with self.broadcasts() as m:
            self.api_post('/api/comments/', {'ticket': ticket.id, 'comment': 'c'}, user=self.admin,
                          HTTP_X_CLIENT_MUTATION='mut-comment')
            resp = self.api_post(f'/api/tickets/{ticket.id}/timer/start/', {}, user=agent,
                                 HTTP_X_CLIENT_MUTATION='mut-timer')
        self.assertEqual(resp.status_code, 201, resp.data)
        payloads = {c.args[2]['kind']: c.args[2] for c in m.activity.await_args_list}
        self.assertEqual(payloads['comment']['client_mutation_id'], 'mut-comment')
        self.assertEqual(payloads['timer']['client_mutation_id'], 'mut-timer')
        self.assertEqual(payloads['timer']['actor_id'], agent.id)


class TestTransferBroadcasts(BroadcastTestCase):

    def setUp(self):
        super().setUp()
        self.other_board = self.create_board(name='Board B', created_by=self.admin)
        self.other_col = self.create_column(self.other_board, name='Inbox')
        self.ticket = self.create_ticket(title='Travelling', column=self.col1, created_by=self.admin)

    def _assert_transfer(self, m):
        m.deleted.assert_awaited_once()
        deleted = m.deleted.await_args.kwargs
        self.assertEqual(deleted['board_id'], self.board.id)
        self.assertEqual(deleted['ticket_id'], self.ticket.id)
        self.assertEqual(deleted['reason'], 'transferred')
        self.assertEqual(deleted['to_board_id'], self.other_board.id)
        self.assertEqual(deleted['deleted_by'], self.admin)

        m.created.assert_awaited_once()
        created = m.created.await_args.kwargs
        self.assertEqual(created['board_id'], self.other_board.id)
        self.assertEqual(created['created_by'], self.admin)
        self.assertEqual(created['ticket_data'], self.expected_card(self.ticket.id))
        self.assertEqual(created['ticket_data']['column_id'], self.other_col.id)

        m.moved.assert_not_awaited()

    def test_move_to_column_transfer(self):
        with self.broadcasts() as m:
            resp = self.api_patch(f'/api/tickets/{self.ticket.id}/move_to_column/', {
                'column_id': self.other_col.id,
            }, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self._assert_transfer(m)

    def test_patch_column_id_transfer(self):
        with self.broadcasts() as m:
            resp = self.api_patch(f'/api/tickets/{self.ticket.id}/', {
                'column_id': self.other_col.id, 'title': 'Moved and renamed',
            }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self._assert_transfer(m)
        self.assertEqual(m.created.await_args.kwargs['ticket_data']['title'], 'Moved and renamed')
        # The new board learns everything from the created card
        m.updated.assert_not_awaited()


class TestTicketActivityKinds(BroadcastTestCase):

    def setUp(self):
        super().setUp()
        self.ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)

    def test_attachment_add_and_remove(self):
        with self.broadcasts() as m:
            att = TicketAttachment.objects.create(
                ticket=self.ticket, file='ticket_attachments/demo/a.txt', filename='a.txt',
                file_size=1, content_type='text/plain', uploaded_by=self.admin,
            )
        self.assertEqual(self.activity_kinds(m.activity), ['attachment'])
        self.assertEqual(m.activity.await_args.args[2]['actor_id'], self.admin.id)
        with self.broadcasts() as m:
            att.delete()
        self.assertEqual(self.activity_kinds(m.activity), ['attachment'])

    def test_checklist_create_toggle_delete(self):
        with self.broadcasts() as m:
            item = ChecklistItem.objects.create(ticket=self.ticket, text='Step', created_by=self.admin)
        self.assertEqual(self.activity_kinds(m.activity), ['checklist'])
        with self.broadcasts() as m:
            resp = self.api_patch(f'/api/checklist-items/{item.id}/toggle_check/', {}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.activity_kinds(m.activity), ['checklist'])
        with self.broadcasts() as m:
            item.delete()
        self.assertEqual(self.activity_kinds(m.activity), ['checklist'])

    def test_form_submission(self):
        form = self.create_ticket_form(title='Intake', created_by=self.admin)
        with self.broadcasts() as m:
            TicketFormSubmission.objects.create(ticket=self.ticket, form=form, submitted_by=self.admin)
        self.assertEqual(self.activity_kinds(m.activity), ['form'])

    def test_ticket_without_board_sends_nothing(self):
        loose = self.create_ticket(title='No column', column=self.col1, created_by=self.admin)
        Ticket.objects.filter(pk=loose.pk).update(column=None)  # save() would pick a default column
        with self.broadcasts() as m:
            ChecklistItem.objects.create(ticket_id=loose.pk, text='x', created_by=self.admin)
        m.activity.assert_not_awaited()


class TestAutocommitCoalescing(TicketTestCase):
    """The same coalescing with real autocommit (no test transaction), the
    way production runs requests (ATOMIC_REQUESTS is unset, R2).

    TestCase is switched to TransactionTestCase behaviour for this class, but
    without its table flush (that would wipe the shared tenant schema): the
    rows this class creates are deleted explicitly in a cleanup.
    """

    @classmethod
    def _databases_support_transactions(cls):
        return False

    def _fixture_teardown(self):
        pass  # no flush; see _cleanup

    def _should_reload_connections(self):
        return False

    def setUp(self):
        super().setUp()
        from django.db import connection
        self.assertFalse(connection.in_atomic_block, 'expected real autocommit')
        self._users, self._boards, self._tags = [], [], []
        self.addCleanup(self._cleanup)
        self.admin = self._user('ac-admin@test.com', is_staff=True, role='admin')
        self.board = self.create_board(name='Autocommit board', created_by=self.admin)
        self._boards.append(self.board)
        self.col = self.create_column(self.board, name='Todo')
        self.tag = self.create_tag(name='ac-tag')
        self._tags.append(self.tag)
        self.ticket = self.create_ticket(title='Before', column=self.col, created_by=self.admin)

    def _user(self, email, **kwargs):
        user = self.create_user(email=email, **kwargs)
        self._users.append(user)
        return user

    def _cleanup(self):
        Ticket.objects.filter(column__board__in=self._boards).delete()
        Ticket.objects.filter(created_by__in=self._users).delete()
        for board in self._boards:
            board.delete()
        for tag in self._tags:
            tag.delete()
        for user in self._users:
            user.delete()

    @contextmanager
    def broadcasts(self):
        patchers = [
            patch(f'users.consumers.broadcast_ticket_{name}', new_callable=AsyncMock)
            for name in BROADCASTS
        ]
        mocks = SimpleNamespace(**{name: p.start() for name, p in zip(BROADCASTS, patchers)})
        try:
            yield mocks
        finally:
            for p in patchers:
                p.stop()

    def test_patch_is_one_update_in_autocommit(self):
        alice = self._user('ac-alice@test.com')
        with self.broadcasts() as m:
            resp = self.api_patch(f'/api/tickets/{self.ticket.id}/', {
                'title': 'After', 'tag_ids': [self.tag.id], 'assigned_user_ids': [alice.id],
            }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        m.updated.assert_awaited_once()
        changes = m.updated.await_args.kwargs['changes']
        self.assertEqual(set(changes), {'title', 'tags', 'assignees'})
        self.assertEqual(m.updated.await_args.kwargs['card']['title'], 'After')

    def test_bulk_assign_is_one_update_in_autocommit(self):
        users = [self._user(f'ac-bulk{i}@test.com') for i in range(3)]
        with self.broadcasts() as m:
            resp = self.api_post(f'/api/tickets/{self.ticket.id}/assignments/bulk_assign/', {
                'user_ids': [u.id for u in users],
            }, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        m.updated.assert_awaited_once()

    def test_model_save_outside_request_sends_immediately(self):
        with self.broadcasts() as m:
            self.ticket.title = 'From a task'
            self.ticket.save()
            m.updated.assert_awaited_once()
