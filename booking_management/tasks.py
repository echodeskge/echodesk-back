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
    Send the reminders that are due right now, in every tenant.

    Runs every few minutes; each salon chooses how many hours before the
    visit its customers are reminded (see due_reminder).
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


# Reminders go out only in these hours on the business's clock; one that
# falls due at night is sent in the morning if the visit is still ahead.
REMINDER_SEND_FROM_HOUR = 9
REMINDER_SEND_UNTIL_HOUR = 21
DEFAULT_REMINDER_HOURS = 24
# A reminder that keeps failing (provider down) is retried on later runs,
# but not forever.
REMINDER_MAX_ATTEMPTS = 3


def due_reminder(booking, now, booking_settings=None):
    """Which reminder a confirmed booking is due right now: 'first', 'second' or None.

    `now` is the business's wall-clock time (naive), as booking times are.
    """
    from datetime import datetime
    from booking_management.utils import _tzinfo

    if booking.status != 'confirmed':
        return None
    if not (REMINDER_SEND_FROM_HOUR <= now.hour < REMINDER_SEND_UNTIL_HOUR):
        return None
    start = datetime.combine(booking.date, booking.start_time)
    if start <= now:
        return None

    first_hours = getattr(booking_settings, 'reminder_hours_before', None) or DEFAULT_REMINDER_HOURS
    second_hours = getattr(booking_settings, 'second_reminder_hours_before', None)
    # When the booking was made, on the same clock
    created = booking.created_at.astimezone(_tzinfo(booking_settings)).replace(tzinfo=None) if booking.created_at else None

    def due(hours):
        if now < start - timedelta(hours=hours):
            return False
        # Booked inside the window: the confirmation they just got is enough
        return created is None or created <= start - timedelta(hours=hours)

    if second_hours and not booking.second_reminder_sent and due(second_hours):
        return 'second'
    if not booking.reminder_sent and due(first_hours):
        # ...unless the closer reminder already covers it
        if second_hours and now >= start - timedelta(hours=second_hours):
            return None
        return 'first'
    return None


def _send_reminders_for_tenant(schema_name):
    """Send due booking reminders (email and SMS) within one tenant schema."""
    from django.core.cache import cache
    from booking_management.emails import booking_recipient, send_booking_email
    from booking_management.models import Booking
    from booking_management.sms import send_booking_sms
    from booking_management.utils import get_booking_settings, tenant_now

    booking_settings = get_booking_settings()
    now = tenant_now(booking_settings)
    if not (REMINDER_SEND_FROM_HOUR <= now.hour < REMINDER_SEND_UNTIL_HOUR):
        return 0

    first_hours = getattr(booking_settings, 'reminder_hours_before', None) or DEFAULT_REMINDER_HOURS
    horizon = (now + timedelta(hours=first_hours)).date()
    bookings = Booking.objects.filter(
        date__gte=now.date(),
        date__lte=horizon,
        status='confirmed',
    ).exclude(
        reminder_sent=True, second_reminder_sent=True,
    ).select_related('client', 'service', 'staff', 'staff__user')

    reminded = 0
    for booking in bookings:
        try:
            which = due_reminder(booking, now, booking_settings)
            if which is None:
                continue
            flag = 'reminder_sent' if which == 'first' else 'second_reminder_sent'

            language = booking.contact_language or None
            has_email = bool(booking_recipient(booking))
            emailed = send_booking_email(booking, 'reminder', schema_name, language) if has_email else False
            sms = send_booking_sms(booking, 'reminder', language, schema_name)
            delivered = emailed or sms == 'sent'

            # Something we could have sent failed: try again on a later run
            failed = (has_email and not emailed) or sms == 'failed'
            if not delivered and failed:
                attempts_key = f'booking_reminder_attempts:{schema_name}:{booking.id}:{which}'
                attempts = (cache.get(attempts_key) or 0) + 1
                cache.set(attempts_key, attempts, 60 * 60 * 48)
                if attempts < REMINDER_MAX_ATTEMPTS:
                    continue

            setattr(booking, flag, True)
            fields = [flag, 'updated_at']
            if which == 'second' and not booking.reminder_sent:
                booking.reminder_sent = True  # the earlier one is moot now
                fields.append('reminder_sent')
            booking.save(update_fields=fields)
            if delivered:
                reminded += 1
                logger.info(
                    'Reminder (%s) for booking %s on %s at %s (tenant %s): email=%s sms=%s',
                    which, booking.booking_number, booking.date, booking.start_time, schema_name, emailed, sms,
                )
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
                    payment_service.last_state = None
                    payment_service.process_webhook({'body': {'external_order_id': booking.booking_number}})
                    booking.refresh_from_db()
                    if payment_service.last_state not in ('pending', 'failed'):
                        # paid (handled), still processing (customer mid-3DS), or
                        # BOG couldn't be asked: don't give the slot away blind.
                        continue
                except Exception:
                    logger.exception(
                        'Could not verify payment for booking %s (tenant %s); leaving it',
                        booking.booking_number, schema_name,
                    )
                    continue
                if booking.status != 'pending' or booking.payment_status in ('deposit_paid', 'fully_paid'):
                    continue  # paid meanwhile, or already cancelled (declined card)

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


@shared_task
def send_booking_email_task(schema_name, booking_id, kind, language=None):
    """Send a booking notice to its client: email, and SMS if the salon uses it.

    kind: 'created' | 'confirmed' | 'cancelled' | 'rescheduled'. (The name is
    historical; tasks may be queued under it.)
    """
    from tenant_schemas.utils import schema_context

    try:
        with schema_context(schema_name):
            from booking_management.emails import send_booking_email
            from booking_management.models import Booking

            booking = Booking.objects.select_related('client', 'service', 'staff', 'staff__user').filter(id=booking_id).first()
            if booking is None:
                return False

            language = language or booking.contact_language
            if not language:
                try:
                    from tenants.models import Tenant
                    language = Tenant.objects.get(schema_name=schema_name).preferred_language
                except Exception:
                    language = 'en'

            emailed = send_booking_email(booking, kind, schema_name, language)
            # SMS goes out on its own switch, whether or not there was an email
            from booking_management.sms import send_booking_sms
            texted = send_booking_sms(booking, kind, language, schema_name) == 'sent'
            return emailed or texted
    except Exception:
        # send_booking_email swallows mail errors itself; anything reaching
        # here is a bug or a missing schema, which a retry would not fix.
        logger.exception('Booking email task failed (booking %s, %s)', booking_id, kind)
        return False
