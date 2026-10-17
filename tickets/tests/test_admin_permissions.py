"""
Permission alignment for ticket administration (columns, tags, item lists,
list items, ticket forms, board Telegram connection), column reorder scoping
and the form/submission fixes.

Reads stay open to every authenticated user; writes need the matching
permission (manage_columns / manage_tags / manage_settings / edit_boards),
with staff and superusers always allowed.
"""
from unittest import mock

from tickets.models import TicketColumn, Tag, ItemList, ListItem, TicketForm, TicketFormSubmission
from tickets.tests.conftest import TicketTestCase


class AdminPermissionMixin:
    def make_staff(self, email):
        # Staff but not superuser and not admin role: staff alone must pass
        return self.create_user(email=email, role='agent', is_staff=True)

    def make_superuser(self, email):
        return self.create_user(email=email, role='agent', is_superuser=True)

    def make_agent(self, email, **flags):
        return self.create_user(email=email, role='agent', **flags)


# ---------------------------------------------------------------- columns

class TestColumnWritePermissions(AdminPermissionMixin, TicketTestCase):

    def setUp(self):
        super().setUp()
        self.board = self.create_board()

    def _create(self, user, name='Review'):
        return self.api_post('/api/columns/', {
            'name': name, 'board': self.board.id, 'position': 0, 'color': '#123ABC',
        }, user=user)

    def test_staff_who_is_not_superuser_can_create(self):
        resp = self._create(self.make_staff('col-staff@test.com'))
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIn('id', resp.data)
        self.assertTrue(TicketColumn.objects.filter(id=resp.data['id'], name='Review').exists())

    def test_superuser_can_create(self):
        resp = self._create(self.make_superuser('col-su@test.com'))
        self.assertEqual(resp.status_code, 201)

    def test_manager_role_can_create(self):
        resp = self._create(self.create_manager(email='col-mgr@test.com'))
        self.assertEqual(resp.status_code, 201)

    def test_agent_with_manage_columns_flag_can_create(self):
        resp = self._create(self.make_agent('col-flag@test.com', can_manage_columns=True))
        self.assertEqual(resp.status_code, 201)

    def test_plain_agent_cannot_write_but_can_read(self):
        agent = self.make_agent('col-plain@test.com')
        col = self.create_column(self.board, name='Todo', position=0)
        self.assertEqual(self._create(agent).status_code, 403)
        self.assertEqual(self.api_patch(f'/api/columns/{col.id}/', {'name': 'X'}, user=agent).status_code, 403)
        self.assertEqual(self.api_put(f'/api/columns/{col.id}/', {
            'name': 'X', 'board': self.board.id,
        }, user=agent).status_code, 403)
        self.assertEqual(self.api_delete(f'/api/columns/{col.id}/', user=agent).status_code, 403)
        self.assertEqual(
            self.api_post(f'/api/columns/{col.id}/reorder/', {'position': 0}, user=agent).status_code, 403
        )
        self.assertEqual(self.api_get(f'/api/columns/?board={self.board.id}', user=agent).status_code, 200)
        self.assertEqual(self.api_get(f'/api/columns/{col.id}/', user=agent).status_code, 200)

    def test_viewer_role_cannot_create(self):
        self.assertEqual(self._create(self.create_viewer(email='col-viewer@test.com')).status_code, 403)

    def test_update_response_includes_id(self):
        staff = self.make_staff('col-upd@test.com')
        col = self.create_column(self.board, name='Todo', position=0)
        resp = self.api_patch(f'/api/columns/{col.id}/', {'name': 'Doing'}, user=staff)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], col.id)
        self.assertEqual(resp.data['name'], 'Doing')

    def test_invalid_colour_rejected_on_create_and_update(self):
        staff = self.make_staff('col-colour@test.com')
        resp = self.api_post('/api/columns/', {
            'name': 'Bad', 'board': self.board.id, 'color': 'red',
        }, user=staff)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('color', resp.data)
        col = self.create_column(self.board, name='Todo', position=0)
        resp = self.api_patch(f'/api/columns/{col.id}/', {'color': '#12345'}, user=staff)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('color', resp.data)

    def test_delete_with_tickets_still_refused(self):
        staff = self.make_staff('col-del@test.com')
        col = self.create_column(self.board, name='Todo', position=0)
        self.create_ticket(column=col)
        resp = self.api_delete(f'/api/columns/{col.id}/', user=staff)
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(TicketColumn.objects.filter(id=col.id).exists())


class TestColumnReorderScoping(AdminPermissionMixin, TicketTestCase):

    def setUp(self):
        super().setUp()
        self.staff = self.make_staff('reorder-staff@test.com')
        self.board_a = self.create_board(name='A')
        self.board_b = self.create_board(name='B')
        self.a = [self.create_column(self.board_a, name=f'A{i}', position=i) for i in range(3)]
        self.b = [self.create_column(self.board_b, name=f'B{i}', position=i) for i in range(3)]

    def positions(self, board):
        return list(
            TicketColumn.objects.filter(board=board)
            .order_by('position', 'created_at', 'id').values_list('name', 'position')
        )

    def test_reorder_only_touches_own_board_and_returns_board_columns(self):
        resp = self.api_post(f'/api/columns/{self.a[0].id}/reorder/', {'position': 2}, user=self.staff)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.positions(self.board_a), [('A1', 0), ('A2', 1), ('A0', 2)])
        self.assertEqual(self.positions(self.board_b), [('B0', 0), ('B1', 1), ('B2', 2)])
        self.assertEqual(resp.data['message'], 'Column reordered successfully')
        self.assertEqual(resp.data['board'], self.board_a.id)
        self.assertEqual([c['name'] for c in resp.data['columns']], ['A1', 'A2', 'A0'])
        self.assertEqual([c['position'] for c in resp.data['columns']], [0, 1, 2])
        self.assertTrue(all(c['board'] == self.board_a.id for c in resp.data['columns']))
        self.assertIn('tickets_count', resp.data['columns'][0])

    def test_move_left(self):
        resp = self.api_post(f'/api/columns/{self.a[2].id}/reorder/', {'position': 0}, user=self.staff)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.positions(self.board_a), [('A2', 0), ('A0', 1), ('A1', 2)])
        self.assertEqual(self.positions(self.board_b), [('B0', 0), ('B1', 1), ('B2', 2)])

    def test_position_past_end_is_clamped(self):
        resp = self.api_post(f'/api/columns/{self.a[0].id}/reorder/', {'position': 99}, user=self.staff)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.positions(self.board_a), [('A1', 0), ('A2', 1), ('A0', 2)])

    def test_gapped_positions_are_renumbered(self):
        TicketColumn.objects.filter(id=self.a[0].id).update(position=1)
        TicketColumn.objects.filter(id=self.a[1].id).update(position=5)
        TicketColumn.objects.filter(id=self.a[2].id).update(position=9)
        resp = self.api_post(f'/api/columns/{self.a[2].id}/reorder/', {'position': 1}, user=self.staff)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.positions(self.board_a), [('A0', 0), ('A2', 1), ('A1', 2)])

    def test_negative_position_rejected(self):
        resp = self.api_post(f'/api/columns/{self.a[0].id}/reorder/', {'position': -1}, user=self.staff)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.positions(self.board_a), [('A0', 0), ('A1', 1), ('A2', 2)])

    def test_manage_columns_flag_can_reorder(self):
        agent = self.make_agent('reorder-flag@test.com', can_manage_columns=True)
        resp = self.api_post(f'/api/columns/{self.a[1].id}/reorder/', {'position': 0}, user=agent)
        self.assertEqual(resp.status_code, 200)


# ------------------------------------------------------------------- tags

class TestTagWritePermissions(AdminPermissionMixin, TicketTestCase):

    def test_plain_agent_can_list_but_not_write(self):
        agent = self.make_agent('tag-agent@test.com')
        tag = self.create_tag(name='Existing')
        self.assertEqual(self.api_get('/api/tags/', user=agent).status_code, 200)
        self.assertEqual(self.api_get(f'/api/tags/{tag.id}/', user=agent).status_code, 200)
        self.assertEqual(self.api_post('/api/tags/', {'name': 'New', 'color': '#FF0000'}, user=agent).status_code, 403)
        self.assertEqual(self.api_patch(f'/api/tags/{tag.id}/', {'name': 'Renamed'}, user=agent).status_code, 403)
        self.assertEqual(self.api_delete(f'/api/tags/{tag.id}/', user=agent).status_code, 403)
        self.assertTrue(Tag.objects.filter(id=tag.id, name='Existing').exists())

    def test_manage_tags_flag_and_staff_can_write(self):
        flagged = self.make_agent('tag-flag@test.com', can_manage_tags=True)
        resp = self.api_post('/api/tags/', {'name': 'Flagged', 'color': '#00FF00'}, user=flagged)
        self.assertEqual(resp.status_code, 201)
        staff = self.make_staff('tag-staff@test.com')
        resp = self.api_patch(f"/api/tags/{resp.data['id']}/", {'name': 'ByStaff'}, user=staff)
        self.assertEqual(resp.status_code, 200)
        su = self.make_superuser('tag-su@test.com')
        self.assertEqual(self.api_delete(f"/api/tags/{resp.data['id']}/", user=su).status_code, 204)


# -------------------------------------------------- item lists / list items

class TestItemListWritePermissions(AdminPermissionMixin, TicketTestCase):

    def setUp(self):
        super().setUp()
        self.item_list = self.create_item_list(title='Products', is_public=True)
        self.item = self.create_list_item(self.item_list, label='Phone')

    def test_plain_agent_reads_but_cannot_write_lists(self):
        agent = self.make_agent('il-agent@test.com')
        self.assertEqual(self.api_get('/api/item-lists/', user=agent).status_code, 200)
        self.assertEqual(self.api_get(f'/api/item-lists/{self.item_list.id}/', user=agent).status_code, 200)
        self.assertEqual(
            self.api_get(f'/api/item-lists/{self.item_list.id}/root_items/', user=agent).status_code, 200
        )
        self.assertEqual(self.api_post('/api/item-lists/', {'title': 'X'}, user=agent).status_code, 403)
        self.assertEqual(
            self.api_patch(f'/api/item-lists/{self.item_list.id}/', {'title': 'Y'}, user=agent).status_code, 403
        )
        self.assertEqual(self.api_delete(f'/api/item-lists/{self.item_list.id}/', user=agent).status_code, 403)
        self.assertTrue(ItemList.objects.filter(id=self.item_list.id, title='Products').exists())

    def test_plain_agent_reads_but_cannot_write_items(self):
        agent = self.make_agent('li-agent@test.com')
        self.assertEqual(self.api_get(f'/api/list-items/?item_list={self.item_list.id}', user=agent).status_code, 200)
        self.assertEqual(self.api_post('/api/list-items/', {
            'item_list': self.item_list.id, 'label': 'Tablet',
        }, user=agent).status_code, 403)
        self.assertEqual(self.api_patch(f'/api/list-items/{self.item.id}/', {'label': 'Z'}, user=agent).status_code, 403)
        self.assertEqual(
            self.api_patch(f'/api/list-items/{self.item.id}/reorder/', {'position': 1}, user=agent).status_code, 403
        )
        self.assertEqual(self.api_delete(f'/api/list-items/{self.item.id}/', user=agent).status_code, 403)
        self.assertTrue(ListItem.objects.filter(id=self.item.id, label='Phone').exists())

    def test_admin_role_flag_and_staff_can_write(self):
        admin_role = self.create_user(email='il-adminrole@test.com', role='admin')  # not staff
        resp = self.api_post('/api/item-lists/', {'title': 'Services'}, user=admin_role)
        self.assertEqual(resp.status_code, 201)
        flagged = self.make_agent('il-flag@test.com', can_manage_settings=True)
        resp = self.api_post('/api/list-items/', {
            'item_list': self.item_list.id, 'label': 'Tablet',
        }, user=flagged)
        self.assertEqual(resp.status_code, 201)
        staff = self.make_staff('il-staff@test.com')
        self.assertEqual(self.api_delete(f'/api/list-items/{self.item.id}/', user=staff).status_code, 204)

    def test_manager_role_lacks_manage_settings(self):
        manager = self.create_manager(email='il-mgr@test.com')
        self.assertEqual(self.api_post('/api/item-lists/', {'title': 'X'}, user=manager).status_code, 403)

    def test_public_endpoints_stay_anonymous(self):
        self.assertEqual(self.api_get('/api/item-lists/public/').status_code, 200)
        self.assertEqual(self.api_get(f'/api/item-lists/{self.item_list.id}/public_items/').status_code, 200)


# ------------------------------------------------------------ ticket forms

class TestTicketFormPermissionsAndFixes(AdminPermissionMixin, TicketTestCase):

    def test_plain_agent_reads_but_cannot_write(self):
        agent = self.make_agent('tf-agent@test.com')
        form = self.create_ticket_form(title='Intake')
        self.assertEqual(self.api_get('/api/ticket-forms/', user=agent).status_code, 200)
        self.assertEqual(self.api_get(f'/api/ticket-forms/{form.id}/', user=agent).status_code, 200)
        self.assertEqual(self.api_post('/api/ticket-forms/', {'title': 'New'}, user=agent).status_code, 403)
        self.assertEqual(self.api_patch(f'/api/ticket-forms/{form.id}/', {'title': 'Z'}, user=agent).status_code, 403)
        self.assertEqual(self.api_delete(f'/api/ticket-forms/{form.id}/', user=agent).status_code, 403)
        self.assertEqual(self.api_post(f'/api/ticket-forms/{form.id}/set_default/', {}, user=agent).status_code, 403)
        form.refresh_from_db()
        self.assertEqual(form.title, 'Intake')
        self.assertFalse(form.is_default)

    def test_manage_settings_user_can_write_and_set_default(self):
        flagged = self.make_agent('tf-flag@test.com', can_manage_settings=True)
        resp = self.api_post('/api/ticket-forms/', {'title': 'Flagged form'}, user=flagged)
        self.assertEqual(resp.status_code, 201)
        resp = self.api_post(f"/api/ticket-forms/{resp.data['id']}/set_default/", {}, user=flagged)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {'status': 'default set'})
        self.assertTrue(TicketForm.objects.get(title='Flagged form').is_default)

    def test_null_parent_form_id_clears_parent(self):
        staff = self.make_staff('tf-parent@test.com')
        parent = self.create_ticket_form(title='Parent')
        child = self.create_ticket_form(title='Child', parent_form=parent)
        # absent: unchanged
        resp = self.api_patch(f'/api/ticket-forms/{child.id}/', {'title': 'Child 2'}, user=staff)
        self.assertEqual(resp.status_code, 200)
        child.refresh_from_db()
        self.assertEqual(child.parent_form_id, parent.id)
        # null: cleared
        resp = self.api_patch(f'/api/ticket-forms/{child.id}/', {'parent_form_id': None}, user=staff)
        self.assertEqual(resp.status_code, 200)
        child.refresh_from_db()
        self.assertIsNone(child.parent_form_id)
        self.assertIsNone(resp.data['parent_form'])
        # id: set again, 0: cleared
        self.api_patch(f'/api/ticket-forms/{child.id}/', {'parent_form_id': parent.id}, user=staff)
        child.refresh_from_db()
        self.assertEqual(child.parent_form_id, parent.id)
        self.api_patch(f'/api/ticket-forms/{child.id}/', {'parent_form_id': 0}, user=staff)
        child.refresh_from_db()
        self.assertIsNone(child.parent_form_id)

    def test_minimal_list_child_forms_only_active(self):
        staff = self.make_staff('tf-children@test.com')
        parent = self.create_ticket_form(title='P')
        self.create_ticket_form(title='Active child', parent_form=parent)
        self.create_ticket_form(title='Inactive child', parent_form=parent, is_active=False)
        resp = self.api_get('/api/ticket-forms/', user=staff)
        self.assertEqual(resp.status_code, 200)
        row = next(r for r in self.get_results(resp) if r['id'] == parent.id)
        self.assertEqual([c['title'] for c in row['child_forms']], ['Active child'])
        self.assertIsInstance(row['submissions_count'], int)

    def test_minimal_list_child_forms_query_count_is_flat(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        staff = self.make_staff('tf-nplus1@test.com')

        def add_parent(i):
            parent = self.create_ticket_form(title=f'P{i}')
            self.create_ticket_form(title=f'C{i}a', parent_form=parent)
            self.create_ticket_form(title=f'C{i}b', parent_form=parent, is_active=False)

        add_parent(0)
        self.api_get('/api/ticket-forms/', user=staff)  # warm caches
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(self.api_get('/api/ticket-forms/', user=staff).status_code, 200)
        for i in range(1, 6):
            add_parent(i)
        with CaptureQueriesContext(connection) as large:
            resp = self.api_get('/api/ticket-forms/', user=staff)
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(self.get_results(resp)), 6)
        self.assertEqual(len(large), len(small), [q['sql'] for q in large.captured_queries])


class TestFormSubmissionFixes(AdminPermissionMixin, TicketTestCase):

    def test_by_ticket_with_several_submissions_returns_newest(self):
        staff = self.make_staff('sub-staff@test.com')
        form = self.create_ticket_form(title='F')
        ticket = self.create_ticket()
        TicketFormSubmission.objects.create(form=form, ticket=ticket, submitted_by=staff, form_data={'n': 1})
        newest = TicketFormSubmission.objects.create(form=form, ticket=ticket, submitted_by=staff, form_data={'n': 2})
        resp = self.api_get(f'/api/form-submissions/by_ticket/?ticket_id={ticket.id}', user=staff)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], newest.id)

    def test_by_ticket_requires_ticket_id(self):
        staff = self.make_staff('sub-staff2@test.com')
        self.assertEqual(self.api_get('/api/form-submissions/by_ticket/', user=staff).status_code, 400)
        self.assertEqual(self.api_get('/api/form-submissions/by_form/', user=staff).status_code, 400)

    def test_non_staff_list_has_no_duplicates(self):
        agent = self.make_agent('sub-agent@test.com')
        other = self.make_agent('sub-other@test.com')
        form = self.create_ticket_form(title='F')
        ticket = self.create_ticket(created_by=agent)
        ticket.assigned_users.add(agent, other)
        sub = TicketFormSubmission.objects.create(form=form, ticket=ticket, submitted_by=agent, form_data={})
        resp = self.api_get('/api/form-submissions/', user=agent)
        self.assertEqual(resp.status_code, 200)
        ids = [r['id'] for r in self.get_results(resp)]
        self.assertEqual(ids, [sub.id])


# ---------------------------------------------------- board telegram writes

class TestBoardTelegramPermissions(AdminPermissionMixin, TicketTestCase):

    def setUp(self):
        super().setUp()
        self.board = self.create_board()
        self.url = f'/api/boards/{self.board.id}/telegram-connection/'

    def test_viewer_can_read_but_not_write(self):
        viewer = self.make_agent('tg-viewer@test.com', can_view_boards=True)
        self.assertEqual(self.api_get(self.url, user=viewer).status_code, 404)  # none configured
        self.assertEqual(self.api_post(self.url, {'bot_token_raw': 't', 'chat_id': '1'}, user=viewer).status_code, 403)
        self.assertEqual(self.api_put(self.url, {'chat_id': '2'}, user=viewer).status_code, 403)
        self.assertEqual(self.api_delete(self.url, user=viewer).status_code, 403)
        with mock.patch('tickets.telegram_utils.send_board_telegram_message', return_value=True) as send:
            resp = self.api_post(f'{self.url}test/', {'bot_token_raw': 't', 'chat_id': '1'}, user=viewer)
        self.assertEqual(resp.status_code, 403)
        send.assert_not_called()

    def test_edit_boards_user_can_write(self):
        editor = self.make_agent('tg-editor@test.com', can_view_boards=True, can_edit_boards=True)
        resp = self.api_post(self.url, {'bot_token_raw': '123:abc', 'chat_id': '42'}, user=editor)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(resp.data['has_token'])
        self.assertNotIn('bot_token_raw', resp.data)
        resp = self.api_put(self.url, {'chat_id': '43'}, user=editor)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['chat_id'], '43')
        with mock.patch('tickets.telegram_utils.send_board_telegram_message', return_value=True) as send:
            resp = self.api_post(f'{self.url}test/', {}, user=editor)
        self.assertEqual(resp.status_code, 200)
        send.assert_called_once()
        self.assertEqual(self.api_delete(self.url, user=editor).status_code, 204)

    def test_set_default_board_still_staff_only(self):
        editor = self.make_agent('tg-setdef@test.com', can_view_boards=True, can_edit_boards=True)
        self.assertEqual(self.api_post(f'/api/boards/{self.board.id}/set_default/', user=editor).status_code, 403)
        staff = self.make_staff('tg-setdef-staff@test.com')
        resp = self.api_post(f'/api/boards/{self.board.id}/set_default/', user=staff)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {'status': 'default set'})
