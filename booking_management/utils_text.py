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
