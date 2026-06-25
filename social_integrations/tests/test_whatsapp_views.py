"""
Tests for WhatsApp-related views.
"""
from unittest.mock import patch, MagicMock, AsyncMock
from rest_framework import status
from social_integrations.tests.conftest import SocialIntegrationTestCase


class TestWhatsAppConnectionStatus(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='wa-agent@test.com')
        self.url = '/api/social/whatsapp/status/'

    def test_returns_status(self):
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_unauthenticated_denied(self):
        resp = self.client.get(self.url, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_no_feature_denied(self):
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_get(self.url, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_with_account(self):
        self.create_wa_account()
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class TestWhatsAppDisconnect(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='wa-disc-admin@test.com')
        self.agent = self.create_user(email='wa-disc-agent@test.com')
        self.url = '/api/social/whatsapp/disconnect/'

    def test_admin_can_disconnect(self):
        resp = self.api_post(self.url, {}, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_agent_cannot_disconnect(self):
        resp = self.api_post(self.url, {}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_disconnect_all_soft_deletes_and_preserves_messages(self):
        from social_integrations.models import (
            WhatsAppBusinessAccount, WhatsAppMessage,
        )
        acct = self.create_wa_account()
        self.create_wa_message(business_account=acct)
        self.create_wa_message(business_account=acct)

        resp = self.api_post(self.url, {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        acct.refresh_from_db()
        self.assertFalse(acct.is_active)
        self.assertEqual(
            WhatsAppMessage.objects.filter(business_account=acct).count(), 2
        )
        self.assertEqual(WhatsAppBusinessAccount.objects.count(), 1)

    def test_disconnect_specific_waba_id_soft_deletes_only_that_account(self):
        from social_integrations.models import WhatsAppBusinessAccount
        keep = self.create_wa_account(waba_id='waba_keep')
        target = self.create_wa_account(waba_id='waba_target')

        resp = self.api_post(self.url, {'waba_id': 'waba_target'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        keep.refresh_from_db()
        target.refresh_from_db()
        self.assertTrue(keep.is_active)
        self.assertFalse(target.is_active)
        self.assertEqual(WhatsAppBusinessAccount.objects.count(), 2)


class TestWhatsAppMessageViewSet(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='wa-msgs@test.com')
        self.url = '/api/social/whatsapp-messages/'

    def test_list_messages(self):
        acct = self.create_wa_account()
        self.create_wa_message(business_account=acct)
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_empty(self):
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class TestWhatsAppAccountViewSet(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='wa-acct@test.com')
        self.url = '/api/social/whatsapp-accounts/'

    def test_list_accounts(self):
        self.create_wa_account()
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_retrieve_account(self):
        acct = self.create_wa_account()
        resp = self.api_get(f'{self.url}{acct.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class TestWhatsAppContactViewSet(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='wa-contacts@test.com')
        self.url = '/api/social/whatsapp-contacts/'

    def test_list_contacts(self):
        acct = self.create_wa_account()
        self.create_wa_contact(account=acct)
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class TestWhatsAppSendMessage(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='wa-send@test.com')
        self.url = '/api/social/whatsapp/send-message/'

    def test_send_no_body_returns_error(self):
        resp = self.api_post(self.url, {}, user=self.agent)
        self.assertIn(resp.status_code, [
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_404_NOT_FOUND,
        ])

    def test_unauthenticated_denied(self):
        resp = self.client.post(self.url, {}, HTTP_HOST='tenant.test.com', content_type='application/json')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class TestWhatsAppTemplateViews(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='wa-tmpl-admin@test.com')
        self.agent = self.create_user(email='wa-tmpl-agent@test.com')

    def test_list_templates(self):
        acct = self.create_wa_account()
        self.create_wa_template(business_account=acct)
        url = f'/api/social/whatsapp/{acct.waba_id}/templates/'
        resp = self.api_get(url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_templates_empty(self):
        acct = self.create_wa_account()
        url = f'/api/social/whatsapp/{acct.waba_id}/templates/'
        resp = self.api_get(url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


def _graph_ok(message_id='wamid.TEST123'):
    return MagicMock(
        status_code=200,
        content=b'{"messages":[{"id":"x"}]}',
        text='ok',
        json=lambda: {'messages': [{'id': message_id}]},
    )


def _graph_error(code=131026, message='bad', http=400):
    return MagicMock(
        status_code=http,
        content=b'{"error":{}}',
        text='err',
        json=lambda: {'error': {'code': code, 'message': message}},
    )


class TestWhatsAppSendTemplateMessage(SocialIntegrationTestCase):
    """Business-initiated template sends — the 'start a new conversation' path."""

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='wa-tmpl-send@test.com')
        self.account = self.create_wa_account(waba_id='waba_send')
        self.template = self.create_wa_template(
            business_account=self.account,
            name='order_update',
            category='UTILITY',
            status='APPROVED',
            components=[{'type': 'BODY', 'text': 'Hi {{1}}, your code is {{2}}'}],
        )
        self.url = '/api/social/whatsapp/templates/send/'

    def _payload(self, **overrides):
        data = {
            'waba_id': self.account.waba_id,
            'template_id': self.template.id,
            'to_number': '+15551234567',
            'parameters': {'param1': 'Ann', 'param2': '123'},
            'opt_in_confirmed': True,
        }
        data.update(overrides)
        return data

    @patch('social_integrations.consumers.send_new_message_notification', new_callable=AsyncMock)
    @patch('social_integrations.views.requests.post')
    def test_renders_body_and_sets_sent_by(self, mock_post, mock_notify):
        from social_integrations.models import WhatsAppMessage
        mock_post.return_value = _graph_ok()
        resp = self.api_post(self.url, self._payload(), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        msg = WhatsAppMessage.objects.get(message_id='wamid.TEST123')
        self.assertEqual(msg.message_text, 'Hi Ann, your code is 123')
        self.assertNotIn('Template:', msg.message_text)
        self.assertEqual(msg.sent_by, self.agent)
        self.assertEqual(msg.message_type, 'template')
        self.assertEqual(msg.to_number, '15551234567')  # stored bare

    @patch('social_integrations.consumers.send_new_message_notification', new_callable=AsyncMock)
    @patch('social_integrations.views.requests.post')
    def test_unarchives_and_clears_completed_assignment(self, mock_post, mock_notify):
        from social_integrations.models import ConversationArchive, ChatAssignment
        mock_post.return_value = _graph_ok()
        bare = '15551234567'
        ConversationArchive.objects.create(
            platform='whatsapp', conversation_id=bare, account_id=self.account.waba_id,
        )
        assignment = self.create_chat_assignment(
            platform='whatsapp', conversation_id=bare, account_id=self.account.waba_id,
            status='completed', user=self.agent,
        )

        resp = self.api_post(self.url, self._payload(), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(ConversationArchive.objects.filter(conversation_id=bare).exists())
        self.assertFalse(ChatAssignment.objects.filter(id=assignment.id).exists())

    @patch('social_integrations.consumers.send_new_message_notification', new_callable=AsyncMock)
    @patch('social_integrations.views.requests.post')
    def test_broadcasts_once_with_rendered_text(self, mock_post, mock_notify):
        mock_post.return_value = _graph_ok()
        resp = self.api_post(self.url, self._payload(), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        mock_notify.assert_called_once()
        args = mock_notify.call_args.args  # (tenant_schema, normalized_to, message_data)
        self.assertEqual(args[1], '15551234567')
        self.assertEqual(args[2]['message_text'], 'Hi Ann, your code is 123')
        self.assertEqual(args[2]['message_type'], 'template')
        self.assertTrue(args[2]['is_from_business'])

    @patch('social_integrations.views.requests.post')
    def test_opt_in_required_when_window_closed(self, mock_post):
        from social_integrations.models import WhatsAppOutboundConsentLog
        resp = self.api_post(self.url, self._payload(opt_in_confirmed=False), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()['error_code'], 'OPT_IN_REQUIRED')
        mock_post.assert_not_called()
        self.assertTrue(
            WhatsAppOutboundConsentLog.objects.filter(
                outcome='blocked', error_code='OPT_IN_REQUIRED',
            ).exists()
        )

    @patch('social_integrations.consumers.send_new_message_notification', new_callable=AsyncMock)
    @patch('social_integrations.views.requests.post')
    def test_opt_in_not_required_when_window_open(self, mock_post, mock_notify):
        # An inbound message within 24h opens the window → no opt-in ack needed.
        self.create_wa_message(
            business_account=self.account, from_number='15551234567', is_from_business=False,
        )
        mock_post.return_value = _graph_ok()
        resp = self.api_post(self.url, self._payload(opt_in_confirmed=False), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    @patch('social_integrations.views.requests.post')
    def test_non_approved_template_blocked(self, mock_post):
        self.template.status = 'PENDING'
        self.template.save()
        resp = self.api_post(self.url, self._payload(), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()['error_code'], 'TEMPLATE_NOT_APPROVED')
        mock_post.assert_not_called()

    @patch('social_integrations.views.requests.post')
    def test_us_marketing_template_blocked(self, mock_post):
        self.template.category = 'MARKETING'
        self.template.save()
        resp = self.api_post(self.url, self._payload(to_number='+15551234567'), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()['error_code'], 'US_MARKETING_PAUSED')
        mock_post.assert_not_called()

    @patch('social_integrations.views.requests.post')
    def test_graph_error_mapped_to_stable_code(self, mock_post):
        from social_integrations.models import WhatsAppMessage, WhatsAppOutboundConsentLog
        mock_post.return_value = _graph_error(code=131026, http=400)
        resp = self.api_post(self.url, self._payload(), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()['error_code'], 'INVALID_WHATSAPP_NUMBER')
        self.assertFalse(WhatsAppMessage.objects.filter(message_type='template').exists())
        self.assertTrue(
            WhatsAppOutboundConsentLog.objects.filter(
                outcome='failed', error_code='INVALID_WHATSAPP_NUMBER',
            ).exists()
        )

    @patch('social_integrations.consumers.send_new_message_notification', new_callable=AsyncMock)
    @patch('social_integrations.views.requests.post')
    def test_audit_row_written_on_success(self, mock_post, mock_notify):
        from social_integrations.models import WhatsAppOutboundConsentLog
        mock_post.return_value = _graph_ok()
        resp = self.api_post(self.url, self._payload(), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        log = WhatsAppOutboundConsentLog.objects.get(outcome='sent')
        self.assertEqual(log.agent, self.agent)
        self.assertEqual(log.to_number, '+15551234567')
        self.assertTrue(log.opt_in_confirmed)
        self.assertEqual(log.template, self.template)

    def test_unauthenticated_denied(self):
        resp = self.client.post(
            self.url, self._payload(), content_type='application/json', HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_no_feature_denied(self):
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_post(self.url, self._payload(), user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
