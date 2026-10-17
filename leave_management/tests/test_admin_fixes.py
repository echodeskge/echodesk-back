"""
Leave admin fixes (parity package P5):
- approval chain progress on the admin route (3-level chain, HR-only chain,
  no-approval types) and the balance moving on the final step
- admin decision actions gated to admins/HR, action value enforced
- notifications: partial vs final approval, next approver, cancellation
- destroy / cancel balance handling
- leave type / approval chain PATCH validation, settings duplicate POST
- balance ordering by available_days
- idempotent carry forward
- admin list filters (status__in, department, overlapping window)
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from rest_framework import status

from leave_management.models import (
    LeaveBalance, LeaveCarryForwardRun, LeaveRequest, LeaveSettings,
)
from leave_management.tests.conftest import LeaveTestCase
from users.models import Department, Notification

BALANCE_URL = '/api/leave/admin/leave-balances/'
REQUEST_URL = '/api/leave/admin/leave-requests/'
LEAVE_TYPE_URL = '/api/leave/admin/leave-types/'
CHAIN_URL = '/api/leave/admin/approval-chains/'
SETTINGS_URL = '/api/leave/admin/settings/'
MANAGER_URL = '/api/leave/manager/team-requests/'
EMPLOYEE_URL = '/api/leave/employee/my-requests/'


def _patch_feature(test_method):
    """Patch the tenant feature gate open."""
    @patch('leave_management.permissions.HasLeaveManagementFeature.has_permission', return_value=True)
    def wrapper(self, mock_perm, *args, **kwargs):
        return test_method(self, *args, **kwargs)
    wrapper.__name__ = test_method.__name__
    wrapper.__doc__ = test_method.__doc__
    return wrapper


def _next_monday(weeks_ahead=2):
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) + 7 * (weeks_ahead - 1))


class AdminFixesBase(LeaveTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='p5-admin@test.com')
        self.hr = self.create_admin(email='p5-hr@test.com')
        self.manager = self.create_user(email='p5-mgr@test.com', role='agent')
        self.employee = self.create_user(
            email='p5-emp@test.com', role='agent', manager=self.manager,
        )
        self.leave_type = self.create_leave_type(created_by=self.admin, updated_by=self.admin)
        self.balance = self.create_leave_balance(
            self.employee, self.leave_type, pending_days=Decimal('3'),
        )

    def make_request(self, **kwargs):
        kwargs.setdefault('total_days', Decimal('3'))
        return self.create_leave_request(self.employee, self.leave_type, **kwargs)

    def approve(self, lr, user, comments='ok'):
        return self.api_post(
            f'{REQUEST_URL}{lr.pk}/approve/', {'action': 'approve', 'comments': comments}, user=user,
        )

    def reload_balance(self):
        return LeaveBalance.objects.get(pk=self.balance.pk)

    def notifs(self, user, notification_type):
        return Notification.objects.filter(user=user, notification_type=notification_type)


# ═══════════════════════════════════════════════════════════
#  Approval chain progress on the admin route
# ═══════════════════════════════════════════════════════════

class TestAdminApprovalChain(AdminFixesBase):

    @_patch_feature
    def test_three_level_chain_final_approval_moves_balance(self):
        self.create_approval_chain(level=1, approver_role='manager')
        self.create_approval_chain(level=2, approver_role='hr')
        self.create_approval_chain(level=3, approver_role='admin')
        lr = self.make_request()

        resp = self.approve(lr, self.admin, 'manager ok')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'manager_approved')

        resp = self.approve(lr, self.hr, 'hr ok')
        self.assertEqual(resp.data['status'], 'hr_approved')
        # Still pending on the balance until the last step
        self.assertEqual(self.reload_balance().used_days, Decimal('0'))

        resp = self.approve(lr, self.admin, 'final ok')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'approved')
        self.assertTrue(resp.data['success'])

        lr.refresh_from_db()
        self.assertEqual(lr.status, 'approved')
        self.assertEqual(lr.manager_comments, 'manager ok')
        self.assertEqual(lr.hr_approver, self.hr)
        self.assertEqual(lr.hr_comments, 'hr ok')
        self.assertEqual(lr.final_approver, self.admin)
        self.assertEqual(lr.final_comments, 'final ok')

        balance = self.reload_balance()
        self.assertEqual(balance.used_days, Decimal('3'))
        self.assertEqual(balance.pending_days, Decimal('0'))

        # Detail exposes the final comments
        detail = self.api_get(f'{REQUEST_URL}{lr.pk}/', user=self.admin)
        self.assertEqual(detail.data['final_comments'], 'final ok')

        # A fourth approve is refused
        resp = self.approve(lr, self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @_patch_feature
    def test_hr_only_chain_needs_one_approval_and_records_hr(self):
        self.create_leave_settings(require_manager_approval=False, require_hr_approval=True)
        lr = self.make_request()

        resp = self.approve(lr, self.hr)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'approved')

        lr.refresh_from_db()
        self.assertEqual(lr.hr_approver, self.hr)
        self.assertIsNone(lr.manager_approver)
        self.assertEqual(self.reload_balance().used_days, Decimal('3'))

    @_patch_feature
    def test_type_without_approval_is_approved_directly(self):
        lt = self.create_leave_type(code='NOAPP', requires_approval=False,
                                    created_by=self.admin, updated_by=self.admin)
        bal = self.create_leave_balance(self.employee, lt, pending_days=Decimal('2'))
        lr = self.create_leave_request(self.employee, lt, total_days=Decimal('2'))

        resp = self.approve(lr, self.admin)
        self.assertEqual(resp.data['status'], 'approved')
        bal.refresh_from_db()
        self.assertEqual(bal.used_days, Decimal('2'))
        self.assertEqual(bal.pending_days, Decimal('0'))

    @_patch_feature
    def test_approve_rejects_mismatched_action(self):
        lr = self.make_request()
        resp = self.api_post(
            f'{REQUEST_URL}{lr.pk}/approve/', {'action': 'reject', 'comments': 'no'}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        lr.refresh_from_db()
        self.assertEqual(lr.status, 'pending')

    @_patch_feature
    def test_reject_requires_reject_action_and_comments(self):
        lr = self.make_request()
        resp = self.api_post(f'{REQUEST_URL}{lr.pk}/reject/', {'action': 'approve'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        resp = self.api_post(f'{REQUEST_URL}{lr.pk}/reject/', {'action': 'reject'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        resp = self.api_post(
            f'{REQUEST_URL}{lr.pk}/reject/', {'action': 'reject', 'comments': 'busy'}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'rejected')

    @_patch_feature
    def test_manager_route_then_hr_on_admin_route(self):
        self.create_leave_settings(require_manager_approval=True, require_hr_approval=True)
        lr = self.make_request()

        resp = self.api_post(
            f'{MANAGER_URL}{lr.pk}/approve/', {'action': 'approve', 'comments': 'fine'}, user=self.manager,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'manager_approved')
        self.assertEqual(self.reload_balance().used_days, Decimal('0'))

        resp = self.approve(lr, self.hr)
        self.assertEqual(resp.data['status'], 'approved')
        self.assertEqual(self.reload_balance().used_days, Decimal('3'))


# ═══════════════════════════════════════════════════════════
#  Permissions on the admin decision actions
# ═══════════════════════════════════════════════════════════

class TestAdminDecisionPermissions(AdminFixesBase):

    @_patch_feature
    def test_team_manager_cannot_use_admin_actions(self):
        lr = self.make_request()
        for action in ('approve', 'reject', 'cancel'):
            resp = self.api_post(
                f'{REQUEST_URL}{lr.pk}/{action}/',
                {'action': 'reject' if action == 'reject' else 'approve', 'comments': 'x', 'reason': 'x'},
                user=self.manager,
            )
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, action)
        lr.refresh_from_db()
        self.assertEqual(lr.status, 'pending')

    @_patch_feature
    def test_employee_cannot_use_admin_actions_on_own_request(self):
        lr = self.make_request()
        resp = self.api_post(f'{REQUEST_URL}{lr.pk}/cancel/', {'reason': 'x'}, user=self.employee)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    @_patch_feature
    def test_manage_leave_balances_codename_grants_access(self):
        hr_user = self.create_user(email='p5-hr-perm@test.com', role='agent')
        resp = self.api_get(REQUEST_URL, user=hr_user)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        def fake_has_perm(user, perm, obj=None):
            return perm == 'leave_management.manage_leave_balances'

        with patch('users.models.User.has_perm', fake_has_perm):
            resp = self.api_get(REQUEST_URL, user=hr_user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


# ═══════════════════════════════════════════════════════════
#  Notifications
# ═══════════════════════════════════════════════════════════

class TestLeaveDecisionNotifications(AdminFixesBase):

    @_patch_feature
    def test_partial_then_final_approval_notifications(self):
        self.create_leave_settings(require_manager_approval=True, require_hr_approval=True)
        lr = self.make_request()
        Notification.objects.all().delete()

        self.api_post(
            f'{MANAGER_URL}{lr.pk}/approve/', {'action': 'approve'}, user=self.manager,
        )
        # Employee: progress, not "approved"
        progress = self.notifs(self.employee, 'leave_request_progress')
        self.assertEqual(progress.count(), 1)
        self.assertEqual(progress.first().title, 'Leave request partially approved')
        self.assertEqual(progress.first().metadata['next_approver_role'], 'hr')
        self.assertEqual(progress.first().link_url, f'/leave/my-requests?id={lr.id}')
        self.assertFalse(self.notifs(self.employee, 'leave_request_approved').exists())

        # HR (staff) is told it is their turn, with the admin link
        for staff in (self.admin, self.hr):
            awaiting = self.notifs(staff, 'leave_request_awaiting_approval')
            self.assertEqual(awaiting.count(), 1)
            self.assertEqual(awaiting.first().link_url, f'/leave/all-requests?id={lr.id}')
            self.assertEqual(awaiting.first().metadata['leave_request_id'], lr.id)
        # Not the acting manager, not the employee
        self.assertFalse(self.notifs(self.manager, 'leave_request_awaiting_approval').exists())
        self.assertFalse(self.notifs(self.employee, 'leave_request_awaiting_approval').exists())

        self.approve(lr, self.hr)
        approved = self.notifs(self.employee, 'leave_request_approved')
        self.assertEqual(approved.count(), 1)
        self.assertEqual(approved.first().metadata['status'], 'approved')

    @_patch_feature
    def test_hr_stage_actor_is_not_notified_about_admin_step(self):
        self.create_approval_chain(level=1, approver_role='hr')
        self.create_approval_chain(level=2, approver_role='admin')
        lr = self.make_request()
        Notification.objects.all().delete()

        resp = self.approve(lr, self.hr)
        self.assertEqual(resp.data['status'], 'hr_approved')
        self.assertFalse(self.notifs(self.hr, 'leave_request_awaiting_approval').exists())
        self.assertTrue(self.notifs(self.admin, 'leave_request_awaiting_approval').exists())
        self.assertEqual(
            self.notifs(self.employee, 'leave_request_progress').first().metadata['next_approver_role'],
            'admin',
        )

    @_patch_feature
    def test_single_step_approval_sends_only_approved(self):
        lr = self.make_request()
        Notification.objects.all().delete()
        self.approve(lr, self.admin)
        self.assertEqual(self.notifs(self.employee, 'leave_request_approved').count(), 1)
        self.assertFalse(self.notifs(self.employee, 'leave_request_progress').exists())
        self.assertFalse(Notification.objects.filter(notification_type='leave_request_awaiting_approval').exists())

    @_patch_feature
    def test_admin_cancel_notifies_employee_and_approvers(self):
        lr = self.make_request(status='approved', manager_approver=self.manager,
                               final_approver=self.manager)
        Notification.objects.all().delete()

        resp = self.api_post(f'{REQUEST_URL}{lr.pk}/cancel/', {'reason': 'Overlap'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'cancelled')

        emp = self.notifs(self.employee, 'leave_request_cancelled')
        self.assertEqual(emp.count(), 1)
        self.assertEqual(emp.first().metadata['cancellation_reason'], 'Overlap')
        self.assertEqual(emp.first().metadata['previous_status'], 'approved')
        self.assertEqual(emp.first().link_url, f'/leave/my-requests?id={lr.id}')

        mgr = self.notifs(self.manager, 'leave_request_cancelled')
        self.assertEqual(mgr.count(), 1)
        self.assertEqual(mgr.first().link_url, f'/leave/team-requests?id={lr.id}')
        # The acting admin is not notified
        self.assertFalse(self.notifs(self.admin, 'leave_request_cancelled').exists())

    @_patch_feature
    def test_employee_cancel_notifies_manager_not_self(self):
        lr = self.make_request()
        Notification.objects.all().delete()
        resp = self.api_post(f'{EMPLOYEE_URL}{lr.pk}/cancel/', {'reason': 'no'}, user=self.employee)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(self.notifs(self.employee, 'leave_request_cancelled').exists())
        self.assertEqual(self.notifs(self.manager, 'leave_request_cancelled').count(), 1)

    @_patch_feature
    def test_staff_approver_gets_admin_link_on_cancel(self):
        lr = self.make_request(status='manager_approved', manager_approver=self.hr)
        Notification.objects.all().delete()
        self.api_post(f'{EMPLOYEE_URL}{lr.pk}/cancel/', {'reason': ''}, user=self.employee)
        n = self.notifs(self.hr, 'leave_request_cancelled').first()
        self.assertIsNotNone(n)
        self.assertEqual(n.link_url, f'/leave/all-requests?id={lr.id}')


# ═══════════════════════════════════════════════════════════
#  Destroy / cancel balance handling
# ═══════════════════════════════════════════════════════════

class TestAdminBalanceHandling(AdminFixesBase):

    @_patch_feature
    def test_destroy_approved_returns_used_days(self):
        LeaveBalance.objects.filter(pk=self.balance.pk).update(
            used_days=Decimal('3'), pending_days=Decimal('1'),
        )
        lr = self.make_request(status='approved')
        resp = self.api_delete(f'{REQUEST_URL}{lr.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        balance = self.reload_balance()
        self.assertEqual(balance.used_days, Decimal('0'))
        self.assertEqual(balance.pending_days, Decimal('1'))

    @_patch_feature
    def test_destroy_pending_releases_pending_days(self):
        lr = self.make_request()
        self.api_delete(f'{REQUEST_URL}{lr.pk}/', user=self.admin)
        self.assertEqual(self.reload_balance().pending_days, Decimal('0'))

    @_patch_feature
    def test_destroy_rejected_leaves_balance(self):
        lr = self.make_request(status='rejected')
        self.api_delete(f'{REQUEST_URL}{lr.pk}/', user=self.admin)
        self.assertEqual(self.reload_balance().pending_days, Decimal('3'))

    @_patch_feature
    def test_cancel_rejected_is_refused_and_balance_untouched(self):
        lr = self.make_request(status='rejected')
        resp = self.api_post(f'{REQUEST_URL}{lr.pk}/cancel/', {'reason': ''}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.reload_balance().pending_days, Decimal('3'))

    @_patch_feature
    def test_cancel_approved_returns_used_days(self):
        LeaveBalance.objects.filter(pk=self.balance.pk).update(
            used_days=Decimal('3'), pending_days=Decimal('0'),
        )
        lr = self.make_request(status='approved')
        self.api_post(f'{REQUEST_URL}{lr.pk}/cancel/', {'reason': ''}, user=self.admin)
        self.assertEqual(self.reload_balance().used_days, Decimal('0'))

    @_patch_feature
    def test_editing_dates_moves_pending_days(self):
        start = _next_monday()
        lr = self.make_request(start_date=start, end_date=start + timedelta(days=2))
        LeaveBalance.objects.filter(pk=self.balance.pk).update(
            year=start.year, pending_days=Decimal('3'),
        )
        resp = self.api_patch(
            f'{REQUEST_URL}{lr.pk}/', {'end_date': str(start + timedelta(days=4))}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        lr.refresh_from_db()
        self.assertEqual(lr.total_days, Decimal('5'))
        self.assertEqual(self.reload_balance().pending_days, Decimal('5'))


# ═══════════════════════════════════════════════════════════
#  Validation fixes
# ═══════════════════════════════════════════════════════════

class TestAdminValidationFixes(AdminFixesBase):

    @_patch_feature
    def test_leave_type_patch_without_day_fields(self):
        resp = self.api_patch(f'{LEAVE_TYPE_URL}{self.leave_type.pk}/', {'is_active': False}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.leave_type.refresh_from_db()
        self.assertFalse(self.leave_type.is_active)

    @_patch_feature
    def test_leave_type_patch_still_validates_submitted_days(self):
        resp = self.api_patch(
            f'{LEAVE_TYPE_URL}{self.leave_type.pk}/', {'default_days_per_year': '0'}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('default_days_per_year', resp.data)

    @_patch_feature
    def test_leave_type_patch_to_accrual_needs_rate(self):
        resp = self.api_patch(
            f'{LEAVE_TYPE_URL}{self.leave_type.pk}/', {'calculation_method': 'accrual'}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('accrual_rate_per_month', resp.data)

    @_patch_feature
    def test_lowercase_duplicate_code_is_400(self):
        self.leave_type.code = 'SICK'
        self.leave_type.save()
        resp = self.api_post(LEAVE_TYPE_URL, {
            'name': {'en': 'Sick'}, 'code': 'sick', 'calculation_method': 'annual',
            'default_days_per_year': '10',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('code', resp.data)

    @_patch_feature
    def test_leave_type_list_has_calculation_fields(self):
        resp = self.api_get(LEAVE_TYPE_URL, user=self.admin)
        row = self.get_results(resp)[0]
        for field in ('calculation_method', 'default_days_per_year', 'accrual_rate_per_month',
                      'max_carry_forward_days', 'carry_forward_expiry_months'):
            self.assertIn(field, row)

    @_patch_feature
    def test_approval_chain_patch_without_level(self):
        chain = self.create_approval_chain(level=1, approver_role='manager')
        resp = self.api_patch(f'{CHAIN_URL}{chain.pk}/', {'approver_role': 'hr'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        chain.refresh_from_db()
        self.assertEqual(chain.approver_role, 'hr')
        self.assertEqual(chain.level, 1)

    @_patch_feature
    def test_approval_chain_patch_keeps_leave_type_for_uniqueness(self):
        self.create_approval_chain(leave_type=self.leave_type, level=1, approver_role='manager')
        other = self.create_approval_chain(leave_type=self.leave_type, level=2, approver_role='hr')
        # Same leave type, level 1 already taken
        resp = self.api_patch(f'{CHAIN_URL}{other.pk}/', {'level': 1}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('level', resp.data)

    @_patch_feature
    def test_approval_chain_create_without_level_is_400(self):
        resp = self.api_post(CHAIN_URL, {'approver_role': 'hr'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @_patch_feature
    def test_second_settings_post_is_400(self):
        self.create_leave_settings()
        resp = self.api_post(SETTINGS_URL, {'require_hr_approval': True}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(LeaveSettings.objects.filter(tenant=self.tenant).count(), 1)

    @_patch_feature
    def test_balance_ordering_by_available_days(self):
        other = self.create_user(email='p5-other@test.com', role='agent')
        self.create_leave_balance(other, self.leave_type, allocated_days=Decimal('5'))
        resp = self.api_get(f'{BALANCE_URL}?ordering=available_days', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        values = [Decimal(r['available_days']) for r in self.get_results(resp)]
        self.assertEqual(values, sorted(values))
        resp = self.api_get(f'{BALANCE_URL}?ordering=-available_days', user=self.admin)
        values = [Decimal(r['available_days']) for r in self.get_results(resp)]
        self.assertEqual(values, sorted(values, reverse=True))

    @_patch_feature
    def test_duplicate_balance_post_is_400(self):
        resp = self.api_post(BALANCE_URL, {
            'user': self.employee.pk, 'leave_type': self.leave_type.pk,
            'year': self.balance.year, 'allocated_days': '10',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @_patch_feature
    def test_initialize_user_response(self):
        resp = self.api_post(f'{BALANCE_URL}initialize_user/', {'user_id': self.manager.pk}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(set(resp.data), {'success', 'message'})


# ═══════════════════════════════════════════════════════════
#  Carry forward
# ═══════════════════════════════════════════════════════════

class TestCarryForward(AdminFixesBase):

    def setUp(self):
        super().setUp()
        self.year = date.today().year - 1
        # 20 allocated + 2 carried in - 8 used = 14 left; 4 pending is ignored;
        # capped at max_carry_forward_days=5.
        self.old = self.create_leave_balance(
            self.manager, self.leave_type, year=self.year,
            allocated_days=Decimal('20'), carried_forward_days=Decimal('2'),
            used_days=Decimal('8'), pending_days=Decimal('4'),
        )
        # 3 allocated - 1 used = 2 left (pending 2 not subtracted) -> 2
        self.low = self.create_leave_balance(
            self.employee, self.leave_type, year=self.year,
            allocated_days=Decimal('3'), used_days=Decimal('1'), pending_days=Decimal('2'),
        )

    def carry(self, user=None, **body):
        body.setdefault('from_year', self.year)
        body.setdefault('to_year', self.year + 1)
        return self.api_post(f'{BALANCE_URL}carry_forward/', body, user=user or self.admin)

    def carried(self, user):
        return LeaveBalance.objects.get(
            user=user, leave_type=self.leave_type, year=self.year + 1,
        ).carried_forward_days

    @_patch_feature
    def test_first_run_carries_remaining_excluding_pending(self):
        resp = self.carry()
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertFalse(resp.data['already_run'])
        self.assertEqual(resp.data['carried_forward'], 2)
        self.assertEqual(Decimal(str(resp.data['total_days'])), Decimal('7'))
        self.assertEqual(self.carried(self.manager), Decimal('5'))
        # The employee already had a balance for the new year (setUp): set, not added
        self.assertEqual(self.carried(self.employee), Decimal('2'))
        self.assertEqual(LeaveCarryForwardRun.objects.filter(tenant=self.tenant, from_year=self.year).count(), 1)

    @_patch_feature
    def test_second_run_is_a_noop(self):
        self.carry()
        resp = self.carry()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['already_run'])
        self.assertEqual(resp.data['carried_forward'], 0)
        self.assertEqual(self.carried(self.manager), Decimal('5'))
        self.assertEqual(self.carried(self.employee), Decimal('2'))

    @_patch_feature
    def test_force_requires_superuser(self):
        self.carry()
        resp = self.carry(force=True)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    @_patch_feature
    def test_force_by_superuser_recomputes_without_double_counting(self):
        self.carry()
        LeaveBalance.objects.filter(pk=self.low.pk).update(used_days=Decimal('2'))
        root = self.create_user(email='p5-root@test.com', role='admin', is_staff=True, is_superuser=True)
        resp = self.carry(user=root, force=True)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertFalse(resp.data['already_run'])
        self.assertEqual(self.carried(self.manager), Decimal('5'))
        self.assertEqual(self.carried(self.employee), Decimal('1'))
        run = LeaveCarryForwardRun.objects.get(tenant=self.tenant, from_year=self.year)
        self.assertEqual(run.forced_runs, 1)
        self.assertEqual(run.run_by, root)

    @_patch_feature
    def test_to_year_must_follow_from_year(self):
        resp = self.carry(to_year=self.year + 2)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(LeaveCarryForwardRun.objects.exists())

    @_patch_feature
    def test_non_admin_cannot_carry_forward(self):
        resp = self.carry(user=self.employee)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# ═══════════════════════════════════════════════════════════
#  Admin list filters
# ═══════════════════════════════════════════════════════════

class TestAdminListFilters(AdminFixesBase):

    def ids(self, query):
        resp = self.api_get(f'{REQUEST_URL}?{query}', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, getattr(resp, 'data', None))
        return {r['id'] for r in self.get_results(resp)}

    @_patch_feature
    def test_status_in(self):
        a = self.make_request(status='manager_approved')
        b = self.make_request(status='hr_approved')
        c = self.make_request(status='pending')
        got = self.ids('status__in=manager_approved,hr_approved')
        self.assertEqual(got, {a.id, b.id})
        self.assertNotIn(c.id, got)

    @_patch_feature
    def test_department(self):
        dept = Department.objects.create(name='P5 Ops')
        other = self.create_user(email='p5-dept@test.com', role='agent', department=dept)
        mine = self.create_leave_request(other, self.leave_type)
        self.make_request()
        self.assertEqual(self.ids(f'department={dept.id}'), {mine.id})

    @_patch_feature
    def test_bad_department_is_400(self):
        resp = self.api_get(f'{REQUEST_URL}?department=abc', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @_patch_feature
    def test_date_window_overlap(self):
        base = date.today() + timedelta(days=30)
        straddle = self.make_request(start_date=base - timedelta(days=2), end_date=base + timedelta(days=1))
        inside = self.make_request(start_date=base + timedelta(days=3), end_date=base + timedelta(days=4))
        outside = self.make_request(start_date=base + timedelta(days=20), end_date=base + timedelta(days=21))
        got = self.ids(f'start_date={base}&end_date={base + timedelta(days=10)}')
        self.assertEqual(got, {straddle.id, inside.id})
        self.assertNotIn(outside.id, got)
