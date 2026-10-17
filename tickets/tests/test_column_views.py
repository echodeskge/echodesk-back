"""Tests for TicketColumnViewSet."""
from tickets.models import TicketColumn
from tickets.tests.conftest import TicketTestCase


class TestColumnCRUD(TicketTestCase):

    def test_superadmin_can_create_column(self):
        admin = self.create_admin()
        admin.is_superuser = True
        admin.save()
        board = self.create_board(created_by=admin)
        resp = self.api_post('/api/columns/', {
            'name': 'New Column', 'board': board.id, 'position': 0
        }, user=admin)
        self.assertEqual(resp.status_code, 201)

    def test_agent_without_manage_columns_cannot_create_column(self):
        admin = self.create_admin()
        agent = self.create_user(email='col-agent-create@test.com')  # plain agent: no manage_columns, not staff
        board = self.create_board(created_by=admin)
        resp = self.api_post('/api/columns/', {
            'name': 'New Column', 'board': board.id, 'position': 0
        }, user=agent)
        self.assertEqual(resp.status_code, 403)

    def test_superadmin_can_update_column(self):
        admin = self.create_admin()
        admin.is_superuser = True
        admin.save()
        board = self.create_board(created_by=admin)
        col = self.create_column(board, name='Old', position=0, created_by=admin)
        resp = self.api_patch(f'/api/columns/{col.id}/', {'name': 'New'}, user=admin)
        self.assertEqual(resp.status_code, 200)

    def test_agent_without_manage_columns_cannot_update_column(self):
        admin = self.create_admin()
        agent = self.create_user(email='col-agent-update@test.com')
        board = self.create_board(created_by=admin)
        col = self.create_column(board, name='Col', position=0, created_by=admin)
        resp = self.api_patch(f'/api/columns/{col.id}/', {'name': 'New'}, user=agent)
        self.assertEqual(resp.status_code, 403)

    def test_superadmin_can_delete_column(self):
        admin = self.create_admin()
        admin.is_superuser = True
        admin.save()
        board = self.create_board(created_by=admin)
        col = self.create_column(board, name='Del', position=0, created_by=admin)
        resp = self.api_delete(f'/api/columns/{col.id}/', user=admin)
        self.assertEqual(resp.status_code, 204)

    def test_agent_without_manage_columns_cannot_delete_column(self):
        admin = self.create_admin()
        agent = self.create_user(email='col-agent-delete@test.com')
        board = self.create_board(created_by=admin)
        col = self.create_column(board, name='Col', position=0, created_by=admin)
        resp = self.api_delete(f'/api/columns/{col.id}/', user=agent)
        self.assertEqual(resp.status_code, 403)

    def test_anyone_authenticated_can_list_columns(self):
        user = self.create_user(email='regularuser@test.com')
        admin = self.create_admin()
        board = self.create_board(created_by=admin)
        self.create_column(board, name='Col 1', position=0, created_by=admin)
        resp = self.api_get('/api/columns/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(self.get_results(resp)), 1)

    def test_filter_columns_by_board(self):
        admin = self.create_admin()
        admin.is_superuser = True
        admin.save()
        board1 = self.create_board(name='Board 1', created_by=admin)
        board2 = self.create_board(name='Board 2', created_by=admin)
        self.create_column(board1, name='C1', position=0, created_by=admin)
        self.create_column(board2, name='C2', position=0, created_by=admin)
        resp = self.api_get(f'/api/columns/?board={board1.id}', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 1)
        self.assertEqual(self.get_results(resp)[0]['name'], 'C1')


class TestColumnReorder(TicketTestCase):

    def _make_superadmin(self):
        admin = self.create_admin()
        admin.is_superuser = True
        admin.save()
        return admin

    def test_superadmin_can_reorder_column(self):
        admin = self._make_superadmin()
        board = self.create_board(created_by=admin)
        col = self.create_column(board, name='C', position=0, created_by=admin)
        resp = self.api_post(f'/api/columns/{col.id}/reorder/', {'position': 2}, user=admin)
        self.assertEqual(resp.status_code, 200)

    def test_agent_without_manage_columns_cannot_reorder(self):
        admin = self.create_admin()
        agent = self.create_user(email='col-agent-reorder@test.com')
        board = self.create_board(created_by=admin)
        col = self.create_column(board, name='C', position=0, created_by=admin)
        resp = self.api_post(f'/api/columns/{col.id}/reorder/', {'position': 2}, user=agent)
        self.assertEqual(resp.status_code, 403)

    def test_reorder_missing_position(self):
        admin = self._make_superadmin()
        board = self.create_board(created_by=admin)
        col = self.create_column(board, name='C', position=0, created_by=admin)
        resp = self.api_post(f'/api/columns/{col.id}/reorder/', {}, user=admin)
        self.assertEqual(resp.status_code, 400)

    def test_reorder_invalid_position(self):
        admin = self._make_superadmin()
        board = self.create_board(created_by=admin)
        col = self.create_column(board, name='C', position=0, created_by=admin)
        resp = self.api_post(f'/api/columns/{col.id}/reorder/', {'position': 'abc'}, user=admin)
        self.assertEqual(resp.status_code, 400)


class TestColumnKanbanBoard(TicketTestCase):

    def test_kanban_board_with_board_id(self):
        admin = self.create_admin()
        board, col1, col2, col3 = self.setup_board_with_columns(admin=admin)
        resp = self.api_get(f'/api/columns/kanban_board/?board_id={board.id}', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('columns', resp.data)

    def test_kanban_board_default(self):
        admin = self.create_admin()
        board, col1, col2, col3 = self.setup_board_with_columns(admin=admin)
        board.is_default = True
        board.save()
        resp = self.api_get('/api/columns/kanban_board/', user=admin)
        self.assertEqual(resp.status_code, 200)

    def test_kanban_board_invalid_board(self):
        admin = self.create_admin()
        resp = self.api_get('/api/columns/kanban_board/?board_id=99999', user=admin)
        self.assertEqual(resp.status_code, 404)
