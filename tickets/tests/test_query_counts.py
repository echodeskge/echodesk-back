"""Query-count bounds for the ticket read and write endpoints (BE-07, A7).

Each bound is checked as "count at N == count at M" (no N+1) plus a loose
absolute ceiling. ``SET search_path`` statements from django-tenant-schemas
are noise and are not counted.
"""
from tickets.models import TicketAssignment
from tickets.tests.conftest import TicketTestCase, capture_queries


def count_real(ctx):
    return len([q for q in ctx.captured_queries if not q['sql'].startswith('SET search_path')])


class QueryCountFixture(TicketTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='qc-admin@test.com')
        self.agent = self.create_user(email='qc-agent@test.com')
        self.helper = self.create_user(email='qc-helper@test.com')
        self.board, self.todo, self.doing, self.done = self.setup_board_with_columns(admin=self.admin)
        self.tags = [self.create_tag(name=f'QC{i}', created_by=self.admin) for i in range(2)]
        self.group = self.create_tenant_group(name='QC group')

    def rich_ticket(self, i=0, column=None):
        t = self.create_ticket(title=f'QC {i}', column=column or self.todo, created_by=self.admin,
                               assigned_to=self.agent)
        t.tags.add(*self.tags)
        t.assigned_groups.add(self.group)
        TicketAssignment.objects.create(ticket=t, user=self.helper, role='collaborator', assigned_by=self.admin)
        self.create_comment(t, self.helper, 'c')
        self.create_checklist_item(t, text='x')
        return t

    def measure(self, method, url, user, data=None):
        with capture_queries() as ctx:
            resp = getattr(self, f'api_{method}')(url, data, user=user) if data is not None \
                else getattr(self, f'api_{method}')(url, user=user)
        self.assertLess(resp.status_code, 300, getattr(resp, 'data', None))
        return count_real(ctx), resp


class TestListQueries(QueryCountFixture):
    def test_list_25_vs_50(self):
        for user in (self.admin, self.agent):
            for i in range(25 - len(self._ids())):
                self.rich_ticket(i)
            n25, resp = self.measure('get', f'/api/tickets/?board={self.board.id}&page_size=100', user)
            self.assertEqual(len(resp.data['results']), 25)
            for i in range(25):
                self.rich_ticket(100 + i)
            n50, resp = self.measure('get', f'/api/tickets/?board={self.board.id}&page_size=100', user)
            self.assertEqual(len(resp.data['results']), 50)
            self.assertEqual(n25, n50)
            self.assertLessEqual(n50, 15)
            # filters and ordering do not add queries
            n_filtered, _ = self.measure(
                'get',
                f'/api/tickets/?board={self.board.id}&page_size=100&group=column&ordering=priority_rank'
                f'&tags__in={self.tags[0].id}&assignee={self.agent.id}&search=QC',
                user,
            )
            self.assertEqual(n_filtered, n50)
            self._delete_all()

    def test_my_tickets_and_assigned_to_me_are_bounded(self):
        for i in range(3):
            self.rich_ticket(i)
        small = [self.measure('get', url, self.agent)[0]
                 for url in ('/api/tickets/my_tickets/', '/api/tickets/assigned_to_me/')]
        for i in range(12):
            self.rich_ticket(10 + i)
        large = [self.measure('get', url, self.agent)[0]
                 for url in ('/api/tickets/my_tickets/', '/api/tickets/assigned_to_me/')]
        self.assertEqual(small, large)

    def _ids(self):
        from tickets.models import Ticket
        return list(Ticket.objects.values_list('id', flat=True))

    def _delete_all(self):
        from tickets.models import Ticket
        Ticket.objects.all().delete()


class TestDetailQueries(QueryCountFixture):
    def _with_children(self, ticket, n):
        for i in range(n):
            self.create_comment(ticket, self.helper, f'c{i}')
            self.create_checklist_item(ticket, text=f'i{i}')
            self.create_attachment(ticket, uploaded_by=self.admin, file=f'attachments/q{i}.txt')

    def test_retrieve_0_vs_10_children(self):
        bare = self.create_ticket(title='bare', column=self.todo, created_by=self.admin, assigned_to=self.agent)
        full = self.create_ticket(title='full', column=self.todo, created_by=self.admin, assigned_to=self.agent)
        self._with_children(full, 10)
        for user in (self.admin, self.agent):
            n0, _ = self.measure('get', f'/api/tickets/{bare.id}/', user)
            n10, resp = self.measure('get', f'/api/tickets/{full.id}/', user)
            self.assertEqual(len(resp.data['comments']), 10)
            self.assertEqual(n0, n10, user.email)
            self.assertLessEqual(n10, 25)

    def test_patch_response_is_bounded(self):
        bare = self.create_ticket(title='bare', column=self.todo, created_by=self.admin)
        full = self.create_ticket(title='full', column=self.todo, created_by=self.admin)
        self._with_children(full, 10)
        n0, _ = self.measure('patch', f'/api/tickets/{bare.id}/', self.admin, {'title': 'bare 2'})
        n10, resp = self.measure('patch', f'/api/tickets/{full.id}/', self.admin, {'title': 'full 2'})
        self.assertEqual(resp.data['title'], 'full 2')
        self.assertEqual(n0, n10)
        self.assertLessEqual(n10, 50)

    def test_move_to_column_is_bounded(self):
        # Each moving ticket leaves its own source column behind a ticket that
        # stays, so both moves do the same renumbering work.
        src_a = self.create_column(self.board, name='Src A', position=3, created_by=self.admin)
        src_b = self.create_column(self.board, name='Src B', position=4, created_by=self.admin)
        for col in (src_a, src_b, self.todo):
            self.create_ticket(title='stays', column=col, created_by=self.admin)
        bare = self.create_ticket(title='bare', column=src_a, created_by=self.admin)
        full = self.create_ticket(title='full', column=src_b, created_by=self.admin)
        self._with_children(full, 10)
        for i in range(5):
            self.create_ticket(title=f'n{i}', column=self.doing, created_by=self.admin)
        # Warm-up: the first request of a test fills per-process caches.
        warm = self.create_ticket(title='warm', column=self.todo, created_by=self.admin)
        self.measure('patch', f'/api/tickets/{warm.id}/move_to_column/', self.admin,
                     {'column_id': self.doing.id, 'position_in_column': 0})
        n0, _ = self.measure('patch', f'/api/tickets/{bare.id}/move_to_column/', self.admin,
                             {'column_id': self.doing.id, 'position_in_column': 0})
        n10, resp = self.measure('patch', f'/api/tickets/{full.id}/move_to_column/', self.admin,
                                 {'column_id': self.doing.id, 'position_in_column': 0})
        self.assertEqual(resp.data['id'], full.id)
        self.assertEqual(len(resp.data['comments']), 10)
        self.assertEqual(n0, n10)
        self.assertLessEqual(n10, 60)


class TestKanbanLiteQueries(QueryCountFixture):
    def test_kanban_lite_3_vs_30_tickets(self):
        url = f'/api/boards/{self.board.id}/kanban_board/?lite=1'
        for i in range(3):
            self.rich_ticket(i, column=[self.todo, self.doing, self.done][i % 3])
        n3 = {}
        for user in (self.admin, self.agent):
            n3[user.id], _ = self.measure('get', url, user)
            self.assertLessEqual(n3[user.id], 12)
        for i in range(27):
            self.rich_ticket(10 + i, column=[self.todo, self.doing, self.done][i % 3])
        for user in (self.admin, self.agent):
            n30, resp = self.measure('get', url, user)
            self.assertEqual(sum(len(c) for c in resp.data['tickets_by_column'].values()), 30)
            self.assertEqual(n30, n3[user.id])
            self.assertLessEqual(n30, 12)
        n_filtered, _ = self.measure('get', url + f'&tags__in={self.tags[0].id}&q=QC&ordering=due_date', self.agent)
        self.assertLessEqual(n_filtered, 12)
