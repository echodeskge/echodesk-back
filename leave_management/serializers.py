from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.utils import timezone
from decimal import Decimal
from drf_spectacular.utils import extend_schema_field
from .models import (
    LeaveSettings, LeaveType, LeaveBalance, LeaveRequest,
    PublicHoliday, LeaveApprovalChain, LeaveAuditEvent,
    SubstituteAssignment, LeaveRequestSubstitute,
)
from .utils import (
    calculate_working_days, check_leave_balance,
    check_overlapping_leaves, update_leave_balance, validate_type_limits
)

User = get_user_model()


# ============================================================================
# LEAVE SETTINGS SERIALIZERS
# ============================================================================

class LeaveSettingsSerializer(serializers.ModelSerializer):
    """Serializer for Leave Settings"""

    class Meta:
        model = LeaveSettings
        fields = [
            'id', 'tenant', 'require_manager_approval', 'require_hr_approval',
            'allow_negative_balance', 'max_negative_days', 'working_days_per_week',
            'weekend_days',
            'substitutes_enabled', 'substitution_pay_enabled', 'payroll_enabled',
            'payroll_self_view_enabled', 'kpi_enabled', 'attendance_grid_enabled',
            'auto_georgian_holidays', 'block_deactivation_with_open_requests',
            'payroll_mode', 'income_tax_rate', 'pension_rate', 'payroll_currency',
            'payroll_managers',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['tenant', 'created_at', 'updated_at']
        extra_kwargs = {'payroll_managers': {'required': False}}

    def validate_payroll_currency(self, value):
        value = (value or '').upper()
        if len(value) != 3 or not value.isalpha():
            raise serializers.ValidationError('Use a 3-letter currency code, e.g. GEL')
        return value

    def validate(self, attrs):
        # One settings row per tenant (OneToOne): a second POST used to
        # raise an IntegrityError (500). PATCH/PUT the existing row instead.
        if self.instance is None:
            request = self.context.get('request')
            tenant = getattr(request, 'tenant', None)
            if tenant is not None and LeaveSettings.objects.filter(tenant=tenant).exists():
                raise serializers.ValidationError(
                    'Leave settings already exist for this organization; update them instead'
                )
        return attrs


# ============================================================================
# LEAVE TYPE SERIALIZERS
# ============================================================================

class LeaveTypeListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for listing leave types"""
    name_display = serializers.SerializerMethodField()

    class Meta:
        model = LeaveType
        fields = [
            'id', 'code', 'name', 'name_display', 'is_paid',
            'requires_approval', 'color', 'is_active', 'sort_order',
            'calculation_method', 'default_days_per_year', 'accrual_rate_per_month',
            'max_carry_forward_days', 'carry_forward_expiry_months',
            'requires_substitutes', 'max_requests_per_month', 'max_days_per_request',
        ]

    def get_name_display(self, obj):
        language = self.context.get('language', 'en')
        return obj.get_name(language)


class LeaveTypeDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for leave type with all information"""
    name_display = serializers.SerializerMethodField()
    description_display = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LeaveType
        fields = [
            'id', 'tenant', 'code', 'name', 'name_display', 'description',
            'description_display', 'is_paid', 'requires_approval',
            'calculation_method', 'default_days_per_year', 'accrual_rate_per_month',
            'max_carry_forward_days', 'carry_forward_expiry_months', 'color',
            'is_active', 'sort_order',
            'requires_substitutes', 'max_requests_per_month', 'max_days_per_request',
            'created_by', 'created_by_name',
            'updated_by', 'updated_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['tenant', 'created_by', 'updated_by', 'created_at', 'updated_at']

    def get_name_display(self, obj):
        language = self.context.get('language', 'en')
        return obj.get_name(language)

    def get_description_display(self, obj):
        language = self.context.get('language', 'en')
        return obj.get_description(language)

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name() or obj.created_by.email
        return None

    def get_updated_by_name(self, obj):
        if obj.updated_by:
            return obj.updated_by.get_full_name() or obj.updated_by.email
        return None


class LeaveTypeCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating leave types"""

    class Meta:
        model = LeaveType
        fields = [
            'name', 'code', 'description', 'is_paid', 'requires_approval',
            'calculation_method', 'default_days_per_year', 'accrual_rate_per_month',
            'max_carry_forward_days', 'carry_forward_expiry_months', 'color',
            'is_active', 'sort_order',
            'requires_substitutes', 'max_requests_per_month', 'max_days_per_request',
        ]

    def validate_code(self, value):
        """Ensure code is unique within tenant (codes are stored upper-case)"""
        value = value.upper()
        tenant = self.context['request'].tenant
        queryset = LeaveType.objects.filter(tenant=tenant, code__iexact=value)

        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)

        if queryset.exists():
            raise serializers.ValidationError(f"Leave type with code '{value}' already exists")

        return value

    def _current(self, attrs, field, default):
        """Submitted value, else the stored one (PATCH), else the default."""
        if field in attrs:
            return attrs[field]
        if self.instance is not None:
            return getattr(self.instance, field)
        return default

    def validate(self, attrs):
        """Validate calculation method fields"""
        calculation_method = self._current(attrs, 'calculation_method', 'annual')

        if calculation_method == 'annual':
            if (self._current(attrs, 'default_days_per_year', 0) or 0) <= 0:
                raise serializers.ValidationError({
                    'default_days_per_year': 'Must be greater than 0 for annual calculation'
                })
        elif calculation_method == 'accrual':
            if (self._current(attrs, 'accrual_rate_per_month', 0) or 0) <= 0:
                raise serializers.ValidationError({
                    'accrual_rate_per_month': 'Must be greater than 0 for accrual calculation'
                })

        return attrs


# ============================================================================
# LEAVE BALANCE SERIALIZERS
# ============================================================================

class LeaveBalanceListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for listing leave balances"""
    employee_name = serializers.SerializerMethodField()
    leave_type_name = serializers.SerializerMethodField()
    leave_type_code = serializers.CharField(source='leave_type.code', read_only=True)
    available_days = serializers.DecimalField(max_digits=5, decimal_places=1, read_only=True)
    total_allocated = serializers.DecimalField(max_digits=5, decimal_places=1, read_only=True)

    class Meta:
        model = LeaveBalance
        fields = [
            'id', 'user', 'employee_name', 'leave_type', 'leave_type_name',
            'leave_type_code', 'year', 'allocated_days', 'used_days',
            'carried_forward_days', 'pending_days', 'available_days', 'total_allocated'
        ]

    def get_employee_name(self, obj):
        return obj.user.get_full_name() or obj.user.email

    def get_leave_type_name(self, obj):
        language = self.context.get('language', 'en')
        return obj.leave_type.get_name(language)


class LeaveBalanceDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for leave balance"""
    employee_name = serializers.SerializerMethodField()
    employee_email = serializers.EmailField(source='user.email', read_only=True)
    leave_type_details = LeaveTypeListSerializer(source='leave_type', read_only=True)
    available_days = serializers.DecimalField(max_digits=5, decimal_places=1, read_only=True)
    total_allocated = serializers.DecimalField(max_digits=5, decimal_places=1, read_only=True)

    class Meta:
        model = LeaveBalance
        fields = [
            'id', 'tenant', 'user', 'employee_name', 'employee_email',
            'leave_type', 'leave_type_details', 'year', 'allocated_days',
            'used_days', 'carried_forward_days', 'pending_days',
            'available_days', 'total_allocated', 'last_accrual_date',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['tenant', 'created_at', 'updated_at']

    def get_employee_name(self, obj):
        return obj.user.get_full_name() or obj.user.email


class LeaveBalanceUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating leave balances (admin only)"""

    class Meta:
        model = LeaveBalance
        fields = ['allocated_days', 'carried_forward_days']

    def validate_allocated_days(self, value):
        if value < 0:
            raise serializers.ValidationError("Allocated days cannot be negative")
        return value

    def validate_carried_forward_days(self, value):
        if value < 0:
            raise serializers.ValidationError("Carried forward days cannot be negative")
        return value


# ============================================================================
# LEAVE REQUEST SERIALIZERS
# ============================================================================

class LeaveRequestListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for listing leave requests"""
    employee_name = serializers.SerializerMethodField()
    leave_type_name = serializers.SerializerMethodField()
    leave_type_color = serializers.CharField(source='leave_type.color', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = LeaveRequest
        fields = [
            'id', 'employee', 'employee_name', 'leave_type', 'leave_type_name',
            'leave_type_color', 'start_date', 'end_date', 'total_days',
            'status', 'status_display', 'created_at'
        ]

    def get_employee_name(self, obj):
        return obj.employee.get_full_name() or obj.employee.email

    def get_leave_type_name(self, obj):
        language = self.context.get('language', 'en')
        return obj.leave_type.get_name(language)


class LeaveRequestSubstituteSerializer(serializers.ModelSerializer):
    """A substitute's consent on a request (pay fields are on the payroll API)."""
    substitute_name = serializers.SerializerMethodField()
    substitute_email = serializers.EmailField(source='substitute.email', read_only=True)

    class Meta:
        model = LeaveRequestSubstitute
        fields = [
            'id', 'substitute', 'substitute_name', 'substitute_email', 'task',
            'status', 'responded_at', 'comment',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField())
    def get_substitute_name(self, obj):
        return obj.substitute.get_full_name() or obj.substitute.email


class SubstituteInputSerializer(serializers.Serializer):
    substitute = serializers.IntegerField()
    task = serializers.CharField(max_length=300)


class LeaveRequestDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for leave request with full approval history"""
    employee_name = serializers.SerializerMethodField()
    employee_email = serializers.EmailField(source='employee.email', read_only=True)
    leave_type_details = LeaveTypeListSerializer(source='leave_type', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    # Approval history
    manager_approver_name = serializers.SerializerMethodField()
    hr_approver_name = serializers.SerializerMethodField()
    final_approver_name = serializers.SerializerMethodField()
    rejected_by_name = serializers.SerializerMethodField()
    history = serializers.SerializerMethodField()
    substitutes = LeaveRequestSubstituteSerializer(many=True, read_only=True)
    substitutes_skipped_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        fields = [
            'id', 'tenant', 'employee', 'employee_name', 'employee_email',
            'leave_type', 'leave_type_details', 'start_date', 'end_date',
            'total_days', 'reason', 'status', 'status_display',
            'manager_approver', 'manager_approver_name', 'manager_approved_at',
            'manager_comments', 'hr_approver', 'hr_approver_name', 'hr_approved_at',
            'hr_comments', 'final_approver', 'final_approver_name', 'final_approved_at',
            'final_comments',
            'rejected_by', 'rejected_by_name', 'rejected_at', 'rejection_reason',
            'cancelled_at', 'cancellation_reason', 'attachment',
            'substitutes', 'substitutes_skipped_by', 'substitutes_skipped_by_name',
            'substitutes_skipped_at', 'substitutes_skip_reason',
            'history', 'created_at', 'updated_at'
        ]
        read_only_fields = ['tenant', 'created_at', 'updated_at']

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_substitutes_skipped_by_name(self, obj):
        u = obj.substitutes_skipped_by
        return (u.get_full_name() or u.email) if u else None

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_history(self, obj):
        from .audit import serialize_events
        return serialize_events(obj.audit_events.order_by('created_at', 'id'))

    def get_employee_name(self, obj):
        return obj.employee.get_full_name() or obj.employee.email

    def get_manager_approver_name(self, obj):
        if obj.manager_approver:
            return obj.manager_approver.get_full_name() or obj.manager_approver.email
        return None

    def get_hr_approver_name(self, obj):
        if obj.hr_approver:
            return obj.hr_approver.get_full_name() or obj.hr_approver.email
        return None

    def get_final_approver_name(self, obj):
        if obj.final_approver:
            return obj.final_approver.get_full_name() or obj.final_approver.email
        return None

    def get_rejected_by_name(self, obj):
        if obj.rejected_by:
            return obj.rejected_by.get_full_name() or obj.rejected_by.email
        return None


class LeaveRequestCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating leave requests"""
    substitutes = SubstituteInputSerializer(
        many=True, required=False, write_only=True,
        help_text='Only for leave types that require substitutes, when your '
                  '"My substitutes" list is empty: who covers which task.'
    )

    class Meta:
        model = LeaveRequest
        fields = [
            'leave_type', 'start_date', 'end_date', 'reason', 'attachment', 'substitutes'
        ]

    def validate(self, attrs):
        """Comprehensive validation for leave request"""
        start_date = attrs['start_date']
        end_date = attrs['end_date']
        leave_type = attrs['leave_type']

        # Date range validation
        if end_date < start_date:
            raise serializers.ValidationError({
                'end_date': 'End date must be on or after start date'
            })

        # Get tenant and user from context
        request = self.context['request']
        tenant = request.tenant
        user = request.user

        if not leave_type.is_active or leave_type.tenant_id != getattr(tenant, 'pk', leave_type.tenant_id):
            raise serializers.ValidationError({'leave_type': 'This leave type is not available'})

        # Calculate working days
        total_days = calculate_working_days(start_date, end_date, tenant)

        if total_days == 0:
            raise serializers.ValidationError(
                'The selected date range contains no working days'
            )

        attrs['total_days'] = total_days

        limit_msg = validate_type_limits(user, leave_type, start_date, total_days, tenant)
        if limit_msg:
            raise serializers.ValidationError({'leave_type': limit_msg})

        # Check for overlapping leaves
        has_overlap, overlap_msg = check_overlapping_leaves(
            user, start_date, end_date, tenant
        )

        if has_overlap:
            raise serializers.ValidationError({'start_date': overlap_msg})

        # Check leave balance
        year = start_date.year
        has_balance, balance_msg = check_leave_balance(
            user, leave_type, total_days, year, tenant
        )

        if not has_balance:
            raise serializers.ValidationError({'leave_type': balance_msg})

        # Substitutes (feature switch + leave type flag)
        from .substitutes import resolve_for_request, check_availability
        substitutes = resolve_for_request(user, leave_type, tenant, attrs.pop('substitutes', None))
        check_availability(user, substitutes, start_date, end_date, tenant)
        attrs['_substitutes'] = substitutes

        return attrs

    def create(self, validated_data):
        """Create leave request and update pending balance"""
        from django.db import transaction
        from .substitutes import snapshot

        request = self.context['request']
        substitutes = validated_data.pop('_substitutes', [])
        validated_data.pop('substitutes', None)

        with transaction.atomic():
            leave_request = LeaveRequest.objects.create(
                tenant=request.tenant,
                employee=request.user,
                status='awaiting_substitutes' if substitutes else 'pending',
                **validated_data
            )

            # Update pending balance
            update_leave_balance(leave_request, action='pending')

            if substitutes:
                snapshot(leave_request, substitutes, actor=request.user, notify=False)
        if substitutes:
            from .substitutes import notify_requested
            notify_requested(leave_request, list(leave_request.substitutes.all()))

        return leave_request


class LeaveRequestPreviewSerializer(serializers.Serializer):
    """Input for the dry-run check the mobile form runs before submitting"""
    leave_type = serializers.PrimaryKeyRelatedField(queryset=LeaveType.objects.none())
    start_date = serializers.DateField()
    end_date = serializers.DateField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only this tenant's active leave types are selectable
        request = self.context.get('request')
        tenant = getattr(request, 'tenant', None)
        if tenant is not None:
            self.fields['leave_type'].queryset = LeaveType.objects.filter(
                tenant=tenant, is_active=True
            )


class LeaveRequestPreviewResponseSerializer(serializers.Serializer):
    """Outcome of the dry-run check (response shape only)"""
    total_days = serializers.DecimalField(max_digits=5, decimal_places=1)
    available_days = serializers.DecimalField(
        max_digits=5, decimal_places=1, allow_null=True,
        help_text='Days left on the balance for the start year, null when no balance applies'
    )
    allowed = serializers.BooleanField()
    message = serializers.CharField(
        allow_blank=True,
        help_text='Why the request would be refused; empty when allowed'
    )


class LeaveRequestUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating leave requests (limited fields)"""

    class Meta:
        model = LeaveRequest
        fields = ['start_date', 'end_date', 'reason', 'attachment']

    def validate(self, attrs):
        """Validate that only pending requests can be updated"""
        if self.instance.status not in ['pending', 'awaiting_substitutes']:
            raise serializers.ValidationError(
                'Only pending leave requests can be updated'
            )

        # Re-validate if dates changed
        if 'start_date' in attrs or 'end_date' in attrs:
            start_date = attrs.get('start_date', self.instance.start_date)
            end_date = attrs.get('end_date', self.instance.end_date)

            if end_date < start_date:
                raise serializers.ValidationError({
                    'end_date': 'End date must be on or after start date'
                })

            request = self.context['request']
            tenant = request.tenant

            # Recalculate working days
            total_days = calculate_working_days(start_date, end_date, tenant)
            attrs['total_days'] = total_days

            # Check for overlapping leaves (exclude current request)
            has_overlap, overlap_msg = check_overlapping_leaves(
                self.instance.employee, start_date, end_date, tenant,
                exclude_request_id=self.instance.id
            )

            if has_overlap:
                raise serializers.ValidationError({'start_date': overlap_msg})

            if total_days == 0:
                raise serializers.ValidationError(
                    'The selected date range contains no working days'
                )
            limit_msg = validate_type_limits(
                self.instance.employee, self.instance.leave_type, start_date,
                total_days, tenant, exclude_request_id=self.instance.id
            )
            if limit_msg:
                raise serializers.ValidationError({'leave_type': limit_msg})

            # New dates need the substitutes' consent again.
            rows = list(self.instance.substitutes.select_related('substitute'))
            if rows:
                from .substitutes import check_availability
                subs = [(r.substitute, r.task) for r in rows]
                check_availability(
                    self.instance.employee, subs, start_date, end_date, tenant,
                    exclude_request_id=self.instance.id,
                )
                attrs['_resnapshot'] = subs

        return attrs

    def update(self, instance, validated_data):
        """Update request and adjust pending balance if days or year changed"""
        from django.db import transaction

        resnapshot = validated_data.pop('_resnapshot', None)
        if resnapshot is not None and (
            validated_data.get('start_date', instance.start_date) == instance.start_date
            and validated_data.get('end_date', instance.end_date) == instance.end_date
        ):
            resnapshot = None
        old_days = instance.total_days
        old_year = instance.start_date.year

        with transaction.atomic():
            # Release the OLD pending amount before anything changes. (This
            # used to run after the new day count was set, so it removed the
            # new count and added it straight back: a net change of zero.)
            new_days = validated_data.get('total_days', old_days)
            new_start = validated_data.get('start_date', instance.start_date)
            moves = new_days != old_days or new_start.year != old_year
            if moves:
                update_leave_balance(instance, action='cancel')

            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()

            if moves:
                update_leave_balance(instance, action='pending')

            if resnapshot:
                from .substitutes import snapshot
                instance.status = 'awaiting_substitutes'
                instance._leave_actor = instance.employee
                instance.save()
                snapshot(instance, resnapshot, actor=instance.employee, notify=False)
        if resnapshot:
            from .substitutes import notify_requested
            notify_requested(instance, list(instance.substitutes.all()))

        return instance


class LeaveApprovalSerializer(serializers.Serializer):
    """Serializer for approving/rejecting leave requests"""
    action = serializers.ChoiceField(choices=['approve', 'reject'])
    comments = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if attrs['action'] == 'reject' and not attrs.get('comments'):
            raise serializers.ValidationError({
                'comments': 'Comments are required when rejecting a leave request'
            })
        return attrs


class LeaveCancellationSerializer(serializers.Serializer):
    """Serializer for cancelling leave requests"""
    reason = serializers.CharField(required=False, allow_blank=True)


class LeaveActionResponseSerializer(serializers.Serializer):
    """Body returned by the admin approve / reject / cancel / initialize actions"""
    success = serializers.BooleanField()
    message = serializers.CharField()
    status = serializers.ChoiceField(
        choices=LeaveRequest.STATUS_CHOICES, required=False,
        help_text='New request status (approve, reject and cancel only)'
    )


class LeaveErrorResponseSerializer(serializers.Serializer):
    """Error body of the leave actions"""
    error = serializers.CharField()


class LeaveActionErrorResponseSerializer(serializers.Serializer):
    """
    400 body of the approve / reject / cancel actions: either a state error
    {error} (request not open, wrong action) or DRF field errors
    ({action: [...]}, {comments: [...]}, ...).
    """
    error = serializers.CharField(required=False)
    action = serializers.ListField(child=serializers.CharField(), required=False)
    comments = serializers.ListField(child=serializers.CharField(), required=False)
    reason = serializers.ListField(child=serializers.CharField(), required=False)
    non_field_errors = serializers.ListField(child=serializers.CharField(), required=False)


class LeaveForbiddenResponseSerializer(serializers.Serializer):
    """
    403 body of the leave decision actions: {detail} from a permission check
    (not admin/HR, not the employee's manager, own request) or {error} when
    the request awaits a different approver role.
    """
    detail = serializers.CharField(required=False)
    error = serializers.CharField(required=False)


class CarryForwardRequestSerializer(serializers.Serializer):
    """Input of the carry forward action"""
    from_year = serializers.IntegerField(min_value=2000, max_value=2100)
    to_year = serializers.IntegerField(
        min_value=2000, max_value=2100,
        help_text='Must be from_year + 1'
    )
    force = serializers.BooleanField(
        required=False, default=False,
        help_text='Superusers only: recompute a carry forward that already ran '
                  '(overwrites the carried forward days of to_year)'
    )

    def validate(self, attrs):
        if attrs['to_year'] != attrs['from_year'] + 1:
            raise serializers.ValidationError({'to_year': 'to_year must be from_year + 1'})
        return attrs


class CarryForwardResponseSerializer(serializers.Serializer):
    """Outcome of the carry forward action"""
    success = serializers.BooleanField()
    message = serializers.CharField()
    already_run = serializers.BooleanField(
        help_text='True when this year was already carried forward and nothing changed'
    )
    carried_forward = serializers.IntegerField(
        help_text='Balances that received carried forward days in this call (0 when already_run)'
    )
    from_year = serializers.IntegerField()
    to_year = serializers.IntegerField()
    run_at = serializers.DateTimeField(help_text='When the carry forward for from_year (last) ran')
    total_days = serializers.DecimalField(max_digits=8, decimal_places=1)


# ============================================================================
# PUBLIC HOLIDAY SERIALIZERS
# ============================================================================

class PublicHolidayListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for listing public holidays"""
    name_display = serializers.SerializerMethodField()

    class Meta:
        model = PublicHoliday
        fields = ['id', 'name', 'name_display', 'date', 'is_recurring']

    def get_name_display(self, obj):
        language = self.context.get('language', 'en')
        return obj.get_name(language)


class PublicHolidayDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for public holidays"""
    name_display = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = PublicHoliday
        fields = [
            'id', 'tenant', 'name', 'name_display', 'date', 'is_recurring',
            'applies_to_all', 'created_by', 'created_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['tenant', 'created_by', 'updated_by', 'created_at', 'updated_at']

    def get_name_display(self, obj):
        language = self.context.get('language', 'en')
        return obj.get_name(language)

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name() or obj.created_by.email
        return None


class PublicHolidayCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating public holidays"""

    class Meta:
        model = PublicHoliday
        fields = ['name', 'date', 'is_recurring', 'applies_to_all']


# ============================================================================
# LEAVE APPROVAL CHAIN SERIALIZERS
# ============================================================================

class LeaveApprovalChainSerializer(serializers.ModelSerializer):
    """Serializer for leave approval chains"""
    leave_type_name = serializers.SerializerMethodField()

    class Meta:
        model = LeaveApprovalChain
        fields = [
            'id', 'tenant', 'leave_type', 'leave_type_name', 'level',
            'approver_role', 'is_required', 'created_at', 'updated_at'
        ]
        read_only_fields = ['tenant', 'created_at', 'updated_at']

    def get_leave_type_name(self, obj):
        if obj.leave_type:
            language = self.context.get('language', 'en')
            return obj.leave_type.get_name(language)
        return "All Leave Types"

    def validate(self, attrs):
        """Ensure unique level per leave type and tenant"""
        tenant = self.context['request'].tenant
        # On PATCH the omitted fields keep their stored values (this used to
        # raise KeyError on a missing 'level' and treat a missing leave_type
        # as "all leave types").
        if 'leave_type' in attrs:
            leave_type = attrs['leave_type']
        else:
            leave_type = self.instance.leave_type if self.instance else None
        if 'level' in attrs:
            level = attrs['level']
        elif self.instance is not None:
            level = self.instance.level
        else:
            raise serializers.ValidationError({'level': 'This field is required.'})

        queryset = LeaveApprovalChain.objects.filter(
            tenant=tenant,
            leave_type=leave_type,
            level=level
        )

        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)

        if queryset.exists():
            raise serializers.ValidationError({
                'level': 'An approval chain already exists for this level and leave type'
            })

        return attrs


class LeaveAuditEventSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()
    subject_user_name = serializers.SerializerMethodField()

    class Meta:
        model = LeaveAuditEvent
        fields = [
            'id', 'kind', 'action', 'leave_request', 'subject_user', 'subject_user_name',
            'actor', 'actor_name', 'object_id', 'from_status', 'to_status', 'data', 'created_at',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_actor_name(self, obj):
        return (obj.actor.get_full_name() or obj.actor.email) if obj.actor else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_subject_user_name(self, obj):
        u = obj.subject_user
        return (u.get_full_name() or u.email) if u else None


# ============================================================================
# SUBSTITUTES
# ============================================================================

class SubstituteAssignmentSerializer(serializers.ModelSerializer):
    """One row of "My substitutes"."""
    substitute_name = serializers.SerializerMethodField()
    substitute_email = serializers.EmailField(source='substitute.email', read_only=True)

    class Meta:
        model = SubstituteAssignment
        fields = ['id', 'substitute', 'substitute_name', 'substitute_email', 'task', 'sort_order']
        read_only_fields = ['id', 'substitute_name', 'substitute_email']

    @extend_schema_field(serializers.CharField())
    def get_substitute_name(self, obj):
        return obj.substitute.get_full_name() or obj.substitute.email

    def validate(self, attrs):
        from .substitutes import clean_rows
        owner = self.context['request'].user
        sub = attrs.get('substitute', getattr(self.instance, 'substitute', None))
        task = attrs.get('task', getattr(self.instance, 'task', ''))
        (user, cleaned_task), = clean_rows(owner, [{'substitute': sub, 'task': task}])
        others = SubstituteAssignment.objects.filter(owner=owner, substitute=user)
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)
        if others.exists():
            raise serializers.ValidationError({'substitute': 'Already in your list.'})
        if self.instance is None and SubstituteAssignment.objects.filter(owner=owner).count() >= SubstituteAssignment.MAX_PER_OWNER:
            raise serializers.ValidationError(
                {'substitute': f'At most {SubstituteAssignment.MAX_PER_OWNER} substitutes.'}
            )
        attrs['substitute'] = user
        attrs['task'] = cleaned_task
        return attrs


class SubstituteReplaceSerializer(serializers.Serializer):
    items = SubstituteInputSerializer(many=True)


class CoveringRequestSerializer(serializers.ModelSerializer):
    """A request the caller is a substitute on."""
    employee_name = serializers.SerializerMethodField()
    leave_type_name = serializers.SerializerMethodField()
    leave_type_color = serializers.CharField(source='leave_type.color', read_only=True)
    my_task = serializers.SerializerMethodField()
    my_status = serializers.SerializerMethodField()
    my_row_id = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        fields = [
            'id', 'employee', 'employee_name', 'leave_type', 'leave_type_name',
            'leave_type_color', 'start_date', 'end_date', 'total_days', 'status',
            'my_row_id', 'my_task', 'my_status', 'created_at',
        ]

    def _row(self, obj):
        user = self.context['request'].user
        for row in obj.substitutes.all():
            if row.substitute_id == user.pk:
                return row
        return None

    @extend_schema_field(serializers.CharField())
    def get_employee_name(self, obj):
        return obj.employee.get_full_name() or obj.employee.email

    @extend_schema_field(serializers.CharField())
    def get_leave_type_name(self, obj):
        return obj.leave_type.get_name(self.context.get('language', 'en'))

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_my_task(self, obj):
        row = self._row(obj)
        return row.task if row else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_my_status(self, obj):
        row = self._row(obj)
        return row.status if row else None

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_my_row_id(self, obj):
        row = self._row(obj)
        return row.pk if row else None


class SubstituteRespondSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=['accept', 'reject'])
    comment = serializers.CharField(required=False, allow_blank=True, default='')


class SkipSubstitutesSerializer(serializers.Serializer):
    reason = serializers.CharField()
