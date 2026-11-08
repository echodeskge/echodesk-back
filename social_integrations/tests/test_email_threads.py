"""Email inbox thread state: assignment, ticket from email, views, annotations."""
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status

from social_integrations.models import (
    ChatAssignment, EmailConnectionUserAssignment, EmailThreadTicket,
)
from social_integrations.tests.conftest import SocialIntegrationTestCase
from tickets.models import Ticket
from users.models import Notification

ASSIGN_URL = '/api/social/email/threads/assign/'
USERS_URL = '/api/social/email/threads/assignable-users/'
TICKET_URL = '/api/social/email/threads/create-ticket/'
LIST_URL = '/api/social/email-messages/'
FEATURE = 'social_integrations.views_email_threads.has_subscription_feature'


class EmailThreadBase(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='thr-admin@test.com')
        self.agent = self.create_user(email='thr-agent@test.com', first_name='Ana', last_name='Beridze')
        self.other = self.create_user(email='thr-other@test.com', first_name='Luka', last_name='Kapanadze')
        self.conn = self.create_email_connection()
        self.msg = self.create_email_message(connection=self.conn, thread_id='t-1', subject='Broken printer',
                                             body_text='It does not print.\nPlease help.')
        self.reply = self.create_email_message(connection=self.conn, thread_id='t-1', is_from_business=True,
                                               subject='Re: Broken printer')

    def assign(self, user, user_id, thread_id='t-1', conn=None):
        return self.api_post(ASSIGN_URL, {
            'connection_id': (conn or self.conn).id, 'thread_id': thread_id, 'user_id': user_id,
        }, user=user)


class TestAssign(EmailThreadBase):
    def test_assign_creates_email_chat_assignment_and_notifies(self):
        resp = self.assign(self.admin, self.agent.id)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['assignee'], {
            'id': self.agent.id, 'name': 'Ana Beridze', 'initials': 'AB', 'email': 'thr-agent@test.com',
        })
        row = ChatAssignment.objects.get(platform='email', conversation_id='t-1')
        self.assertEqual(row.account_id, str(self.conn.id))
        self.assertEqual(row.assigned_user, self.agent)
        self.assertEqual(row.status, 'active')
        note = Notification.objects.get(user=self.agent, notification_type='message_assigned')
        self.assertTrue(note.link_url.startswith('/email/INBOX/'))

    def test_not_gated_by_chat_assignment_mode(self):
        self.create_settings(chat_assignment_enabled=False)
        self.assertEqual(self.assign(self.agent, self.agent.id).status_code, status.HTTP_200_OK)

    def test_self_assign_does_not_notify(self):
        self.assign(self.agent, self.agent.id)
        self.assertFalse(Notification.objects.filter(user=self.agent, notification_type='message_assigned').exists())

    def test_reassign_by_non_holder_forbidden_admin_allowed(self):
        self.assign(self.agent, self.agent.id)
        self.assertEqual(self.assign(self.other, self.other.id).status_code, status.HTTP_403_FORBIDDEN)
        resp = self.assign(self.admin, self.other.id)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(ChatAssignment.objects.filter(platform='email', conversation_id='t-1').count(), 1)
        self.assertEqual(ChatAssignment.objects.get(platform='email', conversation_id='t-1').assigned_user, self.other)

    def test_holder_can_hand_over_and_unassign(self):
        self.assign(self.agent, self.agent.id)
        self.assertEqual(self.assign(self.agent, self.other.id).status_code, status.HTTP_200_OK)
        resp = self.assign(self.other, None)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsNone(resp.json()['assignee'])
        self.assertFalse(ChatAssignment.objects.filter(platform='email', conversation_id='t-1').exists())

    def test_target_must_access_mailbox(self):
        EmailConnectionUserAssignment.objects.create(connection=self.conn, user=self.agent)
        self.assertEqual(self.assign(self.admin, self.other.id).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.assign(self.admin, self.agent.id).status_code, status.HTTP_200_OK)

    def test_caller_without_mailbox_access_gets_404(self):
        EmailConnectionUserAssignment.objects.create(connection=self.conn, user=self.agent)
        self.assertEqual(self.assign(self.other, self.other.id).status_code, status.HTTP_404_NOT_FOUND)

    def test_unknown_thread_404(self):
        self.assertEqual(self.assign(self.admin, self.agent.id, thread_id='nope').status_code,
                         status.HTTP_404_NOT_FOUND)

    def test_inactive_target_refused(self):
        self.other.is_active = False
        self.other.save()
        self.assertEqual(self.assign(self.admin, self.other.id).status_code, status.HTTP_400_BAD_REQUEST)

    def test_assignable_users(self):
        EmailConnectionUserAssignment.objects.create(connection=self.conn, user=self.agent)
        resp = self.api_get(f'{USERS_URL}?connection_id={self.conn.id}', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {u['id'] for u in resp.json()}
        self.assertIn(self.agent.id, ids)
        self.assertIn(self.admin.id, ids)
        self.assertNotIn(self.other.id, ids)


@patch(FEATURE, return_value=True)
class TestCreateTicket(EmailThreadBase):
    def post(self, user, **extra):
        return self.api_post(TICKET_URL, {'connection_id': self.conn.id, 'thread_id': 't-1', **extra}, user=user)

    def test_creates_and_links(self, _feature):
        resp = self.post(self.agent)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        body = resp.json()
        self.assertTrue(body['created'])
        ticket = Ticket.objects.get(id=body['ticket']['id'])
        self.assertEqual(ticket.title, 'Broken printer')
        self.assertEqual(ticket.created_by, self.agent)
        self.assertIn('It does not print.', ticket.description)
        self.assertIn(self.msg.from_email, ticket.description)
        self.assertEqual(ticket.description_format, 'html')
        self.assertTrue(EmailThreadTicket.objects.filter(connection=self.conn, thread_id='t-1', ticket=ticket).exists())

    def test_second_call_returns_existing(self, _feature):
        first = self.post(self.agent).json()['ticket']
        resp = self.post(self.other)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {'ticket': first, 'created': False})
        self.assertEqual(Ticket.objects.count(), 1)

    def test_no_subject_fallback(self, _feature):
        self.create_email_message(connection=self.conn, thread_id='t-2', subject='')
        resp = self.api_post(TICKET_URL, {'connection_id': self.conn.id, 'thread_id': 't-2'}, user=self.agent)
        self.assertEqual(resp.json()['ticket']['title'], '(no subject)')

    def test_permission_required(self, _feature):
        self.agent.can_create_tickets = False
        self.agent.save()
        self.assertEqual(self.post(self.agent).status_code, status.HTTP_403_FORBIDDEN)

    def test_feature_gate(self, feature):
        feature.return_value = False
        self.assertEqual(self.post(self.agent).status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Ticket.objects.exists())

    def test_unknown_column(self, _feature):
        self.assertEqual(self.post(self.agent, column_id=999999).status_code, status.HTTP_400_BAD_REQUEST)


class TestListAnnotationsAndViews(EmailThreadBase):
    def setUp(self):
        super().setUp()
        self.msg2 = self.create_email_message(connection=self.conn, thread_id='t-2', subject='Invoice')
        self.msg3 = self.create_email_message(connection=self.conn, thread_id='t-3', subject='Hello')
        self.assign(self.admin, self.agent.id)                      # t-1 -> agent
        self.assign(self.admin, self.other.id, thread_id='t-2')     # t-2 -> other
        ticket = Ticket.objects.create(title='T', created_by=self.admin)
        EmailThreadTicket.objects.create(connection=self.conn, thread_id='t-3', ticket=ticket)
        self.ticket = ticket

    def rows(self, user, query=''):
        resp = self.api_get(f'{LIST_URL}?lite=1&folder=INBOX{query}', user=user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return {r['thread_id']: r for r in resp.json()['results']}

    def test_rows_carry_assignee_and_ticket(self):
        rows = self.rows(self.agent)
        self.assertEqual(rows['t-1']['assignee']['id'], self.agent.id)
        self.assertEqual(rows['t-2']['assignee']['id'], self.other.id)
        self.assertIsNone(rows['t-3']['assignee'])
        self.assertEqual(rows['t-3']['ticket'], {'id': self.ticket.id, 'title': 'T'})
        self.assertIsNone(rows['t-1']['ticket'])

    def test_full_rows_and_detail_and_threads(self):
        full = self.api_get(f'{LIST_URL}?thread_id=t-1', user=self.agent).json()['results']
        self.assertTrue(all(r['assignee']['id'] == self.agent.id for r in full))
        detail = self.api_get(f'{LIST_URL}{self.msg3.id}/', user=self.agent).json()
        self.assertEqual(detail['ticket']['id'], self.ticket.id)
        threads = {r['thread_id']: r for r in self.api_get(f'{LIST_URL}threads/', user=self.admin).json()}
        self.assertEqual(threads['t-2']['assignee']['id'], self.other.id)
        self.assertEqual(threads['t-3']['ticket']['id'], self.ticket.id)

    def test_views(self):
        self.assertEqual(set(self.rows(self.agent, '&view=assigned_to_me')), {'t-1'})
        self.assertEqual(set(self.rows(self.agent, '&view=unassigned')), {'t-3'})
        self.assertEqual(set(self.rows(self.agent, '&view=ticketed')), {'t-3'})
        threads = self.api_get(f'{LIST_URL}threads/?view=assigned_to_me', user=self.other).json()
        self.assertEqual({r['thread_id'] for r in threads}, {'t-2'})

    def test_unread_filter(self):
        self.msg2.is_read = True
        self.msg2.save()
        self.assertNotIn('t-2', self.rows(self.agent, '&unread=1'))
        self.assertIn('t-2', self.rows(self.agent))

    def test_no_n_plus_one(self):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.rows(self.admin)
            return len(ctx.captured_queries)
        baseline = count()
        for i in range(8):
            m = self.create_email_message(connection=self.conn, thread_id=f'extra-{i}')
            self.assign(self.admin, self.agent.id, thread_id=m.thread_id)
            EmailThreadTicket.objects.create(connection=self.conn, thread_id=m.thread_id,
                                             ticket=Ticket.objects.create(title=f'x{i}', created_by=self.admin))
        self.assertEqual(count(), baseline)
