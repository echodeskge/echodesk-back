from rest_framework import viewsets, filters, status, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.db.models import Q
from drf_spectacular.utils import extend_schema, OpenApiParameter, inline_serializer

from .models import (
    LeaveSettings, LeaveType, LeaveBalance, LeaveRequest,
    PublicHoliday, LeaveApprovalChain
)
from .serializers import (
    LeaveSettingsSerializer,
    LeaveTypeListSerializer, LeaveTypeDetailSerializer, LeaveTypeCreateUpdateSerializer,
    LeaveBalanceListSerializer, LeaveBalanceDetailSerializer, LeaveBalanceUpdateSerializer,
    LeaveRequestListSerializer, LeaveRequestDetailSerializer,
    LeaveRequestCreateSerializer, LeaveRequestUpdateSerializer,
    LeaveApprovalSerializer, LeaveCancellationSerializer,
    PublicHolidayListSerializer, PublicHolidayDetailSerializer, PublicHolidayCreateUpdateSerializer,
    LeaveApprovalChainSerializer
)
from .permissions import (
    HasLeaveManagementFeature, CanManageLeaveSettings, CanManageLeaveTypes,
    CanManageLeaveBalances, CanManagePublicHolidays, CanApproveLeave,
    CanCancelLeave
)
from .utils import (
    update_leave_balance, carry_forward_balances, initialize_leave_balances_for_user
)


# ============================================================================
# ADMIN VIEWSETS
# ============================================================================

class LeaveSettingsViewSet(viewsets.ModelViewSet):
    """
    Admin ViewSet for managing leave settings
    """
    serializer_class = LeaveSettingsSerializer
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, CanManageLeaveSettings]
    http_method_names = ['get', 'post', 'patch', 'put']

    def get_queryset(self):
        return LeaveSettings.objects.filter(tenant=self.request.tenant)

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.tenant)

    @extend_schema(tags=['Leave Management - Admin'], summary='Get leave settings')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Create leave settings')
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Update leave settings')
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Partial update leave settings')
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)


class LeaveTypeViewSet(viewsets.ModelViewSet):
    """
    Admin ViewSet for managing leave types
    """
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, CanManageLeaveTypes]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'calculation_method', 'requires_approval']
    search_fields = ['code', 'name']
    ordering_fields = ['sort_order', 'code', 'created_at']
    ordering = ['sort_order', 'id']

    def get_queryset(self):
        return LeaveType.objects.filter(tenant=self.request.tenant).select_related('created_by', 'updated_by')

    def get_serializer_class(self):
        if self.action == 'list':
            return LeaveTypeListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return LeaveTypeCreateUpdateSerializer
        return LeaveTypeDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    def perform_create(self, serializer):
        serializer.save(
            tenant=self.request.tenant,
            created_by=self.request.user,
            updated_by=self.request.user
        )

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    @extend_schema(tags=['Leave Management - Admin'], summary='List leave types')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Get leave type details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Create leave type')
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Update leave type')
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Delete leave type')
    def destroy(self, request, *args, **kwargs):
        from django.db.models import ProtectedError
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            # Requests already use this type; it can be deactivated instead.
            return Response(
                {'error': 'This leave type is used by existing leave requests and cannot be deleted. Deactivate it instead.'},
                status=status.HTTP_400_BAD_REQUEST,
            )


class AdminLeaveBalanceViewSet(viewsets.ModelViewSet):
    """
    Admin ViewSet for managing all leave balances
    """
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, CanManageLeaveBalances]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['user', 'leave_type', 'year']
    search_fields = ['user__email', 'user__first_name', 'user__last_name']
    ordering_fields = ['year', 'allocated_days', 'used_days', 'available_days']
    ordering = ['-year', 'user__last_name']
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        return LeaveBalance.objects.filter(tenant=self.request.tenant).select_related(
            'user', 'leave_type'
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return LeaveBalanceListSerializer
        elif self.action in ['partial_update', 'update']:
            return LeaveBalanceUpdateSerializer
        return LeaveBalanceDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.tenant)

    @extend_schema(
        tags=['Leave Management - Admin'],
        summary='Initialize balances for user',
        request=inline_serializer(
            name='InitializeUserRequest',
            fields={
                'user_id': serializers.IntegerField(required=True, help_text='User ID'),
                'year': serializers.IntegerField(required=False, help_text='Year (optional, defaults to current year)'),
            }
        ),
        responses={200: LeaveBalanceDetailSerializer}
    )
    @action(detail=False, methods=['post'])
    def initialize_user(self, request):
        """Initialize leave balances for a specific user"""
        user_id = request.data.get('user_id')
        year = request.data.get('year')

        if not user_id:
            return Response(
                {'error': 'user_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        from django.contrib.auth import get_user_model
        User = get_user_model()

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response(
                {'error': 'User not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        count = initialize_leave_balances_for_user(user, request.tenant, year)

        return Response({
            'success': True,
            'message': f'Initialized {count} leave balance(s) for {user.get_full_name()}'
        })

    @extend_schema(
        tags=['Leave Management - Admin'],
        summary='Carry forward balances',
        request=inline_serializer(
            name='CarryForwardRequest',
            fields={
                'from_year': serializers.IntegerField(required=True, help_text='Year to carry forward from'),
                'to_year': serializers.IntegerField(required=True, help_text='Year to carry forward to'),
            }
        ),
        responses={200: inline_serializer(
            name='CarryForwardResponse',
            fields={
                'success': serializers.BooleanField(),
                'message': serializers.CharField(),
            }
        )}
    )
    @action(detail=False, methods=['post'])
    def carry_forward(self, request):
        """Carry forward unused balances from one year to next"""
        from_year = request.data.get('from_year')
        to_year = request.data.get('to_year')

        if not from_year or not to_year:
            return Response(
                {'error': 'from_year and to_year are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        count = carry_forward_balances(request.tenant, from_year, to_year)

        return Response({
            'success': True,
            'message': f'Carried forward {count} balance(s) from {from_year} to {to_year}'
        })

    @extend_schema(tags=['Leave Management - Admin'], summary='List all leave balances')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Get balance details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Update leave balance')
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)


class AdminLeaveRequestViewSet(viewsets.ModelViewSet):
    """
    Admin ViewSet for managing all leave requests
    """
    # Everyone's leave requests: administrators / HR only.
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, CanManageLeaveBalances]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['employee', 'leave_type', 'status']
    search_fields = ['employee__email', 'employee__first_name', 'employee__last_name', 'reason']
    ordering_fields = ['start_date', 'end_date', 'total_days', 'created_at', 'status']
    ordering = ['-created_at']

    def get_queryset(self):
        queryset = LeaveRequest.objects.filter(tenant=self.request.tenant).select_related(
            'employee', 'leave_type', 'manager_approver', 'hr_approver', 'final_approver', 'rejected_by'
        )

        # Filter by date range
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')

        if start_date:
            queryset = queryset.filter(start_date__gte=start_date)
        if end_date:
            queryset = queryset.filter(end_date__lte=end_date)

        return queryset

    def get_serializer_class(self):
        if self.action == 'list':
            return LeaveRequestListSerializer
        elif self.action == 'create':
            return LeaveRequestCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return LeaveRequestUpdateSerializer
        elif self.action in ['approve', 'reject']:
            return LeaveApprovalSerializer
        elif self.action == 'cancel':
            return LeaveCancellationSerializer
        return LeaveRequestDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    def perform_create(self, serializer):
        # The serializer sets tenant (and employee) itself; passing tenant
        # again raised "got multiple values for keyword argument 'tenant'".
        serializer.save()

    @extend_schema(
        tags=['Leave Management - Admin'],
        summary='Approve leave request',
        request=LeaveApprovalSerializer
    )
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasLeaveManagementFeature, CanApproveLeave])
    def approve(self, request, pk=None):
        """Approve a leave request"""
        from .utils import get_next_approver_role
        from django.db import transaction

        leave_request = self.get_object()

        if leave_request.status not in ['pending', 'manager_approved', 'hr_approved']:
            return Response(
                {'error': 'Only pending or partially approved requests can be approved'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = LeaveApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comments = serializer.validated_data.get('comments', '')

        # Determine next required role BEFORE changing status
        next_role = get_next_approver_role(leave_request)

        with transaction.atomic():
            # Apply approval based on current status
            if leave_request.status == 'pending':
                leave_request.manager_approver = request.user
                leave_request.manager_approved_at = timezone.now()
                leave_request.manager_comments = comments
                leave_request.status = 'manager_approved'
            elif leave_request.status == 'manager_approved':
                leave_request.hr_approver = request.user
                leave_request.hr_approved_at = timezone.now()
                leave_request.hr_comments = comments
                leave_request.status = 'hr_approved'
            elif leave_request.status == 'hr_approved':
                leave_request.final_approver = request.user
                leave_request.final_approved_at = timezone.now()
                leave_request.status = 'approved'

            # Check if there are more approval steps needed
            next_role_after = get_next_approver_role(leave_request)

            if not next_role_after:
                # Final approval — set status to approved and update balance
                leave_request.status = 'approved'
                if not leave_request.final_approver:
                    leave_request.final_approver = request.user
                    leave_request.final_approved_at = timezone.now()
                update_leave_balance(leave_request, action='approve')

            leave_request.save()

        return Response({
            'success': True,
            'message': 'Leave request approved successfully',
            'status': leave_request.status
        })

    @extend_schema(
        tags=['Leave Management - Admin'],
        summary='Reject leave request',
        request=LeaveApprovalSerializer
    )
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasLeaveManagementFeature, CanApproveLeave])
    def reject(self, request, pk=None):
        """Reject a leave request"""
        leave_request = self.get_object()

        if leave_request.status not in ['pending', 'manager_approved', 'hr_approved']:
            return Response(
                {'error': 'Only pending or partially approved requests can be rejected'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = LeaveApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        from django.db import transaction
        with transaction.atomic():
            leave_request.status = 'rejected'
            leave_request.rejected_by = request.user
            leave_request.rejected_at = timezone.now()
            leave_request.rejection_reason = serializer.validated_data.get('comments', '')
            leave_request.save()

            # Remove from pending balance
            update_leave_balance(leave_request, action='reject')

        return Response({
            'success': True,
            'message': 'Leave request rejected'
        })

    @extend_schema(
        tags=['Leave Management - Admin'],
        summary='Cancel leave request',
        request=LeaveCancellationSerializer
    )
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasLeaveManagementFeature, CanCancelLeave])
    def cancel(self, request, pk=None):
        """Cancel a leave request"""
        leave_request = self.get_object()

        if leave_request.status == 'cancelled':
            return Response(
                {'error': 'Leave request is already cancelled'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = LeaveCancellationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        was_approved = leave_request.status == 'approved'

        from django.db import transaction
        with transaction.atomic():
            leave_request.status = 'cancelled'
            leave_request.cancelled_at = timezone.now()
            leave_request.cancellation_reason = serializer.validated_data.get('reason', '')
            leave_request.save()

            # Update balance based on previous status
            if was_approved:
                # Return used days back
                update_leave_balance(leave_request, action='cancel_approved')
            else:
                # Remove from pending
                update_leave_balance(leave_request, action='cancel')

        return Response({
            'success': True,
            'message': 'Leave request cancelled successfully'
        })

    @extend_schema(tags=['Leave Management - Admin'], summary='List all leave requests')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Get leave request details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Create leave request')
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Delete leave request')
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        # Update balance before deleting
        if instance.status == 'approved':
            update_leave_balance(instance, action='cancel')
        elif instance.status in ['pending', 'manager_approved', 'hr_approved']:
            update_leave_balance(instance, action='cancel')

        return super().destroy(request, *args, **kwargs)


class PublicHolidayViewSet(viewsets.ModelViewSet):
    """
    Admin ViewSet for managing public holidays
    """
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, CanManagePublicHolidays]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['is_recurring', 'applies_to_all']
    ordering_fields = ['date']
    ordering = ['date']

    def get_queryset(self):
        queryset = PublicHoliday.objects.filter(tenant=self.request.tenant).select_related('created_by')

        # Filter by year
        year = self.request.query_params.get('year')
        if year:
            queryset = queryset.filter(date__year=year)

        return queryset

    def get_serializer_class(self):
        if self.action == 'list':
            return PublicHolidayListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return PublicHolidayCreateUpdateSerializer
        return PublicHolidayDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    def perform_create(self, serializer):
        serializer.save(
            tenant=self.request.tenant,
            created_by=self.request.user,
            updated_by=self.request.user
        )

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    @extend_schema(tags=['Leave Management - Admin'], summary='List public holidays')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Get holiday details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Create public holiday')
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Update public holiday')
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Delete public holiday')
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


class LeaveApprovalChainViewSet(viewsets.ModelViewSet):
    """
    Admin ViewSet for managing leave approval chains
    """
    serializer_class = LeaveApprovalChainSerializer
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, CanManageLeaveSettings]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['leave_type', 'approver_role', 'is_required']
    ordering_fields = ['level']
    ordering = ['level']

    def get_queryset(self):
        return LeaveApprovalChain.objects.filter(tenant=self.request.tenant).select_related('leave_type')

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.tenant)

    @extend_schema(tags=['Leave Management - Admin'], summary='List approval chains')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Get approval chain details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Create approval chain')
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Update approval chain')
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Delete approval chain')
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)
