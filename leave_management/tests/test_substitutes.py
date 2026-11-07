"""B2: substitutes and their consent (ported from INSERVICE tests/access.mjs)."""
from datetime import date
from decimal import Decimal

from rest_framework import status

from leave_management.models import (
    LeaveRequest, LeaveBalance, SubstituteAssignment, LeaveAuditEvent,
)
from leave_management.tests.hr_base import HrTestCase
from users.models import Notification

MY_REQUESTS = '/api/leave/employee/my-requests/'
MY_SUBS = '/api/leave/employee/my-substitutes/'
COVERING = '/api/leave/employee/covering/'
ADMIN_REQ = '/api/leave/admin/leave-requests/'
TEAM = '/api/leave/manager/team-requests/'


class SubstituteBase(HrTestCase):

    def setUp(self):
        super().setUp()
        self.settings_with(substitutes_enabled=True)
        self.admin = self.create_admin(email='sub-admin@test.com')
        self.m = self.create_user(email='sub-m@test.com')
        self.n = self.create_user(email='sub-n@test.com')
        self.a = self.create_user(email='sub-a@test.com', manager=self.m)
        self.b = self.create_user(email='sub-b@test.com', manager=self.n)
        self.leave = self.create_leave_type(code='LEAVE', requires_substitutes=True)
        self.dayoff = self.create_leave_type(code='OFF')
        for u in (self.a, self.b, self.n, self.m):
            self.create_leave_balance(u, self.leave, year=2027)
            self.create_leave_balance(u, self.dayoff, year=2027)

    def save_my_subs(self, user, items):
        return self.api_put(f'{MY_SUBS}replace/', {'items': items}, user=user)

    def request_leave(self, user, start='2027-10-14', end='2027-10-15', **extra):
        return self.api_post(MY_REQUESTS, {
            'leave_type': extra.pop('leave_type', self.leave.id),
            'start_date': start, 'end_date': end, **extra,
        }, user=user)

    def answer(self, req_id, user, action='accept', comment=''):
        return self.api_post(f'{COVERING}{req_id}/respond/', {'action': action, 'comment': comment}, user=user)


class TestMySubstitutes(SubstituteBase):

    def test_validation_rules(self):
        ok = self.save_my_subs(self.a, [
            {'substitute': self.b.id, 'task': 'Clients'},
            {'substitute': self.n.id, 'task': 'Operations'},
        ])
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual(len(ok.data), 2)
        for bad in (
            [{'substitute': self.a.id, 'task': 'Self'}],
            [{'substitute': self.b.id, 'task': 'x'}, {'substitute': self.b.id, 'task': 'y'}],
            [{'substitute': self.b.id, 'task': '   '}],
            [{'substitute': self.b.id, 'task': 'x' * 301}],
        ):
            self.assertEqual(self.save_my_subs(self.a, bad).status_code, 400, bad)
        self.b.is_active = False
        self.b.save()
        self.assertEqual(self.save_my_subs(self.a, [{'substitute': self.b.id, 'task': 'C'}]).status_code, 400)

    def test_only_own_list(self):
        self.save_my_subs(self.a, [{'substitute': self.b.id, 'task': 'Clients'}])
        resp = self.api_get(MY_SUBS, user=self.admin)
        self.assertEqual(resp.data, [])
        row = SubstituteAssignment.objects.get(owner=self.a)
        self.assertEqual(self.api_patch(f'{MY_SUBS}{row.id}/', {'task': 'X'}, user=self.admin).status_code, 404)

    def test_feature_off_is_403(self):
        self.settings_with(substitutes_enabled=False)
        self.assertEqual(self.api_get(MY_SUBS, user=self.a).status_code, 403)


class TestConsentFlow(SubstituteBase):

    def setUp(self):
        super().setUp()
        self.save_my_subs(self.a, [
            {'substitute': self.b.id, 'task': 'Clients'},
            {'substitute': self.n.id, 'task': 'Operations'},
        ])

    def test_all_must_accept_then_manager_approves(self):
        resp = self.request_leave(self.a)
        self.assertEqual(resp.status_code, 201, resp.data)
        req = LeaveRequest.objects.get(employee=self.a)
        self.assertEqual(req.status, 'awaiting_substitutes')
        self.assertEqual(req.substitutes.count(), 2)
        self.assertEqual(LeaveBalance.objects.get(user=self.a, leave_type=self.leave).pending_days, Decimal('2'))
        # substitutes are notified, the manager is not yet
        self.assertTrue(Notification.objects.filter(user=self.b, notification_type='leave_substitute_requested').exists())
        self.assertFalse(Notification.objects.filter(user=self.m, notification_type='leave_request_submitted').exists())
        # manager cannot approve yet
        early = self.api_post(f'{TEAM}{req.id}/approve/', {'action': 'approve'}, user=self.m)
        self.assertNotEqual(early.status_code, 200)

        listing = self.api_get(COVERING, user=self.b)
        self.assertEqual([r['id'] for r in self.get_results(listing)], [req.id])

        self.assertEqual(self.answer(req.id, self.b).status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.status, 'awaiting_substitutes')
        twice = self.answer(req.id, self.b)
        self.assertEqual(twice.status_code, 400)

        self.assertEqual(self.answer(req.id, self.n).status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.status, 'pending')
        self.assertTrue(Notification.objects.filter(user=self.m, notification_type='leave_request_submitted').exists())

        approve = self.api_post(f'{TEAM}{req.id}/approve/', {'action': 'approve'}, user=self.m)
        self.assertEqual(approve.status_code, 200, approve.data)
        req.refresh_from_db()
        self.assertEqual(req.status, 'approved')
        actions = list(LeaveAuditEvent.objects.filter(leave_request=req).values_list('action', flat=True))
        self.assertIn('accepted', actions)

    def test_one_decline_rejects_and_releases_balance(self):
        self.request_leave(self.a)
        req = LeaveRequest.objects.get(employee=self.a)
        self.assertEqual(self.answer(req.id, self.b, 'reject').status_code, 400)  # reason required
        resp = self.answer(req.id, self.b, 'reject', 'I am travelling')
        self.assertEqual(resp.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.status, 'rejected')
        self.assertEqual(req.rejected_by, self.b)
        self.assertEqual(LeaveBalance.objects.get(user=self.a, leave_type=self.leave).pending_days, 0)
        self.assertTrue(Notification.objects.filter(user=self.a, notification_type='leave_request_rejected').exists())

    def test_outsider_cannot_answer(self):
        self.request_leave(self.a)
        req = LeaveRequest.objects.get(employee=self.a)
        self.assertEqual(self.answer(req.id, self.m).status_code, 404)

    def test_snapshot_survives_profile_edits(self):
        self.request_leave(self.a)
        req = LeaveRequest.objects.get(employee=self.a)
        self.save_my_subs(self.a, [{'substitute': self.m.id, 'task': 'Everything'}])
        self.assertEqual(
            sorted(req.substitutes.values_list('substitute_id', flat=True)), sorted([self.b.id, self.n.id])
        )
        detail = self.api_get(f'{COVERING}{req.id}/', user=self.n)
        self.assertEqual(detail.data['substitutes'][1]['task'], 'Operations')

    def test_types_without_flag_skip_consent(self):
        resp = self.request_leave(self.a, '2027-11-04', '2027-11-04', leave_type=self.dayoff.id)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(LeaveRequest.objects.get(employee=self.a).status, 'pending')

    def test_substitute_on_approved_leave_refused(self):
        self.create_leave_request(self.b, self.dayoff, start_date=date(2027, 10, 15),
                                  end_date=date(2027, 10, 15), total_days=Decimal('1'), status='approved')
        resp = self.request_leave(self.a)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('approved leave', str(resp.data))

    def test_confirmed_substitute_cannot_request_overlapping(self):
        self.request_leave(self.a)
        req = LeaveRequest.objects.get(employee=self.a)
        self.answer(req.id, self.b)
        self.answer(req.id, self.n)
        resp = self.request_leave(self.b, '2027-10-15', '2027-10-15', leave_type=self.dayoff.id)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('confirmed substitute', str(resp.data))

    def test_empty_profile_requires_body_substitutes(self):
        self.assertEqual(self.request_leave(self.b).status_code, 400)
        resp = self.request_leave(self.b, substitutes=[{'substitute': self.a.id, 'task': 'Warehouse'}])
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(LeaveRequest.objects.get(employee=self.b).substitutes.get().task, 'Warehouse')

    def test_admin_skip_and_manager_cannot(self):
        self.request_leave(self.a)
        req = LeaveRequest.objects.get(employee=self.a)
        self.answer(req.id, self.b)
        url = f'{ADMIN_REQ}{req.id}/skip-substitutes/'
        self.assertEqual(self.api_post(url, {'reason': 'x'}, user=self.m).status_code, 403)
        self.assertEqual(self.api_post(url, {}, user=self.admin).status_code, 400)
        resp = self.api_post(url, {'reason': 'Urgent family matter'}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        req.refresh_from_db()
        self.assertEqual(req.status, 'pending')
        self.assertEqual(req.substitutes.get(substitute=self.n).status, 'skipped')
        self.assertTrue(LeaveAuditEvent.objects.filter(leave_request=req, action='skipped').exists())

    def test_cancel_while_waiting_releases_and_notifies(self):
        self.request_leave(self.a)
        req = LeaveRequest.objects.get(employee=self.a)
        resp = self.api_post(f'{MY_REQUESTS}{req.id}/cancel/', {'reason': ''}, user=self.a)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(LeaveBalance.objects.get(user=self.a, leave_type=self.leave).pending_days, 0)
        self.assertTrue(Notification.objects.filter(user=self.b, notification_type='leave_request_cancelled').exists())

    def test_edit_dates_resets_consent(self):
        self.request_leave(self.a)
        req = LeaveRequest.objects.get(employee=self.a)
        self.answer(req.id, self.b)
        self.answer(req.id, self.n)
        resp = self.api_patch(f'{MY_REQUESTS}{req.id}/', {'end_date': '2027-10-18'}, user=self.a)
        self.assertEqual(resp.status_code, 200, resp.data)
        req.refresh_from_db()
        self.assertEqual(req.status, 'awaiting_substitutes')
        self.assertEqual(set(req.substitutes.values_list('status', flat=True)), {'pending'})

    def test_feature_off_behaves_as_before(self):
        self.settings_with(substitutes_enabled=False)
        resp = self.request_leave(self.a)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(LeaveRequest.objects.get(employee=self.a).status, 'pending')

    def test_profile_pending_count(self):
        self.request_leave(self.a)
        resp = self.api_get('/api/auth/profile/', user=self.b)
        self.assertEqual(resp.data['leave_features']['pending_substitute_count'], 1)
