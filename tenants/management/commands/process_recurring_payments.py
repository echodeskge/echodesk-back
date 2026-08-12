"""
Django management command to process monthly recurring subscription payments

Run daily via cron:
0 2 * * * cd /path/to/project && python manage.py process_recurring_payments
"""

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from datetime import timedelta
from tenants.models import TenantSubscription, PaymentOrder, Tenant
from tenants.bog_payment import bog_service
import logging
import uuid

logger = logging.getLogger(__name__)

# Overlap lock: two concurrent runs (beat firing while a slow run is still
# iterating, or a manual run next to the scheduled one) must never both
# charge the same subscriptions. cache.add is atomic on Redis.
RUN_LOCK_KEY = 'process-recurring-payments-lock'
RUN_LOCK_TTL = 3600  # seconds; well above any realistic run duration


class Command(BaseCommand):
    help = 'Process recurring subscription payments for tenants'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be charged without actually processing payments',
        )
        parser.add_argument(
            '--days-before',
            type=int,
            default=3,
            help='Process subscriptions expiring within N days (default: 3)',
        )

    def handle(self, *args, **options):
        if not cache.add(RUN_LOCK_KEY, timezone.now().isoformat(), RUN_LOCK_TTL):
            self.stdout.write(self.style.ERROR(
                'Another recurring-payment run is already in progress — aborting to avoid double-charging.'
            ))
            logger.warning('process_recurring_payments: run skipped, lock held by a concurrent run')
            return
        try:
            self._process(*args, **options)
        finally:
            cache.delete(RUN_LOCK_KEY)

    def _process(self, *args, **options):
        dry_run = options['dry_run']
        days_before = options['days_before']

        self.stdout.write(self.style.SUCCESS(
            f'Starting recurring payment processing (dry_run={dry_run}, days_before={days_before})'
        ))

        # Find subscriptions that need renewal (BOG-only — Paddle manages billing automatically)
        cutoff_date = timezone.now() + timedelta(days=days_before)
        # Recent-bill guard: don't pick up a sub whose last successful charge
        # is younger than 25 days. Prevents the "charge daily for 3 days until
        # next_billing_date passes" double-billing bug when the webhook fails
        # to advance next_billing_date (belt-and-braces — the webhook now
        # always advances, but the guard protects against future regressions).
        recent_bill_cutoff = timezone.now() - timedelta(days=25)
        subscriptions_to_renew = TenantSubscription.objects.filter(
            is_active=True,
            next_billing_date__lte=cutoff_date,
            tenant__is_active=True,
            tenant__payment_provider='bog',
        ).exclude(
            last_billed_at__gte=recent_bill_cutoff,
        ).select_related('tenant')

        self.stdout.write(f'Found {subscriptions_to_renew.count()} subscriptions to process')

        success_count = 0
        failed_count = 0
        skipped_count = 0

        for subscription in subscriptions_to_renew:
            tenant = subscription.tenant

            try:
                # Check if tenant has saved card
                if not subscription.parent_order_id:
                    self.stdout.write(self.style.WARNING(
                        f'⚠️  {tenant.schema_name}: No saved card (parent_order_id), skipping'
                    ))
                    skipped_count += 1
                    continue

                # Webhook-independent double-charge guard. The recent-bill
                # exclusion in the queryset keys on last_billed_at, which only
                # the payment webhook advances — when a webhook is lost, the
                # sub stays eligible and the next daily run re-charges (the
                # April 2026 double-charge incident). Our own PaymentOrder
                # rows don't depend on the webhook: a recurring order created
                # in the last 25 days that isn't failed/cancelled means the
                # charge already went out — never fire another one.
                recent_charge = PaymentOrder.objects.filter(
                    tenant=tenant,
                    order_id__startswith='REC-',
                    created_at__gte=recent_bill_cutoff,
                ).exclude(status__in=('failed', 'cancelled')).exists()
                if recent_charge:
                    self.stdout.write(self.style.WARNING(
                        f'⚠️  {tenant.schema_name}: recurring charge already sent in the last 25 days '
                        f'(billing dates not advanced — check the payment webhook), skipping'
                    ))
                    logger.warning(
                        f'process_recurring_payments: skipping {tenant.schema_name} — '
                        f'recent recurring PaymentOrder exists but last_billed_at was not advanced'
                    )
                    skipped_count += 1
                    continue

                # Calculate amount based on subscription type
                # Use subscription.monthly_cost which handles feature-based pricing
                amount = float(subscription.monthly_cost)

                self.stdout.write(
                    f'📋 {tenant.schema_name}: Charging {amount} GEL for subscription '
                    f'(expires: {subscription.expires_at.date() if subscription.expires_at else "N/A"})'
                )

                if dry_run:
                    self.stdout.write(self.style.WARNING('   [DRY RUN] Would charge subscription'))
                    success_count += 1
                    continue

                # Generate new order ID for this charge
                new_order_id = f"REC-{uuid.uuid4().hex[:12].upper()}"

                # Charge the subscription (same amount as original payment)
                charge_result = bog_service.charge_subscription(
                    parent_order_id=subscription.parent_order_id,
                    callback_url=f"https://api.echodesk.ge/api/payments/webhook/",
                    external_order_id=new_order_id
                )

                # Create new payment order for tracking
                metadata = {
                    'type': 'recurring',
                    'parent_order_id': subscription.parent_order_id,
                    'subscription_id': subscription.id
                }

                new_payment_order = PaymentOrder.objects.create(
                    order_id=new_order_id,
                    bog_order_id=charge_result['order_id'],
                    tenant=tenant,
                    amount=amount,
                    currency='GEL',
                    agent_count=subscription.agent_count,  # Use actual agent count from subscription
                    status='pending',
                    card_saved=False,  # This is a charge, not a new card save
                    metadata=metadata
                )

                self.stdout.write(self.style.SUCCESS(
                    f'   ✓ Charged subscription: new_order_id={new_order_id}'
                ))
                success_count += 1

            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'   ✗ {tenant.schema_name}: Failed - {str(e)}'
                ))
                logger.error(f'Recurring payment failed for {tenant.schema_name}: {e}')
                failed_count += 1

                # TODO: Send notification email to tenant about failed payment
                # TODO: Implement grace period logic (e.g., 7 days before suspending)

        # Summary
        self.stdout.write(self.style.SUCCESS('\n' + '='*60))
        self.stdout.write(self.style.SUCCESS('Recurring Payment Processing Complete'))
        self.stdout.write(self.style.SUCCESS('='*60))
        self.stdout.write(f'✓ Success: {success_count}')
        self.stdout.write(f'✗ Failed:  {failed_count}')
        self.stdout.write(f'⊘ Skipped: {skipped_count} (no saved card)')
        self.stdout.write(self.style.SUCCESS('='*60))

        if not dry_run:
            logger.info(
                f'Recurring payments processed: '
                f'{success_count} success, {failed_count} failed, {skipped_count} skipped'
            )
