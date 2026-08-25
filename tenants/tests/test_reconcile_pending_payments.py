"""
Tests for reconcile_pending_payments and the retry double-charge guard.

The Aug 2026 artlighthouse incident: a recurring charge settled at BOG but
the async webhook never arrived, so the order stayed 'pending',
next_billing_date never advanced, and the retry path charged the card a
second time. These tests pin down the fix:

1. reconcile marks a BOG-completed pending order paid + advances billing
2. reconcile cancels pending retry schedules once a charge settles
3. reconcile marks refunded/rejected charges without advancing billing
4. execute_retry refuses to charge when the period is already paid
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.utils import timezone

from users.tests.conftest import EchoDeskTenantTestCase
from tenants.models import (
    PaymentAttempt,
    PaymentOrder,
    PaymentRetrySchedule,
    TenantSubscription,
)


class ReconcilePendingPaymentsTests(EchoDeskTenantTestCase):
    def setUp(self):
        super().setUp()
        self.tenant.payment_provider = 'bog'
        self.tenant.save()
        self.sub = TenantSubscription.objects.create(
            tenant=self.tenant,
            is_active=True,
            starts_at=timezone.now() - timedelta(days=60),
            next_billing_date=timezone.now() - timedelta(days=1),  # stuck in the past
            last_billed_at=timezone.now() - timedelta(days=31),
            parent_order_id=f'BOG-{uuid.uuid4().hex[:8]}',
            agent_count=1,
            failed_payment_count=4,
        )

    def _pending_order(self, prefix='REC-', age_minutes=20):
        o = PaymentOrder.objects.create(
            order_id=f'{prefix}{uuid.uuid4().hex[:12].upper()}',
            bog_order_id=str(uuid.uuid4()),
            tenant=self.tenant,
            amount=Decimal('100.00'),
            currency='GEL',
            status='pending',
            metadata={'type': 'recurring', 'subscription_id': self.sub.id},
        )
        # Backdate created_at past the MIN_AGE window.
        PaymentOrder.objects.filter(pk=o.pk).update(
            created_at=timezone.now() - timedelta(minutes=age_minutes)
        )
        return PaymentOrder.objects.get(pk=o.pk)

    def _run(self, bog_status):
        with patch('tenants.bog_payment.bog_service') as mock_bog:
            mock_bog.check_payment_status.return_value = {
                'bog_status': bog_status, 'status': 'paid', 'response_code': '100',
            }
            out = StringIO()
            call_command('reconcile_pending_payments', stdout=out)
            return out.getvalue()

    def test_completed_charge_marked_paid_and_billing_advanced(self):
        order = self._pending_order()
        before = self.sub.next_billing_date
        self._run('completed')

        order.refresh_from_db()
        self.sub.refresh_from_db()
        self.assertEqual(order.status, 'paid')
        self.assertIsNotNone(order.paid_at)
        self.assertGreater(self.sub.next_billing_date, timezone.now())
        self.assertGreater(self.sub.next_billing_date, before)
        self.assertEqual(self.sub.failed_payment_count, 0)

    def test_settled_charge_cancels_pending_retries(self):
        order = self._pending_order()
        attempt = PaymentAttempt.objects.create(
            payment_order=order, subscription=self.sub, tenant=self.tenant,
            attempt_number=1, amount=Decimal('100.00'), status='failed',
        )
        retry = PaymentRetrySchedule.objects.create(
            payment_order=order, subscription=self.sub, tenant=self.tenant,
            original_attempt=attempt, retry_number=1,
            scheduled_for=timezone.now() + timedelta(days=2), status='pending',
        )
        pending_attempt = PaymentAttempt.objects.create(
            payment_order=order, subscription=self.sub, tenant=self.tenant,
            attempt_number=2, amount=Decimal('100.00'), status='pending', is_retry=True,
        )
        self._run('completed')

        retry.refresh_from_db()
        pending_attempt.refresh_from_db()
        self.assertEqual(retry.status, 'cancelled')
        self.assertEqual(pending_attempt.status, 'cancelled')

    def test_refunded_charge_not_marked_paid(self):
        order = self._pending_order()
        self._run('refunded')
        order.refresh_from_db()
        self.sub.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        # Billing must NOT advance on a refund.
        self.assertLess(self.sub.next_billing_date, timezone.now())

    def test_rejected_charge_marked_failed(self):
        order = self._pending_order()
        self._run('rejected')
        order.refresh_from_db()
        self.assertEqual(order.status, 'failed')

    def test_processing_charge_left_pending(self):
        order = self._pending_order()
        self._run('processing')
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending')

    def test_too_new_order_skipped(self):
        order = self._pending_order(age_minutes=2)  # inside MIN_AGE
        self._run('completed')
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending')


class RetryGuardTests(EchoDeskTenantTestCase):
    def setUp(self):
        super().setUp()
        self.tenant.payment_provider = 'bog'
        self.tenant.save()

    def _make_retry(self, next_billing_offset_days):
        sub = TenantSubscription.objects.create(
            tenant=self.tenant, is_active=True,
            starts_at=timezone.now() - timedelta(days=60),
            next_billing_date=timezone.now() + timedelta(days=next_billing_offset_days),
            parent_order_id=f'BOG-{uuid.uuid4().hex[:8]}', agent_count=1,
        )
        order = PaymentOrder.objects.create(
            order_id=f'REC-{uuid.uuid4().hex[:12].upper()}',
            bog_order_id=str(uuid.uuid4()), tenant=self.tenant,
            amount=Decimal('100.00'), status='failed',
            metadata={'subscription_id': sub.id},
        )
        attempt = PaymentAttempt.objects.create(
            payment_order=order, subscription=sub, tenant=self.tenant,
            attempt_number=1, amount=Decimal('100.00'), status='failed',
        )
        return PaymentRetrySchedule.objects.create(
            payment_order=order, subscription=sub, tenant=self.tenant,
            original_attempt=attempt, retry_number=1,
            scheduled_for=timezone.now(), status='pending',
        )

    def test_retry_skipped_when_already_paid(self):
        from tenants.subscription_utils import execute_retry
        retry = self._make_retry(next_billing_offset_days=15)  # paid through next month
        with patch('tenants.bog_payment.bog_service') as mock_bog:
            result = execute_retry(retry)
        retry.refresh_from_db()
        self.assertFalse(result['success'])
        self.assertEqual(retry.status, 'skipped')
        mock_bog.charge_subscription.assert_not_called()

    def test_retry_proceeds_when_payment_due(self):
        from tenants.subscription_utils import execute_retry
        retry = self._make_retry(next_billing_offset_days=-1)  # overdue
        with patch('tenants.bog_payment.bog_service') as mock_bog:
            mock_bog.charge_subscription.return_value = {'order_id': str(uuid.uuid4())}
            execute_retry(retry)
        mock_bog.charge_subscription.assert_called_once()
        # Callback must target the API host, not the frontend.
        _, kwargs = mock_bog.charge_subscription.call_args
        self.assertIn('api.echodesk.ge', kwargs['callback_url'])
