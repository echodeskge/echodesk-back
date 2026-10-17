"""
Review fixes for ``manage.py seed_demo_tenant``: production guard, schema-name
validation, per-module savepoints, the leave year clamp, PBX entities with
Asterisk sync muted, the restricted viewer role and ignored-flag warnings.

Full seeds are slow (~20 s, they migrate a schema from scratch), so only the
tests that need real data run one; the guard / warning tests mock the heavy
parts of the command.
"""
from datetime import date
from io import StringIO
from itertools import combinations
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test.utils import override_settings
from tenant_schemas.utils import schema_context, schema_exists

from tenants.demo_seed import (
    DEMO_DID, DEMO_PASSWORD, DEMO_VIDEO_URL, VIEWER_FEATURE_KEYS, DemoSeeder, leave_plan_base,
)
from tenants.management.commands import seed_demo_tenant as cmd_module
from tenants.management.commands.seed_demo_tenant import Command as SeedCommand
from tenants.models import Tenant
from users.tests.conftest import EchoDeskTenantTestCase

CMD = 'tenants.management.commands.seed_demo_tenant'


def _seed(*args, debug=True):
    out, err = StringIO(), StringIO()
    with override_settings(DEBUG=debug):
        call_command('seed_demo_tenant', *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


class _Rollback(Exception):
    pass


class LightCommandMixin:
    """Patch out schema migration + seeding so guard tests stay fast."""

    def light(self):
        stack = [
            mock.patch(f'{CMD}.migrate_tenant_schema'),
            mock.patch(f'{CMD}.DemoSeeder'),
            mock.patch(f'{CMD}.demo_counts', return_value={}),
        ]
        mocks = [p.start() for p in stack]
        for p in stack:
            self.addCleanup(p.stop)
        return mocks


# --------------------------------------------------------------------- guard
class ProductionGuardTests(LightCommandMixin, EchoDeskTenantTestCase):

    def test_refuses_without_debug(self):
        migrate, seeder, *_ = self.light()
        with self.assertRaises(CommandError) as ctx:
            _seed('--schema', 'demoguard', debug=False)
        self.assertIn('DEBUG is False', str(ctx.exception))
        self.assertFalse(Tenant.objects.filter(schema_name='demoguard').exists())
        migrate.assert_not_called()
        seeder.assert_not_called()

    def test_refuses_before_touching_anything_even_with_reset(self):
        self.light()
        with mock.patch.object(SeedCommand, '_reset') as reset, \
                mock.patch.object(SeedCommand, '_ensure_subscription') as ensure:
            with self.assertRaises(CommandError):
                _seed('--schema', 'demoguard', '--reset', debug=False)
        reset.assert_not_called()
        ensure.assert_not_called()

    def test_allow_production_runs_with_a_loud_warning(self):
        migrate, seeder, *_ = self.light()
        with mock.patch.object(SeedCommand, '_ensure_subscription'):
            out, err = _seed('--schema', 'demoguard', '--allow-production', debug=False)
        self.assertIn('WARNING', err)
        self.assertIn('--allow-production', err)
        self.assertIn(DEMO_PASSWORD, err)
        self.assertTrue(Tenant.objects.filter(schema_name='demoguard').exists())
        migrate.assert_called_once()
        seeder.return_value.run.assert_called_once()

    def test_debug_true_needs_no_flag_and_prints_no_warning(self):
        self.light()
        with mock.patch.object(SeedCommand, '_ensure_subscription'):
            out, err = _seed('--schema', 'demoguard')
        self.assertNotIn('WARNING', err)
        self.assertIn('staff6@demo.ge', out)

    def test_help_documents_flag_and_every_login(self):
        self.assertIn('--allow-production', SeedCommand.help)
        for login in ('admin@demo.ge', 'manager@demo.ge', 'agent@demo.ge', 'staff1@demo.ge',
                      'staff5@demo.ge', 'staff6@demo.ge'):
            self.assertIn(login, SeedCommand.help)
        self.assertIn(DEMO_PASSWORD, SeedCommand.help)
        self.assertIn('tickets + leave', SeedCommand.help)


# ------------------------------------------------------------- schema names
class SchemaValidationTests(LightCommandMixin, EchoDeskTenantTestCase):

    BAD = ['public', 'PUBLIC', 'information_schema', 'pg_catalog', 'pg_toast', 'Pg_demo',
           'a' * 64, '1demo', 'drop table;', 'demo-x', '', '   ']

    def test_rejects_reserved_and_invalid_names(self):
        self.light()
        before = Tenant.objects.count()
        for bad in self.BAD:
            with self.subTest(schema=bad):
                with self.assertRaises(CommandError):
                    _seed('--schema', bad)
                with self.assertRaises(CommandError):
                    _seed('--schema', bad, '--reset')
        self.assertEqual(Tenant.objects.count(), before)
        self.assertTrue(schema_exists('information_schema'))

    def test_information_schema_reset_reproduction(self):
        """Review scenario: `--schema information_schema --reset` must not create a
        tenant row (which made the ownership check pass) nor drop the schema."""
        self.light()
        with mock.patch.object(SeedCommand, '_reset') as reset:
            with self.assertRaises(CommandError):
                _seed('--schema', 'information_schema', '--reset')
        reset.assert_not_called()
        self.assertFalse(Tenant.objects.filter(schema_name='information_schema').exists())

    def test_accepts_valid_names(self):
        for good in ('demo', 'demosalon', 'demo_2', '_x', 'a' * 63, ' Demo '):
            with self.subTest(schema=good):
                self.assertEqual(cmd_module.validate_schema_name(good), good.strip().lower())

    def test_refuses_existing_schema_without_tenant_row(self):
        self.light()
        with connection.cursor() as cursor:
            cursor.execute('CREATE SCHEMA "orphan_fxseed"')
        with mock.patch.object(SeedCommand, '_reset') as reset:
            with self.assertRaises(CommandError) as ctx:
                _seed('--schema', 'orphan_fxseed', '--reset')
        self.assertIn('no tenant row', str(ctx.exception))
        reset.assert_not_called()
        self.assertTrue(schema_exists('orphan_fxseed'))
        self.assertFalse(Tenant.objects.filter(schema_name='orphan_fxseed').exists())


# ------------------------------------------------------------ flag warnings
class IgnoredFlagWarningTests(LightCommandMixin, EchoDeskTenantTestCase):

    def _existing(self):
        with mock.patch.object(SeedCommand, '_ensure_subscription'):
            _seed('--schema', 'demowarn')  # creates the Tenant row (seeding mocked)
        return Tenant.objects.get(schema_name='demowarn')

    def test_warns_when_domain_or_name_ignored(self):
        self.light()
        tenant = self._existing()
        with mock.patch.object(SeedCommand, '_ensure_subscription'):
            _, err = _seed('--schema', 'demowarn', '--domain', 'other.api.lvh.me', '--name', 'Other')
        self.assertIn('--domain other.api.lvh.me ignored', err)
        self.assertIn('--name "Other" ignored', err)
        tenant.refresh_from_db()
        self.assertEqual(tenant.domain_url, 'demowarn.api.lvh.me')
        self.assertEqual(tenant.name, 'EchoDesk Demo')

    def test_no_warning_when_flags_match_or_absent(self):
        self.light()
        self._existing()
        with mock.patch.object(SeedCommand, '_ensure_subscription'):
            _, err = _seed('--schema', 'demowarn', '--domain', 'demowarn.api.lvh.me', '--name', 'EchoDesk Demo')
            _, err2 = _seed('--schema', 'demowarn')
        self.assertNotIn('ignored', err)
        self.assertNotIn('ignored', err2)

    def test_name_used_for_new_tenant(self):
        self.light()
        with mock.patch.object(SeedCommand, '_ensure_subscription'):
            _, err = _seed('--schema', 'demoname', '--name', 'Salon Demo', '--domain', 'salon.api.lvh.me')
        self.assertNotIn('ignored', err)
        t = Tenant.objects.get(schema_name='demoname')
        self.assertEqual((t.name, t.domain_url), ('Salon Demo', 'salon.api.lvh.me'))


# --------------------------------------------------------------- leave clamp
class LeaveYearClampTests(EchoDeskTenantTestCase):
    PLAN_MIN, PLAN_MAX = -56, 53  # offsets used by seed_leave (staff2 VAC -56, manager VAC 49+5-1)

    def test_base_keeps_plan_inside_the_year(self):
        for today in (date(2026, 12, 18), date(2026, 12, 20), date(2026, 12, 31), date(2026, 1, 1),
                      date(2026, 1, 3), date(2026, 2, 20), date(2026, 6, 15), date(2028, 12, 31)):
            with self.subTest(today=today):
                base = leave_plan_base(today, self.PLAN_MIN, self.PLAN_MAX)
                self.assertEqual(base.weekday(), 0)
                self.assertGreaterEqual((base.toordinal() + self.PLAN_MIN), date(today.year, 1, 1).toordinal())
                self.assertLessEqual((base.toordinal() + self.PLAN_MAX), date(today.year, 12, 31).toordinal())

    def test_mid_year_uses_this_monday(self):
        self.assertEqual(leave_plan_base(date(2026, 6, 17), self.PLAN_MIN, self.PLAN_MAX), date(2026, 6, 15))

    def _seed_leave_on(self, today):
        """Run the real leave seeder in the test schema as if today were ``today``."""
        from leave_management.models import LeaveBalance, LeaveRequest
        result = {}
        try:
            with transaction.atomic():
                seeder = DemoSeeder(self.tenant)
                seeder.today = today
                seeder.seed_users()
                seeder.seed_leave()
                result['requests'] = list(LeaveRequest.objects.filter(tenant=self.tenant).values_list(
                    'employee_id', 'start_date', 'end_date'))
                result['balance_years'] = set(LeaveBalance.objects.filter(tenant=self.tenant)
                                              .values_list('year', flat=True))
                raise _Rollback
        except _Rollback:
            pass
        return result

    def test_requests_never_overlap_per_employee_near_year_end(self):
        # Review reproduction: between ~Dec 18 and Dec 31 agent's VAC (+14) and
        # PERS (+35) were both clamped to Dec 1 and overlapped.
        for today in (date(2026, 12, 18), date(2026, 12, 24), date(2026, 12, 31), date(2026, 1, 2)):
            with self.subTest(today=today):
                res = self._seed_leave_on(today)
                self.assertEqual(len(res['requests']), 10)
                self.assertEqual(res['balance_years'], {today.year})
                for emp, start, end in res['requests']:
                    self.assertEqual(start.year, today.year)
                    self.assertEqual(end.year, today.year)
                by_emp = {}
                for emp, start, end in res['requests']:
                    by_emp.setdefault(emp, []).append((start, end))
                for emp, spans in by_emp.items():
                    for (s1, e1), (s2, e2) in combinations(spans, 2):
                        self.assertTrue(e1 < s2 or e2 < s1, f'overlap for {emp}: {s1}-{e1} / {s2}-{e2}')


# ------------------------------------------------------- full-seed scenarios
class SeedFixesFullTests(EchoDeskTenantTestCase):

    def _counts(self, schema='demo'):
        from tenants.demo_seed import demo_counts
        with schema_context(schema):
            return demo_counts(Tenant.objects.get(schema_name=schema))

    def test_pbx_viewer_push_and_asterisk_muting(self):
        from crm.asterisk_sync import AsteriskStateSync
        from users import notification_utils

        spy_push = mock.Mock(wraps=notification_utils._dispatch_push)
        sync_methods = ['sync_endpoint', 'sync_trunk', 'sync_queue', 'sync_queue_members',
                        'sync_inbound_route', 'tombstone_endpoint', 'tombstone_queue']
        with mock.patch('users.notification_utils._dispatch_push', spy_push), \
                mock.patch('crm.asterisk_sync.get_active_pbx_for_current_tenant') as pbx_lookup, \
                mock.patch.multiple(AsteriskStateSync, **{m: mock.DEFAULT for m in sync_methods}) as syncs:
            _seed()
            counts = self._counts()
            out2, _ = _seed()
            # Control: outside the seed the same spy is reachable, so the
            # "not called" assertions below can actually fail.
            with schema_context('demo'):
                from users.models import User
                notification_utils.create_notification(
                    User.objects.get(email='agent@demo.ge'), 'ticket_assigned', 'x', 'y')
        self.assertEqual(spy_push.call_count, 1, 'push must only be reached by the control call')
        pbx_lookup.assert_not_called()
        for name, m in syncs.items():
            m.assert_not_called()
        self.assertIn('pbx: already seeded', out2)
        self.assertEqual(self._counts(), {**counts, 'notifications': counts['notifications'] + 1})

        from crm.models import InboundRoute, PbxSettings, Queue, QueueMember, Trunk
        from notifications.models import NotificationLog
        from users.models import Notification, User
        with schema_context('demo'):
            # Signals did create notifications (so muting was actually exercised) ...
            self.assertGreater(Notification.objects.count(),
                               Notification.objects.filter(metadata__demo_seed=True).count())
            # ... but nothing was delivered.
            self.assertFalse(NotificationLog.objects.exists())

            trunk = Trunk.objects.get()
            self.assertIn('demo-fake', trunk.password)
            self.assertIn(DEMO_DID, trunk.phone_numbers)
            queue = Queue.objects.get()
            members = set(queue.group.members.values_list('email', flat=True))
            self.assertEqual(members, {'agent@demo.ge', 'manager@demo.ge'})
            self.assertEqual(
                set(QueueMember.objects.filter(queue=queue)
                    .values_list('user_phone_assignment__user__email', flat=True)),
                {'agent@demo.ge', 'manager@demo.ge'})
            route = InboundRoute.objects.get(did=DEMO_DID)
            self.assertEqual((route.destination_type, route.destination_queue_id), ('queue', queue.id))
            self.assertTrue(InboundRoute.objects.filter(destination_type='extension').exists())
            self.assertEqual(PbxSettings.objects.count(), 1)
            self.assertTrue(PbxSettings.objects.get().working_hours_enabled)

            viewer = User.objects.get(email='staff6@demo.ge')
            self.assertEqual(set(viewer.get_feature_keys()), set(VIEWER_FEATURE_KEYS))
            self.assertFalse(viewer.tenant_groups.filter(name='All features').exists())
            agent = User.objects.get(email='agent@demo.ge')
            self.assertIn('ip_calling', agent.get_feature_keys())
            self.assertNotIn('ip_calling', viewer.get_feature_keys())

        for key in ('trunks', 'queues', 'inbound_routes', 'pbx_settings'):
            self.assertGreaterEqual(counts[key], 1, key)
        self.assertEqual(counts['queue_members'], 2)

        from help_center.models import HelpArticle
        self.assertFalse(HelpArticle.objects.filter(video_url__contains='dQw4w9WgXcQ').exists())

    def test_failed_module_is_rolled_back_and_retried(self):
        """Review reproduction: the module's marker row (board "Customer Support")
        used to survive a mid-module failure, so the module was skipped forever."""
        original = DemoSeeder.seed_tickets

        def broken(self):
            from tickets.models import Board
            Board.objects.create(name='Customer Support', created_by=self.admin)  # the marker row
            raise RuntimeError('boom halfway')

        with mock.patch.object(DemoSeeder, 'seed_tickets', broken):
            with self.assertRaises(CommandError) as ctx:
                _seed()
        self.assertIn('tickets', str(ctx.exception))
        from tickets.models import Board, Ticket
        from invoices.models import Invoice
        with schema_context('demo'):
            self.assertFalse(Board.objects.exists(), 'half-seeded module must be rolled back')
            self.assertEqual(Invoice.objects.count(), 8, 'other modules still seed')

        self.assertIs(DemoSeeder.seed_tickets, original)
        out, _ = _seed()
        self.assertIn('tickets: seeded', out)
        self.assertIn('invoices: already seeded', out)
        with schema_context('demo'):
            self.assertEqual(Ticket.objects.count(), 30)

    def test_reset_viewer_upgrade_and_old_video_replaced(self):
        from help_center.models import HelpArticle
        _seed()
        # Simulate a DB seeded by the previous version: viewer in "All features",
        # joke video in the shared help center.
        HelpArticle.objects.filter(content_type='video').update(
            video_url='https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        with schema_context('demo'):
            from users.models import TenantGroup, User
            viewer = User.objects.get(email='staff6@demo.ge')
            viewer.tenant_groups.add(TenantGroup.objects.get(name='All features'))
        out, _ = _seed()
        self.assertIn('already seeded', out)
        with schema_context('demo'):
            viewer = User.objects.get(email='staff6@demo.ge')
            self.assertEqual(set(viewer.get_feature_keys()), set(VIEWER_FEATURE_KEYS))
        self.assertFalse(HelpArticle.objects.filter(video_url__contains='dQw4w9WgXcQ').exists())
        if HelpArticle.objects.filter(content_type='video').exists():
            self.assertTrue(HelpArticle.objects.filter(video_url=DEMO_VIDEO_URL).exists())
