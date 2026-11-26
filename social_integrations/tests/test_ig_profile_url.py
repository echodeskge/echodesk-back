"""
Regression tests for 2026-11-26 (Sentry ECHODESK-BACKEND-8P / 8Q).

A reconnected Facebook page failed to save its Instagram account because the
Graph CDN profile-picture URL was 520 characters and the column was
varchar(500). The OAuth flow swallowed the IntegrityError as a warning, so
Instagram DMs were then rejected by the webhook ("account connection not
found"). The user cancelling the Facebook dialog also logged at ERROR and
created a Sentry issue.
"""
from unittest.mock import patch

from social_integrations.models import InstagramAccountConnection
from social_integrations.oauth_state import make_oauth_state
from social_integrations.tests.conftest import SocialIntegrationTestCase
from social_integrations.tests.test_instagram_health import _resp

LONG_PICTURE_URL = (
    "https://scontent-fra5-2.xx.fbcdn.net/v/t51.2885-15/"
    + "a" * 450
    + "_n.jpg?_nc_cat=1&ccb=1-7&oh=00_AY&oe=69"
)
assert len(LONG_PICTURE_URL) > 500


class TestInstagramLongProfilePicture(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='ig-long-url@test.com')

    def _graph_get(self, url, params=None, **kwargs):
        if url.endswith('/oauth/access_token'):
            return _resp(200, {'access_token': 'user_token'})
        if url.endswith('/me/accounts'):
            return _resp(200, {'data': [
                {'id': 'page_l', 'name': 'Shop', 'access_token': 'page_token_l'}]})
        if url.endswith('/me'):
            return _resp(200, {'id': 'fb_user_1'})
        if url.endswith('/page_l') and params.get('fields') == 'instagram_business_account':
            return _resp(200, {'instagram_business_account': {'id': 'ig_l'}})
        if url.endswith('/ig_l'):
            return _resp(200, {'id': 'ig_l', 'username': 'shop',
                               'profile_picture_url': LONG_PICTURE_URL})
        if url.endswith('/page_l/conversations'):
            return _resp(200, {'data': []})
        raise AssertionError(f'unexpected GET {url}')

    def test_oauth_saves_instagram_account_with_a_long_picture_url(self):
        state = make_oauth_state('test', user_id=self.admin.id)
        with patch('social_integrations.views.requests.get', side_effect=self._graph_get), \
                patch('social_integrations.views.requests.post', return_value=_resp(200, {'success': True})):
            resp = self.client.get('/api/social/facebook/oauth/callback/', {'code': 'c', 'state': state},
                                   HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('facebook_status=connected', resp['Location'])

        account = InstagramAccountConnection.objects.get(instagram_account_id='ig_l')
        self.assertTrue(account.is_active)
        self.assertEqual(account.profile_picture_url, LONG_PICTURE_URL)
        self.assertEqual(account.access_token, 'page_token_l')

    def test_user_cancelling_the_facebook_dialog_is_not_an_error(self):
        state = make_oauth_state('test', user_id=self.admin.id)
        with self.assertLogs('social_integrations.views', level='WARNING') as logs:
            resp = self.client.get('/api/social/facebook/oauth/callback/', {
                'error': 'access_denied', 'error_code': '200',
                'error_description': 'Permissions error', 'error_reason': 'user_denied',
                'state': state,
            }, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('facebook_status=error', resp['Location'])
        self.assertFalse([r for r in logs.records if r.levelname == 'ERROR'], logs.output)
        self.assertTrue(any('cancelled by the user' in r.getMessage() for r in logs.records))


class TestExpiredStateCancel(SocialIntegrationTestCase):
    """ECHODESK-BACKEND-8R: cancelling an expired dialog is not an error either."""

    CANCEL = {'error': 'access_denied', 'error_code': '200', 'error_description': 'Permissions error',
              'error_reason': 'user_denied', 'action': 'cancel'}

    def test_cancel_with_expired_state_logs_a_warning(self):
        with self.assertLogs('social_integrations.views', level='WARNING') as logs:
            resp = self.client.get('/api/social/facebook/oauth/callback/',
                                   {**self.CANCEL, 'state': 'expired-or-forged'}, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 302)
        self.assertFalse([r for r in logs.records if r.levelname == 'ERROR'], logs.output)
        self.assertTrue(any('expired state' in r.getMessage() for r in logs.records))

    def test_forged_state_without_a_cancel_is_still_an_error(self):
        with self.assertLogs('social_integrations.views', level='ERROR') as logs:
            resp = self.client.get('/api/social/facebook/oauth/callback/',
                                   {'code': 'c', 'state': 'forged'}, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(any('invalid state' in r.getMessage() for r in logs.records))
