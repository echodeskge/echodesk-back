"""Tests for TicketFormViewSet and TicketFormSubmissionViewSet."""
from tickets.models import TicketForm
from tickets.tests.conftest import TicketTestCase


class TestTicketFormCRUD(TicketTestCase):

    def test_create_form(self):
        admin = self.create_admin()
        resp = self.api_post('/api/ticket-forms/', {'title': 'Order Form'}, user=admin)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['title'], 'Order Form')

    def test_list_forms(self):
        admin = self.create_admin()
        self.create_ticket_form(title='Form A', created_by=admin)
        self.create_ticket_form(title='Form B', created_by=admin)
        resp = self.api_get('/api/ticket-forms/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 2)

    def test_update_form(self):
        admin = self.create_admin()
        form = self.create_ticket_form(title='Old', created_by=admin)
        resp = self.api_patch(f'/api/ticket-forms/{form.id}/', {'title': 'New'}, user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['title'], 'New')

    def test_delete_form(self):
        admin = self.create_admin()
        form = self.create_ticket_form(title='Del', created_by=admin)
        resp = self.api_delete(f'/api/ticket-forms/{form.id}/', user=admin)
        self.assertEqual(resp.status_code, 204)


class TestTicketFormActions(TicketTestCase):

    def test_get_default_form(self):
        admin = self.create_admin()
        self.create_ticket_form(title='Default', created_by=admin, is_default=True)
        resp = self.api_get('/api/ticket-forms/default/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['title'], 'Default')

    def test_set_default_form(self):
        admin = self.create_admin()
        form = self.create_ticket_form(title='SetDef', created_by=admin)
        resp = self.api_post(f'/api/ticket-forms/{form.id}/set_default/', user=admin)
        self.assertEqual(resp.status_code, 200)
        form.refresh_from_db()
        self.assertTrue(form.is_default)

    def test_non_staff_cannot_set_default_form(self):
        admin = self.create_admin()
        form = self.create_ticket_form(title='F', created_by=admin)
        user = self.create_user(email='regular@test.com')
        resp = self.api_post(f'/api/ticket-forms/{form.id}/set_default/', user=user)
        self.assertEqual(resp.status_code, 403)

    def test_with_lists(self):
        admin = self.create_admin()
        form = self.create_ticket_form(title='WithLists', created_by=admin)
        lst = self.create_item_list(title='Products', created_by=admin)
        form.item_lists.add(lst)
        resp = self.api_get(f'/api/ticket-forms/{form.id}/with_lists/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['item_lists']), 1)


class TestFormSubmission(TicketTestCase):

    def test_create_submission(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        form = self.create_ticket_form(title='F', created_by=admin)
        resp = self.api_post('/api/form-submissions/', {
            'ticket': ticket.id, 'form_id': form.id, 'form_data': {'key': 'value'}
        }, user=admin)
        self.assertEqual(resp.status_code, 201)

    def test_by_form(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        form = self.create_ticket_form(title='F', created_by=admin)
        from tickets.models import TicketFormSubmission
        TicketFormSubmission.objects.create(
            ticket=ticket, form=form, submitted_by=admin, form_data={'k': 'v'}
        )
        resp = self.api_get(f'/api/form-submissions/by_form/?form_id={form.id}', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(self.get_results(resp)), 1)

    def test_by_ticket(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        form = self.create_ticket_form(title='F', created_by=admin)
        from tickets.models import TicketFormSubmission
        TicketFormSubmission.objects.create(
            ticket=ticket, form=form, submitted_by=admin, form_data={'k': 'v'}
        )
        resp = self.api_get(
            f'/api/form-submissions/by_ticket/?ticket_id={ticket.id}', user=admin
        )
        self.assertEqual(resp.status_code, 200)


class TestFormSubmissionVisibility(TicketTestCase):
    """BE-06 / F8: submissions follow ticket visibility."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='fs-admin@test.com')
        self.member = self.create_user(email='fs-member@test.com')
        self.stranger = self.create_user(email='fs-stranger@test.com')
        self.board = self.create_board('Forms', created_by=self.admin, users=[self.member])
        self.col = self.create_column(self.board, 'New', 0)
        self.ticket = self.create_ticket('T', column=self.col, created_by=self.admin)
        self.form = self.create_ticket_form(created_by=self.admin)

    def test_board_member_sees_submissions_of_board_tickets(self):
        from tickets.models import TicketFormSubmission
        sub = TicketFormSubmission.objects.create(ticket=self.ticket, form=self.form, submitted_by=self.admin)
        resp = self.api_get('/api/form-submissions/', user=self.member)
        self.assertEqual([s['id'] for s in self.get_results(resp)], [sub.id])
        resp = self.api_get('/api/form-submissions/', user=self.stranger)
        self.assertEqual(self.get_results(resp), [])

    def test_create_on_invisible_ticket_is_403(self):
        resp = self.api_post('/api/form-submissions/', {
            'ticket': self.ticket.id, 'form_id': self.form.id, 'form_data': {},
        }, user=self.stranger)
        self.assertEqual(resp.status_code, 403)

    def test_create_on_visible_ticket_is_201(self):
        resp = self.api_post('/api/form-submissions/', {
            'ticket': self.ticket.id, 'form_id': self.form.id, 'form_data': {},
        }, user=self.member)
        self.assertEqual(resp.status_code, 201, resp.data)
