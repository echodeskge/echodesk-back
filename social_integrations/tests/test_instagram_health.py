"""
Instagram messaging health (ticket #231): detect the "Allow access to
messages" toggle being off (Graph #200 / subcode 2534041), store it on the
connection, expose it in the status API, and keep reconnect working.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from django.utils import timezone
from rest_framework import status

from social_integrations.models import FacebookPageConnection, InstagramAccountConnection
from social_integrations.oauth_state import make_oauth_state
from social_integrations.services import instagram_health
from social_integrations.tests.conftest import SocialIntegrationTestCase

DISABLED_ERROR = {
    'error': {
        'message': 'The owner of this Instagram account has not allowed access to messages.',
        'type': 'OAuthException',
        'code': 200,
        'error_subcode': 2534041,
    }
}


def _resp(status_code=200, data=None):
    r = MagicMock()
    r.status_code = status_code
    r.content = b'x'
    r.json.return_value = data if data is not None else {}
    r.text = str(data)
    return r


class TestInstagramHealthHelpers(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.page = self.create_fb_connection(page_id='page_h')
        self.account = self.create_ig_connection(fb_page=self.page, username='shop')

    def test_detects_messaging_disabled_error(self):
        self.assertTrue(instagram_health.is_messaging_disabled_error(DISABLED_ERROR))
        self.assertFalse(instagram_health.is_messaging_disabled_error(
            {'error': {'code': 10, 'error_subcode': 2018278}}))
        self.assertFalse(instagram_health.is_messaging_disabled_error({}))
        self.assertFalse(instagram_health.is_messaging_disabled_error(None))

    @patch('social_integrations.services.instagram_health.requests.get')
    def test_check_flags_disabled_messaging(self, get):
        get.return_value = _resp(400, DISABLED_ERROR)
        result = instagram_health.check_instagram_messaging(self.account)
        self.assertEqual(result, 'messaging_disabled')
        self.account.refresh_from_db()
        self.assertEqual(self.account.messaging_status, 'messaging_disabled')
        self.assertEqual(self.account.messaging_error_code, '200/2534041')
        self.assertIn('allowed access to messages', self.account.messaging_error)
        self.assertIsNotNone(self.account.messaging_checked_at)
        # Probes the Page's Instagram conversations with the Page token.
        url = get.call_args[0][0]
        params = get.call_args[1]['params']
        self.assertTrue(url.endswith('/page_h/conversations'))
        self.assertEqual(params['platform'], 'instagram')
        self.assertEqual(params['access_token'], self.page.page_access_token)

    @patch('social_integrations.services.instagram_health.requests.get')
    def test_check_ok_clears_previous_error(self, get):
        self.account.messaging_status = 'messaging_disabled'
        self.account.messaging_error = 'old'
        self.account.save()
        get.return_value = _resp(200, {'data': []})
        self.assertEqual(instagram_health.check_instagram_messaging(self.account), 'ok')
        self.account.refresh_from_db()
        self.assertEqual(self.account.messaging_status, 'ok')
        self.assertEqual(self.account.messaging_error, '')

    @patch('social_integrations.services.instagram_health.requests.get')
    def test_other_graph_error_is_generic_error(self, get):
        get.return_value = _resp(400, {'error': {'code': 190, 'message': 'Token expired'}})
        self.assertEqual(instagram_health.check_instagram_messaging(self.account), 'error')
        self.account.refresh_from_db()
        self.assertEqual(self.account.messaging_error, 'Token expired')

    @patch('social_integrations.services.instagram_health.requests.get')
    def test_network_failure_keeps_previous_status(self, get):
        import requests
        self.account.messaging_status = 'ok'
        self.account.save()
        get.side_effect = requests.ConnectionError('boom')
        self.assertEqual(instagram_health.check_instagram_messaging(self.account), 'ok')

    def test_missing_page_token_is_error(self):
        self.page.page_access_token = ''
        self.page.save()
        self.assertEqual(instagram_health.check_instagram_messaging(self.account), 'error')

    def test_record_ignores_conversation_level_errors(self):
        self.assertFalse(instagram_health.record_messaging_error(
            self.account, {'error': {'code': 10, 'error_subcode': 2018278, 'message': 'window'}}))
        self.account.refresh_from_db()
        self.assertEqual(self.account.messaging_status, 'unknown')

    def test_needs_check(self):
        self.assertTrue(instagram_health.needs_check(self.account))
        self.account.messaging_status = 'ok'
        self.account.messaging_checked_at = timezone.now()
        self.assertFalse(instagram_health.needs_check(self.account))
        self.account.messaging_checked_at = timezone.now() - timedelta(hours=7)
        self.assertTrue(instagram_health.needs_check(self.account))
        self.account.is_active = False
        self.assertFalse(instagram_health.needs_check(self.account))


class TestInstagramStatusHealth(SocialIntegrationTestCase):

    url = '/api/social/instagram/status/'

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='ig-health@test.com')
        self.page = self.create_fb_connection()
        self.account = self.create_ig_connection(fb_page=self.page)

    @patch('social_integrations.services.instagram_health.requests.get')
    def test_status_reports_messaging_disabled(self, get):
        get.return_value = _resp(400, DISABLED_ERROR)
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        row = resp.data['accounts'][0]
        self.assertEqual(row['messaging_status'], 'messaging_disabled')
        self.assertEqual(row['messaging_error_code'], '200/2534041')
        self.assertIsNotNone(row['messaging_checked_at'])

    @patch('social_integrations.services.instagram_health.requests.get')
    def test_fresh_result_is_cached_unless_forced(self, get):
        self.account.messaging_status = 'ok'
        self.account.messaging_checked_at = timezone.now()
        self.account.save()
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.data['accounts'][0]['messaging_status'], 'ok')
        get.assert_not_called()

        get.return_value = _resp(400, DISABLED_ERROR)
        resp = self.api_get(self.url + '?check=true', user=self.agent)
        self.assertEqual(resp.data['accounts'][0]['messaging_status'], 'messaging_disabled')
        get.assert_called_once()

    @patch('social_integrations.services.instagram_health.requests.get')
    def test_inactive_accounts_are_not_probed(self, get):
        self.account.is_active = False
        self.account.save()
        resp = self.api_get(self.url + '?check=true', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['accounts'][0]['messaging_status'], 'unknown')
        get.assert_not_called()


class TestInstagramSendFlagsDisabledMessaging(SocialIntegrationTestCase):

    url = '/api/social/instagram/send-message/'

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='ig-health-send@test.com')
        self.page = self.create_fb_connection()
        self.account = self.create_ig_connection(fb_page=self.page)
        self.create_ig_message(account_connection=self.account, sender_id='cust_1',
                               is_from_business=False, timestamp=timezone.now())

    def _send(self):
        return self.api_post(self.url, {
            'recipient_id': 'cust_1', 'message': 'hi',
            'instagram_account_id': self.account.instagram_account_id,
        }, user=self.agent)

    @patch('social_integrations.views.requests.post')
    def test_send_failure_flags_account(self, post):
        post.return_value = _resp(400, DISABLED_ERROR)
        resp = self._send()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'INSTAGRAM_MESSAGING_DISABLED')
        self.account.refresh_from_db()
        self.assertEqual(self.account.messaging_status, 'messaging_disabled')

    @patch('social_integrations.views.requests.post')
    def test_send_success_clears_flag(self, post):
        self.account.messaging_status = 'messaging_disabled'
        self.account.save()
        post.return_value = _resp(200, {'message_id': 'mid_ok', 'recipient_id': 'cust_1'})
        resp = self._send()
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.account.refresh_from_db()
        self.assertEqual(self.account.messaging_status, 'ok')


class TestFacebookReconnectInstagram(SocialIntegrationTestCase):
    """Disconnect + reconnect must reactivate the Instagram account, keep the
    page webhook subscription, and surface a disabled messaging toggle."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='ig-reconnect@test.com')

    def _graph_get(self, conversations_response):
        def fake_get(url, params=None, **kwargs):
            if url.endswith('/oauth/access_token'):
                return _resp(200, {'access_token': 'user_token'})
            if url.endswith('/me/accounts'):
                return _resp(200, {'data': [
                    {'id': 'page_r', 'name': 'Shop', 'access_token': 'new_page_token'}]})
            if url.endswith('/me'):
                return _resp(200, {'id': 'fb_user_1'})
            if url.endswith('/page_r') and params.get('fields') == 'instagram_business_account':
                return _resp(200, {'instagram_business_account': {'id': 'ig_r'}})
            if url.endswith('/ig_r'):
                return _resp(200, {'id': 'ig_r', 'username': 'shop'})
            if url.endswith('/page_r/conversations'):
                return conversations_response
            raise AssertionError(f'unexpected GET {url}')
        return fake_get

    def _callback(self, conversations_response):
        state = make_oauth_state('test', user_id=self.admin.id)
        with patch('social_integrations.views.requests.get', side_effect=self._graph_get(conversations_response)), \
                patch('social_integrations.views.requests.post', return_value=_resp(200, {'success': True})) as post:
            resp = self.client.get('/api/social/facebook/oauth/callback/', {'code': 'c', 'state': state},
                                   HTTP_HOST='tenant.test.com')
        return resp, post

    def test_disconnect_then_reconnect_reactivates_instagram(self):
        page = self.create_fb_connection(page_id='page_r')
        account = self.create_ig_connection(instagram_account_id='ig_r', fb_page=page)

        resp = self.api_post('/api/social/facebook/disconnect/', {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        account.refresh_from_db()
        # The IG row holds a copy of the Page token — it goes with the Page.
        self.assertFalse(account.is_active)
        self.assertEqual(account.access_token, '')

        resp, post = self._callback(_resp(200, {'data': []}))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('facebook_status=connected', resp['Location'])

        account.refresh_from_db()
        page.refresh_from_db()
        self.assertTrue(page.is_active)
        self.assertTrue(account.is_active)
        self.assertEqual(account.access_token, 'new_page_token')
        self.assertEqual(account.messaging_status, 'ok')
        # Page webhooks re-subscribed with the messaging fields Instagram DMs use.
        fields = post.call_args[1]['params']['subscribed_fields'].split(',')
        for field in ('messages', 'messaging_postbacks', 'message_reactions', 'message_echoes'):
            self.assertIn(field, fields)

    def test_reconnect_warns_when_messaging_access_is_off(self):
        resp, _ = self._callback(_resp(400, DISABLED_ERROR))
        self.assertEqual(resp.status_code, 302)
        message = parse_qs(urlparse(resp['Location']).query)
        flat = ' '.join(v for vals in message.values() for v in vals)
        self.assertIn('Allow access to messages', flat)
        account = InstagramAccountConnection.objects.get(instagram_account_id='ig_r')
        self.assertTrue(account.is_active)
        self.assertEqual(account.messaging_status, 'messaging_disabled')

    def test_single_page_disconnect_deactivates_its_instagram(self):
        page = self.create_fb_connection(page_id='page_s')
        other = self.create_fb_connection(page_id='page_o')
        mine = self.create_ig_connection(fb_page=page)
        theirs = self.create_ig_connection(fb_page=other)
        resp = self.api_post('/api/social/facebook/pages/page_s/disconnect/', {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, getattr(resp, 'data', None))
        mine.refresh_from_db(); theirs.refresh_from_db()
        self.assertFalse(mine.is_active)
        self.assertTrue(theirs.is_active)
        self.assertTrue(FacebookPageConnection.objects.get(page_id='page_o').is_active)
