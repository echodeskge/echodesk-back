"""
TenantSubscription.current_users must reflect the tenant's active users:
live recount on the subscription endpoint / limit checks, signals on user
create / deactivate / delete, bulk actions, and the recount command.
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase
from tenants.models import TenantSubscription
from tenants.permissions import check_subscription_limit
from tenants.tests.test_subscription_views import SubscriptionTestMixin
from tenants.user_count import count_billable_users, sync_subscription_user_count

User = get_user_model()

SUBSCRIPTION_ME_URL = '/api/subscription/me/'
BULK_URL = '/api/users/bulk_action/'


class _Base(SubscriptionTestMixin, EchoDeskTenantTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='countadmin@test.com')
        self.sub = self._make_subscription(agent_count=3)
        # Users created before the subscription existed: start in sync.
        sync_subscription_user_count(self.tenant)

    def stored(self):
        return TenantSubscription.objects.get(pk=self.sub.pk).current_users

    def active(self):
        return User.objects.filter(is_active=True).count()


class TestCountBillableUsers(_Base):

    def test_counts_only_active_users(self):
        before = count_billable_users('test')
        self.create_user(email='a1@test.com')
        self.create_user(email='a2@test.com', is_active=False)
        self.assertEqual(count_billable_users('test'), before + 1)

    def test_counts_from_public_schema_via_schema_context(self):
        expected = self.active()
        connection.set_schema_to_public()
        try:
            self.assertEqual(count_billable_users('test'), expected)
        finally:
            connection.set_tenant(self.tenant)


class TestSignalsKeepStoredCountInSync(_Base):

    def test_create_user_updates_count(self):
        before = self.stored()
        self.create_user(email='new@test.com')
        self.assertEqual(self.stored(), before + 1)
        self.assertEqual(self.stored(), self.active())

    def test_deactivate_and_reactivate_updates_count(self):
        user = self.create_user(email='deact@test.com')
        before = self.stored()
        user.is_active = False
        user.save()
        self.assertEqual(self.stored(), before - 1)
        user.is_active = True
        user.save(update_fields=['is_active'])
        self.assertEqual(self.stored(), before)

    def test_delete_user_updates_count(self):
        user = self.create_user(email='del@test.com')
        before = self.stored()
        user.delete()
        self.assertEqual(self.stored(), before - 1)

    def test_last_login_save_does_not_recount(self):
        TenantSubscription.objects.filter(pk=self.sub.pk).update(current_users=42)
        self.admin.save(update_fields=['last_login'])
        self.assertEqual(self.stored(), 42)

    def test_bulk_deactivate_and_activate_update_count(self):
        u1 = self.create_user(email='b1@test.com')
        u2 = self.create_user(email='b2@test.com')
        before = self.stored()
        resp = self.api_post(BULK_URL, {'user_ids': [u1.id, u2.id], 'action': 'deactivate'},
                             user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self.stored(), before - 2)
        resp = self.api_post(BULK_URL, {'user_ids': [u1.id], 'action': 'activate'},
                             user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self.stored(), before - 1)


class TestSubscriptionEndpointCount(_Base):

    def test_me_returns_live_count_even_if_stored_is_stale(self):
        self.create_user(email='e1@test.com')
        self.create_user(email='e2@test.com', is_active=False)
        TenantSubscription.objects.filter(pk=self.sub.pk).update(current_users=0)

        resp = self.api_get(SUBSCRIPTION_ME_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        expected = self.active()
        self.assertGreater(expected, 0)
        self.assertEqual(resp.data['usage']['current_users'], expected)
        self.assertEqual(resp.data['usage_limits']['users']['current'], expected)
        self.assertEqual(resp.data['limits']['max_users'], 3)
        # Stale stored value was repaired.
        self.assertEqual(self.stored(), expected)

    def test_limit_check_uses_live_count(self):
        # Fill up to the agent limit (3), with a stale stored counter of 0.
        while self.active() < 3:
            self.create_user(email=f'fill{self.active()}@test.com')
        TenantSubscription.objects.filter(pk=self.sub.pk).update(current_users=0)

        class Req:
            tenant = self.tenant
        Req.tenant.refresh_from_db()
        result = check_subscription_limit(Req, 'users')
        self.assertEqual(result['current'], 3)
        self.assertEqual(result['limit'], 3)
        self.assertFalse(result['within_limit'])

        User.objects.filter(email__startswith='fill').update(is_active=False)
        Req.tenant.refresh_from_db()
        result = check_subscription_limit(Req, 'users')
        self.assertTrue(result['within_limit'])


class TestRecountCommand(_Base):

    def run_cmd(self, *args):
        out = StringIO()
        call_command('recount_tenant_users', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_does_not_write(self):
        TenantSubscription.objects.filter(pk=self.sub.pk).update(current_users=0)
        expected = self.active()
        out = self.run_cmd('--schema', 'test', '--dry-run')
        self.assertIn(f'test: 0 -> {expected} (would update)', out)
        self.assertIn('[DRY RUN]', out)
        self.assertEqual(self.stored(), 0)

    def test_real_run_writes(self):
        TenantSubscription.objects.filter(pk=self.sub.pk).update(current_users=0)
        expected = self.active()
        out = self.run_cmd('--schema', 'test')
        self.assertIn(f'test: 0 -> {expected} (updated)', out)
        self.assertEqual(self.stored(), expected)
        out = self.run_cmd('--all')
        self.assertIn(f'test: {expected} (unchanged)', out)

    def test_unknown_schema_errors(self):
        with self.assertRaises(CommandError):
            self.run_cmd('--schema', 'nope')

    def test_requires_target(self):
        with self.assertRaises(CommandError):
            self.run_cmd()

    def test_sync_returns_none_without_subscription(self):
        self.sub.delete()
        self.assertIsNone(sync_subscription_user_count(self.tenant))
