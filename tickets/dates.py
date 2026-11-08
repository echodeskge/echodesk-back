"""Calendar helpers for ticket due dates.

There is no per-tenant timezone (settings.TIME_ZONE is 'UTC') and every
current tenant is Georgian, so "today" for due-date purposes is the current
date in Asia/Tbilisi. Every due-date computation (``is_overdue_task``, the
``due`` filters, ``view_counts``) must go through these helpers.
"""
import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

TICKETS_LOCAL_TZ = ZoneInfo('Asia/Tbilisi')


def tickets_today():
    """The current calendar date in TICKETS_LOCAL_TZ."""
    return timezone.now().astimezone(TICKETS_LOCAL_TZ).date()


def end_of_iso_week(d=None):
    """The Sunday that ends the ISO week (Mon-Sun) containing ``d``
    (defaults to tickets_today())."""
    if d is None:
        d = tickets_today()
    if isinstance(d, datetime.datetime):
        d = d.astimezone(TICKETS_LOCAL_TZ).date() if timezone.is_aware(d) else d.date()
    return d + datetime.timedelta(days=6 - d.weekday())
