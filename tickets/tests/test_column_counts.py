"""``GET tickets/column_counts/`` (A5b)."""
from urllib.parse import urlencode

from tickets.tests.conftest import TicketTestCase, capture_queries


class TestColumnCounts(TicketTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='cc-admin@test.com')
        self.agent = self.create_user(email='cc-agent@test.com')
        self.board, self.todo, self.doing, self.done = self.setup_board_with_columns(admin=self.admin)
        self.tag = self.create_tag(name='Billing', created_by=self.admin)

    def counts(self, params=None, user=None, board=None):
        query = {'board': (board or self.board).id, **(params or {})}
        resp = self.api_get(f'/api/tickets/column_counts/?{urlencode(query)}', user=user or self.admin)
        return resp

    def kanban_counts(self, params=None, user=None):
        query = {'lite': 1, **(params or {})}
        resp = self.api_get(f'/api/boards/{self.board.id}/kanban_board/?{urlencode(query)}', user=user or self.admin)
        self.assertEqual(resp.status_code, 200)
        return {str(col): len(cards) for col, cards in resp.data['tickets_by_column'].items()}

    def test_every_column_present_zeros_included(self):
        self.create_ticket(column=self.todo, created_by=self.admin)
        self.create_ticket(column=self.todo, created_by=self.admin)
        resp = self.counts()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {'counts': {str(self.todo.id): 2, str(self.doing.id): 0, str(self.done.id): 0}})

    def test_counts_match_filtered_kanban(self):
        for i, col in enumerate([self.todo, self.todo, self.doing, self.done, self.doing]):
            t = self.create_ticket(
                title=f'Ticket {i}', column=col, created_by=self.admin,
                priority='critical' if i % 2 else 'low',
                assigned_to=self.agent if i < 3 else None,
            )
            if i in (0, 2, 3):
                t.tags.add(self.tag)
        for params in ({}, {'priority__in': 'critical'}, {'tags__in': str(self.tag.id)},
                       {'assignee': str(self.agent.id)}, {'unassigned': '1'}, {'q': 'billing'},
                       {'q': 'Ticket 3'}, {'is_closed': 'false'}):
            self.assertEqual(self.counts(params).data['counts'], self.kanban_counts(params), params)

    def test_visibility(self):
        restricted = self.create_board(name='R', created_by=self.admin, open=False)
        r_col = self.create_column(restricted, name='R', created_by=self.admin)
        self.create_ticket(column=r_col, created_by=self.admin)
        self.assertEqual(self.counts(user=self.agent, board=restricted).status_code, 404)
        self.assertEqual(self.counts(user=self.admin, board=restricted).data['counts'], {str(r_col.id): 1})

    def test_board_is_required(self):
        resp = self.api_get('/api/tickets/column_counts/', user=self.admin)
        self.assertEqual(resp.status_code, 400)

    def test_one_grouped_query_whatever_the_size(self):
        def measure():
            with capture_queries() as ctx:
                self.assertEqual(self.counts(user=self.agent).status_code, 200)
            return [q['sql'] for q in ctx.captured_queries if not q['sql'].startswith('SET search_path')]

        for col in (self.todo, self.doing):
            self.create_ticket(column=col, created_by=self.admin)
        small = measure()
        for i in range(20):
            self.create_ticket(column=[self.todo, self.doing, self.done][i % 3], created_by=self.admin)
        large = measure()
        self.assertEqual(len(small), len(large))
        grouped = [sql for sql in large if 'GROUP BY' in sql]
        self.assertEqual(len(grouped), 1, grouped)
