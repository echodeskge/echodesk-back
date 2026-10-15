"""
Tests for authentication endpoints:
- Login  (POST /api/auth/login/)
- Logout (POST /api/auth/logout/)
- Change password (POST /api/auth/change-password/)
- Forced password change (POST /api/auth/forced-password-change/)
- Profile (GET /api/auth/profile/)
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.authtoken.models import Token

from users.tests.conftest import EchoDeskTenantTestCase

User = get_user_model()

LOGIN_URL = '/api/auth/login/'
LOGOUT_URL = '/api/auth/logout/'
CHANGE_PASSWORD_URL = '/api/auth/change-password/'
FORCED_PASSWORD_CHANGE_URL = '/api/auth/forced-password-change/'
PROFILE_URL = '/api/auth/profile/'


class TestLogin(EchoDeskTenantTestCase):
    """Tests for tenant_login endpoint."""

    def setUp(self):
        super().setUp()
        self.user = self.create_user(
            email='login@test.com', password='correctpass123',
        )

    @patch('tenants.views.SecurityService')
    def test_login_valid_credentials_returns_token(self, mock_security):
        mock_security.get_client_ip.return_value = '127.0.0.1'
        mock_security.is_ip_whitelisted.return_value = True
        mock_security.log_security_event.return_value = None

        resp = self.api_post(LOGIN_URL, {
            'email': 'login@test.com',
            'password': 'correctpass123',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('token', resp.data)

    @patch('tenants.views.SecurityService')
    def test_login_invalid_password_returns_400(self, mock_security):
        mock_security.get_client_ip.return_value = '127.0.0.1'
        mock_security.is_ip_whitelisted.return_value = True
        mock_security.log_security_event.return_value = None

        resp = self.api_post(LOGIN_URL, {
            'email': 'login@test.com',
            'password': 'wrongpassword',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('tenants.views.SecurityService')
    def test_login_nonexistent_user_returns_400(self, mock_security):
        mock_security.get_client_ip.return_value = '127.0.0.1'
        mock_security.is_ip_whitelisted.return_value = True
        mock_security.log_security_event.return_value = None

        resp = self.api_post(LOGIN_URL, {
            'email': 'noone@test.com',
            'password': 'whatever',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('tenants.views.SecurityService')
    def test_login_inactive_user_returns_400(self, mock_security):
        mock_security.get_client_ip.return_value = '127.0.0.1'
        mock_security.is_ip_whitelisted.return_value = True
        mock_security.log_security_event.return_value = None

        self.user.is_active = False
        self.user.save()

        resp = self.api_post(LOGIN_URL, {
            'email': 'login@test.com',
            'password': 'correctpass123',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('tenants.views.SecurityService')
    def test_login_password_change_required_returns_403(self, mock_security):
        mock_security.get_client_ip.return_value = '127.0.0.1'
        mock_security.is_ip_whitelisted.return_value = True
        mock_security.log_security_event.return_value = None

        self.user.password_change_required = True
        self.user.save()

        resp = self.api_post(LOGIN_URL, {
            'email': 'login@test.com',
            'password': 'correctpass123',
        })
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(resp.data.get('password_change_required'))


class TestLogout(EchoDeskTenantTestCase):
    """Tests for tenant_logout endpoint."""

    def setUp(self):
        super().setUp()
        self.user = self.create_user(email='logout@test.com', password='pass123')
        self.token = Token.objects.create(user=self.user)

    @patch('tenants.views.SecurityService')
    def test_logout_deletes_token(self, mock_security):
        mock_security.log_security_event.return_value = None

        client = self.authenticated_client(self.user)
        resp = client.post(LOGOUT_URL, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(Token.objects.filter(user=self.user).exists())

    def test_logout_unauthenticated_returns_401(self):
        resp = self.api_post(LOGOUT_URL)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class TestChangePassword(EchoDeskTenantTestCase):
    """Tests for change_tenant_password endpoint."""

    def setUp(self):
        super().setUp()
        self.user = self.create_user(
            email='chpw@test.com', password='oldpass1234'
        )

    def test_change_password_success(self):
        resp = self.api_post(CHANGE_PASSWORD_URL, {
            'old_password': 'oldpass1234',
            'new_password': 'newpass1234',
        }, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('newpass1234'))

    def test_change_password_wrong_old_password(self):
        resp = self.api_post(CHANGE_PASSWORD_URL, {
            'old_password': 'wrongold',
            'new_password': 'newpass1234',
        }, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_change_password_too_short(self):
        resp = self.api_post(CHANGE_PASSWORD_URL, {
            'old_password': 'oldpass1234',
            'new_password': 'short',
        }, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_change_password_missing_fields(self):
        resp = self.api_post(CHANGE_PASSWORD_URL, {}, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_change_password_common_password(self):
        resp = self.api_post(CHANGE_PASSWORD_URL, {
            'old_password': 'oldpass1234',
            'new_password': 'password123',
        }, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestForcedPasswordChange(EchoDeskTenantTestCase):
    """Tests for forced_password_change endpoint."""

    def setUp(self):
        super().setUp()
        self.user = self.create_user(
            email='forced@test.com', password='temppass123',
            password_change_required=True,
            temporary_password='temppass123',
        )

    def test_forced_change_success_clears_flag(self):
        resp = self.api_post(FORCED_PASSWORD_CHANGE_URL, {
            'email': 'forced@test.com',
            'current_password': 'temppass123',
            'new_password': 'newstrongpass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertFalse(self.user.password_change_required)
        self.assertTrue(self.user.check_password('newstrongpass1'))

    def test_forced_change_returns_token(self):
        resp = self.api_post(FORCED_PASSWORD_CHANGE_URL, {
            'email': 'forced@test.com',
            'current_password': 'temppass123',
            'new_password': 'newstrongpass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('token', resp.data)

    def test_forced_change_wrong_current_password(self):
        resp = self.api_post(FORCED_PASSWORD_CHANGE_URL, {
            'email': 'forced@test.com',
            'current_password': 'wrongpass',
            'new_password': 'newstrongpass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['error'], 'Invalid credentials')

    def test_forced_change_nonexistent_email(self):
        resp = self.api_post(FORCED_PASSWORD_CHANGE_URL, {
            'email': 'nobody@test.com',
            'current_password': 'whatever',
            'new_password': 'newstrongpass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['error'], 'Invalid credentials')

    def test_forced_change_common_password(self):
        resp = self.api_post(FORCED_PASSWORD_CHANGE_URL, {
            'email': 'forced@test.com',
            'current_password': 'temppass123',
            'new_password': 'password123',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_forced_change_not_required(self):
        self.user.password_change_required = False
        self.user.save()

        resp = self.api_post(FORCED_PASSWORD_CHANGE_URL, {
            'email': 'forced@test.com',
            'current_password': 'temppass123',
            'new_password': 'newstrongpass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestProfile(EchoDeskTenantTestCase):
    """Tests for tenant_profile endpoint."""

    def setUp(self):
        super().setUp()
        self.user = self.create_admin(email='profile@test.com')

    def test_profile_returns_user_data(self):
        resp = self.api_get(PROFILE_URL, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['email'], 'profile@test.com')

    def test_profile_includes_permissions(self):
        resp = self.api_get(PROFILE_URL, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('all_permissions', resp.data)

    def test_profile_includes_feature_keys(self):
        resp = self.api_get(PROFILE_URL, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('feature_keys', resp.data)

    def test_profile_unauthenticated_returns_401(self):
        resp = self.api_get(PROFILE_URL)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_profile_is_leave_manager_true_for_manager_role(self):
        manager = self.create_manager(email='profile-mgr@test.com')
        resp = self.api_get(PROFILE_URL, user=manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['is_leave_manager'])

    def test_profile_is_leave_manager_true_with_direct_report(self):
        lead = self.create_user(email='profile-lead@test.com', role='agent')
        self.create_user(email='profile-report@test.com', manager=lead)
        resp = self.api_get(PROFILE_URL, user=lead)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['is_leave_manager'])

    def test_profile_is_leave_manager_false_for_agent(self):
        agent = self.create_user(email='profile-agent@test.com', role='agent')
        resp = self.api_get(PROFILE_URL, user=agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['is_leave_manager'])


class TestAuthThrottling(EchoDeskTenantTestCase):
    """Tests for rate limiting on auth endpoints."""

    def test_login_has_throttle_class(self):
        """Login endpoint should be throttled — patch throttle to deny and verify 429."""
        with patch('tenants.views.AuthRateThrottle.allow_request', return_value=False), \
             patch('tenants.views.AuthRateThrottle.wait', return_value=60):
            resp = self.api_post(LOGIN_URL, {
                'email': 'nobody@test.com',
                'password': 'whatever',
            })
            self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
