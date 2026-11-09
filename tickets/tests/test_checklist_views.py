"""Tests for ChecklistItemViewSet."""
from tickets.models import ChecklistItem
from tickets.tests.conftest import TicketTestCase


class TestChecklistCRUD(TicketTestCase):

    def test_create_checklist_item(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        resp = self.api_post('/api/checklist-items/', {
            'ticket': ticket.id, 'text': 'Do something'
        }, user=admin)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['text'], 'Do something')

    def test_list_checklist_items(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        self.create_checklist_item(ticket, 'Item 1', created_by=admin)
        self.create_checklist_item(ticket, 'Item 2', created_by=admin)
        resp = self.api_get('/api/checklist-items/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 2)

    def test_filter_by_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        t1 = self.create_ticket(title='T1', column=col1, created_by=admin)
        t2 = self.create_ticket(title='T2', column=col1, created_by=admin)
        self.create_checklist_item(t1, 'A', created_by=admin)
        self.create_checklist_item(t2, 'B', created_by=admin)
        resp = self.api_get(f'/api/checklist-items/?ticket={t1.id}', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 1)

    def test_update_checklist_item(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        item = self.create_checklist_item(ticket, 'Old', created_by=admin)
        resp = self.api_patch(f'/api/checklist-items/{item.id}/', {'text': 'New'}, user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['text'], 'New')

    def test_delete_checklist_item(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        item = self.create_checklist_item(ticket, 'Del', created_by=admin)
        resp = self.api_delete(f'/api/checklist-items/{item.id}/', user=admin)
        self.assertEqual(resp.status_code, 204)


class TestChecklistActions(TicketTestCase):

    def test_toggle_check(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        item = self.create_checklist_item(ticket, 'Toggle', created_by=admin)
        self.assertFalse(item.is_checked)
        resp = self.api_patch(f'/api/checklist-items/{item.id}/toggle_check/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['is_checked'])

    def test_reorder(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        i1 = self.create_checklist_item(ticket, 'First', created_by=admin)
        i2 = self.create_checklist_item(ticket, 'Second', created_by=admin)
        resp = self.api_patch(f'/api/checklist-items/{i1.id}/reorder/', {
            'position': 2
        }, user=admin)
        self.assertEqual(resp.status_code, 200)

    def test_reorder_missing_position(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        item = self.create_checklist_item(ticket, 'X', created_by=admin)
        resp = self.api_patch(f'/api/checklist-items/{item.id}/reorder/', {}, user=admin)
        self.assertEqual(resp.status_code, 400)

    def test_reorder_invalid_position(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        item = self.create_checklist_item(ticket, 'X', created_by=admin)
        resp = self.api_patch(f'/api/checklist-items/{item.id}/reorder/', {
            'position': 'abc'
        }, user=admin)
        self.assertEqual(resp.status_code, 400)

    def test_reorder_negative_position_rejected(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        item = self.create_checklist_item(ticket, 'X', created_by=admin)
        resp = self.api_patch(f'/api/checklist-items/{item.id}/reorder/', {
            'position': -1
        }, user=admin)
        self.assertEqual(resp.status_code, 400)


class TestChecklistPermissions(TicketTestCase):

    def test_staff_sees_all_checklist_items(self):
        admin = self.create_admin()
        user = self.create_user(email='chkuser@test.com')
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=user)
        self.create_checklist_item(ticket, 'A', created_by=user)
        resp = self.api_get('/api/checklist-items/', user=admin)
        self.assertEqual(len(self.get_results(resp)), 1)

    def test_non_staff_sees_own_ticket_items(self):
        admin = self.create_admin()
        user1 = self.create_user(email='chku1@test.com')
        user2 = self.create_user(email='chku2@test.com')
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        t1 = self.create_ticket(title='T1', column=col1, created_by=user1)
        t2 = self.create_ticket(title='T2', column=col1, created_by=user2)
        self.create_checklist_item(t1, 'Mine', created_by=user1)
        self.create_checklist_item(t2, 'Others', created_by=user2)
        resp = self.api_get('/api/checklist-items/', user=user1)
        # user1 only sees items on tickets they created
        texts = [i['text'] for i in self.get_results(resp)]
        self.assertIn('Mine', texts)


class TestChecklistMultiAssigneeDistinct(TicketTestCase):
    """Bug: a non-staff manager reaching an item through several relations got
    MultipleObjectsReturned on /api/checklist-items/<id>/ (missing distinct())."""

    def _multi_assignee_item(self):
        admin = self.create_admin()
        manager = self.create_manager(email='chk-mgr@test.com')
        agent = self.create_user(email='chk-agent@test.com')
        other = self.create_user(email='chk-other@test.com')
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='Shared', column=col1, created_by=manager, assigned_to=manager)
        for u in (manager, agent, other):
            self.create_assignment(ticket, u, assigned_by=manager)
        item = self.create_checklist_item(ticket, 'Check me', created_by=manager)
        return manager, item

    def test_detail_for_manager_with_multiple_relations(self):
        manager, item = self._multi_assignee_item()
        resp = self.api_get(f'/api/checklist-items/{item.id}/', user=manager)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], item.id)

    def test_toggle_for_manager_with_multiple_relations(self):
        manager, item = self._multi_assignee_item()
        resp = self.api_patch(f'/api/checklist-items/{item.id}/toggle_check/', user=manager)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['is_checked'])

    def test_list_has_no_duplicates(self):
        manager, item = self._multi_assignee_item()
        resp = self.api_get('/api/checklist-items/', user=manager)
        ids = [i['id'] for i in self.get_results(resp)]
        self.assertEqual(ids, [item.id])

    def test_unrelated_user_still_denied(self):
        manager, item = self._multi_assignee_item()
        # Restricted board: tickets on an open board are visible to everyone.
        item.ticket.column.board.board_users.add(manager)
        stranger = self.create_user(email='chk-stranger@test.com')
        resp = self.api_get(f'/api/checklist-items/{item.id}/', user=stranger)
        self.assertEqual(resp.status_code, 404)


class TestChecklistToggleAudit(TicketTestCase):
    """BE-02 / A11: toggle_check records who checked an item and when."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='chk-audit-admin@test.com')
        self.agent = self.create_user(email='chk-audit-agent@test.com', first_name='Nino', last_name='B')
        _, col1, _, _ = self.setup_board_with_columns(admin=self.admin)
        self.ticket = self.create_ticket(title='T', column=col1, created_by=self.agent)
        self.item = self.create_checklist_item(self.ticket, 'Audit', created_by=self.agent)

    def toggle(self, user):
        return self.api_patch(f'/api/checklist-items/{self.item.id}/toggle_check/', user=user)

    def test_check_sets_checked_by_and_at(self):
        resp = self.toggle(self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['is_checked'])
        self.assertEqual(resp.data['checked_by'], {'id': self.agent.id, 'name': 'Nino B'})
        self.assertIsNotNone(resp.data['checked_at'])
        self.item.refresh_from_db()
        self.assertEqual(self.item.checked_by_id, self.agent.id)
        self.assertIsNotNone(self.item.checked_at)

    def test_uncheck_clears_both(self):
        self.toggle(self.agent)
        resp = self.toggle(self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['is_checked'])
        self.assertIsNone(resp.data['checked_by'])
        self.assertIsNone(resp.data['checked_at'])
        self.item.refresh_from_db()
        self.assertIsNone(self.item.checked_by_id)
        self.assertIsNone(self.item.checked_at)

    def test_recheck_by_other_user_records_them(self):
        self.toggle(self.agent)
        self.toggle(self.agent)
        resp = self.toggle(self.admin)
        self.assertEqual(resp.data['checked_by']['id'], self.admin.id)

    def test_detail_and_list_expose_fields(self):
        self.toggle(self.agent)
        resp = self.api_get(f'/api/checklist-items/{self.item.id}/', user=self.agent)
        self.assertEqual(resp.data['checked_by']['id'], self.agent.id)
        self.assertIn('checked_at', resp.data)
        rows = self.get_results(self.api_get(f'/api/checklist-items/?ticket={self.ticket.id}', user=self.agent))
        self.assertEqual(rows[0]['checked_by']['id'], self.agent.id)
        resp = self.api_get(f'/api/tickets/{self.ticket.id}/', user=self.agent)
        self.assertEqual(resp.data['checklist_items'][0]['checked_by']['id'], self.agent.id)

    def test_checked_by_fields_are_read_only(self):
        resp = self.api_patch(f'/api/checklist-items/{self.item.id}/', {
            'checked_by': self.admin.id, 'checked_at': '2020-01-01T00:00:00Z', 'text': 'renamed',
        }, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.checked_by_id)
        self.assertIsNone(self.item.checked_at)
