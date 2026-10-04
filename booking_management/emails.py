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
        'code_expiry': 'The code is valid for {minutes} minutes. If you did not request it, you can ignore this email.',
        'created_subject': 'Booking request received — {business}',
        'created_intro': "We received your booking request at {business}. We'll email you once it's confirmed.",
        'confirmed_subject': 'Booking confirmed — {business}',
        'confirmed_intro': 'Your booking at {business} is confirmed.',
        'cancelled_subject': 'Booking cancelled — {business}',
        'cancelled_intro': 'Your booking at {business} was cancelled.',
        'rescheduled_subject': 'Booking time changed — {business}',
        'rescheduled_intro': 'The time of your booking at {business} was changed. The new time is below.',
        'reminder_subject': 'Reminder: your appointment tomorrow — {business}',
        'reminder_intro': 'A reminder of your appointment at {business} tomorrow.',
        'service': 'Service',
        'staff': 'With',
        'when': 'Date and time',
        'price': 'Price',
        'number': 'Booking number',
        'manage': 'View or cancel your booking',
        'view': 'View your booking',
    },
    'ka': {
        'verify_subject': 'თქვენი დადასტურების კოდი — {business}',
        'verify_intro': 'გამოიყენეთ ეს კოდი ელ. ფოსტის დასადასტურებლად ({business}):',
        'reset_subject': 'პაროლის აღდგენა — {business}',
        'reset_intro': 'გამოიყენეთ ეს კოდი პაროლის აღსადგენად ({business}):',
        'code_expiry': 'კოდი მოქმედებს {minutes} წუთის განმავლობაში. თუ კოდი თქვენ არ მოგითხოვიათ, ამ წერილს ყურადღება არ მიაქციოთ.',
        'created_subject': 'ჯავშნის მოთხოვნა მიღებულია — {business}',
        'created_intro': 'თქვენი ჯავშნის მოთხოვნა მიღებულია — {business}. დადასტურებისას შეგატყობინებთ.',
        'confirmed_subject': 'ჯავშანი დადასტურებულია — {business}',
        'confirmed_intro': 'თქვენი ჯავშანი დადასტურებულია — {business}.',
        'cancelled_subject': 'ჯავშანი გაუქმებულია — {business}',
        'cancelled_intro': 'თქვენი ჯავშანი გაუქმდა — {business}.',
        'rescheduled_subject': 'ჯავშნის დრო შეიცვალა — {business}',
        'rescheduled_intro': 'თქვენი ჯავშნის დრო შეიცვალა — {business}. ახალი დრო მითითებულია ქვემოთ.',
        'reminder_subject': 'შეხსენება: ხვალ ვიზიტი გაქვთ — {business}',
        'reminder_intro': 'შეგახსენებთ, რომ ხვალ ვიზიტი გაქვთ — {business}.',
        'service': 'სერვისი',
        'staff': 'სპეციალისტი',
        'when': 'თარიღი და დრო',
        'price': 'ფასი',
        'number': 'ჯავშნის ნომერი',
        'manage': 'ჯავშნის ნახვა ან გაუქმება',
        'view': 'ჯავშნის ნახვა',
    },
}

BOOKING_EMAIL_KINDS = ('created', 'confirmed', 'cancelled', 'rescheduled', 'reminder')


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


def _money(amount):
    """50.00 → '50 ₾', 12.50 → '12.50 ₾' (same as the booking site shows)."""
    text = f'{amount:.2f}'
    if text.endswith('.00'):
        text = text[:-3]
    return f'{text} ₾'


def booking_recipient(booking):
    """Where a booking's notices go: the email given for THIS booking, else
    the client's stored one. (A guest matched to an existing contact by phone
    must get the mail at the address they typed, not the contact's old one.)"""
    return booking.contact_email or (booking.client.email if booking.client_id else '') or ''


def send_booking_email(booking, kind, schema_name, language='en'):
    """kind: one of BOOKING_EMAIL_KINDS."""
    from .utils_text import localized_text

    if kind not in BOOKING_EMAIL_KINDS:
        return False
    recipient = booking_recipient(booking)
    if not recipient:
        return False

    language = language or booking.contact_language or 'en'
    t = _texts(language)
    business = _business_name()
    lines = [
        t[f'{kind}_intro'].format(business=business),
        f"{t['service']}: {localized_text(booking.service.name, language)}",
    ]
    if booking.staff:
        from .utils_text import public_staff_name
        staff_name = public_staff_name(booking.staff)
        if staff_name:
            lines.append(f"{t['staff']}: {staff_name}")
    lines.append(f"{t['when']}: {booking.date.strftime('%d.%m.%Y')}, {booking.start_time.strftime('%H:%M')}")
    lines.append(f"{t['price']}: {_money(booking.total_amount)}")
    lines.append(f"{t['number']}: {booking.booking_number}")

    link = None
    if booking.manage_token:
        link = booking_manage_url(schema_name, booking.manage_token)

    return _send(
        t[f'{kind}_subject'].format(business=business),
        recipient,
        lines,
        link=link,
        link_label=t['view'] if kind == 'cancelled' else t['manage'],
    )
