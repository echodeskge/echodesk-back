from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.utils import timezone
from decimal import Decimal
from .models import (
    LeaveSettings, LeaveType, LeaveBalance, LeaveRequest,
    PublicHoliday, LeaveApprovalChain
)
from .utils import (
    calculate_working_days, check_leave_balance,
    check_overlapping_leaves, update_leave_balance
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
            'weekend_days', 'created_at', 'updated_at'
        ]
        read_only_fields = ['tenant', 'created_at', 'updated_at']


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
            'requires_approval', 'color', 'is_active', 'sort_order'
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
            'is_active', 'sort_order', 'created_by', 'created_by_name',
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
            'is_active', 'sort_order'
        ]

    def validate_code(self, value):
        """Ensure code is unique within tenant"""
        tenant = self.context['request'].tenant
        queryset = LeaveType.objects.filter(tenant=tenant, code=value)

        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)

        if queryset.exists():
            raise serializers.ValidationError(f"Leave type with code '{value}' already exists")

        return value.upper()

    def validate(self, attrs):
        """Validate calculation method fields"""
        calculation_method = attrs.get('calculation_method', self.instance.calculation_method if self.instance else 'annual')

        if calculation_method == 'annual':
            if attrs.get('default_days_per_year', 0) <= 0:
                raise serializers.ValidationError({
                    'default_days_per_year': 'Must be greater than 0 for annual calculation'
                })
        elif calculation_method == 'accrual':
            if attrs.get('accrual_rate_per_month', 0) <= 0:
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

    class Meta:
        model = LeaveRequest
        fields = [
            'id', 'tenant', 'employee', 'employee_name', 'employee_email',
            'leave_type', 'leave_type_details', 'start_date', 'end_date',
            'total_days', 'reason', 'status', 'status_display',
            'manager_approver', 'manager_approver_name', 'manager_approved_at',
            'manager_comments', 'hr_approver', 'hr_approver_name', 'hr_approved_at',
            'hr_comments', 'final_approver', 'final_approver_name', 'final_approved_at',
            'rejected_by', 'rejected_by_name', 'rejected_at', 'rejection_reason',
            'cancelled_at', 'cancellation_reason', 'attachment', 'created_at', 'updated_at'
        ]
        read_only_fields = ['tenant', 'created_at', 'updated_at']

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

    class Meta:
        model = LeaveRequest
        fields = [
            'leave_type', 'start_date', 'end_date', 'reason', 'attachment'
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

        # Calculate working days
        total_days = calculate_working_days(start_date, end_date, tenant)

        if total_days == 0:
            raise serializers.ValidationError(
                'The selected date range contains no working days'
            )

        attrs['total_days'] = total_days

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

        return attrs

    def create(self, validated_data):
        """Create leave request and update pending balance"""
        request = self.context['request']

        leave_request = LeaveRequest.objects.create(
            tenant=request.tenant,
            employee=request.user,
            **validated_data
        )

        # Update pending balance
        update_leave_balance(leave_request, action='pending')

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
        if self.instance.status not in ['pending']:
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

        return attrs

    def update(self, instance, validated_data):
        """Update request and adjust pending balance if days changed"""
        old_days = instance.total_days

        # Update instance
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # Adjust pending balance if total days changed
        if 'total_days' in validated_data:
            new_days = validated_data['total_days']
            if new_days != old_days:
                # Remove old pending amount
                update_leave_balance(instance, action='cancel')
                # Set instance total_days to new value
                instance.total_days = new_days
                # Add new pending amount
                update_leave_balance(instance, action='pending')

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
        leave_type = attrs.get('leave_type')
        level = attrs['level']

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
