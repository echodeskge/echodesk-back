"""Tests for TicketCommentViewSet."""
from tickets.tests.conftest import TicketTestCase


class TestCommentCRUD(TicketTestCase):

    def test_create_comment(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T1', column=col1, created_by=admin)
        resp = self.api_post('/api/comments/', {
            'ticket': ticket.id, 'comment': 'Hello world'
        }, user=admin)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['comment'], 'Hello world')

    def test_list_comments(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T1', column=col1, created_by=admin)
        self.create_comment(ticket, admin, 'C1')
        self.create_comment(ticket, admin, 'C2')
        resp = self.api_get('/api/comments/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 2)

    def test_update_comment(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T1', column=col1, created_by=admin)
        comment = self.create_comment(ticket, admin, 'Old')
        resp = self.api_patch(f'/api/comments/{comment.id}/', {
            'comment': 'Updated'
        }, user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['comment'], 'Updated')

    def test_delete_comment(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T1', column=col1, created_by=admin)
        comment = self.create_comment(ticket, admin, 'ToDelete')
        resp = self.api_delete(f'/api/comments/{comment.id}/', user=admin)
        self.assertEqual(resp.status_code, 204)


class TestCommentPermissions(TicketTestCase):

    def test_staff_sees_all_comments(self):
        admin = self.create_admin()
        user = self.create_user(email='commenter@test.com')
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T1', column=col1, created_by=user)
        self.create_comment(ticket, user, 'C1')
        resp = self.api_get('/api/comments/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 1)

    def test_non_staff_sees_own_ticket_comments(self):
        admin = self.create_admin()
        user = self.create_user(email='u1@test.com')
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T1', column=col1, created_by=user)
        self.create_comment(ticket, admin, 'Staff comment')
        resp = self.api_get('/api/comments/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 1)

    def test_non_staff_cannot_see_other_ticket_comments(self):
        admin = self.create_admin()
        user1 = self.create_user(email='u1c@test.com')
        user2 = self.create_user(email='u2c@test.com')
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        board.board_users.add(admin)  # restricted: open-board tickets are visible to all
        ticket = self.create_ticket(title='Private', column=col1, created_by=user1)
        self.create_comment(ticket, user1, 'Secret')
        resp = self.api_get('/api/comments/', user=user2)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 0)

    def test_group_member_sees_comments_on_group_ticket(self):
        admin = self.create_admin()
        user = self.create_user(email='groupmember@test.com')
        group = self.create_tenant_group(name='Support')
        user.tenant_groups.add(group)
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='GroupTicket', column=col1, created_by=admin)
        ticket.assigned_groups.add(group)
        self.create_comment(ticket, admin, 'Group visible comment')
        resp = self.api_get('/api/comments/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 1)

    def test_unauthenticated_denied(self):
        resp = self.api_get('/api/comments/')
        self.assertIn(resp.status_code, [401, 403])
