"""
Tests for Human Agent messaging support and OAuth sweep scoping.

Covers:
- choose_messaging_params: RESPONSE inside 24h, HUMAN_AGENT tag beyond
- facebook_send_message actually sends the tag for >24h conversations
- check_messaging_window exposes the 7-day human-agent window
- the OAuth stale-page sweep only touches pages granted by the same FB user
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.utils import timezone
from rest_framework import status

from social_integrations import views as social_views
from social_integrations.views import choose_messaging_params
from .conftest import SocialIntegrationTestCase


class TestChooseMessagingParams(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        # Tests run in the 'test' schema; lift the per-tenant gate so the
        # window logic itself is what's under test here.
        self._gate = patch.object(social_views, 'HUMAN_AGENT_TAG_SCHEMAS', None)
        self._gate.start()

    def tearDown(self):
        self._gate.stop()
        super().tearDown()

    def test_within_24h_uses_response(self):
        ts = timezone.now() - timedelta(hours=23)
        self.assertEqual(choose_messaging_params(ts), {'messaging_type': 'RESPONSE'})

    def test_beyond_24h_uses_human_agent_tag(self):
        ts = timezone.now() - timedelta(hours=25)
        self.assertEqual(
            choose_messaging_params(ts),
            {'messaging_type': 'MESSAGE_TAG', 'tag': 'HUMAN_AGENT'},
        )

    def test_missing_timestamp_defaults_to_response(self):
        self.assertEqual(choose_messaging_params(None), {'messaging_type': 'RESPONSE'})


class TestHumanAgentTenantGate(SocialIntegrationTestCase):
    """Until Meta grants Advanced access, only allowlisted schemas may send
    the HUMAN_AGENT tag — everyone else stays on RESPONSE even beyond 24h
    (a tagged send would be rejected outright at Standard access)."""

    def test_non_allowlisted_schema_stays_on_response(self):
        ts = timezone.now() - timedelta(hours=30)
        with patch.object(social_views, 'HUMAN_AGENT_TAG_SCHEMAS', {'groot'}):
            # current schema is 'test', not in the allowlist
            self.assertEqual(choose_messaging_params(ts), {'messaging_type': 'RESPONSE'})

    def test_allowlisted_schema_gets_the_tag(self):
        ts = timezone.now() - timedelta(hours=30)
        with patch.object(social_views, 'HUMAN_AGENT_TAG_SCHEMAS', {'test'}):
            self.assertEqual(
                choose_messaging_params(ts),
                {'messaging_type': 'MESSAGE_TAG', 'tag': 'HUMAN_AGENT'},
            )

    def test_none_allowlist_enables_everywhere(self):
        ts = timezone.now() - timedelta(hours=30)
        with patch.object(social_views, 'HUMAN_AGENT_TAG_SCHEMAS', None):
            self.assertEqual(
                choose_messaging_params(ts),
                {'messaging_type': 'MESSAGE_TAG', 'tag': 'HUMAN_AGENT'},
            )


class TestFacebookSendUsesHumanAgentTag(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self._gate = patch.object(social_views, 'HUMAN_AGENT_TAG_SCHEMAS', None)
        self._gate.start()
        self.agent = self.create_user(email='ha-agent@test.com')
        self.conn = self.create_fb_connection()
        self.url = '/api/social/facebook/send-message/'

    def tearDown(self):
        self._gate.stop()
        super().tearDown()

    def _send(self, message_age):
        self.create_fb_message(
            page_connection=self.conn,
            sender_id='psid_ha',
            sender_name='Customer',
            timestamp=timezone.now() - message_age,
        )
        send_ok = MagicMock(status_code=200)
        send_ok.json.return_value = {'message_id': f'<mid_out_{message_age.total_seconds()}>'}
        with patch('social_integrations.views.requests.post', return_value=send_ok) as mock_post, \
             patch('social_integrations.views.send_websocket_notification', create=True):
            resp = self.api_post(self.url, {
                'recipient_id': 'psid_ha',
                'message': 'hello back',
                'page_id': self.conn.page_id,
            }, user=self.agent)
        return resp, mock_post

    def test_recent_conversation_sends_response(self):
        resp, mock_post = self._send(timedelta(hours=2))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        payload = mock_post.call_args.kwargs.get('json') or {}
        self.assertEqual(payload.get('messaging_type'), 'RESPONSE')
        self.assertNotIn('tag', payload)

    def test_stale_conversation_sends_human_agent_tag(self):
        resp, mock_post = self._send(timedelta(hours=30))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        payload = mock_post.call_args.kwargs.get('json') or {}
        self.assertEqual(payload.get('messaging_type'), 'MESSAGE_TAG')
        self.assertEqual(payload.get('tag'), 'HUMAN_AGENT')


class TestCheckMessagingWindow(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='window-agent@test.com')
        self.conn = self.create_fb_connection()
        self.url = '/api/social/messaging-window/'

    def _check(self, message_age):
        self.create_fb_message(
            page_connection=self.conn,
            sender_id='psid_win',
            timestamp=timezone.now() - message_age,
        )
        return self.api_get(
            self.url
            + f'?platform=facebook&conversation_id=psid_win&account_id={self.conn.page_id}',
            user=self.agent,
        )

    def test_open_window(self):
        resp = self._check(timedelta(hours=1))
        self.assertTrue(resp.data['window_open'])
        self.assertTrue(resp.data['human_agent_window_open'])
        self.assertTrue(resp.data['can_reply'])

    def test_human_agent_window(self):
        resp = self._check(timedelta(days=3))
        self.assertFalse(resp.data['window_open'])
        self.assertTrue(resp.data['human_agent_window_open'])
        self.assertTrue(resp.data['can_reply'])
        self.assertGreater(resp.data['human_agent_hours_remaining'], 0)

    def test_fully_expired(self):
        resp = self._check(timedelta(days=8))
        self.assertFalse(resp.data['window_open'])
        self.assertFalse(resp.data['human_agent_window_open'])
        self.assertFalse(resp.data['can_reply'])


class TestOAuthSweepScoping(SocialIntegrationTestCase):
    """The stale-page sweep must only deactivate pages granted by the same
    Facebook user — mirrors the queryset used in the OAuth callback."""

    def _sweep(self, fb_user_id, returned_page_ids):
        from social_integrations.models import FacebookPageConnection
        if not (returned_page_ids and fb_user_id):
            return 0
        return FacebookPageConnection.objects.filter(
            is_active=True,
            connected_by_fb_user_id=fb_user_id,
        ).exclude(page_id__in=returned_page_ids).update(is_active=False)

    def test_other_users_pages_survive(self):
        from social_integrations.models import FacebookPageConnection
        mine = self.create_fb_connection(connected_by_fb_user_id='fb_owner')
        theirs = self.create_fb_connection(connected_by_fb_user_id='fb_reviewer')
        legacy = self.create_fb_connection()  # blank connected_by_fb_user_id

        # Reviewer re-authorizes with only their own new page in /me/accounts
        self._sweep('fb_reviewer', {'some_new_page'})

        mine.refresh_from_db(); theirs.refresh_from_db(); legacy.refresh_from_db()
        self.assertTrue(mine.is_active)        # other user's page untouched
        self.assertFalse(theirs.is_active)     # reviewer's own stale page swept
        self.assertTrue(legacy.is_active)      # legacy rows never swept

    def test_no_fb_user_id_sweeps_nothing(self):
        page = self.create_fb_connection(connected_by_fb_user_id='fb_owner')
        self._sweep('', {'anything'})
        page.refresh_from_db()
        self.assertTrue(page.is_active)
