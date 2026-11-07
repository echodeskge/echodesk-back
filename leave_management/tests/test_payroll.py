"""B4: payroll report and substitution pay (ported from INSERVICE tests/rules.mjs + access.mjs)."""
from datetime import date
from decimal import Decimal

from rest_framework import status

from leave_management.holidays_ge import load_georgian_holidays
from leave_management.models import (
    EmployeeCompensation, LeaveRequest, LeaveRequestSubstitute, SubstituteAssignment,
    SubstitutionRate, PayrollRun, LeaveAuditEvent,
)
from leave_management.payroll import build_report, set_worked_days, WorkedDaysError, csv_bytes
from leave_management.utils import working_dates
from leave_management.tests.hr_base import HrTestCase

TODAY = date(2026, 10, 30)
P = '/api/leave/payroll/'


class PayrollBase(HrTestCase):

    def setUp(self):
        super().setUp()
        self.settings = self.settings_with(payroll_enabled=True, substitutes_enabled=True,
                                           substitution_pay_enabled=True)
        load_georgian_holidays(self.tenant, 2026)
        load_georgian_holidays(self.tenant, 2027)
        self.admin = self.create_admin(email='pay-admin@test.com')
        self.emp = self.create_user(email='pay-emp@test.com', employment_start_date=date(2026, 1, 1))
        EmployeeCompensation.objects.create(
            tenant=self.tenant, user=self.emp, monthly_salary=Decimal('2200'), effective_from=date(2026, 1, 1),
        )
        self.paid = self.create_leave_type(code='LEAVE', is_paid=True)
        self.dayoff = self.create_leave_type(code='OFF', is_paid=True)
        self.unpaid = self.create_leave_type(code='UNPAID', is_paid=False)

    def approved(self, user, lt, start, end):
        return self.create_leave_request(user, lt, start_date=start, end_date=end,
                                         total_days=Decimal('1'), status='approved')

    def row(self, period='2026-10', today=TODAY):
        report = build_report(self.tenant, period, today=today)
        return next(r for r in report['rows'] if r['user'] == self.emp.pk)


class TestRulesPort(PayrollBase):

    def test_calendar(self):
        self.assertEqual(len(working_dates(date(2026, 10, 1), date(2026, 10, 31), self.tenant)), 21)
        self.assertEqual(working_dates(date(2026, 10, 31), date(2026, 10, 31), self.tenant), [])
        self.assertEqual(working_dates(date(2026, 10, 14), date(2026, 10, 14), self.tenant), [])
        self.assertEqual(working_dates(date(2026, 11, 2), date(2026, 11, 1), self.tenant), [])
        self.assertEqual(len(working_dates(date(2026, 12, 31), date(2027, 1, 4), self.tenant)), 2)
        self.assertEqual(working_dates(date(2026, 4, 14), date(2026, 4, 14), self.tenant), [date(2026, 4, 14)])

    def test_no_absences(self):
        r = self.row()
        self.assertEqual(r['gross'], '2200.00')
        self.assertEqual(r['worked_to_date'], 20)
        self.assertEqual(r['working_days'], 21)

    def test_day_off_is_paid(self):
        self.approved(self.emp, self.dayoff, date(2026, 10, 15), date(2026, 10, 15))
        r = self.row()
        self.assertEqual((r['gross'], r['unpaid_days'], r['worked_to_date']), ('2200.00', 0, 19))

    def test_unpaid_reduces_pay(self):
        self.approved(self.emp, self.unpaid, date(2026, 10, 15), date(2026, 10, 15))
        r = self.row()
        self.assertEqual(r['gross'], '2095.24')  # 2200 * 20 / 21
        self.assertEqual((r['unpaid_days'], r['worked_to_date']), (1, 19))

    def test_paid_leave(self):
        self.approved(self.emp, self.paid, date(2026, 10, 15), date(2026, 10, 16))
        r = self.row()
        self.assertEqual((r['gross'], r['paid_leave_days'], r['worked_to_date']), ('2200.00', 2, 18))

    def test_start_mid_month(self):
        self.emp.employment_start_date = date(2026, 10, 30)
        self.emp.save()
        self.assertEqual(self.row()['gross'], '104.76')  # 2200 / 21

    def test_future_month_and_end_date(self):
        self.assertEqual(self.row('2026-11')['worked_to_date'], 0)
        self.emp.employment_end_date = date(2026, 10, 15)
        self.emp.save()
        r = self.row()
        self.assertEqual(r['scheduled_days'], 10)  # Oct 1-15 minus weekends and the 14th
        self.assertEqual(r['gross'], str((Decimal('2200') * 10 / 21).quantize(Decimal('0.01'))))

    def test_salary_history_and_net_mode(self):
        EmployeeCompensation.objects.create(
            tenant=self.tenant, user=self.emp, monthly_salary=Decimal('3000'), effective_from=date(2026, 10, 1),
        )
        self.assertEqual(self.row()['gross'], '3000.00')
        self.settings_with(payroll_mode='net')
        r = self.row()
        self.assertEqual(r['pension'], '60.00')
        self.assertEqual(r['income_tax'], '588.00')  # (3000 - 60) * 20%
        self.assertEqual(r['net'], '2352.00')

    def test_csv(self):
        data = csv_bytes(build_report(self.tenant, '2026-10', today=TODAY), 'ka')
        self.assertTrue(data.startswith('﻿'.encode('utf-8')))
        text = data.decode('utf-8-sig')
        self.assertIn('\r\n', text)
        self.assertTrue(text.startswith('"თანამშრომელი"'))


class TestSubstitutionPay(PayrollBase):

    def setUp(self):
        super().setUp()
        self.b = self.create_user(email='pay-b@test.com', employment_start_date=date(2026, 1, 1))
        self.n = self.create_user(email='pay-n@test.com', employment_start_date=date(2026, 1, 1))
        self.as_b = SubstituteAssignment.objects.create(tenant=self.tenant, owner=self.emp, substitute=self.b, task='Clients')
        self.as_n = SubstituteAssignment.objects.create(tenant=self.tenant, owner=self.emp, substitute=self.n, task='Operations')
        self.req = self.approved(self.emp, self.paid, date(2026, 10, 15), date(2026, 10, 16))
        self.row_b = LeaveRequestSubstitute.objects.create(leave_request=self.req, substitute=self.b, task='Clients', status='accepted')
        self.row_n = LeaveRequestSubstitute.objects.create(leave_request=self.req, substitute=self.n, task='Operations', status='accepted')

    def set_rate(self, assignment, mode, amount, user=None):
        return self.api_put(f'{P}substitution-rates/{assignment.id}/rate/',
                            {'mode': mode, 'amount': amount}, user=user or self.admin)

    def test_rates_backfill_and_totals(self):
        self.assertEqual(self.set_rate(self.as_b, 'day', '40').status_code, 200)
        self.assertEqual(self.set_rate(self.as_n, 'month', '420').status_code, 200)
        self.row_b.refresh_from_db()
        self.assertEqual(self.row_b.rate_amount, Decimal('40'))
        # a later change does not touch the snapshot
        self.set_rate(self.as_b, 'day', '90')
        self.row_b.refresh_from_db()
        self.assertEqual(self.row_b.rate_amount, Decimal('40'))

        self.row_n.refresh_from_db()
        set_worked_days(self.row_b, ['2026-10-15', '2026-10-16'], self.admin, self.tenant, TODAY)
        set_worked_days(self.row_n, ['2026-10-16'], self.admin, self.tenant, TODAY)
        self.assertEqual(self.row_b.totals_by_month, {'2026-10': '80.00'})
        self.assertEqual(self.row_n.totals_by_month, {'2026-10': '20.00'})
        with self.assertRaises(WorkedDaysError):
            set_worked_days(self.row_n, ['2026-10-17'], self.admin, self.tenant, TODAY)

        # substitution pay shows on the substitute's payroll row
        EmployeeCompensation.objects.create(tenant=self.tenant, user=self.b, monthly_salary=Decimal('1000'),
                                            effective_from=date(2026, 1, 1))
        report = build_report(self.tenant, '2026-10', today=TODAY)
        row_b = next(r for r in report['rows'] if r['user'] == self.b.pk)
        self.assertEqual(row_b['substitution_pay'], '80.00')
        self.assertEqual(row_b['gross'], '1080.00')
        row_n = next(r for r in report['rows'] if r['user'] == self.n.pk)
        self.assertEqual(row_n['gross'], '20.00')

    def test_worked_days_rules(self):
        self.set_rate(self.as_b, 'day', '40')
        # substitute away on approved leave that day
        self.approved(self.b, self.dayoff, date(2026, 10, 16), date(2026, 10, 16))
        self.row_b.refresh_from_db()
        with self.assertRaises(WorkedDaysError):
            set_worked_days(self.row_b, ['2026-10-16'], self.admin, self.tenant, TODAY)
        # future days refused
        with self.assertRaises(WorkedDaysError):
            set_worked_days(self.row_b, ['2026-10-15'], self.admin, self.tenant, date(2026, 10, 14))
        # no rate yet
        with self.assertRaises(WorkedDaysError):
            set_worked_days(self.row_n, ['2026-10-15'], self.admin, self.tenant, TODAY)

    def test_api_worked_days_and_lock(self):
        self.set_rate(self.as_b, 'day', '40')
        url = f'{P}coverage/{self.row_b.id}/worked-days/'
        resp = self.api_post(url, {'days': ['2026-10-15']}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['totals_by_month'], {'2026-10': '40.00'})
        lock = self.api_post(f'{P}runs/lock/', {'period': '2026-10'}, user=self.admin)
        self.assertEqual(lock.status_code, 200, lock.data)
        refused = self.api_post(url, {'days': ['2026-10-15', '2026-10-16']}, user=self.admin)
        self.assertEqual(refused.status_code, 400)
        self.assertIn('locked', refused.data['error'])
        # locked report is served from the snapshot even after salary edits are attempted
        comp = self.api_post(f'{P}compensations/', {'user': self.emp.id, 'monthly_salary': '9999',
                                                    'effective_from': '2026-10-01'}, user=self.admin)
        self.assertEqual(comp.status_code, 400)
        report = self.api_get(f'{P}report/?period=2026-10', user=self.admin)
        self.assertTrue(report.data['locked'])
        run = PayrollRun.objects.get(period='2026-10')
        self.assertEqual(self.api_post(f'{P}runs/{run.id}/unlock/', {}, user=self.admin).status_code, 400)
        self.assertEqual(self.api_post(f'{P}runs/{run.id}/unlock/', {'reason': 'fix'}, user=self.admin).status_code, 200)
        self.assertEqual(self.api_post(url, {'days': ['2026-10-15', '2026-10-16']}, user=self.admin).status_code, 200)


class TestPayrollAccess(PayrollBase):

    def test_permissions_and_salary_privacy(self):
        agent = self.create_user(email='pay-agent@test.com')
        self.assertEqual(self.api_get(f'{P}report/?period=2026-10', user=agent).status_code, 403)
        self.assertEqual(self.api_get(f'{P}compensations/', user=agent).status_code, 403)
        self.settings.payroll_managers.add(agent)
        resp = self.api_get(f'{P}report/?period=2026-10', user=agent)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(LeaveAuditEvent.objects.filter(kind='payroll', action='viewed').exists())
        users = self.api_get('/api/users/', user=self.admin)
        self.assertNotIn('salary', str(users.data).lower())

    def test_export_and_self_view(self):
        resp = self.api_get(f'{P}report/export/?period=2026-10&lang=en', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/csv; charset=utf-8')
        self.assertTrue(resp.content.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(self.api_get(f'{P}my/?period=2026-10', user=self.emp).status_code, 403)
        self.settings_with(payroll_self_view_enabled=True)
        mine = self.api_get(f'{P}my/?period=2026-10', user=self.emp)
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(mine.data['row']['monthly_salary'], '2200.00')

    def test_feature_off(self):
        self.settings_with(payroll_enabled=False)
        self.assertEqual(self.api_get(f'{P}report/?period=2026-10', user=self.admin).status_code, 403)


class TestRateSnapshotOnCreate(PayrollBase):

    def test_new_request_copies_current_rate(self):
        b = self.create_user(email='snap-b@test.com')
        lt = self.create_leave_type(code='SUBLEAVE', requires_substitutes=True)
        self.create_leave_balance(self.emp, lt, year=2027)
        assignment = SubstituteAssignment.objects.create(tenant=self.tenant, owner=self.emp, substitute=b, task='Desk')
        SubstitutionRate.objects.create(assignment=assignment, mode='month', amount=Decimal('500'))
        resp = self.api_post('/api/leave/employee/my-requests/', {
            'leave_type': lt.id, 'start_date': '2027-10-14', 'end_date': '2027-10-15',
        }, user=self.emp)
        self.assertEqual(resp.status_code, 201, resp.data)
        row = LeaveRequestSubstitute.objects.get(substitute=b)
        self.assertEqual((row.rate_mode, row.rate_amount, row.rate_currency), ('month', Decimal('500.00'), 'GEL'))
