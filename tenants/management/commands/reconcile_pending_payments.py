"""
Reconcile recurring/retry payments that are stuck 'pending' against BOG.

BOG's offline saved-card charges (/subscribe) settle ASYNCHRONOUSLY and the
status webhook is unreliable — when it never arrives, the PaymentOrder stays
'pending' forever, subscription.next_billing_date is never advanced, and the
recurring + retry jobs keep re-charging the card (the Aug 2026 artlighthouse
double-charge). This job polls BOG directly for every stuck-pending recurring
charge and applies the same state transition the webhook would have, so we no
longer depend on the webhook being delivered.

Runs on a Celery beat schedule. Idempotent and safe to run repeatedly.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from tenants.models import PaymentAttempt, PaymentOrder, TenantSubscription

logger = logging.getLogger(__name__)

# Only reconcile charges old enough for BOG to have settled, but young enough
# to still be actionable. Older stragglers are left for manual review.
MIN_AGE = timedelta(minutes=10)
MAX_AGE = timedelta(days=14)


def _advance_billing(subscription):
    """Advance a subscription one billing cycle on a confirmed charge.
    Mirrors the recurring branch of bog_webhook. Idempotent: a no-op when
    next_billing_date is already in the future."""
    now = timezone.now()
    if subscription.next_billing_date and subscription.next_billing_date > now:
        return False
    billing_interval = timedelta(days=30)
    if getattr(settings, 'TEST_BILLING_INTERVAL', False):
        billing_interval = timedelta(minutes=2)
    anchor = subscription.next_billing_date or now
    anchor += billing_interval
    while anchor <= now:
        anchor += billing_interval
    subscription.last_billed_at = now
    subscription.expires_at = anchor
    subscription.next_billing_date = anchor
    subscription.failed_payment_count = 0
    subscription.save(update_fields=[
        'last_billed_at', 'expires_at', 'next_billing_date', 'failed_payment_count',
    ])
    return True


class Command(BaseCommand):
    help = "Reconcile stuck-pending recurring/retry payments against BOG"

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        from tenants.bog_payment import bog_service

        dry_run = options['dry_run']
        now = timezone.now()
        cutoff_new = now - MIN_AGE
        cutoff_old = now - MAX_AGE

        orders = PaymentOrder.objects.filter(
            status='pending',
            created_at__lte=cutoff_new,
            created_at__gte=cutoff_old,
        ).filter(order_id__startswith='REC-') | PaymentOrder.objects.filter(
            status='pending',
            created_at__lte=cutoff_new,
            created_at__gte=cutoff_old,
            order_id__startswith='RETRY-',
        )

        counts = {'paid': 0, 'failed': 0, 'refunded': 0, 'still_pending': 0, 'error': 0}

        for order in orders.distinct():
            if not order.bog_order_id:
                continue
            try:
                res = bog_service.check_payment_status(order.bog_order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning('reconcile: BOG status check failed for %s: %s', order.order_id, exc)
                counts['error'] += 1
                continue

            bog_status = res.get('bog_status')
            if bog_status == 'completed':
                self._settle_paid(order, dry_run)
                counts['paid'] += 1
            elif bog_status in ('refunded', 'refunded_partially'):
                if not dry_run:
                    order.status = 'cancelled'
                    order.save(update_fields=['status'])
                counts['refunded'] += 1
            elif bog_status in ('rejected', 'blocked'):
                if not dry_run:
                    order.status = 'failed'
                    order.save(update_fields=['status'])
                counts['failed'] += 1
            else:
                # created / processing / unknown — still settling, leave it.
                counts['still_pending'] += 1

        self.stdout.write(self.style.SUCCESS(
            f"reconcile_pending_payments: {counts}"
            + (' (dry run)' if dry_run else '')
        ))
        logger.info('reconcile_pending_payments: %s', counts)

    def _settle_paid(self, order, dry_run):
        subscription = None
        sub_id = (order.metadata or {}).get('subscription_id')
        if sub_id:
            subscription = TenantSubscription.objects.filter(id=sub_id).first()
        if subscription is None and order.tenant_id:
            subscription = TenantSubscription.objects.filter(
                tenant_id=order.tenant_id
            ).order_by('-id').first()

        if dry_run:
            self.stdout.write(
                f"  [DRY] would mark {order.order_id} paid"
                + (f" + advance sub {subscription.id}" if subscription else "")
            )
            return

        with transaction.atomic():
            locked = PaymentOrder.objects.select_for_update().get(pk=order.pk)
            if locked.status == 'paid':
                return  # another run/webhook won the race
            locked.status = 'paid'
            locked.paid_at = timezone.now()
            locked.save(update_fields=['status', 'paid_at'])
            if subscription is not None:
                sub_locked = TenantSubscription.objects.select_for_update().get(pk=subscription.pk)
                advanced = _advance_billing(sub_locked)
                # A settled recurring charge means the earlier failed attempts
                # are done — cancel any still-pending retry schedules so the
                # retry path can't fire a duplicate charge.
                sub_locked.retry_schedules.filter(status='pending').update(status='cancelled')
                PaymentAttempt.objects.filter(
                    subscription=sub_locked, status='pending',
                ).update(status='cancelled')
                logger.info(
                    'reconcile: %s marked paid; subscription %s advanced=%s',
                    locked.order_id, sub_locked.id, advanced,
                )
