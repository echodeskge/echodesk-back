def localized_text(value, language='en'):
    """Pick a language from a {"en": ..., "ka": ...} value, falling back to any
    language that has text — a business that filled in only Georgian must not
    show blank names to a visitor browsing in English (or the reverse)."""
    if isinstance(value, dict):
        for key in (language, 'ka', 'en'):
            text = value.get(key)
            if text:
                return text
        return next((v for v in value.values() if v), '')
    return value or ''


def public_staff_name(staff):
    """Staff member's name as customers may see it. Never falls back to the
    email address (str(staff) does) — that is internal."""
    if staff is None:
        return ''
    return public_user_name(staff.user)


def public_user_name(user):
    """First + last name only. (User.get_full_name() falls back to the email
    when both are blank, which must not reach customers.)"""
    return f"{user.first_name or ''} {user.last_name or ''}".strip()


def phone_digits(phone):
    """Digits of a phone number in international form, for comparing numbers
    typed in different ways ("+995 555 10 20 30", "555102030", "00995…")."""
    import re
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('00'):
        digits = digits[2:]
    # A Georgian mobile number typed without the country code
    if len(digits) == 9 and digits.startswith('5'):
        digits = '995' + digits
    return digits


def normalize_phone(phone):
    """Canonical stored form: +<country code><number>, no spaces."""
    digits = phone_digits(phone)
    return f'+{digits}' if digits else ''
