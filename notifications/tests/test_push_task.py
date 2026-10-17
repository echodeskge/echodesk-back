"""
Tests for notifications/tasks.py and the create_notification -> task hand-off:
- deliver_push_notification_task hits both channels (Web Push + Expo)
- Task tolerates a vanished user
- Expo network error propagates (so Celery can retry)
- create_notification enqueues the task with the expected data keys
- No push targets -> .delay not called
- push preference disabled -> .delay not called
- Broker failure on .delay -> task runs inline
"""
from unittest.mock import patch, MagicMock

import requests
from celery.exceptions import Retry
from django.contrib.auth import get_user_model
from django.test import override_settings

from users.tests.conftest import EchoDeskTenantTestCase
from users.models import Notification, NotificationPreference
from users.notification_utils import create_notification
from notifications.models import DevicePushToken, NotificationLog, PushSubscription
from notifications.tasks import deliver_push_notification_task

User = get_user_model()

MOCK_VAPID = {
    'private_key': 'test-private-key',
    'public_key': 'test-public-key',
    'admin_email': 'mailto:test@echodesk.ge',
}

# WebSocket broadcast is imported inside create_notification; stub at source.
_ws_patch = patch('asgiref.sync.async_to_sync', return_value=lambda **kw: None)


def _expo_ok(count=1):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {'data': [{'status': 'ok'} for _ in range(count)]}
    return resp


class TestDeliverPushNotificationTask(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        # Class-level override_settings doesn't apply: the tenant base class
        # overrides setUpClass without chaining to SimpleTestCase. Enable per test.
        self._expo_settings = override_settings(
            EXPO_PUSH_URL='https://expo.test/push', EXPO_ACCESS_TOKEN='',
        )
        self._expo_settings.enable()
        self.user = self.create_user(email='push-task@test.com')
        DevicePushToken.objects.all().delete()
        PushSubscription.objects.all().delete()
        NotificationLog.objects.all().delete()
        self.sub = PushSubscription.objects.create(
            user=self.user,
            endpoint='https://push.example.com/task',
            p256dh='p', auth='a',
        )
        self.device = DevicePushToken.objects.create(
            user=self.user, token='ExponentPushToken[task]', platform='ios',
        )

    def tearDown(self):
        self._expo_settings.disable()
        super().tearDown()

    @patch('notifications.expo.requests.post')
    @patch('notifications.utils.get_vapid_keys', return_value=MOCK_VAPID)
    @patch('notifications.utils.webpush')
    def test_task_hits_both_channels(self, mock_wp, mock_keys, mock_post):
        mock_wp.return_value = MagicMock(status_code=201)
        mock_post.return_value = _expo_ok(1)

        result = deliver_push_notification_task.apply(kwargs=dict(
            tenant_schema=self.tenant.schema_name,
            user_id=self.user.id,
            title='Hi', body='There',
            data={'notification_id': 7, 'url': '/tickets/7'},
            url='/tickets/7', tag='echodesk-7', badge=2,
        ))

        self.assertEqual(result.get(), {'web': 1, 'expo': 1})
        mock_wp.assert_called_once()
        mock_post.assert_called_once()
        expo_message = mock_post.call_args.kwargs['json'][0]
        self.assertEqual(expo_message['to'], self.device.token)
        self.assertEqual(expo_message['badge'], 2)
        self.assertEqual(expo_message['data']['notification_id'], 7)

        self.assertEqual(NotificationLog.objects.filter(subscription=self.sub, status='sent').count(), 1)
        self.assertEqual(NotificationLog.objects.filter(device_token=self.device, status='sent').count(), 1)

    @patch('notifications.expo.requests.post')
    @patch('notifications.utils.webpush')
    def test_missing_user_is_noop(self, mock_wp, mock_post):
        result = deliver_push_notification_task.apply(kwargs=dict(
            tenant_schema=self.tenant.schema_name,
            user_id=999999,
            title='Hi', body='There',
        ))

        self.assertEqual(result.get(), {'web': 0, 'expo': 0})
        mock_wp.assert_not_called()
        mock_post.assert_not_called()

    @patch('notifications.expo.requests.post')
    @patch('notifications.utils.get_vapid_keys', return_value=MOCK_VAPID)
    @patch('notifications.utils.webpush')
    def test_expo_network_error_triggers_retry(self, mock_wp, mock_keys, mock_post):
        mock_wp.return_value = MagicMock(status_code=201)
        mock_post.side_effect = requests.ConnectionError('expo down')

        # In eager mode autoretry re-runs the task inline and finally surfaces
        # either Celery's Retry marker or the original RequestException.
        with self.assertRaises((requests.RequestException, Retry)):
            deliver_push_notification_task.apply(
                kwargs=dict(
                    tenant_schema=self.tenant.schema_name,
                    user_id=self.user.id,
                    title='Hi', body='There',
                ),
                throw=True,
            )

        # Expo was attempted (at least once) and every attempt logged a failure
        self.assertGreaterEqual(mock_post.call_count, 1)
        self.assertEqual(
            NotificationLog.objects.filter(device_token=self.device, status='failed').count(),
            mock_post.call_count,
        )
        # Web push went out exactly once — retries must not re-send browser pushes
        mock_wp.assert_called_once()
        self.assertEqual(NotificationLog.objects.filter(subscription=self.sub, status='sent').count(), 1)

    def test_task_declares_retry_policy(self):
        self.assertIn(requests.RequestException, deliver_push_notification_task.autoretry_for)
        self.assertEqual(deliver_push_notification_task.retry_kwargs['max_retries'], 3)
        self.assertTrue(deliver_push_notification_task.retry_backoff)


class TestCreateNotificationDispatch(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user = self.create_user(email='push-dispatch@test.com')
        NotificationPreference.objects.filter(user=self.user).delete()
        Notification.objects.filter(user=self.user).delete()
        DevicePushToken.objects.all().delete()
        PushSubscription.objects.all().delete()

    def tearDown(self):
        Notification.objects.filter(user=self.user).delete()
        super().tearDown()

    @_ws_patch
    @patch('notifications.tasks.deliver_push_notification_task.delay')
    def test_enqueues_with_expected_data_keys(self, mock_delay, mock_ws):
        DevicePushToken.objects.create(
            user=self.user, token='ExponentPushToken[dispatch]', platform='android',
        )

        create_notification(
            user=self.user,
            notification_type='leave_request_submitted',
            title='Leave request',
            message='Someone asked for leave',
            metadata={'leave_request_id': 42},
            link_url='/leave/team-requests?id=42',
        )

        notif = Notification.objects.get(user=self.user, title='Leave request')
        mock_delay.assert_called_once()
        kwargs = mock_delay.call_args.kwargs
        self.assertEqual(kwargs['tenant_schema'], self.tenant.schema_name)
        self.assertEqual(kwargs['user_id'], self.user.id)
        self.assertEqual(kwargs['title'], 'Leave request')
        self.assertEqual(kwargs['body'], 'Someone asked for leave')
        self.assertEqual(kwargs['url'], '/leave/team-requests?id=42')
        self.assertEqual(kwargs['tag'], f'echodesk-{notif.id}')
        self.assertIsInstance(kwargs['badge'], int)
        self.assertGreaterEqual(kwargs['badge'], 1)

        data = kwargs['data']
        self.assertEqual(set(data.keys()), {
            'notification_id', 'notification_type', 'link_url', 'url',
            'ticket_id', 'leave_request_id', 'conversation_id', 'sender_id',
            'call_log_id',
        })
        # not a chat notification: the chat deep-link keys are present but empty
        self.assertIsNone(data['conversation_id'])
        self.assertIsNone(data['sender_id'])
        self.assertEqual(data['notification_id'], notif.id)
        self.assertEqual(data['notification_type'], 'leave_request_submitted')
        self.assertEqual(data['link_url'], '/leave/team-requests?id=42')
        self.assertEqual(data['url'], '/leave/team-requests?id=42')
        self.assertIsNone(data['ticket_id'])
        self.assertEqual(data['leave_request_id'], 42)

    @_ws_patch
    @patch('notifications.tasks.deliver_push_notification_task.delay')
    def test_ticket_notification_defaults_url(self, mock_delay, mock_ws):
        PushSubscription.objects.create(
            user=self.user, endpoint='https://push.example.com/d1', p256dh='p', auth='a',
        )

        create_notification(
            user=self.user,
            notification_type='ticket_assigned',
            title='Assigned',
            message='Ticket #9',
            ticket_id=9,
        )

        kwargs = mock_delay.call_args.kwargs
        self.assertEqual(kwargs['url'], '/tickets/9')
        self.assertEqual(kwargs['tag'], 'echodesk-9')
        self.assertEqual(kwargs['data']['ticket_id'], 9)
        self.assertEqual(kwargs['data']['link_url'], '')
        self.assertIsNone(kwargs['data']['leave_request_id'])

    @_ws_patch
    @patch('notifications.tasks.deliver_push_notification_task.delay')
    def test_no_targets_skips_enqueue(self, mock_delay, mock_ws):
        # Only inactive targets exist
        DevicePushToken.objects.create(
            user=self.user, token='ExponentPushToken[dead]', platform='ios', is_active=False,
        )
        PushSubscription.objects.create(
            user=self.user, endpoint='https://push.example.com/dead', p256dh='p', auth='a',
            is_active=False,
        )

        create_notification(
            user=self.user,
            notification_type='ticket_assigned',
            title='Quiet',
            message='No devices',
        )

        self.assertTrue(Notification.objects.filter(user=self.user, title='Quiet').exists())
        mock_delay.assert_not_called()

    @_ws_patch
    @patch('notifications.tasks.deliver_push_notification_task.delay')
    def test_push_preference_disabled_skips_enqueue(self, mock_delay, mock_ws):
        DevicePushToken.objects.create(
            user=self.user, token='ExponentPushToken[pref]', platform='ios',
        )
        NotificationPreference.objects.create(
            user=self.user, notification_type='ticket_assigned',
            in_app=True, sound=True, push=False,
        )

        create_notification(
            user=self.user,
            notification_type='ticket_assigned',
            title='Pref off',
            message='No push wanted',
        )

        self.assertTrue(Notification.objects.filter(user=self.user, title='Pref off').exists())
        mock_delay.assert_not_called()

    @_ws_patch
    @patch('notifications.tasks.deliver_push_notification_task.apply')
    @patch('notifications.tasks.deliver_push_notification_task.delay')
    def test_broker_failure_runs_inline(self, mock_delay, mock_apply, mock_ws):
        DevicePushToken.objects.create(
            user=self.user, token='ExponentPushToken[inline]', platform='ios',
        )
        mock_delay.side_effect = ConnectionError('redis down')

        # The suite runs eager; the inline fallback only applies to a real broker.
        with override_settings(CELERY_TASK_ALWAYS_EAGER=False):
            create_notification(
                user=self.user,
                notification_type='ticket_assigned',
                title='Inline',
                message='Broker is down',
            )

        mock_delay.assert_called_once()
        mock_apply.assert_called_once()
        self.assertEqual(
            mock_apply.call_args.kwargs['kwargs']['title'], 'Inline'
        )
        self.assertEqual(
            mock_apply.call_args.kwargs['kwargs'],
            mock_delay.call_args.kwargs,
        )

    @_ws_patch
    @patch('notifications.tasks.deliver_push_notification_task.apply')
    @patch('notifications.tasks.deliver_push_notification_task.delay')
    def test_eager_failure_does_not_rerun_inline(self, mock_delay, mock_apply, mock_ws):
        DevicePushToken.objects.create(
            user=self.user, token='ExponentPushToken[eager]', platform='ios',
        )
        mock_delay.side_effect = requests.ConnectionError('expo down (propagated)')

        with override_settings(CELERY_TASK_ALWAYS_EAGER=True):
            create_notification(
                user=self.user,
                notification_type='ticket_assigned',
                title='Eager',
                message='Task already ran inline',
            )

        mock_delay.assert_called_once()
        mock_apply.assert_not_called()
        self.assertTrue(Notification.objects.filter(user=self.user, title='Eager').exists())
