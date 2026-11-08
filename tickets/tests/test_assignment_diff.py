"""BE-02 / E5: PATCH assigned_user_ids diffs the assignments instead of
deleting and recreating them (GAP c, critic 12)."""
from tenants.models import Tenant
from tickets.models import TicketAssignment
from tickets.tests.conftest import TicketTestCase
from users.models import Notification


class TestAssignmentDiff(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='diff-admin@test.com')
        self.board, self.col1, _, _ = self.setup_board_with_columns(admin=self.admin)
        self.ticket = self.create_ticket(title='Diff', column=self.col1, created_by=self.admin)
        self.a = self.create_user(email='diff-a@test.com')
        self.b = self.create_user(email='diff-b@test.com')
        self.c = self.create_user(email='diff-c@test.com')
        self.url = f'/api/tickets/{self.ticket.id}/'
        resp = self.api_patch(self.url, {
            'assigned_user_ids': [self.a.id, self.b.id],
            'assignment_roles': {str(self.a.id): 'observer'},
        }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.row_a = TicketAssignment.objects.get(ticket=self.ticket, user=self.a)

    def assigned_notifications(self, user):
        return Notification.objects.filter(user=user, notification_type='ticket_assigned').count()

    def roles(self):
        return dict(TicketAssignment.objects.filter(ticket=self.ticket).values_list('user_id', 'role'))

    def test_diff_keeps_removes_and_adds(self):
        before = {u.id: self.assigned_notifications(u) for u in (self.a, self.b, self.c)}
        resp = self.api_patch(self.url, {'assigned_user_ids': [self.a.id, self.c.id]}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)

        row_a = TicketAssignment.objects.get(ticket=self.ticket, user=self.a)
        self.assertEqual(row_a.pk, self.row_a.pk)
        self.assertEqual(row_a.assigned_at, self.row_a.assigned_at)
        self.assertEqual(row_a.role, 'observer')  # kept: roles omitted
        self.assertFalse(TicketAssignment.objects.filter(ticket=self.ticket, user=self.b).exists())
        self.assertEqual(TicketAssignment.objects.get(ticket=self.ticket, user=self.c).role, 'collaborator')

        # Only the added user is notified
        self.assertEqual(self.assigned_notifications(self.a), before[self.a.id])
        self.assertEqual(self.assigned_notifications(self.b), before[self.b.id])
        self.assertEqual(self.assigned_notifications(self.c), before[self.c.id] + 1)

    def test_assignment_roles_change_only_named_user(self):
        b_row = TicketAssignment.objects.get(ticket=self.ticket, user=self.b)
        resp = self.api_patch(self.url, {
            'assigned_user_ids': [self.a.id, self.b.id],
            'assignment_roles': {str(self.a.id): 'reviewer'},
        }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.roles(), {self.a.id: 'reviewer', self.b.id: 'collaborator'})
        self.assertEqual(TicketAssignment.objects.get(pk=b_row.pk).assigned_at, b_row.assigned_at)
        self.assertEqual(TicketAssignment.objects.get(ticket=self.ticket, user=self.a).pk, self.row_a.pk)

    def test_omitting_roles_keeps_existing_roles(self):
        # Regression for critic 12: a plain re-save used to reset every role
        resp = self.api_patch(self.url, {'assigned_user_ids': [self.a.id, self.b.id]}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.roles(), {self.a.id: 'observer', self.b.id: 'collaborator'})

    def test_resave_same_set_sends_no_notifications(self):
        before = Notification.objects.filter(notification_type='ticket_assigned').count()
        self.api_patch(self.url, {'assigned_user_ids': [self.b.id, self.a.id]}, user=self.admin)
        self.assertEqual(Notification.objects.filter(notification_type='ticket_assigned').count(), before)

    def test_assigned_to_id_sets_primary_for_new_user(self):
        resp = self.api_patch(self.url, {
            'assigned_to_id': self.c.id,
            'assigned_user_ids': [self.a.id, self.b.id, self.c.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.roles()[self.c.id], 'primary')
        self.assertEqual(self.roles()[self.a.id], 'observer')

    def test_duplicate_ids_are_collapsed(self):
        resp = self.api_patch(self.url, {'assigned_user_ids': [self.c.id, self.c.id, self.a.id]}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(set(self.roles()), {self.a.id, self.c.id})

    def test_empty_list_removes_all(self):
        resp = self.api_patch(self.url, {'assigned_user_ids': []}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.roles(), {})

    def test_min_users_per_ticket_still_400(self):
        Tenant.objects.filter(schema_name='test').update(min_users_per_ticket=2)
        manager = self.create_user(email='diff-mgr@test.com')
        # creator of the ticket so TicketPermission lets them edit
        self.ticket.created_by = manager
        self.ticket.save()
        resp = self.api_patch(self.url, {'assigned_user_ids': [self.a.id]}, user=manager)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn('assigned_user_ids', resp.data)
        self.assertEqual(set(self.roles()), {self.a.id, self.b.id})

    def test_response_shape_unchanged(self):
        resp = self.api_patch(self.url, {'assigned_user_ids': [self.a.id, self.c.id]}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('assigned_user_ids', resp.data)
        self.assertNotIn('assignment_roles', resp.data)
        self.assertEqual({u['id'] for u in resp.data['assigned_users']}, {self.a.id, self.c.id})
        assignment = resp.data['assignments'][0]
        self.assertEqual(set(assignment.keys()), {'id', 'user', 'role', 'assigned_at', 'assigned_by'})
