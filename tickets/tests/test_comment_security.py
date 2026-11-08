"""BE-02 / E6 (serializer part): comment authorship, is_internal, activity
keys. The visibility 403 cases are added by BE-06."""
import datetime

from amanati_crm.schema import TenantAwareSchemaGenerator
from tickets.models import TicketComment
from tickets.tests.conftest import TicketTestCase


class CommentBase(TicketTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='cs-admin@test.com')
        self.agent = self.create_user(email='cs-agent@test.com', first_name='Nino', last_name='Beridze')
        self.other = self.create_user(email='cs-other@test.com')
        self.board, self.col1, _, _ = self.setup_board_with_columns(admin=self.admin)
        self.ticket = self.create_ticket(title='C', column=self.col1, created_by=self.agent)


class TestCommentAuthorship(CommentBase):

    def test_user_id_accepted_and_ignored(self):
        resp = self.api_post('/api/comments/', {
            'ticket': self.ticket.id, 'comment': 'hello', 'user_id': self.other.id,
        }, user=self.agent)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['user']['id'], self.agent.id)
        self.assertEqual(TicketComment.objects.get(id=resp.data['id']).user_id, self.agent.id)
        self.assertNotIn('user_id', resp.data)

    def test_user_id_for_missing_user_is_not_an_error(self):
        resp = self.api_post('/api/comments/', {
            'ticket': self.ticket.id, 'comment': 'hello', 'user_id': 987654,
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['user']['id'], self.admin.id)

    def test_update_cannot_change_author(self):
        c = self.create_comment(self.ticket, self.agent, 'x')
        resp = self.api_patch(f'/api/comments/{c.id}/', {'comment': 'y', 'user_id': self.other.id}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        c.refresh_from_db()
        self.assertEqual(c.user_id, self.agent.id)
        self.assertEqual(c.comment, 'y')

    def test_response_shape(self):
        resp = self.api_post('/api/comments/', {'ticket': self.ticket.id, 'comment': 'x'}, user=self.agent)
        self.assertEqual(
            set(resp.data.keys()),
            {'id', 'ticket', 'user', 'comment', 'is_internal', 'created_at', 'updated_at'},
        )

    def test_openapi_request_schema_still_lists_user_id(self):
        schema = TenantAwareSchemaGenerator().get_schema(request=None, public=True)
        components = schema['components']['schemas']
        request_component = components.get('TicketCommentRequest') or components['TicketComment']
        self.assertIn('user_id', request_component['properties'])
        self.assertIn('Deprecated', request_component['properties']['user_id'].get('description', ''))
        self.assertIn('is_internal', components['TicketComment']['properties'])


class TestIsInternal(CommentBase):

    def test_defaults_to_true(self):
        resp = self.api_post('/api/comments/', {'ticket': self.ticket.id, 'comment': 'x'}, user=self.admin)
        self.assertEqual(resp.status_code, 201)
        self.assertIs(resp.data['is_internal'], True)

    def test_staff_may_send_false(self):
        resp = self.api_post('/api/comments/', {
            'ticket': self.ticket.id, 'comment': 'x', 'is_internal': False,
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201)
        self.assertIs(resp.data['is_internal'], False)
        self.assertFalse(TicketComment.objects.get(id=resp.data['id']).is_internal)

    def test_non_staff_false_is_forced_true(self):
        resp = self.api_post('/api/comments/', {
            'ticket': self.ticket.id, 'comment': 'x', 'is_internal': False,
        }, user=self.agent)
        self.assertEqual(resp.status_code, 201)
        self.assertIs(resp.data['is_internal'], True)
        self.assertTrue(TicketComment.objects.get(id=resp.data['id']).is_internal)

    def test_non_staff_update_cannot_clear(self):
        c = self.create_comment(self.ticket, self.agent, 'x')
        resp = self.api_patch(f'/api/comments/{c.id}/', {'is_internal': False}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        c.refresh_from_db()
        self.assertTrue(c.is_internal)

    def test_staff_patch_without_flag_keeps_it(self):
        c = TicketComment.objects.create(ticket=self.ticket, user=self.admin, comment='x', is_internal=False)
        resp = self.api_patch(f'/api/comments/{c.id}/', {'comment': 'edited'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        c.refresh_from_db()
        self.assertFalse(c.is_internal)

    def test_add_comment_with_ticket_in_body(self):
        # (TicketPermission only lets staff POST to ticket detail actions
        # today; BE-06 owns that rule, so the action is exercised as staff.)
        resp = self.api_post(f'/api/tickets/{self.ticket.id}/add_comment/', {
            'ticket': self.ticket.id, 'comment': 'via action', 'user_id': self.other.id,
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['user']['id'], self.admin.id)
        self.assertIs(resp.data['is_internal'], True)
        resp = self.api_post(f'/api/tickets/{self.ticket.id}/add_comment/', {
            'ticket': self.ticket.id, 'comment': 'staff public', 'is_internal': False,
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIs(resp.data['is_internal'], False)


class TestActivityKeys(CommentBase):

    def test_activity_items_carry_new_keys(self):
        c1 = self.create_comment(self.ticket, self.agent, 'first')
        c2 = TicketComment.objects.create(ticket=self.ticket, user=self.admin, comment='public', is_internal=False)
        # c2 was edited 10s after creation
        TicketComment.objects.filter(pk=c2.pk).update(
            updated_at=c2.created_at + datetime.timedelta(seconds=10),
        )
        self.api_patch(f'/api/tickets/{self.ticket.id}/', {'title': 'Renamed'}, user=self.admin)

        resp = self.api_get(f'/api/tickets/{self.ticket.id}/activity/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.data, list)
        comments = {i['comment_id']: i for i in resp.data if i['type'] == 'comment'}
        events = [i for i in resp.data if i['type'] == 'event']

        first = comments[c1.id]
        self.assertEqual(first['kind'], 'comment')
        self.assertIs(first['is_internal'], True)
        self.assertIs(first['edited'], False)
        self.assertEqual(first['user']['initials'], 'NB')
        self.assertEqual(first['user']['name'], 'Nino Beridze')
        # pre-existing keys are kept
        for key in ('type', 'id', 'comment_id', 'text', 'created_at', 'user'):
            self.assertIn(key, first)

        second = comments[c2.id]
        self.assertIs(second['is_internal'], False)
        self.assertIs(second['edited'], True)

        self.assertTrue(events)
        for e in events:
            self.assertEqual(e['kind'], 'history')
            self.assertIn('initials', e['user'])
            for key in ('action', 'field_name', 'old_value', 'new_value', 'description'):
                self.assertIn(key, e)
        # oldest first
        stamps = [i['created_at'] for i in resp.data]
        self.assertEqual(stamps, sorted(stamps))
