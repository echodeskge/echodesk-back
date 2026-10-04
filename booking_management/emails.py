"""
Emails sent to booking customers: account codes and booking notices.

Kept as small inline HTML (no template files) and in the customer's language
(English or Georgian). Every sender returns True/False and never raises —
a mail failure must not break a booking or a registration.
"""
import logging
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)

CODE_TTL_MINUTES = 30

TEXTS = {
    'en': {
        'verify_subject': 'Your verification code — {business}',
        'verify_intro': 'Use this code to verify your email for {business}:',
        'reset_subject': 'Reset your password — {business}',
        'reset_intro': 'Use this code to reset your password for {business}:',
        'code_expiry': 'The code is valid for {minutes} minutes. If you did not request it, ignore this email.',
        'created_subject': 'Booking request received — {business}',
        'created_intro': 'We received your booking at {business}.',
        'confirmed_subject': 'Booking confirmed — {business}',
        'confirmed_intro': 'Your booking at {business} is confirmed.',
        'cancelled_subject': 'Booking cancelled — {business}',
        'cancelled_intro': 'Your booking at {business} was cancelled.',
        'service': 'Service',
        'staff': 'With',
        'when': 'When',
        'price': 'Price',
        'number': 'Booking number',
        'manage': 'View or cancel your booking',
        'pay': 'Pay online',
    },
    'ka': {
        'verify_subject': 'თქვენი დადასტურების კოდი — {business}',
        'verify_intro': 'გამოიყენეთ ეს კოდი ელ. ფოსტის დასადასტურებლად ({business}):',
        'reset_subject': 'პაროლის აღდგენა — {business}',
        'reset_intro': 'გამოიყენეთ ეს კოდი პაროლის აღსადგენად ({business}):',
        'code_expiry': 'კოდი მოქმედებს {minutes} წუთის განმავლობაში. თუ თქვენ არ მოგითხოვიათ, უგულებელყავით ეს წერილი.',
        'created_subject': 'ჯავშნის მოთხოვნა მიღებულია — {business}',
        'created_intro': 'თქვენი ჯავშანი მიღებულია: {business}.',
        'confirmed_subject': 'ჯავშანი დადასტურებულია — {business}',
        'confirmed_intro': 'თქვენი ჯავშანი დადასტურებულია: {business}.',
        'cancelled_subject': 'ჯავშანი გაუქმებულია — {business}',
        'cancelled_intro': 'თქვენი ჯავშანი გაუქმდა: {business}.',
        'service': 'სერვისი',
        'staff': 'სპეციალისტი',
        'when': 'დრო',
        'price': 'ფასი',
        'number': 'ჯავშნის ნომერი',
        'manage': 'ჯავშნის ნახვა ან გაუქმება',
        'pay': 'ონლაინ გადახდა',
    },
}


def _texts(language):
    return TEXTS.get(language) or TEXTS['en']


def booking_site_url():
    """Base URL of the public booking site (no trailing slash)."""
    return getattr(settings, 'BOOKING_SITE_URL', 'https://book.echodesk.ge').rstrip('/')


def booking_manage_url(schema_name, manage_token):
    return f"{booking_site_url()}/{schema_name}/booking/{manage_token}"


def _business_name():
    try:
        from django.db import connection
        from tenants.models import Tenant
        return Tenant.objects.get(schema_name=connection.schema_name).name
    except Exception:
        return 'EchoDesk'


def _send(subject, recipient, lines, link=None, link_label=None):
    """lines: list of plain strings (already localized). Returns bool."""
    if not recipient:
        return False
    try:
        text = '\n'.join(lines)
        html = ''.join(f'<p>{escape(line)}</p>' for line in lines if line)
        if link:
            text += f'\n\n{link_label}: {link}'
            html += f'<p><a href="{escape(link)}">{escape(link_label)}</a></p>'
        message = EmailMultiAlternatives(
            subject=subject,
            body=text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )
        message.attach_alternative(f'<div style="font-family:sans-serif;font-size:15px">{html}</div>', 'text/html')
        message.send(fail_silently=False)
        return True
    except Exception as exc:
        logger.error('Booking email to %s failed: %s', recipient, exc)
        return False


def send_verification_code(client, code, language='en'):
    t = _texts(language)
    business = _business_name()
    return _send(
        t['verify_subject'].format(business=business),
        client.email,
        [t['verify_intro'].format(business=business), code, t['code_expiry'].format(minutes=CODE_TTL_MINUTES)],
    )


def send_password_reset_code(client, code, language='en'):
    t = _texts(language)
    business = _business_name()
    return _send(
        t['reset_subject'].format(business=business),
        client.email,
        [t['reset_intro'].format(business=business), code, t['code_expiry'].format(minutes=CODE_TTL_MINUTES)],
    )


def _localized(value, language):
    if isinstance(value, dict):
        return value.get(language) or value.get('en') or next(iter(value.values()), '')
    return value or ''


def send_booking_email(booking, kind, schema_name, language='en'):
    """kind: 'created' | 'confirmed' | 'cancelled'."""
    if kind not in ('created', 'confirmed', 'cancelled'):
        return False
    client = booking.client
    if not client or not client.email:
        return False

    t = _texts(language)
    business = _business_name()
    lines = [
        t[f'{kind}_intro'].format(business=business),
        f"{t['service']}: {_localized(booking.service.name, language)}",
    ]
    if booking.staff:
        lines.append(f"{t['staff']}: {booking.staff}")
    lines.append(f"{t['when']}: {booking.date.strftime('%d.%m.%Y')} {booking.start_time.strftime('%H:%M')}")
    lines.append(f"{t['price']}: {booking.total_amount} ₾")
    lines.append(f"{t['number']}: {booking.booking_number}")

    link = None
    if booking.manage_token:
        link = booking_manage_url(schema_name, booking.manage_token)

    return _send(
        t[f'{kind}_subject'].format(business=business),
        client.email,
        lines,
        link=link,
        link_label=t['manage'],
    )
