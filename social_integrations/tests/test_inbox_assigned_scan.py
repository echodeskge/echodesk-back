"""ECHODESK-BACKEND-7W/83/89/8A: the "Assigned to me" inbox tab narrowed the
conversation-head scan by sender only, which the (connection, sender,
-timestamp) index can't serve; the COUNT took up to 5s on busy tenants. The
scan now pins both index columns (connection ids resolved from the
assignments' account ids)."""
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from social_integrations.tests.conftest import SocialIntegrationTestCase

URL = '/api/social/conversations/'


class TestAssignedTabHeadScan(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='assigned-agent@test.com')
        self.now = timezone.now()
        self.page = self.create_fb_connection(page_id='pg1', page_name='Page One')
        self.other_page = self.create_fb_connection(page_id='pg2', page_name='Page Two')
        self.ig = self.create_ig_connection(instagram_account_id='ig1', username='brand')
        self.create_settings(chat_assignment_enabled=True)

    def _ago(self, minutes):
        return self.now - timedelta(minutes=minutes)

    def _ids(self, resp):
        return [r['conversation_id'] for r in resp.data['results']]

    def test_facebook_assigned_tab_pins_both_index_columns(self):
        self.create_fb_message(page_connection=self.page, sender_id='mine', timestamp=self._ago(1),
                               sender_name='Mine', message_text='hi')
        self.create_fb_message(page_connection=self.page, sender_id='theirs', timestamp=self._ago(2),
                               sender_name='Theirs', message_text='hi')
        # Same sender id on another page: a different conversation, not mine.
        self.create_fb_message(page_connection=self.other_page, sender_id='mine', timestamp=self._ago(3),
                               sender_name='Mine elsewhere', message_text='hi')
        self.create_chat_assignment(user=self.agent, platform='facebook', conversation_id='mine',
                                    account_id='pg1', status='active')

        with CaptureQueriesContext(connection) as ctx:
            resp = self.api_get(URL + '?assigned=true&platforms=facebook', user=self.agent)

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._ids(resp), ['fb_pg1_mine'])
        self.assertEqual(resp.data['count'], 1)
        head_scans = [
            q['sql'] for q in ctx.captured_queries
            if 'DISTINCT ON' in q['sql'] and 'social_integrations_facebookmessage' in q['sql']
        ]
        self.assertTrue(head_scans, 'expected a DISTINCT ON head scan')
        for sql in head_scans:
            self.assertIn('"page_connection_id" IN', sql)
            self.assertIn('"sender_id" IN', sql)

    def test_facebook_assigned_tab_with_no_assignments_is_empty(self):
        self.create_fb_message(page_connection=self.page, sender_id='someone', timestamp=self._ago(1))
        resp = self.api_get(URL + '?assigned=true&platforms=facebook', user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._ids(resp), [])
        self.assertEqual(resp.data['count'], 0)

    def test_instagram_assigned_tab_pins_both_index_columns(self):
        self.create_ig_message(account_connection=self.ig, sender_id='mine', sender_username='mine',
                               timestamp=self._ago(1))
        self.create_ig_message(account_connection=self.ig, sender_id='theirs', sender_username='theirs',
                               timestamp=self._ago(2))
        self.create_chat_assignment(user=self.agent, platform='instagram', conversation_id='mine',
                                    account_id='ig1', status='in_session')

        with CaptureQueriesContext(connection) as ctx:
            resp = self.api_get(URL + '?assigned=true&platforms=instagram', user=self.agent)

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._ids(resp), ['ig_ig1_mine'])
        head_scans = [
            q['sql'] for q in ctx.captured_queries
            if 'DISTINCT ON' in q['sql'] and 'social_integrations_instagrammessage' in q['sql']
        ]
        self.assertTrue(head_scans, 'expected a DISTINCT ON head scan')
        for sql in head_scans:
            self.assertIn('"account_connection_id" IN', sql)
            self.assertIn('"sender_id" IN', sql)
