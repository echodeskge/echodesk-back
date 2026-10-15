"""
Tests for the Expo device-token endpoints (native mobile app):
- POST   /notifications/devices/  register (201) / re-register updates (200)
- POST   token re-points to the current user when another user owned it
- POST   invalid token format / platform -> 400
- DELETE /notifications/devices/  by body or ?token= (200), foreign/unknown -> 404
- GET    /notifications/devices/  caller's active devices only
- Unauthenticated access denied
"""
from django.contrib.auth import get_user_model
from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase
from notifications.models import DevicePushToken

User = get_user_model()

DEVICES_URL = '/notifications/devices/'
TOKEN_A = 'ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]'
TOKEN_B = 'ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]'


class TestRegisterDevice(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user = self.create_user(email='device-reg@test.com')
        self.other = self.create_user(email='device-other@test.com')
        DevicePushToken.objects.all().delete()

    def test_register_creates_token(self):
        resp = self.api_post(DEVICES_URL, {
            'token': TOKEN_A,
            'platform': 'ios',
            'device_id': 'iphone-1',
            'device_name': 'iPhone 15',
            'app_version': '1.0.0',
        }, user=self.user)

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['token'], TOKEN_A)
        self.assertEqual(resp.data['platform'], 'ios')
        self.assertTrue(resp.data['is_active'])

        device = DevicePushToken.objects.get(token=TOKEN_A)
        self.assertEqual(device.user, self.user)
        self.assertEqual(device.device_id, 'iphone-1')
        self.assertEqual(device.device_name, 'iPhone 15')
        self.assertEqual(device.app_version, '1.0.0')

    def test_register_minimal_payload(self):
        resp = self.api_post(DEVICES_URL, {
            'token': TOKEN_A,
            'platform': 'android',
        }, user=self.user)

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        device = DevicePushToken.objects.get(token=TOKEN_A)
        self.assertEqual(device.device_id, '')
        self.assertEqual(device.app_version, '')

    def test_reregister_updates_existing_row(self):
        self.api_post(DEVICES_URL, {
            'token': TOKEN_A, 'platform': 'ios', 'app_version': '1.0.0',
        }, user=self.user)
        # Simulate the app unregistering, then registering again with a new version
        DevicePushToken.objects.filter(token=TOKEN_A).update(is_active=False)

        resp = self.api_post(DEVICES_URL, {
            'token': TOKEN_A, 'platform': 'ios', 'app_version': '1.1.0',
        }, user=self.user)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(DevicePushToken.objects.filter(token=TOKEN_A).count(), 1)
        device = DevicePushToken.objects.get(token=TOKEN_A)
        self.assertTrue(device.is_active)
        self.assertEqual(device.app_version, '1.1.0')

    def test_token_moves_between_users(self):
        self.api_post(DEVICES_URL, {'token': TOKEN_A, 'platform': 'ios'}, user=self.other)
        self.assertEqual(DevicePushToken.objects.get(token=TOKEN_A).user, self.other)

        resp = self.api_post(DEVICES_URL, {'token': TOKEN_A, 'platform': 'ios'}, user=self.user)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(DevicePushToken.objects.filter(token=TOKEN_A).count(), 1)
        self.assertEqual(DevicePushToken.objects.get(token=TOKEN_A).user, self.user)

    def test_legacy_expo_prefix_accepted(self):
        resp = self.api_post(DEVICES_URL, {
            'token': 'ExpoPushToken[legacy-token]', 'platform': 'android',
        }, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_invalid_token_format_rejected(self):
        for bad in ['not-a-token', 'ExponentPushToken[]', 'ExponentPushToken[abc', 'fcm:abc123', '']:
            resp = self.api_post(DEVICES_URL, {'token': bad, 'platform': 'ios'}, user=self.user)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, bad)
            self.assertIn('token', resp.data)
        self.assertEqual(DevicePushToken.objects.count(), 0)

    def test_invalid_platform_rejected(self):
        resp = self.api_post(DEVICES_URL, {'token': TOKEN_A, 'platform': 'web'}, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('platform', resp.data)

    def test_missing_platform_rejected(self):
        resp = self.api_post(DEVICES_URL, {'token': TOKEN_A}, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_denied(self):
        resp = self.api_post(DEVICES_URL, {'token': TOKEN_A, 'platform': 'ios'})
        self.assertIn(resp.status_code, [401, 403])


class TestUnregisterDevice(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user = self.create_user(email='device-del@test.com')
        self.other = self.create_user(email='device-del-other@test.com')
        DevicePushToken.objects.all().delete()
        self.device = DevicePushToken.objects.create(
            user=self.user, token=TOKEN_A, platform='ios',
        )
        self.foreign = DevicePushToken.objects.create(
            user=self.other, token=TOKEN_B, platform='android',
        )

    def test_delete_by_body(self):
        client = self.authenticated_client(self.user)
        resp = client.delete(
            DEVICES_URL, {'token': TOKEN_A}, format='json', HTTP_HOST='tenant.test.com',
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.device.refresh_from_db()
        self.assertFalse(self.device.is_active)

    def test_delete_by_query_param(self):
        resp = self.api_delete(f'{DEVICES_URL}?token={TOKEN_A}', user=self.user)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.device.refresh_from_db()
        self.assertFalse(self.device.is_active)

    def test_delete_foreign_token_404(self):
        resp = self.api_delete(f'{DEVICES_URL}?token={TOKEN_B}', user=self.user)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.foreign.refresh_from_db()
        self.assertTrue(self.foreign.is_active)

    def test_delete_unknown_token_404(self):
        resp = self.api_delete(
            f'{DEVICES_URL}?token=ExponentPushToken[nope]', user=self.user
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_without_token_400(self):
        resp = self.api_delete(DEVICES_URL, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_denied(self):
        resp = self.api_delete(f'{DEVICES_URL}?token={TOKEN_A}')
        self.assertIn(resp.status_code, [401, 403])


class TestListDevices(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user = self.create_user(email='device-list@test.com')
        self.other = self.create_user(email='device-list-other@test.com')
        DevicePushToken.objects.all().delete()
        DevicePushToken.objects.create(user=self.user, token=TOKEN_A, platform='ios')
        DevicePushToken.objects.create(
            user=self.user, token='ExponentPushToken[inactive]', platform='ios', is_active=False,
        )
        DevicePushToken.objects.create(user=self.other, token=TOKEN_B, platform='android')

    def test_lists_only_own_active_devices(self):
        resp = self.api_get(DEVICES_URL, user=self.user)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        tokens = [d['token'] for d in resp.data]
        self.assertEqual(tokens, [TOKEN_A])
        self.assertIn('platform', resp.data[0])
        self.assertIn('last_seen_at', resp.data[0])

    def test_empty_list(self):
        DevicePushToken.objects.filter(user=self.user).delete()
        resp = self.api_get(DEVICES_URL, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, [])

    def test_unauthenticated_denied(self):
        resp = self.api_get(DEVICES_URL)
        self.assertIn(resp.status_code, [401, 403])
