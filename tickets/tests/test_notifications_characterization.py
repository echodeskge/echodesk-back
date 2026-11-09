"""Characterisation tests for the notifications that tickets/signals.py sends
(BE-08 step 1, F12).

Written before the BE-08 history/realtime refactor and green on the code as
it was then; they must stay green afterwards (history is muted during
``TicketSerializer.create`` but notifications are not).

Every action runs inside ``captureOnCommitCallbacks(execute=True)`` so the
tests hold whether a side effect runs inline or after commit.
"""
from unittest.mock import AsyncMock, patch

from django.db import connection

from tickets.models import BoardTelegramConnection
from tickets.tests.conftest import TicketTestCase
from users.models import Department, Notification

NOTIFY = 'tickets.signals.create_notification'


def _types_by_user(mock_notify):
    """[(user_id, notification_type), ...] in call order."""
    return [(c.kwargs['user'].id, c.kwargs['notification_type']) for c in mock_notify.call_args_list]


@patch('users.consumers.broadcast_ticket_created', new_callable=AsyncMock)
@patch('users.consumers.broadcast_ticket_updated', new_callable=AsyncMock)
@patch('users.consumers.broadcast_ticket_moved', new_callable=AsyncMock)
class TestNotificationCharacterization(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='nc-admin@test.com')
        self.board, self.col1, self.col2, _ = self.setup_board_with_columns(admin=self.admin)
        self.alice = self.create_user(email='nc-alice@test.com', first_name='Alice')
        self.bob = self.create_user(email='nc-bob@test.com', first_name='Bob')

    def test_column_change_notifies_previous_assignees(self, *mocks):
        ticket = self.create_ticket('Move me', column=self.col1, created_by=self.admin)
        self.create_assignment(ticket, self.alice, assigned_by=self.admin)
        self.create_assignment(ticket, self.bob, assigned_by=self.admin)

        with patch(NOTIFY) as notify, self.captureOnCommitCallbacks(execute=True):
            resp = self.api_patch(f'/api/tickets/{ticket.id}/move_to_column/', {
                'column_id': self.col2.id,
            }, user=self.admin)
        self.assertEqual(resp.status_code, 200)

        status_calls = [c for c in notify.call_args_list
                        if c.kwargs['notification_type'] == 'ticket_status_changed']
        self.assertEqual({c.kwargs['user'].id for c in status_calls}, {self.alice.id, self.bob.id})
        for c in status_calls:
            self.assertEqual(c.kwargs['ticket_id'], ticket.id)
            self.assertEqual(c.kwargs['metadata'], {
                'old_status': self.col1.name, 'new_status': self.col2.name,
            })

    def test_department_change_notifies_department_members(self, *mocks):
        dept = Department.objects.create(name='NC Support')
        self.alice.department = dept
        self.alice.save()
        self.bob.department = dept
        self.bob.save()
        ticket = self.create_ticket('Dept', column=self.col1, created_by=self.admin)

        with patch(NOTIFY) as notify, self.captureOnCommitCallbacks(execute=True):
            resp = self.api_patch(f'/api/tickets/{ticket.id}/', {
                'assigned_department_id': dept.id,
            }, user=self.admin)
        self.assertEqual(resp.status_code, 200)

        dept_calls = [c for c in notify.call_args_list
                      if c.kwargs['notification_type'] == 'ticket_assigned']
        self.assertEqual({c.kwargs['user'].id for c in dept_calls}, {self.alice.id, self.bob.id})
        self.assertEqual(dept_calls[0].kwargs['metadata']['department_id'], dept.id)
        self.assertEqual(dept_calls[0].kwargs['metadata']['department_name'], 'NC Support')

    def test_echodesk_board_bug_report_update_is_sent_cross_tenant(self, *mocks):
        # The reporter's tenant is this test tenant, so the cross-tenant
        # schema switch lands back here and the Notification row is real.
        reporter = self.create_user(email='nc-reporter@test.com')
        board = self.create_board(name='EchoDesk', created_by=self.admin)
        bugs = self.create_column(board, name='Bugs', position=0)
        in_progress = self.create_column(board, name='In Progress', position=1)
        ticket = self.create_ticket('[Bug] x', column=bugs, created_by=self.admin, metadata={
            'bug_report': True,
            'reporter_email': reporter.email,
            'reporter_tenant': connection.schema_name,
        })

        with self.captureOnCommitCallbacks(execute=True), \
                patch('users.consumers.send_notification_to_user', new_callable=AsyncMock), \
                patch('users.notification_utils._dispatch_push'):
            resp = self.api_patch(f'/api/tickets/{ticket.id}/move_to_column/', {
                'column_id': in_progress.id,
            }, user=self.admin)
        self.assertEqual(resp.status_code, 200)

        rows = Notification.objects.filter(user=reporter, notification_type='bug_report_update')
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().metadata, {'status': 'in_progress', 'ticket_title': '[Bug] x'})

    def test_create_sends_telegram_once(self, *mocks):
        conn = BoardTelegramConnection(board=self.board, chat_id='-100', is_active=True, created_by=self.admin)
        conn.set_bot_token('test-token')
        conn.save()

        with patch('tickets.telegram_utils.send_board_telegram_message') as send, \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api_post('/api/tickets/', {
                'title': 'Telegram me', 'description': 'body', 'column_id': self.col1.id,
            }, user=self.admin)
        self.assertEqual(resp.status_code, 201)

        send.assert_called_once()
        bot_token, chat_id, message = send.call_args.args
        self.assertEqual((bot_token, chat_id), ('test-token', '-100'))
        self.assertIn('Telegram me', message)

    def test_serializer_create_notifies_each_assigned_user(self, *mocks):
        with patch(NOTIFY) as notify, self.captureOnCommitCallbacks(execute=True):
            resp = self.api_post('/api/tickets/', {
                'title': 'Team', 'description': 'body', 'column_id': self.col1.id,
                'assigned_user_ids': [self.alice.id, self.bob.id],
            }, user=self.admin)
        self.assertEqual(resp.status_code, 201)

        assigned = [uid for uid, kind in _types_by_user(notify) if kind == 'ticket_assigned']
        self.assertEqual(sorted(assigned), sorted([self.alice.id, self.bob.id]))
        for c in notify.call_args_list:
            self.assertEqual(c.kwargs['ticket_id'], resp.data['id'])
