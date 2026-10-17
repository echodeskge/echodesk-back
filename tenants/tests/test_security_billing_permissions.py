"""
P6: security endpoints open to staff (as the dashboard shows them), the
IP-whitelist lock-out guard, stats input validation, and staff-only billing
reads / subscription changes.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test.utils import override_settings
from rest_framework import status

from tenants.models import TenantIPWhitelist
from tenants.tests.test_security_views import _TEST_MIDDLEWARE_NO_IP, SecurityTestMixin
from users.tests.conftest import EchoDeskTenantTestCase

User = get_user_model()

IP_LIST_URL = '/api/security/ip-whitelist/'
IP_TOGGLE_URL = '/api/security/ip-whitelist/toggle/'
LOGS_URL = '/api/security/logs/'
LOGS_STATS_URL = '/api/security/logs/stats/'
CURRENT_IP_URL = '/api/security/current-ip/'


def manage_url(pk):
    return f'/api/security/ip-whitelist/{pk}/'


# Requests from the test client come from 127.0.0.1 (REMOTE_ADDR).
CALLER_IP = '127.0.0.1'


@override_settings(MIDDLEWARE=_TEST_MIDDLEWARE_NO_IP)
class TestSecurityPermissionAlignment(SecurityTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.staff = self.create_user(email='secstaff@test.com', is_staff=True)
        self.agent = self.create_user(email='secagent@test.com')
        self.manager_with_flag = self.create_user(
            email='secmgr@test.com', role='manager', can_manage_users=True,
        )

    def test_staff_can_read_logs_stats_and_whitelist(self):
        for url in (LOGS_URL, LOGS_STATS_URL, IP_LIST_URL):
            resp = self.api_get(url, user=self.staff)
            self.assertEqual(resp.status_code, status.HTTP_200_OK, url)

    def test_agents_and_user_managers_are_refused(self):
        for user in (self.agent, self.manager_with_flag):
            for url in (LOGS_URL, LOGS_STATS_URL, IP_LIST_URL):
                resp = self.api_get(url, user=user)
                self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, (user.email, url))
            resp = self.api_post(IP_TOGGLE_URL, {'enabled': False}, user=user)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_stats_rejects_bad_days(self):
        for bad in ('abc', '0', '-5', '99999'):
            resp = self.api_get(f'{LOGS_STATS_URL}?days={bad}', user=self.staff)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, bad)
            self.assertIn('error', resp.data)

    def test_whitelist_entries_are_typed_rows(self):
        self._make_whitelist(ip='10.1.1.1')
        resp = self.api_get(IP_LIST_URL, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['entries'][0]['ip_address'], '10.1.1.1')


@override_settings(MIDDLEWARE=_TEST_MIDDLEWARE_NO_IP)
class TestIPWhitelistLockoutGuard(SecurityTestMixin, EchoDeskTenantTestCase):

    @classmethod
    def setup_test_middleware(cls):
        # The whitelist is switched on in some tests; the view, not the
        # middleware, is under test.
        return override_settings(MIDDLEWARE=_TEST_MIDDLEWARE_NO_IP)

    def setUp(self):
        super().setUp()
        self.superuser = self.create_user(email='lock-su@test.com', is_superuser=True, is_staff=True)
        self.staff = self.create_user(email='lock-staff@test.com', is_staff=True)

    def set_tenant(self, enabled, bypass):
        self.tenant.ip_whitelist_enabled = enabled
        self.tenant.superadmin_bypass_whitelist = bypass
        self.tenant.save()

    def test_enable_refused_when_caller_ip_not_listed(self):
        self._make_whitelist(ip='10.0.0.1')
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'would_lock_out')
        self.assertEqual(resp.data['ip_address'], CALLER_IP)
        self.assertIn(CALLER_IP, resp.data['error'])
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.ip_whitelist_enabled)

    def test_enable_allowed_when_caller_ip_listed(self):
        self._make_whitelist(ip=CALLER_IP)
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['ip_whitelist_enabled'])

    def test_enable_allowed_when_caller_in_cidr_range(self):
        self._make_whitelist(ip='127.0.0.0', cidr_notation='8')
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_inactive_entry_does_not_count(self):
        self._make_whitelist(ip=CALLER_IP, is_active=False)
        self._make_whitelist(ip='10.0.0.2')
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'would_lock_out')

    def test_superuser_with_bypass_may_enable(self):
        self._make_whitelist(ip='10.0.0.1')
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True, 'superadmin_bypass': True}, user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['ip_whitelist_enabled'])

    def test_bypass_does_not_help_staff(self):
        self._make_whitelist(ip='10.0.0.1')
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True, 'superadmin_bypass': True}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_superuser_without_bypass_refused(self):
        self._make_whitelist(ip='10.0.0.1')
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True, 'superadmin_bypass': False}, user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'would_lock_out')

    def test_turning_bypass_off_while_enabled_is_guarded(self):
        self._make_whitelist(ip='10.0.0.1')
        self.set_tenant(enabled=True, bypass=True)
        resp = self.api_post(IP_TOGGLE_URL, {'superadmin_bypass': False}, user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.superadmin_bypass_whitelist)

    def test_no_entries_error_has_code(self):
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'no_active_entries')

    def test_disabling_is_never_blocked(self):
        self._make_whitelist(ip='10.0.0.1')
        self.set_tenant(enabled=True, bypass=False)
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': False}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['ip_whitelist_enabled'])

    def test_deleting_own_entry_while_enabled_is_refused(self):
        mine = self._make_whitelist(ip=CALLER_IP)
        self._make_whitelist(ip='10.0.0.1')
        self.set_tenant(enabled=True, bypass=False)
        resp = self.api_delete(manage_url(mine.pk), user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'would_lock_out')
        self.assertTrue(TenantIPWhitelist.objects.filter(pk=mine.pk).exists())

    def test_deactivating_own_entry_while_enabled_is_refused(self):
        mine = self._make_whitelist(ip=CALLER_IP)
        self.set_tenant(enabled=True, bypass=False)
        resp = self.api_patch(manage_url(mine.pk), {'is_active': False}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        mine.refresh_from_db()
        self.assertTrue(mine.is_active)

    def test_deleting_other_entry_while_enabled_is_fine(self):
        self._make_whitelist(ip=CALLER_IP)
        other = self._make_whitelist(ip='10.0.0.1')
        self.set_tenant(enabled=True, bypass=False)
        resp = self.api_delete(manage_url(other.pk), user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_deleting_own_entry_while_disabled_is_fine(self):
        mine = self._make_whitelist(ip=CALLER_IP)
        resp = self.api_delete(manage_url(mine.pk), user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    @patch('tenants.security_views.SecurityService.get_ip_location', return_value={
        'city': 'Local', 'country': 'Local Network', 'country_code': 'LO'})
    def test_current_ip_reports_whitelisted(self, _loc):
        agent = self.create_user(email='lock-agent@test.com')
        resp = self.api_get(CURRENT_IP_URL, user=agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['is_whitelisted'])
        self._make_whitelist(ip=CALLER_IP)
        resp = self.api_get(CURRENT_IP_URL, user=agent)
        self.assertTrue(resp.data['is_whitelisted'])


class TestBillingPermissions(EchoDeskTenantTestCase):

    ENDPOINTS = [
        ('post', '/api/subscription/features/add/'),
        ('post', '/api/subscription/features/remove/'),
        ('put', '/api/subscription/agent-count/'),
        ('get', '/api/subscription/features/available/'),
        ('get', '/api/payments/invoices/'),
    ]

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='bill-agent@test.com')
        # Role admin with every user-management flag, but not staff.
        self.role_admin = self.create_user(
            email='bill-roleadmin@test.com', role='admin', can_manage_users=True,
        )
        self.staff = self.create_user(email='bill-staff@test.com', is_staff=True)
        self.superuser = self.create_user(email='bill-su@test.com', is_superuser=True)

    def call(self, method, url, user):
        if method == 'get':
            return self.api_get(url, user=user)
        if method == 'put':
            return self.api_put(url, {}, user=user)
        return self.api_post(url, {}, user=user)

    @patch('tenants.feature_views.bog_service')
    def test_non_staff_refused(self, bog):
        for user in (self.agent, self.role_admin):
            for method, url in self.ENDPOINTS:
                resp = self.call(method, url, user)
                self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, (user.email, url))
        bog.create_payment.assert_not_called()

    def test_staff_and_superuser_pass_the_permission_check(self):
        for user in (self.staff, self.superuser):
            # Empty bodies are rejected by validation, i.e. after permissions.
            resp = self.call('post', '/api/subscription/features/add/', user)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(resp.data['error'], 'feature_id is required')
            resp = self.call('post', '/api/subscription/features/remove/', user)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
            resp = self.call('put', '/api/subscription/agent-count/', user)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
            resp = self.call('get', '/api/subscription/features/available/', user)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertIn('features_by_category', resp.data)
            resp = self.call('get', '/api/payments/invoices/', user)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(resp.data, {'invoices': []})

    def test_subscription_me_still_open_to_agents(self):
        # Every dashboard user reads it for feature gating.
        resp = self.api_get('/api/subscription/me/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_saved_cards_still_staff_only(self):
        resp = self.api_get('/api/payments/saved-card/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_get('/api/payments/saved-card/', user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, [])
