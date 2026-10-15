"""
Tests for the board WebSocket broadcasts fired from tickets/signals.py.

The users.consumers.broadcast_* coroutines are patched with AsyncMock
(signals call them through async_to_sync, which rejects a plain Mock) so
we assert on what the signals send without a channel layer.
"""
import json
from unittest.mock import AsyncMock, patch

from tickets.models import Ticket, TicketHistory
from tickets.tests.conftest import TicketTestCase


class BroadcastTestCase(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bc-admin@test.com')
        self.board, self.col1, self.col2, _ = self.setup_board_with_columns(admin=self.admin)


class TestTicketCreatedBroadcast(BroadcastTestCase):

    @patch('users.consumers.broadcast_ticket_created', new_callable=AsyncMock)
    def test_create_broadcasts_card(self, mock_broadcast):
        resp = self.api_post('/api/tickets/', {
            'title': 'Fresh', 'description': 'body', 'column_id': self.col1.id,
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201)

        mock_broadcast.assert_awaited_once()
        kwargs = mock_broadcast.await_args.kwargs
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
        # Channel-layer safe: plain JSON types only
        json.dumps(card)

    @patch('users.consumers.broadcast_ticket_created', new_callable=AsyncMock)
    def test_create_card_includes_tags_and_assignees(self, mock_broadcast):
        # post_save fires before the serializer writes tags / assignments, so
        # the API path must broadcast the card only once those are in place
        tag = self.create_tag(name='urgent')
        assignee = self.create_user(email='bc-assignee@test.com')
        resp = self.api_post('/api/tickets/', {
            'title': 'Tagged', 'description': 'body', 'column_id': self.col1.id,
            'tag_ids': [tag.id], 'assigned_user_ids': [assignee.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201)

        mock_broadcast.assert_awaited_once()
        card = mock_broadcast.await_args.kwargs['ticket_data']
        self.assertEqual([t['id'] for t in card['tags']], [tag.id])
        self.assertEqual([u['id'] for u in card['assignees']], [assignee.id])

    @patch('users.consumers.broadcast_ticket_created', new_callable=AsyncMock)
    def test_model_create_still_broadcasts(self, mock_broadcast):
        # Non-API creates (e.g. support bug reports) broadcast from post_save
        ticket = self.create_ticket(title='Plain', column=self.col1, created_by=self.admin)
        mock_broadcast.assert_awaited_once()
        self.assertEqual(mock_broadcast.await_args.kwargs['ticket_data']['id'], ticket.id)

    @patch('users.consumers.broadcast_ticket_created', new_callable=AsyncMock)
    def test_update_does_not_broadcast_created(self, mock_broadcast):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        mock_broadcast.reset_mock()
        ticket.title = 'T2'
        ticket.save()
        mock_broadcast.assert_not_awaited()


class TestTicketDeletedBroadcast(BroadcastTestCase):

    @patch('users.consumers.broadcast_ticket_deleted', new_callable=AsyncMock)
    def test_delete_broadcasts_with_deleting_user(self, mock_broadcast):
        ticket = self.create_ticket(title='Gone', column=self.col1, created_by=self.admin)
        resp = self.api_delete(f'/api/tickets/{ticket.id}/', user=self.admin)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Ticket.objects.filter(pk=ticket.pk).exists())

        mock_broadcast.assert_awaited_once()
        kwargs = mock_broadcast.await_args.kwargs
        self.assertEqual(kwargs['board_id'], self.board.id)
        self.assertEqual(kwargs['ticket_id'], ticket.id)
        self.assertEqual(kwargs['deleted_by'], self.admin)

    @patch('users.consumers.broadcast_ticket_deleted', new_callable=AsyncMock)
    def test_model_delete_broadcasts_without_user(self, mock_broadcast):
        ticket = self.create_ticket(title='Gone', column=self.col1, created_by=self.admin)
        ticket_id = ticket.id
        ticket.delete()
        mock_broadcast.assert_awaited_once()
        kwargs = mock_broadcast.await_args.kwargs
        self.assertEqual(kwargs['ticket_id'], ticket_id)
        self.assertIsNone(kwargs['deleted_by'])

    @patch('users.consumers.broadcast_ticket_deleted', new_callable=AsyncMock)
    def test_queryset_delete_broadcasts_each_ticket(self, mock_broadcast):
        t1 = self.create_ticket(title='A', column=self.col1, created_by=self.admin)
        t2 = self.create_ticket(title='B', column=self.col1, created_by=self.admin)
        Ticket.objects.filter(pk__in=[t1.pk, t2.pk]).delete()
        self.assertEqual(mock_broadcast.await_count, 2)
        self.assertEqual(
            {c.kwargs['ticket_id'] for c in mock_broadcast.await_args_list}, {t1.id, t2.id},
        )

    @patch('users.consumers.broadcast_ticket_deleted', new_callable=AsyncMock)
    def test_user_cascade_does_not_broadcast_per_ticket(self, mock_broadcast):
        # Ticket.created_by cascades: deleting a user removes their tickets in
        # bulk and must not fan out one group_send per ticket
        author = self.create_user(email='bc-author@test.com')
        t1 = self.create_ticket(title='A', column=self.col1, created_by=author)
        t2 = self.create_ticket(title='B', column=self.col2, created_by=author)
        author.delete()
        self.assertFalse(Ticket.objects.filter(pk__in=[t1.pk, t2.pk]).exists())
        mock_broadcast.assert_not_awaited()

    @patch('users.consumers.broadcast_ticket_deleted', new_callable=AsyncMock)
    def test_receiver_honours_delete_origin(self, mock_broadcast):
        from tickets.signals import broadcast_on_ticket_delete
        ticket = self.create_ticket(title='A', column=self.col1, created_by=self.admin)

        broadcast_on_ticket_delete(Ticket, instance=ticket, origin=self.col1)
        mock_broadcast.assert_not_awaited()

        broadcast_on_ticket_delete(Ticket, instance=ticket, origin=ticket)
        broadcast_on_ticket_delete(Ticket, instance=ticket, origin=Ticket.objects.all())
        broadcast_on_ticket_delete(Ticket, instance=ticket)  # no origin: pre-4.1 style call
        self.assertEqual(mock_broadcast.await_count, 3)


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
            'comment': {'id': 3, 'comment': 'hi'}, 'comments_count': 4,
            'user_id': self.admin.id, 'user_name': 'Admin',
            'exclude_channel': 'someone-else',
        }
        async_to_sync(consumer.ticket_commented)(event)
        consumer.send.assert_awaited_once()
        sent = json.loads(consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(sent, {
            'type': 'ticket_commented', 'ticket_id': 7,
            'comment': {'id': 3, 'comment': 'hi'}, 'comments_count': 4,
            'user_id': self.admin.id, 'user_name': 'Admin',
        })

    def test_handler_skips_excluded_channel(self):
        from asgiref.sync import async_to_sync
        consumer = self._consumer(channel_name='me')
        async_to_sync(consumer.ticket_commented)({
            'type': 'ticket_commented', 'ticket_id': 7, 'exclude_channel': 'me',
        })
        consumer.send.assert_not_awaited()


class TestTicketCommentedBroadcast(BroadcastTestCase):

    @patch('users.consumers.broadcast_ticket_commented', new_callable=AsyncMock)
    def test_comment_broadcasts_comment_and_count(self, mock_broadcast):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        self.create_comment(ticket, self.admin, 'first')
        mock_broadcast.reset_mock()

        resp = self.api_post('/api/comments/', {
            'ticket': ticket.id, 'comment': 'second',
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201)

        mock_broadcast.assert_awaited_once()
        kwargs = mock_broadcast.await_args.kwargs
        self.assertEqual(kwargs['board_id'], self.board.id)
        self.assertEqual(kwargs['ticket_id'], ticket.id)
        self.assertEqual(kwargs['comments_count'], 2)
        self.assertEqual(kwargs['user'], self.admin)
        comment = kwargs['comment']
        self.assertEqual(comment['id'], resp.data['id'])
        self.assertEqual(comment['comment'], 'second')
        self.assertEqual(comment['user']['id'], self.admin.id)
        self.assertEqual(comment['user']['email'], 'bc-admin@test.com')
        self.assertIsInstance(comment['created_at'], str)
        json.dumps(comment)

    @patch('users.consumers.broadcast_ticket_commented', new_callable=AsyncMock)
    def test_add_comment_action_broadcasts(self, mock_broadcast):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        resp = self.api_post(f'/api/tickets/{ticket.id}/add_comment/', {
            'ticket': ticket.id, 'comment': 'via action',
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201)
        mock_broadcast.assert_awaited_once()
        self.assertEqual(mock_broadcast.await_args.kwargs['comments_count'], 1)


class TestUpdatedByAttribution(BroadcastTestCase):

    @patch('users.consumers.broadcast_ticket_moved', new_callable=AsyncMock)
    def test_move_broadcast_carries_user_and_position(self, mock_broadcast):
        ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)
        self.create_ticket(title='Other', column=self.col2, created_by=self.admin)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/move_to_column/', {
            'column_id': self.col2.id, 'position_in_column': 0,
        }, user=self.admin)
        self.assertEqual(resp.status_code, 200)

        mock_broadcast.assert_awaited_once()
        kwargs = mock_broadcast.await_args.kwargs
        self.assertEqual(kwargs['updated_by'], self.admin)
        self.assertEqual(kwargs['from_column_id'], self.col1.id)
        self.assertEqual(kwargs['to_column_id'], self.col2.id)
        self.assertEqual(kwargs['position'], 0)

    @patch('users.consumers.broadcast_ticket_updated', new_callable=AsyncMock)
    def test_patch_broadcast_carries_user_and_records_history_user(self, mock_broadcast):
        ticket = self.create_ticket(title='Old', column=self.col1, created_by=self.admin)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/', {'title': 'New'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)

        mock_broadcast.assert_awaited_once()
        kwargs = mock_broadcast.await_args.kwargs
        self.assertEqual(kwargs['updated_by'], self.admin)
        self.assertEqual(kwargs['changes']['title'], {'old': 'Old', 'new': 'New'})

        entry = TicketHistory.objects.get(ticket=ticket, field_name='title')
        self.assertEqual(entry.user_id, self.admin.id)
