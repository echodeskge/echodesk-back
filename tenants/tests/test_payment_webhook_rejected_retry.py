"""
Sentry ECHODESK-BACKEND-6Z / 70: "Payment order not found for failed payment".

execute_retry charges a saved card under ``RETRY-{schedule}-{order}``, an id
that is not a PaymentOrder. When BOG rejected such a charge the webhook's
'rejected' branch looked the id up as a PaymentOrder, logged an error and
recorded nothing. The failure now lands on the retry schedule and its
pending PaymentAttempt, with a ``retry_failed`` event.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from tenants.models import (
    PaymentAttempt, PaymentOrder, PaymentRetrySchedule, SubscriptionEvent, TenantSubscription,
)
from users.tests.conftest import EchoDeskTenantTestCase

WEBHOOK = '/api/payments/webhook/'
LOGGER = 'tenants.payment_views'


def _rejected(external_order_id, bog_order_id='bog-retry-1', code='116'):
    return {
        'event': 'order_payment',
        'body': {
            'order_id': bog_order_id,
            'external_order_id': external_order_id,
            'order_status': {'key': 'rejected'},
            'payment_detail': {'code': code},
            'message': 'Insufficient funds',
        },
    }


class RejectedRetryWebhookTests(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.tenant.payment_provider = 'bog'
        self.tenant.save()
        self.sub = TenantSubscription.objects.create(
            tenant=self.tenant,
            is_active=True,
            starts_at=timezone.now() - timedelta(days=60),
            next_billing_date=timezone.now() - timedelta(days=1),
            parent_order_id=f'BOG-{uuid.uuid4().hex[:8]}',
            agent_count=1,
            payment_status='retrying',
            failed_payment_count=1,
        )
        self.order = PaymentOrder.objects.create(
            order_id=f'REC-{uuid.uuid4().hex[:12].upper()}',
            tenant=self.tenant,
            amount=Decimal('100.00'),
            currency='GEL',
            status='failed',
        )
        self.original = PaymentAttempt.objects.create(
            payment_order=self.order, subscription=self.sub, tenant=self.tenant,
            attempt_number=1, status='failed', amount=self.order.amount,
        )
        self.retry_attempt = PaymentAttempt.objects.create(
            payment_order=self.order, subscription=self.sub, tenant=self.tenant,
            attempt_number=2, status='pending', amount=self.order.amount,
            bog_order_id='bog-retry-1', is_retry=True, parent_attempt=self.original,
        )
        self.schedule = PaymentRetrySchedule.objects.create(
            payment_order=self.order, subscription=self.sub, tenant=self.tenant,
            original_attempt=self.original, retry_number=1,
            scheduled_for=timezone.now() - timedelta(hours=1), status='executing',
            executed_at=timezone.now() - timedelta(minutes=5), retry_attempt=self.retry_attempt,
        )
        self.external_id = f'RETRY-{self.schedule.id}-{self.order.order_id}'

    def _post(self, body):
        with self.assertLogs(LOGGER, level='INFO') as logs:
            resp = self.api_post(WEBHOOK, body)
        return resp, logs

    def test_rejected_retry_marks_attempt_and_schedule_failed(self):
        resp, logs = self._post(_rejected(self.external_id))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data, {'status': 'received'})
        self.assertFalse([r for r in logs.records if r.levelno >= 40], logs.output)

        self.retry_attempt.refresh_from_db()
        self.assertEqual(self.retry_attempt.status, 'failed')
        self.assertEqual(self.retry_attempt.failed_reason, 'Insufficient funds')
        self.assertEqual(self.retry_attempt.bog_error_code, '116')
        self.assertIsNotNone(self.retry_attempt.completed_at)
        self.assertEqual(self.retry_attempt.bog_response['external_order_id'], self.external_id)

        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.status, 'failed')
        self.assertEqual(self.schedule.skip_reason, 'Insufficient funds')

        event = SubscriptionEvent.objects.get(subscription=self.sub, event_type='retry_failed')
        self.assertEqual(event.payment_attempt_id, self.retry_attempt.id)
        self.assertEqual(event.metadata['retry_number'], 1)

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.failed_payment_count, 2)
        self.assertEqual(self.sub.payment_status, 'retrying')
        # The original order is untouched: it already failed on its own.
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'failed')

    def test_duplicate_rejected_retry_is_idempotent(self):
        self._post(_rejected(self.external_id))
        resp, logs = self._post(_rejected(self.external_id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SubscriptionEvent.objects.filter(event_type='retry_failed').count(), 1)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.failed_payment_count, 2)

    def test_rejected_retry_for_unknown_schedule_is_acknowledged_at_warning(self):
        resp, logs = self._post(_rejected(f'RETRY-999999-{self.order.order_id}'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual({r.levelname for r in logs.records}, {'INFO', 'WARNING'})
        self.assertTrue(any('Retry schedule not found' in line for line in logs.output))

    def test_rejected_unknown_order_is_acknowledged_at_warning(self):
        resp, logs = self._post(_rejected('ORD-does-not-exist'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual({r.levelname for r in logs.records}, {'INFO', 'WARNING'})
        self.assertTrue(any('Payment order not found' in line for line in logs.output))
