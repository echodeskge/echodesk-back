from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiParameter

from .models import LeaveRequest, LeaveBalance
from .serializers import (
    LeaveRequestListSerializer, LeaveRequestDetailSerializer,
    LeaveApprovalSerializer,
    LeaveBalanceListSerializer, LeaveBalanceDetailSerializer,
    LeaveActionResponseSerializer,
)
from .permissions import (
    HasLeaveManagementFeature, IsLeaveManager, CanApproveLeave
)
from .utils import (
    update_leave_balance, get_next_approver_role, apply_approval_step,
    team_q, filter_by_status_in, OPEN_APPROVAL_STATUSES,
)
from .views import ACTION_ERROR_RESPONSE, FORBIDDEN_RESPONSE, lock_leave_request


STATUS_IN_PARAM = OpenApiParameter(
    name='status__in', type=str, location=OpenApiParameter.QUERY,
    description='Comma-separated list of statuses to include, e.g. '
                '`pending,manager_approved`. Use `status` for a single value.'
)
START_DATE_PARAM = OpenApiParameter(
    name='start_date', type=str, location=OpenApiParameter.QUERY,
    description='Window start (YYYY-MM-DD). Returns requests that overlap the '
                'window, i.e. whose end_date is on or after this day.'
)
END_DATE_PARAM = OpenApiParameter(
    name='end_date', type=str, location=OpenApiParameter.QUERY,
    description='Window end (YYYY-MM-DD). Returns requests that overlap the '
                'window, i.e. whose start_date is on or before this day.'
)


# ============================================================================
# MANAGER VIEWSETS
# ============================================================================

class ManagerLeaveRequestViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Manager ViewSet for managing team leave requests
    Managers can:
    - View their team's leave requests
    - Approve/reject pending requests
    """
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, IsLeaveManager]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['leave_type', 'status']
    search_fields = ['employee__email', 'employee__first_name', 'employee__last_name', 'reason']
    ordering_fields = ['start_date', 'end_date', 'created_at', 'status']
    ordering = ['-created_at']

    def get_queryset(self):
        """Get leave requests for team members"""
        user = self.request.user

        # Staff see everyone; others see their direct reports / department
        queryset = LeaveRequest.objects.filter(
            tenant=self.request.tenant
        ).filter(team_q(user))

        queryset = queryset.select_related(
            'employee', 'leave_type', 'manager_approver', 'hr_approver', 'rejected_by'
        )

        # Filter by date window: a request is included when it overlaps the
        # window, so a leave straddling the window's edge still shows up
        # (the team calendar asks month by month).
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')

        if start_date:
            queryset = queryset.filter(end_date__gte=start_date)
        if end_date:
            queryset = queryset.filter(start_date__lte=end_date)

        # Multi-status filter (?status__in=pending,manager_approved)
        queryset = filter_by_status_in(
            queryset, self.request.query_params.get('status__in')
        )

        # By default, show pending requests first
        if not self.request.query_params.get('ordering'):
            queryset = queryset.order_by('status', '-created_at')

        return queryset

    def get_serializer_class(self):
        if self.action == 'list':
            return LeaveRequestListSerializer
        elif self.action in ['approve', 'reject']:
            return LeaveApprovalSerializer
        return LeaveRequestDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    @extend_schema(
        tags=['Leave Management - Manager'],
        summary='Approve team member leave request',
        description='Signs off the manager step of the approval chain. When '
                    'no later step is left the request becomes approved. 403 '
                    '{error} when the request awaits a different approver role.',
        request=LeaveApprovalSerializer,
        responses={200: LeaveActionResponseSerializer, 400: ACTION_ERROR_RESPONSE, 403: FORBIDDEN_RESPONSE},
    )
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasLeaveManagementFeature, IsLeaveManager, CanApproveLeave])
    def approve(self, request, pk=None):
        """Approve a team member's leave request"""
        leave_request = self.get_object()

        # Check if manager can approve
        next_role = get_next_approver_role(leave_request)
        if next_role != 'manager':
            return Response(
                {'error': 'This leave request requires a different approver role'},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = LeaveApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        comments = serializer.validated_data.get('comments', '')

        # Records the manager step; when no HR/admin step follows it also
        # finalises the request and moves the days from pending to used.
        with transaction.atomic():
            # Re-check on the locked row: a concurrent decision may have
            # moved the request on while we waited.
            leave_request = lock_leave_request(leave_request)
            if get_next_approver_role(leave_request) != 'manager':
                return Response(
                    {'error': 'This leave request requires a different approver role'},
                    status=status.HTTP_403_FORBIDDEN
                )
            apply_approval_step(leave_request, request.user, comments)
            leave_request._leave_actor = request.user
            leave_request.save()

        return Response({
            'success': True,
            'message': 'Leave request approved successfully',
            'status': leave_request.status
        })

    @extend_schema(
        tags=['Leave Management - Manager'],
        summary='Reject team member leave request',
        description='Rejects a request that is pending or awaits the manager '
                    'step; `comments` (the reason) are required.',
        request=LeaveApprovalSerializer,
        responses={200: LeaveActionResponseSerializer, 400: ACTION_ERROR_RESPONSE, 403: FORBIDDEN_RESPONSE},
    )
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasLeaveManagementFeature, IsLeaveManager, CanApproveLeave])
    def reject(self, request, pk=None):
        """Reject a team member's leave request"""
        leave_request = self.get_object()

        if not self._manager_can_reject(leave_request):
            return Response(
                {'error': 'Managers can only reject a request that is pending or awaiting the manager step'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = LeaveApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            leave_request = lock_leave_request(leave_request)
            if not self._manager_can_reject(leave_request):
                return Response(
                    {'error': 'Managers can only reject a request that is pending or awaiting the manager step'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            leave_request.status = 'rejected'
            leave_request.rejected_by = request.user
            leave_request.rejected_at = timezone.now()
            leave_request.rejection_reason = serializer.validated_data.get('comments', '')
            leave_request._leave_actor = request.user
            leave_request.save()

            # Remove from pending balance
            update_leave_balance(leave_request, action='reject')

        return Response({
            'success': True,
            'message': 'Leave request rejected',
            'status': leave_request.status,
        })

    @staticmethod
    def _manager_can_reject(leave_request):
        """
        A manager may reject a pending request, or one whose next open step
        is the manager's (e.g. a chain stored hr@1, manager@2 after HR
        signed off).
        """
        if leave_request.status == 'pending':
            return True
        return (
            leave_request.status in OPEN_APPROVAL_STATUSES
            and get_next_approver_role(leave_request) == 'manager'
        )

    @extend_schema(
        tags=['Leave Management - Manager'],
        summary='Get pending team leave requests'
    )
    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Get all pending leave requests for team"""
        queryset = self.get_queryset().filter(status='pending')
        page = self.paginate_queryset(queryset)

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(
        tags=['Leave Management - Manager'],
        summary='List team leave requests',
        parameters=[STATUS_IN_PARAM, START_DATE_PARAM, END_DATE_PARAM],
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Manager'], summary='Get team member leave request details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)


class ManagerTeamBalanceViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Manager ViewSet for viewing team leave balances
    """
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, IsLeaveManager]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['leave_type', 'year']
    ordering_fields = ['year', 'allocated_days', 'used_days', 'available_days']
    ordering = ['-year', 'user__last_name']

    def get_queryset(self):
        """Get leave balances for team members"""
        user = self.request.user

        # Staff see everyone; others see their direct reports / department.
        # LeaveBalance points at the employee through ``user``.
        queryset = LeaveBalance.objects.filter(
            tenant=self.request.tenant
        ).filter(team_q(user, prefix='user'))

        return queryset.select_related('user', 'leave_type')

    def get_serializer_class(self):
        if self.action == 'list':
            return LeaveBalanceListSerializer
        return LeaveBalanceDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    @extend_schema(
        tags=['Leave Management - Manager'],
        summary='Get team balances summary'
    )
    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Get aggregated balance summary for team"""
        from django.db.models import Sum
        from datetime import date

        queryset = self.get_queryset().filter(year=date.today().year)

        summary = queryset.aggregate(
            total_allocated=Sum('allocated_days'),
            total_used=Sum('used_days'),
            total_pending=Sum('pending_days'),
            total_carried_forward=Sum('carried_forward_days')
        )

        summary['team_size'] = queryset.values('user').distinct().count()

        return Response(summary)

    @extend_schema(tags=['Leave Management - Manager'], summary='List team leave balances')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Manager'], summary='Get team member balance details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)
