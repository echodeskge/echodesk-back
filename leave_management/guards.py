"""
Checks run before a user is deactivated or deleted, so open leave work is
not orphaned and leave history is not wiped by CASCADE deletes.
"""
from django.db import connection

OPEN_REQUEST_STATUSES = ('awaiting_substitutes', 'pending', 'manager_approved', 'hr_approved')


def _tenant():
    return getattr(connection, 'tenant', None)


def guard_enabled(tenant=None):
    from .utils import get_leave_settings
    settings_obj = get_leave_settings(tenant or _tenant())
    # No settings row yet: the default (True) applies.
    return True if settings_obj is None else settings_obj.block_deactivation_with_open_requests


def deactivation_blockers(user):
    """Readable reasons why ``user`` cannot be deactivated now (empty = allowed)."""
    from .models import LeaveRequest

    reasons = []
    open_requests = LeaveRequest.objects.filter(
        employee=user, status__in=OPEN_REQUEST_STATUSES
    ).count()
    if open_requests:
        reasons.append(f'{open_requests} open leave request(s) must be decided or cancelled first')

    try:
        from .models import LeaveRequestSubstitute
    except ImportError:  # pragma: no cover
        LeaveRequestSubstitute = None
    if LeaveRequestSubstitute is not None:
        covering = LeaveRequestSubstitute.objects.filter(
            substitute=user,
            leave_request__status__in=OPEN_REQUEST_STATUSES,
        ).exclude(status__in=('rejected', 'skipped')).count()
        if covering:
            reasons.append(f'substitute on {covering} open leave request(s)')

    reports = user.managed_employees.filter(is_active=True).count()
    if reports:
        reasons.append(f'manager of {reports} active employee(s); reassign them first')
    return reasons


def deletion_blockers(user):
    """Deactivation reasons plus records that a delete would erase."""
    from .models import LeaveRequest

    reasons = deactivation_blockers(user)
    has_history = LeaveRequest.objects.filter(employee=user).exists()
    try:
        from .models import LeaveRequestSubstitute
        has_history = has_history or LeaveRequestSubstitute.objects.filter(substitute=user).exists()
    except ImportError:  # pragma: no cover
        pass
    try:
        from .models import KpiGoal
        has_history = has_history or KpiGoal.objects.filter(employee=user).exists()
    except ImportError:  # pragma: no cover
        pass
    if has_history:
        reasons.append('has leave/KPI history; deactivate the user instead of deleting')
    return reasons


def check_removal(user, delete=False):
    """[] when allowed (or the guard is off), else the reasons."""
    if not guard_enabled():
        return []
    return deletion_blockers(user) if delete else deactivation_blockers(user)
