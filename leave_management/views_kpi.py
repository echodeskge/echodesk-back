"""
Monthly KPI goals (INSERVICE app/kpi.tsx + policy.ts rules).

draft --(employee submits)--> pending --(manager approves)--> approved
                                    \\--(manager returns + feedback)--> returned
A target can be edited by the employee's manager (never by the employee)
while draft or returned; editing resets the result. The employee can
resubmit at any time, which sends the KPI back to review.
"""
import re
from datetime import date

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view, extend_schema_field, OpenApiParameter
from rest_framework import serializers, viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .audit import log_event, serialize_events
from .models import KpiGoal
from .permissions import HasLeaveManagementFeature, LeaveFeatureEnabled
from .utils import manages_employee, team_q, is_leave_manager

TAG = ['Leave Management - KPI']
PERIOD_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
TARGET_FIELDS = ('title', 'target', 'unit', 'direction')


def _name(user):
    return (user.get_full_name() or user.email) if user else None


def validate_period(value):
    if not PERIOD_RE.match(value or ''):
        raise serializers.ValidationError('Use YYYY-MM.')
    return value


class KpiGoalSerializer(serializers.ModelSerializer):
    employee_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()
    percent = serializers.SerializerMethodField()
    target_met = serializers.SerializerMethodField()
    can_edit_target = serializers.SerializerMethodField()
    can_submit = serializers.SerializerMethodField()
    can_review = serializers.SerializerMethodField()
    history = serializers.SerializerMethodField()

    class Meta:
        model = KpiGoal
        fields = [
            'id', 'employee', 'employee_name', 'period', 'title', 'target', 'unit',
            'direction', 'actual', 'employee_note', 'manager_feedback', 'status',
            'percent', 'target_met', 'submitted_at', 'reviewed_at', 'reviewed_by',
            'reviewed_by_name', 'created_by', 'created_by_name', 'created_at', 'updated_at',
            'can_edit_target', 'can_submit', 'can_review', 'history',
        ]
        read_only_fields = [
            'actual', 'employee_note', 'manager_feedback', 'status', 'submitted_at',
            'reviewed_at', 'reviewed_by', 'created_by', 'created_at', 'updated_at',
        ]

    def validate_period(self, value):
        return validate_period(value)

    def validate_title(self, value):
        if not value.strip():
            raise serializers.ValidationError('Required.')
        return value.strip()

    def validate_unit(self, value):
        if not value.strip():
            raise serializers.ValidationError('Required.')
        return value.strip()

    def validate(self, attrs):
        if self.instance is not None:
            for field in ('employee', 'period'):
                if field in attrs and attrs[field] != getattr(self.instance, field):
                    raise serializers.ValidationError({field: 'Cannot be changed after creation.'})
        return attrs

    def _user(self):
        request = self.context.get('request')
        return getattr(request, 'user', None)

    @extend_schema_field(serializers.CharField())
    def get_employee_name(self, obj):
        return _name(obj.employee)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_created_by_name(self, obj):
        return _name(obj.created_by)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_reviewed_by_name(self, obj):
        return _name(obj.reviewed_by)

    @extend_schema_field(serializers.DecimalField(max_digits=8, decimal_places=1, allow_null=True))
    def get_percent(self, obj):
        value = obj.percent
        return str(value) if value is not None else None

    @extend_schema_field(serializers.BooleanField())
    def get_target_met(self, obj):
        if obj.actual is None:
            return False
        if obj.direction == 'higher':
            return obj.actual >= obj.target
        return obj.actual <= obj.target

    @extend_schema_field(serializers.BooleanField())
    def get_can_edit_target(self, obj):
        return can_manage_goal(self._user(), obj) and obj.status in ('draft', 'returned')

    @extend_schema_field(serializers.BooleanField())
    def get_can_submit(self, obj):
        user = self._user()
        return bool(user and obj.employee_id == user.pk)

    @extend_schema_field(serializers.BooleanField())
    def get_can_review(self, obj):
        return can_manage_goal(self._user(), obj) and obj.status == 'pending'

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_history(self, obj):
        if self.context.get('view') and getattr(self.context['view'], 'action', None) == 'list':
            return []
        return serialize_events(obj.audit_events.order_by('created_at', 'id'))


class KpiSubmitSerializer(serializers.Serializer):
    actual = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    note = serializers.CharField(required=False, allow_blank=True, default='')


class KpiReviewSerializer(serializers.Serializer):
    feedback = serializers.CharField(required=False, allow_blank=True, default='')


class KpiCopySerializer(serializers.Serializer):
    period = serializers.CharField(help_text='Target month YYYY-MM; goals are copied from the month before')

    def validate_period(self, value):
        return validate_period(value)


def can_manage_goal(user, goal_or_employee):
    """Manager (or staff) of the employee, never the employee themselves."""
    if not user or not user.is_authenticated:
        return False
    employee = getattr(goal_or_employee, 'employee', goal_or_employee)
    if employee is None or employee.pk == user.pk:
        return False
    return manages_employee(user, employee)


def _notify(user, ntype, key, goal, link_user_is_employee=True, **params):
    from users.notification_utils import create_notification
    from users.notification_texts import T
    if user is None or not user.is_active:
        return
    create_notification(
        user=user,
        notification_type=ntype,
        title=T(f'{key}.title', period=goal.period),
        message=T(f'{key}.message', title=goal.title, period=goal.period, unit=goal.unit,
                  target=goal.target, actual=goal.actual, **params),
        metadata={'kpi_id': goal.id, 'period': goal.period, 'employee_id': goal.employee_id},
        link_url=f'/leave/kpi?id={goal.id}',
    )


def _reviewer_for(goal):
    manager = goal.employee.manager
    if manager and manager.is_active and manager.pk != goal.employee_id:
        return manager
    if goal.created_by and goal.created_by.pk != goal.employee_id:
        return goal.created_by
    return None


SCOPE_PARAM = OpenApiParameter(
    'scope', str, OpenApiParameter.QUERY, enum=['mine', 'team', 'all'],
    description='mine (default): my KPIs; team: my reports; all: everyone (staff)'
)


@extend_schema_view(
    list=extend_schema(tags=TAG, summary='List KPI goals', parameters=[
        SCOPE_PARAM,
        OpenApiParameter('period', str, OpenApiParameter.QUERY, description='YYYY-MM'),
        OpenApiParameter('employee', int, OpenApiParameter.QUERY),
        OpenApiParameter('status', str, OpenApiParameter.QUERY),
    ]),
    retrieve=extend_schema(tags=TAG, summary='KPI goal'),
    create=extend_schema(tags=TAG, summary='Set a KPI target (manager)'),
    partial_update=extend_schema(tags=TAG, summary='Edit a KPI target (manager; draft/returned only)'),
    destroy=extend_schema(tags=TAG, summary='Delete a draft KPI'),
)
class KpiGoalViewSet(viewsets.ModelViewSet):
    serializer_class = KpiGoalSerializer
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, LeaveFeatureEnabled('kpi_enabled')]
    http_method_names = ['get', 'post', 'patch', 'delete']

    def _visible(self):
        user = self.request.user
        base = KpiGoal.objects.filter(tenant=self.request.tenant)
        if user.is_staff or user.is_superuser:
            return base
        return base.filter(Q(employee=user) | team_q(user))

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return KpiGoal.objects.none()
        user = self.request.user
        qs = self._visible().select_related('employee', 'created_by', 'reviewed_by')
        if self.action == 'list':
            scope = self.request.query_params.get('scope', 'mine')
            if scope == 'mine':
                qs = qs.filter(employee=user)
            elif scope == 'team':
                qs = qs.filter(team_q(user)).exclude(employee=user)
            elif scope == 'all':
                if not (user.is_staff or user.is_superuser):
                    raise PermissionDenied('Only administrators can list everyone.')
            else:
                raise ValidationError({'scope': 'Use mine, team or all.'})
            params = self.request.query_params
            if params.get('period'):
                qs = qs.filter(period=params['period'])
            if params.get('employee'):
                try:
                    qs = qs.filter(employee_id=int(params['employee']))
                except (TypeError, ValueError):
                    raise ValidationError({'employee': 'Must be an integer id'})
            if params.get('status'):
                qs = qs.filter(status=params['status'])
        return qs

    def perform_create(self, serializer):
        employee = serializer.validated_data['employee']
        if not can_manage_goal(self.request.user, employee):
            raise PermissionDenied("Only the employee's manager can set their KPI targets.")
        goal = serializer.save(
            tenant=self.request.tenant, created_by=self.request.user,
            updated_by=self.request.user, status='draft',
        )
        log_event(goal.tenant, 'kpi', 'created', actor=self.request.user, kpi=goal,
                  subject_user=goal.employee, object_id=goal.pk, from_status='new', to_status='draft',
                  data={f: str(getattr(goal, f)) for f in TARGET_FIELDS})
        _notify(goal.employee, 'kpi_assigned', 'kpi.assigned', goal)

    def perform_update(self, serializer):
        goal = serializer.instance
        if not can_manage_goal(self.request.user, goal):
            raise PermissionDenied("Only the employee's manager can edit the target.")
        if goal.status not in ('draft', 'returned'):
            raise ValidationError('Targets can only be edited while draft or returned.')
        old = goal.status
        goal = serializer.save(
            updated_by=self.request.user, status='draft', actual=None, employee_note='',
            manager_feedback='', submitted_at=None, reviewed_at=None, reviewed_by=None,
        )
        log_event(goal.tenant, 'kpi', 'target_changed', actor=self.request.user, kpi=goal,
                  subject_user=goal.employee, object_id=goal.pk, from_status=old, to_status='draft',
                  data={f: str(getattr(goal, f)) for f in TARGET_FIELDS})

    def perform_destroy(self, instance):
        user = self.request.user
        if instance.status != 'draft' or not (
            instance.created_by_id == user.pk or user.is_staff or user.is_superuser
        ):
            raise PermissionDenied('Only draft KPIs can be deleted, by their creator.')
        log_event(instance.tenant, 'kpi', 'deleted', actor=user, subject_user=instance.employee,
                  object_id=instance.pk, from_status=instance.status, to_status='deleted',
                  data={'title': instance.title, 'period': instance.period})
        instance.delete()

    def _locked(self):
        goal = self.get_object()
        return KpiGoal.objects.select_for_update().select_related('employee').get(pk=goal.pk)

    @extend_schema(tags=TAG, summary='Submit my result', request=KpiSubmitSerializer,
                   responses={200: KpiGoalSerializer})
    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        data = KpiSubmitSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        with transaction.atomic():
            goal = self._locked()
            if goal.employee_id != request.user.pk:
                raise PermissionDenied('Only the employee submits the result.')
            old = goal.status
            goal.actual = data.validated_data['actual']
            goal.employee_note = data.validated_data.get('note', '')
            goal.manager_feedback = ''
            goal.status = 'pending'
            goal.submitted_at = timezone.now()
            goal.reviewed_at = None
            goal.reviewed_by = None
            goal.updated_by = request.user
            goal.save()
            log_event(goal.tenant, 'kpi', 'submitted', actor=request.user, kpi=goal,
                      subject_user=goal.employee, object_id=goal.pk, from_status=old,
                      to_status='pending', data={'actual': str(goal.actual)})
        _notify(_reviewer_for(goal), 'kpi_submitted', 'kpi.submitted', goal,
                employee=_name(goal.employee))
        return Response(self.get_serializer(goal).data)

    def _review(self, request, decision):
        data = KpiReviewSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        feedback = (data.validated_data.get('feedback') or '').strip()
        if decision == 'returned' and not feedback:
            return Response({'feedback': ['Feedback is required when returning a KPI.']},
                            status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            goal = self._locked()
            if not can_manage_goal(request.user, goal):
                raise PermissionDenied("Only the employee's manager can review.")
            if goal.status != 'pending':
                return Response({'error': 'Only submitted KPIs can be reviewed.'},
                                status=status.HTTP_400_BAD_REQUEST)
            goal.status = decision
            goal.manager_feedback = feedback
            goal.reviewed_at = timezone.now()
            goal.reviewed_by = request.user
            goal.updated_by = request.user
            goal.save()
            log_event(goal.tenant, 'kpi', decision, actor=request.user, kpi=goal,
                      subject_user=goal.employee, object_id=goal.pk, from_status='pending',
                      to_status=decision, data={'feedback': feedback})
        key = 'kpi.approved' if decision == 'approved' else 'kpi.returned'
        _notify(goal.employee, f'kpi_{decision}', key, goal, feedback=feedback)
        return Response(self.get_serializer(goal).data)

    @extend_schema(tags=TAG, summary='Approve a submitted result', request=KpiReviewSerializer,
                   responses={200: KpiGoalSerializer})
    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        return self._review(request, 'approved')

    @extend_schema(tags=TAG, summary='Return a submitted result with feedback',
                   request=KpiReviewSerializer, responses={200: KpiGoalSerializer})
    @action(detail=True, methods=['post'], url_path='return')
    def return_goal(self, request, pk=None):
        return self._review(request, 'returned')

    @extend_schema(
        tags=TAG, summary="Copy last month's goals to a month (as drafts)",
        request=KpiCopySerializer,
        responses={200: KpiGoalSerializer(many=True)},
    )
    @action(detail=False, methods=['post'], url_path='copy-previous')
    def copy_previous(self, request):
        data = KpiCopySerializer(data=request.data)
        data.is_valid(raise_exception=True)
        period = data.validated_data['period']
        year, month = map(int, period.split('-'))
        prev = date(year - 1, 12, 1) if month == 1 else date(year, month - 1, 1)
        prev_period = prev.strftime('%Y-%m')
        user = request.user
        if not is_leave_manager(user):
            raise PermissionDenied('Only managers can copy goals.')
        sources = KpiGoal.objects.filter(
            tenant=request.tenant, period=prev_period
        ).filter(team_q(user)).exclude(employee=user).select_related('employee')
        existing = set(
            KpiGoal.objects.filter(tenant=request.tenant, period=period)
            .values_list('employee_id', 'title')
        )
        created = []
        with transaction.atomic():
            for src in sources:
                if (src.employee_id, src.title) in existing or not src.employee.is_active:
                    continue
                goal = KpiGoal.objects.create(
                    tenant=request.tenant, employee=src.employee, period=period,
                    title=src.title, target=src.target, unit=src.unit, direction=src.direction,
                    created_by=user, updated_by=user,
                )
                log_event(goal.tenant, 'kpi', 'created', actor=user, kpi=goal,
                          subject_user=goal.employee, object_id=goal.pk, from_status='new',
                          to_status='draft', data={'copied_from': src.pk})
                created.append(goal)
        for goal in created:
            _notify(goal.employee, 'kpi_assigned', 'kpi.assigned', goal)
        return Response(self.get_serializer(created, many=True).data)
