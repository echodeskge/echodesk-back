"""
Tests for notifications/expo.py — Expo push delivery:
- send_expo_push(): active token -> one POST, NotificationLog status='sent'
- No active tokens -> 0 sent, no HTTP call
- DeviceNotRegistered ticket -> token deactivated, log failed
- Other Expo error -> log failed, token stays active
- 101 tokens -> two POSTs (batches of 100)
- Network error -> logs failed + raises (for Celery retry)
- Authorization header only when EXPO_ACCESS_TOKEN is set
- Message shape (badge, data, sound)
"""
from unittest.mock import patch, MagicMock

import requests
from django.contrib.auth import get_user_model
from django.test import override_settings

from users.tests.conftest import EchoDeskTenantTestCase
from notifications.expo import send_expo_push, is_valid_expo_token, EXPO_BATCH_SIZE
from notifications.models import DevicePushToken, NotificationLog

User = get_user_model()


def _ok_response(count):
    """Build a mocked Expo response with `count` ok tickets."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {'data': [{'status': 'ok', 'id': f'ticket-{i}'} for i in range(count)]}
    resp.raise_for_status.return_value = None
    return resp


def _ticket_response(tickets):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {'data': tickets}
    resp.raise_for_status.return_value = None
    return resp


class TestTokenRegex(EchoDeskTenantTestCase):

    def test_valid_and_invalid_tokens(self):
        self.assertTrue(is_valid_expo_token('ExponentPushToken[abc123]'))
        self.assertTrue(is_valid_expo_token('ExpoPushToken[abc123]'))
        self.assertFalse(is_valid_expo_token('ExponentPushToken[]'))
        self.assertFalse(is_valid_expo_token('ExponentPushToken[abc'))
        self.assertFalse(is_valid_expo_token('abc'))
        self.assertFalse(is_valid_expo_token(''))
        self.assertFalse(is_valid_expo_token(None))


class TestSendExpoPush(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        # Class-level override_settings doesn't apply: the tenant base class
        # overrides setUpClass without chaining to SimpleTestCase. Enable per test.
        self._expo_settings = override_settings(
            EXPO_PUSH_URL='https://expo.test/push', EXPO_ACCESS_TOKEN='', EXPO_PUSH_TIMEOUT=5,
        )
        self._expo_settings.enable()
        self.user = self.create_user(email='expo-user@test.com')
        DevicePushToken.objects.all().delete()
        NotificationLog.objects.all().delete()

    def tearDown(self):
        self._expo_settings.disable()
        super().tearDown()

    def _make_token(self, suffix='a', is_active=True):
        return DevicePushToken.objects.create(
            user=self.user,
            token=f'ExponentPushToken[{suffix}]',
            platform='ios',
            is_active=is_active,
        )

    @patch('notifications.expo.requests.post')
    def test_sends_to_active_token_and_logs_sent(self, mock_post):
        mock_post.return_value = _ok_response(1)
        device = self._make_token()

        sent = send_expo_push(
            self.user, title='Hello', body='World',
            data={'notification_id': 1, 'url': '/tickets/1'}, badge=3,
        )

        self.assertEqual(sent, 1)
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], 'https://expo.test/push')
        self.assertEqual(kwargs['timeout'], 5)
        messages = kwargs['json']
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['to'], device.token)
        self.assertEqual(messages[0]['title'], 'Hello')
        self.assertEqual(messages[0]['body'], 'World')
        self.assertEqual(messages[0]['badge'], 3)
        self.assertEqual(messages[0]['sound'], 'default')
        self.assertEqual(messages[0]['data']['url'], '/tickets/1')

        log = NotificationLog.objects.get(device_token=device)
        self.assertEqual(log.status, 'sent')
        self.assertEqual(log.user, self.user)
        self.assertEqual(log.title, 'Hello')
        self.assertEqual(log.data['notification_id'], 1)
        self.assertIsNone(log.subscription)

    @patch('notifications.expo.requests.post')
    def test_no_active_tokens_skips_http(self, mock_post):
        self._make_token(is_active=False)

        sent = send_expo_push(self.user, title='T', body='B')

        self.assertEqual(sent, 0)
        mock_post.assert_not_called()
        self.assertEqual(NotificationLog.objects.count(), 0)

    @patch('notifications.expo.requests.post')
    def test_device_not_registered_deactivates_token(self, mock_post):
        mock_post.return_value = _ticket_response([{
            'status': 'error',
            'message': 'The recipient device is not registered',
            'details': {'error': 'DeviceNotRegistered'},
        }])
        device = self._make_token()

        sent = send_expo_push(self.user, title='T', body='B')

        self.assertEqual(sent, 0)
        device.refresh_from_db()
        self.assertFalse(device.is_active)
        log = NotificationLog.objects.get(device_token=device)
        self.assertEqual(log.status, 'failed')
        self.assertIn('DeviceNotRegistered', log.error_message)

    @patch('notifications.expo.requests.post')
    def test_other_error_keeps_token_active(self, mock_post):
        mock_post.return_value = _ticket_response([{
            'status': 'error',
            'message': 'Too many messages',
            'details': {'error': 'MessageRateExceeded'},
        }])
        device = self._make_token()

        send_expo_push(self.user, title='T', body='B')

        device.refresh_from_db()
        self.assertTrue(device.is_active)
        log = NotificationLog.objects.get(device_token=device)
        self.assertEqual(log.status, 'failed')
        self.assertIn('MessageRateExceeded', log.error_message)

    @patch('notifications.expo.requests.post')
    def test_batches_of_100(self, mock_post):
        total = EXPO_BATCH_SIZE + 1
        for i in range(total):
            self._make_token(suffix=f'tok{i:03d}')
        mock_post.side_effect = [_ok_response(EXPO_BATCH_SIZE), _ok_response(1)]

        sent = send_expo_push(self.user, title='T', body='B')

        self.assertEqual(sent, total)
        self.assertEqual(mock_post.call_count, 2)
        first_batch = mock_post.call_args_list[0].kwargs['json']
        second_batch = mock_post.call_args_list[1].kwargs['json']
        self.assertEqual(len(first_batch), EXPO_BATCH_SIZE)
        self.assertEqual(len(second_batch), 1)
        self.assertEqual(
            NotificationLog.objects.filter(user=self.user, status='sent').count(), total
        )

    @patch('notifications.expo.requests.post')
    def test_network_error_logs_failed_and_raises(self, mock_post):
        mock_post.side_effect = requests.ConnectionError('boom')
        device = self._make_token()

        with self.assertRaises(requests.RequestException):
            send_expo_push(self.user, title='T', body='B')

        log = NotificationLog.objects.get(device_token=device)
        self.assertEqual(log.status, 'failed')
        self.assertIn('boom', log.error_message)
        device.refresh_from_db()
        self.assertTrue(device.is_active)

    @patch('notifications.expo.requests.post')
    def test_http_error_raises(self, mock_post):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError('502 Bad Gateway')
        mock_post.return_value = resp
        self._make_token()

        with self.assertRaises(requests.RequestException):
            send_expo_push(self.user, title='T', body='B')

    @patch('notifications.expo.requests.post')
    def test_malformed_response_raises(self, mock_post):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {'errors': [{'code': 'PUSH_TOO_MANY_EXPERIENCE_IDS'}]}
        mock_post.return_value = resp
        self._make_token()

        with self.assertRaises(requests.RequestException):
            send_expo_push(self.user, title='T', body='B')

    @patch('notifications.expo.requests.post')
    def test_no_auth_header_without_access_token(self, mock_post):
        mock_post.return_value = _ok_response(1)
        self._make_token()

        send_expo_push(self.user, title='T', body='B')

        headers = mock_post.call_args.kwargs['headers']
        self.assertNotIn('Authorization', headers)
        self.assertEqual(headers['Content-Type'], 'application/json')

    @override_settings(EXPO_ACCESS_TOKEN='secret-expo-token')
    @patch('notifications.expo.requests.post')
    def test_auth_header_with_access_token(self, mock_post):
        mock_post.return_value = _ok_response(1)
        self._make_token()

        send_expo_push(self.user, title='T', body='B')

        headers = mock_post.call_args.kwargs['headers']
        self.assertEqual(headers['Authorization'], 'Bearer secret-expo-token')

    @patch('notifications.expo.requests.post')
    def test_badge_omitted_when_none(self, mock_post):
        mock_post.return_value = _ok_response(1)
        self._make_token()

        send_expo_push(self.user, title='T', body='B', badge=None)

        message = mock_post.call_args.kwargs['json'][0]
        self.assertNotIn('badge', message)
        self.assertEqual(message['data'], {})

    @patch('notifications.expo.requests.post')
    def test_only_this_users_tokens(self, mock_post):
        mock_post.return_value = _ok_response(1)
        other = self.create_user(email='expo-other@test.com')
        DevicePushToken.objects.create(user=other, token='ExponentPushToken[other]', platform='android')
        mine = self._make_token()

        sent = send_expo_push(self.user, title='T', body='B')

        self.assertEqual(sent, 1)
        self.assertEqual(mock_post.call_args.kwargs['json'][0]['to'], mine.token)
