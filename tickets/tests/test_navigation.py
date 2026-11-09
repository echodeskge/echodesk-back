"""``GET tickets/{id}/navigation/`` (A6)."""
from urllib.parse import urlencode

from tickets.models import Ticket
from tickets.tests.conftest import TicketTestCase, capture_queries


class NavigationFixture(TicketTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='nav-admin@test.com')
        self.agent = self.create_user(email='nav-agent@test.com')
        self.board, self.todo, self.doing, self.done = self.setup_board_with_columns(admin=self.admin)

    def t(self, title, column, pos, **kwargs):
        return self.at(self.create_ticket(title=title, column=column, created_by=self.admin, **kwargs), pos)

    @staticmethod
    def at(ticket, pos):
        """Pin position_in_column (Ticket.save appends a new ticket at 0)."""
        Ticket.objects.filter(pk=ticket.pk).update(position_in_column=pos)
        ticket.position_in_column = pos
        return ticket

    def nav(self, ticket, params=None, user=None):
        query = urlencode(params or {})
        resp = self.api_get(f'/api/tickets/{ticket.id}/navigation/?{query}', user=user or self.admin)
        return resp

    def assertNav(self, ticket, index, total, prev, nxt, params=None, user=None):
        resp = self.nav(ticket, params, user)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data, {
            'index': index, 'total': total,
            'prev_id': prev.id if prev else None, 'next_id': nxt.id if nxt else None,
        }, params)


class TestBoardOrder(NavigationFixture):
    def setUp(self):
        super().setUp()
        # Created out of order on purpose: the board order is column position, then position.
        self.d0 = self.t('d0', self.done, 0)
        self.p1 = self.t('p1', self.doing, 1)
        self.a1 = self.t('a1', self.todo, 1)
        self.a0 = self.t('a0', self.todo, 0)
        self.p0 = self.t('p0', self.doing, 0)
        # board order: a0, a1, p0, p1, d0

    def test_index_total_prev_next(self):
        self.assertNav(self.a1, 2, 5, self.a0, self.p0)
        self.assertNav(self.p0, 3, 5, self.a1, self.p1, {'view': 'board'})

    def test_first_and_last_have_null_neighbours(self):
        self.assertNav(self.a0, 1, 5, None, self.a1)
        self.assertNav(self.d0, 5, 5, self.p1, None)

    def test_equal_positions_break_ties_like_the_kanban(self):
        """R14: equal position_in_column -> -created_at, -id, as the kanban."""
        x = self.t('x', self.todo, 5)
        y = self.t('y', self.todo, 5)
        z = self.t('z', self.todo, 5)
        Ticket.objects.filter(pk__in=[x.pk, y.pk, z.pk]).update(created_at=x.created_at)
        kanban = self.api_get(f'/api/boards/{self.board.id}/kanban_board/?lite=1', user=self.admin)
        todo_ids = [c['id'] for c in kanban.data['tickets_by_column'][self.todo.id]]
        self.assertEqual(todo_ids, [self.a0.id, self.a1.id, z.id, y.id, x.id])
        self.assertNav(z, 3, 8, self.a1, y)
        self.assertNav(y, 4, 8, z, x)
        self.assertNav(x, 5, 8, y, self.p0)

    def test_board_view_with_a_sort_follows_the_sorted_columns(self):
        self.a0.priority = 'low'
        self.a0.save()
        self.a1.priority = 'critical'
        self.a1.save()
        self.assertNav(self.a1, 1, 5, None, self.a0, {'ordering': 'priority_rank'})

    def test_filters_narrow_the_order(self):
        for t in (self.a0, self.p1, self.d0):
            t.priority = 'critical'
            t.save()
        self.assertNav(self.p1, 2, 3, self.a0, self.d0, {'priority__in': 'critical'})

    def test_ticket_outside_the_filter_falls_back_to_board_order(self):
        self.a1.priority = 'critical'
        self.a1.save()
        self.assertNav(self.p0, None, 5, self.a1, self.p1, {'priority__in': 'critical'})

    def test_other_boards_are_not_counted(self):
        other, o_col, *_ = self.setup_board_with_columns(admin=self.admin)
        self.create_ticket(column=o_col, created_by=self.admin)
        self.assertNav(self.a0, 1, 5, None, self.a1)


class TestListOrder(NavigationFixture):
    def test_list_view_with_ordering_and_filters(self):
        a = self.t('a', self.todo, 0, priority='high')
        b = self.t('b', self.todo, 1, priority='low')
        c = self.t('c', self.doing, 0, priority='high')
        d = self.t('d', self.todo, 2, priority='high')
        # list order with group=column: todo (-created_at: d, b, a), then doing (c)
        self.assertNav(b, 2, 4, d, a, {'view': 'list', 'ordering': '-created_at'})
        self.assertNav(c, 4, 4, a, None, {'view': 'list'})
        # filtered to high: d, a, c
        self.assertNav(a, 2, 3, d, c, {'view': 'list', 'ordering': '-created_at', 'priority__in': 'high'})
        # ascending created_at: a, d, then c
        self.assertNav(d, 2, 3, a, c, {'view': 'list', 'ordering': 'created_at', 'priority__in': 'high'})

    def test_list_matches_the_list_endpoint(self):
        tickets = [self.t(f'L{i}', [self.todo, self.doing][i % 2], i, priority=['low', 'high', 'critical'][i % 3])
                   for i in range(7)]
        resp = self.api_get(
            f'/api/tickets/?board={self.board.id}&group=column&ordering=priority_rank&page_size=100',
            user=self.admin,
        )
        order = [row['id'] for row in resp.data['results']]
        by_id = {t.id: t for t in tickets}
        for i, tid in enumerate(order):
            expected_prev = by_id[order[i - 1]] if i else None
            expected_next = by_id[order[i + 1]] if i + 1 < len(order) else None
            self.assertNav(by_id[tid], i + 1, 7, expected_prev, expected_next,
                           {'view': 'list', 'ordering': 'priority_rank'})


class TestNavigationVisibility(NavigationFixture):
    def test_agent_never_gets_an_invisible_neighbour(self):
        restricted = self.create_board(name='R', created_by=self.admin, open=False)
        r_col = self.create_column(restricted, name='R', created_by=self.admin)
        hidden_before = self.at(self.create_ticket(title='hidden', column=r_col, created_by=self.admin), 0)
        mine = self.at(self.create_ticket(title='mine', column=r_col, created_by=self.admin,
                                          assigned_to=self.agent), 1)
        hidden_after = self.at(self.create_ticket(title='hidden 2', column=r_col, created_by=self.admin), 2)
        self.assertNav(mine, 1, 1, None, None, user=self.agent)
        self.assertNav(mine, 2, 3, hidden_before, hidden_after, user=self.admin)

    def test_invisible_ticket_is_404(self):
        restricted = self.create_board(name='R', created_by=self.admin, open=False)
        r_col = self.create_column(restricted, name='R', created_by=self.admin)
        hidden = self.create_ticket(title='hidden', column=r_col, created_by=self.admin)
        self.assertEqual(self.nav(hidden, user=self.agent).status_code, 404)

    def test_ticket_without_column(self):
        loose = self.create_ticket(title='loose', column=self.todo, created_by=self.admin)
        Ticket.objects.filter(pk=loose.pk).update(column=None)  # save() would pick a default column
        resp = self.nav(loose)
        self.assertEqual(resp.data, {'index': None, 'total': 0, 'prev_id': None, 'next_id': None})


class TestNavigationQueryCount(NavigationFixture):
    def _measure(self, ticket, params):
        with capture_queries() as ctx:
            resp = self.nav(ticket, params, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        return len([q for q in ctx.captured_queries if not q['sql'].startswith('SET search_path')])

    def test_constant_for_5_and_50_tickets(self):
        target = self.t('target', self.todo, 0, priority='high')
        for i in range(4):
            self.t(f's{i}', self.doing, i, priority='high')
        cases = ({}, {'priority__in': 'high'}, {'priority__in': 'low'}, {'view': 'list', 'ordering': 'due_date'})
        small = [self._measure(target, p) for p in cases]
        for i in range(45):
            self.t(f'l{i}', [self.todo, self.doing, self.done][i % 3], i + 1, priority='high')
        large = [self._measure(target, p) for p in cases]
        self.assertEqual(small, large)
