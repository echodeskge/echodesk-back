"""
Tests for notifications/utils.py — push notification delivery:
- send_notification_to_user(): with active PushSubscription creates NotificationLog with status='sent'
- With no subscriptions: returns 0
- With inactive subscription: skips it
- Invalid subscription (410 response): marks subscription as inactive
- Failed delivery: NotificationLog status='failed' with error_message
"""
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase
from notifications.models import PushSubscription, NotificationLog
from notifications.utils import (
    send_push_notification,
    send_notification_to_user,
    get_vapid_keys,
)

User = get_user_model()

MOCK_VAPID = {
    'private_key': 'test-private-key',
    'public_key': 'test-public-key',
    'admin_email': 'mailto:test@echodesk.ge',
}


class TestSendNotificationToUser(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user = self.create_user(email='push-user@test.com')

    def _make_subscription(self, is_active=True, endpoint_suffix='default'):
        return PushSubscription.objects.create(
            user=self.user,
            endpoint=f'https://push.example.com/{endpoint_suffix}/{PushSubscription.objects.count()}',
            p256dh='test-p256dh',
            auth='test-auth',
            is_active=is_active,
        )

    @patch('notifications.utils.get_vapid_keys', return_value=MOCK_VAPID)
    @patch('notifications.utils.webpush')
    def test_send_with_active_subscription_creates_sent_log(self, mock_wp, mock_keys):
        mock_wp.return_value = MagicMock(status_code=201)
        sub = self._make_subscription()

        count = send_notification_to_user(
            user=self.user,
            title='Test Push',
            body='Push body text',
        )

        self.assertEqual(count, 1)
        log = NotificationLog.objects.filter(user=self.user).latest('created_at')
        self.assertEqual(log.status, 'sent')
        self.assertEqual(log.title, 'Test Push')
        mock_wp.assert_called_once()

    def test_no_subscriptions_returns_zero(self):
        count = send_notification_to_user(
            user=self.user,
            title='No Subs',
            body='Nobody listening',
        )
        self.assertEqual(count, 0)

    @patch('notifications.utils.get_vapid_keys', return_value=MOCK_VAPID)
    @patch('notifications.utils.webpush')
    def test_inactive_subscription_skipped(self, mock_wp, mock_keys):
        self._make_subscription(is_active=False)

        count = send_notification_to_user(
            user=self.user,
            title='Skip',
            body='Inactive',
        )

        self.assertEqual(count, 0)
        mock_wp.assert_not_called()


class TestSendPushNotification(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user = self.create_user(email='push-single@test.com')
        self.sub = PushSubscription.objects.create(
            user=self.user,
            endpoint='https://push.example.com/single',
            p256dh='test-p256dh',
            auth='test-auth',
            is_active=True,
        )

    @patch('notifications.utils.get_vapid_keys', return_value=MOCK_VAPID)
    @patch('notifications.utils.webpush')
    def test_successful_send(self, mock_wp, mock_keys):
        mock_wp.return_value = MagicMock(status_code=201)

        result = send_push_notification(
            subscription=self.sub,
            title='Hello',
            body='World',
        )

        self.assertTrue(result)
        log = NotificationLog.objects.filter(subscription=self.sub).latest('created_at')
        self.assertEqual(log.status, 'sent')

    @patch('notifications.utils.get_vapid_keys', return_value=MOCK_VAPID)
    @patch('notifications.utils.webpush')
    def test_410_response_marks_subscription_inactive(self, mock_wp, mock_keys):
        from pywebpush import WebPushException

        mock_response = MagicMock()
        mock_response.status_code = 410
        mock_wp.side_effect = WebPushException(
            'Gone', response=mock_response
        )

        result = send_push_notification(
            subscription=self.sub,
            title='Gone',
            body='Subscription expired',
        )

        self.assertFalse(result)
        self.sub.refresh_from_db()
        self.assertFalse(self.sub.is_active)

        log = NotificationLog.objects.filter(subscription=self.sub).latest('created_at')
        self.assertEqual(log.status, 'failed')
        self.assertIn('Gone', log.error_message)

    @patch('notifications.utils.get_vapid_keys', return_value=MOCK_VAPID)
    @patch('notifications.utils.webpush')
    def test_410_with_real_response_marks_subscription_inactive(self, mock_wp, mock_keys):
        # A real requests.Response with a 4xx status is falsy (unlike the
        # MagicMock above), which is what pywebpush actually attaches.
        import requests
        from pywebpush import WebPushException

        response = requests.Response()
        response.status_code = 410
        self.assertFalse(bool(response))
        mock_wp.side_effect = WebPushException('Push failed: 410 Gone', response=response)

        result = send_push_notification(
            subscription=self.sub,
            title='Gone',
            body='Subscription expired',
        )

        self.assertFalse(result)
        self.sub.refresh_from_db()
        self.assertFalse(self.sub.is_active)

    @patch('notifications.utils.get_vapid_keys', return_value=MOCK_VAPID)
    @patch('notifications.utils.webpush')
    def test_generic_failure_logs_error(self, mock_wp, mock_keys):
        mock_wp.side_effect = Exception('Connection refused')

        result = send_push_notification(
            subscription=self.sub,
            title='Fail',
            body='Error test',
        )

        self.assertFalse(result)
        log = NotificationLog.objects.filter(subscription=self.sub).latest('created_at')
        self.assertEqual(log.status, 'failed')
        self.assertIn('Connection refused', log.error_message)

    @patch('notifications.utils.get_vapid_keys', return_value=MOCK_VAPID)
    @patch('notifications.utils.webpush')
    def test_404_response_marks_subscription_inactive(self, mock_wp, mock_keys):
        from pywebpush import WebPushException

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_wp.side_effect = WebPushException(
            'Not Found', response=mock_response
        )

        result = send_push_notification(
            subscription=self.sub,
            title='Missing',
            body='Endpoint gone',
        )

        self.assertFalse(result)
        self.sub.refresh_from_db()
        self.assertFalse(self.sub.is_active)

    def test_inactive_subscription_returns_false(self):
        self.sub.is_active = False
        self.sub.save()

        result = send_push_notification(
            subscription=self.sub,
            title='Skip',
            body='Should not send',
        )

        self.assertFalse(result)

    @patch('notifications.utils.get_vapid_keys', return_value=None)
    def test_no_vapid_keys_returns_false(self, mock_keys):
        result = send_push_notification(
            subscription=self.sub,
            title='No VAPID',
            body='Keys missing',
        )

        self.assertFalse(result)

    @patch('notifications.utils.get_vapid_keys', return_value=MOCK_VAPID)
    @patch('notifications.utils.webpush')
    def test_url_and_tag_passed_in_data(self, mock_wp, mock_keys):
        mock_wp.return_value = MagicMock(status_code=201)

        send_push_notification(
            subscription=self.sub,
            title='Nav',
            body='Click me',
            url='/tickets/123',
            tag='echodesk-123',
        )

        # Verify webpush was called with correct data
        call_args = mock_wp.call_args
        import json
        sent_data = json.loads(call_args.kwargs.get('data') or call_args[1].get('data', '{}'))
        self.assertEqual(sent_data.get('tag'), 'echodesk-123')
        self.assertEqual(sent_data['data'].get('url'), '/tickets/123')

    @patch('notifications.utils.get_vapid_keys', return_value=MOCK_VAPID)
    @patch('notifications.utils.webpush')
    def test_notification_log_records_data(self, mock_wp, mock_keys):
        mock_wp.return_value = MagicMock(status_code=201)

        send_push_notification(
            subscription=self.sub,
            title='Log Data',
            body='Check log',
            data={'ticket_id': 42, 'notification_type': 'ticket_assigned'},
        )

        log = NotificationLog.objects.filter(subscription=self.sub).latest('created_at')
        self.assertEqual(log.title, 'Log Data')
        self.assertEqual(log.body, 'Check log')
        self.assertEqual(log.data.get('ticket_id'), 42)
