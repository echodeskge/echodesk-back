"""
SMS notices to booking customers (sender.ge).

A salon sends through its own sender.ge key when it has entered one,
otherwise through the shared EchoDesk account, capped per calendar month.
Every attempt is written to BookingSmsLog. Nothing here raises: an SMS that
cannot be sent must never break a booking.
"""
import logging
import re

from django.conf import settings as django_settings
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

SMS_KINDS = ('created', 'confirmed', 'rescheduled', 'cancelled', 'reminder')
PLACEHOLDERS = ('name', 'business', 'service', 'date', 'time', 'staff', 'link')
MAX_TEMPLATE_LENGTH = 600
DEFAULT_PLATFORM_MONTHLY_LIMIT = 200

DEFAULT_TEMPLATES = {
    'created': {
        'ka': '{business}: თქვენი ჯავშნის მოთხოვნა მიღებულია — {date}, {time}. დადასტურებისას შეგატყობინებთ.',
        'en': '{business}: we received your booking request for {date} at {time}. We will confirm it shortly.',
    },
    'confirmed': {
        'ka': '{business}: თქვენი ჯავშანი დადასტურებულია — {service}, {date}, {time}.',
        'en': '{business}: your booking is confirmed — {service}, {date} at {time}.',
    },
    'rescheduled': {
        'ka': '{business}: თქვენი ჯავშნის დრო შეიცვალა. ახალი დრო: {date}, {time}.',
        'en': '{business}: your booking time has changed. New time: {date} at {time}.',
    },
    'cancelled': {
        'ka': '{business}: თქვენი ჯავშანი ({date}, {time}) გაუქმდა.',
        'en': '{business}: your booking on {date} at {time} was cancelled.',
    },
    'reminder': {
        'ka': '{business}: შეგახსენებთ ვიზიტს — {service}, {date}, {time}.',
        'en': '{business}: a reminder of your visit — {service}, {date} at {time}.',
    },
}

# Characters of the GSM 03.38 basic alphabet (plus the extension table, whose
# characters cost two). Anything else (Georgian, emoji…) makes the SMS Unicode.
_GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ!\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_EXTENDED = set("^{}\\[~]|€")


def sms_segments(text):
    """How many SMS a text is billed as (160/153 GSM, 70/67 Unicode)."""
    if not text:
        return 0
    if all(c in _GSM_BASIC or c in _GSM_EXTENDED for c in text):
        length = sum(2 if c in _GSM_EXTENDED else 1 for c in text)
        return 1 if length <= 160 else -(-length // 153)
    length = len(text.encode('utf-16-be')) // 2
    return 1 if length <= 70 else -(-length // 67)


def _language(language):
    return language if language in ('ka', 'en') else 'en'


def template_for(booking_settings, kind, language):
    """The salon's text for this notice, else the built-in default."""
    language = _language(language)
    custom = ((getattr(booking_settings, 'sms_templates', None) or {}).get(kind) or {}).get(language)
    if isinstance(custom, str) and custom.strip():
        return custom
    return DEFAULT_TEMPLATES[kind][language]


def render_template(template, values):
    """Fill {placeholders}; unknown ones stay as typed (never raises)."""
    return re.sub(
        r'\{(\w+)\}',
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        template,
    ).strip()


def _business_name():
    from .emails import _business_name as name
    return name()


def booking_values(booking, language, schema_name=None):
    from django.db import connection
    from .emails import booking_manage_url
    from .utils_text import localized_text, public_staff_name

    link = ''
    if booking.manage_token:
        link = booking_manage_url(schema_name or connection.schema_name, booking.manage_token)
    client = booking.client
    return {
        'name': (client.first_name or client.full_name or '').strip(),
        'business': _business_name(),
        'service': localized_text(booking.service.name, _language(language)),
        'date': booking.date.strftime('%d.%m'),
        'time': booking.start_time.strftime('%H:%M'),
        'staff': public_staff_name(booking.staff) if booking.staff else '',
        'link': link,
    }


SAMPLE_VALUES = {
    'ka': {'name': 'ანა', 'service': 'კონსულტაცია', 'staff': 'ნინო'},
    'en': {'name': 'Ana', 'service': 'Consultation', 'staff': 'Nino'},
}


def sample_values(language):
    now = timezone.now()
    values = dict(SAMPLE_VALUES[_language(language)])
    values.update({
        'business': _business_name(),
        'date': now.strftime('%d.%m'),
        'time': '14:00',
        'link': 'https://book.echodesk.ge/…',
    })
    return values


# ----------------------------------------------------------------------------
# Account: own key, else the shared account under a monthly cap
# ----------------------------------------------------------------------------

def platform_api_key():
    return getattr(django_settings, 'SENDER_GE_API_KEY', '') or ''


def platform_monthly_limit(booking_settings):
    own = getattr(booking_settings, 'sms_platform_monthly_limit', None)
    if own is not None:
        return max(0, own)
    return int(getattr(django_settings, 'BOOKING_SMS_PLATFORM_MONTHLY_LIMIT', DEFAULT_PLATFORM_MONTHLY_LIMIT))


def _month_start():
    return timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def sent_this_month(account=None):
    """SMS segments successfully sent this calendar month (optionally one account)."""
    from .models import BookingSmsLog

    logs = BookingSmsLog.objects.filter(status='sent', created_at__gte=_month_start())
    if account:
        logs = logs.filter(account=account)
    return logs.aggregate(total=Sum('segments'))['total'] or 0


def resolve_account(booking_settings, segments=1):
    """→ (api_key, account, reason). account is 'own' | 'platform' | ''."""
    own = booking_settings.sms_api_key
    if own:
        return own, 'own', ''
    key = platform_api_key()
    if not key:
        return '', '', 'No SMS account: add a sender.ge key in booking settings'
    if sent_this_month('platform') + segments > platform_monthly_limit(booking_settings):
        return '', '', 'Monthly SMS limit reached'
    return key, 'platform', ''


def _is_georgian_mobile(phone):
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('995'):
        digits = digits[3:]
    return len(digits) == 9 and digits.startswith('5')


def send_text(booking_settings, phone, text, kind, language='', booking=None):
    """Send one SMS and log it. Returns the BookingSmsLog row."""
    from crm.sms_utils import send_sms
    from .models import BookingSmsLog

    segments = sms_segments(text)
    log = BookingSmsLog(
        booking=booking, phone=(phone or '')[:50], kind=kind, language=language or '',
        text=text, segments=segments, status='skipped',
    )
    try:
        if not phone:
            log.error = 'No phone number'
        elif not _is_georgian_mobile(phone):
            log.error = 'Not a Georgian mobile number'
        else:
            api_key, account, reason = resolve_account(booking_settings, segments)
            if not api_key:
                log.error = reason[:255]
            else:
                log.account = account
                result = send_sms(api_key, phone, text)
                if not isinstance(result, dict) or result.get('error') or not result.get('messageId'):
                    log.status = 'failed'
                    log.error = str((result or {}).get('error') or result or 'No message id returned')[:255]
                else:
                    log.status = 'sent'
                    log.provider_message_id = str(result.get('messageId'))[:100]
    except Exception as exc:  # never let an SMS break the caller
        logger.exception('Booking SMS failed')
        log.status = 'failed'
        log.error = str(exc)[:255]
    try:
        log.save()
    except Exception:
        logger.exception('Could not write the booking SMS log')
    return log


def sms_wanted(booking_settings, kind):
    return bool(
        booking_settings and booking_settings.sms_enabled
        and kind in SMS_KINDS and getattr(booking_settings, f'sms_on_{kind}', False)
    )


def send_booking_sms(booking, kind, language=None, schema_name=None):
    """SMS a booking notice to its customer if the salon switched it on.

    Returns 'sent' (sender.ge accepted it), 'failed' (worth retrying),
    'skipped' (nothing we can send to) or 'off' (SMS not wanted for this).
    """
    try:
        from .utils import get_booking_settings

        booking_settings = get_booking_settings()
        if not sms_wanted(booking_settings, kind):
            return 'off'
        phone = booking.client.phone if booking.client_id else ''
        if not phone:
            return 'skipped'
        language = _language(language or booking.contact_language)
        text = render_template(
            template_for(booking_settings, kind, language),
            booking_values(booking, language, schema_name),
        )
        return send_text(booking_settings, phone, text, kind, language, booking=booking).status
    except Exception:
        logger.exception('Booking SMS (%s) failed for booking %s', kind, getattr(booking, 'id', None))
        return 'failed'
