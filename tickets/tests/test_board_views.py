"""Tests for BoardViewSet."""
from django.db.models import Count
from tickets.models import Board
from tickets.tests.conftest import TicketTestCase


class TestBoardCRUD(TicketTestCase):

    def test_admin_can_create_board(self):
        admin = self.create_admin()
        resp = self.api_post('/api/boards/', {'name': 'New Board'}, user=admin)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['name'], 'New Board')

    def test_admin_can_update_board(self):
        admin = self.create_admin()
        board = self.create_board(created_by=admin)
        resp = self.api_patch(f'/api/boards/{board.id}/', {'name': 'Updated'}, user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['name'], 'Updated')

    def test_admin_can_delete_board(self):
        admin = self.create_admin()
        board = self.create_board(created_by=admin)
        resp = self.api_delete(f'/api/boards/{board.id}/', user=admin)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Board.objects.filter(id=board.id).exists())

    def test_list_boards(self):
        admin = self.create_admin()
        self.create_board(name='Board A', created_by=admin)
        self.create_board(name='Board B', created_by=admin)
        resp = self.api_get('/api/boards/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 2)


class TestBoardPermissions(TicketTestCase):

    def test_staff_can_access_all_boards(self):
        admin = self.create_admin()
        self.create_board(name='Board 1', created_by=admin)
        self.create_board(name='Board 2', created_by=admin)
        resp = self.api_get('/api/boards/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 2)

    def test_unauthenticated_denied(self):
        resp = self.api_get('/api/boards/')
        self.assertIn(resp.status_code, [401, 403])

    def test_user_with_view_boards_permission(self):
        user = self.create_user(email='viewer@test.com', can_view_boards=True)
        admin = self.create_admin()
        self.create_board(name='B1', created_by=admin)
        resp = self.api_get('/api/boards/', user=user)
        self.assertEqual(resp.status_code, 200)

    def test_user_with_access_orders_permission(self):
        user = self.create_user(email='order@test.com', can_access_orders=True)
        admin = self.create_admin()
        self.create_board(name='B1', created_by=admin)
        resp = self.api_get('/api/boards/', user=user)
        self.assertEqual(resp.status_code, 200)

    def test_user_in_board_group_can_access(self):
        user = self.create_user(email='groupuser@test.com')
        group = self.create_tenant_group(name='Sales')
        user.tenant_groups.add(group)
        admin = self.create_admin()
        board = self.create_board(name='Restricted Board', created_by=admin)
        board.board_groups.add(group)
        resp = self.api_get('/api/boards/', user=user)
        self.assertEqual(resp.status_code, 200)
        board_ids = [b['id'] for b in self.get_results(resp)]
        self.assertIn(board.id, board_ids)

    def test_user_directly_attached_can_access(self):
        user = self.create_user(email='directuser@test.com')
        admin = self.create_admin()
        board = self.create_board(name='Direct Board', created_by=admin)
        board.board_users.add(user)
        resp = self.api_get('/api/boards/', user=user)
        self.assertEqual(resp.status_code, 200)
        board_ids = [b['id'] for b in self.get_results(resp)]
        self.assertIn(board.id, board_ids)

    def test_user_without_access_cannot_see_restricted_board(self):
        # User has view_boards to pass permission check, but can't see restricted boards
        user = self.create_user(email='noaccuser@test.com', can_view_boards=True)
        admin = self.create_admin()
        board = self.create_board(name='Restricted', created_by=admin)
        # Add a restriction (some user, not our test user)
        other_user = self.create_user(email='other@test.com')
        board.board_users.add(other_user)
        resp = self.api_get('/api/boards/', user=user)
        self.assertEqual(resp.status_code, 200)
        board_ids = [b['id'] for b in self.get_results(resp)]
        self.assertNotIn(board.id, board_ids)

    def test_unrestricted_board_visible_to_all(self):
        # User with view_boards perm can see unrestricted boards
        user = self.create_user(email='anyuser@test.com', can_view_boards=True)
        admin = self.create_admin()
        board = self.create_board(name='Open Board', created_by=admin)
        # No board_users or board_groups — unrestricted
        resp = self.api_get('/api/boards/', user=user)
        self.assertEqual(resp.status_code, 200)
        board_ids = [b['id'] for b in self.get_results(resp)]
        self.assertIn(board.id, board_ids)


class TestBoardActions(TicketTestCase):

    def test_set_default(self):
        admin = self.create_admin()
        board = self.create_board(created_by=admin)
        resp = self.api_post(f'/api/boards/{board.id}/set_default/', user=admin)
        self.assertEqual(resp.status_code, 200)
        board.refresh_from_db()
        self.assertTrue(board.is_default)

    def test_non_staff_cannot_set_default(self):
        admin = self.create_admin()
        board = self.create_board(created_by=admin)
        user = self.create_user(email='viewer@test.com', can_view_boards=True)
        resp = self.api_post(f'/api/boards/{board.id}/set_default/', user=user)
        self.assertEqual(resp.status_code, 403)

    def test_get_default_board(self):
        admin = self.create_admin()
        board = self.create_board(created_by=admin, is_default=True)
        resp = self.api_get('/api/boards/default/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], board.id)

    def test_get_default_returns_404_when_none(self):
        admin = self.create_admin()
        # No boards exist
        resp = self.api_get('/api/boards/default/', user=admin)
        self.assertEqual(resp.status_code, 404)

    def test_kanban_board_action(self):
        admin = self.create_admin()
        board, col1, col2, col3 = self.setup_board_with_columns(admin=admin)
        self.create_ticket(title='T1', column=col1, created_by=admin)
        resp = self.api_get(f'/api/boards/{board.id}/kanban_board/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('columns', resp.data)
        self.assertIn('tickets_by_column', resp.data)


class TestBoardQueryset(TicketTestCase):

    def test_staff_sees_all_boards(self):
        admin = self.create_admin()
        self.create_board(name='B1', created_by=admin)
        self.create_board(name='B2', created_by=admin)
        resp = self.api_get('/api/boards/', user=admin)
        self.assertEqual(len(self.get_results(resp)), 2)

    def test_regular_user_sees_accessible_boards_only(self):
        admin = self.create_admin()
        user = self.create_user(email='reguser@test.com', can_view_boards=True)
        open_board = self.create_board(name='Open', created_by=admin)
        restricted = self.create_board(name='Restricted', created_by=admin)
        other = self.create_user(email='other2@test.com')
        restricted.board_users.add(other)
        resp = self.api_get('/api/boards/', user=user)
        board_ids = [b['id'] for b in self.get_results(resp)]
        self.assertIn(open_board.id, board_ids)
        self.assertNotIn(restricted.id, board_ids)


class TestKanbanBoardLite(TicketTestCase):
    """kanban_board?lite=1 returns compact ticket cards with the same top-level keys."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='lite-admin@test.com')
        self.board, self.col1, self.col2, self.col3 = self.setup_board_with_columns(admin=self.admin)
        self.helper = self.create_user(email='lite-helper@test.com', first_name='Helen', last_name='Per')
        self.tag = self.create_tag(name='Urgent', color='#FF0000', created_by=self.admin)
        self.ticket = self.create_ticket(
            title='Card', column=self.col1, created_by=self.admin,
            description='<p>long body</p>', assigned_to=self.admin,
        )
        self.ticket.assigned_users.add(self.helper)
        self.ticket.tags.add(self.tag)
        self.create_comment(self.ticket, self.admin, 'c1')
        self.create_comment(self.ticket, self.helper, 'c2')
        # A plain path keeps the FileField off the (S3) storage backend
        self.create_attachment(self.ticket, uploaded_by=self.admin, file='attachments/test.txt')

    def test_lite_cards_have_no_description_and_carry_counts(self):
        resp = self.api_get(f'/api/boards/{self.board.id}/kanban_board/?lite=1', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.data.keys()), {'board', 'columns', 'tickets_by_column'})
        cards = resp.data['tickets_by_column'][self.col1.id]
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertNotIn('description', card)
        self.assertNotIn('created_by', card)
        self.assertEqual(card['id'], self.ticket.id)
        self.assertEqual(card['column_id'], self.col1.id)
        self.assertEqual(card['comments_count'], 2)
        self.assertEqual(card['attachments_count'], 1)
        self.assertEqual(card['is_closed'], False)
        self.assertEqual([t['name'] for t in card['tags']], ['Urgent'])
        assignee_ids = [a['id'] for a in card['assignees']]
        self.assertEqual(assignee_ids, [self.admin.id, self.helper.id])
        self.assertEqual(card['assignees'][1]['name'], 'Helen Per')
        self.assertEqual(card['assignees'][1]['email'], 'lite-helper@test.com')
        for key in ('position_in_column', 'priority', 'is_order', 'price', 'currency',
                    'is_paid', 'payment_due_date', 'created_at', 'updated_at'):
            self.assertIn(key, card)

    def test_lite_accepts_true(self):
        resp = self.api_get(f'/api/boards/{self.board.id}/kanban_board/?lite=true', user=self.admin)
        self.assertNotIn('description', resp.data['tickets_by_column'][self.col1.id][0])

    def test_default_shape_unchanged(self):
        resp = self.api_get(f'/api/boards/{self.board.id}/kanban_board/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        ticket = resp.data['tickets_by_column'][self.col1.id][0]
        self.assertIn('description', ticket)
        self.assertIn('column', ticket)
        self.assertIn('created_by', ticket)
        self.assertEqual(ticket['comments_count'], 2)
        self.assertNotIn('attachments_count', ticket)

    def test_lite_query_count_is_bounded(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        # More tickets, tags and assignees: query count must not grow with them
        for i in range(6):
            t = self.create_ticket(title=f'T{i}', column=self.col2, created_by=self.admin)
            t.assigned_users.add(self.helper)
            t.tags.add(self.tag)
            self.create_comment(t, self.helper, 'x')
        with CaptureQueriesContext(connection) as ctx:
            resp = self.api_get(f'/api/boards/{self.board.id}/kanban_board/?lite=1', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['tickets_by_column'][self.col2.id]), 6)
        # tenant + board (+3 access prefetches) + columns + tickets + 2 prefetches
        # + the column tickets_count aggregate; django-tenant-schemas pairs each
        # with a SET search_path, which is noise here.
        real = [q['sql'] for q in ctx.captured_queries if not q['sql'].startswith('SET search_path')]
        self.assertLessEqual(len(real), 12, [sql[:80] for sql in real])
