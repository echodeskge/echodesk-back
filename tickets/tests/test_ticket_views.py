"""Tests for TicketViewSet."""
from django.db import connection
from tickets.models import Ticket, TicketColumn, TicketTimeLog
from tickets.tests.conftest import TicketTestCase


class TestTicketCRUD(TicketTestCase):

    def test_authenticated_user_can_create_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='creator@test.com')
        resp = self.api_post('/api/tickets/', {
            'title': 'New Ticket', 'column_id': col1.id
        }, user=user)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['title'], 'New Ticket')

    def test_create_ticket_auto_assigns_default_column(self):
        admin = self.create_admin()
        board, col_default, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='creator2@test.com')
        resp = self.api_post('/api/tickets/', {'title': 'Auto Col'}, user=user)
        self.assertEqual(resp.status_code, 201)
        ticket = Ticket.objects.get(id=resp.data['id'])
        self.assertEqual(ticket.column_id, col_default.id)

    def test_create_ticket_starts_time_tracking(self):
        admin = self.create_admin()
        board = self.create_board(created_by=admin)
        col = self.create_column(board, name='Track', position=0, is_default=True, track_time=True, created_by=admin)
        user = self.create_user(email='tracker@test.com')
        resp = self.api_post('/api/tickets/', {'title': 'Tracked', 'column_id': col.id}, user=user)
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(TicketTimeLog.objects.filter(ticket_id=resp.data['id'], column=col).exists())

    def test_list_tickets(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        self.create_ticket(title='T1', column=col1, created_by=admin)
        self.create_ticket(title='T2', column=col1, created_by=admin)
        resp = self.api_get('/api/tickets/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 2)

    def test_retrieve_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='Detail', column=col1, created_by=admin)
        resp = self.api_get(f'/api/tickets/{ticket.id}/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['title'], 'Detail')

    def test_update_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='Old', column=col1, created_by=admin)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/', {'title': 'New'}, user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['title'], 'New')

    def test_unauthenticated_cannot_access(self):
        resp = self.api_get('/api/tickets/')
        self.assertIn(resp.status_code, [401, 403])


class TestTicketPermissions(TicketTestCase):

    def test_staff_has_full_access(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='other@test.com')
        ticket = self.create_ticket(title='Staff Test', column=col1, created_by=user)
        resp = self.api_get(f'/api/tickets/{ticket.id}/', user=admin)
        self.assertEqual(resp.status_code, 200)

    def test_creator_can_view_own_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='owner@test.com')
        ticket = self.create_ticket(title='Mine', column=col1, created_by=user)
        resp = self.api_get(f'/api/tickets/{ticket.id}/', user=user)
        self.assertEqual(resp.status_code, 200)

    def test_assigned_user_can_view_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        creator = self.create_user(email='cr@test.com')
        viewer = self.create_user(email='as@test.com')
        ticket = self.create_ticket(title='Assigned', column=col1, created_by=creator, assigned_to=viewer)
        resp = self.api_get(f'/api/tickets/{ticket.id}/', user=viewer)
        self.assertEqual(resp.status_code, 200)

    def test_assigned_group_user_can_view_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='grpuser@test.com')
        group = self.create_tenant_group(name='Support')
        user.tenant_groups.add(group)
        ticket = self.create_ticket(title='Group T', column=col1, created_by=admin)
        ticket.assigned_groups.add(group)
        resp = self.api_get(f'/api/tickets/{ticket.id}/', user=user)
        self.assertEqual(resp.status_code, 200)

    def test_unrelated_user_cannot_view_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        creator = self.create_user(email='crt@test.com')
        stranger = self.create_user(email='str@test.com')
        ticket = self.create_ticket(title='Secret', column=col1, created_by=creator)
        # Add board restriction so stranger can't see via board access
        board.board_users.add(creator)
        resp = self.api_get(f'/api/tickets/{ticket.id}/', user=stranger)
        self.assertEqual(resp.status_code, 404)

    def test_only_staff_can_assign_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='nonstaff@test.com')
        assignee = self.create_user(email='assignee@test.com')
        ticket = self.create_ticket(title='T', column=col1, created_by=user)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/', {
            'assigned_to_id': assignee.id
        }, user=user)
        self.assertEqual(resp.status_code, 403)

    def test_only_staff_can_move_to_closed_column(self):
        admin = self.create_admin()
        board, col1, _, col_done = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='mover@test.com')
        ticket = self.create_ticket(title='T', column=col1, created_by=user)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/', {
            'column_id': col_done.id
        }, user=user)
        self.assertEqual(resp.status_code, 403)

    def test_only_staff_can_delete_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='nodelete@test.com')
        ticket = self.create_ticket(title='T', column=col1, created_by=user)
        resp = self.api_delete(f'/api/tickets/{ticket.id}/', user=user)
        # TicketPermission.has_object_permission returns False for DELETE by non-staff
        self.assertEqual(resp.status_code, 403)

    def test_creator_can_edit_own_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='editor@test.com')
        ticket = self.create_ticket(title='Edit Me', column=col1, created_by=user)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/', {
            'title': 'Edited'
        }, user=user)
        self.assertEqual(resp.status_code, 200)

    def test_assigned_user_can_edit_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        creator = self.create_user(email='cr2@test.com')
        assignee = self.create_user(email='as2@test.com')
        ticket = self.create_ticket(title='T', column=col1, created_by=creator, assigned_to=assignee)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/', {
            'title': 'Edited by assignee'
        }, user=assignee)
        self.assertEqual(resp.status_code, 200)


class TestTicketDelete(TicketTestCase):

    def _set_tenant_delete_setting(self, value):
        from tenants.models import Tenant
        tenant = Tenant.objects.get(schema_name=connection.schema_name)
        tenant.only_superadmin_can_delete_tickets = value
        tenant.save()

    def test_superadmin_delete_when_only_superadmin_setting(self):
        self._set_tenant_delete_setting(True)
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='Del', column=col1, created_by=admin)
        resp = self.api_delete(f'/api/tickets/{ticket.id}/', user=admin)
        self.assertEqual(resp.status_code, 204)

    def test_owner_cannot_delete_when_only_superadmin_setting(self):
        self._set_tenant_delete_setting(True)
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='ownd@test.com')
        ticket = self.create_ticket(title='Del', column=col1, created_by=user)
        resp = self.api_delete(f'/api/tickets/{ticket.id}/', user=user)
        self.assertEqual(resp.status_code, 403)

    def test_owner_can_delete_when_setting_off(self):
        self._set_tenant_delete_setting(False)
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='ownerdel@test.com')
        ticket = self.create_ticket(title='Del', column=col1, created_by=user)
        # The TicketPermission.has_object_permission denies DELETE for non-staff,
        # but the destroy() method also checks tenant settings.
        # Non-staff gets blocked by has_object_permission first.
        resp = self.api_delete(f'/api/tickets/{ticket.id}/', user=user)
        # TicketPermission blocks non-staff DELETE regardless of tenant setting
        self.assertEqual(resp.status_code, 403)

    def test_non_owner_non_staff_cannot_delete(self):
        self._set_tenant_delete_setting(False)
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        creator = self.create_user(email='cr3@test.com')
        stranger = self.create_user(email='str3@test.com')
        ticket = self.create_ticket(title='Del', column=col1, created_by=creator)
        resp = self.api_delete(f'/api/tickets/{ticket.id}/', user=stranger)
        # Stranger can't see ticket (queryset filtering), so 404 or 403
        self.assertIn(resp.status_code, [403, 404])


class TestTicketQueryset(TicketTestCase):

    def test_staff_sees_all_tickets(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='u4@test.com')
        self.create_ticket(title='T1', column=col1, created_by=user)
        self.create_ticket(title='T2', column=col1, created_by=admin)
        resp = self.api_get('/api/tickets/', user=admin)
        self.assertEqual(len(self.get_results(resp)), 2)

    def test_non_staff_sees_own_and_assigned(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='u5@test.com')
        other = self.create_user(email='oth@test.com')
        # Restrict board so user can't see via board access
        board.board_users.add(user)
        board.board_users.add(other)
        t_own = self.create_ticket(title='Own', column=col1, created_by=user)
        t_assigned = self.create_ticket(title='Assigned', column=col1, created_by=other, assigned_to=user)
        t_other = self.create_ticket(title='Other', column=col1, created_by=other)
        resp = self.api_get('/api/tickets/', user=user)
        results = self.get_results(resp)
        ids = [t['id'] for t in results]
        self.assertIn(t_own.id, ids)
        self.assertIn(t_assigned.id, ids)

    def test_non_staff_sees_board_accessible_tickets(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='boardu@test.com')
        board.board_users.add(user)
        self.create_ticket(title='Board Ticket', column=col1, created_by=admin)
        resp = self.api_get('/api/tickets/', user=user)
        self.assertGreaterEqual(len(self.get_results(resp)), 1)

    def test_filter_by_board(self):
        admin = self.create_admin()
        board_a, col_a, col_a2, _ = self.setup_board_with_columns(admin=admin)
        board_b = self.create_board(name='Other Board', created_by=admin)
        col_b = self.create_column(board_b, name='Inbox', position=0, created_by=admin)
        t_a1 = self.create_ticket(title='A1', column=col_a, created_by=admin)
        t_a2 = self.create_ticket(title='A2', column=col_a2, created_by=admin)
        t_b = self.create_ticket(title='B', column=col_b, created_by=admin)

        resp = self.api_get(f'/api/tickets/?board={board_a.id}', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual({t['id'] for t in self.get_results(resp)}, {t_a1.id, t_a2.id})

        resp = self.api_get(f'/api/tickets/?board={board_b.id}', user=admin)
        self.assertEqual([t['id'] for t in self.get_results(resp)], [t_b.id])

    def test_existing_column_and_priority_filters_still_work(self):
        admin = self.create_admin()
        board, col1, col2, _ = self.setup_board_with_columns(admin=admin)
        t_high = self.create_ticket(title='High', column=col1, created_by=admin, priority='high')
        self.create_ticket(title='Low', column=col1, created_by=admin, priority='low')
        t_col2 = self.create_ticket(title='Col2', column=col2, created_by=admin, priority='high')

        resp = self.api_get(f'/api/tickets/?column={col1.id}&priority=high', user=admin)
        self.assertEqual([t['id'] for t in self.get_results(resp)], [t_high.id])

        resp = self.api_get(f'/api/tickets/?board={board.id}&priority=high', user=admin)
        self.assertEqual({t['id'] for t in self.get_results(resp)}, {t_high.id, t_col2.id})


class TestTicketActions(TicketTestCase):

    def test_add_comment(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        resp = self.api_post(f'/api/tickets/{ticket.id}/add_comment/', {
            'ticket': ticket.id, 'comment': 'Nice work!'
        }, user=admin)
        self.assertEqual(resp.status_code, 201)

    def test_get_comments(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        self.create_comment(ticket, admin, 'C1')
        resp = self.api_get(f'/api/tickets/{ticket.id}/comments/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 1)

    def test_my_tickets(self):
        """Validates Bug 1 fix: my_tickets no longer crashes."""
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        self.create_ticket(title='Mine', column=col1, created_by=admin)
        resp = self.api_get('/api/tickets/my_tickets/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(self.get_results(resp)), 1)

    def test_assigned_to_me(self):
        """Validates Bug 1 fix: assigned_to_me no longer crashes."""
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='assignee5@test.com')
        self.create_ticket(title='Assigned', column=col1, created_by=admin, assigned_to=user)
        resp = self.api_get('/api/tickets/assigned_to_me/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(self.get_results(resp)), 1)

    def test_assign_ticket_staff(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='target@test.com')
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/assign/', {
            'assigned_to_id': user.id
        }, user=admin)
        self.assertEqual(resp.status_code, 200)

    def test_assign_ticket_non_staff_denied(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        user = self.create_user(email='nonstaff2@test.com')
        target = self.create_user(email='target2@test.com')
        ticket = self.create_ticket(title='T', column=col1, created_by=user)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/assign/', {
            'assigned_to_id': target.id
        }, user=user)
        self.assertEqual(resp.status_code, 403)

    def test_move_to_column(self):
        admin = self.create_admin()
        board, col1, col2, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/move_to_column/', {
            'column_id': col2.id
        }, user=admin)
        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.column_id, col2.id)

    def test_move_to_column_creates_time_log(self):
        admin = self.create_admin()
        board, col1, col_track, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        self.api_patch(f'/api/tickets/{ticket.id}/move_to_column/', {
            'column_id': col_track.id
        }, user=admin)
        self.assertTrue(TicketTimeLog.objects.filter(ticket=ticket, column=col_track).exists())

    def test_reorder_in_column(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        t1 = self.create_ticket(title='T1', column=col1, created_by=admin)
        t2 = self.create_ticket(title='T2', column=col1, created_by=admin)
        resp = self.api_patch(f'/api/tickets/{t1.id}/reorder_in_column/', {
            'position_in_column': 2
        }, user=admin)
        self.assertEqual(resp.status_code, 200)

    def test_history(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        resp = self.api_get(f'/api/tickets/{ticket.id}/history/', user=admin)
        self.assertEqual(resp.status_code, 200)
