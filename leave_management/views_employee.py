from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from datetime import date
from drf_spectacular.utils import extend_schema

from .models import LeaveRequest, LeaveBalance, PublicHoliday, LeaveType
from .serializers import (
    LeaveRequestListSerializer, LeaveRequestDetailSerializer,
    LeaveRequestCreateSerializer, LeaveRequestUpdateSerializer,
    LeaveCancellationSerializer,
    LeaveBalanceListSerializer, LeaveBalanceDetailSerializer,
    PublicHolidayListSerializer,
    LeaveTypeListSerializer
)
from .permissions import (
    HasLeaveManagementFeature, IsLeaveEmployee, CanCancelLeave
)
from .utils import update_leave_balance


# ============================================================================
# EMPLOYEE VIEWSETS
# ============================================================================

class EmployeeLeaveRequestViewSet(viewsets.ModelViewSet):
    """
    Employee ViewSet for managing own leave requests
    Employees can:
    - View their own leave requests
    - Submit new leave requests
    - Update pending requests
    - Cancel requests
    """
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['leave_type', 'status']
    ordering_fields = ['start_date', 'end_date', 'created_at', 'status']
    ordering = ['-created_at']
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        """Get only the employee's own leave requests"""
        return LeaveRequest.objects.filter(
            tenant=self.request.tenant,
            employee=self.request.user
        ).select_related(
            'leave_type', 'manager_approver', 'hr_approver', 'final_approver', 'rejected_by'
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return LeaveRequestListSerializer
        elif self.action == 'create':
            return LeaveRequestCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return LeaveRequestUpdateSerializer
        elif self.action == 'cancel':
            return LeaveCancellationSerializer
        return LeaveRequestDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    def perform_create(self, serializer):
        """Create leave request for current user"""
        serializer.save()

    def perform_update(self, serializer):
        """Update leave request - only pending requests"""
        if self.get_object().status != 'pending':
            from rest_framework.exceptions import ValidationError
            raise ValidationError('Only pending leave requests can be updated')
        serializer.save()

    def perform_destroy(self, instance):
        """Delete leave request - only pending requests"""
        if instance.status != 'pending':
            from rest_framework.exceptions import ValidationError
            raise ValidationError('Only pending leave requests can be deleted')

        # Remove from pending balance
        update_leave_balance(instance, action='cancel')
        instance.delete()

    @extend_schema(
        tags=['Leave Management - Employee'],
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

        if leave_request.status not in ['pending', 'manager_approved', 'hr_approved', 'approved']:
            return Response(
                {'error': 'Cannot cancel a rejected leave request'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = LeaveCancellationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        was_approved = leave_request.status == 'approved'

        leave_request.status = 'cancelled'
        leave_request.cancelled_at = timezone.now()
        leave_request.cancellation_reason = serializer.validated_data.get('reason', '')
        leave_request.save()

        # Update balance
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

    @extend_schema(
        tags=['Leave Management - Employee'],
        summary='Get pending leave requests'
    )
    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Get all pending leave requests"""
        queryset = self.get_queryset().filter(
            status__in=['pending', 'manager_approved', 'hr_approved']
        )
        page = self.paginate_queryset(queryset)

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(
        tags=['Leave Management - Employee'],
        summary='Get approved leave requests'
    )
    @action(detail=False, methods=['get'])
    def approved(self, request):
        """Get all approved leave requests"""
        queryset = self.get_queryset().filter(status='approved')
        page = self.paginate_queryset(queryset)

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(
        tags=['Leave Management - Employee'],
        summary='Get leave history'
    )
    @action(detail=False, methods=['get'])
    def history(self, request):
        """Get leave request history (approved, rejected, cancelled)"""
        queryset = self.get_queryset().filter(
            status__in=['approved', 'rejected', 'cancelled']
        ).order_by('-start_date')

        page = self.paginate_queryset(queryset)

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(tags=['Leave Management - Employee'], summary='List my leave requests')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Employee'], summary='Get my leave request details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Employee'], summary='Submit new leave request')
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Employee'], summary='Update pending leave request')
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Employee'], summary='Delete pending leave request')
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


class EmployeeLeaveBalanceViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Employee ViewSet for viewing own leave balances
    """
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['leave_type', 'year']
    ordering_fields = ['year', 'leave_type__sort_order']
    ordering = ['-year', 'leave_type__sort_order']

    def get_queryset(self):
        """Get only the employee's own leave balances"""
        # Make sure this year's balances exist, so a new employee (or the
        # first visit in a new year) sees their allowance instead of nothing.
        try:
            from .utils import initialize_leave_balances_for_user
            initialize_leave_balances_for_user(self.request.user, self.request.tenant)
        except Exception:
            pass
        return LeaveBalance.objects.filter(
            tenant=self.request.tenant,
            user=self.request.user
        ).select_related('leave_type')

    def get_serializer_class(self):
        if self.action == 'list':
            return LeaveBalanceListSerializer
        return LeaveBalanceDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    @extend_schema(
        tags=['Leave Management - Employee'],
        summary='Get current year balance summary'
    )
    @action(detail=False, methods=['get'])
    def current(self, request):
        """Get leave balance for current year"""
        current_year = date.today().year
        queryset = self.get_queryset().filter(year=current_year)

        serializer = self.get_serializer(queryset, many=True)

        return Response({
            'year': current_year,
            'balances': serializer.data
        })

    @extend_schema(
        tags=['Leave Management - Employee'],
        summary='Get balance summary by leave type'
    )
    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Get aggregated balance summary"""
        from django.db.models import Sum

        current_year = date.today().year
        queryset = self.get_queryset().filter(year=current_year)

        summary = queryset.aggregate(
            total_allocated=Sum('allocated_days'),
            total_used=Sum('used_days'),
            total_pending=Sum('pending_days'),
            total_carried_forward=Sum('carried_forward_days')
        )

        # Calculate total available
        total_allocated = summary['total_allocated'] or 0
        total_carried_forward = summary['total_carried_forward'] or 0
        total_used = summary['total_used'] or 0
        total_pending = summary['total_pending'] or 0

        summary['total_available'] = total_allocated + total_carried_forward - total_used - total_pending

        return Response(summary)

    @extend_schema(tags=['Leave Management - Employee'], summary='List my leave balances')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Employee'], summary='Get my balance details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)


class EmployeePublicHolidayViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Employee ViewSet for viewing public holidays
    """
    serializer_class = PublicHolidayListSerializer
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['is_recurring']
    ordering = ['date']

    def get_queryset(self):
        """Get public holidays for current tenant"""
        queryset = PublicHoliday.objects.filter(tenant=self.request.tenant)

        # Filter by year
        year = self.request.query_params.get('year', date.today().year)
        if year:
            queryset = queryset.filter(date__year=year)

        return queryset

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    @extend_schema(
        tags=['Leave Management - Employee'],
        summary='Get upcoming holidays'
    )
    @action(detail=False, methods=['get'])
    def upcoming(self, request):
        """Get upcoming public holidays"""
        today = date.today()
        queryset = self.get_queryset().filter(date__gte=today).order_by('date')[:10]

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(tags=['Leave Management - Employee'], summary='List public holidays')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Employee'], summary='Get holiday details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)


class EmployeeLeaveTypeViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Employee ViewSet for viewing available leave types
    """
    serializer_class = LeaveTypeListSerializer
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['is_paid', 'requires_approval']
    ordering = ['sort_order', 'id']

    def get_queryset(self):
        """Get active leave types"""
        return LeaveType.objects.filter(
            tenant=self.request.tenant,
            is_active=True
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    @extend_schema(tags=['Leave Management - Employee'], summary='List available leave types')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Employee'], summary='Get leave type details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)
