"""
In-app notifications to the business's staff about things customers do on the
public booking site (new booking, cancellation, time change).

Never raises: a notification problem must not fail a customer's booking.
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


def notify_staff_of_booking(booking, kind, client_label=''):
    """kind: 'created' | 'cancelled' | 'rescheduled'."""
    try:
        from django.db import connection
        from django.db.models import Q
        from tenants.models import Tenant
        from users.models import User
        from users.notification_utils import create_notification
        from .utils_text import localized_text

        try:
            language = Tenant.objects.get(schema_name=connection.schema_name).preferred_language
        except Exception:
            language = 'en'
        texts = TEXTS.get(language) or TEXTS['en']
        title, template = texts[kind]

        message = template.format(
            client=client_label or booking.client.full_name or booking.client.name or booking.client.phone or '—',
            service=localized_text(booking.service.name, language),
            when=f"{booking.date.strftime('%d.%m.%Y')} {booking.start_time.strftime('%H:%M')}",
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
                metadata={'booking_id': booking.id, 'booking_number': booking.booking_number},
                link_url=f'/bookings/bookings/{booking.id}',
            )
    except Exception:
        logger.exception('Could not notify staff about booking %s (%s)', getattr(booking, 'id', None), kind)
