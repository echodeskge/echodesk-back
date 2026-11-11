"""
Management command to process trial subscription expirations

Runs daily to:
1. Find trials ending today or already expired
2. Charge the saved card automatically
3. Convert trial to paid subscription
4. Deactivate subscription if payment fails

Usage:
python manage.py process_trial_expirations
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import datetime, timedelta
from tenants.models import TenantSubscription, PaymentOrder, Tenant
from tenants.bog_payment import bog_service
from tenants.payment_views import create_subscription_event
import logging

BILLING_PERIOD_DAYS = 30

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Process trial subscription expirations and charge saved cards'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without actually processing payments',
        )
        parser.add_argument(
            '--days-before',
            type=int,
            default=0,
            help='Process trials expiring in N days (default: 0 = today)',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        days_before = options['days_before']

        self.stdout.write(f"Processing trial expirations (dry_run={dry_run}, days_before={days_before})")

        # Find trials ending today or in the past (not yet converted)
        expiration_date = timezone.now() + timedelta(days=days_before)
        expiring_trials = TenantSubscription.objects.filter(
            is_trial=True,
            trial_converted=False,
            is_active=True,
            trial_ends_at__lte=expiration_date
        ).select_related('tenant')

        total = expiring_trials.count()
        self.stdout.write(f"Found {total} trials to process")

        if total == 0:
            self.stdout.write(self.style.SUCCESS('No trials to process'))
            return

        processed = 0
        failed = 0

        for subscription in expiring_trials:
            tenant = subscription.tenant

            self.stdout.write(f"\nProcessing: {tenant.name} ({tenant.schema_name})")
            self.stdout.write(f"  Trial ends: {subscription.trial_ends_at}")
            self.stdout.write(f"  Agent count: {subscription.agent_count}")

            # Check if we have a saved card
            if not subscription.parent_order_id:
                self.stdout.write(self.style.ERROR(f"  ❌ No saved card found - cannot charge"))
                failed += 1
                # No card at trial end: deactivate. The subscription
                # middleware then attaches nothing, exactly as for an
                # expired paid subscription that was suspended.
                if not dry_run:
                    subscription.is_active = False
                    subscription.payment_status = 'no_card'
                    subscription.save()
                    create_subscription_event(
                        subscription=subscription,
                        event_type='trial_expired',
                        description='Trial ended without a saved card; subscription deactivated',
                        metadata={'trial_ends_at': subscription.trial_ends_at.isoformat()},
                    )
                    self.stdout.write(f"  Subscription deactivated")
                continue

            # Calculate subscription amount using monthly_cost
            amount = float(subscription.monthly_cost)

            self.stdout.write(f"  Amount to charge: {amount} GEL")

            if dry_run:
                self.stdout.write(self.style.WARNING(f"  [DRY RUN] Would charge {amount} GEL"))
                processed += 1
                continue

            # Attempt to charge the saved card
            try:
                # Generate order ID for this charge
                import uuid
                order_id = f"TRIAL-CONV-{uuid.uuid4().hex[:12].upper()}"

                self.stdout.write(f"  Charging saved card (order: {subscription.parent_order_id})...")

                # Charge the saved card
                charge_result = bog_service.charge_saved_card(
                    parent_order_id=subscription.parent_order_id,
                    amount=amount,
                    currency='GEL',
                    callback_url=f"https://api.echodesk.ge/api/payments/webhook/",
                    external_order_id=order_id
                )

                # Create payment order record
                payment_order = PaymentOrder.objects.create(
                    order_id=order_id,
                    bog_order_id=charge_result['order_id'],
                    tenant=tenant,
                    amount=amount,
                    currency='GEL',
                    agent_count=subscription.agent_count,
                    status='processing',
                    card_saved=False,  # Using parent's saved card
                    metadata={
                        'type': 'trial_conversion',
                        'parent_order_id': subscription.parent_order_id,
                        'trial_ends_at': subscription.trial_ends_at.isoformat()
                    }
                )

                self.stdout.write(self.style.SUCCESS(f"  ✓ Charge initiated: {charge_result['order_id']}"))

                # Mark trial as converted: a paid subscription billed every
                # 30 days from now by process_recurring_payments. The webhook
                # for this order ('trial_conversion') settles the payment.
                now = timezone.now()
                subscription.trial_converted = True
                subscription.is_trial = False
                subscription.subscription_type = 'paid'
                subscription.last_billed_at = now
                subscription.next_billing_date = now + timedelta(days=BILLING_PERIOD_DAYS)
                subscription.expires_at = now + timedelta(days=BILLING_PERIOD_DAYS)
                subscription.save()

                create_subscription_event(
                    subscription=subscription,
                    event_type='trial_converted',
                    payment_order=payment_order,
                    description=f'Trial ended; charged {amount} GEL on the saved card',
                    metadata={
                        'amount': str(amount),
                        'agent_count': subscription.agent_count,
                        'next_billing_date': subscription.next_billing_date.isoformat(),
                    },
                )

                self.stdout.write(self.style.SUCCESS(f"  ✓ Trial converted to paid subscription"))
                processed += 1

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ❌ Failed to charge: {str(e)}"))
                logger.error(f"Failed to charge trial conversion for {tenant.schema_name}: {e}")
                failed += 1

                # Deactivate subscription on payment failure
                subscription.is_active = False
                subscription.payment_status = 'failed'
                subscription.save()
                self.stdout.write(f"  Subscription deactivated due to payment failure")

        # Summary
        self.stdout.write(f"\n{'='*50}")
        self.stdout.write(f"Summary:")
        self.stdout.write(f"  Total: {total}")
        self.stdout.write(f"  Processed: {processed}")
        self.stdout.write(f"  Failed: {failed}")
        self.stdout.write(f"{'='*50}")

        if processed > 0:
            self.stdout.write(self.style.SUCCESS(f'Successfully processed {processed} trial expirations'))
        if failed > 0:
            self.stdout.write(self.style.WARNING(f'{failed} trial conversions failed'))
