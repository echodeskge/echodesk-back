"""
Tests for per-device auth tokens (mobile apps):
- Login with platform=ios/android issues an AuthDeviceToken, web keeps DRF Token
- MultiTokenAuthentication accepts both kinds in the same header
- Logout / change password scope of invalidation
- WebSocket auth and IP-whitelist superuser bypass accept device tokens
"""
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from amanati_crm.websocket_auth import get_user_from_token
from tenants.ip_whitelist_middleware import IPWhitelistMiddleware
from users.models import AuthDeviceToken
from users.tests.conftest import EchoDeskTenantTestCase

User = get_user_model()

LOGIN_URL = '/api/auth/login/'
LOGOUT_URL = '/api/auth/logout/'
CHANGE_PASSWORD_URL = '/api/auth/change-password/'
FORCED_PASSWORD_CHANGE_URL = '/api/auth/forced-password-change/'
PROFILE_URL = '/api/auth/profile/'


def _mock_security(mock_security):
    mock_security.get_client_ip.return_value = '127.0.0.1'
    mock_security.is_ip_whitelisted.return_value = True
    mock_security.log_security_event.return_value = None


class DeviceAuthTestCase(EchoDeskTenantTestCase):
    """Shared helpers for device-token tests."""

    def setUp(self):
        super().setUp()
        self.user = self.create_user(
            email='device@test.com', password='correctpass123',
        )

    def login(self, **extra):
        """POST the login endpoint with the test user's credentials."""
        return self.api_post(LOGIN_URL, {
            'email': 'device@test.com',
            'password': 'correctpass123',
            **extra,
        })

    def token_client(self, key):
        """APIClient authenticating with ``Authorization: Token <key>``."""
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {key}')
        return client

    def get_profile(self, key):
        return self.token_client(key).get(PROFILE_URL, HTTP_HOST='tenant.test.com')


class TestLoginIssuesTokens(DeviceAuthTestCase):
    """Login response: shared token for web, device token for mobile."""

    @patch('tenants.views.SecurityService')
    def test_ios_login_returns_device_token(self, mock_security):
        _mock_security(mock_security)

        resp = self.login(platform='ios', device_id='iphone-1', device_name='iPhone 15')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['token_type'], 'device')

        device = AuthDeviceToken.objects.get(key=resp.data['token'])
        self.assertEqual(device.user, self.user)
        self.assertEqual(device.platform, 'ios')
        self.assertEqual(device.device_id, 'iphone-1')
        self.assertEqual(device.device_name, 'iPhone 15')
        self.assertFalse(Token.objects.filter(user=self.user).exists())

    @patch('tenants.views.SecurityService')
    def test_android_login_returns_device_token(self, mock_security):
        _mock_security(mock_security)

        resp = self.login(platform='android', device_id='pixel-1')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['token_type'], 'device')
        self.assertTrue(AuthDeviceToken.objects.filter(key=resp.data['token']).exists())

    @patch('tenants.views.SecurityService')
    def test_login_without_platform_returns_shared_token(self, mock_security):
        _mock_security(mock_security)

        resp = self.login()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['token_type'], 'shared')
        self.assertEqual(Token.objects.get(user=self.user).key, resp.data['token'])
        self.assertFalse(AuthDeviceToken.objects.filter(user=self.user).exists())

    @patch('tenants.views.SecurityService')
    def test_web_platform_returns_shared_token(self, mock_security):
        _mock_security(mock_security)

        resp = self.login(platform='web')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['token_type'], 'shared')
        self.assertFalse(AuthDeviceToken.objects.filter(user=self.user).exists())

    @patch('tenants.views.SecurityService')
    def test_unknown_platform_rejected(self, mock_security):
        _mock_security(mock_security)

        resp = self.login(platform='blackberry')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('tenants.views.SecurityService')
    def test_two_devices_get_independent_tokens(self, mock_security):
        _mock_security(mock_security)

        key_a = self.login(platform='ios', device_id='device-a').data['token']
        key_b = self.login(platform='android', device_id='device-b').data['token']
        self.assertNotEqual(key_a, key_b)
        self.assertEqual(AuthDeviceToken.objects.filter(user=self.user).count(), 2)

        for key in (key_a, key_b):
            resp = self.get_profile(key)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(resp.data['email'], 'device@test.com')

    @patch('tenants.views.SecurityService')
    def test_same_device_login_replaces_previous_token(self, mock_security):
        _mock_security(mock_security)

        first = self.login(platform='ios', device_id='same-device').data['token']
        second = self.login(platform='ios', device_id='same-device').data['token']
        self.assertNotEqual(first, second)
        self.assertEqual(AuthDeviceToken.objects.filter(user=self.user).count(), 1)

        self.assertEqual(self.get_profile(first).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.get_profile(second).status_code, status.HTTP_200_OK)

    @patch('tenants.views.SecurityService')
    def test_device_login_without_device_id_keeps_other_tokens(self, mock_security):
        _mock_security(mock_security)

        # No device_id → nothing to replace, each login adds a token
        self.login(platform='ios')
        self.login(platform='ios')
        self.assertEqual(AuthDeviceToken.objects.filter(user=self.user).count(), 2)

    def test_forced_password_change_issues_device_token(self):
        user = self.create_user(
            email='forced@test.com', password='temppass123',
            password_change_required=True, temporary_password='temppass123',
        )
        resp = self.api_post(FORCED_PASSWORD_CHANGE_URL, {
            'email': 'forced@test.com',
            'current_password': 'temppass123',
            'new_password': 'newstrongpass1',
            'platform': 'android',
            'device_id': 'pixel-2',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['token_type'], 'device')
        self.assertTrue(AuthDeviceToken.objects.filter(key=resp.data['token'], user=user).exists())
        self.assertFalse(Token.objects.filter(user=user).exists())

    def test_forced_password_change_without_platform_issues_shared_token(self):
        user = self.create_user(
            email='forced2@test.com', password='temppass123',
            password_change_required=True, temporary_password='temppass123',
        )
        resp = self.api_post(FORCED_PASSWORD_CHANGE_URL, {
            'email': 'forced2@test.com',
            'current_password': 'temppass123',
            'new_password': 'newstrongpass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['token_type'], 'shared')
        self.assertEqual(Token.objects.get(user=user).key, resp.data['token'])


class TestMultiTokenAuthentication(DeviceAuthTestCase):
    """Authorization: Token <key> resolves against both token tables."""

    def test_shared_token_still_authenticates(self):
        token = Token.objects.create(user=self.user)
        resp = self.get_profile(token.key)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_device_token_authenticates(self):
        device = AuthDeviceToken.issue(self.user, 'ios', device_id='d1')
        resp = self.get_profile(device.key)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['email'], 'device@test.com')

    def test_garbage_key_returns_401(self):
        resp = self.get_profile('0' * 40)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_inactive_user_device_token_returns_401(self):
        device = AuthDeviceToken.issue(self.user, 'ios', device_id='d1')
        self.user.is_active = False
        self.user.save()
        resp = self.get_profile(device.key)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_expired_device_token_returns_401(self):
        device = AuthDeviceToken.issue(self.user, 'ios', device_id='d1')
        device.expires_at = timezone.now() - timedelta(minutes=1)
        device.save(update_fields=['expires_at'])
        resp = self.get_profile(device.key)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_last_used_at_is_set_then_throttled(self):
        device = AuthDeviceToken.issue(self.user, 'ios', device_id='d1')
        self.assertIsNone(device.last_used_at)

        self.get_profile(device.key)
        device.refresh_from_db()
        first_seen = device.last_used_at
        self.assertIsNotNone(first_seen)

        # A request a minute later must not touch the row again
        self.get_profile(device.key)
        device.refresh_from_db()
        self.assertEqual(device.last_used_at, first_seen)

        # ...but one past the write interval does
        stale = timezone.now() - timedelta(minutes=10)
        AuthDeviceToken.objects.filter(key=device.key).update(last_used_at=stale)
        self.get_profile(device.key)
        device.refresh_from_db()
        self.assertGreater(device.last_used_at, stale)


class TestLogoutScope(DeviceAuthTestCase):
    """Logout invalidates only the token that was used."""

    def setUp(self):
        super().setUp()
        self.web_token = Token.objects.create(user=self.user)
        self.phone = AuthDeviceToken.issue(self.user, 'ios', device_id='phone')
        self.tablet = AuthDeviceToken.issue(self.user, 'android', device_id='tablet')

    @patch('tenants.views.SecurityService')
    def test_device_logout_deletes_only_that_device(self, mock_security):
        _mock_security(mock_security)

        resp = self.token_client(self.phone.key).post(LOGOUT_URL, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.assertFalse(AuthDeviceToken.objects.filter(key=self.phone.key).exists())
        self.assertTrue(AuthDeviceToken.objects.filter(key=self.tablet.key).exists())
        self.assertTrue(Token.objects.filter(key=self.web_token.key).exists())

        # The other sessions still work, the logged-out phone does not
        self.assertEqual(self.get_profile(self.phone.key).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.get_profile(self.tablet.key).status_code, status.HTTP_200_OK)
        self.assertEqual(self.get_profile(self.web_token.key).status_code, status.HTTP_200_OK)

    @patch('tenants.views.SecurityService')
    def test_web_logout_leaves_device_tokens(self, mock_security):
        _mock_security(mock_security)

        resp = self.token_client(self.web_token.key).post(LOGOUT_URL, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.assertFalse(Token.objects.filter(user=self.user).exists())
        self.assertEqual(AuthDeviceToken.objects.filter(user=self.user).count(), 2)
        self.assertEqual(self.get_profile(self.phone.key).status_code, status.HTTP_200_OK)


class TestChangePasswordPurgesDevices(DeviceAuthTestCase):
    """Changing the password logs out every web session and every device."""

    def test_change_password_deletes_device_tokens(self):
        Token.objects.create(user=self.user)
        AuthDeviceToken.issue(self.user, 'ios', device_id='phone')
        AuthDeviceToken.issue(self.user, 'android', device_id='tablet')

        resp = self.api_post(CHANGE_PASSWORD_URL, {
            'old_password': 'correctpass123',
            'new_password': 'newpass1234',
        }, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.assertFalse(Token.objects.filter(user=self.user).exists())
        self.assertFalse(AuthDeviceToken.objects.filter(user=self.user).exists())


class TestWebSocketAuth(DeviceAuthTestCase):
    """Channels middleware resolves device tokens in the tenant schema."""

    def resolve(self, key):
        # database_sync_to_async calls close_old_connections() around the
        # lookup, which would discard the test transaction's connection.
        with patch('channels.db.close_old_connections'):
            return async_to_sync(get_user_from_token)(key, self.tenant.schema_name)

    def test_device_token_resolves_user(self):
        device = AuthDeviceToken.issue(self.user, 'ios', device_id='phone')
        self.assertEqual(self.resolve(device.key), self.user)

    def test_shared_token_still_resolves_user(self):
        token = Token.objects.create(user=self.user)
        self.assertEqual(self.resolve(token.key), self.user)

    def test_unknown_key_is_anonymous(self):
        self.assertFalse(self.resolve('f' * 40).is_authenticated)

    def test_inactive_user_device_token_is_anonymous(self):
        device = AuthDeviceToken.issue(self.user, 'ios', device_id='phone')
        self.user.is_active = False
        self.user.save()
        self.assertFalse(self.resolve(device.key).is_authenticated)


class TestIPWhitelistSuperuserBypass(DeviceAuthTestCase):
    """The middleware's superuser bypass reads device tokens too."""

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.tenant.ip_whitelist_enabled = True
        self.tenant.superadmin_bypass_whitelist = True
        self.superuser = self.create_user(
            email='root@test.com', password='rootpass123', is_superuser=True,
        )

    def tearDown(self):
        # The tenant object is shared across test classes; restore it
        self.tenant.ip_whitelist_enabled = False
        self.tenant.superadmin_bypass_whitelist = False
        super().tearDown()

    def _run(self, key):
        request = self.factory.get(
            '/api/tickets/',
            HTTP_AUTHORIZATION=f'Token {key}',
            REMOTE_ADDR='203.0.113.9',
        )
        request.tenant = self.tenant
        middleware = IPWhitelistMiddleware(lambda req: 'passed')
        with patch.object(IPWhitelistMiddleware, '_is_excluded_path', return_value=False):
            return middleware(request)

    def test_superuser_device_token_bypasses_whitelist(self):
        device = AuthDeviceToken.issue(self.superuser, 'ios', device_id='root-phone')
        self.assertTrue(IPWhitelistMiddleware(lambda r: r)._check_superuser_from_token(
            self.factory.get('/', HTTP_AUTHORIZATION=f'Token {device.key}')
        ))
        self.assertEqual(self._run(device.key), 'passed')

    def test_regular_user_device_token_is_blocked(self):
        device = AuthDeviceToken.issue(self.user, 'ios', device_id='phone')
        response = self._run(device.key)
        self.assertEqual(response.status_code, 403)

    def test_superuser_shared_token_still_bypasses(self):
        token = Token.objects.create(user=self.superuser)
        self.assertEqual(self._run(token.key), 'passed')
