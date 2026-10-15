from rest_framework import permissions


class HasLeaveManagementFeature(permissions.BasePermission):
    """
    Permission check: Tenant has leave_management feature enabled
    """
    message = "Your organization does not have access to the Leave Management feature."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # Check if tenant has the feature enabled
        if hasattr(request, 'tenant_has_feature'):
            return request.tenant_has_feature('leave_management')

        # Fallback to legacy feature check
        if hasattr(request.user, 'has_feature'):
            return request.user.has_feature('leave_management')

        return False


class IsLeaveEmployee(permissions.BasePermission):
    """
    Object-level permission: Employee can only access their own leave requests
    """
    message = "You can only access your own leave requests."

    def has_object_permission(self, request, view, obj):
        # Admins and staff have full access
        if request.user.is_staff or request.user.is_superuser:
            return True

        # Check if the leave request belongs to the user
        return obj.employee == request.user


class IsLeaveManager(permissions.BasePermission):
    """
    Permission check: User is a manager (staff, 'manager' role, has direct
    reports, or the view_team_leaves permission)
    Object-level: Can access team members' leave requests
    """
    message = "You do not have manager permissions."

    def has_permission(self, request, view):
        from .utils import is_leave_manager

        if not request.user or not request.user.is_authenticated:
            return False

        return is_leave_manager(request.user)

    def has_object_permission(self, request, view, obj):
        from .utils import manages_employee

        # Staff pass through; otherwise the employee must be on the user's
        # team. LeaveRequest points at the employee via ``employee``,
        # LeaveBalance via ``user``.
        employee = getattr(obj, 'employee', None) or getattr(obj, 'user', None)
        return manages_employee(request.user, employee)


class CanApproveLeave(permissions.BasePermission):
    """
    Object-level permission: User can approve leave requests based on approval chain
    """
    message = "You do not have permission to approve this leave request."

    def has_object_permission(self, request, view, obj):
        from .utils import can_user_approve

        # Admins always have permission
        if request.user.is_staff or request.user.is_superuser:
            return True

        # Check using utility function
        can_approve, reason = can_user_approve(request.user, obj)

        if not can_approve:
            self.message = reason

        return can_approve


class CanManageLeaveSettings(permissions.BasePermission):
    """
    Permission check: User can manage leave settings (admin only)
    """
    message = "Only administrators can manage leave settings."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # Only staff and superusers can manage settings
        return request.user.is_staff or request.user.is_superuser


class CanManageLeaveTypes(permissions.BasePermission):
    """
    Permission check: User can manage leave types (admin only)
    """
    message = "Only administrators can manage leave types."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # Only staff and superusers can manage leave types
        return request.user.is_staff or request.user.is_superuser


class CanManageLeaveBalances(permissions.BasePermission):
    """
    Permission check: User can manage leave balances (admin/HR only)
    """
    message = "Only administrators and HR can manage leave balances."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # Staff and superusers have access
        if request.user.is_staff or request.user.is_superuser:
            return True

        # HR role has access
        if request.user.has_perm('leave_management.manage_balances'):
            return True

        return False


class CanViewTeamBalance(permissions.BasePermission):
    """
    Object-level permission: Manager can view their team's leave balances
    """
    message = "You can only view your team members' balances."

    def has_object_permission(self, request, view, obj):
        from .utils import manages_employee

        # Employees can view their own balance
        if obj.user == request.user:
            return True

        # Staff and managers can view their team's balances
        return manages_employee(request.user, obj.user)


class CanManagePublicHolidays(permissions.BasePermission):
    """
    Permission check: User can manage public holidays (admin only)
    """
    message = "Only administrators can manage public holidays."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        # Only staff and superusers can manage holidays
        return request.user.is_staff or request.user.is_superuser


class CanCancelLeave(permissions.BasePermission):
    """
    Object-level permission: User can cancel a leave request
    """
    message = "You cannot cancel this leave request."

    def has_object_permission(self, request, view, obj):
        from .utils import manages_employee

        # Admins can cancel any leave
        if request.user.is_staff or request.user.is_superuser:
            return True

        # Employees can cancel their own pending leaves
        if obj.employee == request.user and obj.status in ['pending', 'manager_approved', 'hr_approved']:
            return True

        # Managers can cancel their team's pending leaves
        if obj.status in ['pending', 'manager_approved']:
            return manages_employee(request.user, obj.employee)

        return False


class IsLeaveOwnerOrManager(permissions.BasePermission):
    """
    Object-level permission: User is the leave owner or their manager
    """
    message = "You can only access your own leaves or your team members' leaves."

    def has_object_permission(self, request, view, obj):
        from .utils import manages_employee

        # Owner has access
        if obj.employee == request.user:
            return True

        # Staff and the employee's manager have access
        return manages_employee(request.user, obj.employee)
