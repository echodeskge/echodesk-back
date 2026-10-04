import logging
from datetime import datetime, timedelta, date
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


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


def carry_forward_balances(tenant, from_year, to_year):
    """
    Carry forward unused leave balances from one year to next

    Args:
        tenant: Tenant object
        from_year: Year to carry forward from
        to_year: Year to carry forward to

    Returns:
        int: Number of balances carried forward
    """
    from .models import LeaveBalance, LeaveType

    carried_forward = 0

    # Get all leave types that allow carry forward
    leave_types = LeaveType.objects.filter(
        tenant=tenant,
        is_active=True,
        max_carry_forward_days__gt=0
    )

    for leave_type in leave_types:
        # Get all balances for from_year
        old_balances = LeaveBalance.objects.filter(
            tenant=tenant,
            leave_type=leave_type,
            year=from_year
        )

        for old_balance in old_balances:
            # Calculate unused days
            unused_days = old_balance.available_days

            if unused_days > 0:
                # Limit to max carry forward
                carry_forward_days = min(
                    unused_days,
                    Decimal(str(leave_type.max_carry_forward_days))
                )

                # Create or update balance for new year
                new_balance, created = LeaveBalance.objects.get_or_create(
                    tenant=tenant,
                    user=old_balance.user,
                    leave_type=leave_type,
                    year=to_year,
                    defaults={
                        'allocated_days': leave_type.default_days_per_year,
                        'carried_forward_days': carry_forward_days
                    }
                )

                if not created:
                    new_balance.carried_forward_days += carry_forward_days
                    new_balance.save()

                carried_forward += 1

    return carried_forward


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


def get_next_approver_role(leave_request):
    """
    Determine the next required approver role based on current status

    Args:
        leave_request: LeaveRequest object

    Returns:
        str or None: Next approver role ('manager', 'hr', 'admin') or None if fully approved
    """
    chain = get_approval_chain(leave_request)

    if not chain:
        return None

    status_to_level = {
        'pending': 0,
        'manager_approved': 1,
        'hr_approved': 2,
    }

    current_level = status_to_level.get(leave_request.status, 0)

    # Find next required level
    for approval in chain:
        if approval['level'] > current_level:
            return approval['role']

    return None  # All approvals completed


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

    # Check if user has the required role
    # This would need to be implemented based on your user role system
    # For now, we'll check basic permissions

    if next_role == 'manager':
        # Check if user is employee's manager
        if hasattr(leave_request.employee, 'manager') and leave_request.employee.manager == user:
            return True, ""
        # Or has manager permissions
        if user.is_staff or user.has_perm('leave_management.approve_leave'):
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
