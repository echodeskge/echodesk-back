"""
Sentry ECHODESK-BACKEND-7H / 7G "Failed to subscribe page to webhooks" and
87 "Failed to send WhatsApp message: Invalid OAuth access token".

Both are expected 4xx answers from Meta that are already reported to the
user (unsubscribed_pages in the OAuth redirect, the 400 body of the send
endpoint). They are logged at warning now, not error.
"""
from unittest.mock import MagicMock, patch

from rest_framework import status

from social_integrations.models import FacebookPageConnection
from social_integrations.oauth_state import make_oauth_state
from social_integrations.tests.conftest import SocialIntegrationTestCase

LOGGER = 'social_integrations.views'


def _resp(status_code=200, data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data or {}
    resp.text = str(data or {})
    return resp


class TestWhatsAppSendMetaError(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='wa-4xx@test.com')
        self.account = self.create_wa_account(waba_id='waba-4xx')

    def test_invalid_token_is_a_warning_and_a_400(self):
        meta_error = {'error': {
            'message': 'Invalid OAuth access token - Cannot parse access token',
            'type': 'OAuthException', 'code': 190,
        }}
        with patch('social_integrations.http_retry.requests.post', return_value=_resp(401, meta_error)), \
                self.assertLogs(LOGGER, level='WARNING') as logs:
            resp = self.api_post('/api/social/whatsapp/send-message/', {
                'to_number': '+995555000111', 'message': 'hi', 'waba_id': 'waba-4xx',
            }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)
        self.assertEqual(resp.data['error'],
                         'Failed to send message: Invalid OAuth access token - Cannot parse access token')
        send_logs = [r for r in logs.records if 'Failed to send WhatsApp message' in r.getMessage()]
        self.assertEqual([r.levelname for r in send_logs], ['WARNING'])
        self.assertIn('Meta code 190', send_logs[0].getMessage())
        self.account.refresh_from_db()
        self.assertTrue(self.account.is_active)


class TestFacebookSubscribeMetaError(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='fb-4xx@test.com')

    @staticmethod
    def _graph_get(url, params=None, **kwargs):
        if url.endswith('/oauth/access_token'):
            return _resp(200, {'access_token': 'user_token'})
        if url.endswith('/me/accounts'):
            return _resp(200, {'data': [{'id': 'page_4xx', 'name': 'Shop', 'access_token': 'page_token'}]})
        if url.endswith('/me'):
            return _resp(200, {'id': 'fb_user_1'})
        if url.endswith('/page_4xx') and (params or {}).get('fields') == 'instagram_business_account':
            return _resp(200, {})
        if url.endswith('/page_4xx/conversations'):
            return _resp(200, {'data': []})
        return _resp(200, {})

    def test_subscribe_4xx_is_a_warning_and_marks_the_page_unsubscribed(self):
        denied = {'error': {'message': '(#200) Requires pages_manage_metadata permission', 'code': 200}}
        state = make_oauth_state('test', user_id=self.admin.id)
        with patch('social_integrations.views.requests.get', side_effect=self._graph_get), \
                patch('social_integrations.views.requests.post', return_value=_resp(400, denied)), \
                self.assertLogs(LOGGER, level='INFO') as logs:
            resp = self.client.get('/api/social/facebook/oauth/callback/', {'code': 'c', 'state': state},
                                   HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 302)
        subscribe_logs = [r for r in logs.records if 'Failed to subscribe page' in r.getMessage()]
        self.assertEqual([r.levelname for r in subscribe_logs], ['WARNING'])
        page = FacebookPageConnection.objects.get(page_id='page_4xx')
        self.assertTrue(page.is_active)
        self.assertFalse(page.webhook_subscribed)
