"""
Tests for the check_subscription_status management command (run nightly by
Celery beat). It used to crash with a FieldError on select_related('package')
after subscriptions moved from packages to feature sets.
"""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from users.tests.conftest import EchoDeskTenantTestCase
from tenants.models import TenantSubscription
from tenants.management.commands.check_subscription_status import Command


class TestCheckSubscriptionStatus(EchoDeskTenantTestCase):

    def _subscription(self, expires_in_days, **kwargs):
        defaults = {
            'tenant': self.tenant,
            'is_active': True,
            'starts_at': timezone.now() - timedelta(days=60),
            'expires_at': timezone.now() + timedelta(days=expires_in_days),
            'agent_count': 5,
            'payment_status': 'current',
        }
        defaults.update(kwargs)
        return TenantSubscription.objects.create(**defaults)

    def _run(self):
        out = StringIO()
        call_command('check_subscription_status', stdout=out)
        return out.getvalue()

    def test_runs_without_error_and_sends_reminder(self):
        self._subscription(expires_in_days=7, expires_at=timezone.now() + timedelta(days=7, hours=1))
        output = self._run()
        self.assertIn('Subscription Status Check Complete', output)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('expires in 7 days', mail.outbox[0].subject)
        # The plan is described by users + features, not a package name.
        self.assertIn('5 users, 0 features', mail.outbox[0].body)

    def test_active_subscription_untouched(self):
        sub = self._subscription(expires_in_days=30)
        self._run()
        sub.refresh_from_db()
        self.tenant.refresh_from_db()
        self.assertTrue(sub.is_active)
        self.assertTrue(self.tenant.is_active)
        self.assertEqual(len(mail.outbox), 0)

    def test_grace_period_warns_without_suspending(self):
        sub = self._subscription(expires_in_days=-2)
        self._run()
        sub.refresh_from_db()
        self.assertTrue(sub.is_active)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Payment Required', mail.outbox[0].subject)

    def test_past_grace_suspends(self):
        sub = self._subscription(expires_in_days=-30)
        self._run()
        sub.refresh_from_db()
        self.tenant.refresh_from_db()
        self.assertFalse(sub.is_active)
        self.assertFalse(self.tenant.is_active)

    def test_exempt_tenant_is_never_suspended(self):
        sub = self._subscription(expires_in_days=-30)
        with mock.patch.object(Command, 'EXEMPT_TENANTS', [self.tenant.schema_name]):
            self._run()
        sub.refresh_from_db()
        self.tenant.refresh_from_db()
        self.assertTrue(sub.is_active)
        self.assertTrue(self.tenant.is_active)

    def test_groot_and_amanati_are_exempt(self):
        self.assertIn('groot', Command.EXEMPT_TENANTS)
        self.assertIn('amanati', Command.EXEMPT_TENANTS)
