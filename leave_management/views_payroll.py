from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    extend_schema, extend_schema_view, extend_schema_field, OpenApiParameter, inline_serializer,
)
from rest_framework import serializers, viewsets, status, mixins
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .audit import log_event
from .models import (
    SubstituteAssignment, SubstitutionRate, LeaveRequestSubstitute,
    EmployeeCompensation, PayrollRun,
)
from .payroll import (
    set_worked_days, WorkedDaysError, workable_days, backfill_rate,
    report_for, build_report, csv_bytes, parse_period, period_of,
)
from .permissions import HasLeaveManagementFeature, LeaveFeatureEnabled, IsPayrollManager

TAG = ['Leave Management - Payroll']
SubstitutionPayOn = LeaveFeatureEnabled('substitutes_enabled', 'substitution_pay_enabled')
PayrollOn = LeaveFeatureEnabled('payroll_enabled')
PERIOD_PARAM = OpenApiParameter('period', str, OpenApiParameter.QUERY, description='YYYY-MM', required=True)


def _name(user):
    return (user.get_full_name() or user.email) if user else None




# ── substitution rates ──────────────────────────────────────────────────────

class SubstitutionRateRowSerializer(serializers.ModelSerializer):
    """A substitute task with its rate (if any)."""
    owner_name = serializers.SerializerMethodField()
    substitute_name = serializers.SerializerMethodField()
    rate = serializers.SerializerMethodField()

    class Meta:
        model = SubstituteAssignment
        fields = ['id', 'owner', 'owner_name', 'substitute', 'substitute_name', 'task', 'rate']

    @extend_schema_field(serializers.CharField())
    def get_owner_name(self, obj):
        return _name(obj.owner)

    @extend_schema_field(serializers.CharField())
    def get_substitute_name(self, obj):
        return _name(obj.substitute)

    @extend_schema_field(inline_serializer('SubstitutionRateValue', {
        'mode': serializers.ChoiceField(choices=SubstitutionRate.MODES),
        'amount': serializers.DecimalField(max_digits=12, decimal_places=2),
        'currency': serializers.CharField(),
    }, allow_null=True))
    def get_rate(self, obj):
        rate = getattr(obj, 'rate', None)
        if rate is None:
            return None
        return {'mode': rate.mode, 'amount': str(rate.amount), 'currency': rate.currency}


class SubstitutionRateInputSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=SubstitutionRate.MODES)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0.01, max_value=1000000)
    currency = serializers.CharField(required=False, max_length=3)


@extend_schema_view(
    list=extend_schema(tags=TAG, summary='Substitute tasks and their rates', parameters=[
        OpenApiParameter('owner', int, OpenApiParameter.QUERY),
    ]),
    retrieve=extend_schema(tags=TAG, summary='Substitute task and its rate'),
)
class SubstitutionRateViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SubstitutionRateRowSerializer
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, SubstitutionPayOn, IsPayrollManager]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return SubstituteAssignment.objects.none()
        qs = SubstituteAssignment.objects.filter(tenant=self.request.tenant).select_related(
            'owner', 'substitute', 'rate'
        ).order_by('owner__first_name', 'owner__email', 'sort_order', 'id')
        owner = self.request.query_params.get('owner')
        if owner:
            qs = qs.filter(owner_id=owner)
        return qs

    @extend_schema(tags=TAG, summary='Set the rate for a substitute task',
                   description='Applies to new requests; requests that had no rate yet get it too. '
                               'Existing snapshots are never changed.',
                   request=SubstitutionRateInputSerializer, responses={200: SubstitutionRateRowSerializer})
    @action(detail=True, methods=['put'], url_path='rate')
    def set_rate(self, request, pk=None):
        from .utils import get_leave_settings
        assignment = self.get_object()
        data = SubstitutionRateInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        currency = (data.validated_data.get('currency')
                    or get_leave_settings(request.tenant).payroll_currency).upper()
        rate, _ = SubstitutionRate.objects.update_or_create(
            assignment=assignment,
            defaults={'mode': data.validated_data['mode'], 'amount': data.validated_data['amount'],
                      'currency': currency, 'updated_by': request.user},
        )
        filled = backfill_rate(assignment, rate)
        log_event(request.tenant, 'rate', 'set', actor=request.user, subject_user=assignment.substitute,
                  object_id=assignment.pk, data={'owner': assignment.owner_id, 'task': assignment.task,
                                                 'mode': rate.mode, 'amount': str(rate.amount),
                                                 'currency': rate.currency, 'backfilled': filled})
        assignment.refresh_from_db()
        return Response(self.get_serializer(assignment).data)

    @extend_schema(tags=TAG, summary='Remove the rate for a substitute task', request=None,
                   responses={204: None})
    @set_rate.mapping.delete
    def delete_rate(self, request, pk=None):
        assignment = self.get_object()
        deleted, _ = SubstitutionRate.objects.filter(assignment=assignment).delete()
        if deleted:
            log_event(request.tenant, 'rate', 'deleted', actor=request.user,
                      subject_user=assignment.substitute, object_id=assignment.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── worked days ─────────────────────────────────────────────────────────────

class CoverageRowSerializer(serializers.ModelSerializer):
    substitute_name = serializers.SerializerMethodField()
    employee = serializers.IntegerField(source='leave_request.employee_id', read_only=True)
    employee_name = serializers.SerializerMethodField()
    start_date = serializers.DateField(source='leave_request.start_date', read_only=True)
    end_date = serializers.DateField(source='leave_request.end_date', read_only=True)
    request_status = serializers.CharField(source='leave_request.status', read_only=True)
    workable_days = serializers.SerializerMethodField()
    worked_confirmed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequestSubstitute
        fields = [
            'id', 'leave_request', 'employee', 'employee_name', 'start_date', 'end_date',
            'request_status', 'substitute', 'substitute_name', 'task', 'status',
            'rate_mode', 'rate_amount', 'rate_currency', 'worked_days', 'totals_by_month',
            'workable_days', 'worked_confirmed_by', 'worked_confirmed_by_name', 'worked_confirmed_at',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField())
    def get_substitute_name(self, obj):
        return _name(obj.substitute)

    @extend_schema_field(serializers.CharField())
    def get_employee_name(self, obj):
        return _name(obj.leave_request.employee)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_worked_confirmed_by_name(self, obj):
        return _name(obj.worked_confirmed_by)

    @extend_schema_field(serializers.ListField(child=serializers.DateField()))
    def get_workable_days(self, obj):
        return [str(d) for d in workable_days(obj, obj.leave_request.tenant)]


class WorkedDaysInputSerializer(serializers.Serializer):
    days = serializers.ListField(child=serializers.DateField(), max_length=366, allow_empty=True)


@extend_schema_view(
    list=extend_schema(tags=TAG, summary='Substitute rows on approved requests', parameters=[
        OpenApiParameter('period', str, OpenApiParameter.QUERY, description='Requests overlapping YYYY-MM'),
        OpenApiParameter('substitute', int, OpenApiParameter.QUERY),
        OpenApiParameter('leave_request', int, OpenApiParameter.QUERY),
    ]),
    retrieve=extend_schema(tags=TAG, summary='Substitute row'),
)
class CoverageViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CoverageRowSerializer
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, SubstitutionPayOn, IsPayrollManager]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return LeaveRequestSubstitute.objects.none()
        qs = LeaveRequestSubstitute.objects.filter(
            leave_request__tenant=self.request.tenant, leave_request__status='approved',
        ).exclude(status='rejected').select_related(
            'leave_request__employee', 'leave_request__tenant', 'substitute', 'worked_confirmed_by'
        ).order_by('-leave_request__start_date', 'sort_order')
        params = self.request.query_params
        if params.get('period'):
            try:
                first, last = parse_period(params['period'])
            except ValueError:
                raise serializers.ValidationError({'period': 'Use YYYY-MM.'})
            qs = qs.filter(leave_request__start_date__lte=last, leave_request__end_date__gte=first)
        if params.get('substitute'):
            qs = qs.filter(substitute_id=params['substitute'])
        if params.get('leave_request'):
            qs = qs.filter(leave_request_id=params['leave_request'])
        return qs

    @extend_schema(tags=TAG, summary='Confirm the days a substitute actually worked',
                   request=WorkedDaysInputSerializer, responses={200: CoverageRowSerializer})
    @action(detail=True, methods=['post'], url_path='worked-days')
    def worked_days(self, request, pk=None):
        data = WorkedDaysInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        with transaction.atomic():
            row = LeaveRequestSubstitute.objects.select_for_update().select_related(
                'leave_request', 'substitute'
            ).get(pk=self.get_object().pk)
            try:
                set_worked_days(row, data.validated_data['days'], request.user, request.tenant)
            except WorkedDaysError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(row).data)


# ── salaries ────────────────────────────────────────────────────────────────

class EmployeeCompensationSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = EmployeeCompensation
        fields = ['id', 'user', 'user_name', 'monthly_salary', 'currency', 'effective_from',
                  'note', 'created_by', 'created_at']
        read_only_fields = ['id', 'user_name', 'created_by', 'created_at']

    @extend_schema_field(serializers.CharField())
    def get_user_name(self, obj):
        return _name(obj.user)

    def validate_currency(self, value):
        value = (value or '').upper()
        if len(value) != 3 or not value.isalpha():
            raise serializers.ValidationError('Use a 3-letter currency code.')
        return value

    def validate(self, attrs):
        user = attrs.get('user', getattr(self.instance, 'user', None))
        eff = attrs.get('effective_from', getattr(self.instance, 'effective_from', None))
        qs = EmployeeCompensation.objects.filter(user=user, effective_from=eff)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({'effective_from': 'A salary already starts on this date.'})
        return attrs


@extend_schema_view(
    list=extend_schema(tags=TAG, summary='Salary history', parameters=[
        OpenApiParameter('user', int, OpenApiParameter.QUERY),
    ]),
    retrieve=extend_schema(tags=TAG, summary='Salary record'),
    create=extend_schema(tags=TAG, summary='Add a salary record'),
    partial_update=extend_schema(tags=TAG, summary='Correct a salary record'),
    destroy=extend_schema(tags=TAG, summary='Delete a salary record'),
)
class CompensationViewSet(viewsets.ModelViewSet):
    serializer_class = EmployeeCompensationSerializer
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, PayrollOn, IsPayrollManager]
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return EmployeeCompensation.objects.none()
        qs = EmployeeCompensation.objects.filter(tenant=self.request.tenant).select_related('user')
        if self.request.query_params.get('user'):
            qs = qs.filter(user_id=self.request.query_params['user'])
        return qs

    def _guard_locked(self, eff_date):
        if PayrollRun.objects.filter(
            tenant=self.request.tenant, is_locked=True, period__gte=period_of(eff_date)
        ).exists():
            raise serializers.ValidationError(
                {'effective_from': 'A payroll month on or after this date is locked; unlock it first.'}
            )

    def _log(self, action_name, comp, before=None):
        log_event(self.request.tenant, 'compensation', action_name, actor=self.request.user,
                  subject_user=comp.user, object_id=comp.pk,
                  data={'before': before, 'monthly_salary': str(comp.monthly_salary),
                        'currency': comp.currency, 'effective_from': str(comp.effective_from)})

    def perform_create(self, serializer):
        from .utils import get_leave_settings
        self._guard_locked(serializer.validated_data['effective_from'])
        currency = serializer.validated_data.get('currency') or get_leave_settings(self.request.tenant).payroll_currency
        comp = serializer.save(tenant=self.request.tenant, created_by=self.request.user, currency=currency)
        self._log('created', comp)

    def perform_update(self, serializer):
        before = {'monthly_salary': str(serializer.instance.monthly_salary),
                  'effective_from': str(serializer.instance.effective_from)}
        self._guard_locked(min(serializer.instance.effective_from,
                               serializer.validated_data.get('effective_from', serializer.instance.effective_from)))
        comp = serializer.save()
        self._log('updated', comp, before)

    def perform_destroy(self, instance):
        self._guard_locked(instance.effective_from)
        self._log('deleted', instance)
        instance.delete()


# ── report, export, runs ────────────────────────────────────────────────────

REPORT_SCHEMA = inline_serializer('PayrollReport', {
    'period': serializers.CharField(),
    'mode': serializers.ChoiceField(choices=['gross', 'net']),
    'currency': serializers.CharField(),
    'income_tax_rate': serializers.CharField(),
    'pension_rate': serializers.CharField(),
    'working_days': serializers.IntegerField(),
    'locked': serializers.BooleanField(),
    'generated_at': serializers.CharField(),
    'rows': serializers.ListField(child=serializers.DictField()),
    'totals': serializers.DictField(),
})


class PayrollReportViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, PayrollOn, IsPayrollManager]

    def _period(self, request):
        period = request.query_params.get('period')
        try:
            parse_period(period)
        except ValueError:
            return None
        return period

    @extend_schema(tags=TAG, summary='Monthly payroll report', parameters=[PERIOD_PARAM],
                   responses={200: REPORT_SCHEMA})
    def list(self, request):
        period = self._period(request)
        if not period:
            return Response({'period': ['Use YYYY-MM.']}, status=status.HTTP_400_BAD_REQUEST)
        report = report_for(request.tenant, period)
        log_event(request.tenant, 'payroll', 'viewed', actor=request.user, object_id=period,
                  data={'period': period})
        return Response(report)

    @extend_schema(tags=TAG, summary='Payroll report as CSV', parameters=[
        PERIOD_PARAM, OpenApiParameter('lang', str, OpenApiParameter.QUERY, enum=['en', 'ka']),
    ], responses={(200, 'text/csv'): OpenApiTypes.BINARY})
    @action(detail=False, methods=['get'])
    def export(self, request):
        period = self._period(request)
        if not period:
            return Response({'period': ['Use YYYY-MM.']}, status=status.HTTP_400_BAD_REQUEST)
        report = report_for(request.tenant, period)
        lang = request.query_params.get('lang', 'en')
        log_event(request.tenant, 'payroll', 'exported', actor=request.user, object_id=period,
                  data={'period': period, 'rows': len(report['rows'])})
        response = HttpResponse(csv_bytes(report, lang), content_type='text/csv; charset=utf-8')
        schema = getattr(request.tenant, 'schema_name', 'payroll')
        response['Content-Disposition'] = f'attachment; filename="payroll_{schema}_{period}.csv"'
        return response


class MyPayrollViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature,
                          LeaveFeatureEnabled('payroll_enabled', 'payroll_self_view_enabled')]

    @extend_schema(tags=TAG, summary='My payroll row', parameters=[PERIOD_PARAM],
                   responses={200: inline_serializer('MyPayroll', {
                       'period': serializers.CharField(),
                       'mode': serializers.CharField(),
                       'locked': serializers.BooleanField(),
                       'row': serializers.DictField(allow_null=True),
                       'substitutions': serializers.ListField(child=serializers.DictField()),
                   })})
    def list(self, request):
        period = request.query_params.get('period')
        try:
            parse_period(period)
        except ValueError:
            return Response({'period': ['Use YYYY-MM.']}, status=status.HTTP_400_BAD_REQUEST)
        report = report_for(request.tenant, period)
        row = next((r for r in report['rows'] if r['user'] == request.user.pk), None)
        subs = LeaveRequestSubstitute.objects.filter(
            substitute=request.user, leave_request__tenant=request.tenant,
            leave_request__status='approved', totals_by_month__has_key=period,
        ).select_related('leave_request__employee')
        return Response({
            'period': period, 'mode': report['mode'], 'locked': report['locked'], 'row': row,
            'substitutions': [{
                'leave_request': s.leave_request_id, 'employee_name': _name(s.leave_request.employee),
                'task': s.task, 'days': [d for d in s.worked_days if d.startswith(period)],
                'amount': s.totals_by_month.get(period), 'currency': s.rate_currency,
            } for s in subs],
        })


class PayrollRunSerializer(serializers.ModelSerializer):
    locked_by_name = serializers.SerializerMethodField()

    class Meta:
        model = PayrollRun
        fields = ['id', 'period', 'mode', 'is_locked', 'locked_by', 'locked_by_name', 'locked_at',
                  'unlocked_by', 'unlocked_at', 'unlock_reason']
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_locked_by_name(self, obj):
        return _name(obj.locked_by)


@extend_schema_view(
    list=extend_schema(tags=TAG, summary='Payroll runs (locked months)'),
    retrieve=extend_schema(tags=TAG, summary='Payroll run'),
)
class PayrollRunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PayrollRunSerializer
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, PayrollOn, IsPayrollManager]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return PayrollRun.objects.none()
        return PayrollRun.objects.filter(tenant=self.request.tenant).select_related('locked_by')

    @extend_schema(tags=TAG, summary='Lock a payroll month',
                   description='Freezes the report as it is now; worked days and salary changes '
                               'for that month are refused until it is unlocked.',
                   request=inline_serializer('PayrollLockRequest', {'period': serializers.CharField()}),
                   responses={200: PayrollRunSerializer})
    @action(detail=False, methods=['post'])
    def lock(self, request):
        period = request.data.get('period')
        try:
            parse_period(period)
        except ValueError:
            return Response({'period': ['Use YYYY-MM.']}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            run = PayrollRun.objects.select_for_update().filter(tenant=request.tenant, period=period).first()
            if run and run.is_locked:
                return Response({'error': 'This month is already locked.'}, status=status.HTTP_400_BAD_REQUEST)
            report = build_report(request.tenant, period)
            if run is None:
                run = PayrollRun(tenant=request.tenant, period=period)
            run.mode = report['mode']
            run.snapshot = report
            run.is_locked = True
            run.locked_by = request.user
            run.locked_at = timezone.now()
            run.save()
            log_event(request.tenant, 'payroll', 'locked', actor=request.user, object_id=period,
                      data={'period': period, 'rows': len(report['rows']), 'totals': report['totals']})
        return Response(self.get_serializer(run).data)

    @extend_schema(tags=TAG, summary='Unlock a payroll month (staff)',
                   request=inline_serializer('PayrollUnlockRequest', {'reason': serializers.CharField()}),
                   responses={200: PayrollRunSerializer})
    @action(detail=True, methods=['post'])
    def unlock(self, request, pk=None):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'detail': 'Only administrators can unlock payroll.'}, status=status.HTTP_403_FORBIDDEN)
        reason = (request.data.get('reason') or '').strip()
        if not reason:
            return Response({'reason': ['A reason is required.']}, status=status.HTTP_400_BAD_REQUEST)
        run = self.get_object()
        if not run.is_locked:
            return Response({'error': 'This month is not locked.'}, status=status.HTTP_400_BAD_REQUEST)
        run.is_locked = False
        run.unlocked_by = request.user
        run.unlocked_at = timezone.now()
        run.unlock_reason = reason
        run.save()
        log_event(request.tenant, 'payroll', 'unlocked', actor=request.user, object_id=run.period,
                  data={'period': run.period, 'reason': reason})
        return Response(self.get_serializer(run).data)
