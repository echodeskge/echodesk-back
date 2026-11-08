"""Deactivation / delete guard for users with open leave work."""
from datetime import date
from decimal import Decimal

from rest_framework import status

from leave_management.tests.hr_base import HrTestCase

USERS = '/api/users/'


class TestDeactivationGuard(HrTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='guard-admin@test.com')
        self.manager = self.create_user(email='guard-mgr@test.com')
        self.emp = self.create_user(email='guard-emp@test.com', manager=self.manager)
        self.lt = self.create_leave_type()

    def deactivate(self, user):
        return self.api_patch(f'{USERS}{user.id}/', {'is_active': False}, user=self.admin)

    def test_open_request_blocks_deactivation(self):
        req = self.create_leave_request(self.emp, self.lt)
        resp = self.deactivate(self.emp)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)
        self.assertEqual(resp.data['code'], 'leave_removal_blocked')
        self.assertEqual(resp.data['reason_codes'], [{'code': 'open_requests', 'count': 1}])
        req.status = 'approved'
        req.save()
        resp = self.deactivate(self.emp)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.emp.refresh_from_db()
        self.assertFalse(self.emp.is_active)
        self.assertIsNotNone(self.emp.employment_end_date)

    def test_manager_with_active_reports_blocked(self):
        resp = self.deactivate(self.manager)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('manager of 1', str(resp.data))
        self.assertEqual(resp.data['reason_codes'], [{'code': 'manager_of', 'count': 1}])

    def test_delete_with_history_blocked_but_clean_user_deletable(self):
        self.create_leave_request(self.emp, self.lt, status='approved')
        resp = self.api_delete(f'{USERS}{self.emp.id}/', user=self.admin)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['reason_codes'], [{'code': 'has_history', 'count': None}])
        clean = self.create_user(email='guard-clean@test.com')
        resp = self.api_delete(f'{USERS}{clean.id}/', user=self.admin)
        self.assertEqual(resp.status_code, 204)

    def test_bulk_deactivate_skips_blocked(self):
        self.create_leave_request(self.emp, self.lt)
        free = self.create_user(email='guard-free@test.com')
        resp = self.api_post(f'{USERS}bulk_action/', {
            'user_ids': [self.emp.id, free.id], 'action': 'deactivate',
        }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['affected'], 1)
        self.assertIn(str(self.emp.id), resp.data['blocked'])
        self.assertEqual(resp.data['blocked_codes'][str(self.emp.id)], [{'code': 'open_requests', 'count': 1}])
        free.refresh_from_db()
        self.assertFalse(free.is_active)

    def test_switch_off_allows(self):
        self.settings_with(block_deactivation_with_open_requests=False)
        self.create_leave_request(self.emp, self.lt)
        self.assertEqual(self.deactivate(self.emp).status_code, 200)
