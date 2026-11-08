"""
Tickets redesign (BE-01): schema, migration-safety and model-helper tests.

Production is the only database and migrations run on every tenant while the
previous release is still serving (and again after a revert), so 0041/0042
must be purely additive and safe for code that doesn't know the new columns
(plan R1 / E31).
"""
import datetime
import importlib
import re
from unittest import mock

from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.operations.fields import AddField
from django.db.migrations.operations.models import (
    AddConstraint, AddIndex, CreateModel,
)
from django.db.migrations.operations.special import RunSQL
from django.utils import timezone

from tickets.dates import TICKETS_LOCAL_TZ, end_of_iso_week, tickets_today
from tickets.models import Board, Ticket, TicketComment, TicketTimeEntry
from tickets.tests.conftest import (
    TicketTestCase, assert_constant_queries, capture_queries,
)

M0041 = importlib.import_module('tickets.migrations.0041_redesign_fields').Migration
M0042 = importlib.import_module('tickets.migrations.0042_ticket_time_entry').Migration

ALLOWED_OPS = (AddField, CreateModel, AddIndex, AddConstraint, RunSQL)
RUNSQL_RE = re.compile(r'^ALTER TABLE tickets_\w+ ALTER COLUMN \w+ SET DEFAULT .+;$')
RUNSQL_REVERSE_RE = re.compile(r'^ALTER TABLE tickets_\w+ ALTER COLUMN \w+ DROP DEFAULT;$')


def _freeze(dt):
    """Patch django.utils.timezone.now (as used by tickets.dates)."""
    return mock.patch('tickets.dates.timezone.now', return_value=dt)


class TestMigrationOperations(TicketTestCase):
    """Static checks on the migration files themselves."""

    def test_only_additive_operations(self):
        for mig in (M0041, M0042):
            for op in mig.operations:
                self.assertIsInstance(
                    op, ALLOWED_OPS,
                    f'{mig.__module__}: disallowed operation {type(op).__name__}',
                )
                if isinstance(op, RunSQL):
                    self.assertRegex(op.sql, RUNSQL_RE)
                    self.assertRegex(op.reverse_sql, RUNSQL_REVERSE_RE)
                if isinstance(op, AddField):
                    f = op.field
                    self.assertTrue(
                        f.null or f.has_default(),
                        f'{op.model_name}.{op.name} must be nullable or have a default',
                    )

    def test_runsql_only_in_0041_and_exactly_three(self):
        sqls = [op.sql for op in M0041.operations if isinstance(op, RunSQL)]
        self.assertEqual(sorted(sqls), sorted([
            "ALTER TABLE tickets_ticketcomment ALTER COLUMN is_internal SET DEFAULT true;",
            "ALTER TABLE tickets_board ALTER COLUMN color SET DEFAULT '#3b82f6';",
            "ALTER TABLE tickets_board ALTER COLUMN position SET DEFAULT 0;",
        ]))
        self.assertFalse([op for op in M0042.operations if isinstance(op, RunSQL)])

    def test_0041_dependencies(self):
        self.assertIn(('social_integrations', '0065_conversation_ticket_client_tags'), M0041.dependencies)
        self.assertIn(('tickets', '0040_add_performance_indexes'), M0041.dependencies)
        self.assertIn(('tickets', '0041_redesign_fields'), M0042.dependencies)

    def test_on_delete_semantics(self):
        customer = Ticket._meta.get_field('customer')
        self.assertIs(customer.remote_field.on_delete, models.SET_NULL)
        self.assertTrue(customer.null)
        self.assertEqual(customer.related_model._meta.label, 'social_integrations.Client')
        user = TicketTimeEntry._meta.get_field('user')
        self.assertIs(user.remote_field.on_delete, models.SET_NULL)
        self.assertTrue(user.null)
        self.assertIs(
            TicketTimeEntry._meta.get_field('ticket').remote_field.on_delete, models.CASCADE,
        )

    def test_board_meta_ordering_unchanged(self):
        self.assertEqual(Board._meta.ordering, ['name'])

    def test_model_defaults(self):
        self.assertIs(TicketComment().is_internal, True)
        self.assertEqual(Board().color, '#3b82f6')
        self.assertEqual(Board().position, 0)
        self.assertIsNone(Ticket().due_date)
        self.assertIsNone(Ticket().customer_id)


class TestOldCodeSafety(TicketTestCase):
    """R1: rows inserted by code that predates 0041 must still succeed."""

    def _column_default(self, table, column):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT column_default FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s
                """,
                [table, column],
            )
            row = cur.fetchone()
        return row[0] if row else None

    def test_db_defaults_present(self):
        self.assertEqual(self._column_default('tickets_ticketcomment', 'is_internal'), 'true')
        self.assertIn('#3b82f6', self._column_default('tickets_board', 'color'))
        self.assertEqual(self._column_default('tickets_board', 'position'), '0')

    def test_raw_comment_insert_without_is_internal(self):
        user = self.create_user(email='oldcode-commenter@test.com')
        ticket = self.create_ticket(created_by=user)
        now = timezone.now()
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tickets_ticketcomment (ticket_id, user_id, comment, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s) RETURNING id, is_internal
                """,
                [ticket.id, user.id, 'old code comment', now, now],
            )
            pk, is_internal = cur.fetchone()
        self.assertIs(is_internal, True)
        self.assertTrue(TicketComment.objects.get(pk=pk).is_internal)

    def test_raw_board_insert_without_color_or_position(self):
        admin = self.create_admin(email='oldcode-board@test.com')
        now = timezone.now()
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tickets_board (name, description, is_default, created_at, updated_at, created_by_id)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, color, position
                """,
                ['Old code board', '', False, now, now, admin.id],
            )
            pk, color, position = cur.fetchone()
        self.assertEqual(color, '#3b82f6')
        self.assertEqual(position, 0)
        board = Board.objects.get(pk=pk)
        self.assertEqual((board.color, board.position), ('#3b82f6', 0))

    def test_raw_ticket_and_checklist_insert_without_new_columns(self):
        """New Ticket / ChecklistItem columns are nullable, so old INSERTs work."""
        user = self.create_user(email='oldcode-ticket@test.com')
        now = timezone.now()
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tickets_ticket (title, description, description_format, priority,
                    position_in_column, created_at, updated_at, created_by_id, is_order,
                    currency, is_paid, amount_paid)
                VALUES (%s, '', 'plain', 'medium', 0, %s, %s, %s, false, 'USD', false, 0)
                RETURNING id, due_date, customer_id
                """,
                ['Old code ticket', now, now, user.id],
            )
            ticket_id, due_date, customer_id = cur.fetchone()
            cur.execute(
                """
                INSERT INTO tickets_checklistitem (ticket_id, text, is_checked, position,
                    created_at, updated_at, created_by_id)
                VALUES (%s, 'x', true, 1, %s, %s, %s) RETURNING checked_by_id, checked_at
                """,
                [ticket_id, now, now, user.id],
            )
            checked_by_id, checked_at = cur.fetchone()
        self.assertIsNone(due_date)
        self.assertIsNone(customer_id)
        self.assertIsNone(checked_by_id)
        self.assertIsNone(checked_at)

    def test_customer_delete_sets_null(self):
        client = self.create_client(name='Doomed Client', email='doomed@test.com')
        ticket = self.create_ticket(customer=client)
        client.delete()
        ticket.refresh_from_db()
        self.assertIsNone(ticket.customer_id)


class TestTicketTimeEntry(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.user = self.create_user(email='timer-user@test.com')
        self.other = self.create_user(email='timer-other@test.com')
        self.ticket = self.create_ticket(created_by=self.user)
        self.ticket2 = self.create_ticket(title='Second', created_by=self.user)

    def test_second_running_entry_for_same_user_rejected(self):
        self.create_time_entry(self.ticket, self.user)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_time_entry(self.ticket2, self.user)
        # The outer test transaction is still usable.
        self.assertEqual(TicketTimeEntry.objects.filter(user=self.user).count(), 1)

    def test_stopped_entries_allowed(self):
        e1 = self.create_time_entry(self.ticket, self.user)
        e1.stop()
        self.create_time_entry(self.ticket2, self.user)
        self.create_time_entry(
            self.ticket, self.user,
            started_at=timezone.now() - datetime.timedelta(hours=2),
            ended_at=timezone.now() - datetime.timedelta(hours=1),
        )
        self.assertEqual(TicketTimeEntry.objects.filter(user=self.user).count(), 3)
        self.assertEqual(
            TicketTimeEntry.objects.filter(user=self.user, ended_at__isnull=True).count(), 1,
        )

    def test_two_users_can_both_run(self):
        self.create_time_entry(self.ticket, self.user)
        self.create_time_entry(self.ticket, self.other)
        self.assertEqual(TicketTimeEntry.objects.filter(ended_at__isnull=True).count(), 2)

    def test_two_null_user_running_entries_allowed(self):
        self.create_time_entry(self.ticket, None)
        self.create_time_entry(self.ticket2, None)
        self.assertEqual(TicketTimeEntry.objects.filter(user__isnull=True).count(), 2)

    def test_user_delete_keeps_entries(self):
        entry = self.create_time_entry(self.ticket, self.other)
        entry.stop()
        self.other.delete()
        entry.refresh_from_db()
        self.assertIsNone(entry.user_id)

    def test_stop_sets_end_and_floored_duration(self):
        started = timezone.now() - datetime.timedelta(minutes=5)
        entry = self.create_time_entry(self.ticket, self.user, started_at=started)
        ended = started + datetime.timedelta(seconds=125, milliseconds=900)
        entry.stop(now=ended)
        entry.refresh_from_db()
        self.assertEqual(entry.ended_at, ended)
        self.assertEqual(entry.duration_seconds, 125)
        self.assertFalse(entry.is_running)

    def test_stop_is_idempotent(self):
        started = timezone.now() - datetime.timedelta(minutes=5)
        entry = self.create_time_entry(self.ticket, self.user, started_at=started)
        first_end = started + datetime.timedelta(seconds=60)
        entry.stop(now=first_end)
        entry.stop(now=first_end + datetime.timedelta(hours=1))
        entry.refresh_from_db()
        self.assertEqual(entry.ended_at, first_end)
        self.assertEqual(entry.duration_seconds, 60)

    def test_stop_never_negative(self):
        started = timezone.now()
        entry = self.create_time_entry(self.ticket, self.user, started_at=started)
        entry.stop(now=started - datetime.timedelta(seconds=3))
        self.assertEqual(entry.duration_seconds, 0)

    def test_default_ordering_newest_first(self):
        now = timezone.now()
        old = self.create_time_entry(
            self.ticket, self.user, started_at=now - datetime.timedelta(hours=3),
            ended_at=now - datetime.timedelta(hours=2),
        )
        new = self.create_time_entry(self.ticket, self.user, started_at=now)
        self.assertEqual(list(self.ticket.time_entries.all()), [new, old])


class TestTicketsDates(TicketTestCase):

    def test_local_tz_is_tbilisi(self):
        self.assertEqual(str(TICKETS_LOCAL_TZ), 'Asia/Tbilisi')

    def test_today_is_tbilisi_date(self):
        # 21:30 UTC on 2026-11-09 is 01:30 on 2026-11-10 in Tbilisi (UTC+4).
        frozen = datetime.datetime(2026, 11, 9, 21, 30, tzinfo=datetime.timezone.utc)
        with _freeze(frozen):
            self.assertEqual(tickets_today(), datetime.date(2026, 11, 10))

    def test_today_before_local_midnight(self):
        frozen = datetime.datetime(2026, 11, 9, 19, 59, tzinfo=datetime.timezone.utc)
        with _freeze(frozen):
            self.assertEqual(tickets_today(), datetime.date(2026, 11, 9))

    def test_end_of_iso_week_returns_sunday(self):
        # 2026-11-09 is a Monday.
        self.assertEqual(end_of_iso_week(datetime.date(2026, 11, 9)), datetime.date(2026, 11, 15))
        self.assertEqual(end_of_iso_week(datetime.date(2026, 11, 12)), datetime.date(2026, 11, 15))
        self.assertEqual(end_of_iso_week(datetime.date(2026, 11, 15)), datetime.date(2026, 11, 15))
        self.assertEqual(end_of_iso_week(datetime.date(2026, 11, 15)).weekday(), 6)

    def test_end_of_iso_week_defaults_to_today(self):
        frozen = datetime.datetime(2026, 11, 15, 21, 0, tzinfo=datetime.timezone.utc)
        with _freeze(frozen):
            # Already Monday 2026-11-16 in Tbilisi -> week ends 2026-11-22.
            self.assertEqual(end_of_iso_week(), datetime.date(2026, 11, 22))


class TestIsOverdueTask(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.board, self.todo, self.progress, self.done = self.setup_board_with_columns()
        self.frozen = datetime.datetime(2026, 11, 9, 21, 30, tzinfo=datetime.timezone.utc)

    def test_no_due_date(self):
        t = self.create_ticket(column=self.todo)
        self.assertFalse(t.is_overdue_task)

    def test_overdue_uses_tbilisi_today(self):
        # Tbilisi today = 2026-11-10, so a 2026-11-09 due date is overdue
        # even though the UTC date is still 2026-11-09.
        t = self.create_ticket(column=self.todo, due_date=datetime.date(2026, 11, 9))
        with _freeze(self.frozen):
            self.assertTrue(t.is_overdue_task)

    def test_due_today_not_overdue(self):
        t = self.create_ticket(column=self.todo, due_date=datetime.date(2026, 11, 10))
        with _freeze(self.frozen):
            self.assertFalse(t.is_overdue_task)

    def test_closed_column_never_overdue(self):
        t = self.create_ticket(column=self.done, due_date=datetime.date(2026, 1, 1))
        with _freeze(self.frozen):
            self.assertFalse(t.is_overdue_task)

    def test_independent_of_payment_overdue(self):
        t = self.create_ticket(
            column=self.todo, payment_due_date=datetime.date(2020, 1, 1),
            price=10, due_date=None,
        )
        self.assertTrue(t.is_overdue)
        self.assertFalse(t.is_overdue_task)


class TestConftestHelpers(TicketTestCase):

    def test_create_board_access_lists(self):
        u = self.create_user(email='helper-u@test.com')
        g = self.create_tenant_group(name='Helper G')
        open_board = self.create_board(name='Open', open=True)
        self.assertFalse(open_board.board_users.exists() or open_board.board_groups.exists())
        restricted = self.create_board(name='Restricted', open=False)
        self.assertEqual(list(restricted.board_users.all()), [restricted.created_by])
        listed = self.create_board(name='Listed', users=[u], groups=[g], color='#ff0000')
        self.assertEqual(list(listed.board_users.all()), [u])
        self.assertEqual(list(listed.board_groups.all()), [g])
        self.assertEqual(listed.color, '#ff0000')
        with self.assertRaises(ValueError):
            self.create_board(name='Bad', open=True, users=[u])

    def test_make_inactive_group(self):
        g = self.make_inactive_group()
        self.assertFalse(g.is_active)

    def test_create_client(self):
        c = self.create_client(name='Nino', email='nino@test.com', phone='+995555')
        self.assertEqual((c.name, c.email, c.phone), ('Nino', 'nino@test.com', '+995555'))

    def test_capture_and_constant_queries(self):
        with capture_queries() as ctx:
            list(Board.objects.all())
        # >= 1: tenant_schemas may also emit a SET search_path.
        self.assertTrue(any('tickets_board' in q['sql'] for q in ctx.captured_queries))
        n_small, n_large = assert_constant_queries(
            lambda: list(Board.objects.all()), lambda: list(Board.objects.all()),
        )
        self.assertEqual(n_small, n_large)
        with self.assertRaises(AssertionError):
            assert_constant_queries(
                lambda: list(Board.objects.all()),
                lambda: [list(Board.objects.all()) for _ in range(2)],
            )
