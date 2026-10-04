from datetime import datetime, timedelta, time, timezone as dt_timezone
from decimal import Decimal

from django.utils import timezone

from .models import Booking, BookingSettings, StaffAvailability, StaffException

DEFAULT_TIMEZONE = 'Asia/Tbilisi'
DEFAULT_MIN_HOURS_BEFORE = 2
DEFAULT_MAX_DAYS_ADVANCE = 60

# Bookings in these states occupy their staff member's time.
ACTIVE_BOOKING_STATUSES = ['pending', 'confirmed', 'in_progress']


# ---------------------------------------------------------------------------
# Tenant settings / clock
# ---------------------------------------------------------------------------

def get_booking_settings():
    """Return the current tenant's BookingSettings row, or None."""
    try:
        from django.db import connection
        from tenants.models import Tenant
        tenant = Tenant.objects.get(schema_name=connection.schema_name)
        return BookingSettings.objects.filter(tenant=tenant).first()
    except Exception:
        return None


def get_or_create_booking_settings():
    """The current tenant's BookingSettings row, created with defaults if missing."""
    from django.db import connection
    from tenants.models import Tenant
    tenant = Tenant.objects.get(schema_name=connection.schema_name)
    booking_settings, _ = BookingSettings.objects.get_or_create(tenant=tenant)
    return booking_settings


def _tzinfo(booking_settings=None):
    name = getattr(booking_settings, 'timezone', None) or DEFAULT_TIMEZONE
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        # No tz database on the host (or a bad name): Georgia has no DST, so a
        # fixed +04:00 is the right fallback for the default zone.
        return dt_timezone(timedelta(hours=4))


def tenant_now(booking_settings=None):
    """Current wall-clock time at the business, as a naive datetime.

    Booking dates and times are stored naive, in the business's local time, so
    "is this in the past / too soon" must be judged against local time too —
    not UTC, which is four hours behind Tbilisi.
    """
    return timezone.now().astimezone(_tzinfo(booking_settings)).replace(tzinfo=None)


def check_booking_window(date, start_time, booking_settings=None):
    """Lead-time and advance-booking limits for customer-made bookings.

    Returns: (ok: bool, error_message: str)
    """
    min_hours = getattr(booking_settings, 'min_hours_before_booking', DEFAULT_MIN_HOURS_BEFORE)
    max_days = getattr(booking_settings, 'max_days_advance_booking', DEFAULT_MAX_DAYS_ADVANCE)
    now = tenant_now(booking_settings)

    if datetime.combine(date, start_time) < now + timedelta(hours=min_hours):
        return False, f"Bookings must be made at least {min_hours} hours in advance."
    if date > now.date() + timedelta(days=max_days):
        return False, f"Bookings cannot be made more than {max_days} days in advance."
    return True, ""


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

def _candidate_start_times(service, availability):
    """Start times a service could begin at within one staff member's day."""
    if service.booking_type == 'fixed_slots':
        slots = service.available_time_slots if isinstance(service.available_time_slots, list) else []
        times = []
        for slot_str in slots:
            try:
                times.append(datetime.strptime(slot_str, '%H:%M').time())
            except (ValueError, TypeError):
                continue
        return times

    if service.booking_type == 'duration_based':
        # A slot every 30 minutes (or the service duration, whichever is smaller)
        step = max(1, min(30, service.duration_minutes))
        start = time_to_minutes(availability['start_time'])
        end = time_to_minutes(availability['end_time'])
        return [minutes_to_time(m) for m in range(start, end, step)]

    return []


def _fits_working_hours(availability, start_time, total_minutes):
    """Whole appointment (service + buffer) inside working hours and clear of the break."""
    start = time_to_minutes(start_time)
    end = start + total_minutes

    if start < time_to_minutes(availability['start_time']):
        return False
    if end > time_to_minutes(availability['end_time']):
        return False

    break_start = availability.get('break_start')
    break_end = availability.get('break_end')
    if break_start and break_end:
        if start < time_to_minutes(break_end) and end > time_to_minutes(break_start):
            return False
    return True


def staff_can_perform(service, staff):
    """Staff member is bookable and assigned to this service."""
    return bool(staff.is_active_for_bookings) and service.staff_members.filter(pk=staff.pk).exists()


def generate_available_slots(service, date, staff=None, language='en', booking_settings=None):
    """
    Generate list of available time slots for a service on a given date

    Args:
        service: Service instance
        date: Date to check availability
        staff: Optional BookingStaff instance
        language: Language for error messages
        booking_settings: BookingSettings for the tenant (looked up if omitted)

    Returns:
        list: One dict per start time — start_time, end_time, staff_id,
        staff_name and available_staff (everyone free at that time).
    """
    if booking_settings is None:
        booking_settings = get_booking_settings()

    if staff is not None:
        if not staff_can_perform(service, staff):
            return []
        staff_members = [staff]
    else:
        staff_members = list(service.staff_members.filter(is_active_for_bookings=True))

    total_minutes = service.total_duration_minutes
    slots_by_time = {}

    for staff_member in staff_members:
        availability = get_staff_availability(staff_member, date)
        if not availability:
            continue

        for start_time in _candidate_start_times(service, availability):
            if not _fits_working_hours(availability, start_time, total_minutes):
                continue
            ok, _ = check_booking_window(date, start_time, booking_settings)
            if not ok:
                continue
            if is_slot_booked(staff_member, date, start_time, total_minutes):
                continue

            key = start_time.strftime('%H:%M')
            slot = slots_by_time.setdefault(key, {
                'start_time': key,
                'end_time': add_minutes_to_time(start_time, service.duration_minutes).strftime('%H:%M'),
                'available_staff': [],
            })
            slot['available_staff'].append({
                'staff_id': staff_member.id,
                'staff_name': public_staff_name(staff_member),
            })

    grouped_slots = []
    for key in sorted(slots_by_time.keys()):
        slot = slots_by_time[key]
        primary = slot['available_staff'][0]
        grouped_slots.append({
            'start_time': slot['start_time'],
            'end_time': slot['end_time'],
            'staff_id': primary['staff_id'],  # Primary staff
            'staff_name': primary['staff_name'],
            'available_staff': slot['available_staff'],  # All available staff at this time
        })

    return grouped_slots


def get_staff_availability(staff, date):
    """
    Get staff availability for a specific date

    Returns dict with start_time, end_time, break_start, break_end or None
    """
    day_of_week = date.weekday()

    weekly = StaffAvailability.objects.filter(
        staff=staff, day_of_week=day_of_week, is_available=True
    ).order_by('-id').first()

    # Exceptions for the date win. (.filter().first(), not .get(): nothing
    # stops the same day being entered twice, and that must not 500 the page.)
    exception = StaffException.objects.filter(staff=staff, date=date).order_by('-id').first()
    if exception is not None:
        if not exception.is_available:
            return None
        if exception.start_time and exception.end_time:
            return {
                'start_time': exception.start_time,
                'end_time': exception.end_time,
                'break_start': None,
                'break_end': None
            }
        if weekly is None:
            # Working on a normally-off day with no hours given
            return {
                'start_time': exception.start_time or time(9, 0),
                'end_time': exception.end_time or time(17, 0),
                'break_start': None,
                'break_end': None
            }
        # "Available" exception without hours: the normal day applies

    if weekly is None:
        return None
    return {
        'start_time': weekly.start_time,
        'end_time': weekly.end_time,
        'break_start': weekly.break_start,
        'break_end': weekly.break_end
    }


def is_slot_booked(staff, date, start_time, duration_minutes, exclude_booking_id=None):
    """
    Check if a time slot is already booked for staff
    """
    end_time = add_minutes_to_time(start_time, duration_minutes)

    # Check for overlapping bookings
    overlapping = Booking.objects.filter(
        staff=staff,
        date=date,
        status__in=ACTIVE_BOOKING_STATUSES
    ).filter(
        start_time__lt=end_time,
        end_time__gt=start_time
    )
    if exclude_booking_id is not None:
        overlapping = overlapping.exclude(pk=exclude_booking_id)

    return overlapping.exists()


def check_staff_slot(service, staff, date, start_time, exclude_booking_id=None, enforce_grid=False):
    """Can this staff member take this service at this time?

    enforce_grid: also require the start to be one of the times the slot list
    offers (customers may only book offered times; staff may book any time).

    Returns: (is_available: bool, error_message: str)
    """
    staff_availability = get_staff_availability(staff, date)
    if not staff_availability:
        return False, f"Staff {staff} is not available on this date"

    if enforce_grid:
        offered = {(t.hour, t.minute) for t in _candidate_start_times(service, staff_availability)}
        if (start_time.hour, start_time.minute) not in offered or start_time.second:
            return False, "This time is not available"

    # Check if time is within working hours
    if not is_time_in_range(start_time, staff_availability['start_time'], staff_availability['end_time']):
        return False, f"Time is outside staff working hours ({staff_availability['start_time']} - {staff_availability['end_time']})"

    # Check if end time is within working hours
    total_minutes = service.total_duration_minutes
    if time_to_minutes(start_time) + total_minutes > time_to_minutes(staff_availability['end_time']):
        return False, "Booking would extend beyond staff working hours"

    # Check the whole appointment against the break, not just its start
    if not _fits_working_hours(staff_availability, start_time, total_minutes):
        return False, "Time conflicts with staff break time"

    # Check if slot is already booked
    if is_slot_booked(staff, date, start_time, total_minutes, exclude_booking_id=exclude_booking_id):
        return False, "This time slot is already booked"

    return True, ""


def find_available_staff(service, date, start_time, exclude_booking_id=None, enforce_grid=False):
    """First active staff member of the service who is free at this time, or None."""
    for staff_member in service.staff_members.filter(is_active_for_bookings=True).order_by('id'):
        ok, _ = check_staff_slot(
            service, staff_member, date, start_time,
            exclude_booking_id=exclude_booking_id, enforce_grid=enforce_grid,
        )
        if ok:
            return staff_member
    return None


def validate_booking_availability(service, staff, date, start_time, exclude_booking_id=None,
                                  booking_settings=None, enforce_grid=False):
    """
    Validate if booking can be made

    With no staff given, the booking is possible when any active staff member
    of the service is free at that time.

    Returns: (is_available: bool, error_message: str)
    """
    now = tenant_now(booking_settings if booking_settings is not None else get_booking_settings())

    # Check if date is in the past
    if date < now.date():
        return False, "Cannot book in the past"

    # Check if date is today and time has passed
    if date == now.date() and start_time <= now.time():
        return False, "Cannot book a time that has already passed"

    if staff:
        return check_staff_slot(
            service, staff, date, start_time,
            exclude_booking_id=exclude_booking_id, enforce_grid=enforce_grid,
        )

    if not service.staff_members.filter(is_active_for_bookings=True).exists():
        return False, "No staff available for this service"
    if find_available_staff(
        service, date, start_time, exclude_booking_id=exclude_booking_id, enforce_grid=enforce_grid
    ) is None:
        return False, "This time is not available"
    return True, ""


from .utils_text import localized_text, public_staff_name  # noqa: E402,F401  (re-exported)


def is_time_in_range(check_time, start_time, end_time):
    """Check if time is within range"""
    return start_time <= check_time < end_time


def time_to_minutes(t):
    """Convert time to minutes since midnight"""
    return t.hour * 60 + t.minute


def minutes_to_time(minutes):
    """Convert minutes since midnight to a time object"""
    return time(minutes // 60, minutes % 60)


def add_minutes_to_time(t, minutes):
    """Add minutes to a time object"""
    dt = datetime.combine(datetime.today(), t)
    dt = dt + timedelta(minutes=minutes)
    return dt.time()


def calculate_refund_amount(booking, settings):
    """
    Calculate refund amount based on cancellation policy

    Args:
        booking: Booking instance
        settings: BookingSettings instance

    Returns:
        Decimal: Refund amount
    """
    paid = Decimal(str(booking.paid_amount or 0))
    if paid == 0:
        return Decimal('0')

    # Check cancellation policy
    if settings.refund_policy == 'full':
        return paid
    elif settings.refund_policy == 'partial_50':
        return (paid * Decimal('0.5')).quantize(Decimal('0.01'))
    elif settings.refund_policy == 'partial_25':
        return (paid * Decimal('0.25')).quantize(Decimal('0.01'))
    else:  # no_refund
        return Decimal('0')


def can_cancel_booking(booking, settings):
    """
    Check if booking can be cancelled based on time policy

    Returns: (can_cancel: bool, reason: str)
    """
    if booking.status in ['completed', 'cancelled']:
        return False, "Booking is already completed or cancelled"

    # Calculate time until booking
    booking_datetime = datetime.combine(booking.date, booking.start_time)
    time_until_booking = booking_datetime - tenant_now(settings)

    # Check minimum cancellation time
    min_hours = timedelta(hours=settings.cancellation_hours_before)
    if time_until_booking < min_hours:
        return False, f"Bookings must be cancelled at least {settings.cancellation_hours_before} hours in advance"

    return True, ""


# ---------------------------------------------------------------------------
# Payment options
# ---------------------------------------------------------------------------

def card_payment_enabled(booking_settings):
    """Online card payment is on only when the tenant allows card payment and
    has saved its BOG credentials. (The dashboard exposes exactly these two
    things; the legacy `payment_method` field is not editable there.)"""
    return bool(
        booking_settings
        and booking_settings.allow_card_payment
        and booking_settings.bog_client_id
        and booking_settings.bog_client_secret
    )


def available_payment_options(booking_settings, service=None):
    """Payment choices a customer may pick for a booking.

    'cash'    — pay at the venue (no online payment)
    'deposit' — pay the service's deposit online now, the rest at the venue
    'full'    — pay the full price online now
    """
    if service is not None and not service.base_price:
        return ['cash']  # free service: nothing to charge

    card_ok = card_payment_enabled(booking_settings)
    cash_ok = booking_settings is None or booking_settings.allow_cash_payment
    if booking_settings is not None and booking_settings.require_deposit and card_ok:
        cash_ok = False

    options = []
    if cash_ok:
        options.append('cash')
    if card_ok:
        if service is None or 0 < service.deposit_percentage < 100:
            options.append('deposit')
        options.append('full')

    # Never leave a business unbookable because of a settings combination.
    return options or ['cash']
