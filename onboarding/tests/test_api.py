"""GET /api/onboarding/ and the apply / skip / reset actions."""
from onboarding.models import TenantOnboarding
from users.tests.conftest import EchoDeskTenantTestCase

from .helpers import FAKE_FEATURE, FakeStep, FakeStepMixin, enable_features

ONB = '/api/onboarding/'


class OnboardingApiTests(FakeStepMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.agent = self.create_user(email='agent@test.com')

    def _get(self, user=None):
        return self.api_get(ONB, user=user or self.admin)

    # ── permissions ──

    def test_non_admin_gets_403_everywhere(self):
        self.assertEqual(self._get(self.agent).status_code, 403)
        for action in ('apply', 'skip', 'reset'):
            resp = self.api_post(f'{ONB}workspace/{action}/', {'answers': {}}, user=self.agent)
            self.assertEqual(resp.status_code, 403, action)

    def test_anonymous_is_rejected(self):
        self.assertIn(self.api_get(ONB).status_code, (401, 403))

    def test_manage_settings_flag_is_enough(self):
        settings_user = self.create_user(email='settings@test.com', can_manage_settings=True)
        self.assertEqual(self._get(settings_user).status_code, 200)

    def test_admin_role_is_enough(self):
        role_admin = self.create_user(email='roleadmin@test.com', role='admin')
        self.assertEqual(self._get(role_admin).status_code, 200)

    # ── GET shape ──

    def test_get_shape(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        body = resp.data
        self.assertEqual(
            set(body.keys()),
            {'industry', 'started_at', 'finished_at', 'pending_count', 'total', 'steps'},
        )
        self.assertIsNone(body['industry'])
        self.assertIsNone(body['started_at'])
        self.assertIsNone(body['finished_at'])
        self.assertEqual(body['total'], 1)
        self.assertEqual(body['pending_count'], 1)
        step = body['steps'][0]
        self.assertEqual(
            set(step.keys()),
            {'key', 'feature_key', 'order', 'status', 'answers', 'prefill', 'context', 'completed_at'},
        )
        self.assertEqual(step['key'], 'workspace')
        self.assertIsNone(step['feature_key'])
        self.assertEqual(step['order'], 0)
        self.assertEqual(step['status'], 'pending')
        self.assertEqual(step['answers'], {})
        self.assertEqual(step['prefill']['company_name'], self.tenant.name)
        self.assertEqual(step['prefill']['preferred_language'], self.tenant.preferred_language)
        industries = step['context']['industries']
        self.assertIn('salon', [i['key'] for i in industries])
        self.assertTrue(all(i['label']['en'] and i['label']['ka'] for i in industries))
        self.assertIsNone(step['completed_at'])

    def test_feature_step_only_listed_when_feature_enabled(self):
        self.assertEqual([s['key'] for s in self._get().data['steps']], ['workspace'])
        self.assertEqual(self.api_post(f'{ONB}fake/skip/', user=self.admin).status_code, 404)

        enable_features(self.tenant, FAKE_FEATURE)
        body = self._get().data
        self.assertEqual([s['key'] for s in body['steps']], ['workspace', 'fake'])
        fake = body['steps'][1]
        self.assertEqual(fake['feature_key'], FAKE_FEATURE)
        self.assertEqual(fake['order'], 99)
        self.assertEqual(fake['status'], 'pending')
        self.assertEqual(fake['prefill'], {'name': 'suggested'})
        self.assertEqual(fake['context'], {'flag': True})
        self.assertEqual(body['total'], 2)
        self.assertEqual(body['pending_count'], 2)

    def test_feature_without_registered_step_is_ignored(self):
        enable_features(self.tenant, 'feature_with_no_step')
        self.assertEqual([s['key'] for s in self._get().data['steps']], ['workspace'])

    def test_unknown_step_is_404(self):
        for action in ('apply', 'skip', 'reset'):
            resp = self.api_post(f'{ONB}nope/{action}/', {'answers': {}}, user=self.admin)
            self.assertEqual(resp.status_code, 404, action)

    # ── apply ──

    def test_apply_validates_answers(self):
        resp = self.api_post(f'{ONB}workspace/apply/', {'answers': {}}, user=self.admin)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('company_name', resp.data['errors'])
        self.assertIn('industry', resp.data['errors'])

        resp = self.api_post(f'{ONB}workspace/apply/', {'answers': 'nope'}, user=self.admin)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('answers', resp.data['errors'])
        self.assertEqual(self._get().data['steps'][0]['status'], 'pending')

    def test_apply_persists_and_returns_result(self):
        enable_features(self.tenant, FAKE_FEATURE)
        resp = self.api_post(f'{ONB}fake/apply/', {'answers': {'name': 'x', 'extra': 1}}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['result'], {'created': {'things': 1}, 'updated': ['name'], 'notes': ['hello']})
        step = resp.data['step']
        self.assertEqual(step['status'], 'done')
        self.assertEqual(step['answers'], {'name': 'x'})  # validated, unknown keys dropped
        self.assertIsNotNone(step['completed_at'])
        self.assertEqual(FakeStep.applied, [{'name': 'x'}])

        body = self._get().data
        self.assertIsNotNone(body['started_at'])
        self.assertIsNone(body['finished_at'])  # workspace still pending
        self.assertEqual(body['pending_count'], 1)
        state = TenantOnboarding.get()
        self.assertEqual(state.status_of('fake'), 'done')
        self.assertEqual(state.answers_of('fake'), {'name': 'x'})

    def test_apply_twice_is_fine(self):
        enable_features(self.tenant, FAKE_FEATURE)
        for _ in range(2):
            resp = self.api_post(f'{ONB}fake/apply/', {'answers': {'name': 'x'}}, user=self.admin)
            self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(FakeStep.applied), 2)
        self.assertEqual(TenantOnboarding.objects.count(), 1)

    # ── skip / reset / finished_at ──

    def test_skip_then_reset(self):
        resp = self.api_post(f'{ONB}workspace/skip/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['step']['status'], 'skipped')
        self.assertIsNotNone(resp.data['step']['completed_at'])
        body = self._get().data
        self.assertIsNotNone(body['started_at'])
        self.assertIsNotNone(body['finished_at'])  # nothing pending any more
        self.assertEqual(body['pending_count'], 0)

        resp = self.api_post(f'{ONB}workspace/reset/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['step']['status'], 'pending')
        self.assertIsNone(resp.data['step']['completed_at'])
        body = self._get().data
        self.assertIsNone(body['finished_at'])
        self.assertIsNotNone(body['started_at'])
        self.assertEqual(body['pending_count'], 1)

    def test_reset_keeps_answers(self):
        enable_features(self.tenant, FAKE_FEATURE)
        self.api_post(f'{ONB}fake/apply/', {'answers': {'name': 'kept'}}, user=self.admin)
        resp = self.api_post(f'{ONB}fake/reset/', user=self.admin)
        self.assertEqual(resp.data['step']['status'], 'pending')
        self.assertEqual(resp.data['step']['answers'], {'name': 'kept'})

    def test_finished_only_when_no_step_pending(self):
        enable_features(self.tenant, FAKE_FEATURE)
        self.api_post(f'{ONB}workspace/skip/', user=self.admin)
        self.assertIsNone(self._get().data['finished_at'])
        self.api_post(f'{ONB}fake/apply/', {'answers': {'name': 'x'}}, user=self.admin)
        body = self._get().data
        self.assertIsNotNone(body['finished_at'])
        self.assertEqual(body['pending_count'], 0)
        self.assertEqual(body['total'], 2)

    def test_newly_enabled_feature_reopens_onboarding(self):
        self.api_post(f'{ONB}workspace/skip/', user=self.admin)
        self.assertIsNotNone(self._get().data['finished_at'])
        enable_features(self.tenant, FAKE_FEATURE)
        body = self._get().data
        self.assertIsNone(body['finished_at'])
        self.assertEqual(body['pending_count'], 1)
        self.assertEqual(body['steps'][1]['status'], 'pending')
