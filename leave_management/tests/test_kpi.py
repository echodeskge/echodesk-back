"""B3: monthly KPIs (ported from INSERVICE tests/access.mjs KPI block)."""
from rest_framework import status

from leave_management.models import KpiGoal, LeaveAuditEvent
from leave_management.tests.hr_base import HrTestCase
from users.models import Notification

KPIS = '/api/leave/kpis/'


class TestKpi(HrTestCase):

    def setUp(self):
        super().setUp()
        self.settings_with(kpi_enabled=True)
        self.admin = self.create_admin(email='kpi-admin@test.com')
        self.m = self.create_user(email='kpi-m@test.com')
        self.n = self.create_user(email='kpi-n@test.com')
        self.a = self.create_user(email='kpi-a@test.com', manager=self.m)

    def create_goal(self, user=None, **extra):
        body = {'employee': self.a.id, 'period': '2026-10', 'title': 'Calls handled',
                'target': '10', 'unit': 'calls', 'direction': 'higher', **extra}
        return self.api_post(KPIS, body, user=user or self.m)

    def test_full_workflow(self):
        resp = self.create_goal()
        self.assertEqual(resp.status_code, 201, resp.data)
        k = resp.data['id']
        self.assertTrue(Notification.objects.filter(user=self.a, notification_type='kpi_assigned').exists())

        # employee cannot edit the target or approve themselves
        self.assertEqual(self.api_patch(f'{KPIS}{k}/', {'target': '5'}, user=self.a).status_code, 403)
        self.assertEqual(self.api_post(f'{KPIS}{k}/approve/', {}, user=self.a).status_code, 403)
        # outsider sees nothing
        self.assertEqual(self.api_post(f'{KPIS}{k}/submit/', {'actual': 1}, user=self.n).status_code, 404)

        sub = self.api_post(f'{KPIS}{k}/submit/', {'actual': '8', 'note': 'busy'}, user=self.a)
        self.assertEqual(sub.status_code, 200, sub.data)
        self.assertEqual(sub.data['status'], 'pending')
        self.assertEqual(sub.data['percent'], '80.0')
        self.assertTrue(Notification.objects.filter(user=self.m, notification_type='kpi_submitted').exists())

        self.assertEqual(self.api_post(f'{KPIS}{k}/return/', {}, user=self.m).status_code, 400)
        ret = self.api_post(f'{KPIS}{k}/return/', {'feedback': 'Check the CRM log'}, user=self.m)
        self.assertEqual(ret.data['status'], 'returned')

        self.api_post(f'{KPIS}{k}/submit/', {'actual': '9'}, user=self.a)
        appr = self.api_post(f'{KPIS}{k}/approve/', {}, user=self.m)
        self.assertEqual(appr.data['status'], 'approved')
        # resubmitting after approval needs review again
        again = self.api_post(f'{KPIS}{k}/submit/', {'actual': '10'}, user=self.a)
        self.assertEqual(again.data['status'], 'pending')
        self.assertTrue(again.data['target_met'])

        detail = self.api_get(f'{KPIS}{k}/', user=self.a)
        self.assertGreaterEqual(len(detail.data['history']), 6)
        self.assertEqual(self.api_get(f'{KPIS}?scope=team', user=self.n).data['results'], [])

    def test_target_edit_rules(self):
        k = self.create_goal().data['id']
        self.api_post(f'{KPIS}{k}/submit/', {'actual': '3'}, user=self.a)
        self.assertEqual(self.api_patch(f'{KPIS}{k}/', {'target': '20'}, user=self.m).status_code, 400)
        self.api_post(f'{KPIS}{k}/return/', {'feedback': 'redo'}, user=self.m)
        resp = self.api_patch(f'{KPIS}{k}/', {'target': '20'}, user=self.m)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'draft')
        self.assertIsNone(resp.data['actual'])
        self.assertEqual(self.api_patch(f'{KPIS}{k}/', {'period': '2026-11'}, user=self.m).status_code, 400)

    def test_no_self_targets_and_validation(self):
        self.assertEqual(self.create_goal(user=self.a).status_code, 403)
        self.assertEqual(self.create_goal(user=self.n).status_code, 403)
        self.assertEqual(self.create_goal(period='2026-13').status_code, 400)
        self.assertEqual(self.create_goal(target='0').status_code, 400)
        self.assertEqual(self.create_goal(title='  ').status_code, 400)
        # staff may set targets for others, not for themselves
        self.assertEqual(self.create_goal(user=self.admin).status_code, 201)
        self.assertEqual(self.create_goal(user=self.admin, employee=self.admin.id).status_code, 403)

    def test_lower_is_better_percent(self):
        k = self.create_goal(direction='lower', target='5', title='Complaints').data['id']
        r = self.api_post(f'{KPIS}{k}/submit/', {'actual': '0'}, user=self.a)
        self.assertIsNone(r.data['percent'])
        self.assertTrue(r.data['target_met'])
        r = self.api_post(f'{KPIS}{k}/submit/', {'actual': '10'}, user=self.a)
        self.assertEqual(r.data['percent'], '50.0')

    def test_delete_only_drafts(self):
        k = self.create_goal().data['id']
        self.api_post(f'{KPIS}{k}/submit/', {'actual': '3'}, user=self.a)
        self.assertEqual(self.api_delete(f'{KPIS}{k}/', user=self.m).status_code, 403)
        k2 = self.create_goal(title='Other').data['id']
        self.assertEqual(self.api_delete(f'{KPIS}{k2}/', user=self.m).status_code, 204)

    def test_scopes_and_copy_previous(self):
        self.create_goal()
        self.assertEqual(len(self.api_get(KPIS, user=self.a).data['results']), 1)
        self.assertEqual(len(self.api_get(f'{KPIS}?scope=team', user=self.m).data['results']), 1)
        self.assertEqual(self.api_get(f'{KPIS}?scope=all', user=self.m).status_code, 403)
        self.assertEqual(len(self.api_get(f'{KPIS}?scope=all', user=self.admin).data['results']), 1)
        copied = self.api_post(f'{KPIS}copy-previous/', {'period': '2026-11'}, user=self.m)
        self.assertEqual(copied.status_code, 200, copied.data)
        self.assertEqual(len(copied.data), 1)
        again = self.api_post(f'{KPIS}copy-previous/', {'period': '2026-11'}, user=self.m)
        self.assertEqual(len(again.data), 0)
        self.assertEqual(KpiGoal.objects.filter(period='2026-11').get().status, 'draft')

    def test_feature_off(self):
        self.settings_with(kpi_enabled=False)
        self.assertEqual(self.api_get(KPIS, user=self.a).status_code, status.HTTP_403_FORBIDDEN)
