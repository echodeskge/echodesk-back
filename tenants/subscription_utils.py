"""
Subscription Management Utility Functions

Helper functions for payment retry scheduling, subscription health checks,
metrics calculation, and other subscription-related operations.
"""
import logging
from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from django.db.models import Sum, Count, Q, Avg
from .models import (
    TenantSubscription,
    PaymentOrder,
    PaymentAttempt,
    PaymentRetrySchedule,
    SubscriptionEvent,
    PlatformMetrics,
    Tenant,
)
from .telegram_notifications import (
    notify_retry_scheduled, notify_all_retries_exhausted, notify_subscription_suspended
)

logger = logging.getLogger(__name__)


def schedule_payment_retries(payment_order, original_attempt):
    """
    Schedule automatic retries for a failed payment

    Retry schedule:
    - Retry 1: 4 hours after failure
    - Retry 2: 3 days after failure
    - Retry 3: 7 days after failure

    Only applies to BOG tenants — Paddle manages retries automatically.

    Args:
        payment_order: PaymentOrder instance that failed
        original_attempt: PaymentAttempt instance of the failed payment

    Returns:
        List of created PaymentRetrySchedule instances
    """
    # Skip retry scheduling for providers that manage billing automatically
    tenant = payment_order.tenant
    if tenant and getattr(tenant, 'payment_provider', 'bog') != 'bog':
        logger.info(f'Skipping retry scheduling for {tenant.name}: provider={tenant.payment_provider} manages retries')
        return []

    now = timezone.now()
    subscription = payment_order.tenant.subscription

    # Define retry schedule
    retry_schedule_config = [
        (1, timedelta(hours=4)),    # Retry 1: 4 hours later
        (2, timedelta(days=3)),     # Retry 2: 3 days later
        (3, timedelta(days=7)),     # Retry 3: 7 days later
    ]

    created_retries = []

    for retry_num, time_delta in retry_schedule_config:
        retry = PaymentRetrySchedule.objects.create(
            payment_order=payment_order,
            subscription=subscription,
            tenant=payment_order.tenant,
            original_attempt=original_attempt,
            retry_number=retry_num,
            scheduled_for=now + time_delta,
            status='pending',
        )
        created_retries.append(retry)

        logger.info(
            f"Scheduled retry #{retry_num} for payment {payment_order.order_id} "
            f"at {retry.scheduled_for}"
        )

    # Log event
    SubscriptionEvent.objects.create(
        subscription=subscription,
        tenant=payment_order.tenant,
        event_type='retry_scheduled',
        payment_order=payment_order,
        payment_attempt=original_attempt,
        description=f"Scheduled {len(created_retries)} payment retries",
        metadata={
            'retry_count': len(created_retries),
            'failed_amount': str(payment_order.amount),
        }
    )

    # Send Telegram notification for the first retry being scheduled
    try:
        if created_retries:
            notify_retry_scheduled(subscription, created_retries[0])
    except Exception as e:
        logger.error(f'Failed to send Telegram notification for retry scheduled: {e}')

    # Update subscription payment status
    subscription.payment_status = 'retrying'
    subscription.mark_payment_failed()

    return created_retries


def cancel_pending_retries(subscription, reason='Payment succeeded'):
    """
    Cancel all pending retries for a subscription

    Args:
        subscription: TenantSubscription instance
        reason: Reason for cancellation

    Returns:
        Number of retries cancelled
    """
    pending_retries = PaymentRetrySchedule.objects.filter(
        subscription=subscription,
        status='pending'
    )

    count = pending_retries.count()

    pending_retries.update(
        status='cancelled',
        skip_reason=reason,
        executed_at=timezone.now()
    )

    if count > 0:
        logger.info(f"Cancelled {count} pending retries for {subscription.tenant.name}: {reason}")

    return count


def execute_retry(retry_schedule):
    """
    Execute a scheduled payment retry (BOG only)

    Args:
        retry_schedule: PaymentRetrySchedule instance to execute

    Returns:
        dict with 'success' bool and 'bog_order_id' or 'error' message
    """
    # Skip execution for non-BOG tenants
    tenant = retry_schedule.tenant
    if getattr(tenant, 'payment_provider', 'bog') != 'bog':
        retry_schedule.status = 'skipped'
        retry_schedule.skip_reason = f'Provider {tenant.payment_provider} manages retries automatically'
        retry_schedule.save()
        return {'success': False, 'error': retry_schedule.skip_reason}

    from .bog_payment import bog_service

    # Mark as executing
    retry_schedule.status = 'executing'
    retry_schedule.executed_at = timezone.now()
    retry_schedule.save()

    subscription = retry_schedule.subscription
    payment_order = retry_schedule.payment_order

    # Guard: never retry a charge for a period that's already paid. Once any
    # charge settles (via webhook OR the reconcile_pending_payments job),
    # next_billing_date moves into the future — a pending retry must NOT fire
    # a second charge. This is what double-charged artlighthouse in Aug 2026:
    # the Aug 23 charge settled at BOG but the webhook never arrived, billing
    # never advanced, and this retry path charged the card again on Aug 25.
    if subscription.next_billing_date and subscription.next_billing_date > timezone.now():
        retry_schedule.status = 'skipped'
        retry_schedule.skip_reason = 'Subscription already paid for the current period (next_billing_date in the future)'
        retry_schedule.save()
        return {'success': False, 'error': retry_schedule.skip_reason}

    try:
        # Generate new external order ID for retry
        external_order_id = f"RETRY-{retry_schedule.id}-{payment_order.order_id}"

        # Call BOG to charge saved card. Callback MUST hit the backend API host
        # (api.echodesk.ge) — app.echodesk.ge is the frontend and silently drops
        # the webhook, leaving the charge stuck 'pending' (Aug 2026 incident).
        result = bog_service.charge_subscription(
            parent_order_id=subscription.parent_order_id,
            callback_url=f"https://api.echodesk.ge/api/payments/webhook/",
            external_order_id=external_order_id
        )

        # Create new payment attempt
        attempt = PaymentAttempt.objects.create(
            payment_order=payment_order,
            subscription=subscription,
            tenant=subscription.tenant,
            attempt_number=retry_schedule.retry_number + 1,
            bog_order_id=result['order_id'],
            amount=payment_order.amount,
            status='pending',
            is_retry=True,
            parent_attempt=retry_schedule.original_attempt,
        )

        # Link retry to attempt
        retry_schedule.retry_attempt = attempt
        retry_schedule.save()

        # Log event
        SubscriptionEvent.objects.create(
            subscription=subscription,
            tenant=subscription.tenant,
            event_type='retry_scheduled',
            payment_order=payment_order,
            payment_attempt=attempt,
            description=f"Retry attempt #{retry_schedule.retry_number} initiated",
            metadata={
                'bog_order_id': result['order_id'],
                'requires_auth': result.get('requires_authentication', False),
            }
        )

        logger.info(
            f"Executed retry #{retry_schedule.retry_number} for {subscription.tenant.name}, "
            f"BOG order: {result['order_id']}"
        )

        return {
            'success': True,
            'bog_order_id': result['order_id'],
            'attempt': attempt,
        }

    except Exception as e:
        logger.error(f"Retry execution failed: {e}", exc_info=True)

        retry_schedule.status = 'failed'
        retry_schedule.skip_reason = str(e)
        retry_schedule.save()

        return {
            'success': False,
            'error': str(e),
        }


def calculate_mrr(date=None):
    """
    Calculate Monthly Recurring Revenue

    Args:
        date: Date to calculate MRR for (defaults to today)

    Returns:
        Decimal: Total MRR in GEL
    """
    if not date:
        date = timezone.now().date()

    # Get all active subscriptions
    active_subs = TenantSubscription.objects.filter(
        is_active=True
    )

    total_mrr = Decimal(0)

    for sub in active_subs:
        total_mrr += Decimal(str(sub.monthly_cost))

    return total_mrr


def calculate_churn_rate(start_date, end_date):
    """
    Calculate churn rate for a period

    Churn rate = (Cancelled subscriptions / Total at start) * 100

    Args:
        start_date: Period start date
        end_date: Period end date

    Returns:
        Decimal: Churn rate percentage
    """
    # Get subscriptions active at start
    active_at_start = TenantSubscription.objects.filter(
        created_at__lte=start_date,
        is_active=True
    ).count()

    if active_at_start == 0:
        return Decimal(0)

    # Get subscriptions cancelled in period
    cancelled_in_period = SubscriptionEvent.objects.filter(
        event_type='cancelled',
        created_at__range=[start_date, end_date]
    ).values('subscription').distinct().count()

    churn_rate = (Decimal(cancelled_in_period) / Decimal(active_at_start)) * 100

    return round(churn_rate, 2)


def get_subscription_health(subscription):
    """
    Get comprehensive health status for a subscription

    Args:
        subscription: TenantSubscription instance

    Returns:
        dict with health metrics
    """
    # Get payment attempts
    total_attempts = PaymentAttempt.objects.filter(
        subscription=subscription
    ).count()

    failed_attempts = PaymentAttempt.objects.filter(
        subscription=subscription,
        status='failed'
    ).count()

    success_rate = 0
    if total_attempts > 0:
        success_rate = ((total_attempts - failed_attempts) / total_attempts) * 100

    # Get pending retries
    pending_retries = PaymentRetrySchedule.objects.filter(
        subscription=subscription,
        status='pending'
    ).count()

    # Check if overdue
    is_overdue = False
    if subscription.next_billing_date:
        is_overdue = timezone.now() > subscription.next_billing_date

    return {
        'payment_status': subscription.payment_status,
        'payment_health': subscription.payment_health_status,
        'has_saved_card': bool(subscription.parent_order_id),
        'failed_payment_count': subscription.failed_payment_count,
        'last_payment_failure': subscription.last_payment_failure,
        'total_payment_attempts': total_attempts,
        'failed_payment_attempts': failed_attempts,
        'payment_success_rate': round(success_rate, 2),
        'pending_retries': pending_retries,
        'is_overdue': is_overdue,
        'days_until_next_billing': subscription.days_until_next_billing,
        'monthly_cost': subscription.monthly_cost,
    }


def get_failed_payments_summary():
    """
    Get summary of all failed payments needing attention

    Returns:
        dict with failed payment statistics
    """
    # Subscriptions with payment issues
    problem_subs = TenantSubscription.objects.filter(
        payment_status__in=['retrying', 'failed', 'overdue']
    )

    # Pending retries
    pending_retries = PaymentRetrySchedule.objects.filter(
        status='pending'
    )

    # Overdue retries
    overdue_retries = pending_retries.filter(
        scheduled_for__lte=timezone.now()
    )

    # Recent failures (last 7 days)
    week_ago = timezone.now() - timedelta(days=7)
    recent_failures = PaymentAttempt.objects.filter(
        status='failed',
        attempted_at__gte=week_ago
    )

    return {
        'total_problem_subscriptions': problem_subs.count(),
        'retrying_count': problem_subs.filter(payment_status='retrying').count(),
        'failed_count': problem_subs.filter(payment_status='failed').count(),
        'overdue_count': problem_subs.filter(payment_status='overdue').count(),
        'pending_retries': pending_retries.count(),
        'overdue_retries': overdue_retries.count(),
        'recent_failures_7d': recent_failures.count(),
    }


def format_payment_status_badge(status):
    """
    Get HTML badge class for payment status

    Args:
        status: Payment status string

    Returns:
        str: CSS class for badge
    """
    badge_map = {
        'current': 'success',
        'overdue': 'warning',
        'retrying': 'info',
        'failed': 'danger',
        'no_card': 'secondary',
    }
    return badge_map.get(status, 'secondary')


def get_upcoming_billings(days=7):
    """
    Get subscriptions with upcoming billing in next N days

    Args:
        days: Number of days to look ahead

    Returns:
        QuerySet of TenantSubscription
    """
    today = timezone.now()
    future_date = today + timedelta(days=days)

    return TenantSubscription.objects.filter(
        is_active=True,
        next_billing_date__range=[today, future_date]
    ).order_by('next_billing_date')


def suspend_subscription_for_payment_failure(subscription):
    """
    Suspend a subscription due to payment failure

    Args:
        subscription: TenantSubscription to suspend
    """
    subscription.is_active = False
    subscription.payment_status = 'failed'
    subscription.save()

    # Deactivate tenant
    subscription.tenant.is_active = False
    subscription.tenant.save()

    # Log event
    SubscriptionEvent.objects.create(
        subscription=subscription,
        tenant=subscription.tenant,
        event_type='suspended',
        description='Subscription suspended due to payment failure after all retries exhausted',
        metadata={
            'failed_count': subscription.failed_payment_count,
            'last_failure': str(subscription.last_payment_failure),
        }
    )

    # Send Telegram notification
    try:
        notify_subscription_suspended(subscription, reason='All payment retries exhausted')
    except Exception as e:
        logger.error(f'Failed to send Telegram notification for suspension: {e}')

    logger.warning(f"Suspended subscription for {subscription.tenant.name} due to payment failure")
