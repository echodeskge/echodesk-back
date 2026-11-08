"""
BE-05 commands and seed data (tickets redesign):

- ``prepare_tickets_e2e``: local-only guard, fixture content, --reset
  idempotency, the no-social variant;
- ``backfill_ticket_due_dates``: dry run, real run, scope;
- the demo seed's redesign enrichment (board colours, due dates, checked_by,
  mixed is_internal, a ticket customer).
"""
import json
import os
import tempfile
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test.utils import override_settings

from tickets.dates import tickets_today
from tickets.models import (
    Board, ChecklistItem, Ticket, TicketAttachment, TicketColumn, TicketComment, TicketPayment,
)
from tickets.tests.conftest import TicketTestCase


def run_e2e(*args, debug=True, **kwargs):
    out, err = StringIO(), StringIO()
    with override_settings(DEBUG=debug):
        call_command('prepare_tickets_e2e', *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue()


class PrepareTicketsE2ETestBase(TicketTestCase):

    def setUp(self):
        super().setUp()
        fd, self.json_path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        self.addCleanup(os.remove, self.json_path)

    def prepare(self, *extra):
        run_e2e('--schema', self.tenant.schema_name, '--reset', '--json', self.json_path, *extra)
        with open(self.json_path, encoding='utf-8') as fh:
            return json.load(fh)

    @staticmethod
    def snapshot():
        e2e_tickets = Ticket.objects.filter(title__startswith='E2E-')
        return {
            'boards': sorted(Board.objects.filter(name__startswith='E2E ').values_list('name', flat=True)),
            'columns': TicketColumn.objects.filter(board__name__startswith='E2E ').count(),
            'titles': sorted(e2e_tickets.values_list('title', flat=True)),
            'checklist': ChecklistItem.objects.filter(ticket__in=e2e_tickets).count(),
            'attachments': TicketAttachment.objects.filter(ticket__in=e2e_tickets).count(),
            'comments': TicketComment.objects.filter(ticket__in=e2e_tickets).count(),
            'payments': TicketPayment.objects.filter(ticket__in=e2e_tickets).count(),
        }


class TestPrepareTicketsE2EGuards(PrepareTicketsE2ETestBase):

    def test_refuses_when_debug_is_false(self):
        with self.assertRaises(CommandError) as ctx:
            run_e2e('--schema', self.tenant.schema_name, '--reset', debug=False)
        self.assertIn('DEBUG', str(ctx.exception))
        self.assertFalse(Board.objects.filter(name__startswith='E2E ').exists())

    def test_refuses_on_a_non_local_database_host(self):
        with mock.patch.dict(settings.DATABASES['default'], {'HOST': 'db.example.com'}):
            with self.assertRaises(CommandError) as ctx:
                run_e2e('--schema', self.tenant.schema_name, '--reset')
        self.assertIn('db.example.com', str(ctx.exception))
        self.assertFalse(Board.objects.filter(name__startswith='E2E ').exists())

    def test_refuses_public_and_unknown_schemas(self):
        with self.assertRaises(CommandError):
            run_e2e('--schema', 'public')
        with self.assertRaises(CommandError):
            run_e2e('--schema', 'no_such_schema')

    def test_second_run_without_reset_refuses(self):
        self.prepare()
        before = self.snapshot()
        with self.assertRaises(CommandError) as ctx:
            run_e2e('--schema', self.tenant.schema_name)
        self.assertIn('--reset', str(ctx.exception))
        self.assertEqual(self.snapshot(), before)


class TestPrepareTicketsE2EFixtures(PrepareTicketsE2ETestBase):

    def test_builds_the_documented_fixtures(self):
        with mock.patch('users.notification_utils._dispatch_push') as push:
            ids = self.prepare()
        push.assert_not_called()

        # Boards and columns.
        self.assertEqual(set(ids['boards']), {'support', 'sales', 'open', 'restricted', 'empty'})
        support = Board.objects.get(pk=ids['boards']['support']['id'])
        self.assertEqual(support.name, 'E2E Support')
        self.assertTrue(support.is_default)
        self.assertEqual(support.color, '#3b82f6')
        self.assertEqual(Board.objects.get(pk=ids['boards']['sales']['id']).color, '#22c55e')
        cols = {c.name: c for c in support.columns.all()}
        self.assertEqual(list(support.columns.order_by('position').values_list('name', flat=True)),
                         ['New', 'In Progress', 'Waiting', 'Resolved'])
        self.assertTrue(cols['New'].is_default)
        self.assertTrue(cols['In Progress'].track_time)
        self.assertTrue(cols['Resolved'].is_closed_status)
        self.assertEqual(Board.objects.get(pk=ids['boards']['empty']['id']).columns.count(), 0)
        self.assertEqual(ids['boards']['empty']['columns'], {})

        # Users and access.
        admin = self._user(ids, 'admin')
        agent = self._user(ids, 'agent')
        agent2 = self._user(ids, 'agent2')
        self.assertTrue(admin.is_staff)
        self.assertFalse(agent.is_staff)
        for perm in ('manage_columns', 'create_boards', 'access_orders'):
            self.assertFalse(agent.has_permission(perm), perm)
        group_id = ids['groups']['support_team']
        self.assertEqual(list(support.board_groups.values_list('id', flat=True)), [group_id])
        self.assertFalse(support.board_users.exists())
        self.assertTrue(agent.tenant_groups.filter(pk=group_id).exists())
        self.assertTrue(agent2.tenant_groups.filter(pk=group_id).exists())
        open_board = Board.objects.get(pk=ids['boards']['open']['id'])
        self.assertFalse(open_board.board_users.exists() or open_board.board_groups.exists()
                         or open_board.order_users.exists())
        restricted = Board.objects.get(pk=ids['boards']['restricted']['id'])
        self.assertFalse(restricted.board_users.filter(pk=agent.pk).exists())
        self.assertFalse(restricted.board_groups.filter(members=agent).exists())

        # Tickets.
        tickets = Ticket.objects.filter(title__startswith='E2E-')
        self.assertEqual(tickets.count(), 12)
        self.assertEqual(sorted(ids['tickets']), [f'E2E-{n:03d}' for n in range(1, 13)])
        self.assertEqual(set(tickets.values_list('priority', flat=True)), {'low', 'medium', 'high', 'critical'})
        self.assertTrue(tickets.filter(tags__name='VIP').exists())
        self.assertTrue(tickets.filter(tags__name='Bug').exists())
        self.assertTrue(tickets.filter(assigned_to__isnull=True, column__board=support).exists())

        roles = ids['roles']
        today = tickets_today()
        self.assertEqual(Ticket.objects.get(pk=roles['due_today']).due_date, today)
        overdue = Ticket.objects.get(pk=roles['overdue'])
        self.assertLess(overdue.due_date, today)
        self.assertTrue(overdue.is_overdue_task)
        self.assertFalse(Ticket.objects.get(pk=roles['resolved']).is_overdue_task)  # closed column

        checklist = ChecklistItem.objects.filter(ticket_id=roles['checklist'])
        self.assertEqual(checklist.count(), 5)
        self.assertEqual(checklist.filter(is_checked=True).count(), 2)
        self.assertFalse(checklist.filter(is_checked=True, checked_by__isnull=True).exists())

        attachments = TicketAttachment.objects.filter(ticket_id=roles['attachments'])
        self.assertEqual(sorted(attachments.values_list('content_type', flat=True)),
                         ['application/pdf', 'image/png'])
        self.assertTrue(attachments.filter(uploaded_by=admin).exists())

        customer_ticket = Ticket.objects.get(pk=roles['customer'])
        self.assertEqual(customer_ticket.customer_id, ids['customer']['id'])
        self.assertTrue(customer_ticket.comments.filter(is_internal=True).exists())

        order = Ticket.objects.get(pk=roles['order'])
        self.assertTrue(order.is_order)
        self.assertEqual(order.payments.count(), 1)
        self.assertEqual(order.amount_paid, Decimal('40.00'))

        a = Ticket.objects.get(pk=roles['equal_position_a'])
        b = Ticket.objects.get(pk=roles['equal_position_b'])
        self.assertEqual(a.column_id, b.column_id)
        self.assertEqual(a.position_in_column, b.position_in_column)

        self.assertEqual(Ticket.objects.get(pk=roles['restricted']).column.board_id, restricted.id)
        self.assertEqual(Ticket.objects.get(pk=roles['open_board']).column.board_id, open_board.id)
        self.assertEqual(ids['schema'], self.tenant.schema_name)
        self.assertFalse(ids['variant']['agent_without_social'])

    def test_reset_is_idempotent(self):
        first_ids = self.prepare()
        first = self.snapshot()
        # Something a spec created on an E2E board without the prefix.
        self.create_ticket(title='Created from the UI',
                           column=TicketColumn.objects.get(pk=first_ids['boards']['support']['columns']['new']))
        second_ids = self.prepare()
        second = self.snapshot()
        self.assertEqual(first, second)
        self.assertFalse(Ticket.objects.filter(title='Created from the UI').exists())
        self.assertEqual(Board.objects.filter(name__startswith='E2E ').count(), 5)
        self.assertEqual(Board.objects.filter(is_default=True).count(), 1)
        self.assertEqual(first_ids['users'], second_ids['users'])
        self.assertEqual(first_ids['customer'], second_ids['customer'])

    def test_stdout_json_when_no_path_given(self):
        out, _ = run_e2e('--schema', self.tenant.schema_name, '--reset')
        ids = json.loads(out)
        self.assertEqual(len(ids['tickets']), 12)

    def test_existing_demo_users_are_reused(self):
        agent = self.create_user(email='agent@demo.ge', can_access_orders=True, can_manage_columns=True)
        ids = self.prepare()
        self.assertEqual(ids['users']['agent']['id'], agent.id)
        agent.refresh_from_db()
        self.assertFalse(agent.can_access_orders)
        self.assertFalse(agent.can_manage_columns)

    def test_agent_without_social_variant(self):
        from tenants.feature_models import Feature
        social, _ = Feature.objects.get_or_create(key='social_integrations',
                                                  defaults={'name': 'Social', 'is_active': True})
        if not social.is_active:
            social.is_active = True
            social.save(update_fields=['is_active'])
        agent = self.create_user(email='agent@demo.ge')
        everything = self.create_tenant_group(name='All features (test)')
        everything.features.add(social)
        agent.tenant_groups.add(everything)

        ids = self.prepare('--agent-without-social')
        self.assertTrue(ids['variant']['agent_without_social'])
        agent.refresh_from_db()
        self.assertFalse(agent.tenant_groups.filter(features__key='social_integrations').exists())
        self.assertTrue(agent.tenant_groups.filter(pk=ids['groups']['support_team']).exists())

    @staticmethod
    def _user(ids, key):
        from users.models import User
        return User.objects.get(pk=ids['users'][key]['id'])


class TestBackfillTicketDueDates(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.board, self.col, *_ = self.setup_board_with_columns()
        self.pay_due = date(2026, 12, 1)
        self.plain = self.create_ticket(title='plain', column=self.col, payment_due_date=self.pay_due)
        self.order = self.create_ticket(title='order', column=self.col, is_order=True,
                                        payment_due_date=self.pay_due)
        self.has_due = self.create_ticket(title='has due', column=self.col, payment_due_date=self.pay_due,
                                          due_date=date(2026, 11, 20))
        self.no_dates = self.create_ticket(title='no dates', column=self.col)

    def run_cmd(self, *args):
        out, err = StringIO(), StringIO()
        call_command('backfill_ticket_due_dates', *args, stdout=out, stderr=err)
        return out.getvalue()

    def dues(self):
        return {t.title: t.due_date for t in Ticket.objects.all()}

    def test_dry_run_changes_nothing(self):
        before = self.dues()
        out = self.run_cmd('--schema', self.tenant.schema_name, '--dry-run')
        self.assertEqual(self.dues(), before)
        self.assertIn(f'{self.tenant.schema_name}: 1 ticket(s) would update', out)
        self.assertIn('[DRY RUN]', out)

    def test_real_run_copies_only_non_order_rows_with_null_due_date(self):
        out = self.run_cmd('--schema', self.tenant.schema_name)
        self.assertIn(f'{self.tenant.schema_name}: 1 ticket(s) updated', out)
        dues = self.dues()
        self.assertEqual(dues['plain'], self.pay_due)
        self.assertIsNone(dues['order'])
        self.assertEqual(dues['has due'], date(2026, 11, 20))
        self.assertIsNone(dues['no dates'])
        self.plain.refresh_from_db()
        self.assertEqual(self.plain.payment_due_date, self.pay_due)  # untouched

        # Re-running is a no-op.
        self.assertIn(f'{self.tenant.schema_name}: 0 ticket(s) updated',
                      self.run_cmd('--schema', self.tenant.schema_name))

    def test_all_includes_the_tenant(self):
        out = self.run_cmd('--all', '--dry-run')
        self.assertIn(f'{self.tenant.schema_name}: 1 ticket(s) would update', out)

    def test_writes_no_history(self):
        from tickets.models import TicketHistory
        before = TicketHistory.objects.count()
        self.run_cmd('--schema', self.tenant.schema_name)
        self.assertEqual(TicketHistory.objects.count(), before)

    def test_target_is_required_and_validated(self):
        with self.assertRaises(CommandError):
            self.run_cmd()
        with self.assertRaises(CommandError):
            self.run_cmd('--schema', self.tenant.schema_name, '--all')
        with self.assertRaises(CommandError):
            self.run_cmd('--schema', 'no_such_schema')


class TestDemoSeedRedesignData(TicketTestCase):
    """The ticket and social modules of the demo seed, run in the test schema."""

    def test_seed_fills_the_redesign_fields(self):
        from tenants.demo_seed import DemoSeeder, muted_asterisk_sync, muted_push
        seeder = DemoSeeder(self.tenant)
        with muted_push(), muted_asterisk_sync():
            seeder.seed_users()
            seeder.seed_tickets()
            seeder.seed_social()

        boards = {b.name: b for b in Board.objects.all()}
        self.assertEqual(boards['Customer Support'].color, '#3b82f6')
        self.assertEqual(boards['Sales Pipeline'].color, '#22c55e')
        self.assertEqual([boards['Customer Support'].position, boards['Sales Pipeline'].position], [0, 1])

        tickets = Ticket.objects.all()
        self.assertEqual(tickets.count(), 30)
        with_due = tickets.filter(due_date__isnull=False).count()
        self.assertEqual(with_due, 12)  # 40 %
        overdue = [t for t in tickets.filter(due_date__lt=tickets_today()).select_related('column')
                   if t.is_overdue_task]
        self.assertTrue(overdue, 'some open tickets must be overdue')
        self.assertTrue(tickets.filter(due_date__gte=tickets_today()).exists())

        checked = ChecklistItem.objects.filter(is_checked=True)
        self.assertTrue(checked.exists())
        self.assertFalse(checked.filter(checked_by__isnull=True).exists())
        self.assertFalse(checked.filter(checked_at__isnull=True).exists())
        self.assertFalse(ChecklistItem.objects.filter(is_checked=False, checked_by__isnull=False).exists())

        self.assertTrue(TicketComment.objects.filter(is_internal=True).exists())
        self.assertTrue(TicketComment.objects.filter(is_internal=False).exists())
        # Backdated comments must not read as "edited" in the activity feed
        # (edited = updated_at - created_at > 2s).
        from django.db.models import F
        self.assertFalse(
            TicketComment.objects.exclude(updated_at=F('created_at')).exists(),
            'seeded comments keep updated_at == created_at',
        )

        linked = tickets.filter(customer__isnull=False)
        self.assertEqual(linked.count(), 1)
        self.assertEqual(linked.get().title, 'Instagram order follow-up')
        self.assertTrue(linked.get().customer.social_accounts.filter(platform='instagram').exists())

    def test_due_dates_use_the_tbilisi_today(self):
        from tenants.demo_seed import DemoSeeder, muted_asterisk_sync, muted_push
        fixed = date(2026, 3, 10)
        with mock.patch('tickets.dates.tickets_today', return_value=fixed):
            seeder = DemoSeeder(self.tenant)
            with muted_push(), muted_asterisk_sync():
                seeder.seed_users()
                seeder.seed_tickets()
        offsets = sorted((d - fixed).days for d in
                         Ticket.objects.filter(due_date__isnull=False).values_list('due_date', flat=True))
        self.assertEqual(offsets, sorted([-6, 0, -2, 3, 1, 7, -1, 14, 2, 5, -4, 21]))
        self.assertTrue(all(abs(o) < 30 for o in offsets))
