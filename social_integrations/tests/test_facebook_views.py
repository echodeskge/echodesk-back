"""
Tests for Facebook-related views.
Covers connection status, disconnect, send message, and message viewset.
"""
from unittest.mock import patch, MagicMock
from rest_framework import status
from social_integrations.tests.conftest import SocialIntegrationTestCase


class TestFacebookConnectionStatus(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='fb-admin@test.com')
        self.agent = self.create_user(email='fb-agent@test.com')
        self.url = '/api/social/facebook/status/'

    def test_returns_status_no_connections(self):
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_returns_connected_pages(self):
        self.create_fb_connection(page_name='My Page')
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_unauthenticated_denied(self):
        resp = self.client.get(self.url, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_no_feature_denied(self):
        """Fix 6 verification: connection status requires feature."""
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_get(self.url, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestFacebookDisconnect(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='fb-disc-admin@test.com')
        self.agent = self.create_user(email='fb-disc-agent@test.com')
        self.url = '/api/social/facebook/disconnect/'

    def test_admin_can_disconnect(self):
        self.create_fb_connection(page_id='page_to_disc')
        resp = self.api_post(self.url, {}, user=self.admin)
        # Should not be 403
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_agent_cannot_disconnect(self):
        resp = self.api_post(self.url, {}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestFacebookPageDisconnect(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='fb-pd-admin@test.com')

    def test_disconnect_specific_page(self):
        conn = self.create_fb_connection(page_id='specific_page')
        url = f'/api/social/facebook/pages/{conn.page_id}/disconnect/'
        resp = self.api_post(url, {}, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestFacebookSendMessage(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='fb-send@test.com')
        self.url = '/api/social/facebook/send-message/'

    def test_send_without_body_returns_400(self):
        resp = self.api_post(self.url, {}, user=self.agent)
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND])

    @patch('social_integrations.views.requests.post')
    def test_send_message_with_valid_data(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {'recipient_id': '123', 'message_id': 'mid_123'}
        )
        conn = self.create_fb_connection()
        resp = self.api_post(self.url, {
            'page_id': conn.page_id,
            'recipient_id': 'recipient_1',
            'message': 'Hello!',
        }, user=self.agent)
        # If connection found, should attempt to send
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestFacebookMessageViewSet(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='fb-msgs@test.com')
        self.url = '/api/social/facebook-messages/'

    def test_list_messages(self):
        conn = self.create_fb_connection()
        self.create_fb_message(page_connection=conn)
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_empty(self):
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_unauthenticated_denied(self):
        resp = self.client.get(self.url, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class TestFacebookPageViewSet(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='fb-pages-admin@test.com')
        self.agent = self.create_user(email='fb-pages-agent@test.com')
        self.url = '/api/social/facebook-pages/'

    def test_list_pages(self):
        self.create_fb_connection()
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_returns_connected_pages(self):
        self.create_fb_connection(page_name='Page A')
        self.create_fb_connection(page_name='Page B')
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_retrieve_page(self):
        conn = self.create_fb_connection()
        resp = self.api_get(f'{self.url}{conn.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class TestClearPlatformHistory(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='clear-admin@test.com')
        self.agent = self.create_user(email='clear-agent@test.com')
        self.url = '/api/social/clear-history/'

    def test_admin_can_clear(self):
        resp = self.api_post(self.url, {'platform': 'facebook'}, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_agent_cannot_clear(self):
        resp = self.api_post(self.url, {'platform': 'facebook'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestFacebookWebhookNameRecovery(SocialIntegrationTestCase):
    """When the Graph API refuses the message `from` lookup (400), the
    webhook must reuse the name already known for the PSID instead of
    renaming the conversation to 'Messenger User'."""

    def setUp(self):
        super().setUp()
        self.url = '/api/social/facebook/webhook/'
        self.conn = self.create_fb_connection()

    def _post_webhook(self, sender_id, mid):
        payload = {
            'object': 'page',
            'entry': [{
                'id': self.conn.page_id,
                'time': 1753200000000,
                'messaging': [{
                    'sender': {'id': sender_id},
                    'recipient': {'id': self.conn.page_id},
                    'timestamp': 1753200000000,
                    'message': {'mid': mid, 'text': 'hello'},
                }],
            }],
        }
        failed = MagicMock(status_code=400)
        failed.json.return_value = {'error': {'code': 100}}
        with patch('social_integrations.views.find_tenant_by_page_id', return_value=self.tenant.schema_name), \
             patch('social_integrations.views.requests.get', return_value=failed), \
             patch('social_integrations.views.get_facebook_profile_pic', return_value=None), \
             patch('social_integrations.views.send_websocket_notification'):
            return self.client.post(
                self.url, data=payload, format='json', HTTP_HOST='tenant.test.com'
            )

    def test_recovers_name_from_social_account(self):
        from social_integrations.models import FacebookMessage
        self.create_social_account(
            platform='facebook',
            platform_id='psid_recover_1',
            account_connection_id=self.conn.page_id,
            display_name='Salome Testishvili',
        )
        resp = self._post_webhook('psid_recover_1', '<mid_recover_1>')
        self.assertEqual(resp.status_code, 200)
        msg = FacebookMessage.objects.get(message_id='<mid_recover_1>')
        self.assertEqual(msg.sender_name, 'Salome Testishvili')

    def test_recovers_name_from_prior_message(self):
        from social_integrations.models import FacebookMessage
        self.create_fb_message(
            page_connection=self.conn,
            sender_id='psid_recover_2',
            sender_name='Levan Testishvili',
        )
        resp = self._post_webhook('psid_recover_2', '<mid_recover_2>')
        self.assertEqual(resp.status_code, 200)
        msg = FacebookMessage.objects.get(message_id='<mid_recover_2>')
        self.assertEqual(msg.sender_name, 'Levan Testishvili')

    def test_falls_back_when_nothing_known(self):
        from social_integrations.models import FacebookMessage
        resp = self._post_webhook('psid_unknown_3', '<mid_unknown_3>')
        self.assertEqual(resp.status_code, 200)
        msg = FacebookMessage.objects.get(message_id='<mid_unknown_3>')
        self.assertEqual(msg.sender_name, 'Messenger User')


class TestFacebookWebhookProfilePicCache(SocialIntegrationTestCase):
    """Incoming messages store the cached (our-storage) picture URL, and
    duplicate webhook deliveries skip Graph API work entirely."""

    def setUp(self):
        super().setUp()
        self.url = '/api/social/facebook/webhook/'
        self.conn = self.create_fb_connection()

    def _post_webhook(self, sender_id, mid, pic='https://spaces.test/media/x.jpg'):
        payload = {
            'object': 'page',
            'entry': [{
                'id': self.conn.page_id,
                'time': 1753200000000,
                'messaging': [{
                    'sender': {'id': sender_id},
                    'recipient': {'id': self.conn.page_id},
                    'timestamp': 1753200000000,
                    'message': {'mid': mid, 'text': 'hello'},
                }],
            }],
        }
        failed = MagicMock(status_code=400)
        failed.json.return_value = {'error': {'code': 100}}
        with patch('social_integrations.views.find_tenant_by_page_id', return_value=self.tenant.schema_name), \
             patch('social_integrations.views.requests.get', return_value=failed), \
             patch('social_integrations.views.get_facebook_profile_pic', return_value=pic) as mock_pic, \
             patch('social_integrations.views.send_websocket_notification'):
            resp = self.client.post(
                self.url, data=payload, format='json', HTTP_HOST='tenant.test.com'
            )
        return resp, mock_pic

    def test_message_stores_cached_picture_url(self):
        from social_integrations.models import FacebookMessage
        resp, mock_pic = self._post_webhook('psid_pic_1', '<mid_pic_1>')
        self.assertEqual(resp.status_code, 200)
        msg = FacebookMessage.objects.get(message_id='<mid_pic_1>')
        self.assertEqual(msg.profile_pic_url, 'https://spaces.test/media/x.jpg')
        mock_pic.assert_called_once_with('psid_pic_1', self.conn.page_access_token)

    def test_duplicate_delivery_skips_graph_work(self):
        from social_integrations.models import FacebookMessage
        self._post_webhook('psid_pic_2', '<mid_pic_2>')
        resp, mock_pic = self._post_webhook('psid_pic_2', '<mid_pic_2>')
        self.assertEqual(resp.status_code, 200)
        mock_pic.assert_not_called()
        self.assertEqual(
            FacebookMessage.objects.filter(message_id='<mid_pic_2>').count(), 1
        )
