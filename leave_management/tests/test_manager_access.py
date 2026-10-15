"""
Tests for manager-side team scoping (User.manager + department):
- team_q / is_leave_manager / manages_employee helpers
- /api/leave/manager/team-requests/ visibility, object access, approval
- /api/leave/manager/team-balances/ visibility
- status__in and overlapping date-window filters
"""
from decimal import Decimal
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status

from leave_management.tests.conftest import LeaveTestCase
from leave_management.utils import is_leave_manager, manages_employee
from users.models import Department

User = get_user_model()

TEAM_REQUESTS_URL = '/api/leave/manager/team-requests/'
TEAM_BALANCES_URL = '/api/leave/manager/team-balances/'
MY_REQUESTS_URL = '/api/leave/employee/my-requests/'


def _patch_feature(test_method):
    """Decorator that patches tenant_has_feature to always return True."""
    @patch('leave_management.permissions.HasLeaveManagementFeature.has_permission', return_value=True)
    def wrapper(self, mock_perm, *args, **kwargs):
        return test_method(self, *args, **kwargs)
    wrapper.__name__ = test_method.__name__
    wrapper.__doc__ = test_method.__doc__
    return wrapper


class TestTeamHelpers(LeaveTestCase):
    """Unit tests for the scoping helpers in leave_management.utils."""

    def test_plain_agent_is_not_a_manager(self):
        agent = self.create_user(email='th-agent@test.com', role='agent')
        self.assertFalse(is_leave_manager(agent))

    def test_agent_with_direct_report_is_a_manager(self):
        agent = self.create_user(email='th-lead@test.com', role='agent')
        self.create_user(email='th-report@test.com', manager=agent)
        self.assertTrue(is_leave_manager(agent))

    def test_agent_with_only_inactive_report_is_not_a_manager(self):
        agent = self.create_user(email='th-lead2@test.com', role='agent')
        self.create_user(email='th-gone@test.com', manager=agent, is_active=False)
        self.assertFalse(is_leave_manager(agent))

    def test_manager_role_and_staff_are_managers(self):
        self.assertTrue(is_leave_manager(self.create_manager(email='th-mgr@test.com')))
        self.assertTrue(is_leave_manager(self.create_admin(email='th-admin@test.com')))

    def test_nobody_manages_themselves(self):
        dept = Department.objects.create(name='TH Self Dept')
        mgr = self.create_manager(email='th-self@test.com')
        mgr.department = dept
        mgr.save()
        self.assertFalse(manages_employee(mgr, mgr))

    def test_manages_direct_report_and_department(self):
        dept = Department.objects.create(name='TH Dept')
        mgr = self.create_manager(email='th-m@test.com')
        mgr.department = dept
        mgr.save()
        direct = self.create_user(email='th-d@test.com', manager=mgr)
        same_dept = self.create_user(email='th-sd@test.com', department=dept)
        outsider = self.create_user(email='th-o@test.com')
        self.assertTrue(manages_employee(mgr, direct))
        self.assertTrue(manages_employee(mgr, same_dept))
        self.assertFalse(manages_employee(mgr, outsider))

    def test_agent_does_not_get_department_by_role(self):
        """Only the 'manager' role widens scope to the department."""
        dept = Department.objects.create(name='TH Agent Dept')
        agent = self.create_user(email='th-a1@test.com', role='agent', department=dept)
        peer = self.create_user(email='th-a2@test.com', role='agent', department=dept)
        self.assertFalse(manages_employee(agent, peer))


class TestTeamRequestsVisibility(LeaveTestCase):

    def setUp(self):
        super().setUp()
        self.leave_type = self.create_leave_type()
        # An agent who happens to have one direct report
        self.lead = self.create_user(email='ma-lead@test.com', role='agent')
        self.report = self.create_user(email='ma-report@test.com', manager=self.lead)
        self.other = self.create_user(email='ma-other@test.com')
        self.report_lr = self.create_leave_request(self.report, self.leave_type)
        self.other_lr = self.create_leave_request(self.other, self.leave_type)

    @_patch_feature
    def test_agent_with_direct_report_sees_only_it(self):
        """Regression: this used to raise FieldError (employee__manager)."""
        resp = self.api_get(TEAM_REQUESTS_URL, user=self.lead)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r['id'] for r in self.get_results(resp)]
        self.assertEqual(ids, [self.report_lr.id])

    @_patch_feature
    def test_agent_without_reports_is_forbidden(self):
        resp = self.api_get(TEAM_REQUESTS_URL, user=self.other)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    @_patch_feature
    def test_staff_sees_everything(self):
        admin = self.create_admin(email='ma-admin@test.com')
        resp = self.api_get(TEAM_REQUESTS_URL, user=admin)
        ids = {r['id'] for r in self.get_results(resp)}
        self.assertEqual(ids, {self.report_lr.id, self.other_lr.id})

    @_patch_feature
    def test_request_outside_team_is_404(self):
        resp = self.api_get(f'{TEAM_REQUESTS_URL}{self.other_lr.id}/', user=self.lead)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    @_patch_feature
    def test_request_inside_team_is_visible(self):
        resp = self.api_get(f'{TEAM_REQUESTS_URL}{self.report_lr.id}/', user=self.lead)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['id'], self.report_lr.id)

    @_patch_feature
    def test_manager_role_sees_department_not_others(self):
        dept = Department.objects.create(name='MA Sales')
        other_dept = Department.objects.create(name='MA Support')
        mgr = self.create_manager(email='ma-mgr@test.com')
        mgr.department = dept
        mgr.save()
        member = self.create_user(email='ma-member@test.com', department=dept)
        stranger = self.create_user(email='ma-stranger@test.com', department=other_dept)
        member_lr = self.create_leave_request(member, self.leave_type)
        self.create_leave_request(stranger, self.leave_type)
        # The manager's own request must not show up in the team list
        self.create_leave_request(mgr, self.leave_type)

        resp = self.api_get(TEAM_REQUESTS_URL, user=mgr)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r['id'] for r in self.get_results(resp)]
        self.assertEqual(ids, [member_lr.id])

    @_patch_feature
    def test_manager_role_without_department_sees_only_direct_reports(self):
        mgr = self.create_manager(email='ma-mgr2@test.com')
        direct = self.create_user(email='ma-direct2@test.com', manager=mgr)
        direct_lr = self.create_leave_request(direct, self.leave_type)

        resp = self.api_get(TEAM_REQUESTS_URL, user=mgr)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r['id'] for r in self.get_results(resp)]
        self.assertEqual(ids, [direct_lr.id])


class TestTeamRequestsApproval(LeaveTestCase):

    def setUp(self):
        super().setUp()
        self.create_leave_settings(require_manager_approval=True, require_hr_approval=False)
        self.leave_type = self.create_leave_type()
        self.lead = self.create_user(email='ap-lead@test.com', role='agent')
        self.report = self.create_user(email='ap-report@test.com', manager=self.lead)
        self.create_leave_balance(self.report, self.leave_type, pending_days=Decimal('3'))
        self.lr = self.create_leave_request(self.report, self.leave_type, status='pending')

    @_patch_feature
    def test_direct_manager_approves(self):
        resp = self.api_post(
            f'{TEAM_REQUESTS_URL}{self.lr.id}/approve/',
            {'action': 'approve', 'comments': 'ok'},
            user=self.lead,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.lr.refresh_from_db()
        self.assertEqual(self.lr.status, 'approved')
        self.assertEqual(self.lr.manager_approver, self.lead)

    @_patch_feature
    def test_direct_manager_rejects(self):
        resp = self.api_post(
            f'{TEAM_REQUESTS_URL}{self.lr.id}/reject/',
            {'action': 'reject', 'comments': 'no'},
            user=self.lead,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.lr.refresh_from_db()
        self.assertEqual(self.lr.status, 'rejected')

    @_patch_feature
    def test_unrelated_manager_cannot_approve(self):
        """A manager of a different team cannot reach the request at all."""
        other_lead = self.create_user(email='ap-other-lead@test.com', role='agent')
        self.create_user(email='ap-other-report@test.com', manager=other_lead)
        resp = self.api_post(
            f'{TEAM_REQUESTS_URL}{self.lr.id}/approve/',
            {'action': 'approve'},
            user=other_lead,
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.lr.refresh_from_db()
        self.assertEqual(self.lr.status, 'pending')


class TestTeamBalances(LeaveTestCase):

    def setUp(self):
        super().setUp()
        self.leave_type = self.create_leave_type()
        self.lead = self.create_user(email='tb-lead@test.com', role='agent')
        self.report = self.create_user(email='tb-report@test.com', manager=self.lead)
        self.other = self.create_user(email='tb-other@test.com')
        self.report_bal = self.create_leave_balance(self.report, self.leave_type)
        self.other_bal = self.create_leave_balance(self.other, self.leave_type)
        self.own_bal = self.create_leave_balance(self.lead, self.leave_type)

    @_patch_feature
    def test_team_balances_filtered_to_direct_reports(self):
        resp = self.api_get(TEAM_BALANCES_URL, user=self.lead)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [b['id'] for b in self.get_results(resp)]
        self.assertEqual(ids, [self.report_bal.id])

    @_patch_feature
    def test_balance_outside_team_is_404(self):
        resp = self.api_get(f'{TEAM_BALANCES_URL}{self.other_bal.id}/', user=self.lead)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    @_patch_feature
    def test_balance_inside_team_is_visible(self):
        # Object permission runs against a LeaveBalance (``user``, not
        # ``employee``) -- this used to raise AttributeError for non-staff.
        resp = self.api_get(f'{TEAM_BALANCES_URL}{self.report_bal.id}/', user=self.lead)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['id'], self.report_bal.id)

    @_patch_feature
    def test_own_balance_is_not_a_team_balance(self):
        resp = self.api_get(f'{TEAM_BALANCES_URL}{self.own_bal.id}/', user=self.lead)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    @_patch_feature
    def test_staff_retrieves_any_balance(self):
        staff = self.create_user(email='tb-staff@test.com', is_staff=True)
        resp = self.api_get(f'{TEAM_BALANCES_URL}{self.other_bal.id}/', user=staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    @_patch_feature
    def test_summary_counts_only_the_team(self):
        resp = self.api_get(f'{TEAM_BALANCES_URL}summary/', user=self.lead)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['team_size'], 1)


class TestListFilters(LeaveTestCase):

    def setUp(self):
        super().setUp()
        self.leave_type = self.create_leave_type()
        self.lead = self.create_user(email='lf-lead@test.com', role='agent')
        self.report = self.create_user(email='lf-report@test.com', manager=self.lead)
        today = date.today()
        self.pending = self.create_leave_request(
            self.report, self.leave_type, status='pending',
            start_date=today + timedelta(days=5), end_date=today + timedelta(days=15),
        )
        self.approved = self.create_leave_request(
            self.report, self.leave_type, status='approved',
            start_date=today + timedelta(days=30), end_date=today + timedelta(days=31),
        )
        self.rejected = self.create_leave_request(
            self.report, self.leave_type, status='rejected',
            start_date=today + timedelta(days=40), end_date=today + timedelta(days=41),
        )

    def _ids(self, resp):
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return {r['id'] for r in self.get_results(resp)}

    @_patch_feature
    def test_status_in_on_team_requests(self):
        resp = self.api_get(f'{TEAM_REQUESTS_URL}?status__in=pending,rejected', user=self.lead)
        self.assertEqual(self._ids(resp), {self.pending.id, self.rejected.id})

    @_patch_feature
    def test_status_in_ignores_blank_entries(self):
        resp = self.api_get(f'{TEAM_REQUESTS_URL}?status__in=approved,', user=self.lead)
        self.assertEqual(self._ids(resp), {self.approved.id})

    @_patch_feature
    def test_status_in_on_my_requests(self):
        resp = self.api_get(f'{MY_REQUESTS_URL}?status__in=approved,rejected', user=self.report)
        self.assertEqual(self._ids(resp), {self.approved.id, self.rejected.id})

    @_patch_feature
    def test_date_window_includes_straddling_request(self):
        """A leave that starts before the window and ends inside it is returned."""
        today = date.today()
        start = (today + timedelta(days=10)).isoformat()
        end = (today + timedelta(days=20)).isoformat()
        resp = self.api_get(
            f'{TEAM_REQUESTS_URL}?start_date={start}&end_date={end}', user=self.lead
        )
        self.assertEqual(self._ids(resp), {self.pending.id})

    @_patch_feature
    def test_date_window_excludes_requests_outside_it(self):
        today = date.today()
        start = (today + timedelta(days=16)).isoformat()
        end = (today + timedelta(days=29)).isoformat()
        resp = self.api_get(
            f'{TEAM_REQUESTS_URL}?start_date={start}&end_date={end}', user=self.lead
        )
        self.assertEqual(self._ids(resp), set())

    @_patch_feature
    def test_date_window_spanning_everything(self):
        today = date.today()
        start = today.isoformat()
        end = (today + timedelta(days=60)).isoformat()
        resp = self.api_get(
            f'{TEAM_REQUESTS_URL}?start_date={start}&end_date={end}', user=self.lead
        )
        self.assertEqual(
            self._ids(resp), {self.pending.id, self.approved.id, self.rejected.id}
        )
