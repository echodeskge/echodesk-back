"""
Review fixes on the P6 users/security package:

4. Endpoints that charge the tenant's card, save a card or cancel the
   subscription need staff, a superuser or role=admin.
7. A staff user who is not a superuser cannot change the IP whitelist so
   that no active superuser can get in any more while the bypass is off.
"""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test.utils import override_settings
from rest_framework import status

from tenants.models import SecurityLog, TenantIPWhitelist
from tenants.tests.test_security_views import _TEST_MIDDLEWARE_NO_IP, SecurityTestMixin
from users.tests.conftest import EchoDeskTenantTestCase

User = get_user_model()

IP_TOGGLE_URL = '/api/security/ip-whitelist/toggle/'
CALLER_IP = '127.0.0.1'
SU_IP = '10.9.9.9'


def manage_url(pk):
    return f'/api/security/ip-whitelist/{pk}/'


# ---------------------------------------------------------------------------
# 4. Billing endpoints
# ---------------------------------------------------------------------------

BILLING_ENDPOINTS = [
    ('post', '/api/payments/create/'),
    ('post', '/api/payments/saved-card/add/'),
    ('post', '/api/payments/saved-card/add-ecommerce/'),
    ('post', '/api/payments/manual/'),
    ('post', '/api/payments/reactivate/'),
    ('post', '/api/payments/cancel/'),
    ('delete', '/api/payments/saved-card/remove/'),
    ('post', '/api/payments/saved-card/set-default/'),
]


class TestPaymentEndpointsNeedBillingAdmin(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='pay-agent@test.com')
        self.user_manager = self.create_user(
            email='pay-mgr@test.com', role='manager', can_manage_users=True, can_manage_settings=True,
        )
        self.role_admin = self.create_user(email='pay-roleadmin@test.com', role='admin')
        self.staff = self.create_user(email='pay-staff@test.com', is_staff=True)
        self.superuser = self.create_user(email='pay-su@test.com', is_superuser=True)

    def call(self, method, url, user):
        if method == 'delete':
            return self.api_delete(url, user=user)
        return self.api_post(url, {}, user=user)

    @patch('tenants.payment_views.bog_service')
    def test_review_repro_non_admins_get_403(self, mock_bog):
        for user in (self.agent, self.user_manager):
            for method, url in BILLING_ENDPOINTS:
                resp = self.call(method, url, user)
                self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, (user.email, url))
        mock_bog.create_payment.assert_not_called()
        mock_bog.is_configured.assert_not_called()

    def test_unauthenticated_gets_401(self):
        resp = self.client.post('/api/payments/create/', {}, format='json', HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch('tenants.payment_views.bog_service')
    def test_billing_admins_pass_the_gate(self, mock_bog):
        # Gateway "not configured": the views stop before any external call.
        mock_bog.is_configured.return_value = False
        mock_bog.create_payment = MagicMock(side_effect=AssertionError('no external calls'))
        for user in (self.role_admin, self.staff, self.superuser):
            resp = self.api_post('/api/payments/create/', {}, user=user)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, user.email)
            self.assertEqual(resp.data['error'], 'package_id is required')
            for url in ('/api/payments/saved-card/add/', '/api/payments/saved-card/add-ecommerce/'):
                resp = self.api_post(url, {}, user=user)
                self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE, (user.email, url))


# ---------------------------------------------------------------------------
# 7. Whitelist cannot lock superusers out (bypass off)
# ---------------------------------------------------------------------------

@override_settings(MIDDLEWARE=_TEST_MIDDLEWARE_NO_IP)
class TestWhitelistProtectsSuperusers(SecurityTestMixin, EchoDeskTenantTestCase):

    @classmethod
    def setup_test_middleware(cls):
        return override_settings(MIDDLEWARE=_TEST_MIDDLEWARE_NO_IP)

    def setUp(self):
        super().setUp()
        self.superuser = self.create_user(email='wl-su@test.com', is_superuser=True, is_staff=True)
        self.staff = self.create_user(email='wl-staff@test.com', is_staff=True)
        SecurityLog.objects.create(
            tenant=self.tenant, user_id=self.superuser.id, event_type='login_success', ip_address=SU_IP,
        )
        # The staff caller's own IP is always listed, so only the superuser
        # guard can refuse.
        self.mine = self._make_whitelist(ip=CALLER_IP)

    def set_tenant(self, enabled, bypass):
        self.tenant.ip_whitelist_enabled = enabled
        self.tenant.superadmin_bypass_whitelist = bypass
        self.tenant.save()

    def assert_superuser_lockout(self, resp):
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, getattr(resp, 'data', None))
        self.assertEqual(resp.data['code'], 'would_lock_out_superusers')
        self.assertIn('superuser', resp.data['error'])

    def test_review_repro_staff_enable_refused_when_no_superuser_covered(self):
        self.set_tenant(enabled=False, bypass=False)
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True}, user=self.staff)
        self.assert_superuser_lockout(resp)
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.ip_whitelist_enabled)

    def test_staff_enable_allowed_once_a_superuser_ip_is_listed(self):
        self.set_tenant(enabled=False, bypass=False)
        self._make_whitelist(ip='10.9.0.0', cidr_notation='16')
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['ip_whitelist_enabled'])

    def test_staff_enable_allowed_with_bypass_on(self):
        self.set_tenant(enabled=False, bypass=True)
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_superuser_who_never_signed_in_counts_as_uncovered(self):
        SecurityLog.objects.filter(user_id=self.superuser.id).delete()
        self.set_tenant(enabled=False, bypass=False)
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True}, user=self.staff)
        self.assert_superuser_lockout(resp)

    def test_latest_sign_in_ip_is_the_one_that_counts(self):
        self._make_whitelist(ip=SU_IP)
        SecurityLog.objects.create(
            tenant=self.tenant, user_id=self.superuser.id, event_type='login_success', ip_address='10.7.7.7',
        )
        self.set_tenant(enabled=False, bypass=False)
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True}, user=self.staff)
        self.assert_superuser_lockout(resp)

    def test_one_covered_superuser_is_enough(self):
        other_su = self.create_user(email='wl-su2@test.com', is_superuser=True)
        SecurityLog.objects.create(
            tenant=self.tenant, user_id=other_su.id, event_type='login_success', ip_address='10.5.5.5',
        )
        self._make_whitelist(ip='10.5.5.5')
        self.set_tenant(enabled=False, bypass=False)
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_staff_cannot_delete_or_deactivate_last_superuser_entry(self):
        su_entry = self._make_whitelist(ip=SU_IP)
        self.set_tenant(enabled=True, bypass=False)
        resp = self.api_delete(manage_url(su_entry.pk), user=self.staff)
        self.assert_superuser_lockout(resp)
        self.assertTrue(TenantIPWhitelist.objects.filter(pk=su_entry.pk).exists())
        resp = self.api_patch(manage_url(su_entry.pk), {'is_active': False}, user=self.staff)
        self.assert_superuser_lockout(resp)
        resp = self.api_patch(manage_url(su_entry.pk), {'ip_address': '10.1.2.3'}, user=self.staff)
        self.assert_superuser_lockout(resp)
        su_entry.refresh_from_db()
        self.assertTrue(su_entry.is_active)
        self.assertEqual(su_entry.ip_address, SU_IP)

    def test_staff_may_delete_unrelated_entries(self):
        self._make_whitelist(ip=SU_IP)
        other = self._make_whitelist(ip='10.3.3.3')
        self.set_tenant(enabled=True, bypass=False)
        resp = self.api_delete(manage_url(other.pk), user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_deleting_with_bypass_on_is_fine(self):
        su_entry = self._make_whitelist(ip=SU_IP)
        self.set_tenant(enabled=True, bypass=True)
        resp = self.api_delete(manage_url(su_entry.pk), user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_superuser_caller_not_restricted_by_this_guard(self):
        # A superuser protects themselves via their own-IP check.
        su_entry = self._make_whitelist(ip=SU_IP)
        self.set_tenant(enabled=True, bypass=False)
        resp = self.api_delete(manage_url(su_entry.pk), user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_no_superusers_no_restriction(self):
        User.objects.filter(is_superuser=True).update(is_active=False)
        self.set_tenant(enabled=False, bypass=False)
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': True}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_disabling_never_blocked(self):
        self.set_tenant(enabled=True, bypass=False)
        resp = self.api_post(IP_TOGGLE_URL, {'enabled': False}, user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
