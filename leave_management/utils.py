import logging
from datetime import datetime, timedelta, date
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


# ============================================================================
# TEAM SCOPING
# ============================================================================
# Who counts as "my team" for the manager endpoints, in one place so the
# permissions, the querysets and the approval check cannot drift apart:
#   - staff / superusers see everyone;
#   - a user sees their direct reports (``User.manager``);
#   - a user with role 'manager' and a department also sees that department;
#   - nobody manages themselves.

def _is_elevated(user):
    """Staff and superusers bypass team scoping entirely."""
    return bool(user.is_staff or user.is_superuser)


def _manages_department(user):
    """True when the user's role entitles them to their whole department."""
    return user.role == 'manager' and user.department_id is not None


def is_leave_manager(user):
    """
    Can this user open the manager side of leave management at all?

    True for staff/superusers, the 'manager' role, anyone with at least one
    active direct report, and the legacy ``view_team_leaves`` permission.
    """
    if not user or not user.is_authenticated:
        return False
    if _is_elevated(user) or user.role == 'manager':
        return True
    if user.managed_employees.filter(is_active=True).exists():
        return True
    return user.has_perm('leave_management.view_team_leaves')


def team_q(user, prefix='employee'):
    """
    Q object restricting a queryset to the user's team.

    ``prefix`` names the FK to the employee on the model being filtered:
    ``employee`` for LeaveRequest, ``user`` for LeaveBalance.
    """
    if _is_elevated(user):
        return Q()

    q = Q(**{f'{prefix}__manager': user})
    if _manages_department(user):
        q |= Q(**{f'{prefix}__department_id': user.department_id})
    return q & ~Q(**{prefix: user})


def manages_employee(user, employee):
    """Object-level counterpart of team_q: does ``user`` manage ``employee``?"""
    if _is_elevated(user):
        return True
    if employee is None or employee.pk == user.pk:
        return False
    if employee.manager_id == user.pk:
        return True
    if _manages_department(user) and employee.department_id == user.department_id:
        return True
    return False


def filter_by_status_in(queryset, raw):
    """
    Apply a ``?status__in=pending,approved`` query parameter.

    ``raw`` is the parameter's string value (or None); blank entries are
    ignored, and an empty list leaves the queryset untouched.
    """
    if not raw:
        return queryset
    statuses = [s.strip() for s in raw.split(',') if s.strip()]
    if not statuses:
        return queryset
    return queryset.filter(status__in=statuses)


def calculate_working_days(start_date, end_date, tenant, exclude_public_holidays=True):
    """
    Calculate working days between start_date and end_date (inclusive)
    excluding weekends and optionally public holidays.

    Args:
        start_date: Start date
        end_date: End date (inclusive)
        tenant: Tenant object
        exclude_public_holidays: Whether to exclude public holidays

    Returns:
        Decimal: Number of working days
    """
    if start_date > end_date:
        return Decimal('0')

    # Get weekend days from settings (default: Saturday=5, Sunday=6)
    try:
        from .models import LeaveSettings
        leave_settings = LeaveSettings.objects.get(tenant=tenant)
        weekend_days = leave_settings.weekend_days if leave_settings.weekend_days else [5, 6]
    except Exception as e:
        logger.debug(f"No leave settings for tenant, using default weekends: {e}")
        weekend_days = [5, 6]  # Default to Sat/Sun

    # Get public holidays in date range
    public_holidays = set()
    if exclude_public_holidays:
        from .models import PublicHoliday
        holidays = PublicHoliday.objects.filter(
            tenant=tenant,
            date__gte=start_date,
            date__lte=end_date
        ).values_list('date', flat=True)
        public_holidays = set(holidays)

    # Count working days
    working_days = 0
    current_date = start_date

    while current_date <= end_date:
        # Check if it's a weekend day
        if current_date.weekday() not in weekend_days:
            # Check if it's a public holiday
            if current_date not in public_holidays:
                working_days += 1
        current_date += timedelta(days=1)

    return Decimal(str(working_days))


def check_leave_balance(user, leave_type, days, year, tenant):
    """
    Check if user has sufficient leave balance

    Args:
        user: User object
        leave_type: LeaveType object
        days: Number of days requested
        year: Year to check balance for
        tenant: Tenant object

    Returns:
        tuple: (bool, str) - (has_balance, error_message)
    """
    from .models import LeaveBalance, LeaveSettings

    # Get or create balance for this year. Balances are allocated lazily: the
    # first time an employee needs one, it is created from the leave type's
    # yearly allowance (there is no screen where an admin would do it by hand).
    balance = LeaveBalance.objects.filter(
        tenant=tenant, user=user, leave_type=leave_type, year=year
    ).first()
    if balance is None:
        initialize_leave_balances_for_user(user, tenant, year)
        balance = LeaveBalance.objects.filter(
            tenant=tenant, user=user, leave_type=leave_type, year=year
        ).first()

    if balance is None:
        # No balance record, check if negative balance is allowed
        try:
            settings = LeaveSettings.objects.get(tenant=tenant)
            if settings.allow_negative_balance:
                if days <= settings.max_negative_days:
                    return True, ""
                return False, f"Requested days exceed maximum negative balance ({settings.max_negative_days} days)"
            return False, "No leave balance available for this year"
        except LeaveSettings.DoesNotExist:
            return False, "No leave balance available for this year"

    # Check available balance
    available = balance.available_days

    if available >= days:
        return True, ""

    # Check if negative balance is allowed
    try:
        settings = LeaveSettings.objects.get(tenant=tenant)
        if settings.allow_negative_balance:
            deficit = days - available
            if deficit <= settings.max_negative_days:
                return True, ""
            return False, f"Insufficient balance. You would need {deficit} days beyond your balance (max allowed: {settings.max_negative_days})"
    except LeaveSettings.DoesNotExist:
        pass

    return False, f"Insufficient balance. Available: {available} days, Requested: {days} days"


def update_leave_balance(leave_request, action='approve'):
    """
    Update leave balance after approval, rejection, or cancellation.
    Uses select_for_update to prevent race conditions.

    Args:
        leave_request: LeaveRequest object
        action: 'pending', 'approve', 'reject', 'cancel', or 'cancel_approved'
    """
    from .models import LeaveBalance

    year = leave_request.start_date.year

    with transaction.atomic():
        # Get or create balance with row lock
        balance, created = LeaveBalance.objects.select_for_update().get_or_create(
            tenant=leave_request.tenant,
            user=leave_request.employee,
            leave_type=leave_request.leave_type,
            year=year,
            defaults={
                'allocated_days': leave_request.leave_type.default_days_per_year
            }
        )

        if action == 'pending':
            # New request submitted — add to pending
            balance.pending_days += leave_request.total_days
        elif action == 'approve':
            # Move from pending to used
            balance.pending_days = max(Decimal('0'), balance.pending_days - leave_request.total_days)
            balance.used_days += leave_request.total_days
        elif action in ['reject', 'cancel']:
            # Remove from pending (request was pending, not yet approved)
            balance.pending_days = max(Decimal('0'), balance.pending_days - leave_request.total_days)
        elif action == 'cancel_approved':
            # Cancel an already-approved leave — return used days
            balance.used_days = max(Decimal('0'), balance.used_days - leave_request.total_days)

        balance.save()


def check_overlapping_leaves(employee, start_date, end_date, tenant, exclude_request_id=None):
    """
    Check if employee has overlapping leave requests

    Args:
        employee: User object
        start_date: Start date
        end_date: End date
        tenant: Tenant object
        exclude_request_id: Request ID to exclude (for updates)

    Returns:
        tuple: (bool, str) - (has_overlap, error_message)
    """
    from .models import LeaveRequest

    # Build query for overlapping dates
    query = Q(employee=employee, tenant=tenant)
    query &= Q(status__in=['pending', 'manager_approved', 'hr_approved', 'approved'])
    query &= (
        Q(start_date__lte=end_date, end_date__gte=start_date) |
        Q(start_date__range=[start_date, end_date]) |
        Q(end_date__range=[start_date, end_date])
    )

    if exclude_request_id:
        query &= ~Q(id=exclude_request_id)

    overlapping = LeaveRequest.objects.filter(query).first()

    if overlapping:
        return True, f"You have an overlapping leave request from {overlapping.start_date} to {overlapping.end_date}"

    return False, ""


def process_accrual(user, leave_type, tenant, current_date=None):
    """
    Calculate and add accrued leave days for accrual-based leave types

    Args:
        user: User object
        leave_type: LeaveType object with accrual calculation
        tenant: Tenant object
        current_date: Date to process accrual until (default: today)

    Returns:
        Decimal: Days accrued
    """
    from .models import LeaveBalance

    if leave_type.calculation_method != 'accrual':
        return Decimal('0')

    if current_date is None:
        current_date = date.today()

    year = current_date.year

    # Get or create balance
    balance, created = LeaveBalance.objects.get_or_create(
        tenant=tenant,
        user=user,
        leave_type=leave_type,
        year=year,
        defaults={'allocated_days': Decimal('0')}
    )

    # Determine start date for accrual calculation
    if balance.last_accrual_date:
        start_date = balance.last_accrual_date
    else:
        # Start from beginning of year or user's join date
        start_date = date(year, 1, 1)
        if hasattr(user, 'date_joined') and user.date_joined:
            join_date = user.date_joined.date() if isinstance(user.date_joined, datetime) else user.date_joined
            if join_date.year == year:
                start_date = join_date

    # Calculate months between start_date and current_date
    if current_date <= start_date:
        return Decimal('0')

    months_elapsed = (current_date.year - start_date.year) * 12 + (current_date.month - start_date.month)

    # Calculate days to accrue
    days_to_accrue = Decimal(str(months_elapsed)) * leave_type.accrual_rate_per_month

    if days_to_accrue > 0:
        balance.allocated_days += days_to_accrue
        balance.last_accrual_date = current_date
        balance.save()

    return days_to_accrue


def carryable_days(balance, leave_type):
    """
    Days of ``balance`` that may move to the next year: what is left of the
    year's allowance (allocated + carried in - used; pending requests are
    NOT subtracted, they belong to the old year and are settled there),
    capped at the leave type's max_carry_forward_days.
    """
    remaining = balance.allocated_days + balance.carried_forward_days - balance.used_days
    if remaining <= 0:
        return Decimal('0')
    return min(remaining, Decimal(str(leave_type.max_carry_forward_days)))


class CarryForwardAlreadyRun(Exception):
    """Raised when a carry forward for the year already ran and force is off."""

    def __init__(self, run):
        super().__init__(f'Carry forward from {run.from_year} already ran')
        self.run = run


def carry_forward_balances(tenant, from_year, to_year, user=None, force=False):
    """
    Carry forward unused leave balances from one year to the next.

    Idempotent: the run is recorded in LeaveCarryForwardRun (one row per
    tenant and from_year). A second run raises CarryForwardAlreadyRun unless
    ``force`` is set; a forced run recomputes and OVERWRITES the carried
    forward days of the target year, so it never double counts.

    Each target balance's ``carried_forward_days`` is set to (not increased
    by) the carryable amount, so re-applying gives the same result.

    Returns:
        tuple: (LeaveCarryForwardRun, int balances_updated)
    """
    from .models import LeaveBalance, LeaveType, LeaveCarryForwardRun

    from_year = int(from_year)
    to_year = int(to_year)

    with transaction.atomic():
        run = (
            LeaveCarryForwardRun.objects.select_for_update()
            .filter(tenant=tenant, from_year=from_year)
            .first()
        )
        if run is not None and not force:
            raise CarryForwardAlreadyRun(run)

        carried_forward = 0
        total_days = Decimal('0')

        leave_types = LeaveType.objects.filter(
            tenant=tenant,
            is_active=True,
            max_carry_forward_days__gt=0
        )

        for leave_type in leave_types:
            old_balances = LeaveBalance.objects.filter(
                tenant=tenant,
                leave_type=leave_type,
                year=from_year
            )

            for old_balance in old_balances:
                carry_days = carryable_days(old_balance, leave_type)

                new_balance = LeaveBalance.objects.select_for_update().filter(
                    tenant=tenant,
                    user_id=old_balance.user_id,
                    leave_type=leave_type,
                    year=to_year,
                ).first()

                if new_balance is None:
                    if carry_days <= 0:
                        continue
                    LeaveBalance.objects.create(
                        tenant=tenant,
                        user_id=old_balance.user_id,
                        leave_type=leave_type,
                        year=to_year,
                        allocated_days=(
                            leave_type.default_days_per_year
                            if leave_type.calculation_method == 'annual'
                            else Decimal('0')
                        ),
                        carried_forward_days=carry_days,
                    )
                elif new_balance.carried_forward_days != carry_days:
                    new_balance.carried_forward_days = carry_days
                    new_balance.save(update_fields=['carried_forward_days', 'updated_at'])
                elif carry_days <= 0:
                    continue

                if carry_days > 0:
                    carried_forward += 1
                    total_days += carry_days

        if run is None:
            run = LeaveCarryForwardRun.objects.create(
                tenant=tenant,
                from_year=from_year,
                to_year=to_year,
                run_by=user,
                balances_count=carried_forward,
                total_days=total_days,
            )
        else:
            run.to_year = to_year
            run.run_by = user
            run.balances_count = carried_forward
            run.total_days = total_days
            run.forced_runs += 1
            run.save()

    return run, carried_forward


def get_approval_chain(leave_request):
    """
    Get the required approval chain for a leave request

    Args:
        leave_request: LeaveRequest object

    Returns:
        list: List of approval levels with required roles
    """
    from .models import LeaveApprovalChain, LeaveSettings

    tenant = leave_request.tenant
    leave_type = leave_request.leave_type

    # Check if leave type requires approval
    if not leave_type.requires_approval:
        return []

    # Get specific approval chain for this leave type
    chain = LeaveApprovalChain.objects.filter(
        tenant=tenant,
        leave_type=leave_type,
        is_required=True
    ).order_by('level')

    # If no specific chain, get default chain (leave_type=null)
    if not chain.exists():
        chain = LeaveApprovalChain.objects.filter(
            tenant=tenant,
            leave_type__isnull=True,
            is_required=True
        ).order_by('level')

    # If no chain configured, use settings defaults
    if not chain.exists():
        try:
            settings = LeaveSettings.objects.get(tenant=tenant)
            default_chain = []
            if settings.require_manager_approval:
                default_chain.append({'level': 1, 'role': 'manager'})
            if settings.require_hr_approval:
                default_chain.append({'level': 2, 'role': 'hr'})
            return default_chain
        except LeaveSettings.DoesNotExist:
            return [{'level': 1, 'role': 'manager'}]  # Default to manager approval

    return [{'level': item.level, 'role': item.approver_role} for item in chain]


# Approval progress. A request moves pending -> manager_approved ->
# hr_approved -> approved; each status says which roles have signed off.
# Chain steps are matched by ROLE (not by their numeric level), so an
# HR-only chain stored at level 1 or 2 behaves the same and an 'admin' step
# after HR is never skipped.
APPROVAL_STATUS_RANK = {
    'pending': 0,
    'manager_approved': 1,
    'hr_approved': 2,
}
APPROVER_ROLE_RANK = {
    'manager': 1,
    'hr': 2,
    'admin': 3,
}
# Status a request gets once a step of the given role signs off. 'admin' is
# always the last possible step, so it ends the chain.
ROLE_APPROVED_STATUS = {
    'manager': 'manager_approved',
    'hr': 'hr_approved',
    'admin': 'approved',
}
OPEN_APPROVAL_STATUSES = tuple(APPROVAL_STATUS_RANK)


def get_next_approver_role(leave_request):
    """
    Determine the next required approver role based on current status

    Args:
        leave_request: LeaveRequest object

    Returns:
        str or None: Next approver role ('manager', 'hr', 'admin') or None if
        fully approved, closed (approved/rejected/cancelled) or no approval
        is required.
    """
    current_rank = APPROVAL_STATUS_RANK.get(leave_request.status)
    if current_rank is None:
        # approved / rejected / cancelled: nothing left to approve. (This
        # used to fall back to rank 0 and report 'manager' for an approved
        # request, so the final approval never moved the balance.)
        return None

    chain = get_approval_chain(leave_request)

    if not chain:
        return None

    for approval in chain:
        role_rank = APPROVER_ROLE_RANK.get(approval['role'], APPROVER_ROLE_RANK['admin'])
        if role_rank > current_rank:
            return approval['role']

    return None  # All approvals completed


def apply_approval_step(leave_request, user, comments=''):
    """
    Record ``user``'s approval of the next open step on ``leave_request``.

    Sets the approver fields for the step's role, moves the status forward
    and, when no step is left, finalises the request: status 'approved',
    final approver recorded, pending days moved to used days. The caller
    saves the request (inside its own transaction).

    Returns the role of the step that was approved (None when the request
    needed no approval at all and was approved directly).
    """
    now = timezone.now()
    role = get_next_approver_role(leave_request)

    if role == 'manager':
        leave_request.manager_approver = user
        leave_request.manager_approved_at = now
        leave_request.manager_comments = comments or ''
        leave_request.status = 'manager_approved'
    elif role == 'hr':
        leave_request.hr_approver = user
        leave_request.hr_approved_at = now
        leave_request.hr_comments = comments or ''
        leave_request.status = 'hr_approved'
    elif role == 'admin':
        leave_request.final_approver = user
        leave_request.final_approved_at = now
        leave_request.final_comments = comments or ''
        leave_request.status = 'approved'

    if leave_request.status == 'approved' or get_next_approver_role(leave_request) is None:
        leave_request.status = 'approved'
        if not leave_request.final_approver_id:
            leave_request.final_approver = user
            leave_request.final_approved_at = now
            if role is None:
                leave_request.final_comments = comments or ''
        update_leave_balance(leave_request, action='approve')

    return role


def can_user_approve(user, leave_request):
    """
    Check if user has permission to approve this leave request

    Args:
        user: User object
        leave_request: LeaveRequest object

    Returns:
        tuple: (bool, str) - (can_approve, reason)
    """
    next_role = get_next_approver_role(leave_request)

    if not next_role:
        return False, "Leave request is already fully approved or does not require further approval"

    if next_role == 'manager':
        # The employee's direct manager (or department manager, or staff)
        if manages_employee(user, leave_request.employee):
            return True, ""
        # Or has an explicit approval permission
        if user.has_perm('leave_management.approve_leave'):
            return True, ""
        return False, "You are not the employee's manager"

    elif next_role == 'hr':
        # Check if user has HR role/permissions
        if user.has_perm('leave_management.approve_leave_hr') or user.is_staff:
            return True, ""
        return False, "You do not have HR approval permissions"

    elif next_role == 'admin':
        if user.is_staff or user.is_superuser:
            return True, ""
        return False, "You do not have admin permissions"

    return False, "Unable to determine approval permissions"


def initialize_leave_balances_for_user(user, tenant, year=None):
    """
    Initialize leave balances for a new user or new year

    Args:
        user: User object
        tenant: Tenant object
        year: Year to initialize (default: current year)

    Returns:
        int: Number of balances created
    """
    from .models import LeaveBalance, LeaveType

    if year is None:
        year = date.today().year

    # Get all active leave types
    leave_types = LeaveType.objects.filter(tenant=tenant, is_active=True)

    created_count = 0
    for leave_type in leave_types:
        # Only initialize for annual and accrual types
        if leave_type.calculation_method in ['annual', 'accrual']:
            balance, created = LeaveBalance.objects.get_or_create(
                tenant=tenant,
                user=user,
                leave_type=leave_type,
                year=year,
                defaults={
                    'allocated_days': leave_type.default_days_per_year if leave_type.calculation_method == 'annual' else Decimal('0')
                }
            )
            if created:
                created_count += 1

    return created_count
