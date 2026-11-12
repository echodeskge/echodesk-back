"""Leave onboarding step: admin gate, apply writes LeaveSettings / LeaveType /
org / holidays / balances, idempotent, context/prefill, seed_defaults, and the
monthly accrual beat task."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings as django_settings
from django.utils import timezone

from leave_management.models import LeaveBalance, LeaveSettings, LeaveType, PublicHoliday
from onboarding.models import TenantOnboarding
from onboarding.tests.helpers import enable_features
from users.models import Department
from users.tests.conftest import EchoDeskTenantTestCase

ONB = '/api/onboarding/'
APPLY = f'{ONB}leave_management/apply/'


class LeaveStepTests(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        enable_features(self.tenant, 'leave_management')
        self.owner = self.create_user(email='owner@test.com', role='admin', is_staff=True, is_superuser=True)
        self.manager = self.create_user(email='manager@test.com', role='manager', first_name='Mana')
        self.agent = self.create_user(email='agent@test.com', role='agent', first_name='Agi')

    def answers(self, **overrides):
        base = {
            'working_days_per_week': 5, 'weekend_days': [6, 5, 5],
            'auto_georgian_holidays': True,
            'leave_types': [
                {'code': 'Annual', 'name': {'ka': 'ყოველწლიური შვებულება', 'en': 'Annual leave'}, 'is_paid': True,
                 'calculation_method': 'accrual', 'days_per_year': 24, 'carry_forward_max_days': 5,
                 'carry_forward_expiry_months': 3, 'color': '#3b82f6'},
                {'code': 'sick', 'name': {'ka': 'საავადმყოფო ფურცელი', 'en': ''}, 'is_paid': True,
                 'calculation_method': 'manual', 'days_per_year': 0, 'color': '#EF4444'},
                {'code': 'bonus', 'name': {'ka': '', 'en': 'Bonus days'}, 'is_paid': True,
                 'calculation_method': 'annual', 'days_per_year': 3},
            ],
            'approval': 'both',
            'org': [
                {'user_id': self.agent.id, 'manager_id': self.manager.id, 'department': 'Support'},
                {'user_id': self.manager.id, 'manager_id': None, 'department': 'support'},
            ],
            'allow_negative_balance': True, 'max_negative_days': 3,
            'extras': {'substitutes': True, 'attendance_grid': True, 'kpi': False,
                       'payroll': {'enabled': True, 'mode': 'net', 'income_tax_rate': '20.00', 'pension_rate': '2.00',
                                   'currency': 'gel', 'manager_ids': [self.manager.id]}},
            'initialize_balances': True,
        }
        base.update(overrides)
        return base

    def _apply(self, data, user=None):
        return self.api_post(APPLY, {'answers': data}, user=user or self.owner)

    def test_non_admin_gets_403(self):
        self.assertEqual(self._apply(self.answers(), user=self.agent).status_code, 403)

    def test_apply_writes_everything(self):
        resp = self._apply(self.answers())
        self.assertEqual(resp.status_code, 200, resp.data)

        s = LeaveSettings.objects.get(tenant=self.tenant)
        self.assertEqual(s.working_days_per_week, 5)
        self.assertEqual(s.weekend_days, [5, 6])
        self.assertTrue(s.auto_georgian_holidays)
        self.assertTrue(s.require_manager_approval)
        self.assertTrue(s.require_hr_approval)
        self.assertTrue(s.allow_negative_balance)
        self.assertEqual(s.max_negative_days, 3)
        self.assertTrue(s.substitutes_enabled)
        self.assertTrue(s.attendance_grid_enabled)
        self.assertFalse(s.kpi_enabled)
        self.assertTrue(s.payroll_enabled)
        self.assertEqual(s.payroll_mode, 'net')
        self.assertEqual(s.income_tax_rate, Decimal('20.00'))
        self.assertEqual(s.payroll_currency, 'GEL')
        self.assertEqual(list(s.payroll_managers.values_list('id', flat=True)), [self.manager.id])

        # Holidays for this and next year, like the settings view.
        year = timezone.localdate().year
        self.assertTrue(PublicHoliday.objects.filter(date__year=year).exists())
        self.assertTrue(PublicHoliday.objects.filter(date__year=year + 1).exists())

        annual = LeaveType.objects.get(tenant=self.tenant, code='annual')
        self.assertEqual(annual.name, {'ka': 'ყოველწლიური შვებულება', 'en': 'Annual leave'})
        self.assertEqual(annual.calculation_method, 'accrual')
        self.assertEqual(annual.default_days_per_year, Decimal('24'))
        self.assertEqual(annual.accrual_rate_per_month, Decimal('2.00'))
        self.assertEqual(annual.max_carry_forward_days, 5)
        self.assertEqual(annual.carry_forward_expiry_months, 3)
        self.assertEqual(annual.color, '#3B82F6')
        self.assertEqual(annual.created_by, self.owner)
        sick = LeaveType.objects.get(tenant=self.tenant, code='sick')
        self.assertEqual(sick.name['en'], 'საავადმყოფო ფურცელი')   # missing language copies the other
        self.assertEqual(sick.max_carry_forward_days, 0)
        self.assertEqual(LeaveType.objects.filter(tenant=self.tenant).count(), 3)

        self.agent.refresh_from_db()
        self.manager.refresh_from_db()
        self.assertEqual(self.agent.manager, self.manager)
        self.assertEqual(self.agent.department.name, 'Support')
        self.assertEqual(self.manager.department, self.agent.department)   # case-insensitive match
        self.assertEqual(Department.objects.count(), 1)

        # Balances for every active user, annual + accrual types only.
        active_users = 3
        self.assertEqual(LeaveBalance.objects.filter(year=date.today().year).count(), active_users * 2)
        self.assertEqual(LeaveBalance.objects.get(user=self.agent, leave_type__code='bonus').allocated_days, Decimal('3'))

        created = resp.data['result']['created']
        self.assertEqual(created['leave_settings'], 1)
        self.assertEqual(created['leave_types'], 3)
        self.assertEqual(created['departments'], 1)
        self.assertEqual(created['leave_balances'], active_users * 2)
        self.assertGreater(created['holidays'], 0)
        self.assertEqual(TenantOnboarding.get().status_of('leave_management'), 'done')

    def test_apply_twice_creates_nothing_twice(self):
        self.assertEqual(self._apply(self.answers()).status_code, 200)
        holidays = PublicHoliday.objects.count()
        resp = self._apply(self.answers())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(LeaveSettings.objects.count(), 1)
        self.assertEqual(LeaveType.objects.count(), 3)
        self.assertEqual(Department.objects.count(), 1)
        self.assertEqual(PublicHoliday.objects.count(), holidays)
        self.assertEqual(LeaveBalance.objects.count(), 6)
        self.assertEqual(resp.data['result']['created'], {})
        self.assertEqual(resp.data['result']['updated'], [])

    def test_existing_type_matched_by_code_is_updated_not_duplicated(self):
        LeaveType.objects.create(
            tenant=self.tenant, code='ANNUAL', name={'en': 'Old'}, calculation_method='annual',
            default_days_per_year=Decimal('20'),
        )
        resp = self._apply(self.answers(leave_types=[
            {'code': 'annual', 'name': {'ka': 'შვებულება', 'en': 'Annual'}, 'calculation_method': 'annual', 'days_per_year': 24},
        ]))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(LeaveType.objects.count(), 1)
        t = LeaveType.objects.get()
        self.assertEqual(t.code, 'ANNUAL')
        self.assertEqual(t.default_days_per_year, Decimal('24'))
        self.assertEqual(t.name['en'], 'Annual')
        self.assertIn('leave_types', resp.data['result']['updated'])

    def test_validation(self):
        resp = self._apply({'leave_types': [{'code': 'x', 'name': {'ka': 'ა', 'en': 'A'}, 'calculation_method': 'annual', 'days_per_year': 0}]})
        self.assertEqual(resp.status_code, 400)
        resp = self._apply({'org': [{'user_id': self.agent.id, 'manager_id': self.agent.id}]})
        self.assertEqual(resp.status_code, 400)
        resp = self._apply({'org': [{'user_id': 999999}]})
        self.assertEqual(resp.status_code, 400)
        resp = self._apply({'working_days_per_week': 6, 'weekend_days': [5, 6]})
        self.assertEqual(resp.status_code, 400)

    def test_approval_manager_only(self):
        self.assertEqual(self._apply({'approval': 'hr'}).status_code, 200)
        s = LeaveSettings.objects.get(tenant=self.tenant)
        self.assertFalse(s.require_manager_approval)
        self.assertTrue(s.require_hr_approval)

    def test_context_and_prefill(self):
        step = next(s for s in self.api_get(ONB, user=self.owner).data['steps'] if s['key'] == 'leave_management')
        self.assertEqual(step['order'], 60)
        ctx = step['context']
        self.assertEqual([u['email'] for u in ctx['users']], ['agent@test.com', 'manager@test.com', 'owner@test.com'])
        self.assertEqual(ctx['users'][0]['manager_id'], None)
        self.assertEqual(ctx['users'][0]['department'], None)
        self.assertEqual(ctx['departments'], [])
        self.assertEqual([p['code'] for p in ctx['type_presets']], ['annual', 'sick', 'unpaid', 'maternity', 'study'])
        annual = ctx['type_presets'][0]
        self.assertEqual(annual['days_per_year'], 24)
        self.assertEqual(annual['calculation_method'], 'accrual')
        self.assertTrue(all(p['name']['ka'] and p['name']['en'] for p in ctx['type_presets']))
        self.assertEqual(ctx['existing_types'], 0)
        self.assertFalse(ctx['has_settings'])
        pre = step['prefill']
        self.assertEqual(pre['weekend_days'], [5, 6])
        self.assertEqual(pre['approval'], 'manager')
        self.assertEqual(pre['leave_types'], ctx['type_presets'])
        self.assertEqual(len(pre['org']), 3)

        self._apply(self.answers())
        step = next(s for s in self.api_get(ONB, user=self.owner).data['steps'] if s['key'] == 'leave_management')
        self.assertEqual(step['context']['existing_types'], 3)
        agent = next(u for u in step['context']['users'] if u['id'] == self.agent.id)
        self.assertEqual(agent['manager_id'], self.manager.id)
        self.assertEqual(agent['department'], 'Support')
        self.assertEqual(step['prefill']['approval'], 'both')
        self.assertEqual(step['prefill']['leave_types'][0]['code'], 'annual')
        self.assertEqual(step['prefill']['extras']['payroll']['manager_ids'], [self.manager.id])

    def test_prefill_weekend_from_another_steps_hours(self):
        state = TenantOnboarding.get()
        state.set_status('booking_management', 'done', answers={'hours': {
            'mon': {'from': '09:00', 'to': '18:00'}, 'tue': {'from': '09:00', 'to': '18:00'},
            'wed': {'from': '09:00', 'to': '18:00'}, 'thu': {'from': '09:00', 'to': '18:00'},
            'fri': {'from': '09:00', 'to': '15:00'}, 'sat': {'from': '10:00', 'to': '14:00'}, 'sun': None,
        }})
        step = next(s for s in self.api_get(ONB, user=self.owner).data['steps'] if s['key'] == 'leave_management')
        self.assertEqual(step['prefill']['weekend_days'], [6])
        self.assertEqual(step['prefill']['working_days_per_week'], 6)

    def test_seed_defaults(self):
        from leave_management.onboarding import LeaveStep
        LeaveStep().seed_defaults(self.tenant)
        LeaveStep().seed_defaults(self.tenant)
        self.assertEqual(LeaveSettings.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(LeaveSettings.objects.get(tenant=self.tenant).weekend_days, [5, 6])
        self.assertEqual(
            list(LeaveType.objects.filter(tenant=self.tenant).order_by('sort_order').values_list('code', flat=True)),
            ['annual', 'sick', 'unpaid', 'maternity', 'study'],
        )
        self.assertEqual(LeaveType.objects.get(code='annual').accrual_rate_per_month, Decimal('2.00'))


class AccrualBeatTests(EchoDeskTenantTestCase):

    def test_beat_entry_present(self):
        entry = django_settings.CELERY_BEAT_SCHEDULE['leave-process-accruals']
        self.assertEqual(entry['task'], 'leave_management.tasks.process_leave_accruals')
        self.assertEqual(str(entry['schedule'].day_of_month), '{1}')

    def test_task_accrues_for_active_users_of_tenants_with_accrual_types(self):
        from leave_management.tasks import process_leave_accruals

        user = self.create_user(email='accrue@test.com')
        inactive = self.create_user(email='gone@test.com', is_active=False)
        annual = LeaveType.objects.create(
            tenant=self.tenant, code='annual', name={'en': 'Annual'}, calculation_method='accrual',
            default_days_per_year=Decimal('24'), accrual_rate_per_month=Decimal('2.00'),
        )
        LeaveType.objects.create(
            tenant=self.tenant, code='sick', name={'en': 'Sick'}, calculation_method='manual',
        )
        today = date.today()
        LeaveBalance.objects.create(
            tenant=self.tenant, user=user, leave_type=annual, year=today.year,
            allocated_days=Decimal('0'), last_accrual_date=date(today.year, 1, 1),
        )
        with patch('leave_management.utils.date') as mock_date:
            mock_date.today.return_value = date(today.year, 4, 1)
            mock_date.side_effect = lambda *a, **k: date(*a, **k)
            total = process_leave_accruals()
        self.assertEqual(Decimal(total), Decimal('6.00'))   # Jan → Apr = 3 months × 2 days
        balance = LeaveBalance.objects.get(user=user, leave_type=annual)
        self.assertEqual(balance.allocated_days, Decimal('6.00'))
        self.assertEqual(balance.last_accrual_date, date(today.year, 4, 1))
        self.assertFalse(LeaveBalance.objects.filter(user=inactive).exists())

        # Running again the same month accrues nothing more.
        with patch('leave_management.utils.date') as mock_date:
            mock_date.today.return_value = date(today.year, 4, 1)
            mock_date.side_effect = lambda *a, **k: date(*a, **k)
            self.assertEqual(Decimal(process_leave_accruals()), Decimal('0'))
