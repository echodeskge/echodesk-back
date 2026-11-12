"""
Shared answer types used by the module steps (see ``~/Echodesk/.wt/onb/SCHEMAS.md``):

- ``I18n`` = ``{"ka": str, "en": str}`` — :class:`I18nSerializer`, and
  :func:`pick_text` to choose the tenant's language with a fallback.
- ``Hours`` = ``{"mon": {"from", "to", "break_from", "break_to"} | null, ... "sun"}``
  (``null`` = closed) — :class:`HoursSerializer`, :func:`parse_clock`.
- :func:`tenant_language` — ``"ka"`` when the tenant dashboard language is
  Georgian, else ``"en"`` (the two languages the presets are written in).
- :func:`users_context` — the people-picker shape every step exposes.
"""
import datetime as dt

from django.contrib.auth import get_user_model
from rest_framework import serializers

DAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
# ``Hours`` key → ``SocialIntegrationSettings.away_hours_schedule`` key
DAY_NAMES = {
    'mon': 'monday', 'tue': 'tuesday', 'wed': 'wednesday', 'thu': 'thursday',
    'fri': 'friday', 'sat': 'saturday', 'sun': 'sunday',
}
LANGS = ('ka', 'en')

DEFAULT_HOURS = {
    'mon': {'from': '09:00', 'to': '18:00', 'break_from': None, 'break_to': None},
    'tue': {'from': '09:00', 'to': '18:00', 'break_from': None, 'break_to': None},
    'wed': {'from': '09:00', 'to': '18:00', 'break_from': None, 'break_to': None},
    'thu': {'from': '09:00', 'to': '18:00', 'break_from': None, 'break_to': None},
    'fri': {'from': '09:00', 'to': '18:00', 'break_from': None, 'break_to': None},
    'sat': None,
    'sun': None,
}


def tenant_language(tenant):
    """``"ka"`` for a Georgian dashboard, otherwise ``"en"``."""
    return 'ka' if getattr(tenant, 'preferred_language', None) == 'ka' else 'en'


def pick_text(i18n, language):
    """The ``language`` text of an ``I18n`` dict, falling back to the other one."""
    if not isinstance(i18n, dict):
        return (i18n or '') if isinstance(i18n, str) else ''
    primary = (i18n.get(language) or '').strip()
    if primary:
        return primary
    for other in LANGS:
        text = (i18n.get(other) or '').strip()
        if text:
            return text
    return ''


def i18n(en, ka):
    return {'en': en, 'ka': ka}


class I18nSerializer(serializers.Serializer):
    """``{"ka": str, "en": str}``; both optional/blank, at least one non-empty
    when ``require_text`` (the default)."""
    ka = serializers.CharField(required=False, allow_blank=True, default='', max_length=5000)
    en = serializers.CharField(required=False, allow_blank=True, default='', max_length=5000)

    def __init__(self, *args, require_text=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.require_text = require_text

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = {'en': data, 'ka': ''}
        return super().to_internal_value(data)

    def validate(self, attrs):
        attrs = {'ka': attrs.get('ka', ''), 'en': attrs.get('en', '')}
        if self.require_text and not (attrs['ka'] or attrs['en']):
            raise serializers.ValidationError('Provide the text in Georgian or English.')
        return attrs


class ClockField(serializers.CharField):
    """``"HH:MM"`` string, kept as a string (the answers are stored as JSON)."""

    def __init__(self, **kwargs):
        kwargs.setdefault('max_length', 5)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data).strip()
        if parse_clock(value) is None:
            raise serializers.ValidationError('Use the HH:MM format.')
        return value


def parse_clock(value):
    """``"09:30"`` → ``datetime.time(9, 30)``; ``None`` when not a clock string."""
    if isinstance(value, dt.time):
        return value
    if not isinstance(value, str):
        return None
    try:
        hour, minute = value.strip().split(':', 1)
        hour, minute = int(hour), int(minute[:2])
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dt.time(hour, minute)


def clock_str(value):
    """``datetime.time`` → ``"HH:MM"`` (``None`` passes through)."""
    if value is None:
        return None
    return value.strftime('%H:%M')


class DayHoursSerializer(serializers.Serializer):
    to = ClockField()
    break_from = ClockField(required=False, allow_null=True, allow_blank=True, default=None)
    break_to = ClockField(required=False, allow_null=True, allow_blank=True, default=None)

    def get_fields(self):
        # ``from`` is a Python keyword, so it cannot be declared as an attribute.
        fields = super().get_fields()
        return {'from': ClockField(), **fields}

    def validate(self, attrs):
        start, end = parse_clock(attrs['from']), parse_clock(attrs['to'])
        if end <= start:
            raise serializers.ValidationError('"to" must be after "from".')
        b_from, b_to = attrs.get('break_from') or None, attrs.get('break_to') or None
        if bool(b_from) != bool(b_to):
            raise serializers.ValidationError('Give both break_from and break_to, or neither.')
        if b_from:
            bs, be = parse_clock(b_from), parse_clock(b_to)
            if not (start <= bs < be <= end):
                raise serializers.ValidationError('The break must lie inside the working hours.')
        return {'from': attrs['from'], 'to': attrs['to'], 'break_from': b_from, 'break_to': b_to}


class HoursSerializer(serializers.Serializer):
    """``Hours``: one entry per weekday, ``null`` = closed. Missing days count
    as closed so the stored answer is always complete."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for day in DAY_KEYS:
            self.fields[day] = DayHoursSerializer(required=False, allow_null=True, default=None)

    def validate(self, attrs):
        return {day: attrs.get(day) for day in DAY_KEYS}


def hours_to_away_schedule(hours):
    """``Hours`` → ``SocialIntegrationSettings.away_hours_schedule``:
    ``{"monday": [hours 0-23 when the business is AWAY], ...}``. A closed
    day is away all day; an hour counts as working when any part of it lies
    inside ``from``–``to`` (breaks are not "away")."""
    schedule = {}
    for day in DAY_KEYS:
        entry = (hours or {}).get(day)
        if not entry:
            schedule[DAY_NAMES[day]] = list(range(24))
            continue
        start, end = parse_clock(entry['from']), parse_clock(entry['to'])
        first = start.hour
        last = end.hour if end.minute == 0 else end.hour + 1   # exclusive
        schedule[DAY_NAMES[day]] = [h for h in range(24) if h < first or h >= last]
    return schedule


def away_schedule_to_hours(schedule):
    """Inverse of :func:`hours_to_away_schedule` for prefill (one contiguous
    working block per day; anything else returns the day as is-or-closed)."""
    hours = {}
    for day in DAY_KEYS:
        away = set((schedule or {}).get(DAY_NAMES[day]) or [])
        working = [h for h in range(24) if h not in away]
        if not working:
            hours[day] = None
            continue
        hours[day] = {
            'from': f'{working[0]:02d}:00',
            'to': f'{working[-1] + 1:02d}:00' if working[-1] < 23 else '23:59',
            'break_from': None, 'break_to': None,
        }
    return hours


def users_context():
    """The people-picker shape shared by every step (see the workspace step)."""
    User = get_user_model()
    return [
        {
            'id': u.id,
            'email': u.email,
            'name': u.get_full_name(),
            'role': u.role,
            'is_active': u.is_active,
        }
        for u in User.objects.order_by('email').only('id', 'email', 'first_name', 'last_name', 'role', 'is_active')
    ]


class UserIdsField(serializers.ListField):
    """``[UserId]`` — de-duplicated, every id must be an existing user."""

    def __init__(self, **kwargs):
        kwargs.setdefault('child', serializers.IntegerField(min_value=1))
        kwargs.setdefault('required', False)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        ids = []
        for value in super().to_internal_value(data):
            if value not in ids:
                ids.append(value)
        if ids:
            User = get_user_model()
            known = set(User.objects.filter(id__in=ids).values_list('id', flat=True))
            missing = [i for i in ids if i not in known]
            if missing:
                raise serializers.ValidationError(f'Unknown user id(s): {missing}.')
        return ids
