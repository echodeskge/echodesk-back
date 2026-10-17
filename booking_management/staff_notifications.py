"""
In-app (WebSocket + push) notifications to the business's staff about bookings.

Two audiences:
- notify_staff_of_booking: things CUSTOMERS do on the public booking site
  (new booking, cancellation, time change). Goes to everyone who runs the
  business (is_staff / superuser) plus the assigned staff member.
- notify_assigned_staff: things that change a staff member's own agenda
  (a colleague booked / moved / confirmed / cancelled one of their clients,
  a booking was handed to them, their next client is coming). Goes to the
  assigned staff member only, never to the person who made the change.

Every notification carries link_url '/bookings/bookings/<id>' and metadata
{booking_id, booking_number, ...}. Texts follow each recipient's language
(users.notification_texts.user_language: their own preferred_language, else
the tenant's, else Georgian), rendered per recipient.

Never raises: a notification problem must not fail a booking.
"""
import logging

logger = logging.getLogger(__name__)

TEXTS = {
    'en': {
        'created': ('New online booking', '{client} booked {service} for {when}.'),
        'cancelled': ('Online booking cancelled', '{client} cancelled {service} on {when}.'),
        'rescheduled': ('Booking time changed', '{client} moved {service} to {when}.'),
    },
    'ka': {
        'created': ('ახალი ონლაინ ჯავშანი', '{client} — {service}, {when}.'),
        'cancelled': ('ონლაინ ჯავშანი გაუქმდა', '{client} — {service}, {when}.'),
        'rescheduled': ('ჯავშნის დრო შეიცვალა', '{client} — {service}, ახალი დრო: {when}.'),
    },
}

NOTIFICATION_TYPES = {
    'created': 'booking_created',
    'cancelled': 'booking_cancelled',
    'rescheduled': 'booking_rescheduled',
}

# Notices to the assigned staff member about their own agenda.
STAFF_TEXTS = {
    'en': {
        'created': ('New booking for you', '{client} — {service}, {when}.'),
        'confirmed': ('Booking confirmed', '{client} — {service}, {when}.'),
        'cancelled': ('Booking cancelled', '{client} — {service}, {when}.'),
        'rescheduled': ('Booking moved', '{client} — {service}, new time: {when}.'),
        'assigned': ('Booking assigned to you', '{client} — {service}, {when}.'),
        'unassigned': ('Booking reassigned', '{client} — {service}, {when} now goes to {other}.'),
        'reminder': ('Next client in {minutes} min', '{client} — {service}, {when}.'),
    },
    'ka': {
        'created': ('ახალი ჯავშანი თქვენთან', '{client} — {service}, {when}.'),
        'confirmed': ('ჯავშანი დადასტურდა', '{client} — {service}, {when}.'),
        'cancelled': ('ჯავშანი გაუქმდა', '{client} — {service}, {when}.'),
        'rescheduled': ('ჯავშანი გადაიტანეს', '{client} — {service}, ახალი დრო: {when}.'),
        'assigned': ('ჯავშანი მოგენიჭათ', '{client} — {service}, {when}.'),
        'unassigned': ('ჯავშანი სხვას გადაეცა', '{client} — {service}, {when} — {other}.'),
        'reminder': ('შემდეგი კლიენტი {minutes} წუთში', '{client} — {service}, {when}.'),
    },
}

STAFF_NOTIFICATION_TYPES = {
    'created': 'booking_created',
    'confirmed': 'booking_confirmed',
    'cancelled': 'booking_cancelled',
    'rescheduled': 'booking_rescheduled',
    'assigned': 'booking_assigned',
    'unassigned': 'booking_assigned',
    'reminder': 'booking_reminder',
}


def _hhmm(value):
    """'HH:MM' from a time (or the 'HH:MM[:SS]' string a freshly built row may still hold)."""
    return value.strftime('%H:%M') if hasattr(value, 'strftime') else str(value)[:5]


def _when(booking):
    day = booking.date.strftime('%d.%m.%Y') if hasattr(booking.date, 'strftime') else str(booking.date)
    return f'{day} {_hhmm(booking.start_time)}'


def _format(booking, template, language, client_label='', **extra):
    from .utils_text import localized_text
    client = booking.client
    return template.format(
        client=client_label or client.full_name or client.name or client.phone or '—',
        service=localized_text(booking.service.name, language),
        when=_when(booking),
        **extra,
    )


def _metadata(booking, **extra):
    data = {
        'booking_id': booking.id,
        'booking_number': booking.booking_number,
        'date': str(booking.date),
        'start_time': _hhmm(booking.start_time),
    }
    data.update(extra)
    return data


def notify_staff_of_booking(booking, kind, client_label=''):
    """Customer action on the public site. kind: 'created' | 'cancelled' | 'rescheduled'."""
    try:
        from django.db.models import Q
        from users.models import User
        from users.notification_utils import create_notification

        from users.notification_texts import LazyText

        def _texts(language):
            return (TEXTS.get(language) or TEXTS['en'])[kind]

        title = LazyText(lambda language: _texts(language)[0])
        message = LazyText(
            lambda language: _format(booking, _texts(language)[1], language, client_label=client_label)
        )

        # The assigned staff member, plus everyone who runs the business.
        recipients = Q(is_staff=True) | Q(is_superuser=True)
        if booking.staff_id:
            recipients |= Q(pk=booking.staff.user_id)
        for user in User.objects.filter(recipients, is_active=True).distinct():
            create_notification(
                user,
                NOTIFICATION_TYPES[kind],
                title,
                message,
                metadata=_metadata(booking),
                link_url=f'/bookings/bookings/{booking.id}',
            )
    except Exception:
        logger.exception('Could not notify staff about booking %s (%s)', getattr(booking, 'id', None), kind)


def notify_assigned_staff(booking, kind, actor=None, staff=None, **extra):
    """Tell a booking's staff member that their agenda changed.

    kind: 'created' | 'confirmed' | 'cancelled' | 'rescheduled' | 'assigned' |
    'unassigned' | 'reminder'. staff: whom to tell (default: the booking's
    assigned staff). actor: the user who made the change; they are never
    notified about their own action. extra: template values ('other' for
    'unassigned', 'minutes' for 'reminder').
    Returns True when a notification was created.
    """
    try:
        from users.notification_utils import create_notification

        staff = staff if staff is not None else booking.staff
        user = getattr(staff, 'user', None) if staff is not None else None
        if user is None or not user.is_active:
            return False
        actor_id = getattr(actor, 'pk', actor)
        if actor_id is not None and actor_id == user.pk:
            return False

        from users.notification_texts import LazyText

        def _texts(language):
            return (STAFF_TEXTS.get(language) or STAFF_TEXTS['en'])[kind]

        title = LazyText(
            lambda language: _texts(language)[0].format(**extra) if extra else _texts(language)[0]
        )
        message = LazyText(lambda language: _format(booking, _texts(language)[1], language, **extra))

        metadata = _metadata(booking, kind=kind)
        if booking.cancelled_by and kind == 'cancelled':
            metadata['cancelled_by'] = booking.cancelled_by
        notification = create_notification(
            user,
            STAFF_NOTIFICATION_TYPES[kind],
            title,
            message,
            metadata=metadata,
            link_url=f'/bookings/bookings/{booking.id}',
        )
        return notification is not None
    except Exception:
        logger.exception('Could not notify staff about booking %s (%s)', getattr(booking, 'id', None), kind)
        return False
