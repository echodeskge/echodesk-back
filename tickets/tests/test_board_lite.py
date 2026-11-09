"""``GET boards/?lite=1`` (A1) and the board list order (E28)."""
from tickets.models import Board
from tickets.tests.conftest import TicketTestCase, capture_queries


def real_queries(ctx):
    return [q['sql'] for q in ctx.captured_queries if not q['sql'].startswith('SET search_path')]


class TestBoardLite(TicketTestCase):
    LITE_FIELDS = {'id', 'name', 'color', 'position', 'is_default', 'open_count', 'columns_count'}

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bl-admin@test.com')
        self.agent = self.create_user(email='bl-agent@test.com')

    def lite(self, user=None):
        resp = self.api_get('/api/boards/?lite=1&page_size=100', user=user or self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.data['results']

    def test_fields_present(self):
        board, *_ = self.setup_board_with_columns(admin=self.admin)
        board.color = '#abcdef'
        board.is_default = True
        board.save()
        rows = self.lite()
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0].keys()), self.LITE_FIELDS)
        self.assertEqual(rows[0]['color'], '#abcdef')
        self.assertIs(rows[0]['is_default'], True)
        self.assertEqual(rows[0]['columns_count'], 3)

    def test_open_count_excludes_closed_columns(self):
        board, todo, doing, done = self.setup_board_with_columns(admin=self.admin)
        for col in (todo, todo, doing, done, done):
            self.create_ticket(column=col, created_by=self.admin)
        empty = self.create_board(name='Empty', created_by=self.admin)
        rows = {r['id']: r for r in self.lite()}
        self.assertEqual(rows[board.id]['open_count'], 3)
        self.assertEqual(rows[empty.id]['open_count'], 0)
        self.assertEqual(rows[empty.id]['columns_count'], 0)

    def test_order_is_position_then_name(self):
        c = self.create_board(name='C', created_by=self.admin, position=0)
        a = self.create_board(name='A', created_by=self.admin, position=1)
        b = self.create_board(name='B', created_by=self.admin, position=0)
        self.assertEqual([r['id'] for r in self.lite()], [b.id, c.id, a.id])
        resp = self.api_get('/api/boards/?page_size=100', user=self.admin)
        self.assertEqual([r['id'] for r in resp.data['results']], [b.id, c.id, a.id])
        # Meta.ordering is unchanged (no AlterModelOptions migration)
        self.assertEqual(Board._meta.ordering, ['name'])

    def _make_boards(self, n):
        for i in range(n):
            board = self.create_board(name=f'Board {Board.objects.count()}', created_by=self.admin)
            col = self.create_column(board, name='Open', created_by=self.admin)
            self.create_column(board, name='Closed', position=1, is_closed_status=True, created_by=self.admin)
            self.create_ticket(column=col, created_by=self.admin)

    def test_query_count_does_not_depend_on_board_count(self):
        self._make_boards(3)
        with capture_queries() as small:
            self.assertEqual(len(self.lite()), 3)
        self._make_boards(27)
        with capture_queries() as large:
            self.assertEqual(len(self.lite()), 30)
        self.assertEqual(len(real_queries(small)), len(real_queries(large)), real_queries(large))
        offenders = [sql for sql in real_queries(large) if 'GROUP BY' in sql]
        self.assertEqual(offenders, [])

    def test_agent_query_count_does_not_depend_on_board_count(self):
        self._make_boards(3)
        with capture_queries() as small:
            self.lite(self.agent)
        self._make_boards(27)
        with capture_queries() as large:
            self.lite(self.agent)
        self.assertEqual(len(real_queries(small)), len(real_queries(large)))

    def test_without_lite_shape_is_unchanged_apart_from_color_and_position(self):
        self.setup_board_with_columns(admin=self.admin)
        resp = self.api_get('/api/boards/', user=self.admin)
        row = resp.data['results'][0]
        self.assertEqual(set(row.keys()), {
            'id', 'name', 'description', 'is_default', 'created_at', 'updated_at', 'created_by',
            'columns_count', 'order_users', 'board_groups', 'board_users', 'payment_summary',
            'color', 'position',
        })
        self.assertNotIn('open_count', row)

    def test_agent_sees_only_accessible_boards_open_included(self):
        group = self.create_tenant_group(name='BL group')
        self.agent.tenant_groups.add(group)
        inactive = self.make_inactive_group(name='BL inactive')
        self.agent.tenant_groups.add(inactive)
        open_board = self.create_board(name='Open', created_by=self.admin, open=True)
        mine = self.create_board(name='Mine', created_by=self.admin, users=[self.agent])
        orders = self.create_board(name='Orders', created_by=self.admin, order_users=[self.agent])
        by_group = self.create_board(name='Group', created_by=self.admin, groups=[group])
        self.create_board(name='Inactive group', created_by=self.admin, groups=[inactive])
        self.create_board(name='Restricted', created_by=self.admin, open=False)
        ids = {r['id'] for r in self.lite(self.agent)}
        self.assertEqual(ids, {open_board.id, mine.id, orders.id, by_group.id})
        self.assertEqual(len(self.lite(self.admin)), 6)
