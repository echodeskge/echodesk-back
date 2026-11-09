"""BE-06 / E2: one visibility definition (tickets.visibility) used everywhere.

Scenario (``VisibilityBase``):

- ``restricted`` board: only ``member`` (board_users) may see it;
- ``open_board`` ("E2E Open"): no users, groups or order users;
- ``inactive_board``: reachable only through an inactive group of ``agent``.

``agent`` is a plain non-staff user with no board permissions.
"""
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tickets.models import Ticket, TicketComment
from tickets.tests.conftest import TicketTestCase
from tickets.visibility import (
    accessible_boards, can_access_board, ticket_viewer_permissions,
    ticket_visibility_q, visible_tickets,
)


class VisibilityBase(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='vis-admin@test.com')
        self.agent = self.create_user(email='vis-agent@test.com')
        self.member = self.create_user(email='vis-member@test.com')

        self.restricted = self.create_board('Restricted', created_by=self.admin, users=[self.member])
        self.r_col = self.create_column(self.restricted, 'To Do', 0)
        self.r_col2 = self.create_column(self.restricted, 'Doing', 1)

        self.open_board = self.create_board('E2E Open', created_by=self.admin, open=True)
        self.o_col = self.create_column(self.open_board, 'To Do', 0)

        inactive_group = self.make_inactive_group('Old team')
        self.agent.tenant_groups.add(inactive_group)
        self.inactive_board = self.create_board('Old', created_by=self.admin, groups=[inactive_group])
        self.i_col = self.create_column(self.inactive_board, 'To Do', 0)

        self.hidden = self.create_ticket('Hidden', column=self.r_col, created_by=self.member)
        self.open_ticket = self.create_ticket('Open', column=self.o_col, created_by=self.member)
        self.inactive_ticket = self.create_ticket('Inactive', column=self.i_col, created_by=self.member)


class TestAccessibleBoards(VisibilityBase):

    def test_agent_sees_open_board_only(self):
        ids = set(accessible_boards(self.agent).values_list('id', flat=True))
        self.assertEqual(ids, {self.open_board.id})

    def test_member_and_staff(self):
        self.assertIn(self.restricted.id, accessible_boards(self.member).values_list('id', flat=True))
        self.assertEqual(accessible_boards(self.admin).count(), 3)

    def test_active_group_and_order_users_grant_access(self):
        group = self.create_tenant_group(name='Live team')
        self.agent.tenant_groups.add(group)
        self.restricted.board_groups.add(group)
        self.assertTrue(can_access_board(self.agent, self.restricted.id))
        other = self.create_user(email='vis-order@test.com')
        self.inactive_board.order_users.add(other)
        self.assertTrue(can_access_board(other, self.inactive_board.id))

    def test_can_access_board_rejects_garbage(self):
        self.assertFalse(can_access_board(self.agent, 'default'))
        self.assertFalse(can_access_board(self.agent, None))


class TestTicketVisibility(VisibilityBase):

    def _visible_ids(self, user):
        return set(visible_tickets(user).values_list('id', flat=True))

    def test_open_board_ticket_visible_to_agent(self):
        resp = self.api_get(f'/api/tickets/{self.open_ticket.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 200)

    def test_restricted_and_inactive_group_tickets_hidden(self):
        for ticket in (self.hidden, self.inactive_ticket):
            with self.subTest(ticket=ticket.title):
                resp = self.api_get(f'/api/tickets/{ticket.id}/', user=self.agent)
                self.assertEqual(resp.status_code, 404)

    def test_list_scope(self):
        resp = self.api_get('/api/tickets/', user=self.agent)
        self.assertEqual({t['id'] for t in self.get_results(resp)}, {self.open_ticket.id})

    def test_creator_assignee_assigned_user_and_group(self):
        created = self.create_ticket('Mine', column=self.r_col, created_by=self.agent)
        assigned = self.create_ticket('Assigned', column=self.r_col, created_by=self.member,
                                      assigned_to=self.agent)
        multi = self.create_ticket('Multi', column=self.r_col, created_by=self.member)
        self.create_assignment(multi, self.agent)
        grouped = self.create_ticket('Group', column=self.r_col, created_by=self.member)
        # ticket-group assignment counts for any group of the user, even inactive
        grouped.assigned_groups.add(self.agent.tenant_groups.first())
        visible = self._visible_ids(self.agent)
        self.assertTrue({created.id, assigned.id, multi.id, grouped.id} <= visible)
        self.assertNotIn(self.hidden.id, visible)
        for t in (created, assigned, multi, grouped):
            self.assertEqual(self.api_get(f'/api/tickets/{t.id}/', user=self.agent).status_code, 200)

    def test_staff_sees_everything(self):
        self.assertEqual(self._visible_ids(self.admin), set(Ticket.objects.values_list('id', flat=True)))

    def test_shim_matches_visible_tickets(self):
        self.create_assignment(self.hidden, self.agent)
        for user in (self.agent, self.member, self.admin):
            with self.subTest(user=user.email):
                via_shim = set(Ticket.objects.filter(ticket_visibility_q(user)).values_list('id', flat=True))
                self.assertEqual(via_shim, self._visible_ids(user))

    def test_shim_importable_from_views(self):
        from tickets import views
        self.assertIs(views.ticket_visibility_q, ticket_visibility_q)

    def test_list_has_constant_queries_and_no_distinct(self):
        extra_col = self.create_column(self.open_board, 'Doing', 1)

        def run():
            with CaptureQueriesContext(connection) as ctx:
                resp = self.api_get('/api/tickets/?page_size=100', user=self.agent)
            self.assertEqual(resp.status_code, 200)
            return ctx

        for i in range(2):
            self.create_ticket(f'small {i}', column=self.o_col, created_by=self.member)
        small = run()
        for i in range(27):
            t = self.create_ticket(f'large {i}', column=extra_col if i % 2 else self.o_col,
                                   created_by=self.member)
            self.create_assignment(t, self.member)
            self.create_assignment(t, self.agent)
        large = run()
        self.assertEqual(len(small.captured_queries), len(large.captured_queries))
        for q in large.captured_queries:
            self.assertNotIn('DISTINCT', q['sql'].upper())

    def test_assigned_to_me_has_no_duplicates(self):
        t = self.create_ticket('Dup', column=self.r_col, created_by=self.member, assigned_to=self.agent)
        self.create_assignment(t, self.agent)
        self.create_assignment(t, self.member)
        resp = self.api_get('/api/tickets/assigned_to_me/', user=self.agent)
        ids = [row['id'] for row in self.get_results(resp)]
        self.assertEqual(ids.count(t.id), 1)


class TestBoardsAndKanban(VisibilityBase):

    def test_open_board_only_agent_gets_boards_list_and_kanban(self):
        """R10: an agent whose only access is the open board is not 403."""
        resp = self.api_get('/api/boards/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([b['id'] for b in self.get_results(resp)], [self.open_board.id])
        resp = self.api_get(f'/api/boards/{self.open_board.id}/kanban_board/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        resp = self.api_get(f'/api/boards/{self.open_board.id}/kanban_board/?lite=1', user=self.agent)
        self.assertEqual(resp.status_code, 200)

    def test_kanban_of_restricted_board_not_found(self):
        resp = self.api_get(f'/api/boards/{self.restricted.id}/kanban_board/', user=self.agent)
        self.assertEqual(resp.status_code, 404)

    def test_kanban_returns_all_tickets_of_accessible_board(self):
        other = self.create_ticket('Someone else', column=self.r_col2, created_by=self.admin)
        resp = self.api_get(f'/api/boards/{self.restricted.id}/kanban_board/?lite=1', user=self.member)
        self.assertEqual(resp.status_code, 200)
        ids = {c['id'] for cards in resp.data['tickets_by_column'].values() for c in cards}
        self.assertEqual(ids, {self.hidden.id, other.id})

    def test_user_without_any_board_gets_403(self):
        self.open_board.board_users.add(self.member)  # no open board left
        resp = self.api_get('/api/boards/', user=self.agent)
        self.assertEqual(resp.status_code, 403)


class TestDefaultBoard(VisibilityBase):

    def test_accessible_default_board(self):
        self.open_board.is_default = True
        self.open_board.save()
        resp = self.api_get('/api/boards/default/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], self.open_board.id)

    def test_inaccessible_default_falls_back_to_first_accessible(self):
        self.restricted.is_default = True
        self.restricted.save()
        second_open = self.create_board('A first by name', created_by=self.admin, open=True)
        second_open.position = 5
        second_open.save()
        resp = self.api_get('/api/boards/default/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        # position 0 ("E2E Open") wins over position 5, despite the name
        self.assertEqual(resp.data['id'], self.open_board.id)

    def test_404_without_accessible_boards(self):
        self.open_board.board_users.add(self.member)
        self.agent.can_view_boards = True  # passes the permission, sees no board
        self.agent.save()
        resp = self.api_get('/api/boards/default/', user=self.agent)
        self.assertEqual(resp.status_code, 404)

    def test_staff_gets_tenant_default(self):
        self.restricted.is_default = True
        self.restricted.save()
        resp = self.api_get('/api/boards/default/', user=self.admin)
        self.assertEqual(resp.data['id'], self.restricted.id)


class TestChildEndpoints(VisibilityBase):
    """Every child endpoint follows ticket visibility: 200/201 on the open
    board, 404 (reads, writes on rows) or 403 (creates) on hidden tickets."""

    def test_comments(self):
        TicketComment.objects.create(ticket=self.hidden, user=self.member, comment='h')
        TicketComment.objects.create(ticket=self.open_ticket, user=self.member, comment='o')
        resp = self.api_get('/api/comments/', user=self.agent)
        self.assertEqual({c['ticket'] for c in self.get_results(resp)}, {self.open_ticket.id})
        ok = self.api_post('/api/comments/', {'ticket': self.open_ticket.id, 'comment': 'x'}, user=self.agent)
        self.assertEqual(ok.status_code, 201)
        denied = self.api_post('/api/comments/', {'ticket': self.hidden.id, 'comment': 'x'}, user=self.agent)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(
            self.api_get(f'/api/tickets/{self.hidden.id}/comments/', user=self.agent).status_code, 404,
        )

    def test_add_comment_action_for_viewer(self):
        resp = self.api_post(f'/api/tickets/{self.open_ticket.id}/add_comment/',
                             {'comment': 'via action'}, user=self.agent)
        self.assertEqual(resp.status_code, 201, resp.data)
        resp = self.api_post(f'/api/tickets/{self.hidden.id}/add_comment/',
                             {'comment': 'x'}, user=self.agent)
        self.assertEqual(resp.status_code, 404)

    def test_attachments(self):
        self.create_attachment(self.hidden)
        mine = self.create_attachment(self.open_ticket)
        resp = self.api_get('/api/attachments/', user=self.agent)
        self.assertEqual([a['id'] for a in self.get_results(resp)], [mine.id])
        client = self.authenticated_client(self.agent)
        up = client.post('/api/attachments/', {
            'ticket': self.open_ticket.id, 'file': SimpleUploadedFile('a.txt', b'a'),
        }, format='multipart', HTTP_HOST='tenant.test.com')
        self.assertEqual(up.status_code, 201, up.data)
        up = client.post('/api/attachments/', {
            'ticket': self.hidden.id, 'file': SimpleUploadedFile('a.txt', b'a'),
        }, format='multipart', HTTP_HOST='tenant.test.com')
        self.assertEqual(up.status_code, 403)

    def test_checklist(self):
        hidden_item = self.create_checklist_item(self.hidden, 'h', created_by=self.agent)
        open_item = self.create_checklist_item(self.open_ticket, 'o')
        resp = self.api_get('/api/checklist-items/', user=self.agent)
        # an item the agent created on a hidden ticket is hidden too (F7)
        self.assertEqual([i['id'] for i in self.get_results(resp)], [open_item.id])

        self.assertEqual(self.api_post('/api/checklist-items/', {
            'ticket': self.open_ticket.id, 'text': 'new'}, user=self.agent).status_code, 201)
        self.assertEqual(self.api_post('/api/checklist-items/', {
            'ticket': self.hidden.id, 'text': 'new'}, user=self.agent).status_code, 403)

        base = f'/api/checklist-items/{open_item.id}/'
        self.assertEqual(self.api_patch(base + 'toggle_check/', user=self.agent).status_code, 200)
        self.assertEqual(self.api_patch(base + 'reorder/', {'position': 0}, user=self.agent).status_code, 200)
        self.assertEqual(self.api_patch(base, {'text': 'edited'}, user=self.agent).status_code, 200)

        hidden = f'/api/checklist-items/{hidden_item.id}/'
        self.assertEqual(self.api_patch(hidden + 'toggle_check/', user=self.agent).status_code, 404)
        self.assertEqual(self.api_patch(hidden + 'reorder/', {'position': 0}, user=self.agent).status_code, 404)
        self.assertEqual(self.api_patch(hidden, {'text': 'x'}, user=self.agent).status_code, 404)
        self.assertEqual(self.api_delete(hidden, user=self.agent).status_code, 404)
        self.assertEqual(self.api_delete(base, user=self.agent).status_code, 204)

    def test_form_submissions(self):
        form = self.create_ticket_form(created_by=self.admin)
        from tickets.models import TicketFormSubmission
        TicketFormSubmission.objects.create(ticket=self.hidden, form=form, submitted_by=self.agent)
        visible = TicketFormSubmission.objects.create(ticket=self.open_ticket, form=form, submitted_by=self.member)
        resp = self.api_get('/api/form-submissions/', user=self.agent)
        self.assertEqual([s['id'] for s in self.get_results(resp)], [visible.id])
        ok = self.api_post('/api/form-submissions/', {
            'ticket': self.open_ticket.id, 'form_id': form.id, 'form_data': {}}, user=self.agent)
        self.assertEqual(ok.status_code, 201, ok.data)
        denied = self.api_post('/api/form-submissions/', {
            'ticket': self.hidden.id, 'form_id': form.id, 'form_data': {}}, user=self.agent)
        self.assertEqual(denied.status_code, 403)

    def test_time_logs(self):
        self.create_time_log(self.hidden, self.r_col, self.agent)
        visible = self.create_time_log(self.open_ticket, self.o_col, self.member)
        resp = self.api_get('/api/time-logs/', user=self.agent)
        self.assertEqual([t['id'] for t in self.get_results(resp)], [visible.id])
        self.assertEqual(self.api_get(
            f'/api/time-logs/ticket_summary/?ticket={self.open_ticket.id}', user=self.agent).status_code, 200)
        self.assertEqual(self.api_get(
            f'/api/time-logs/ticket_summary/?ticket={self.hidden.id}', user=self.agent).status_code, 404)

    def test_assignments(self):
        self.create_assignment(self.open_ticket, self.member)
        self.create_assignment(self.hidden, self.admin)
        resp = self.api_get(f'/api/tickets/{self.open_ticket.id}/assignments/', user=self.agent)
        self.assertEqual(len(self.get_results(resp)), 1)
        resp = self.api_get(f'/api/tickets/{self.hidden.id}/assignments/', user=self.agent)
        self.assertEqual(len(self.get_results(resp)), 0)
        denied = self.api_post(f'/api/tickets/{self.hidden.id}/assignments/',
                               {'role': 'collaborator'}, user=self.agent)
        self.assertEqual(denied.status_code, 403)

    def test_timer_and_time_entries(self):
        self.create_time_entry(self.hidden, self.member, ended_at=self.hidden.created_at, duration_seconds=5)
        self.create_time_entry(self.open_ticket, self.member, ended_at=self.open_ticket.created_at,
                               duration_seconds=5)
        self.assertEqual(self.api_get(f'/api/tickets/{self.open_ticket.id}/timer/', user=self.agent).status_code, 200)
        self.assertEqual(self.api_get(f'/api/tickets/{self.hidden.id}/timer/', user=self.agent).status_code, 404)
        resp = self.api_get('/api/time-entries/', user=self.agent)
        self.assertEqual({e['ticket_id'] for e in self.get_results(resp)}, {self.open_ticket.id})

    def test_activity_and_history(self):
        for action in ('activity', 'history'):
            with self.subTest(action=action):
                self.assertEqual(self.api_get(
                    f'/api/tickets/{self.open_ticket.id}/{action}/', user=self.agent).status_code, 200)
                self.assertEqual(self.api_get(
                    f'/api/tickets/{self.hidden.id}/{action}/', user=self.agent).status_code, 404)


def _feature(enabled):
    return mock.patch(
        'users.models.User.has_feature',
        side_effect=lambda key, *a, **k: enabled and key == 'social_integrations',
    )


class TestViewerPermissions(VisibilityBase):

    def _perms(self, user, ticket, feature=True):
        with _feature(feature):
            return ticket_viewer_permissions(user, ticket)

    @staticmethod
    def _expected(**overrides):
        base = {
            'can_edit': False, 'can_delete': False, 'can_move_to_closed': True,
            'can_assign': False, 'can_change_groups': False, 'can_transfer': False,
            'can_manage_payments': False, 'can_view_customer': True,
        }
        base.update(overrides)
        return base

    def test_staff(self):
        self.assertEqual(self._perms(self.admin, self.hidden), self._expected(
            can_edit=True, can_delete=True, can_assign=True, can_change_groups=True,
            can_transfer=True, can_manage_payments=True,
        ))

    def test_creator(self):
        self.assertEqual(self._perms(self.member, self.hidden),
                         self._expected(can_edit=True, can_transfer=True))

    def test_assignee(self):
        t = self.create_ticket('A', column=self.r_col, created_by=self.member, assigned_to=self.agent)
        self.assertEqual(self._perms(self.agent, t), self._expected(can_edit=True, can_transfer=True))
        t2 = self.create_ticket('B', column=self.r_col, created_by=self.member)
        self.create_assignment(t2, self.agent)
        self.assertEqual(self._perms(self.agent, t2), self._expected(can_edit=True, can_transfer=True))

    def test_group_assignee(self):
        group = self.create_tenant_group(name='Support')
        self.agent.tenant_groups.add(group)
        t = self.create_ticket('G', column=self.r_col, created_by=self.member)
        t.assigned_groups.add(group)
        self.assertEqual(self._perms(self.agent, t), self._expected(can_edit=True, can_transfer=True))

    def test_board_only_user(self):
        self.assertEqual(self._perms(self.agent, self.open_ticket), self._expected())

    def test_without_social_feature(self):
        self.assertEqual(self._perms(self.agent, self.open_ticket, feature=False),
                         self._expected(can_view_customer=False))

    def test_access_orders_can_manage_payments(self):
        self.agent.can_access_orders = True
        self.agent.save()
        self.assertTrue(self._perms(self.agent, self.open_ticket)['can_manage_payments'])

    def test_only_superadmin_delete_setting(self):
        """Deletion is staff-only whatever the tenant setting (TicketPermission
        refuses DELETE to non-staff); can_delete reports exactly that."""
        for flag in (True, False):
            with self.subTest(only_superadmin=flag):
                self.tenant.only_superadmin_can_delete_tickets = flag
                self.tenant.save()
                self.assertFalse(self._perms(self.member, self.hidden)['can_delete'])
                self.assertTrue(self._perms(self.admin, self.hidden)['can_delete'])
                self.assertEqual(
                    self.api_delete(f'/api/tickets/{self.hidden.id}/', user=self.member).status_code, 403,
                )

    def test_detail_response_carries_viewer_permissions(self):
        with _feature(True):
            resp = self.api_get(f'/api/tickets/{self.open_ticket.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['viewer_permissions'], self._expected())
