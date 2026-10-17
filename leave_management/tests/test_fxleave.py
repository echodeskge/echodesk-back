"""
Review fixes on the leave admin package (parity-fix/leave):
1. decisions re-check the status on a locked row (no double-counted days)
2. carry forward survives the first-run unique-constraint race
3. nobody but a superuser approves / rejects their own request
4. chain steps are walked in level order, not by role rank; a step is
   done when its approver signed (a role repeated around another role
   does not skip it)
6. employee cancel / delete / edit lock the row and re-check the status
5. schema documents the real action bodies, 403s and 400 field errors
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError
from rest_framework import status

from amanati_crm.schema import TenantAwareSchemaGenerator
from leave_management.models import LeaveBalance, LeaveCarryForwardRun, LeaveRequest
from leave_management.tests.test_admin_fixes import (
    AdminFixesBase, BALANCE_URL, EMPLOYEE_URL, MANAGER_URL, REQUEST_URL, _patch_feature,
)
from leave_management.utils import get_next_approver_role
from leave_management.views import AdminLeaveRequestViewSet
from leave_management.views_employee import EmployeeLeaveRequestViewSet
from leave_management.views_manager import ManagerLeaveRequestViewSet


def _stale(lr):
    """A copy of ``lr`` as another request loaded it before it was decided."""
    return LeaveRequest.objects.get(pk=lr.pk)


# ═══════════════════════════════════════════════════════════
#  1. Concurrency: locked re-check of the status
# ═══════════════════════════════════════════════════════════

class TestDecisionLocking(AdminFixesBase):

    @_patch_feature
    def test_sequential_double_final_approve_counts_days_once(self):
        lr = self.make_request()
        first = self.approve(lr, self.admin)
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data['status'], 'approved')

        second = self.approve(lr, self.hr)
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', second.data)

        balance = self.reload_balance()
        self.assertEqual(balance.used_days, Decimal('3'))
        self.assertEqual(balance.pending_days, Decimal('0'))

    @_patch_feature
    def test_concurrent_final_approve_with_stale_object_is_refused(self):
        """Reproduction: both approvals loaded the request while it was pending."""
        lr = self.make_request()
        stale = _stale(lr)
        self.assertEqual(self.approve(lr, self.admin).data['status'], 'approved')

        with patch.object(AdminLeaveRequestViewSet, 'get_object', return_value=stale):
            resp = self.approve(lr, self.hr)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        balance = self.reload_balance()
        self.assertEqual(balance.used_days, Decimal('3'))  # not 6
        lr.refresh_from_db()
        self.assertEqual(lr.final_approver, self.admin)

    @_patch_feature
    def test_concurrent_reject_after_approve_is_refused(self):
        lr = self.make_request()
        stale = _stale(lr)
        self.approve(lr, self.admin)

        with patch.object(AdminLeaveRequestViewSet, 'get_object', return_value=stale):
            resp = self.api_post(
                f'{REQUEST_URL}{lr.pk}/reject/', {'action': 'reject', 'comments': 'no'}, user=self.hr,
            )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        lr.refresh_from_db()
        self.assertEqual(lr.status, 'approved')
        self.assertEqual(self.reload_balance().used_days, Decimal('3'))

    @_patch_feature
    def test_concurrent_cancel_after_cancel_is_refused(self):
        lr = self.make_request()
        self.approve(lr, self.admin)
        stale = _stale(lr)
        resp = self.api_post(f'{REQUEST_URL}{lr.pk}/cancel/', {'reason': 'x'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self.reload_balance().used_days, Decimal('0'))

        # A second cancel that loaded the approved row before the first one
        self.balance.used_days = Decimal('5')
        self.balance.save()
        with patch.object(AdminLeaveRequestViewSet, 'get_object', return_value=stale):
            resp = self.api_post(f'{REQUEST_URL}{lr.pk}/cancel/', {'reason': 'x'}, user=self.hr)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.reload_balance().used_days, Decimal('5'))

    @_patch_feature
    def test_destroy_uses_locked_status(self):
        """A destroy holding a stale 'approved' copy must not return used days twice."""
        lr = self.make_request()
        self.approve(lr, self.admin)
        stale = _stale(lr)
        self.api_post(f'{REQUEST_URL}{lr.pk}/cancel/', {}, user=self.admin)
        self.balance.used_days = Decimal('4')  # another approved request's days
        self.balance.save()

        with patch.object(AdminLeaveRequestViewSet, 'get_object', return_value=stale):
            resp = self.api_delete(f'{REQUEST_URL}{lr.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(LeaveRequest.objects.filter(pk=lr.pk).exists())
        self.assertEqual(self.reload_balance().used_days, Decimal('4'))

    @_patch_feature
    def test_destroy_balance_update_rolls_back_when_delete_fails(self):
        lr = self.make_request()
        with patch.object(AdminLeaveRequestViewSet, 'perform_destroy', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.api_delete(f'{REQUEST_URL}{lr.pk}/', user=self.admin)
        self.assertTrue(LeaveRequest.objects.filter(pk=lr.pk).exists())
        self.assertEqual(self.reload_balance().pending_days, Decimal('3'))

    @_patch_feature
    def test_manager_concurrent_approve_is_refused(self):
        self.create_leave_settings(require_manager_approval=True, require_hr_approval=True)
        lr = self.make_request()
        stale = _stale(lr)
        ok = self.api_post(
            f'{MANAGER_URL}{lr.pk}/approve/', {'action': 'approve'}, user=self.manager,
        )
        self.assertEqual(ok.data['status'], 'manager_approved')

        with patch.object(ManagerLeaveRequestViewSet, 'get_object', return_value=stale):
            resp = self.api_post(
                f'{MANAGER_URL}{lr.pk}/approve/', {'action': 'approve'}, user=self.manager,
            )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        lr.refresh_from_db()
        self.assertEqual(lr.status, 'manager_approved')

    @_patch_feature
    def test_manager_reject_response_has_status(self):
        lr = self.make_request()
        resp = self.api_post(
            f'{MANAGER_URL}{lr.pk}/reject/', {'action': 'reject', 'comments': 'busy'}, user=self.manager,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, {
            'success': True, 'message': 'Leave request rejected', 'status': 'rejected',
        })
        self.assertEqual(self.reload_balance().pending_days, Decimal('0'))


class TestEmployeeRouteLocking(AdminFixesBase):
    """Employee cancel / delete / edit re-check the status on a locked row."""

    @_patch_feature
    def test_employee_cancel_with_stale_pending_copy_returns_used_days(self):
        lr = self.make_request()
        stale = _stale(lr)
        self.assertEqual(self.approve(lr, self.admin).data['status'], 'approved')
        self.assertEqual(self.reload_balance().used_days, Decimal('3'))

        with patch.object(EmployeeLeaveRequestViewSet, 'get_object', return_value=stale):
            resp = self.api_post(f'{EMPLOYEE_URL}{lr.pk}/cancel/', {'reason': 'x'}, user=self.employee)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        balance = self.reload_balance()
        self.assertEqual(balance.used_days, Decimal('0'))     # not left counted
        self.assertEqual(balance.pending_days, Decimal('0'))  # not released twice

    @_patch_feature
    def test_employee_cancel_with_stale_copy_after_reject_is_refused(self):
        lr = self.make_request()
        stale = _stale(lr)
        self.api_post(
            f'{REQUEST_URL}{lr.pk}/reject/', {'action': 'reject', 'comments': 'no'}, user=self.admin,
        )
        with patch.object(EmployeeLeaveRequestViewSet, 'get_object', return_value=stale):
            resp = self.api_post(f'{EMPLOYEE_URL}{lr.pk}/cancel/', {'reason': 'x'}, user=self.employee)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        lr.refresh_from_db()
        self.assertEqual(lr.status, 'rejected')
        self.assertEqual(self.reload_balance().pending_days, Decimal('0'))

    @_patch_feature
    def test_employee_delete_with_stale_pending_copy_is_refused(self):
        lr = self.make_request()
        stale = _stale(lr)
        self.approve(lr, self.admin)

        with patch.object(EmployeeLeaveRequestViewSet, 'get_object', return_value=stale):
            resp = self.api_delete(f'{EMPLOYEE_URL}{lr.pk}/', user=self.employee)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Only pending', str(resp.data))
        self.assertTrue(LeaveRequest.objects.filter(pk=lr.pk).exists())
        balance = self.reload_balance()
        self.assertEqual(balance.used_days, Decimal('3'))
        self.assertEqual(balance.pending_days, Decimal('0'))

    @_patch_feature
    def test_employee_delete_pending_releases_days(self):
        lr = self.make_request()
        resp = self.api_delete(f'{EMPLOYEE_URL}{lr.pk}/', user=self.employee)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(LeaveRequest.objects.filter(pk=lr.pk).exists())
        self.assertEqual(self.reload_balance().pending_days, Decimal('0'))

    @_patch_feature
    def test_employee_edit_with_stale_copy_does_not_revert_approval(self):
        lr = self.make_request()
        stale = _stale(lr)
        self.approve(lr, self.admin)

        with patch.object(EmployeeLeaveRequestViewSet, 'get_object', return_value=stale):
            resp = self.api_patch(f'{EMPLOYEE_URL}{lr.pk}/', {'reason': 'edited'}, user=self.employee)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Only pending', str(resp.data))
        lr.refresh_from_db()
        self.assertEqual(lr.status, 'approved')


# ═══════════════════════════════════════════════════════════
#  2. Carry forward first-run race
# ═══════════════════════════════════════════════════════════

class TestCarryForwardRace(AdminFixesBase):

    def setUp(self):
        super().setUp()
        self.year = date.today().year - 1
        self.create_leave_balance(
            self.manager, self.leave_type, year=self.year,
            allocated_days=Decimal('20'), used_days=Decimal('10'),
        )

    def carry(self):
        return self.api_post(
            f'{BALANCE_URL}carry_forward/',
            {'from_year': self.year, 'to_year': self.year + 1}, user=self.admin,
        )

    @_patch_feature
    def test_losing_first_run_returns_already_run_not_500(self):
        """Reproduction: a concurrent first run created the row after our lookup saw none."""
        winner = LeaveCarryForwardRun.objects.create(
            tenant=self.tenant, from_year=self.year, to_year=self.year + 1,
            balances_count=1, total_days=Decimal('5'),
        )
        with patch('leave_management.utils._locked_carry_forward_run', return_value=None):
            resp = self.carry()

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data['already_run'])
        self.assertEqual(resp.data['carried_forward'], 0)
        self.assertEqual(Decimal(str(resp.data['total_days'])), winner.total_days)
        # Our writes were rolled back with the failed transaction
        self.assertFalse(LeaveBalance.objects.filter(
            user=self.manager, leave_type=self.leave_type, year=self.year + 1,
        ).exists())
        self.assertEqual(LeaveCarryForwardRun.objects.filter(tenant=self.tenant).count(), 1)

    @_patch_feature
    def test_integrity_error_without_visible_row_is_409(self):
        with patch.object(LeaveCarryForwardRun.objects, 'create', side_effect=IntegrityError('dup')):
            resp = self.carry()
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('error', resp.data)
        self.assertFalse(LeaveBalance.objects.filter(
            user=self.manager, leave_type=self.leave_type, year=self.year + 1,
        ).exists())


# ═══════════════════════════════════════════════════════════
#  3. Self-approval
# ═══════════════════════════════════════════════════════════

class TestSelfApproval(AdminFixesBase):

    def setUp(self):
        super().setUp()
        self.staff_emp = self.create_admin(email='fx-staff-emp@test.com')
        self.staff_balance = self.create_leave_balance(
            self.staff_emp, self.leave_type, pending_days=Decimal('3'),
        )
        self.own = self.create_leave_request(self.staff_emp, self.leave_type)

    def assert_untouched(self):
        self.own.refresh_from_db()
        self.assertEqual(self.own.status, 'pending')
        self.staff_balance.refresh_from_db()
        self.assertEqual(self.staff_balance.used_days, Decimal('0'))
        self.assertEqual(self.staff_balance.pending_days, Decimal('3'))

    @_patch_feature
    def test_staff_cannot_approve_own_on_admin_route(self):
        resp = self.approve(self.own, self.staff_emp)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('own leave request', str(resp.data['detail']))
        self.assert_untouched()

    @_patch_feature
    def test_staff_cannot_reject_own_on_admin_route(self):
        resp = self.api_post(
            f'{REQUEST_URL}{self.own.pk}/reject/', {'action': 'reject', 'comments': 'x'},
            user=self.staff_emp,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assert_untouched()

    @_patch_feature
    def test_hr_cannot_approve_own_hr_step(self):
        self.create_leave_settings(require_manager_approval=True, require_hr_approval=True)
        self.assertEqual(self.approve(self.own, self.admin).data['status'], 'manager_approved')
        resp = self.approve(self.own, self.staff_emp)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.own.refresh_from_db()
        self.assertEqual(self.own.status, 'manager_approved')

    @_patch_feature
    def test_cannot_approve_or_reject_own_on_manager_route(self):
        for verb in ('approve', 'reject'):
            resp = self.api_post(
                f'{MANAGER_URL}{self.own.pk}/{verb}/', {'action': verb, 'comments': 'x'},
                user=self.staff_emp,
            )
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, verb)
        self.assert_untouched()

    @_patch_feature
    def test_superuser_may_approve_own(self):
        boss = self.create_user(
            email='fx-super@test.com', role='admin', is_staff=True, is_superuser=True,
        )
        self.create_leave_balance(boss, self.leave_type, pending_days=Decimal('3'))
        own = self.create_leave_request(boss, self.leave_type)
        resp = self.approve(own, boss)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'approved')

    @_patch_feature
    def test_other_staff_can_still_approve(self):
        resp = self.approve(self.own, self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


# ═══════════════════════════════════════════════════════════
#  4. Chain steps follow level order
# ═══════════════════════════════════════════════════════════

class TestChainLevelOrder(AdminFixesBase):

    def setUp(self):
        super().setUp()
        # Stored out of the default order: HR first, then the manager
        self.create_approval_chain(level=1, approver_role='hr')
        self.create_approval_chain(level=2, approver_role='manager')

    def test_next_role_follows_levels(self):
        lr = self.make_request()
        self.assertEqual(get_next_approver_role(lr), 'hr')
        lr.status = 'hr_approved'
        lr.hr_approver = self.hr
        self.assertEqual(get_next_approver_role(lr), 'manager')
        lr.status = 'manager_approved'
        lr.manager_approver = self.manager
        self.assertIsNone(get_next_approver_role(lr))

    def test_role_repeated_around_another_role_does_not_skip_it(self):
        """manager@1, hr@2, manager@3: HR must sign after the first manager step."""
        lt = self.create_leave_type(code='MHM', created_by=self.admin, updated_by=self.admin)
        self.create_approval_chain(leave_type=lt, level=1, approver_role='manager')
        self.create_approval_chain(leave_type=lt, level=2, approver_role='hr')
        self.create_approval_chain(leave_type=lt, level=3, approver_role='manager')
        lr = self.create_leave_request(self.employee, lt)
        self.assertEqual(get_next_approver_role(lr), 'manager')
        lr.status = 'manager_approved'
        lr.manager_approver = self.manager
        self.assertEqual(get_next_approver_role(lr), 'hr')  # used to be None
        lr.status = 'hr_approved'
        lr.hr_approver = self.hr
        # The repeated manager role needs only one sign-off
        self.assertIsNone(get_next_approver_role(lr))

    @_patch_feature
    def test_role_repeated_around_another_role_end_to_end(self):
        lt = self.create_leave_type(code='MHX', created_by=self.admin, updated_by=self.admin)
        self.create_approval_chain(leave_type=lt, level=1, approver_role='manager')
        self.create_approval_chain(leave_type=lt, level=2, approver_role='hr')
        self.create_approval_chain(leave_type=lt, level=3, approver_role='manager')
        balance = self.create_leave_balance(self.employee, lt, pending_days=Decimal('3'))
        lr = self.create_leave_request(self.employee, lt, total_days=Decimal('3'))

        resp = self.api_post(f'{MANAGER_URL}{lr.pk}/approve/', {'action': 'approve'}, user=self.manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'manager_approved')  # NOT approved
        balance.refresh_from_db()
        self.assertEqual(balance.used_days, Decimal('0'))

        resp = self.approve(lr, self.hr)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'approved')
        lr.refresh_from_db()
        self.assertEqual(lr.manager_approver, self.manager)
        self.assertEqual(lr.hr_approver, self.hr)
        balance.refresh_from_db()
        self.assertEqual(balance.used_days, Decimal('3'))
        self.assertEqual(balance.pending_days, Decimal('0'))

    def test_chain_edited_in_flight_resumes_at_first_unsigned_step(self):
        """Signed manager@1 under manager, hr; chain then becomes hr@1, manager@2."""
        lr = self.make_request(status='manager_approved', manager_approver=self.manager)
        self.assertEqual(get_next_approver_role(lr), 'hr')  # used to be None

    @_patch_feature
    def test_submitted_notification_goes_to_first_step_approvers(self):
        """With HR first, the manager is not told to act on a new request."""
        lr = self.make_request()
        self.assertFalse(self.notifs(self.manager, 'leave_request_submitted').exists())
        hr_notif = self.notifs(self.hr, 'leave_request_submitted').first()
        self.assertIsNotNone(hr_notif)
        self.assertIn(str(lr.pk), hr_notif.link_url)

    @_patch_feature
    def test_hr_then_manager_both_required(self):
        lr = self.make_request()

        # The manager cannot go first
        resp = self.api_post(f'{MANAGER_URL}{lr.pk}/approve/', {'action': 'approve'}, user=self.manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.approve(lr, self.hr)
        self.assertEqual(resp.data['status'], 'hr_approved')
        self.assertEqual(self.reload_balance().used_days, Decimal('0'))

        # The manager step is NOT skipped (role rank used to skip it)
        resp = self.api_post(f'{MANAGER_URL}{lr.pk}/approve/', {'action': 'approve'}, user=self.manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'approved')

        lr.refresh_from_db()
        self.assertEqual(lr.hr_approver, self.hr)
        self.assertEqual(lr.manager_approver, self.manager)
        balance = self.reload_balance()
        self.assertEqual(balance.used_days, Decimal('3'))
        self.assertEqual(balance.pending_days, Decimal('0'))

    @_patch_feature
    def test_manager_can_reject_at_manager_step(self):
        lr = self.make_request()
        self.approve(lr, self.hr)
        resp = self.api_post(
            f'{MANAGER_URL}{lr.pk}/reject/', {'action': 'reject', 'comments': 'no'}, user=self.manager,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'rejected')

    def test_default_order_unchanged(self):
        LeaveRequest.objects.none()
        lt = self.create_leave_type(code='STD', created_by=self.admin, updated_by=self.admin)
        self.create_approval_chain(leave_type=lt, level=1, approver_role='manager')
        self.create_approval_chain(leave_type=lt, level=2, approver_role='hr')
        self.create_approval_chain(leave_type=lt, level=3, approver_role='admin')
        lr = self.create_leave_request(self.employee, lt)
        self.assertEqual(get_next_approver_role(lr), 'manager')
        lr.status = 'manager_approved'
        lr.manager_approver = self.manager
        self.assertEqual(get_next_approver_role(lr), 'hr')
        lr.status = 'hr_approved'
        lr.hr_approver = self.hr
        self.assertEqual(get_next_approver_role(lr), 'admin')
        for closed in ('approved', 'rejected', 'cancelled'):
            lr.status = closed
            self.assertIsNone(get_next_approver_role(lr))

    def test_status_role_missing_from_edited_chain_resumes_at_first_unsigned(self):
        lt = self.create_leave_type(code='EDT', created_by=self.admin, updated_by=self.admin)
        self.create_approval_chain(leave_type=lt, level=1, approver_role='hr')
        self.create_approval_chain(leave_type=lt, level=2, approver_role='admin')
        lr = self.create_leave_request(self.employee, lt, status='manager_approved')
        self.assertEqual(get_next_approver_role(lr), 'hr')


# ═══════════════════════════════════════════════════════════
#  5. Schema
# ═══════════════════════════════════════════════════════════

class TestLeaveActionSchema(AdminFixesBase):
    _schema = None

    @classmethod
    def schema(cls):
        if cls._schema is None:
            cls._schema = TenantAwareSchemaGenerator().get_schema(request=None, public=True)
        return cls._schema

    def ref(self, op, code):
        return op['responses'][code]['content']['application/json']['schema']['$ref'].rsplit('/', 1)[-1]

    def test_manager_actions_return_action_response(self):
        paths = self.schema()['paths']
        for verb in ('approve', 'reject'):
            op = paths[f'/api/leave/manager/team-requests/{{id}}/{verb}/']['post']
            self.assertEqual(self.ref(op, '200'), 'LeaveActionResponse', verb)
            self.assertEqual(self.ref(op, '400'), 'LeaveActionErrorResponse', verb)
            self.assertEqual(self.ref(op, '403'), 'LeaveForbiddenResponse', verb)

    def test_admin_actions_document_403_and_field_errors(self):
        paths = self.schema()['paths']
        for verb in ('approve', 'reject', 'cancel'):
            op = paths[f'/api/leave/admin/leave-requests/{{id}}/{verb}/']['post']
            self.assertEqual(self.ref(op, '200'), 'LeaveActionResponse', verb)
            self.assertEqual(self.ref(op, '400'), 'LeaveActionErrorResponse', verb)
            self.assertEqual(self.ref(op, '403'), 'LeaveForbiddenResponse', verb)
        props = self.schema()['components']['schemas']['LeaveActionErrorResponse']['properties']
        for field in ('error', 'action', 'comments'):
            self.assertIn(field, props)
        self.assertEqual(props['comments']['type'], 'array')

    def test_carry_forward_documents_409(self):
        op = self.schema()['paths']['/api/leave/admin/leave-balances/carry_forward/']['post']
        self.assertIn('409', op['responses'])
