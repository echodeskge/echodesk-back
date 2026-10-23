"""Agent "End session" on a website (widget) chat — bug #236.

The inbox names a widget chat by visitor id (the list row's sender_id), and
ChatAssignment rows are keyed by it too, but the close endpoint only looked
the chat up by session id, so ending an assigned chat failed with
session_not_found and the assignment stayed open.
"""
from django.utils import timezone
from rest_framework import status

from social_integrations.models import ChatAssignment, ConversationArchive, WidgetMessage, WidgetSession
from social_integrations.tests.test_widget_public import WidgetPublicTestCase

CLOSE_URL = '/api/widget/admin/sessions/close/'
SEND_URL = '/api/widget/admin/messages/send/'
VISITOR_ID = 'dec835b6-40ff-4bb6-a786-47c3ef1a8fc8'


class WidgetAdminCloseTests(WidgetPublicTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_admin(email='widget-agent@test.com')
        self.conn = self._make_connection()
        self.session = WidgetSession.objects.create(
            connection_id=self.conn.id, session_id='63ed3c42b6bd400e92763183b1ddd98b',
            visitor_id=VISITOR_ID,
        )
        self.assignment = ChatAssignment.objects.create(
            platform='widget', conversation_id=VISITOR_ID, account_id=str(self.conn.id),
            assigned_user=self.agent, status='in_session', session_started_at=timezone.now(),
        )

    def _close(self, key):
        return self.api_post(
            CLOSE_URL, {'connection_id': self.conn.id, 'session_id': key}, user=self.agent,
        )

    def _assert_released(self):
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, 'completed')
        self.assertIsNone(self.assignment.assigned_user_id)
        self.assertIsNotNone(self.assignment.session_ended_at)

    def test_close_by_visitor_id_ends_session_and_assignment(self):
        # What the dashboard sends from the Assigned tab
        resp = self._close(VISITOR_ID)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'ok')
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.ended_at)
        self.assertEqual(self.session.ended_by, 'agent')
        self._assert_released()
        self.assertTrue(ConversationArchive.objects.filter(
            platform='widget', conversation_id=self.session.session_id,
            account_id=str(self.conn.id)).exists())

    def test_close_by_session_id_releases_visitor_keyed_assignment(self):
        resp = self._close(self.session.session_id)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self._assert_released()

    def test_close_by_list_id(self):
        resp = self._close(f'widget_{self.conn.id}_{self.session.session_id}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self._assert_released()

    def test_close_picks_newest_session_of_visitor(self):
        newer = WidgetSession.objects.create(
            connection_id=self.conn.id, session_id='7a2dba0745c341468d767393d19a1617',
            visitor_id=VISITOR_ID,
        )
        WidgetSession.objects.filter(pk=self.session.pk).update(
            last_seen_at=timezone.now() - timezone.timedelta(hours=1))
        resp = self._close(VISITOR_ID)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        newer.refresh_from_db()
        self.assertIsNotNone(newer.ended_at)

    def test_already_ended_session_still_releases_stuck_assignment(self):
        # Prod state for the ticket: session closed earlier, assignment left open
        self.session.ended_at = timezone.now()
        self.session.ended_by = 'agent'
        self.session.save()
        resp = self._close(VISITOR_ID)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'already_ended')
        self._assert_released()

    def test_repeat_close_is_idempotent(self):
        self.assertEqual(self._close(VISITOR_ID).status_code, status.HTTP_200_OK)
        resp = self._close(VISITOR_ID)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'already_ended')
        self.assertEqual(ConversationArchive.objects.filter(platform='widget').count(), 1)

    def test_assignment_without_session_is_released(self):
        orphan = ChatAssignment.objects.create(
            platform='widget', conversation_id='no-such-visitor', account_id=str(self.conn.id),
            assigned_user=self.agent, status='in_session',
        )
        resp = self._close('no-such-visitor')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'already_ended')
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, 'completed')

    def test_unknown_chat_returns_404(self):
        resp = self._close('nothing-here')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(resp.data['error'], 'session_not_found')

    def test_send_by_visitor_id_reaches_the_session(self):
        resp = self.api_post(
            SEND_URL,
            {'connection_id': self.conn.id, 'session_id': VISITOR_ID, 'message_text': 'hello'},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertTrue(WidgetMessage.objects.filter(session=self.session, message_text='hello').exists())
