"""
Leave / HR step (key ``leave_management``, order 60).

Answers schema (every key optional; only the keys present are applied)::

    {
      "working_days_per_week": int (1-7), "weekend_days": [int],      # 0 = Monday
      "auto_georgian_holidays": bool,
      "leave_types": [{"code": str, "name": {"ka": str, "en": str}, "is_paid": bool,
                       "calculation_method": "annual" | "accrual" | "manual",
                       "days_per_year": int, "carry_forward_max_days": int | null,
                       "carry_forward_expiry_months": int | null, "color": "#RRGGBB"}],
      "approval": "manager" | "hr" | "both",
      "org": [{"user_id": int, "manager_id": int | null, "department": str | null}],
      "allow_negative_balance": bool, "max_negative_days": int,
      "extras": {"substitutes": bool, "attendance_grid": bool, "kpi": bool,
                 "payroll": {"enabled": bool, "mode": "gross" | "net", "income_tax_rate": "20.00",
                             "pension_rate": "2.00", "currency": "GEL", "manager_ids": [int]}},
      "initialize_balances": bool
    }

``apply`` writes the tenant's ``LeaveSettings`` row (created when missing):
work week, ``auto_georgian_holidays`` (when on, this and next year's Georgian
holidays are loaded the way the settings view does), ``approval`` → the two
``require_*_approval`` flags, negative balance rules, the HR toggles and the
payroll block (``payroll_managers`` is set to ``manager_ids``). ``leave_types``
become ``LeaveType`` rows matched by ``code`` (case-insensitive; existing rows
are updated, never deleted; accrual types get ``accrual_rate_per_month`` =
days / 12). ``org`` sets ``User.manager`` / ``User.department`` (departments
are get-or-created by name). ``initialize_balances`` runs the same logic as
``leave-balances/initialize_user`` for every active user. Idempotent.

``context``: ``users`` (id, email, name, role, is_active, manager_id,
department), ``departments``, ``type_presets`` (Annual / Sick / Unpaid /
Maternity / Study with Georgian names), ``existing_types``, ``has_settings``.
``seed_defaults``: the settings row (Mon–Fri week) and the five preset leave
types, so the module works for a tenant that skipped the wizard.

Also in this package: ``leave_management.tasks.process_leave_accruals`` runs
monthly from Celery beat (``process_accrual`` was never scheduled).
"""
import re
from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from onboarding.base import OnboardingStep, empty_result
from onboarding.registry import register
from users.models import Department

from .models import LeaveSettings, LeaveType

User = get_user_model()

LANGUAGES = ['ka', 'en']
CALCULATION_METHODS = [key for key, _ in LeaveType.CALCULATION_METHODS]
APPROVAL_CHOICES = ['manager', 'hr', 'both']
PAYROLL_MODES = [key for key, _ in LeaveSettings.PAYROLL_MODES]
HEX_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')

TYPE_PRESETS = [
    {
        'code': 'annual', 'name': {'ka': 'ყოველწლიური შვებულება', 'en': 'Annual leave'},
        'is_paid': True, 'calculation_method': 'accrual', 'days_per_year': 24,
        'carry_forward_max_days': 5, 'carry_forward_expiry_months': 3, 'color': '#3B82F6',
    },
    {
        'code': 'sick', 'name': {'ka': 'საავადმყოფო ფურცელი', 'en': 'Sick leave'},
        'is_paid': True, 'calculation_method': 'manual', 'days_per_year': 0,
        'carry_forward_max_days': None, 'carry_forward_expiry_months': None, 'color': '#EF4444',
    },
    {
        'code': 'unpaid', 'name': {'ka': 'ანაზღაურების გარეშე შვებულება', 'en': 'Unpaid leave'},
        'is_paid': False, 'calculation_method': 'manual', 'days_per_year': 0,
        'carry_forward_max_days': None, 'carry_forward_expiry_months': None, 'color': '#6B7280',
    },
    {
        'code': 'maternity', 'name': {'ka': 'დეკრეტული შვებულება', 'en': 'Maternity leave'},
        'is_paid': True, 'calculation_method': 'manual', 'days_per_year': 0,
        'carry_forward_max_days': None, 'carry_forward_expiry_months': None, 'color': '#EC4899',
    },
    {
        'code': 'study', 'name': {'ka': 'სასწავლო შვებულება', 'en': 'Study leave'},
        'is_paid': True, 'calculation_method': 'manual', 'days_per_year': 0,
        'carry_forward_max_days': None, 'carry_forward_expiry_months': None, 'color': '#8B5CF6',
    },
]

# ``Hours`` day keys (other steps' answers) → weekday index, 0 = Monday.
HOURS_DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


def _clean(value):
    return ' '.join(str(value or '').split())


class I18nField(serializers.DictField):
    child = serializers.CharField(allow_blank=True, max_length=255)

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        value = {lang: _clean(data.get(lang)) for lang in LANGUAGES}
        if not any(value.values()):
            raise serializers.ValidationError('A name in at least one language is required.')
        if not value['en']:
            value['en'] = value['ka']
        if not value['ka']:
            value['ka'] = value['en']
        return value


class LeaveTypeAnswerSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=20)
    name = I18nField()
    is_paid = serializers.BooleanField(required=False, default=True)
    calculation_method = serializers.ChoiceField(choices=CALCULATION_METHODS, required=False, default='annual')
    days_per_year = serializers.IntegerField(min_value=0, max_value=366, required=False, default=0)
    carry_forward_max_days = serializers.IntegerField(min_value=0, max_value=366, required=False, allow_null=True, default=None)
    carry_forward_expiry_months = serializers.IntegerField(min_value=0, max_value=36, required=False, allow_null=True, default=None)
    color = serializers.CharField(max_length=7, required=False, default='#3B82F6')

    def validate_code(self, value):
        value = _clean(value).lower().replace(' ', '_')
        if not re.match(r'^[a-z0-9_\-]+$', value):
            raise serializers.ValidationError('Use letters, digits, "_" or "-" only.')
        return value

    def validate_color(self, value):
        value = _clean(value) or '#3B82F6'
        if not HEX_COLOR.match(value):
            raise serializers.ValidationError('Use a hex colour like #3B82F6.')
        return value.upper()

    def validate(self, attrs):
        if attrs.get('calculation_method') in ('annual', 'accrual') and attrs.get('days_per_year', 0) <= 0:
            raise serializers.ValidationError({'days_per_year': 'Must be greater than 0 for annual / accrual leave.'})
        return attrs


class OrgRowSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    manager_id = serializers.IntegerField(required=False, allow_null=True)
    department = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)

    def validate_department(self, value):
        return _clean(value) or None

    def validate(self, attrs):
        if attrs.get('manager_id') is not None and attrs['manager_id'] == attrs['user_id']:
            raise serializers.ValidationError({'manager_id': 'A user cannot be their own manager.'})
        return attrs


class PayrollSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
    mode = serializers.ChoiceField(choices=PAYROLL_MODES, required=False)
    income_tax_rate = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'), required=False)
    pension_rate = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'), required=False)
    currency = serializers.CharField(max_length=3, required=False)
    manager_ids = serializers.ListField(child=serializers.IntegerField(), required=False)

    def validate_currency(self, value):
        value = (value or '').upper()
        if len(value) != 3 or not value.isalpha():
            raise serializers.ValidationError('Use a 3-letter currency code, e.g. GEL')
        return value


class ExtrasSerializer(serializers.Serializer):
    substitutes = serializers.BooleanField(required=False)
    attendance_grid = serializers.BooleanField(required=False)
    kpi = serializers.BooleanField(required=False)
    payroll = PayrollSerializer(required=False)


class LeaveAnswersSerializer(serializers.Serializer):
    working_days_per_week = serializers.IntegerField(min_value=1, max_value=7, required=False)
    weekend_days = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=6), required=False)
    auto_georgian_holidays = serializers.BooleanField(required=False)
    leave_types = LeaveTypeAnswerSerializer(many=True, required=False)
    approval = serializers.ChoiceField(choices=APPROVAL_CHOICES, required=False)
    org = OrgRowSerializer(many=True, required=False)
    allow_negative_balance = serializers.BooleanField(required=False)
    max_negative_days = serializers.IntegerField(min_value=0, max_value=366, required=False)
    extras = ExtrasSerializer(required=False)
    initialize_balances = serializers.BooleanField(required=False)

    def validate_weekend_days(self, value):
        return sorted(set(value))

    def validate_leave_types(self, value):
        seen = set()
        for row in value:
            if row['code'] in seen:
                raise serializers.ValidationError(f'Leave type code "{row["code"]}" is listed more than once.')
            seen.add(row['code'])
        return value

    def validate_org(self, value):
        ids = {row['user_id'] for row in value} | {row['manager_id'] for row in value if row.get('manager_id')}
        known = set(User.objects.filter(id__in=ids).values_list('id', flat=True))
        missing = sorted(ids - known)
        if missing:
            raise serializers.ValidationError(f'Unknown user id(s): {", ".join(map(str, missing))}.')
        return value

    def validate(self, attrs):
        if 'working_days_per_week' in attrs and 'weekend_days' in attrs:
            if attrs['working_days_per_week'] + len(attrs['weekend_days']) > 7:
                raise serializers.ValidationError({'weekend_days': 'Working days plus weekend days cannot exceed 7.'})
        return attrs


def _set(instance, field, value, changed):
    if getattr(instance, field) != value:
        setattr(instance, field, value)
        changed.append(field)


def _accrual_rate(days):
    return (Decimal(days) / Decimal(12)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _user_payload(u):
    return {
        'id': u.id,
        'email': u.email,
        'name': u.get_full_name(),
        'role': u.role,
        'is_active': u.is_active,
        'manager_id': u.manager_id,
        'department': u.department.name if u.department_id else None,
    }


def _weekend_from_hours(hours):
    """Closed days of an ``Hours`` answer → weekend day indexes."""
    if not isinstance(hours, dict):
        return None
    closed = [i for i, day in enumerate(HOURS_DAYS) if not hours.get(day)]
    if len(closed) == 7:
        return None
    return closed


@register
class LeaveStep(OnboardingStep):
    key = 'leave_management'
    feature_key = 'leave_management'
    order = 60
    serializer_class = LeaveAnswersSerializer

    # ── reads ──

    def context(self, tenant, request):
        settings_obj = LeaveSettings.objects.filter(tenant=tenant).first()
        users = [
            _user_payload(u)
            for u in User.objects.select_related('department').order_by('email')
        ]
        return {
            'users': users,
            'departments': [{'id': d.id, 'name': d.name} for d in Department.objects.order_by('name')],
            'type_presets': [dict(p, name=dict(p['name'])) for p in TYPE_PRESETS],
            'existing_types': LeaveType.objects.filter(tenant=tenant).count(),
            'has_settings': settings_obj is not None,
            'payroll_manager_ids': list(settings_obj.payroll_managers.values_list('id', flat=True)) if settings_obj else [],
        }

    def prefill(self, tenant, state):
        s = LeaveSettings.objects.filter(tenant=tenant).first()
        weekend = list(s.weekend_days) if s and s.weekend_days else None
        if weekend is None and state is not None:
            for step_key in ('booking_management', 'ip_calling', 'social_integrations'):
                answers = state.answers_of(step_key) or {}
                hours = answers.get('hours')
                if isinstance(hours, dict) and 'schedule' in hours:
                    hours = hours['schedule']
                weekend = _weekend_from_hours(hours)
                if weekend is not None:
                    break
        if weekend is None:
            weekend = [5, 6]

        types = LeaveType.objects.filter(tenant=tenant, is_active=True).order_by('sort_order', 'id')
        if types.exists():
            leave_types = [
                {
                    'code': t.code,
                    'name': {lang: (t.name or {}).get(lang, '') for lang in LANGUAGES} if isinstance(t.name, dict) else {'ka': str(t.name), 'en': str(t.name)},
                    'is_paid': t.is_paid,
                    'calculation_method': t.calculation_method,
                    'days_per_year': int(t.default_days_per_year or 0),
                    'carry_forward_max_days': t.max_carry_forward_days or None,
                    'carry_forward_expiry_months': t.carry_forward_expiry_months or None,
                    'color': t.color,
                }
                for t in types
            ]
        else:
            leave_types = [dict(p, name=dict(p['name'])) for p in TYPE_PRESETS]

        if s is None:
            approval = 'manager'
        elif s.require_manager_approval and s.require_hr_approval:
            approval = 'both'
        elif s.require_hr_approval:
            approval = 'hr'
        else:
            approval = 'manager'

        org = [
            {'user_id': u.id, 'manager_id': u.manager_id, 'department': u.department.name if u.department_id else None}
            for u in User.objects.filter(is_active=True).select_related('department').order_by('email')
        ]
        return {
            'working_days_per_week': s.working_days_per_week if s else max(1, 7 - len(weekend)),
            'weekend_days': weekend,
            'auto_georgian_holidays': s.auto_georgian_holidays if s else True,
            'leave_types': leave_types,
            'approval': approval,
            'org': org,
            'allow_negative_balance': s.allow_negative_balance if s else False,
            'max_negative_days': s.max_negative_days if s else 0,
            'extras': {
                'substitutes': s.substitutes_enabled if s else False,
                'attendance_grid': s.attendance_grid_enabled if s else False,
                'kpi': s.kpi_enabled if s else False,
                'payroll': {
                    'enabled': s.payroll_enabled if s else False,
                    'mode': s.payroll_mode if s else 'gross',
                    'income_tax_rate': str(s.income_tax_rate if s else Decimal('20.00')),
                    'pension_rate': str(s.pension_rate if s else Decimal('2.00')),
                    'currency': s.payroll_currency if s else 'GEL',
                    'manager_ids': list(s.payroll_managers.values_list('id', flat=True)) if s else [],
                },
            },
            'initialize_balances': True,
        }

    # ── writes ──

    def apply(self, tenant, answers, user):
        result = empty_result()
        changed = []
        settings_obj, created = LeaveSettings.objects.get_or_create(tenant=tenant, defaults={'weekend_days': [5, 6]})
        if created:
            result['created']['leave_settings'] = 1

        for field in ('working_days_per_week', 'weekend_days', 'allow_negative_balance', 'max_negative_days'):
            if field in answers:
                _set(settings_obj, field, answers[field], changed)

        approval = answers.get('approval')
        if approval:
            _set(settings_obj, 'require_manager_approval', approval in ('manager', 'both'), changed)
            _set(settings_obj, 'require_hr_approval', approval in ('hr', 'both'), changed)

        extras = answers.get('extras', {})
        if 'substitutes' in extras:
            _set(settings_obj, 'substitutes_enabled', extras['substitutes'], changed)
        if 'attendance_grid' in extras:
            _set(settings_obj, 'attendance_grid_enabled', extras['attendance_grid'], changed)
        if 'kpi' in extras:
            _set(settings_obj, 'kpi_enabled', extras['kpi'], changed)
        payroll = extras.get('payroll', {})
        if 'enabled' in payroll:
            _set(settings_obj, 'payroll_enabled', payroll['enabled'], changed)
        if 'mode' in payroll:
            _set(settings_obj, 'payroll_mode', payroll['mode'], changed)
        if 'income_tax_rate' in payroll:
            _set(settings_obj, 'income_tax_rate', payroll['income_tax_rate'], changed)
        if 'pension_rate' in payroll:
            _set(settings_obj, 'pension_rate', payroll['pension_rate'], changed)
        if 'currency' in payroll:
            _set(settings_obj, 'payroll_currency', payroll['currency'], changed)

        holidays_on = False
        if 'auto_georgian_holidays' in answers:
            _set(settings_obj, 'auto_georgian_holidays', answers['auto_georgian_holidays'], changed)
            holidays_on = answers['auto_georgian_holidays']

        if created or changed:
            settings_obj.save()

        if 'manager_ids' in payroll:
            wanted = set(User.objects.filter(id__in=payroll['manager_ids']).values_list('id', flat=True))
            current = set(settings_obj.payroll_managers.values_list('id', flat=True))
            if wanted != current:
                settings_obj.payroll_managers.set(wanted)
                changed.append('payroll_managers')

        if holidays_on:
            self._load_holidays(tenant, user, result)

        if 'leave_types' in answers:
            self._apply_leave_types(tenant, answers['leave_types'], user, changed, result)

        if 'org' in answers:
            self._apply_org(answers['org'], changed, result)

        if answers.get('initialize_balances'):
            from .utils import initialize_leave_balances_for_user
            total = 0
            for member in User.objects.filter(is_active=True).order_by('id'):
                total += initialize_leave_balances_for_user(member, tenant)
            if total:
                result['created']['leave_balances'] = total

        result['updated'] = changed
        return result

    def seed_defaults(self, tenant):
        LeaveSettings.objects.get_or_create(tenant=tenant, defaults={'weekend_days': [5, 6]})
        if not LeaveType.objects.filter(tenant=tenant).exists():
            for index, preset in enumerate(TYPE_PRESETS):
                LeaveType.objects.create(tenant=tenant, **self._type_fields(preset), sort_order=index)

    # ── helpers ──

    @staticmethod
    def _type_fields(row):
        days = Decimal(row.get('days_per_year') or 0)
        method = row.get('calculation_method') or 'annual'
        return {
            'code': row['code'],
            'name': dict(row['name']),
            'is_paid': row.get('is_paid', True),
            'calculation_method': method,
            'default_days_per_year': days,
            'accrual_rate_per_month': _accrual_rate(days) if method == 'accrual' else Decimal('0'),
            'max_carry_forward_days': row.get('carry_forward_max_days') or 0,
            'carry_forward_expiry_months': row.get('carry_forward_expiry_months') or 0,
            'color': row.get('color') or '#3B82F6',
            'is_active': True,
        }

    def _apply_leave_types(self, tenant, rows, user, changed, result):
        existing = {t.code.lower(): t for t in LeaveType.objects.filter(tenant=tenant)}
        created_count = 0
        next_order = max((t.sort_order for t in existing.values()), default=-1) + 1
        for row in rows:
            fields = self._type_fields(row)
            match = existing.get(row['code'].lower())
            if match is None:
                LeaveType.objects.create(tenant=tenant, created_by=user, sort_order=next_order, **fields)
                next_order += 1
                created_count += 1
                continue
            updates = []
            fields.pop('code')
            for field, value in fields.items():
                if getattr(match, field) != value:
                    setattr(match, field, value)
                    updates.append(field)
            if updates:
                match.updated_by = user
                match.save(update_fields=updates + ['updated_by', 'updated_at'])
                if 'leave_types' not in changed:
                    changed.append('leave_types')
        if created_count:
            result['created']['leave_types'] = created_count

    @staticmethod
    def _apply_org(rows, changed, result):
        users = {u.id: u for u in User.objects.filter(id__in=[r['user_id'] for r in rows] + [r['manager_id'] for r in rows if r.get('manager_id')])}
        departments = {d.name.lower(): d for d in Department.objects.all()}
        created_departments = 0
        touched = False
        for row in rows:
            member = users.get(row['user_id'])
            if member is None:
                continue
            updates = []
            if 'manager_id' in row:
                manager = users.get(row['manager_id']) if row['manager_id'] else None
                if member.manager_id != (manager.id if manager else None):
                    member.manager = manager
                    updates.append('manager')
            if 'department' in row:
                dept = None
                if row['department']:
                    dept = departments.get(row['department'].lower())
                    if dept is None:
                        dept = Department.objects.create(name=row['department'])
                        departments[dept.name.lower()] = dept
                        created_departments += 1
                if member.department_id != (dept.id if dept else None):
                    member.department = dept
                    updates.append('department')
            if updates:
                member.save(update_fields=updates)
                touched = True
        if touched:
            changed.append('org')
        if created_departments:
            result['created']['departments'] = created_departments

    @staticmethod
    def _load_holidays(tenant, user, result):
        """This and next year's Georgian holidays, like the settings view does."""
        from .audit import log_event
        from .holidays_ge import load_georgian_holidays

        year = timezone.localdate().year
        total = 0
        for y in (year, year + 1):
            created, _ = load_georgian_holidays(tenant, y, user)
            if created:
                total += len(created)
                log_event(
                    tenant, 'holiday', 'georgian_loaded', actor=user,
                    object_id=y, data={'year': y, 'created': [str(d) for d in created]},
                )
        if total:
            result['created']['holidays'] = total
