"""Tickets from chat conversations and client tags (Messages profile rail)."""
from unittest.mock import patch

from rest_framework import status

from social_integrations.models import ConversationTicket, SocialAccount, SocialClient
from social_integrations.tests.conftest import SocialIntegrationTestCase
from tickets.models import Board, Ticket, TicketColumn

CREATE_URL = '/api/social/conversations/create-ticket/'
LIST_URL = '/api/social/conversations/tickets/'
FEATURE = 'social_integrations.views_conversation_tickets.has_subscription_feature'


@patch(FEATURE, return_value=True)
class TestConversationTickets(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='ct-admin@test.com')
        self.agent = self.create_user(email='ct-agent@test.com')
        self.wa = self.create_wa_account(waba_id='waba-1')
        self.number = '+995555123456'
        self.create_wa_message(business_account=self.wa, from_number=self.number,
                               contact_name='Nino Beridze', message_text='My order is late')
        self.create_wa_message(business_account=self.wa, from_number=self.number,
                               contact_name='Nino Beridze', message_text='Order #4821')
        self.create_wa_message(business_account=self.wa, from_number=self.wa.phone_number,
                               to_number=self.number, is_from_business=True, message_text='Checking now')

    def payload(self, **extra):
        return {'platform': 'whatsapp', 'conversation_id': self.number, 'account_id': 'waba-1', **extra}

    def private_column(self):
        board = Board.objects.create(name='Private', created_by=self.admin)
        board.board_users.add(self.admin)
        return TicketColumn.objects.create(board=board, name='Todo', position=0, created_by=self.admin)

    def test_creates_and_links(self, _f):
        resp = self.api_post(CREATE_URL, self.payload(), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        body = resp.json()
        self.assertTrue(body['created'])
        ticket = Ticket.objects.get(id=body['ticket']['id'])
        self.assertEqual(ticket.title, 'Nino Beridze · WhatsApp')
        self.assertIn('My order is late', ticket.description)
        self.assertIn('Order #4821', ticket.description)
        self.assertNotIn('Checking now', ticket.description)  # only customer messages
        self.assertEqual(ticket.created_by, self.agent)
        link = ConversationTicket.objects.get(ticket=ticket)
        self.assertEqual((link.platform, link.conversation_id, link.account_id), ('whatsapp', self.number, 'waba-1'))

    def test_second_call_returns_existing_even_with_other_key_form(self, _f):
        first = self.api_post(CREATE_URL, self.payload(), user=self.agent).json()['ticket']
        resp = self.api_post(CREATE_URL, self.payload(conversation_id=self.number.lstrip('+')), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {'ticket': first, 'created': False})
        self.assertEqual(Ticket.objects.count(), 1)

    def test_unknown_conversation_404(self, _f):
        resp = self.api_post(CREATE_URL, self.payload(conversation_id='+10000000000'), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_gates(self, feature):
        self.agent.can_create_tickets = False
        self.agent.save()
        self.assertEqual(self.api_post(CREATE_URL, self.payload(), user=self.agent).status_code,
                         status.HTTP_403_FORBIDDEN)
        feature.return_value = False
        self.assertEqual(self.api_post(CREATE_URL, self.payload(), user=self.admin).status_code,
                         status.HTTP_403_FORBIDDEN)
        self.assertFalse(Ticket.objects.exists())

    def test_email_platform_rejected(self, _f):
        resp = self.api_post(CREATE_URL, self.payload(platform='email'), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_inaccessible_column_rejected(self, _f):
        column = self.private_column()
        resp = self.api_post(CREATE_URL, self.payload(column_id=column.id), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        resp = self.api_post(CREATE_URL, self.payload(column_id=column.id), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_by_conversation_and_by_client(self, _f):
        ticket_id = self.api_post(CREATE_URL, self.payload(), user=self.agent).json()['ticket']['id']
        rows = self.api_get(f'{LIST_URL}?platform=whatsapp&conversation_id={self.number.lstrip("+")}'
                            f'&account_id=waba-1', user=self.agent).json()
        self.assertEqual([r['id'] for r in rows], [ticket_id])
        self.assertEqual(rows[0]['platform'], 'whatsapp')
        self.assertEqual(set(rows[0]), {'id', 'title', 'column', 'platform', 'created_at'})

        client = SocialClient.objects.create(name='Nino')
        SocialAccount.objects.create(client=client, platform='whatsapp', platform_id=self.number.lstrip('+'),
                                     account_connection_id='waba-1')
        rows = self.api_get(f'{LIST_URL}?client_id={client.id}', user=self.agent).json()
        self.assertEqual([r['id'] for r in rows], [ticket_id])
        other = SocialClient.objects.create(name='Nobody')
        self.assertEqual(self.api_get(f'{LIST_URL}?client_id={other.id}', user=self.agent).json(), [])

    def test_list_hides_title_of_restricted_board(self, _f):
        column = self.private_column()
        self.api_post(CREATE_URL, self.payload(column_id=column.id), user=self.admin)
        query = f'{LIST_URL}?platform=whatsapp&conversation_id={self.number}&account_id=waba-1'
        hidden = self.api_get(query, user=self.agent).json()[0]
        self.assertIsNone(hidden['title'])
        self.assertIsNone(hidden['column'])
        self.assertEqual(self.api_get(query, user=self.admin).json()[0]['column'], 'Todo')

    def test_list_needs_params(self, _f):
        self.assertEqual(self.api_get(LIST_URL, user=self.agent).status_code, status.HTTP_400_BAD_REQUEST)


class TestClientTags(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='tags-admin@test.com')
        self.client_obj = SocialClient.objects.create(name='Ana')

    def test_patch_tags_normalised_and_returned(self):
        url = f'/api/social/clients/{self.client_obj.id}/'
        resp = self.api_patch(url, {'tags': [' VIP ', 'vip', '', 'Wholesale  buyer']}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.tags, ['VIP', 'Wholesale buyer'])
        self.assertEqual(self.api_get(url, user=self.admin).json()['tags'], ['VIP', 'Wholesale buyer'])
        listed = self.api_get('/api/social/clients/', user=self.admin).json()
        rows = listed['results'] if isinstance(listed, dict) else listed
        self.assertEqual(next(r for r in rows if r['id'] == self.client_obj.id)['tags'], ['VIP', 'Wholesale buyer'])

    def test_limits(self):
        url = f'/api/social/clients/{self.client_obj.id}/'
        self.assertEqual(self.api_patch(url, {'tags': ['x' * 41]}, user=self.admin).status_code,
                         status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.api_patch(url, {'tags': [f't{i}' for i in range(31)]}, user=self.admin).status_code,
                         status.HTTP_400_BAD_REQUEST)
