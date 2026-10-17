from rest_framework import viewsets, filters, status, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.db import IntegrityError, transaction
from django.db.models import F
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    extend_schema, extend_schema_view, OpenApiParameter, OpenApiResponse, inline_serializer
)

from .models import (
    LeaveSettings, LeaveType, LeaveBalance, LeaveRequest,
    PublicHoliday, LeaveApprovalChain, LeaveCarryForwardRun
)
from .serializers import (
    LeaveActionResponseSerializer, LeaveErrorResponseSerializer,
    LeaveActionErrorResponseSerializer, LeaveForbiddenResponseSerializer,
    CarryForwardRequestSerializer, CarryForwardResponseSerializer,
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
    update_leave_balance, carry_forward_balances, initialize_leave_balances_for_user,
    CarryForwardAlreadyRun, apply_approval_step, filter_by_status_in,
    OPEN_APPROVAL_STATUSES,
)


# ============================================================================
# SCHEMA HELPERS
# ============================================================================
# The admin viewsets build their querysets from request.tenant, which does
# not exist during schema generation; they return .none() for the fake view
# so drf-spectacular can document their django-filter params. Their {id}
# path params stay strings (that is what the dashboard's client was
# generated with).

LANG_PARAM = OpenApiParameter(
    name='lang', type=str, location=OpenApiParameter.QUERY, enum=['en', 'ka'],
    description='Language of the *_display / *_name fields (default en)'
)
STR_ID = {'id': OpenApiTypes.STR}
ERROR_RESPONSE = OpenApiResponse(LeaveErrorResponseSerializer, description='{error}')
ACTION_ERROR_RESPONSE = OpenApiResponse(
    LeaveActionErrorResponseSerializer,
    description='{error} when the request is not in a state that allows the '
                'action, or DRF field errors such as {action: [...]} / '
                '{comments: [...]} when the body is invalid'
)
FORBIDDEN_RESPONSE = OpenApiResponse(
    LeaveForbiddenResponseSerializer,
    description='{detail} when the user may not act on this request (not '
                'admin/HR, not the approver, or their own request)'
)


def lock_leave_request(leave_request):
    """
    Re-read ``leave_request`` with a row lock (call inside
    transaction.atomic). Callers re-check its status on the returned row,
    so two concurrent decisions on the same request are serialised and the
    second one sees the first one's result (no double-counted days).
    """
    return LeaveRequest.objects.select_for_update().get(pk=leave_request.pk)


def _is_schema_view(view):
    return getattr(view, 'swagger_fake_view', False)


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

    schema_path_param_types = STR_ID

    def get_queryset(self):
        if _is_schema_view(self):
            return LeaveSettings.objects.none()
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

    schema_path_param_types = STR_ID

    def get_queryset(self):
        if _is_schema_view(self):
            return LeaveType.objects.none()
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

    @extend_schema(tags=['Leave Management - Admin'], summary='List leave types', parameters=[LANG_PARAM])
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Get leave type details', parameters=[LANG_PARAM])
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Create leave type')
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Update leave type')
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Leave Management - Admin'], summary='Partially update leave type',
        description='Omitted fields keep their stored values, so e.g. '
                    '`{"is_active": false}` deactivates a type.'
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        tags=['Leave Management - Admin'], summary='Delete leave type',
        responses={204: None, 400: ERROR_RESPONSE},
    )
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


class BalanceOrderingFilter(filters.OrderingFilter):
    """
    OrderingFilter that can sort by the computed ``available_days``
    (a model property, so it is annotated as ``available_days_value``).
    """
    ALIASES = {'available_days': 'available_days_value'}

    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        if not ordering:
            return ordering
        mapped = []
        for field in ordering:
            desc = field.startswith('-')
            name = field.lstrip('-')
            name = self.ALIASES.get(name, name)
            mapped.append(f'-{name}' if desc else name)
        return mapped


class AdminLeaveBalanceViewSet(viewsets.ModelViewSet):
    """
    Admin ViewSet for managing all leave balances
    """
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, CanManageLeaveBalances]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, BalanceOrderingFilter]
    filterset_fields = ['user', 'leave_type', 'year']
    search_fields = ['user__email', 'user__first_name', 'user__last_name']
    ordering_fields = ['year', 'allocated_days', 'used_days', 'available_days']
    ordering = ['-year', 'user__last_name']
    http_method_names = ['get', 'post', 'patch', 'delete']
    schema_path_param_types = STR_ID

    def get_queryset(self):
        if _is_schema_view(self):
            return LeaveBalance.objects.none()
        return LeaveBalance.objects.filter(tenant=self.request.tenant).select_related(
            'user', 'leave_type'
        ).annotate(
            available_days_value=(
                F('allocated_days') + F('carried_forward_days')
                - F('used_days') - F('pending_days')
            )
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
        try:
            with transaction.atomic():
                serializer.save(tenant=self.request.tenant)
        except IntegrityError:
            raise serializers.ValidationError(
                {'non_field_errors': ['A balance for this user, leave type and year already exists']}
            )

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
        responses={
            200: LeaveActionResponseSerializer,
            400: ERROR_RESPONSE,
            404: ERROR_RESPONSE,
        }
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
        except (User.DoesNotExist, ValueError, TypeError):
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
        description=(
            'Moves what is left of each from_year balance (allocated + carried in '
            '- used; pending days are not subtracted), capped at the leave '
            "type's max_carry_forward_days, into the to_year balance's "
            'carried_forward_days (set, not added). Runs once per year: a '
            'repeat call is a no-op that returns already_run=true. A superuser '
            'can pass force=true to recompute (overwrites the carried days).'
        ),
        request=CarryForwardRequestSerializer,
        responses={
            200: CarryForwardResponseSerializer,
            400: OpenApiResponse(description='{error} when a year is missing, else field errors ({to_year: [...]})'),
            403: ERROR_RESPONSE,
            409: OpenApiResponse(
                LeaveErrorResponseSerializer,
                description='{error} when a concurrent first run collided and its result is not visible yet'
            ),
        }
    )
    @action(detail=False, methods=['post'])
    def carry_forward(self, request):
        """Carry forward unused balances from one year to next"""
        if not request.data.get('from_year') or not request.data.get('to_year'):
            return Response(
                {'error': 'from_year and to_year are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = CarryForwardRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from_year = serializer.validated_data['from_year']
        to_year = serializer.validated_data['to_year']
        force = serializer.validated_data.get('force', False)

        if force and not request.user.is_superuser:
            return Response(
                {'error': 'Only a superuser can force a repeated carry forward'},
                status=status.HTTP_403_FORBIDDEN
            )

        try:
            run, count = carry_forward_balances(
                request.tenant, from_year, to_year, user=request.user, force=force
            )
        except (CarryForwardAlreadyRun, IntegrityError) as exc:
            if isinstance(exc, CarryForwardAlreadyRun):
                run = exc.run
            else:
                # Lost the race with a concurrent first run: its row now
                # exists (unique per tenant and from_year) and our writes
                # were rolled back with the failed transaction.
                run = LeaveCarryForwardRun.objects.filter(
                    tenant=request.tenant, from_year=from_year
                ).first()
                if run is None:
                    return Response(
                        {'error': 'A carry forward for this year is already in progress; try again'},
                        status=status.HTTP_409_CONFLICT
                    )
            return Response({
                'success': True,
                'message': f'Balances from {run.from_year} were already carried forward to {run.to_year}; nothing changed',
                'already_run': True,
                'carried_forward': 0,
                'from_year': run.from_year,
                'to_year': run.to_year,
                'run_at': run.updated_at,
                'total_days': run.total_days,
            })

        return Response({
            'success': True,
            'message': f'Carried forward {count} balance(s) from {from_year} to {to_year}',
            'already_run': False,
            'carried_forward': count,
            'from_year': from_year,
            'to_year': to_year,
            'run_at': run.updated_at,
            'total_days': run.total_days,
        })

    @extend_schema(
        tags=['Leave Management - Admin'], summary='List all leave balances',
        parameters=[LANG_PARAM],
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Get balance details', parameters=[LANG_PARAM])
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Update leave balance')
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)


@extend_schema_view(
    list=extend_schema(
        tags=['Leave Management - Admin'],
        summary='List all leave requests',
        parameters=[
            OpenApiParameter(
                name='status__in', type=str, location=OpenApiParameter.QUERY,
                description='Comma-separated statuses, e.g. `manager_approved,hr_approved` '
                            '(the HR inbox). Use `status` for a single value.'
            ),
            OpenApiParameter(
                name='department', type=int, location=OpenApiParameter.QUERY,
                description="Employee's department id"
            ),
            OpenApiParameter(
                name='start_date', type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY,
                description='Window start (YYYY-MM-DD): requests whose end_date is on or after this day'
            ),
            OpenApiParameter(
                name='end_date', type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY,
                description='Window end (YYYY-MM-DD): requests whose start_date is on or before this day'
            ),
            LANG_PARAM,
        ],
    ),
    retrieve=extend_schema(
        tags=['Leave Management - Admin'], summary='Get leave request details',
        parameters=[LANG_PARAM],
    ),
    create=extend_schema(tags=['Leave Management - Admin'], summary='Create leave request'),
    update=extend_schema(tags=['Leave Management - Admin'], summary='Update leave request'),
    partial_update=extend_schema(tags=['Leave Management - Admin'], summary='Partially update leave request'),
)
class AdminLeaveRequestViewSet(viewsets.ModelViewSet):
    """
    Admin ViewSet for managing all leave requests
    """
    # Everyone's leave requests: administrators / HR only. The decision
    # actions keep this gate too (they used to drop it, letting a team
    # manager act through the admin route).
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, CanManageLeaveBalances]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['employee', 'leave_type', 'status']
    search_fields = ['employee__email', 'employee__first_name', 'employee__last_name', 'reason']
    ordering_fields = ['start_date', 'end_date', 'total_days', 'created_at', 'status']
    ordering = ['-created_at']
    schema_path_param_types = STR_ID

    def get_queryset(self):
        if _is_schema_view(self):
            return LeaveRequest.objects.none()
        queryset = LeaveRequest.objects.filter(tenant=self.request.tenant).select_related(
            'employee', 'leave_type', 'manager_approver', 'hr_approver', 'final_approver', 'rejected_by'
        )

        params = self.request.query_params

        # Date window: a request is included when it overlaps the window
        # (same rule as the manager list), so leave straddling an edge shows.
        start_date = params.get('start_date')
        end_date = params.get('end_date')

        if start_date:
            queryset = queryset.filter(end_date__gte=start_date)
        if end_date:
            queryset = queryset.filter(start_date__lte=end_date)

        queryset = filter_by_status_in(queryset, params.get('status__in'))

        department = params.get('department')
        if department:
            try:
                queryset = queryset.filter(employee__department_id=int(department))
            except (TypeError, ValueError):
                raise serializers.ValidationError({'department': 'Must be an integer id'})

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
        description=(
            'Signs off the next open step of the approval chain '
            '(pending -> manager_approved -> hr_approved -> approved; steps '
            'not in the chain are skipped). When no step is left the request '
            'becomes approved and its days move from pending to used. '
            '`action` must be "approve".'
        ),
        request=LeaveApprovalSerializer,
        responses={200: LeaveActionResponseSerializer, 400: ACTION_ERROR_RESPONSE, 403: FORBIDDEN_RESPONSE},
    )
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasLeaveManagementFeature, CanManageLeaveBalances, CanApproveLeave])
    def approve(self, request, pk=None):
        """Approve a leave request"""
        leave_request = self.get_object()

        if leave_request.status not in OPEN_APPROVAL_STATUSES:
            return Response(
                {'error': 'Only pending or partially approved requests can be approved'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = LeaveApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data['action'] != 'approve':
            return Response(
                {'error': 'action must be "approve" on this endpoint (use reject/ to reject)'},
                status=status.HTTP_400_BAD_REQUEST
            )
        comments = serializer.validated_data.get('comments', '')

        with transaction.atomic():
            leave_request = lock_leave_request(leave_request)
            if leave_request.status not in OPEN_APPROVAL_STATUSES:
                # Decided by a concurrent request while we waited for the lock
                return Response(
                    {'error': 'Only pending or partially approved requests can be approved'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            apply_approval_step(leave_request, request.user, comments)
            leave_request._leave_actor = request.user
            leave_request.save()

        message = (
            'Leave request approved successfully'
            if leave_request.status == 'approved'
            else 'Approval recorded; the request awaits the next approver'
        )
        return Response({
            'success': True,
            'message': message,
            'status': leave_request.status
        })

    @extend_schema(
        tags=['Leave Management - Admin'],
        summary='Reject leave request',
        description='`action` must be "reject" and `comments` (the reason) are required.',
        request=LeaveApprovalSerializer,
        responses={200: LeaveActionResponseSerializer, 400: ACTION_ERROR_RESPONSE, 403: FORBIDDEN_RESPONSE},
    )
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasLeaveManagementFeature, CanManageLeaveBalances, CanApproveLeave])
    def reject(self, request, pk=None):
        """Reject a leave request"""
        leave_request = self.get_object()

        if leave_request.status not in OPEN_APPROVAL_STATUSES:
            return Response(
                {'error': 'Only pending or partially approved requests can be rejected'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = LeaveApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data['action'] != 'reject':
            return Response(
                {'error': 'action must be "reject" on this endpoint (use approve/ to approve)'},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            leave_request = lock_leave_request(leave_request)
            if leave_request.status not in OPEN_APPROVAL_STATUSES:
                return Response(
                    {'error': 'Only pending or partially approved requests can be rejected'},
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

    @extend_schema(
        tags=['Leave Management - Admin'],
        summary='Cancel leave request',
        description='Cancels a pending, partially approved or approved request '
                    '(approved days go back to the balance). Rejected or '
                    'cancelled requests cannot be cancelled.',
        request=LeaveCancellationSerializer,
        responses={200: LeaveActionResponseSerializer, 400: ACTION_ERROR_RESPONSE, 403: FORBIDDEN_RESPONSE},
    )
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasLeaveManagementFeature, CanManageLeaveBalances, CanCancelLeave])
    def cancel(self, request, pk=None):
        """Cancel a leave request"""
        leave_request = self.get_object()

        if leave_request.status == 'cancelled':
            return Response(
                {'error': 'Leave request is already cancelled'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if leave_request.status == 'rejected':
            # Its pending days were already released on rejection; releasing
            # them again ate into the employee's other open requests.
            return Response(
                {'error': 'Cannot cancel a rejected leave request'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = LeaveCancellationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            leave_request = lock_leave_request(leave_request)
            if leave_request.status in ('cancelled', 'rejected'):
                return Response(
                    {'error': f'Leave request is already {leave_request.status}'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            was_approved = leave_request.status == 'approved'

            leave_request.status = 'cancelled'
            leave_request.cancelled_at = timezone.now()
            leave_request.cancellation_reason = serializer.validated_data.get('reason', '')
            leave_request._leave_actor = request.user
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
            'message': 'Leave request cancelled successfully',
            'status': leave_request.status,
        })

    @extend_schema(tags=['Leave Management - Admin'], summary='Delete leave request')
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        # Give the days back and delete in one transaction, on the locked
        # row, so the balance never changes without the delete (or twice
        # for two concurrent deletes). Approved requests hold used days
        # (this used to release pending days instead), open ones hold
        # pending days; rejected / cancelled ones hold nothing.
        with transaction.atomic():
            instance = lock_leave_request(instance)
            if instance.status == 'approved':
                update_leave_balance(instance, action='cancel_approved')
            elif instance.status in OPEN_APPROVAL_STATUSES:
                update_leave_balance(instance, action='cancel')
            self.perform_destroy(instance)

        return Response(status=status.HTTP_204_NO_CONTENT)


class PublicHolidayViewSet(viewsets.ModelViewSet):
    """
    Admin ViewSet for managing public holidays
    """
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, CanManagePublicHolidays]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['is_recurring', 'applies_to_all']
    ordering_fields = ['date']
    ordering = ['date']
    schema_path_param_types = STR_ID

    def get_queryset(self):
        if _is_schema_view(self):
            return PublicHoliday.objects.none()
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

    @extend_schema(
        tags=['Leave Management - Admin'], summary='List public holidays',
        parameters=[
            OpenApiParameter(
                name='year', type=int, location=OpenApiParameter.QUERY,
                description='Only holidays dated in this year'
            ),
            LANG_PARAM,
        ],
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Get holiday details', parameters=[LANG_PARAM])
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
    schema_path_param_types = STR_ID

    def get_queryset(self):
        if _is_schema_view(self):
            return LeaveApprovalChain.objects.none()
        return LeaveApprovalChain.objects.filter(tenant=self.request.tenant).select_related('leave_type')

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.tenant)

    @extend_schema(tags=['Leave Management - Admin'], summary='List approval chains', parameters=[LANG_PARAM])
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Get approval chain details', parameters=[LANG_PARAM])
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Create approval chain')
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Update approval chain')
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Leave Management - Admin'], summary='Partially update approval chain',
        description='Omitted fields (level, leave_type) keep their stored values.'
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(tags=['Leave Management - Admin'], summary='Delete approval chain')
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)
