"""TicketHistory rows written by tickets/signals.py since BE-08 (E15).

One test per event, plus the noise checks: creating a ticket writes exactly
one ``created`` row (and a plain ``Ticket.objects.create`` none at all),
re-writing the same set writes nothing, and changes without a request user
are recorded with ``user=None``.

Board broadcasts are not asserted here (see test_broadcasts.py); their
on-commit callbacks are simply never run inside the test transaction.
"""
from contextlib import nullcontext
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status

from social_integrations.models import Client
from social_integrations.tests.conftest import SocialIntegrationTestCase
from tickets.models import ChecklistItem, Ticket, TicketAttachment, TicketFormSubmission, TicketHistory
from tickets.tests.conftest import TicketTestCase
from tickets.tests.test_attachment_views import InMemoryStorageMixin


def rows(ticket, **filters):
    return list(TicketHistory.objects.filter(ticket=ticket, **filters).order_by('id'))


class HistoryTestCase(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='hr-admin@test.com')
        self.board = self.create_board(name='Board A', created_by=self.admin)
        self.col1 = self.create_column(self.board, name='Todo', position=0)
        self.col2 = self.create_column(self.board, name='Doing', position=1)
        self.ticket = self.create_ticket('History', column=self.col1, created_by=self.admin)
        self.alice = self.create_user(email='hr-alice@test.com', first_name='Alice', last_name='A')
        self.bob = self.create_user(email='hr-bob@test.com', first_name='Bob', last_name='B')

    def patch_ticket(self, data):
        resp = self.api_patch(f'/api/tickets/{self.ticket.id}/', data, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp


class TestDueDateHistory(HistoryTestCase):

    def test_set_change_clear(self):
        self.patch_ticket({'due_date': '2026-03-10'})
        self.patch_ticket({'due_date': '2026-03-12'})
        self.patch_ticket({'due_date': None})
        due = rows(self.ticket, action='due_date_changed')
        self.assertEqual(
            [(r.old_value, r.new_value, r.description) for r in due],
            [
                ('', '2026-03-10', 'Due date: none → 2026-03-10'),
                ('2026-03-10', '2026-03-12', 'Due date: 2026-03-10 → 2026-03-12'),
                ('2026-03-12', '', 'Due date: 2026-03-12 → none'),
            ],
        )
        self.assertTrue(all(r.user_id == self.admin.id and r.field_name == 'due_date' for r in due))


class TestTransferHistory(HistoryTestCase):

    def setUp(self):
        super().setUp()
        self.board_b = self.create_board(name='Board B', created_by=self.admin)
        self.col_b = self.create_column(self.board_b, name='Inbox')

    def assert_transferred(self):
        transferred = rows(self.ticket, action='transferred')
        self.assertEqual(len(transferred), 1)
        row = transferred[0]
        self.assertEqual((row.old_value, row.new_value), ('Board A', 'Board B'))
        self.assertEqual(row.description, 'Board A → Board B')
        self.assertEqual(row.user_id, self.admin.id)

    def test_move_to_column(self):
        resp = self.api_patch(f'/api/tickets/{self.ticket.id}/move_to_column/', {
            'column_id': self.col_b.id,
        }, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assert_transferred()

    def test_patch_column_id(self):
        self.patch_ticket({'column_id': self.col_b.id})
        self.assert_transferred()

    def test_move_within_board_is_not_a_transfer(self):
        self.patch_ticket({'column_id': self.col2.id})
        self.assertEqual(rows(self.ticket, action='transferred'), [])
        self.assertEqual(len(rows(self.ticket, action='status_changed')), 1)


class TestCustomerHistory(HistoryTestCase):

    def test_customer_set_and_changed(self):
        nino = Client.objects.create(name='Nino')
        giorgi = Client.objects.create(name='Giorgi')
        self.ticket._current_user = self.admin
        self.ticket.customer = nino
        self.ticket.save()
        self.ticket.customer = giorgi
        self.ticket.save()
        customer = rows(self.ticket, field_name='customer')
        self.assertEqual([(r.action, r.old_value, r.new_value) for r in customer],
                         [('updated', '', 'Nino'), ('updated', 'Nino', 'Giorgi')])
        self.assertEqual(customer[1].description, 'Customer: Nino → Giorgi')
        self.assertEqual(customer[0].user_id, self.admin.id)


class TestTagHistory(HistoryTestCase):

    def test_added_removed_and_cleared(self):
        bug = self.create_tag(name='Bug')
        vip = self.create_tag(name='VIP')
        self.patch_ticket({'tag_ids': [bug.id, vip.id]})
        self.patch_ticket({'tag_ids': [vip.id]})
        self.patch_ticket({'tag_ids': [vip.id]})  # same set again: no rows
        self.assertEqual(
            [(r.action, r.old_value or r.new_value) for r in rows(self.ticket, field_name='tags')],
            [('tag_added', 'Bug'), ('tag_added', 'VIP'), ('tag_removed', 'Bug')],
        )
        self.ticket.tags.clear()  # post_clear uses the set captured in pre_clear
        last = rows(self.ticket, field_name='tags')[-1]
        self.assertEqual((last.action, last.old_value, last.description), ('tag_removed', 'VIP', 'Removed label VIP'))

    def test_reverse_side_add(self):
        bug = self.create_tag(name='Bug')
        bug.tickets.add(self.ticket)
        self.assertEqual([r.new_value for r in rows(self.ticket, action='tag_added')], ['Bug'])


class TestGroupHistory(HistoryTestCase):

    def test_group_added_and_removed(self):
        group = self.create_tenant_group(name='Support')
        self.patch_ticket({'assigned_group_ids': [group.id]})
        self.patch_ticket({'assigned_group_ids': []})
        self.assertEqual(
            [(r.action, r.description) for r in rows(self.ticket, field_name='assigned_groups')],
            [('assigned', 'Assigned group Support'), ('unassigned', 'Unassigned group Support')],
        )


class TestAssignmentHistory(HistoryTestCase):

    def assignment_rows(self):
        return [(r.action, r.old_value or r.new_value)
                for r in rows(self.ticket, field_name='assigned_users')]

    def test_diff_based_with_no_noise(self):
        self.patch_ticket({'assigned_user_ids': [self.alice.id, self.bob.id]})
        self.patch_ticket({'assigned_user_ids': [self.alice.id, self.bob.id]})  # same set: nothing
        self.patch_ticket({'assigned_user_ids': [self.bob.id]})
        self.assertEqual(self.assignment_rows(), [
            ('assigned', 'Alice A'), ('assigned', 'Bob B'), ('unassigned', 'Alice A'),
        ])
        self.assertTrue(all(r.user_id == self.admin.id for r in rows(self.ticket, field_name='assigned_users')))

    def test_bulk_unassign_one_row_per_removed_user(self):
        carol = self.create_user(email='hr-carol@test.com', first_name='Carol', last_name='C')
        for user in (self.alice, self.bob, carol):
            self.create_assignment(self.ticket, user, assigned_by=self.admin)
        TicketHistory.objects.filter(ticket=self.ticket).delete()
        client = self.authenticated_client(self.admin)
        resp = client.delete(f'/api/tickets/{self.ticket.id}/assignments/bulk_unassign/',
                             {'user_ids': [self.alice.id, self.bob.id]}, format='json',
                             HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(sorted(self.assignment_rows()), [('unassigned', 'Alice A'), ('unassigned', 'Bob B')])

    def test_assignment_delete_endpoint(self):
        assignment = self.create_assignment(self.ticket, self.alice, assigned_by=self.admin)
        TicketHistory.objects.filter(ticket=self.ticket).delete()
        resp = self.api_delete(f'/api/tickets/{self.ticket.id}/assignments/{assignment.id}/', user=self.admin)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(self.assignment_rows(), [('unassigned', 'Alice A')])

    def test_bulk_assign_one_row_per_user_actually_added(self):
        self.create_assignment(self.ticket, self.alice, assigned_by=self.admin)
        TicketHistory.objects.filter(ticket=self.ticket).delete()
        carol = self.create_user(email='hr-carol2@test.com', first_name='Carol', last_name='C')
        resp = self.api_post(f'/api/tickets/{self.ticket.id}/assignments/bulk_assign/', {
            'user_ids': [self.alice.id, self.bob.id, carol.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(sorted(self.assignment_rows()), [('assigned', 'Bob B'), ('assigned', 'Carol C')])

    def test_deleting_the_ticket_or_user_writes_nothing(self):
        self.create_assignment(self.ticket, self.alice, assigned_by=self.admin)
        other = self.create_ticket('Other', column=self.col1, created_by=self.admin)
        self.create_assignment(other, self.bob, assigned_by=self.admin)
        TicketHistory.objects.all().delete()
        self.bob.delete()
        self.assertFalse(TicketHistory.objects.exists())
        self.ticket.delete()  # cascades to its assignment, attachments, checklist
        self.assertFalse(TicketHistory.objects.exists())


class TestAttachmentHistory(InMemoryStorageMixin, HistoryTestCase):

    def test_added_and_removed(self):
        client = self.authenticated_client(self.admin)
        resp = client.post('/api/attachments/', {
            'ticket': self.ticket.id,
            'file': SimpleUploadedFile('notes.txt', b'hello', content_type='text/plain'),
        }, format='multipart', HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 201, resp.data)
        resp = self.api_delete(f'/api/attachments/{resp.data["id"]}/', user=self.admin)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(
            [(r.action, r.description, r.user_id) for r in rows(self.ticket, field_name='attachments')],
            [('attachment_added', 'Attached notes.txt', self.admin.id),
             ('attachment_removed', 'Removed attachment notes.txt', self.admin.id)],
        )


class TestChecklistHistory(HistoryTestCase):

    def test_create_toggle_rename_delete_and_reorder(self):
        resp = self.api_post('/api/checklist-items/', {
            'ticket': self.ticket.id, 'text': 'Call back',
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        item_id = resp.data['id']
        other = ChecklistItem.objects.create(ticket=self.ticket, text='Second', position=1,
                                             created_by=self.admin)
        self.api_patch(f'/api/checklist-items/{item_id}/toggle_check/', {}, user=self.admin)
        self.api_patch(f'/api/checklist-items/{item_id}/toggle_check/', {}, user=self.admin)
        self.api_patch(f'/api/checklist-items/{item_id}/', {'text': 'Call back today'}, user=self.admin)
        before_reorder = len(rows(self.ticket, action='checklist_updated'))
        resp = self.api_patch(f'/api/checklist-items/{other.id}/reorder/', {'position': 0}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(rows(self.ticket, action='checklist_updated')), before_reorder,
                         'a position-only save writes no row')
        self.api_delete(f'/api/checklist-items/{item_id}/', user=self.admin)

        self.assertEqual(
            [r.description for r in rows(self.ticket, action='checklist_updated')],
            [
                'Added checklist item: Call back',
                'Added checklist item: Second',
                'Checked: Call back',
                'Unchecked: Call back',
                'Renamed checklist item: Call back → Call back today',
                'Removed checklist item: Call back today',
            ],
        )


class TestFormSubmissionHistory(HistoryTestCase):

    def test_form_submitted(self):
        form = self.create_ticket_form(title='Intake', created_by=self.admin)
        TicketFormSubmission.objects.create(ticket=self.ticket, form=form, submitted_by=self.alice)
        row = rows(self.ticket, action='form_submitted')
        self.assertEqual([(r.new_value, r.description, r.user_id) for r in row],
                         [('Intake', 'Submitted form Intake', self.alice.id)])


class TestCreateNoise(HistoryTestCase):

    def test_api_create_writes_exactly_one_created_row(self):
        tag = self.create_tag(name='Bug')
        group = self.create_tenant_group(name='Support')
        resp = self.api_post('/api/tickets/', {
            'title': 'Full', 'description': 'b', 'column_id': self.col1.id,
            'tag_ids': [tag.id], 'assigned_user_ids': [self.alice.id, self.bob.id],
            'assigned_group_ids': [group.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        history = rows(Ticket.objects.get(pk=resp.data['id']))
        self.assertEqual([(r.action, r.user_id) for r in history], [('created', self.admin.id)])

    def test_plain_model_create_writes_nothing(self):
        ticket = self.create_ticket('Plain', column=self.col1, created_by=self.admin)
        ticket.tags.add(self.create_tag(name='x'))  # later edits are recorded
        self.assertEqual([r.action for r in rows(ticket)], ['tag_added'])
        fresh = Ticket.objects.create(title='Bare', column=self.col1, created_by=self.admin)
        self.assertEqual(rows(fresh), [])

    def test_change_without_request_user_records_none(self):
        tag = self.create_tag(name='Admin edit')
        self.ticket.tags.add(tag)
        self.ticket.assigned_groups.add(self.create_tenant_group(name='G'))
        self.ticket.title = 'Edited in admin'
        self.ticket.save()
        history = rows(self.ticket)
        self.assertEqual({r.action for r in history}, {'tag_added', 'assigned', 'updated'})
        self.assertTrue(all(r.user_id is None for r in history))


class TestReportBugHistory(InMemoryStorageMixin, HistoryTestCase):

    def test_report_bug_writes_one_created_row(self):
        """B27: report_bug still works and writes one ``created`` row; its
        attachment writes none."""
        superuser = self.create_user(email='hr-root@test.com', is_superuser=True, is_staff=True)
        bugs_board = self.create_board(name='EchoDesk', created_by=superuser)
        self.create_column(bugs_board, name='Bugs')
        # report_bug files into the 'groot' schema; run it in the test schema
        with patch('tickets.support_views.schema_context', lambda name: nullcontext()):
            resp = self.authenticated_client(self.alice).post('/api/support/report-bug/', {
                'title': 'Broken', 'description': 'It broke', 'priority': 'high',
                'files': [SimpleUploadedFile('shot.png', b'png', content_type='image/png')],
            }, format='multipart', HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 201, resp.data)
        ticket = Ticket.objects.get(pk=resp.data['ticket_id'])
        self.assertEqual(TicketAttachment.objects.filter(ticket=ticket).count(), 1)
        self.assertEqual([(r.action, r.user_id) for r in rows(ticket)], [('created', superuser.id)])


class TestDemoSeedHistory(TicketTestCase):

    def test_seed_writes_only_its_own_history_rows(self):
        """F11: with history muted, the seed's row count is what it was before
        BE-08 (only the explicit 'Moved from' rows; no tag / assignee /
        checklist / attachment rows)."""
        from tenants.demo_seed import DemoSeeder, muted_asterisk_sync, muted_push
        seeder = DemoSeeder(self.tenant)
        with muted_push(), muted_asterisk_sync():
            seeder.seed_users()
            seeder.seed_tickets()
        history = TicketHistory.objects.all()
        self.assertTrue(history.exists())
        self.assertEqual(set(history.values_list('action', flat=True)), {'status_changed'})
        self.assertEqual(history.count(), history.filter(description__startswith='Moved from ').count())


@patch('social_integrations.views_conversation_tickets.has_subscription_feature', return_value=True)
@patch('social_integrations.views_email_threads.has_subscription_feature', return_value=True)
class TestSocialCreateHistory(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='hr-social@test.com')

    def test_conversation_create_ticket_writes_one_created_row(self, *_):
        wa = self.create_wa_account(waba_id='waba-hr')
        number = '+995555000111'
        self.create_wa_message(business_account=wa, from_number=number, contact_name='Nino',
                               message_text='Hello')
        resp = self.api_post('/api/social/conversations/create-ticket/', {
            'platform': 'whatsapp', 'conversation_id': number, 'account_id': 'waba-hr',
        }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        ticket = Ticket.objects.get(pk=resp.json()['ticket']['id'])
        self.assertEqual([(r.action, r.user_id) for r in rows(ticket)], [('created', self.agent.id)])

    def test_email_thread_create_ticket_writes_one_created_row(self, *_):
        conn = self.create_email_connection()
        self.create_email_message(connection=conn, thread_id='hr-t', subject='Printer')
        resp = self.api_post('/api/social/email/threads/create-ticket/', {
            'connection_id': conn.id, 'thread_id': 'hr-t',
        }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        ticket = Ticket.objects.get(pk=resp.json()['ticket']['id'])
        self.assertEqual([(r.action, r.user_id) for r in rows(ticket)], [('created', self.agent.id)])
