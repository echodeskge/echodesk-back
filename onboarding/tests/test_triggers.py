"""Provisioning (process_pending_tenants) and feature-sync triggers."""
import io
from unittest.mock import MagicMock, patch

from onboarding import services
from onboarding.models import TenantOnboarding
from tenants.management.commands.process_pending_tenants import Command
from tenants.models import Tenant
from users.tests.conftest import EchoDeskTenantTestCase

from .helpers import FAKE_FEATURE, FakeStep, FakeStepMixin, disable_features, enable_features


class ProvisioningTriggerTests(FakeStepMixin, EchoDeskTenantTestCase):

    def test_initialize_creates_row_marks_pending_and_seeds_enabled_steps(self):
        enable_features(self.tenant, FAKE_FEATURE)
        TenantOnboarding.objects.all().delete()

        summary = services.initialize_for_tenant(self.tenant)

        self.assertEqual(TenantOnboarding.objects.count(), 1)
        state = TenantOnboarding.get()
        self.assertEqual(state.status_of('workspace'), 'pending')
        self.assertEqual(state.status_of('fake'), 'pending')
        self.assertIsNone(state.started_at)
        self.assertIsNone(state.finished_at)
        self.assertEqual(summary['pending'], ['workspace', 'fake'])
        self.assertEqual(summary['seeded'], ['workspace', 'fake'])
        self.assertEqual(summary['errors'], [])
        self.assertEqual(FakeStep.seeded, [self.tenant.schema_name])

    def test_initialize_ignores_steps_of_disabled_features(self):
        summary = services.initialize_for_tenant(self.tenant)
        state = TenantOnboarding.get()
        self.assertEqual(list(state.state.keys()), ['workspace'])
        self.assertEqual(summary['pending'], ['workspace'])
        self.assertEqual(FakeStep.seeded, [])

    def test_seed_failure_is_logged_not_raised(self):
        enable_features(self.tenant, FAKE_FEATURE)
        with patch.object(FakeStep, 'seed_defaults', side_effect=RuntimeError('boom')):
            summary = services.initialize_for_tenant(self.tenant)
        self.assertEqual(summary['errors'], ['fake'])
        self.assertEqual(summary['seeded'], ['workspace'])
        self.assertEqual(TenantOnboarding.get().status_of('fake'), 'pending')

    def test_initialize_twice_keeps_progress(self):
        services.initialize_for_tenant(self.tenant)
        TenantOnboarding.get().set_status('workspace', 'done', answers={'company_name': 'x'})
        services.initialize_for_tenant(self.tenant)
        state = TenantOnboarding.get()
        self.assertEqual(state.status_of('workspace'), 'done')
        self.assertEqual(state.answers_of('workspace'), {'company_name': 'x'})
        self.assertEqual(TenantOnboarding.objects.count(), 1)

    def test_process_tenant_runs_setup_onboarding_after_sync_features(self):
        command = Command(stdout=io.StringIO())
        calls = MagicMock()
        patches = [
            patch.object(Command, name, side_effect=lambda tenant, _n=name: calls(_n))
            for name in (
                'create_schema', 'run_migrations', 'create_admin_user', 'sync_features',
                'setup_onboarding', 'setup_frontend', 'send_welcome_email',
            )
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        command.process_tenant(self.tenant)

        names = [c.args[0] for c in calls.call_args_list]
        self.assertEqual(names, [
            'create_schema', 'run_migrations', 'create_admin_user', 'sync_features',
            'setup_onboarding', 'setup_frontend', 'send_welcome_email',
        ])
        self.assertEqual(Tenant.objects.get(pk=self.tenant.pk).deployment_status, 'deployed')

    def test_setup_onboarding_command_step_does_the_real_thing(self):
        enable_features(self.tenant, FAKE_FEATURE)
        TenantOnboarding.objects.all().delete()
        out = io.StringIO()
        Command(stdout=out).setup_onboarding(self.tenant)
        self.assertEqual(TenantOnboarding.get().status_of('fake'), 'pending')
        self.assertEqual(FakeStep.seeded, [self.tenant.schema_name])
        self.assertIn('2 step(s) pending', out.getvalue())

    def test_setup_onboarding_never_fails_provisioning(self):
        out = io.StringIO()
        with patch('onboarding.services.initialize_for_tenant', side_effect=RuntimeError('boom')):
            Command(stdout=out).setup_onboarding(self.tenant)  # no raise
        self.assertIn('skipped', out.getvalue())


class FeatureChangeTriggerTests(FakeStepMixin, EchoDeskTenantTestCase):

    def test_newly_enabled_feature_marks_its_step_pending(self):
        state = TenantOnboarding.get()
        self.assertNotIn('fake', state.state)
        enable_features(self.tenant, FAKE_FEATURE)
        state = TenantOnboarding.get()
        self.assertEqual(state.state['fake'], {'status': 'pending', 'answers': {}, 'completed_at': None})

    def test_disabled_feature_removes_its_entry(self):
        enable_features(self.tenant, FAKE_FEATURE)
        TenantOnboarding.get().set_status('fake', 'done', answers={'name': 'x'})
        disable_features(self.tenant, FAKE_FEATURE)
        self.assertNotIn('fake', TenantOnboarding.get().state)

    def test_re_enabled_feature_is_pending_again(self):
        enable_features(self.tenant, FAKE_FEATURE)
        TenantOnboarding.get().set_status('fake', 'done')
        disable_features(self.tenant, FAKE_FEATURE)
        enable_features(self.tenant, FAKE_FEATURE)
        self.assertEqual(TenantOnboarding.get().status_of('fake'), 'pending')

    def test_feature_without_a_step_changes_nothing(self):
        enable_features(self.tenant, 'feature_with_no_step')
        self.assertEqual(list(TenantOnboarding.get().state.keys()), [])

    def test_progress_is_kept_when_already_enabled(self):
        enable_features(self.tenant, FAKE_FEATURE)
        TenantOnboarding.get().set_status('fake', 'skipped')
        enable_features(self.tenant, FAKE_FEATURE)  # re-sync, nothing new
        self.assertEqual(TenantOnboarding.get().status_of('fake'), 'skipped')

    def test_new_feature_clears_finished_at(self):
        state = TenantOnboarding.get()
        state.set_status('workspace', 'skipped')
        state.refresh_finished(set())
        self.assertIsNotNone(TenantOnboarding.get().finished_at)
        enable_features(self.tenant, FAKE_FEATURE)
        self.assertIsNone(TenantOnboarding.get().finished_at)

    def test_tenant_without_schema_is_skipped(self):
        ghost = Tenant(schema_name='no_such_schema_xyz', name='Ghost', domain_url='ghost.test.com')
        self.assertIsNone(services.on_features_changed(ghost, [FAKE_FEATURE], []))
        self.assertIsNone(services.on_features_changed(None, [FAKE_FEATURE], []))

    def test_sync_result_reports_feature_keys(self):
        from tenants.subscription_service import SubscriptionService
        sub = enable_features(self.tenant, FAKE_FEATURE)
        result = SubscriptionService.sync_tenant_features(sub)
        self.assertEqual(result['enabled_feature_keys'], [])  # already enabled
        self.assertEqual(result['disabled_feature_keys'], [])
        sub.selected_features.clear()
        result = SubscriptionService.sync_tenant_features(sub)
        self.assertEqual(result['disabled_feature_keys'], [FAKE_FEATURE])
