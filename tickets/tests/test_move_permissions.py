"""BE-06 / E4: move rule for move_to_column, reorder_in_column and
``column_id`` writes. Denied moves are 403 (mobile relies on it, C7)."""
from tickets.models import Ticket
from tickets.tests.conftest import TicketTestCase


class MoveBase(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='mv-admin@test.com')
        self.agent = self.create_user(email='mv-agent@test.com')
        self.other = self.create_user(email='mv-other@test.com')

        # Board A: agent is a member. Board B: agent is a member too.
        # Board X: agent has no access.
        self.board_a = self.create_board('A', created_by=self.admin, users=[self.agent, self.other])
        self.a1 = self.create_column(self.board_a, 'To Do', 0)
        self.a2 = self.create_column(self.board_a, 'Done', 1, is_closed_status=True)
        self.board_b = self.create_board('B', created_by=self.admin, users=[self.agent, self.other])
        self.b1 = self.create_column(self.board_b, 'To Do', 0)
        self.board_x = self.create_board('X', created_by=self.admin, users=[self.other])
        self.x1 = self.create_column(self.board_x, 'To Do', 0)

        # Visible to agent (board member) but not editable (created by other)
        self.ticket = self.create_ticket('T', column=self.a1, created_by=self.other)
        self.invisible = self.create_ticket('Secret', column=self.x1, created_by=self.other)

    def move(self, ticket, column, user=None, **extra):
        body = {'column_id': column.id, **extra}
        return self.api_patch(f'/api/tickets/{ticket.id}/move_to_column/', body, user=user or self.agent)


class TestMoveToColumn(MoveBase):

    def test_invisible_ticket_is_403(self):
        resp = self.move(self.invisible, self.a1)
        self.assertEqual(resp.status_code, 403)
        self.invisible.refresh_from_db()
        self.assertEqual(self.invisible.column_id, self.x1.id)

    def test_missing_ticket_is_404(self):
        resp = self.api_patch('/api/tickets/999999/move_to_column/', {'column_id': self.a1.id}, user=self.agent)
        self.assertEqual(resp.status_code, 404)

    def test_target_on_inaccessible_board_is_403(self):
        mine = self.create_ticket('Mine', column=self.a1, created_by=self.agent)
        resp = self.move(mine, self.x1)
        self.assertEqual(resp.status_code, 403)
        mine.refresh_from_db()
        self.assertEqual(mine.column_id, self.a1.id)

    def test_cross_board_without_edit_rights_is_403(self):
        resp = self.move(self.ticket, self.b1)
        self.assertEqual(resp.status_code, 403)

    def test_cross_board_with_edit_rights_is_200(self):
        mine = self.create_ticket('Mine', column=self.a1, created_by=self.agent)
        resp = self.move(mine, self.b1)
        self.assertEqual(resp.status_code, 200, resp.data)
        mine.refresh_from_db()
        self.assertEqual(mine.column_id, self.b1.id)

    def test_same_board_move_by_viewer_is_200(self):
        other_col = self.create_column(self.board_a, 'Doing', 2)
        resp = self.move(self.ticket, other_col, position_in_column=0)
        self.assertEqual(resp.status_code, 200)

    def test_move_into_closed_column_by_viewer_is_200(self):
        """E4 status quo, confirmed by the user (§13 Q1): anyone who can see
        the ticket may close it by drag."""
        resp = self.move(self.ticket, self.a2)
        self.assertEqual(resp.status_code, 200)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.column_id, self.a2.id)

    def test_staff_moves_anywhere(self):
        resp = self.move(self.invisible, self.b1, user=self.admin)
        self.assertEqual(resp.status_code, 200)

    def test_unknown_column_still_400(self):
        resp = self.api_patch(f'/api/tickets/{self.ticket.id}/move_to_column/',
                              {'column_id': 999999}, user=self.agent)
        self.assertEqual(resp.status_code, 400)

    def test_mobile_contract_denied_move_is_403_not_404_or_400(self):
        for ticket, column in ((self.invisible, self.a1), (self.ticket, self.x1), (self.ticket, self.b1)):
            with self.subTest(ticket=ticket.title, column=column.board.name):
                self.assertEqual(self.move(ticket, column).status_code, 403)


class TestReorderInColumn(MoveBase):

    def reorder(self, ticket, position=0, user=None):
        return self.api_patch(f'/api/tickets/{ticket.id}/reorder_in_column/',
                              {'position_in_column': position}, user=user or self.agent)

    def test_visible_ticket_reorders(self):
        self.create_ticket('Second', column=self.a1, created_by=self.other)
        self.assertEqual(self.reorder(self.ticket, 1).status_code, 200)

    def test_invisible_ticket_is_403(self):
        self.assertEqual(self.reorder(self.invisible).status_code, 403)

    def test_assigned_ticket_on_inaccessible_board_reorders(self):
        """Visible through assignment on a board the user is not a member of:
        moves within that board keep working (pre-B2 behaviour; E2's only
        narrowing is inactive groups)."""
        self.create_assignment(self.invisible, self.agent)
        resp = self.api_get(f'/api/tickets/{self.invisible.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.reorder(self.invisible).status_code, 200)


class TestAssigneeOutsideBoard(MoveBase):
    """An assignee who is not a member of the ticket's (restricted) board."""

    def setUp(self):
        super().setUp()
        self.x2 = self.create_column(self.board_x, 'Done', 1, is_closed_status=True)
        self.x3 = self.create_column(self.board_x, 'Doing', 2)
        self.create_assignment(self.invisible, self.agent)

    def test_move_within_its_board_is_200(self):
        for column in (self.x3, self.x2):
            with self.subTest(column=column.name):
                resp = self.move(self.invisible, column)
                self.assertEqual(resp.status_code, 200, resp.data)
                self.invisible.refresh_from_db()
                self.assertEqual(self.invisible.column_id, column.id)

    def test_patch_column_within_its_board_is_200(self):
        resp = self.api_patch(f'/api/tickets/{self.invisible.id}/', {'column_id': self.x3.id}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_move_off_its_board_still_needs_target_access(self):
        # Board A is accessible and the agent can edit (assignee): allowed
        self.assertEqual(self.move(self.invisible, self.a1).status_code, 200)
        # Back onto board X, which the agent cannot access: refused
        self.assertEqual(self.move(self.invisible, self.x1).status_code, 403)


class TestColumnIdWrites(MoveBase):

    def test_patch_column_on_inaccessible_board_is_403(self):
        mine = self.create_ticket('Mine', column=self.a1, created_by=self.agent)
        resp = self.api_patch(f'/api/tickets/{mine.id}/', {'column_id': self.x1.id}, user=self.agent)
        self.assertEqual(resp.status_code, 403)
        mine.refresh_from_db()
        self.assertEqual(mine.column_id, self.a1.id)

    def test_patch_cross_board_without_edit_rights_is_403(self):
        resp = self.api_patch(f'/api/tickets/{self.ticket.id}/', {'column_id': self.b1.id}, user=self.agent)
        self.assertEqual(resp.status_code, 403)

    def test_patch_cross_board_with_edit_rights_is_200(self):
        mine = self.create_ticket('Mine', column=self.a1, created_by=self.agent)
        resp = self.api_patch(f'/api/tickets/{mine.id}/', {'column_id': self.b1.id}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_patch_unchanged_column_on_inaccessible_board_is_allowed(self):
        mine = self.create_ticket('Mine', column=self.x1, created_by=self.agent)
        resp = self.api_patch(f'/api/tickets/{mine.id}/',
                              {'column_id': self.x1.id, 'title': 'Renamed'}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_create_on_inaccessible_board_is_403(self):
        before = Ticket.objects.count()
        resp = self.api_post('/api/tickets/', {'title': 'New', 'column_id': self.x1.id}, user=self.agent)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Ticket.objects.count(), before)

    def test_create_order_on_inaccessible_board_is_403(self):
        resp = self.api_post('/api/tickets/', {
            'title': 'Order', 'column_id': self.x1.id, 'is_order': True,
        }, user=self.agent)
        self.assertEqual(resp.status_code, 403)

    def test_create_on_accessible_board_is_201(self):
        resp = self.api_post('/api/tickets/', {'title': 'New', 'column_id': self.a1.id}, user=self.agent)
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_create_unknown_column_is_400(self):
        resp = self.api_post('/api/tickets/', {'title': 'New', 'column_id': 999999}, user=self.agent)
        self.assertEqual(resp.status_code, 400)

    def test_staff_create_anywhere(self):
        resp = self.api_post('/api/tickets/', {'title': 'New', 'column_id': self.x1.id}, user=self.admin)
        self.assertEqual(resp.status_code, 201)


class TestNotVisibleIs404ForOtherActions(MoveBase):

    def test_assign_history_activity(self):
        self.assertEqual(self.api_patch(f'/api/tickets/{self.invisible.id}/assign/',
                                        {'assigned_to_id': self.agent.id}, user=self.admin).status_code, 200)
        # assign is staff-only; a non-staff caller is refused before lookup
        self.assertEqual(self.api_patch(f'/api/tickets/{self.invisible.id}/assign/',
                                        {'assigned_to_id': None}, user=self.agent).status_code, 403)
        hidden = self.create_ticket('Hidden 2', column=self.x1, created_by=self.other)
        for action in ('history', 'activity'):
            with self.subTest(action=action):
                resp = self.api_get(f'/api/tickets/{hidden.id}/{action}/', user=self.agent)
                self.assertEqual(resp.status_code, 404)
