"""``GET tickets/view_counts/`` (A5).

The clock is frozen at 2026-11-09 21:30 UTC (2026-11-10 in Tbilisi; the ISO
week ends Sunday 2026-11-15) and every due date is absolute.
"""
import datetime
import re
from unittest import mock

from tickets.models import Ticket, TicketAssignment
from tickets.tests.conftest import TicketTestCase, capture_queries

FROZEN_NOW = datetime.datetime(2026, 11, 9, 21, 30, tzinfo=datetime.timezone.utc)
D = datetime.date
TICKETS_TABLE = re.compile(r'FROM "tickets_ticket"(?!_)')


def frozen():
    return mock.patch('tickets.dates.timezone.now', return_value=FROZEN_NOW)


class TestViewCounts(TicketTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='vc-admin@test.com')
        self.agent = self.create_user(email='vc-agent@test.com')
        self.board, self.todo, self.doing, self.done = self.setup_board_with_columns(admin=self.admin)

    def t(self, column=None, **kwargs):
        return self.create_ticket(column=column or self.todo, created_by=self.admin, **kwargs)

    def counts(self, user=None, board=None):
        url = '/api/tickets/view_counts/'
        if board is not None:
            url += f'?board={board.id}'
        with frozen():
            resp = self.api_get(url, user=user or self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.data

    def test_four_counts(self):
        mine_legacy = self.t(assigned_to=self.admin, priority='low')
        mine_m2m = self.t(priority='low')
        TicketAssignment.objects.create(ticket=mine_m2m, user=self.admin)
        self.t(priority='critical')                                   # unassigned + urgent
        self.t(priority='high', due_date=D(2026, 11, 9))              # unassigned + urgent + due (overdue)
        self.t(priority='medium', due_date=D(2026, 11, 15))           # unassigned + due (Sunday)
        self.t(priority='medium', due_date=D(2026, 11, 16))           # unassigned, next week
        self.assertTrue(mine_legacy and mine_m2m)
        self.assertEqual(self.counts(), {'mine': 2, 'unassigned': 4, 'urgent': 2, 'due_this_week': 2})

    def test_open_tickets_only(self):
        self.t(column=self.done, assigned_to=self.admin, priority='critical', due_date=D(2026, 11, 1))
        self.assertEqual(self.counts(), {'mine': 0, 'unassigned': 0, 'urgent': 0, 'due_this_week': 0})

    def test_scoped_to_board(self):
        other, o_col, *_ = self.setup_board_with_columns(admin=self.admin)
        self.t(priority='critical')
        self.create_ticket(column=o_col, created_by=self.admin, priority='critical')
        self.create_ticket(column=o_col, created_by=self.admin, priority='high')
        self.assertEqual(self.counts(board=self.board)['urgent'], 1)
        self.assertEqual(self.counts(board=other)['urgent'], 2)
        self.assertEqual(self.counts()['urgent'], 3)

    def test_agent_visibility(self):
        restricted = self.create_board(name='R', created_by=self.admin, open=False)
        r_col = self.create_column(restricted, name='R', created_by=self.admin)
        self.create_ticket(column=r_col, created_by=self.admin, priority='critical')
        self.create_ticket(column=r_col, created_by=self.admin, priority='critical', assigned_to=self.agent)
        self.t(priority='critical')
        self.t(assigned_to=self.agent)
        # Without board: accessible boards only.
        self.assertEqual(self.counts(user=self.agent), {'mine': 1, 'unassigned': 1, 'urgent': 1, 'due_this_week': 0})
        # On the restricted board the agent only counts what it can see.
        self.assertEqual(
            self.counts(user=self.agent, board=restricted),
            {'mine': 1, 'unassigned': 0, 'urgent': 1, 'due_this_week': 0},
        )
        self.assertEqual(self.counts(board=restricted)['urgent'], 2)

    def test_bad_board_is_400(self):
        resp = self.api_get('/api/tickets/view_counts/?board=abc', user=self.admin)
        self.assertEqual(resp.status_code, 400)

    def _measure(self, user):
        with frozen(), capture_queries() as ctx:
            resp = self.api_get(f'/api/tickets/view_counts/?board={self.board.id}', user=user)
        self.assertEqual(resp.status_code, 200)
        real = [q['sql'] for q in ctx.captured_queries if not q['sql'].startswith('SET search_path')]
        ticket_queries = [sql for sql in real if TICKETS_TABLE.search(sql)]
        return len(real), ticket_queries

    def test_constant_queries_and_one_aggregate(self):
        for user in (self.admin, self.agent):
            Ticket.objects.all().delete()
            for i in range(3):
                self.t(priority='high', assigned_to=self.agent if i else None)
            n_small, small_ticket_q = self._measure(user)
            for i in range(27):
                t = self.t(priority='low', due_date=D(2026, 11, 12))
                TicketAssignment.objects.create(ticket=t, user=self.agent)
            n_large, large_ticket_q = self._measure(user)
            self.assertEqual(n_small, n_large)
            self.assertEqual(len(large_ticket_q), 1, large_ticket_q)
            self.assertEqual(len(small_ticket_q), 1)
