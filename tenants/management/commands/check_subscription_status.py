"""
Django management command to check subscription status and handle expirations

Run daily via cron:
0 3 * * * cd /path/to/project && python manage.py check_subscription_status
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from datetime import timedelta
from tenants.models import TenantSubscription, Tenant
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Check subscription status and send notifications for expiring/expired subscriptions'

    # Tenants with permanent subscriptions (excluded from expiration checks):
    # amanati (permanent customer) and groot (EchoDesk's own workspace).
    EXEMPT_TENANTS = ['amanati', 'groot']

    def add_arguments(self, parser):
        parser.add_argument(
            '--grace-days',
            type=int,
            default=7,
            help='Number of grace days after expiration before suspending (default: 7)',
        )

    def handle(self, *args, **options):
        grace_days = options['grace_days']
        now = timezone.now()

        self.stdout.write(self.style.SUCCESS(
            f'Checking subscription status (grace_days={grace_days})'
        ))

        # 1. Find subscriptions expiring in 7 days (reminder)
        seven_days_away = now + timedelta(days=7)
        expiring_soon = TenantSubscription.objects.filter(
            is_active=True,
            expires_at__date=seven_days_away.date(),
            tenant__is_active=True
        ).exclude(
            tenant__schema_name__in=self.EXEMPT_TENANTS
        ).select_related('tenant')

        for subscription in expiring_soon:
            self.stdout.write(f'📧 Reminder: {subscription.tenant.schema_name} expires in 7 days')
            self._send_expiration_reminder(subscription, days=7)

        # 2. Find subscriptions expiring in 3 days (urgent reminder)
        three_days_away = now + timedelta(days=3)
        expiring_urgent = TenantSubscription.objects.filter(
            is_active=True,
            expires_at__date=three_days_away.date(),
            tenant__is_active=True
        ).exclude(
            tenant__schema_name__in=self.EXEMPT_TENANTS
        ).select_related('tenant')

        for subscription in expiring_urgent:
            self.stdout.write(self.style.WARNING(
                f'⚠️  Urgent: {subscription.tenant.schema_name} expires in 3 days'
            ))
            self._send_expiration_reminder(subscription, days=3)

        # 3. Find subscriptions expired but within grace period
        grace_cutoff = now - timedelta(days=grace_days)
        in_grace_period = TenantSubscription.objects.filter(
            is_active=True,
            expires_at__lt=now,
            expires_at__gte=grace_cutoff,
            tenant__is_active=True
        ).exclude(
            tenant__schema_name__in=self.EXEMPT_TENANTS
        ).select_related('tenant')

        for subscription in in_grace_period:
            days_overdue = (now - subscription.expires_at).days
            self.stdout.write(self.style.WARNING(
                f'⏳ Grace period: {subscription.tenant.schema_name} '
                f'(expired {days_overdue} days ago)'
            ))
            self._send_grace_period_warning(subscription, days_overdue, grace_days)

        # 4. Find subscriptions past grace period (suspend)
        past_grace = TenantSubscription.objects.filter(
            is_active=True,
            expires_at__lt=grace_cutoff,
            tenant__is_active=True
        ).exclude(
            tenant__schema_name__in=self.EXEMPT_TENANTS
        ).select_related('tenant')

        suspended_count = 0
        for subscription in past_grace:
            days_overdue = (now - subscription.expires_at).days
            tenant = subscription.tenant

            self.stdout.write(self.style.ERROR(
                f'🔒 Suspending: {tenant.schema_name} ({days_overdue} days overdue)'
            ))

            # Suspend tenant
            tenant.is_active = False
            tenant.save()

            subscription.is_active = False
            subscription.save()

            self._send_suspension_notice(subscription)
            suspended_count += 1

        # Summary
        self.stdout.write(self.style.SUCCESS('\n' + '='*60))
        self.stdout.write(self.style.SUCCESS('Subscription Status Check Complete'))
        self.stdout.write(self.style.SUCCESS('='*60))
        self.stdout.write(f'📧 7-day reminders sent: {expiring_soon.count()}')
        self.stdout.write(f'⚠️  3-day urgent reminders: {expiring_urgent.count()}')
        self.stdout.write(f'⏳ In grace period: {in_grace_period.count()}')
        self.stdout.write(f'🔒 Suspended: {suspended_count}')
        self.stdout.write(self.style.SUCCESS('='*60))

        logger.info(
            f'Subscription status check complete: '
            f'{suspended_count} suspended, {in_grace_period.count()} in grace'
        )

    @staticmethod
    def _plan_summary(subscription):
        """Short plan description; plans are feature sets + a user count (no packages)."""
        users = subscription.agent_count or 0
        features = subscription.selected_features.count()
        return f'{users} users, {features} features'

    def _send_expiration_reminder(self, subscription, days):
        """Send reminder email about upcoming expiration"""
        tenant = subscription.tenant
        subject = f'EchoDesk: Your subscription expires in {days} days'
        message = f"""
Hello {tenant.admin_name},

Your EchoDesk subscription ({self._plan_summary(subscription)}) will expire in {days} days.

Expiration Date: {subscription.expires_at.strftime('%Y-%m-%d')}

To continue using EchoDesk without interruption, please ensure your payment method is up to date.

If you have a saved card, payment will be processed automatically. Otherwise, please visit your settings to update payment information.

Settings: https://{tenant.domain_url}/settings/subscription

Thank you,
EchoDesk Team
        """

        try:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [tenant.admin_email],
                fail_silently=False,
            )
            logger.info(f'Expiration reminder sent to {tenant.admin_email}')
        except Exception as e:
            logger.error(f'Failed to send reminder to {tenant.admin_email}: {e}')

    def _send_grace_period_warning(self, subscription, days_overdue, total_grace_days):
        """Send warning during grace period"""
        tenant = subscription.tenant
        days_remaining = total_grace_days - days_overdue
        subject = f'⚠️ EchoDesk: Payment Required - {days_remaining} days until suspension'
        message = f"""
Hello {tenant.admin_name},

Your EchoDesk subscription has expired, and payment is required.

Days Overdue: {days_overdue}
Days Until Suspension: {days_remaining}

Your account will be suspended if payment is not received within {days_remaining} days.

Please update your payment method immediately:
https://{tenant.domain_url}/settings/subscription

If you have questions, please contact support.

Thank you,
EchoDesk Team
        """

        try:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [tenant.admin_email],
                fail_silently=False,
            )
            logger.info(f'Grace period warning sent to {tenant.admin_email}')
        except Exception as e:
            logger.error(f'Failed to send warning to {tenant.admin_email}: {e}')

    def _send_suspension_notice(self, subscription):
        """Send notice about account suspension"""
        tenant = subscription.tenant
        subject = 'EchoDesk: Account Suspended - Payment Required'
        message = f"""
Hello {tenant.admin_name},

Your EchoDesk account has been suspended due to non-payment.

To reactivate your account, please update your payment method and pay the outstanding balance:
https://{tenant.domain_url}/settings/subscription

Your data is safe and will be restored immediately upon payment.

If you have questions or need assistance, please contact support@echodesk.ge

Thank you,
EchoDesk Team
        """

        try:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [tenant.admin_email],
                fail_silently=False,
            )
            logger.info(f'Suspension notice sent to {tenant.admin_email}')
        except Exception as e:
            logger.error(f'Failed to send suspension notice to {tenant.admin_email}: {e}')
