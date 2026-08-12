"""
Double-charge protection tests for process_recurring_payments.

The April 2026 incident: a charge succeeded but the payment webhook never
advanced last_billed_at / next_billing_date, so the next daily run charged
the same customer again. These tests pin down the three guards:

1. queryset guard — last_billed_at within 25 days excludes the sub
2. webhook-independent guard — a recent recurring PaymentOrder we created
   ourselves blocks another charge even when the webhook was lost
3. overlap lock — a concurrent run aborts instead of racing
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone

from users.tests.conftest import EchoDeskTenantTestCase
from tenants.models import TenantSubscription, PaymentOrder
from tenants.management.commands.process_recurring_payments import RUN_LOCK_KEY


class RecurringPaymentGuardTests(EchoDeskTenantTestCase):
    def setUp(self):
        super().setUp()
        cache.delete(RUN_LOCK_KEY)
        self.tenant.payment_provider = 'bog'
        self.tenant.is_active = True
        self.tenant.save()
        self.sub = TenantSubscription.objects.create(
            tenant=self.tenant,
            is_active=True,
            starts_at=timezone.now() - timedelta(days=60),
            next_billing_date=timezone.now() - timedelta(days=1),  # due
            last_billed_at=timezone.now() - timedelta(days=31),    # outside guard
            parent_order_id=f'BOG-{uuid.uuid4().hex[:8]}',
            agent_count=2,
            payment_status='current',
        )

    def tearDown(self):
        cache.delete(RUN_LOCK_KEY)
        super().tearDown()

    def _run(self):
        out = StringIO()
        with patch(
            'tenants.management.commands.process_recurring_payments.bog_service'
        ) as mock_bog:
            mock_bog.charge_subscription.return_value = {'order_id': 'bog-ord-1'}
            call_command('process_recurring_payments', stdout=out)
        return mock_bog, out.getvalue()

    def test_due_subscription_is_charged_once(self):
        mock_bog, _ = self._run()
        self.assertEqual(mock_bog.charge_subscription.call_count, 1)
        self.assertEqual(
            PaymentOrder.objects.filter(
                tenant=self.tenant, order_id__startswith='REC-'
            ).count(),
            1,
        )

    def test_recent_recurring_order_blocks_recharge_even_without_webhook(self):
        # Simulate the April 2026 incident: yesterday's run charged (our
        # PaymentOrder row exists, still 'pending' because the webhook never
        # arrived), and neither last_billed_at nor next_billing_date moved.
        PaymentOrder.objects.create(
            order_id=f'REC-{uuid.uuid4().hex[:12].upper()}',
            tenant=self.tenant,
            amount=Decimal('100.00'),
            currency='GEL',
            status='pending',
        )
        mock_bog, out = self._run()
        mock_bog.charge_subscription.assert_not_called()
        self.assertIn('already sent', out)

    def test_failed_recent_order_does_not_block(self):
        # A failed charge attempt must not stop billing forever.
        PaymentOrder.objects.create(
            order_id=f'REC-{uuid.uuid4().hex[:12].upper()}',
            tenant=self.tenant,
            amount=Decimal('100.00'),
            currency='GEL',
            status='failed',
        )
        mock_bog, _ = self._run()
        self.assertEqual(mock_bog.charge_subscription.call_count, 1)

    def test_recent_last_billed_at_excludes_subscription(self):
        self.sub.last_billed_at = timezone.now() - timedelta(days=5)
        self.sub.save(update_fields=['last_billed_at'])
        mock_bog, _ = self._run()
        mock_bog.charge_subscription.assert_not_called()

    def test_concurrent_run_aborts_on_lock(self):
        cache.add(RUN_LOCK_KEY, 'held-by-other-run', 60)
        mock_bog, out = self._run()
        mock_bog.charge_subscription.assert_not_called()
        self.assertIn('already in progress', out)
        # The other run's lock must not be released by the aborted run.
        self.assertIsNotNone(cache.get(RUN_LOCK_KEY))

    def test_lock_released_after_run(self):
        self._run()
        self.assertIsNone(cache.get(RUN_LOCK_KEY))
