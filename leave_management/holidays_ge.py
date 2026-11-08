"""
Georgia's public holidays (Labour Code, Article 30).

13 fixed dates, 17 May (from 2025) and Orthodox Easter: Good Friday,
Holy Saturday, Easter Sunday and Easter Monday. Easter uses the Julian
computus shifted to the Gregorian calendar.
"""
from datetime import date, timedelta

FIXED_HOLIDAYS = [
    ((1, 1), {'en': "New Year's Day", 'ka': 'ახალი წელი'}),
    ((1, 2), {'en': "New Year's Holiday", 'ka': 'ბედობა'}),
    ((1, 7), {'en': 'Orthodox Christmas', 'ka': 'შობა'}),
    ((1, 19), {'en': 'Epiphany', 'ka': 'ნათლისღება'}),
    ((3, 3), {'en': "Mother's Day", 'ka': 'დედის დღე'}),
    ((3, 8), {'en': "International Women's Day", 'ka': 'ქალთა საერთაშორისო დღე'}),
    ((4, 9), {'en': 'National Unity Day', 'ka': 'ეროვნული ერთიანობის დღე'}),
    ((5, 9), {'en': 'Victory Day', 'ka': 'ფაშიზმზე გამარჯვების დღე'}),
    ((5, 12), {'en': "St. Andrew's Day", 'ka': 'წმინდა ანდრია პირველწოდებულის დღე'}),
    ((5, 26), {'en': 'Independence Day', 'ka': 'დამოუკიდებლობის დღე'}),
    ((8, 28), {'en': 'Assumption of Mary', 'ka': 'მარიამობა'}),
    ((10, 14), {'en': 'Svetitskhovloba', 'ka': 'სვეტიცხოვლობა'}),
    ((11, 23), {'en': "St. George's Day", 'ka': 'გიორგობა'}),
]
FAMILY_DAY = ((5, 17), {'en': 'Family Sanctity Day', 'ka': 'ოჯახის სიწმინდის დღე'})
FAMILY_DAY_FROM_YEAR = 2025

EASTER_DAYS = [
    (-2, {'en': 'Good Friday', 'ka': 'წითელი პარასკევი'}),
    (-1, {'en': 'Holy Saturday', 'ka': 'დიდი შაბათი'}),
    (0, {'en': 'Easter Sunday', 'ka': 'აღდგომა'}),
    (1, {'en': 'Easter Monday', 'ka': 'მიცვალებულთა მოხსენიების დღე'}),
]


def orthodox_easter(year):
    """Orthodox Easter Sunday (Gregorian date)."""
    moon = (19 * (year % 19) + 15) % 30
    weekday = (2 * (year % 4) + 4 * (year % 7) - moon + 34) % 7
    julian = moon + weekday + 114
    offset = year // 100 - year // 400 - 2
    month, day = julian // 31, julian % 31 + 1
    return date(year, month, 1) + timedelta(days=day - 1 + offset)


def georgian_holidays(year):
    """Sorted list of (date, {'en', 'ka'}) for ``year``."""
    days = [(date(year, m, d), name) for (m, d), name in FIXED_HOLIDAYS]
    if year >= FAMILY_DAY_FROM_YEAR:
        (m, d), name = FAMILY_DAY
        days.append((date(year, m, d), name))
    easter = orthodox_easter(year)
    days.extend((easter + timedelta(days=delta), name) for delta, name in EASTER_DAYS)
    return sorted(days, key=lambda item: item[0])


def load_georgian_holidays(tenant, year, user=None):
    """
    Create the year's PublicHoliday rows (idempotent: dates that already
    have a holiday are skipped). Returns (created, skipped) as lists of dates.
    """
    from .models import PublicHoliday

    existing = set(
        PublicHoliday.objects.filter(tenant=tenant, date__year=year).values_list('date', flat=True)
    )
    created, skipped = [], []
    for day, name in georgian_holidays(year):
        if day in existing:
            skipped.append(day)
            continue
        PublicHoliday.objects.create(
            tenant=tenant, name=dict(name), date=day, is_recurring=False,
            created_by=user, updated_by=user,
        )
        created.append(day)
    return created, skipped
