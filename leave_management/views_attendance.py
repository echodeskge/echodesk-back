"""Employees x days attendance grid for a month (INSERVICE Calendar tab)."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiParameter, inline_serializer
from rest_framework import serializers, viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import LeaveRequest, PublicHoliday
from .payroll import parse_period
from .permissions import HasLeaveManagementFeature, IsLeaveManager, LeaveFeatureEnabled
from .utils import employment_start, employment_end, _weekend_days, holiday_dates

User = get_user_model()


class AttendanceViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature,
                          LeaveFeatureEnabled('attendance_grid_enabled'), IsLeaveManager]

    @extend_schema(
        tags=['Leave Management - Manager'], summary='Attendance grid for a month',
        description='Cell codes: worked, future, off (weekend/holiday), not_employed, or '
                    'leave:<leave_type_id> (see legend).',
        parameters=[
            OpenApiParameter('period', str, OpenApiParameter.QUERY, required=True, description='YYYY-MM'),
            OpenApiParameter('department', int, OpenApiParameter.QUERY),
        ],
        responses={200: inline_serializer('AttendanceGrid', {
            'period': serializers.CharField(),
            'days': serializers.ListField(child=serializers.DictField()),
            'rows': serializers.ListField(child=serializers.DictField()),
            'legend': serializers.ListField(child=serializers.DictField()),
        })},
    )
    def list(self, request):
        try:
            first, last = parse_period(request.query_params.get('period'))
        except ValueError:
            return Response({'period': ['Use YYYY-MM.']}, status=status.HTTP_400_BAD_REQUEST)
        tenant = request.tenant
        user = request.user
        lang = request.query_params.get('lang', 'en')
        today = timezone.localdate()

        users = User.objects.filter(is_active=True) | User.objects.filter(employment_end_date__gte=first)
        if not (user.is_staff or user.is_superuser):
            q = Q(manager=user)
            if user.role == 'manager' and user.department_id:
                q |= Q(department_id=user.department_id)
            users = users.filter(q).exclude(pk=user.pk)
        department = request.query_params.get('department')
        if department:
            try:
                users = users.filter(department_id=int(department))
            except (TypeError, ValueError):
                return Response({'department': ['Must be an integer id']}, status=status.HTTP_400_BAD_REQUEST)
        users = list(users.distinct().select_related('department').order_by('first_name', 'last_name', 'email'))

        weekend = _weekend_days(tenant)
        holidays = holiday_dates(first, last, tenant)
        names = {}
        for h in PublicHoliday.objects.filter(tenant=tenant, date__gte=first, date__lte=last):
            names[h.date] = h.get_name(lang)
        days = []
        current = first
        while current <= last:
            kind = 'holiday' if current in holidays else ('weekend' if current.weekday() in weekend else 'working')
            days.append({'date': str(current), 'kind': kind, 'holiday_name': names.get(current)})
            current += timedelta(days=1)

        leave_by_user = {}
        legend = {}
        for req in LeaveRequest.objects.filter(
            tenant=tenant, status='approved', employee__in=users,
            start_date__lte=last, end_date__gte=first,
        ).select_related('leave_type'):
            leave_by_user.setdefault(req.employee_id, []).append(req)
            lt = req.leave_type
            legend[lt.pk] = {'code': f'leave:{lt.pk}', 'leave_type': lt.pk,
                             'name': lt.get_name(lang), 'color': lt.color, 'is_paid': lt.is_paid}

        rows = []
        for u in users:
            start_emp, end_emp = employment_start(u), employment_end(u)
            cells = {}
            for day in days:
                d = date.fromisoformat(day['date'])
                if (start_emp and d < start_emp) or (end_emp and d > end_emp):
                    code = 'not_employed'
                elif day['kind'] != 'working':
                    code = 'off'
                else:
                    req = next((r for r in leave_by_user.get(u.pk, []) if r.start_date <= d <= r.end_date), None)
                    if req:
                        code = f'leave:{req.leave_type_id}'
                    elif d > today:
                        code = 'future'
                    else:
                        code = 'worked'
                cells[day['date']] = code
            rows.append({
                'user': {'id': u.pk, 'name': u.get_full_name() or u.email,
                         'department': u.department.name if u.department_id else None},
                'cells': cells,
            })
        return Response({'period': first.strftime('%Y-%m'), 'days': days, 'rows': rows,
                         'legend': list(legend.values())})
