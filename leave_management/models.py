from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils.translation import gettext_lazy as _
from decimal import Decimal


class LeaveSettings(models.Model):
    """Tenant-wide leave management configuration"""
    tenant = models.OneToOneField(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        related_name='leave_settings'
    )
    require_manager_approval = models.BooleanField(
        default=True,
        help_text="Require manager approval for leave requests"
    )
    require_hr_approval = models.BooleanField(
        default=False,
        help_text="Require HR approval after manager approval"
    )
    allow_negative_balance = models.BooleanField(
        default=False,
        help_text="Allow employees to take leave in advance"
    )
    max_negative_days = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Maximum negative balance days allowed"
    )
    working_days_per_week = models.IntegerField(
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(7)],
        help_text="Number of working days per week for calculations"
    )
    weekend_days = models.JSONField(
        default=list,
        blank=True,
        help_text="Weekend days (0=Monday, 6=Sunday). Default: [5,6] for Sat/Sun"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Leave Settings'
        verbose_name_plural = 'Leave Settings'
        permissions = [
            ('manage_leave_settings', 'Can manage leave settings'),
            ('view_leave_settings', 'Can view leave settings'),
        ]

    def __str__(self):
        return f"Leave Settings for {self.tenant.name}"


class LeaveType(models.Model):
    """Configurable leave categories (Vacation, Sick, Personal, etc.)"""

    CALCULATION_METHODS = [
        ('annual', 'Annual Allocation'),
        ('accrual', 'Accrual Based'),
        ('manual', 'Manual Assignment'),
    ]

    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        related_name='leave_types'
    )
    name = models.JSONField(
        help_text="Leave type name in different languages {'en': 'Vacation', 'ka': 'შვებულება'}"
    )
    code = models.CharField(
        max_length=20,
        help_text="Unique code for this leave type (e.g., VAC, SICK, PERSONAL)"
    )
    description = models.JSONField(
        blank=True,
        default=dict,
        help_text="Description in different languages"
    )
    is_paid = models.BooleanField(
        default=True,
        help_text="Is this a paid leave type?"
    )
    requires_approval = models.BooleanField(
        default=True,
        help_text="Does this leave type require approval?"
    )
    calculation_method = models.CharField(
        max_length=20,
        choices=CALCULATION_METHODS,
        default='annual',
        help_text="How leave balance is calculated"
    )
    default_days_per_year = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=0,
        validators=[MinValueValidator(Decimal('0'))],
        help_text="Default annual allocation (for annual calculation)"
    )
    accrual_rate_per_month = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(Decimal('0'))],
        help_text="Days earned per month (for accrual calculation)"
    )
    max_carry_forward_days = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Maximum days that can be carried forward to next year"
    )
    carry_forward_expiry_months = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Months until carried forward days expire (0 = no expiry)"
    )
    color = models.CharField(
        max_length=7,
        default='#3B82F6',
        help_text="Hex color code for UI display"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Is this leave type currently active?"
    )
    sort_order = models.IntegerField(
        default=0,
        help_text="Display order"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_leave_types'
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='updated_leave_types'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'id']
        verbose_name = 'Leave Type'
        verbose_name_plural = 'Leave Types'
        unique_together = [['tenant', 'code']]
        indexes = [
            models.Index(fields=['tenant', 'is_active']),
            models.Index(fields=['code']),
        ]
        permissions = [
            ('manage_leave_types', 'Can manage leave types'),
            ('view_leave_types', 'Can view leave types'),
        ]

    def __str__(self):
        if isinstance(self.name, dict):
            return self.name.get('en', self.name.get(list(self.name.keys())[0], self.code))
        return str(self.name)

    def get_name(self, language='en'):
        """Get leave type name in specific language"""
        if isinstance(self.name, dict):
            return self.name.get(language, self.name.get('en', self.code))
        return str(self.name)

    def get_description(self, language='en'):
        """Get description in specific language"""
        if isinstance(self.description, dict):
            return self.description.get(language, self.description.get('en', ''))
        return str(self.description)


class LeaveBalance(models.Model):
    """Employee leave balances per leave type and year"""
    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        related_name='leave_balances'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='leave_balances'
    )
    leave_type = models.ForeignKey(
        LeaveType,
        on_delete=models.CASCADE,
        related_name='balances'
    )
    year = models.IntegerField(
        help_text="Year for this balance (e.g., 2025)"
    )
    allocated_days = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=0,
        validators=[MinValueValidator(Decimal('0'))],
        help_text="Total days allocated for this year"
    )
    used_days = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=0,
        validators=[MinValueValidator(Decimal('0'))],
        help_text="Days used (approved leaves)"
    )
    carried_forward_days = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=0,
        validators=[MinValueValidator(Decimal('0'))],
        help_text="Days carried forward from previous year"
    )
    pending_days = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=0,
        validators=[MinValueValidator(Decimal('0'))],
        help_text="Days in pending leave requests"
    )
    last_accrual_date = models.DateField(
        null=True,
        blank=True,
        help_text="Last date when accrual was processed"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Leave Balance'
        verbose_name_plural = 'Leave Balances'
        unique_together = [['tenant', 'user', 'leave_type', 'year']]
        indexes = [
            models.Index(fields=['tenant', 'user', 'year']),
            models.Index(fields=['leave_type', 'year']),
        ]
        permissions = [
            ('manage_leave_balances', 'Can manage leave balances'),
            ('view_leave_balances', 'Can view leave balances'),
            ('view_own_balance', 'Can view own leave balance'),
            ('view_team_balances', 'Can view team leave balances'),
        ]

    def __str__(self):
        return f"{self.user.get_full_name()} - {self.leave_type} ({self.year})"

    @property
    def available_days(self):
        """Calculate available days (allocated + carried forward - used - pending)"""
        return self.allocated_days + self.carried_forward_days - self.used_days - self.pending_days

    @property
    def total_allocated(self):
        """Total days including carried forward"""
        return self.allocated_days + self.carried_forward_days


class LeaveRequest(models.Model):
    """Leave request submitted by employees"""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('manager_approved', 'Manager Approved'),
        ('hr_approved', 'HR Approved'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ]

    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        related_name='leave_requests'
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='leave_requests'
    )
    leave_type = models.ForeignKey(
        LeaveType,
        on_delete=models.PROTECT,
        related_name='requests'
    )
    start_date = models.DateField(
        help_text="Leave start date"
    )
    end_date = models.DateField(
        help_text="Leave end date (inclusive)"
    )
    total_days = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        validators=[MinValueValidator(Decimal('0.5'))],
        help_text="Total working days (excluding weekends/holidays)"
    )
    reason = models.TextField(
        blank=True,
        help_text="Reason for leave request"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )

    # Manager approval
    manager_approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='manager_approved_leaves'
    )
    manager_approved_at = models.DateTimeField(null=True, blank=True)
    manager_comments = models.TextField(blank=True)

    # HR approval
    hr_approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hr_approved_leaves'
    )
    hr_approved_at = models.DateTimeField(null=True, blank=True)
    hr_comments = models.TextField(blank=True)

    # Final approval
    final_approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='final_approved_leaves'
    )
    final_approved_at = models.DateTimeField(null=True, blank=True)
    final_comments = models.TextField(
        blank=True,
        default='',
        help_text="Comments left by the final (admin) approver"
    )

    # Rejection
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rejected_leaves'
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)

    # Cancellation
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True)

    # Optional attachment
    attachment = models.URLField(
        max_length=2000,
        blank=True,
        help_text="Supporting document URL (e.g., medical certificate)"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Leave Request'
        verbose_name_plural = 'Leave Requests'
        indexes = [
            models.Index(fields=['tenant', 'employee', 'status']),
            models.Index(fields=['tenant', 'status', 'start_date']),
            models.Index(fields=['leave_type', 'status']),
            models.Index(fields=['start_date', 'end_date']),
        ]
        permissions = [
            ('manage_leave_requests', 'Can manage all leave requests'),
            ('view_leave_requests', 'Can view all leave requests'),
            ('approve_leave', 'Can approve leave requests'),
            ('approve_leave_hr', 'Can approve leave requests as HR'),
            ('reject_leave', 'Can reject leave requests'),
            ('cancel_leave', 'Can cancel leave requests'),
            ('view_own_leaves', 'Can view own leave requests'),
            ('submit_leave', 'Can submit leave requests'),
            ('view_team_leaves', 'Can view team leave requests'),
            ('approve_team_leaves', 'Can approve team leave requests'),
        ]

    def __str__(self):
        return f"{self.employee.get_full_name()} - {self.leave_type} ({self.start_date} to {self.end_date})"

    @property
    def is_pending(self):
        """Check if request is in any pending state"""
        return self.status in ['pending', 'manager_approved', 'hr_approved']

    @property
    def is_approved(self):
        """Check if fully approved"""
        return self.status == 'approved'

    @property
    def is_rejected(self):
        """Check if rejected"""
        return self.status == 'rejected'

    @property
    def is_cancelled(self):
        """Check if cancelled"""
        return self.status == 'cancelled'


class PublicHoliday(models.Model):
    """Company-wide public holidays"""
    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        related_name='public_holidays'
    )
    name = models.JSONField(
        help_text="Holiday name in different languages {'en': 'New Year', 'ka': 'ახალი წელი'}"
    )
    date = models.DateField(
        help_text="Holiday date"
    )
    is_recurring = models.BooleanField(
        default=False,
        help_text="Does this holiday recur annually?"
    )
    applies_to_all = models.BooleanField(
        default=True,
        help_text="Applies to all employees or specific departments"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_holidays'
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='updated_holidays'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['date']
        verbose_name = 'Public Holiday'
        verbose_name_plural = 'Public Holidays'
        indexes = [
            models.Index(fields=['tenant', 'date']),
            models.Index(fields=['date']),
        ]
        permissions = [
            ('manage_public_holidays', 'Can manage public holidays'),
            ('view_public_holidays', 'Can view public holidays'),
        ]

    def __str__(self):
        name = self.name if isinstance(self.name, str) else self.name.get('en', 'Holiday')
        return f"{name} - {self.date}"

    def get_name(self, language='en'):
        """Get holiday name in specific language"""
        if isinstance(self.name, dict):
            return self.name.get(language, self.name.get('en', 'Holiday'))
        return str(self.name)


class LeaveApprovalChain(models.Model):
    """Configurable approval workflow for leave requests"""

    APPROVER_ROLES = [
        ('manager', 'Manager'),
        ('hr', 'HR'),
        ('admin', 'Admin'),
    ]

    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        related_name='leave_approval_chains'
    )
    leave_type = models.ForeignKey(
        LeaveType,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='approval_chains',
        help_text="Leave type (null means applies to all types)"
    )
    level = models.IntegerField(
        validators=[MinValueValidator(1)],
        help_text="Approval level (1 = first, 2 = second, etc.)"
    )
    approver_role = models.CharField(
        max_length=20,
        choices=APPROVER_ROLES,
        help_text="Role required to approve at this level"
    )
    is_required = models.BooleanField(
        default=True,
        help_text="Is this approval level required?"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['level']
        verbose_name = 'Leave Approval Chain'
        verbose_name_plural = 'Leave Approval Chains'
        unique_together = [['tenant', 'leave_type', 'level']]
        indexes = [
            models.Index(fields=['tenant', 'leave_type']),
        ]
        permissions = [
            ('manage_approval_chains', 'Can manage leave approval chains'),
            ('view_approval_chains', 'Can view leave approval chains'),
        ]

    def __str__(self):
        type_name = f"{self.leave_type}" if self.leave_type else "All Leave Types"
        return f"{type_name} - Level {self.level} ({self.approver_role})"


class LeaveCarryForwardRun(models.Model):
    """
    Marker that a year's balances were carried forward, so the action is a
    no-op when it is triggered again (one row per tenant and source year).
    """
    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        related_name='leave_carry_forward_runs'
    )
    from_year = models.IntegerField(help_text="Year the unused days came from")
    to_year = models.IntegerField(help_text="Year the days were carried into")
    run_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='leave_carry_forward_runs'
    )
    balances_count = models.IntegerField(
        default=0,
        help_text="Balances that received carried forward days"
    )
    total_days = models.DecimalField(
        max_digits=8,
        decimal_places=1,
        default=0,
        help_text="Sum of the carried forward days"
    )
    forced_runs = models.IntegerField(
        default=0,
        help_text="How many times a superuser forced a recompute"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Leave Carry Forward Run'
        verbose_name_plural = 'Leave Carry Forward Runs'
        unique_together = [['tenant', 'from_year']]

    def __str__(self):
        return f"Carry forward {self.from_year} -> {self.to_year}"
