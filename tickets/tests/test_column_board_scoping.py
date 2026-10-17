"""
Column endpoints are scoped to the boards the caller can access.

Review repro (privilege escalation): a manager attached to board A only could
PATCH a column of board B to ``{"board": A}`` and so pull every ticket of that
column into a board they can see. ``board`` is now read-only on update and
every column action (list, retrieve, create, update, delete, reorder,
kanban_board) only sees columns of accessible boards. Staff and superusers
see all boards.
"""
from tickets.models import TicketColumn
from tickets.tests.conftest import TicketTestCase


class TestColumnBoardScoping(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.owner = self.create_admin(email='scope-owner@test.com')
        self.other = self.create_user(email='scope-other@test.com')
        self.manager = self.create_manager(email='scope-mgr@test.com')

        self.board_a = self.create_board(name='A', created_by=self.owner)
        self.board_a.board_users.add(self.manager)
        self.board_b = self.create_board(name='B', created_by=self.owner)
        self.board_b.board_users.add(self.other)  # B is restricted, manager not in it

        self.col_a = self.create_column(self.board_a, name='A-Todo', position=0)
        self.col_b = self.create_column(self.board_b, name='B-Todo', position=0)
        self.col_b2 = self.create_column(self.board_b, name='B-Done', position=1)
        self.ticket_b = self.create_ticket(title='Secret B ticket', column=self.col_b, created_by=self.owner)

    # ------------------------------------------------------- the exploit

    def test_manager_cannot_move_hidden_column_to_own_board(self):
        self.assertEqual(self.api_get(f'/api/tickets/{self.ticket_b.id}/', user=self.manager).status_code, 404)

        resp = self.api_patch(f'/api/columns/{self.col_b.id}/', {'board': self.board_a.id}, user=self.manager)
        self.assertEqual(resp.status_code, 404)
        self.col_b.refresh_from_db()
        self.assertEqual(self.col_b.board_id, self.board_b.id)

        resp = self.api_put(f'/api/columns/{self.col_b.id}/', {
            'name': 'B-Todo', 'board': self.board_a.id,
        }, user=self.manager)
        self.assertEqual(resp.status_code, 404)
        self.col_b.refresh_from_db()
        self.assertEqual(self.col_b.board_id, self.board_b.id)

        resp = self.api_get(f'/api/tickets/{self.ticket_b.id}/', user=self.manager)
        self.assertEqual(resp.status_code, 404)

    def test_board_is_read_only_on_update_even_between_accessible_boards(self):
        # Staff can see both boards, but moving a column is still not supported.
        staff = self.create_user(email='scope-staff@test.com', is_staff=True)
        resp = self.api_patch(f'/api/columns/{self.col_b.id}/', {
            'board': self.board_a.id, 'name': 'Renamed',
        }, user=staff)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['board'], self.board_b.id)
        self.col_b.refresh_from_db()
        self.assertEqual(self.col_b.board_id, self.board_b.id)
        self.assertEqual(self.col_b.name, 'Renamed')

    def test_put_with_same_board_still_works_for_own_board(self):
        # The web sends the column's current board on PUT; it is ignored.
        resp = self.api_put(f'/api/columns/{self.col_a.id}/', {
            'name': 'A-Doing', 'board': self.board_a.id,
        }, user=self.manager)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['board'], self.board_a.id)
        self.col_a.refresh_from_db()
        self.assertEqual(self.col_a.name, 'A-Doing')

    # ------------------------------------------------------------- reads

    def test_list_filtered_by_hidden_board_does_not_leak(self):
        resp = self.api_get(f'/api/columns/?board={self.board_b.id}', user=self.manager)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.get_results(resp), [])

    def test_unfiltered_list_only_has_accessible_boards(self):
        resp = self.api_get('/api/columns/', user=self.manager)
        self.assertEqual(resp.status_code, 200)
        ids = {c['id'] for c in self.get_results(resp)}
        self.assertIn(self.col_a.id, ids)
        self.assertNotIn(self.col_b.id, ids)
        self.assertNotIn(self.col_b2.id, ids)

    def test_retrieve_hidden_column_is_404(self):
        self.assertEqual(self.api_get(f'/api/columns/{self.col_b.id}/', user=self.manager).status_code, 404)
        self.assertEqual(self.api_get(f'/api/columns/{self.col_a.id}/', user=self.manager).status_code, 200)

    def test_plain_agent_reads_are_scoped_too(self):
        agent = self.create_user(email='scope-agent@test.com')
        self.board_a.board_users.add(agent)
        self.assertEqual(self.api_get(f'/api/columns/{self.col_b.id}/', user=agent).status_code, 404)
        resp = self.api_get(f'/api/columns/?board={self.board_b.id}', user=agent)
        self.assertEqual(self.get_results(resp), [])
        self.assertEqual(self.api_get(f'/api/columns/{self.col_a.id}/', user=agent).status_code, 200)

    def test_open_board_columns_visible_to_everyone(self):
        open_board = self.create_board(name='Open', created_by=self.owner)
        col = self.create_column(open_board, name='Open-Todo')
        agent = self.create_user(email='scope-open@test.com')
        self.assertEqual(self.api_get(f'/api/columns/{col.id}/', user=agent).status_code, 200)

    def test_group_access_counts(self):
        from users.models import TenantGroup
        group = TenantGroup.objects.create(name='B team')
        member = self.create_manager(email='scope-group@test.com')
        member.tenant_groups.add(group)
        self.board_b.board_groups.add(group)
        self.assertEqual(self.api_get(f'/api/columns/{self.col_b.id}/', user=member).status_code, 200)
        resp = self.api_patch(f'/api/columns/{self.col_b.id}/', {'name': 'B-Renamed'}, user=member)
        self.assertEqual(resp.status_code, 200)

    def test_staff_and_superuser_see_all_columns(self):
        for user in (self.create_user(email='scope-st@test.com', is_staff=True),
                     self.create_user(email='scope-su@test.com', is_superuser=True)):
            resp = self.api_get(f'/api/columns/?board={self.board_b.id}', user=user)
            self.assertEqual({c['id'] for c in self.get_results(resp)}, {self.col_b.id, self.col_b2.id})

    def test_columns_kanban_board_scoped(self):
        resp = self.api_get(f'/api/columns/kanban_board/?board_id={self.board_b.id}', user=self.manager)
        self.assertEqual(resp.status_code, 404)
        resp = self.api_get(f'/api/columns/kanban_board/?board_id={self.board_a.id}', user=self.manager)
        self.assertEqual(resp.status_code, 200)
        resp = self.api_get('/api/columns/kanban_board/?board_id=abc', user=self.manager)
        self.assertEqual(resp.status_code, 404)
        staff = self.create_user(email='scope-kb-staff@test.com', is_staff=True)
        resp = self.api_get(f'/api/columns/kanban_board/?board_id={self.board_b.id}', user=staff)
        self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------ writes

    def test_create_on_inaccessible_board_denied(self):
        resp = self.api_post('/api/columns/', {
            'name': 'Injected', 'board': self.board_b.id, 'position': 0,
        }, user=self.manager)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(TicketColumn.objects.filter(name='Injected').exists())

    def test_create_on_accessible_board_allowed(self):
        resp = self.api_post('/api/columns/', {
            'name': 'Review', 'board': self.board_a.id, 'position': 1,
        }, user=self.manager)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(TicketColumn.objects.filter(name='Review', board=self.board_a).exists())

    def test_staff_can_create_on_any_board(self):
        staff = self.create_user(email='scope-create-staff@test.com', is_staff=True)
        resp = self.api_post('/api/columns/', {
            'name': 'StaffCol', 'board': self.board_b.id, 'position': 2,
        }, user=staff)
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_delete_on_inaccessible_board_denied(self):
        resp = self.api_delete(f'/api/columns/{self.col_b2.id}/', user=self.manager)
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(TicketColumn.objects.filter(id=self.col_b2.id).exists())

    def test_delete_on_accessible_board_allowed(self):
        empty = self.create_column(self.board_a, name='A-Empty', position=1)
        resp = self.api_delete(f'/api/columns/{empty.id}/', user=self.manager)
        self.assertEqual(resp.status_code, 204)

    def test_reorder_on_inaccessible_board_denied(self):
        resp = self.api_post(f'/api/columns/{self.col_b2.id}/reorder/', {'position': 0}, user=self.manager)
        self.assertEqual(resp.status_code, 404)
        self.col_b2.refresh_from_db()
        self.assertEqual(self.col_b2.position, 1)
