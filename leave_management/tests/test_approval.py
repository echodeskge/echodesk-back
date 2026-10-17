"""
Tests for leave request approval workflow.
"""
from decimal import Decimal
from datetime import date, timedelta
from .conftest import LeaveTestCase
from leave_management.utils import get_next_approver_role, get_approval_chain


class TestApprovalChain(LeaveTestCase):
    """Test approval chain resolution."""

    def test_no_chain_returns_default_manager(self):
        lt = self.create_leave_type()
        employee = self.create_user(email='emp@test.com')
        request = self.create_leave_request(employee, lt)
        chain = get_approval_chain(request)
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0]['role'], 'manager')

    def test_settings_with_hr_approval(self):
        self.create_leave_settings(require_manager_approval=True, require_hr_approval=True)
        lt = self.create_leave_type()
        employee = self.create_user(email='emp@test.com')
        request = self.create_leave_request(employee, lt)
        chain = get_approval_chain(request)
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0]['role'], 'manager')
        self.assertEqual(chain[1]['role'], 'hr')

    def test_no_approval_needed(self):
        lt = self.create_leave_type(requires_approval=False)
        employee = self.create_user(email='emp@test.com')
        request = self.create_leave_request(employee, lt)
        chain = get_approval_chain(request)
        self.assertEqual(len(chain), 0)

    def test_custom_chain_overrides_settings(self):
        self.create_leave_settings(require_manager_approval=True)
        lt = self.create_leave_type()
        # Custom chain with HR only
        self.create_approval_chain(leave_type=lt, level=1, approver_role='hr')
        employee = self.create_user(email='emp@test.com')
        request = self.create_leave_request(employee, lt)
        chain = get_approval_chain(request)
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0]['role'], 'hr')


class TestNextApproverRole(LeaveTestCase):
    """Test next approver role determination."""

    def test_pending_needs_manager(self):
        self.create_leave_settings(require_manager_approval=True)
        lt = self.create_leave_type()
        employee = self.create_user(email='emp@test.com')
        request = self.create_leave_request(employee, lt, status='pending')
        role = get_next_approver_role(request)
        self.assertEqual(role, 'manager')

    def test_manager_approved_needs_hr(self):
        self.create_leave_settings(require_manager_approval=True, require_hr_approval=True)
        lt = self.create_leave_type()
        employee = self.create_user(email='emp@test.com')
        request = self.create_leave_request(employee, lt, status='manager_approved')
        role = get_next_approver_role(request)
        self.assertEqual(role, 'hr')

    def test_fully_approved_returns_none(self):
        self.create_leave_settings(require_manager_approval=True)
        lt = self.create_leave_type()
        employee = self.create_user(email='emp@test.com')
        request = self.create_leave_request(employee, lt, status='manager_approved')
        role = get_next_approver_role(request)
        # Only manager required, and it's already manager_approved
        self.assertIsNone(role)

    def test_no_approval_needed_returns_none(self):
        lt = self.create_leave_type(requires_approval=False)
        employee = self.create_user(email='emp@test.com')
        request = self.create_leave_request(employee, lt, status='pending')
        role = get_next_approver_role(request)
        self.assertIsNone(role)


    def test_closed_statuses_have_no_next_role(self):
        # 'approved' used to fall back to rank 0 and report 'manager', so
        # the final step of a 3-level chain never moved the balance.
        self.create_approval_chain(level=1, approver_role='manager')
        self.create_approval_chain(level=2, approver_role='hr')
        self.create_approval_chain(level=3, approver_role='admin')
        lt = self.create_leave_type()
        employee = self.create_user(email='emp@test.com')
        for closed in ('approved', 'rejected', 'cancelled'):
            request = self.create_leave_request(employee, lt, status=closed)
            self.assertIsNone(get_next_approver_role(request), closed)

    def test_three_level_chain_progress(self):
        self.create_approval_chain(level=1, approver_role='manager')
        self.create_approval_chain(level=2, approver_role='hr')
        self.create_approval_chain(level=3, approver_role='admin')
        lt = self.create_leave_type()
        employee = self.create_user(email='emp@test.com')
        expected = {'pending': 'manager', 'manager_approved': 'hr', 'hr_approved': 'admin'}
        for current, role in expected.items():
            request = self.create_leave_request(employee, lt, status=current)
            self.assertEqual(get_next_approver_role(request), role, current)

    def test_hr_only_settings_chain(self):
        self.create_leave_settings(require_manager_approval=False, require_hr_approval=True)
        lt = self.create_leave_type()
        employee = self.create_user(email='emp@test.com')
        pending = self.create_leave_request(employee, lt, status='pending')
        self.assertEqual(get_next_approver_role(pending), 'hr')
        done = self.create_leave_request(employee, lt, status='hr_approved')
        self.assertIsNone(get_next_approver_role(done))


class TestApprovalWorkflow(LeaveTestCase):
    """Test the full approval workflow via API."""
    pass
