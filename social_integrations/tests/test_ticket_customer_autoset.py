"""Tickets created from a chat or an email get ``Ticket.customer`` set (BE-04).

Chat: the ``Client`` linked to the conversation's ``SocialAccount``.
Email: the one ``Client`` whose email matches the customer sender
(case-insensitive); none or several matches leave it null. Response shapes
are unchanged.
"""
from unittest.mock import patch

from rest_framework import status

from social_integrations.models import SocialAccount, SocialClient
from social_integrations.tests.conftest import SocialIntegrationTestCase
from tickets.models import Ticket

CHAT_URL = '/api/social/conversations/create-ticket/'
EMAIL_URL = '/api/social/email/threads/create-ticket/'
CHAT_FEATURE = 'social_integrations.views_conversation_tickets.has_subscription_feature'
EMAIL_FEATURE = 'social_integrations.views_email_threads.has_subscription_feature'


@patch(CHAT_FEATURE, return_value=True)
class TestConversationCustomer(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='auto-agent@test.com')
        self.wa = self.create_wa_account(waba_id='waba-auto')
        self.number = '+995555000111'
        self.create_wa_message(business_account=self.wa, from_number=self.number,
                               contact_name='Giorgi', message_text='Hello')

    def post(self, **extra):
        payload = {'platform': 'whatsapp', 'conversation_id': self.number, 'account_id': 'waba-auto', **extra}
        return self.api_post(CHAT_URL, payload, user=self.agent)

    def link(self, client, platform_id=None, account='waba-auto', platform='whatsapp'):
        return SocialAccount.objects.create(
            client=client, platform=platform, platform_id=platform_id or self.number.lstrip('+'),
            account_connection_id=account,
        )

    def test_sets_customer_from_social_account(self, _f):
        client = SocialClient.objects.create(name='Giorgi')
        self.link(client)  # stored without '+', posted with '+'
        resp = self.post()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(set(resp.json()), {'ticket', 'created'})
        self.assertEqual(set(resp.json()['ticket']), {'id', 'title'})
        self.assertEqual(Ticket.objects.get(id=resp.json()['ticket']['id']).customer_id, client.id)

    def test_matches_other_key_form(self, _f):
        client = SocialClient.objects.create(name='Giorgi')
        self.link(client, platform_id=self.number)
        resp = self.post(conversation_id=self.number.lstrip('+'))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(Ticket.objects.get().customer_id, client.id)

    def test_no_client_leaves_null(self, _f):
        resp = self.post()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertIsNone(Ticket.objects.get().customer_id)

    def test_account_on_other_connection_or_platform_ignored(self, _f):
        other = SocialClient.objects.create(name='Elsewhere')
        self.link(other, account='waba-other')
        self.link(other, platform='telegram', account='waba-auto')
        self.post()
        self.assertIsNone(Ticket.objects.get().customer_id)

    def test_two_clients_for_key_forms_is_ambiguous(self, _f):
        self.link(SocialClient.objects.create(name='A'), platform_id=self.number)
        self.link(SocialClient.objects.create(name='B'), platform_id=self.number.lstrip('+'))
        self.post()
        self.assertIsNone(Ticket.objects.get().customer_id)

    def test_existing_ticket_not_changed(self, _f):
        first = self.post().json()['ticket']
        client = SocialClient.objects.create(name='Giorgi')
        self.link(client)
        resp = self.post()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {'ticket': first, 'created': False})
        self.assertIsNone(Ticket.objects.get().customer_id)

    def test_listing_shape_unchanged(self, _f):
        client = SocialClient.objects.create(name='Giorgi')
        self.link(client)
        ticket_id = self.post().json()['ticket']['id']
        rows = self.api_get(f'/api/social/conversations/tickets/?client_id={client.id}', user=self.agent).json()
        self.assertEqual([r['id'] for r in rows], [ticket_id])
        self.assertEqual(set(rows[0]), {'id', 'title', 'column', 'platform', 'created_at'})


@patch(EMAIL_FEATURE, return_value=True)
class TestEmailCustomer(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='auto-mail-agent@test.com')
        self.conn = self.create_email_connection()
        self.create_email_message(connection=self.conn, thread_id='t-a', subject='Invoice',
                                  from_email='Nino.Beridze@Example.com')
        self.create_email_message(connection=self.conn, thread_id='t-a', is_from_business=True,
                                  from_email=self.conn.email_address, subject='Re: Invoice')

    def post(self, thread_id='t-a'):
        return self.api_post(EMAIL_URL, {'connection_id': self.conn.id, 'thread_id': thread_id}, user=self.agent)

    def test_sets_customer_case_insensitive(self, _f):
        client = SocialClient.objects.create(name='Nino', email='nino.beridze@example.com')
        resp = self.post()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(set(resp.json()), {'ticket', 'created'})
        self.assertEqual(set(resp.json()['ticket']), {'id', 'title'})
        self.assertEqual(Ticket.objects.get(id=resp.json()['ticket']['id']).customer_id, client.id)

    def test_no_match_leaves_null(self, _f):
        SocialClient.objects.create(name='Other', email='other@example.com')
        SocialClient.objects.create(name='No email')
        self.post()
        self.assertIsNone(Ticket.objects.get().customer_id)

    def test_two_clients_same_email_is_ambiguous(self, _f):
        SocialClient.objects.create(name='Nino 1', email='nino.beridze@example.com')
        SocialClient.objects.create(name='Nino 2', email='NINO.BERIDZE@example.com')
        self.post()
        self.assertIsNone(Ticket.objects.get().customer_id)

    def test_business_only_thread_does_not_match_mailbox(self, _f):
        SocialClient.objects.create(name='Mailbox', email=self.conn.email_address)
        self.create_email_message(connection=self.conn, thread_id='t-out', is_from_business=True,
                                  from_email=self.conn.email_address, subject='Outbound')
        resp = self.post('t-out')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertIsNone(Ticket.objects.get().customer_id)

    def test_existing_ticket_not_changed(self, _f):
        first = self.post().json()['ticket']
        SocialClient.objects.create(name='Nino', email='nino.beridze@example.com')
        resp = self.post()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {'ticket': first, 'created': False})
        self.assertIsNone(Ticket.objects.get().customer_id)
