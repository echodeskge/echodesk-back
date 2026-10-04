import logging
from datetime import datetime, timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def create_recurring_bookings():
    """
    Auto-create bookings from active recurring booking records.

    Iterates over all tenant schemas and for each active RecurringBooking
    whose next_booking_date <= today, creates a new Booking and advances
    the schedule.
    """
    from tenant_schemas.utils import schema_context
    from tenants.models import Tenant

    tenants = Tenant.objects.exclude(schema_name='public')
    total_created = 0

    for tenant in tenants:
        try:
            with schema_context(tenant.schema_name):
                created = _create_recurring_bookings_for_tenant(tenant.schema_name)
                total_created += created
        except Exception:
            logger.exception(
                'Error creating recurring bookings for tenant %s',
                tenant.schema_name,
            )

    logger.info('create_recurring_bookings completed: %d bookings created', total_created)
    return total_created


def _create_recurring_bookings_for_tenant(schema_name):
    """Create recurring bookings within a single tenant schema context."""
    from booking_management.models import Booking, RecurringBooking

    today = timezone.now().date()
    recurring_qs = RecurringBooking.objects.filter(
        status='active',
        next_booking_date__lte=today,
    ).select_related('client', 'service', 'staff')

    created = 0
    for recurring in recurring_qs:
        try:
            if not recurring.should_create_booking():
                continue

            service = recurring.service
            start_time = recurring.preferred_time

            # Calculate end_time from service duration
            start_dt = datetime.combine(recurring.next_booking_date, start_time)
            end_dt = start_dt + timedelta(minutes=service.duration_minutes)
            end_time = end_dt.time()

            booking = Booking.objects.create(
                client=recurring.client,
                service=service,
                staff=recurring.staff,
                date=recurring.next_booking_date,
                start_time=start_time,
                end_time=end_time,
                status='confirmed',
                payment_status='pending',
                total_amount=service.base_price,
                deposit_amount=service.calculate_deposit_amount(),
                client_notes=f'Auto-created from recurring booking #{recurring.id}',
            )

            # Advance the recurring booking schedule
            recurring.last_created_booking = booking
            recurring.current_occurrences += 1
            recurring.next_booking_date = recurring.calculate_next_date()

            # Mark completed if max occurrences reached
            if (
                recurring.max_occurrences
                and recurring.current_occurrences >= recurring.max_occurrences
            ):
                recurring.status = 'completed'

            # Mark completed if past end date
            if recurring.end_date and recurring.next_booking_date > recurring.end_date:
                recurring.status = 'completed'

            recurring.save(update_fields=[
                'last_created_booking',
                'current_occurrences',
                'next_booking_date',
                'status',
                'updated_at',
            ])

            created += 1
            logger.info(
                'Created booking %s from recurring #%d for tenant %s',
                booking.booking_number,
                recurring.id,
                schema_name,
            )
        except Exception:
            logger.exception(
                'Error creating booking from recurring #%d for tenant %s',
                recurring.id,
                schema_name,
            )

    return created


@shared_task
def send_booking_reminders():
    """
    Send reminders for bookings happening in the next 24 hours.

    Finds all confirmed bookings with date = tomorrow that haven't been
    reminded yet, and marks them as reminded.  Actual email/SMS delivery
    can be plugged in later.
    """
    from tenant_schemas.utils import schema_context
    from tenants.models import Tenant

    tenants = Tenant.objects.exclude(schema_name='public')
    total_reminded = 0

    for tenant in tenants:
        try:
            with schema_context(tenant.schema_name):
                reminded = _send_reminders_for_tenant(tenant.schema_name)
                total_reminded += reminded
        except Exception:
            logger.exception(
                'Error sending booking reminders for tenant %s',
                tenant.schema_name,
            )

    logger.info('send_booking_reminders completed: %d reminders sent', total_reminded)
    return total_reminded


def _send_reminders_for_tenant(schema_name):
    """Send booking reminders within a single tenant schema context."""
    from booking_management.models import Booking

    tomorrow = timezone.now().date() + timedelta(days=1)
    bookings = Booking.objects.filter(
        date=tomorrow,
        status='confirmed',
        reminder_sent=False,
    ).select_related('client', 'service', 'staff')

    reminded = 0
    for booking in bookings:
        try:
            # TODO: integrate actual email/SMS sending here
            logger.info(
                'Reminder for booking %s: %s on %s at %s (tenant %s)',
                booking.booking_number,
                booking.client.full_name,
                booking.date,
                booking.start_time,
                schema_name,
            )

            booking.reminder_sent = True
            booking.save(update_fields=['reminder_sent', 'updated_at'])
            reminded += 1
        except Exception:
            logger.exception(
                'Error sending reminder for booking %s (tenant %s)',
                booking.booking_number,
                schema_name,
            )

    return reminded


# How long a customer has to complete an online card payment before the
# booking is released and the slot becomes bookable again.
UNPAID_CARD_GRACE_MINUTES = 30


@shared_task
def cancel_unpaid_bookings():
    """
    Release bookings whose online card payment was never completed.

    Only bookings the customer chose to pay by card are touched. Bookings to
    be paid at the venue (and ones created by staff) wait for the business to
    confirm them, however long that takes.
    """
    from tenant_schemas.utils import schema_context
    from tenants.models import Tenant

    tenants = Tenant.objects.exclude(schema_name='public')
    total_cancelled = 0

    for tenant in tenants:
        try:
            with schema_context(tenant.schema_name):
                cancelled = _cancel_unpaid_for_tenant(tenant.schema_name)
                total_cancelled += cancelled
        except Exception:
            logger.exception(
                'Error cancelling unpaid bookings for tenant %s',
                tenant.schema_name,
            )

    logger.info('cancel_unpaid_bookings completed: %d bookings cancelled', total_cancelled)
    return total_cancelled


def _cancel_unpaid_for_tenant(schema_name):
    """Cancel unpaid card bookings within a single tenant schema context."""
    from booking_management.models import Booking

    grace_cutoff = timezone.now() - timedelta(minutes=UNPAID_CARD_GRACE_MINUTES)
    bookings = Booking.objects.filter(
        status='pending',
        payment_method='card',
        payment_status__in=['pending', 'failed'],
        created_at__lt=grace_cutoff,
    ).select_related('client', 'service')

    cancelled = 0
    payment_service = None
    for booking in bookings:
        try:
            # The payment may have gone through with the callback lost — ask
            # BOG before giving the slot away.
            if booking.bog_order_id and booking.payment_status == 'pending':
                try:
                    if payment_service is None:
                        from booking_management.payment_service import get_booking_payment_service
                        payment_service = get_booking_payment_service()
                    payment_service.process_webhook({'body': {'external_order_id': booking.booking_number}})
                    booking.refresh_from_db()
                except Exception:
                    logger.exception(
                        'Could not verify payment for booking %s (tenant %s); leaving it',
                        booking.booking_number, schema_name,
                    )
                    continue
                if booking.status != 'pending' or booking.payment_status in ('deposit_paid', 'fully_paid'):
                    continue

            booking.cancel(
                cancelled_by='admin',
                reason='Auto-cancelled: online payment was not completed',
                notify=False,
            )
            cancelled += 1
            logger.info(
                'Auto-cancelled unpaid booking %s (tenant %s)',
                booking.booking_number,
                schema_name,
            )
        except Exception:
            logger.exception(
                'Error cancelling booking %s (tenant %s)',
                booking.booking_number,
                schema_name,
            )

    return cancelled


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_booking_email_task(self, schema_name, booking_id, kind, language=None):
    """Email a booking notice ('created' | 'confirmed' | 'cancelled') to its client."""
    from tenant_schemas.utils import schema_context

    try:
        with schema_context(schema_name):
            from booking_management.emails import send_booking_email
            from booking_management.models import Booking

            booking = Booking.objects.select_related('client', 'service', 'staff', 'staff__user').filter(id=booking_id).first()
            if booking is None or not booking.client.email:
                return False

            if not language:
                try:
                    from tenants.models import Tenant
                    language = Tenant.objects.get(schema_name=schema_name).preferred_language
                except Exception:
                    language = 'en'

            return send_booking_email(booking, kind, schema_name, language)
    except Exception as exc:
        logger.exception('Booking email task failed (booking %s, %s)', booking_id, kind)
        raise self.retry(exc=exc)
