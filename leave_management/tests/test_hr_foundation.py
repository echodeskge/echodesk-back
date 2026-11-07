"""B1: Georgian holidays, recurring holidays, per-type limits, audit, profile flags."""
from datetime import date
from decimal import Decimal

from rest_framework import status

from leave_management.holidays_ge import orthodox_easter, georgian_holidays
from leave_management.models import PublicHoliday, LeaveAuditEvent, LeaveRequest
from leave_management.utils import calculate_working_days, working_dates
from leave_management.tests.hr_base import HrTestCase

MY_REQUESTS = '/api/leave/employee/my-requests/'
HOLIDAYS = '/api/leave/admin/public-holidays/'


class TestGeorgianHolidays(HrTestCase):

    def test_easter_dates(self):
        self.assertEqual(orthodox_easter(2026), date(2026, 4, 12))
        self.assertEqual(orthodox_easter(2027), date(2027, 5, 2))
        self.assertEqual(orthodox_easter(2025), date(2025, 4, 20))

    def test_holiday_set(self):
        days = {d for d, _ in georgian_holidays(2026)}
        self.assertIn(date(2026, 4, 9), days)
        self.assertIn(date(2026, 4, 10), days)   # Good Friday
        self.assertIn(date(2026, 4, 13), days)   # Easter Monday
        self.assertNotIn(date(2026, 4, 14), days)
        self.assertIn(date(2026, 5, 17), days)
        self.assertNotIn(date(2024, 5, 17), {d for d, _ in georgian_holidays(2024)})
        self.assertEqual(len(days), 18)

    def test_load_endpoint_is_idempotent_and_counts_as_non_working(self):
        admin = self.create_admin(email='hol-admin@test.com')
        self.create_leave_settings()
        resp = self.api_post(f'{HOLIDAYS}load-georgian/', {'year': 2027}, user=admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(len(resp.data['created']), 18)
        again = self.api_post(f'{HOLIDAYS}load-georgian/', {'year': 2027}, user=admin)
        self.assertEqual(len(again.data['created']), 0)
        self.assertEqual(PublicHoliday.objects.filter(date__year=2027).count(), 18)
        # 2027-05-03 is Easter Monday, 2027-05-17 Family Day, 05-18 a Tuesday
        self.assertEqual(working_dates(date(2027, 5, 3), date(2027, 5, 3), self.tenant), [])
        self.assertEqual(calculate_working_days(date(2027, 5, 17), date(2027, 5, 18), self.tenant), Decimal('1'))
        self.assertTrue(LeaveAuditEvent.objects.filter(kind='holiday').exists())

    def test_non_staff_cannot_load(self):
        agent = self.create_user(email='hol-agent@test.com')
        resp = self.api_post(f'{HOLIDAYS}load-georgian/', {'year': 2027}, user=agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_turning_auto_on_loads_this_and_next_year(self):
        admin = self.create_admin(email='hol-auto@test.com')
        settings_obj = self.create_leave_settings()
        resp = self.api_patch(
            f'/api/leave/admin/settings/{settings_obj.id}/', {'auto_georgian_holidays': True},
            user=admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        year = date.today().year
        self.assertTrue(PublicHoliday.objects.filter(date__year=year).exists())
        self.assertTrue(PublicHoliday.objects.filter(date__year=year + 1).exists())

    def test_recurring_holiday_counts_in_other_years(self):
        self.create_leave_settings()
        self.create_public_holiday(date=date(2020, 10, 14), is_recurring=True)
        # 2026-10-14 is a Wednesday
        self.assertEqual(calculate_working_days(date(2026, 10, 14), date(2026, 10, 14), self.tenant), 0)
        self.create_public_holiday(date=date(2020, 10, 15), is_recurring=False)
        self.assertEqual(calculate_working_days(date(2026, 10, 15), date(2026, 10, 15), self.tenant), 1)


class TestTypeLimits(HrTestCase):

    def setUp(self):
        super().setUp()
        self.create_leave_settings()
        self.employee = self.create_user(email='limits-emp@test.com')
        self.dayoff = self.create_leave_type(
            code='DAYOFF', max_requests_per_month=1, max_days_per_request=Decimal('1'),
        )
        self.create_leave_balance(self.employee, self.dayoff, year=2027)

    def post(self, start, end):
        return self.api_post(MY_REQUESTS, {
            'leave_type': self.dayoff.id, 'start_date': start, 'end_date': end,
        }, user=self.employee)

    def test_once_per_month_and_one_day(self):
        self.assertEqual(self.post('2027-11-04', '2027-11-05').status_code, 400)
        first = self.post('2027-11-04', '2027-11-04')
        self.assertEqual(first.status_code, 201, first.data)
        second = self.post('2027-11-08', '2027-11-08')
        self.assertEqual(second.status_code, 400)
        self.assertIn('per month', str(second.data))
        # A different month is fine
        self.assertEqual(self.post('2027-12-06', '2027-12-06').status_code, 201)

    def test_rejected_or_cancelled_do_not_count(self):
        self.create_leave_request(
            self.employee, self.dayoff, start_date=date(2027, 11, 4), end_date=date(2027, 11, 4),
            total_days=Decimal('1'), status='rejected',
        )
        self.assertEqual(self.post('2027-11-08', '2027-11-08').status_code, 201)

    def test_holiday_day_off_refused(self):
        PublicHoliday.objects.create(tenant=self.tenant, name={'en': 'Easter Monday'}, date=date(2027, 5, 3))
        resp = self.post('2027-05-03', '2027-05-03')
        self.assertEqual(resp.status_code, 400)

    def test_preview_reports_limit(self):
        self.post('2027-11-04', '2027-11-04')
        resp = self.api_post(f'{MY_REQUESTS}preview/', {
            'leave_type': self.dayoff.id, 'start_date': '2027-11-09', 'end_date': '2027-11-09',
        }, user=self.employee)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['allowed'])

    def test_inactive_type_refused(self):
        self.dayoff.is_active = False
        self.dayoff.save()
        self.assertEqual(self.post('2027-11-04', '2027-11-04').status_code, 400)


class TestAuditAndProfile(HrTestCase):

    def test_status_changes_are_audited_and_shown_in_history(self):
        self.create_leave_settings()
        admin = self.create_admin(email='audit-admin@test.com')
        employee = self.create_user(email='audit-emp@test.com', manager=admin)
        lt = self.create_leave_type()
        req = self.create_leave_request(employee, lt)
        req.status = 'cancelled'
        req._leave_actor = employee
        req.save()
        events = list(LeaveAuditEvent.objects.filter(leave_request=req).order_by('id'))
        self.assertEqual([e.action for e in events], ['created', 'status_changed'])
        self.assertEqual(events[1].from_status, 'pending')
        self.assertEqual(events[1].to_status, 'cancelled')
        resp = self.api_get(f'{MY_REQUESTS}{req.id}/', user=employee)
        self.assertEqual(len(resp.data['history']), 2)
        audit = self.api_get(f'/api/leave/admin/audit/?leave_request={req.id}', user=admin)
        self.assertEqual(audit.status_code, 200)
        agent_audit = self.api_get('/api/leave/admin/audit/', user=employee)
        self.assertEqual(agent_audit.status_code, 403)

    def test_profile_leave_features(self):
        user = self.create_user(email='profile-flags@test.com')
        resp = self.api_get('/api/auth/profile/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['leave_features']['kpi'])
        self.settings_with(kpi_enabled=True, payroll_enabled=True)
        resp = self.api_get('/api/auth/profile/', user=user)
        self.assertTrue(resp.data['leave_features']['kpi'])
        self.assertFalse(resp.data['leave_features']['can_manage_payroll'])
