"""BE-02: redesign serializer fields (due date, customer gate, board,
reporter, card and list fields, description preview)."""
import datetime
from unittest import mock

from django.utils import timezone

from tickets.models import Ticket
from tickets.querysets import DESCRIPTION_PREVIEW_CHARS, make_description_preview
from tickets.serializers import TicketCardSerializer, initials_for
from tickets.tests.conftest import TicketTestCase, capture_queries


def freeze(dt):
    return mock.patch('tickets.dates.timezone.now', return_value=dt)


def social_feature(enabled):
    """Patch User.has_feature so only 'social_integrations' is toggled."""
    return mock.patch(
        'users.models.User.has_feature',
        side_effect=lambda key, *a, **k: enabled and key == 'social_integrations',
        autospec=False,
    )


# Every key TicketSerializer returned before the redesign (0040 snapshot).
TICKET_KEYS_0040 = {
    'id', 'title', 'description', 'rich_description', 'description_format',
    'status', 'priority', 'is_closed', 'is_order', 'column', 'position_in_column',
    'created_at', 'updated_at', 'created_by', 'assigned_to',
    'assigned_users', 'assignments', 'assigned_groups',
    'assigned_department', 'tags', 'comments', 'comments_count',
    'checklist_items', 'checklist_items_count', 'completed_checklist_items_count',
    'price', 'currency', 'is_paid', 'amount_paid', 'payment_due_date',
    'payments', 'remaining_balance', 'payment_status', 'is_overdue', 'form_submissions',
    'attachments',
}
TICKET_LIST_KEYS_0040 = {
    'id', 'title', 'description', 'description_format',
    'status', 'priority', 'is_closed', 'column', 'position_in_column',
    'created_at', 'updated_at', 'created_by', 'assigned_to', 'assigned_users',
    'assigned_groups', 'assignments', 'tags', 'comments_count',
}


class RedesignBase(TicketTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='be02-admin@test.com')
        self.board, self.col1, self.col2, self.done = self.setup_board_with_columns(admin=self.admin)
        self.board.color = '#ff8800'
        self.board.save()
        self.ticket = self.create_ticket(title='T', column=self.col1, created_by=self.admin)

    def url(self, ticket=None):
        return f'/api/tickets/{(ticket or self.ticket).id}/'


class TestDueDate(RedesignBase):

    def test_due_date_round_trips_including_null(self):
        resp = self.api_patch(self.url(), {'due_date': '2026-11-12'}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['due_date'], '2026-11-12')
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.due_date, datetime.date(2026, 11, 12))

        resp = self.api_get(self.url(), user=self.admin)
        self.assertEqual(resp.data['due_date'], '2026-11-12')

        resp = self.api_patch(self.url(), {'due_date': None}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIsNone(resp.data['due_date'])
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.due_date)

    def test_is_overdue_task_uses_tbilisi_date_and_open_column(self):
        self.ticket.due_date = datetime.date(2026, 11, 9)
        self.ticket.save()
        # 19:00 UTC = 23:00 in Tbilisi: still the 9th -> not overdue
        with freeze(datetime.datetime(2026, 11, 9, 19, 0, tzinfo=datetime.timezone.utc)):
            resp = self.api_get(self.url(), user=self.admin)
        self.assertFalse(resp.data['is_overdue_task'])
        # 21:30 UTC = 01:30 on the 10th in Tbilisi -> overdue
        with freeze(datetime.datetime(2026, 11, 9, 21, 30, tzinfo=datetime.timezone.utc)):
            resp = self.api_get(self.url(), user=self.admin)
            self.assertTrue(resp.data['is_overdue_task'])
            # payment overdue is unrelated
            self.assertFalse(resp.data['is_overdue'])
            # in a closed column it is never overdue
            self.ticket.column = self.done
            self.ticket.save()
            resp = self.api_get(self.url(), user=self.admin)
            self.assertFalse(resp.data['is_overdue_task'])

    def test_future_or_missing_due_date_not_overdue(self):
        resp = self.api_get(self.url(), user=self.admin)
        self.assertFalse(resp.data['is_overdue_task'])
        self.ticket.due_date = timezone.now().date() + datetime.timedelta(days=3)
        self.ticket.save()
        resp = self.api_get(self.url(), user=self.admin)
        self.assertFalse(resp.data['is_overdue_task'])


class TestCustomerGate(RedesignBase):

    def setUp(self):
        super().setUp()
        self.client_a = self.create_client(name='Nino B.', email='n@x.ge', phone='+995555')
        self.client_b = self.create_client(name='Other', email=None, phone='')

    def test_set_and_clear_with_feature(self):
        with social_feature(True):
            resp = self.api_patch(self.url(), {'customer_id': self.client_a.id}, user=self.admin)
            self.assertEqual(resp.status_code, 200, resp.data)
            self.assertEqual(resp.data['customer'], {
                'id': self.client_a.id, 'name': 'Nino B.', 'email': 'n@x.ge', 'phone': '+995555',
            })
            self.assertNotIn('customer_id', resp.data)
            self.ticket.refresh_from_db()
            self.assertEqual(self.ticket.customer_id, self.client_a.id)

            resp = self.api_get(self.url(), user=self.admin)
            self.assertEqual(resp.data['customer']['id'], self.client_a.id)

            resp = self.api_patch(self.url(), {'customer_id': self.client_b.id}, user=self.admin)
            self.assertEqual(resp.data['customer']['email'], None)

            resp = self.api_patch(self.url(), {'customer_id': None}, user=self.admin)
            self.assertEqual(resp.status_code, 200, resp.data)
            self.assertIsNone(resp.data['customer'])
            self.ticket.refresh_from_db()
            self.assertIsNone(self.ticket.customer_id)

    def test_missing_client_is_400(self):
        with social_feature(True):
            resp = self.api_patch(self.url(), {'customer_id': 999999}, user=self.admin)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('customer_id', resp.data)

    def test_without_feature_customer_hidden_and_write_ignored(self):
        self.ticket.customer = self.client_a
        self.ticket.save()
        with social_feature(False):
            resp = self.api_get(self.url(), user=self.admin)
            self.assertIsNone(resp.data['customer'])
            resp = self.api_patch(
                self.url(), {'customer_id': self.client_b.id, 'title': 'renamed'}, user=self.admin,
            )
            self.assertEqual(resp.status_code, 200, resp.data)
            self.assertIsNone(resp.data['customer'])
            resp = self.api_patch(self.url(), {'customer_id': None}, user=self.admin)
            self.assertEqual(resp.status_code, 200, resp.data)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.customer_id, self.client_a.id)  # unchanged
        self.assertEqual(self.ticket.title, 'renamed')

    def test_create_with_customer_respects_gate(self):
        body = {'title': 'New', 'column_id': self.col1.id, 'customer_id': self.client_a.id}
        with social_feature(True):
            resp = self.api_post('/api/tickets/', body, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Ticket.objects.get(id=resp.data['id']).customer_id, self.client_a.id)
        with social_feature(False):
            resp = self.api_post('/api/tickets/', body, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIsNone(Ticket.objects.get(id=resp.data['id']).customer_id)


class TestBoardAndReporter(RedesignBase):

    def test_board_and_reporter_present(self):
        self.admin.first_name, self.admin.last_name = 'Giorgi', 'Gordia'
        self.admin.save()
        resp = self.api_get(self.url(), user=self.admin)
        self.assertEqual(resp.data['board'], {'id': self.board.id, 'name': 'Main Board', 'color': '#ff8800'})
        self.assertEqual(resp.data['reporter'], {
            'id': self.admin.id, 'name': 'Giorgi Gordia', 'initials': 'GG', 'email': 'be02-admin@test.com',
        })

    def test_board_null_without_column(self):
        t = self.create_ticket(title='No column', column=self.col1, created_by=self.admin)
        Ticket.objects.filter(pk=t.pk).update(column=None)  # save() would pick a default column
        resp = self.api_get(self.url(t), user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data['board'])

    def test_reporter_name_falls_back_to_email(self):
        user = self.create_user(email='nobody.here@x.ge')
        t = self.create_ticket(title='x', column=self.col1, created_by=user)
        resp = self.api_get(self.url(t), user=self.admin)
        self.assertEqual(resp.data['reporter']['name'], 'nobody.here@x.ge')
        self.assertEqual(resp.data['reporter']['initials'], 'NH')

    def test_initials_rules(self):
        self.assertEqual(initials_for('Nino'), 'N')
        self.assertEqual(initials_for('nino beridze'), 'NB')
        self.assertEqual(initials_for('Anna Maria Smith'), 'AS')
        self.assertEqual(initials_for('', 'g@x.ge'), 'G')
        self.assertEqual(initials_for('', 'giorgi_g@x.ge'), 'GG')
        self.assertEqual(initials_for('', ''), '')
        # Georgian: no Mtavruli upper-casing
        self.assertEqual(initials_for('ნინო ბერიძე'), 'ნბ')
        self.assertEqual(initials_for('გიორგი'), 'გ')

    def test_all_pre_existing_keys_still_present(self):
        resp = self.api_get(self.url(), user=self.admin)
        missing = TICKET_KEYS_0040 - set(resp.data.keys())
        self.assertFalse(missing, missing)
        for key in ('due_date', 'is_overdue_task', 'customer', 'board', 'reporter'):
            self.assertIn(key, resp.data)

    def test_checklist_and_comment_nested_fields(self):
        item = self.create_checklist_item(self.ticket, text='a')
        self.create_comment(self.ticket, self.admin, 'hi')
        self.api_patch(f'/api/checklist-items/{item.id}/toggle_check/', user=self.admin)
        resp = self.api_get(self.url(), user=self.admin)
        ci = resp.data['checklist_items'][0]
        self.assertEqual(ci['checked_by']['id'], self.admin.id)
        self.assertIsNotNone(ci['checked_at'])
        self.assertIs(resp.data['comments'][0]['is_internal'], True)


class TestCardSerializer(RedesignBase):

    def test_bare_instance_serialises_with_defaults(self):
        t = Ticket.objects.create(title='bare', column=self.col1, created_by=self.admin)
        data = TicketCardSerializer(t).data
        self.assertEqual(data['checklist_items_count'], 0)
        self.assertEqual(data['completed_checklist_items_count'], 0)
        self.assertEqual(data['comments_count'], 0)
        self.assertEqual(data['attachments_count'], 0)
        self.assertIsNone(data['due_date'])
        self.assertIs(data['is_overdue_task'], False)
        self.assertEqual(data['description_preview'], '')

    def test_bare_instance_preview_from_loaded_description(self):
        t = Ticket.objects.create(
            title='bare', column=self.col1, created_by=self.admin, description='<p>Hello <b>there</b></p>',
        )
        self.assertEqual(TicketCardSerializer(t).data['description_preview'], 'Hello there')

    def test_preview_helper(self):
        self.assertEqual(make_description_preview('<p>a&amp;b</p>\n\n<p>c</p>'), 'a&b c')
        self.assertEqual(make_description_preview(None), '')
        long = 'x' * 500
        self.assertEqual(len(make_description_preview(long)), DESCRIPTION_PREVIEW_CHARS)
        # A tag cut off by the database-side substring is dropped
        self.assertEqual(make_description_preview('Hello <img src="data:image/png;base64,AAA'), 'Hello')


class TestDescriptionPreviewKanban(RedesignBase):

    def test_large_description_with_base64_image(self):
        image = '<img src="data:image/png;base64,' + ('A' * 50_000) + '">'
        text = 'Customer reports the printer is jammed. ' * 10
        self.ticket.description = f'<p>{image}</p><p>{text}</p>'
        self.ticket.save()
        with capture_queries() as ctx:
            resp = self.api_get(f'/api/boards/{self.board.id}/kanban_board/?lite=1', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        card = resp.data['tickets_by_column'][self.col1.id][0]
        preview = card['description_preview']
        self.assertTrue(preview.startswith('Customer reports the printer is jammed.'), preview)
        self.assertLessEqual(len(preview), DESCRIPTION_PREVIEW_CHARS)
        self.assertNotIn('base64', preview)

        ticket_sql = [q['sql'] for q in ctx.captured_queries
                      if 'FROM "tickets_ticket"' in q['sql'] and 'SUBSTRING' in q['sql']]
        self.assertEqual(len(ticket_sql), 1, [q['sql'][:120] for q in ctx.captured_queries])
        sql = ticket_sql[0]
        self.assertNotIn('"tickets_ticket"."rich_description"', sql)
        # The only reference to description is inside the SUBSTRING(regexp_replace(...)) head
        self.assertEqual(sql.count('"tickets_ticket"."description"'), 1)
        self.assertIn('SUBSTRING(regexp_replace("tickets_ticket"."description"', sql)


class TestListSerializer(RedesignBase):

    def test_list_has_new_and_old_keys(self):
        self.ticket.due_date = datetime.date(2020, 1, 1)
        self.ticket.save()
        self.create_checklist_item(self.ticket, text='a', is_checked=True)
        self.create_checklist_item(self.ticket, text='b')
        self.create_attachment(self.ticket, uploaded_by=self.admin, file='attachments/x.txt')
        self.create_comment(self.ticket, self.admin, 'c')
        resp = self.api_get('/api/tickets/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        row = self.get_results(resp)[0]
        missing = TICKET_LIST_KEYS_0040 - set(row.keys())
        self.assertFalse(missing, missing)
        self.assertEqual(row['attachments_count'], 1)
        self.assertEqual(row['checklist_items_count'], 2)
        self.assertEqual(row['completed_checklist_items_count'], 1)
        self.assertEqual(row['comments_count'], 1)
        self.assertEqual(row['due_date'], '2020-01-01')
        self.assertIs(row['is_overdue_task'], True)

    def test_list_counts_not_inflated_by_joins(self):
        # Several tags, assignees and children: counts stay exact
        u1 = self.create_user(email='l1@test.com')
        u2 = self.create_user(email='l2@test.com')
        self.ticket.assigned_users.add(u1, u2)
        self.ticket.tags.add(self.create_tag('A'), self.create_tag('B'))
        for i in range(3):
            self.create_comment(self.ticket, self.admin, f'c{i}')
            self.create_checklist_item(self.ticket, text=f'i{i}')
        resp = self.api_get('/api/tickets/', user=self.admin)
        row = self.get_results(resp)[0]
        self.assertEqual(row['comments_count'], 3)
        self.assertEqual(row['checklist_items_count'], 3)

    def test_list_sql_has_no_group_by(self):
        with capture_queries() as ctx:
            self.api_get('/api/tickets/', user=self.admin)
        main = [q['sql'] for q in ctx.captured_queries if 'FROM "tickets_ticket"' in q['sql']]
        self.assertTrue(main)
        for sql in main:
            self.assertNotIn('GROUP BY', sql)

    def test_my_tickets_and_assigned_to_me_have_new_keys(self):
        resp = self.api_get('/api/tickets/my_tickets/', user=self.admin)
        row = self.get_results(resp)[0]
        for key in ('attachments_count', 'checklist_items_count', 'due_date', 'is_overdue_task'):
            self.assertIn(key, row)
