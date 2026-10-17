"""
Signed-in devices: GET /api/auth/devices/, DELETE /api/auth/devices/{id}/,
POST /api/auth/devices/revoke-others/.
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from users.models import AuthDeviceToken
from users.tests.conftest import EchoDeskTenantTestCase
from users.views import device_session_id

DEVICES_URL = '/api/auth/devices/'
REVOKE_OTHERS_URL = '/api/auth/devices/revoke-others/'


class TestDeviceSessions(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user = self.create_user(email='devices@test.com')
        self.other = self.create_user(email='devices-other@test.com')
        self.phone = AuthDeviceToken.issue(self.user, 'ios', device_id='a', device_name='iPhone 15')
        self.tablet = AuthDeviceToken.issue(self.user, 'android', device_id='b', device_name='Pixel')
        self.web = Token.objects.create(user=self.user)
        self.foreign = AuthDeviceToken.issue(self.other, 'ios', device_id='c', device_name='Not mine')
        # phone used most recently
        now = timezone.now()
        AuthDeviceToken.objects.filter(key=self.tablet.key).update(last_used_at=now - timedelta(days=1))
        AuthDeviceToken.objects.filter(key=self.phone.key).update(last_used_at=now)

    def client_for(self, key):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {key}')
        return client

    def get(self, key, url=DEVICES_URL):
        return self.client_for(key).get(url, HTTP_HOST='tenant.test.com')

    def delete(self, key, url):
        return self.client_for(key).delete(url, HTTP_HOST='tenant.test.com')

    def post(self, key, url, data=None):
        return self.client_for(key).post(url, data or {}, format='json', HTTP_HOST='tenant.test.com')

    def test_list_own_devices_only(self):
        resp = self.get(self.phone.key)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([d['device_name'] for d in resp.data], ['iPhone 15', 'Pixel'])
        current = [d for d in resp.data if d['is_current']]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['id'], device_session_id(self.phone.key))
        self.assertEqual(set(resp.data[0].keys()),
                         {'id', 'platform', 'device_name', 'created_at', 'last_used_at', 'is_current'})
        self.assertEqual(resp.data[0]['platform'], 'ios')

    def test_list_never_leaks_token_keys(self):
        resp = self.get(self.phone.key)
        body = resp.content.decode()
        for key in (self.phone.key, self.tablet.key, self.web.key, self.foreign.key):
            self.assertNotIn(key, body)
        self.assertNotIn('Not mine', body)

    def test_list_from_web_token_has_no_current(self):
        resp = self.get(self.web.key)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(any(d['is_current'] for d in resp.data))

    def test_unauthenticated(self):
        resp = self.client.get(DEVICES_URL, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_revoke_one_device(self):
        url = f'{DEVICES_URL}{device_session_id(self.tablet.key)}/'
        resp = self.delete(self.phone.key, url)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(AuthDeviceToken.objects.filter(key=self.tablet.key).exists())
        self.assertTrue(AuthDeviceToken.objects.filter(key=self.phone.key).exists())
        # the revoked token no longer authenticates
        self.assertEqual(self.get(self.tablet.key).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cannot_revoke_someone_elses_device(self):
        url = f'{DEVICES_URL}{device_session_id(self.foreign.key)}/'
        resp = self.delete(self.phone.key, url)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(AuthDeviceToken.objects.filter(key=self.foreign.key).exists())

    def test_revoke_unknown_id(self):
        resp = self.delete(self.phone.key, f'{DEVICES_URL}deadbeefdeadbeef/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_revoke_current_device_signs_it_out(self):
        url = f'{DEVICES_URL}{device_session_id(self.phone.key)}/'
        resp = self.delete(self.phone.key, url)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.get(self.phone.key).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_revoke_others_from_phone(self):
        resp = self.post(self.phone.key, REVOKE_OTHERS_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, {'revoked': 1, 'web_session_revoked': True})
        self.assertEqual(list(AuthDeviceToken.objects.filter(user=self.user)), [self.phone])
        self.assertFalse(Token.objects.filter(user=self.user).exists())
        self.assertTrue(AuthDeviceToken.objects.filter(key=self.foreign.key).exists())
        self.assertEqual(self.get(self.phone.key).status_code, status.HTTP_200_OK)

    def test_revoke_others_keeping_web(self):
        resp = self.post(self.phone.key, REVOKE_OTHERS_URL, {'include_web': False})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, {'revoked': 1, 'web_session_revoked': False})
        self.assertTrue(Token.objects.filter(user=self.user).exists())

    def test_revoke_others_from_web_keeps_web(self):
        resp = self.post(self.web.key, REVOKE_OTHERS_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, {'revoked': 2, 'web_session_revoked': False})
        self.assertFalse(AuthDeviceToken.objects.filter(user=self.user).exists())
        self.assertTrue(Token.objects.filter(key=self.web.key).exists())
