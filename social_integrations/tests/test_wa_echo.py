"""WhatsApp ``smb_message_echoes`` (messages typed in the WhatsApp Business app).

nitchiani, 2026-11-16: the owner answered a customer from the phone app. The
echoes were stored but (a) the WebSocket frame carried no ``platform`` /
``waba_id`` so the web inbox dropped it and the reply only appeared after a
reload, and (b) a photo sent from the phone was stored with no media at all.
"""
import json
from unittest.mock import MagicMock, patch

from social_integrations.models import WhatsAppMessage
from social_integrations.tests.conftest import SocialIntegrationTestCase

WEBHOOK = '/api/social/whatsapp/webhook/'
CUSTOMER = '8618836109809'


class WhatsAppEchoWebhookTests(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.waba = self.create_wa_account(
            waba_id='1383134697093574', phone_number_id='1290221420841082',
            phone_number='+995579370374', access_token='tok',
        )
        self.create_wa_message(
            business_account=self.waba, from_number=CUSTOMER, to_number='995579370374',
            contact_name='Chloe Lee', message_text='Final order list, right',
            is_from_business=False,
        )

    def _post_echo(self, echo):
        body = {
            'object': 'whatsapp_business_account',
            'entry': [{'id': '1383134697093574', 'changes': [{
                'field': 'smb_message_echoes',
                'value': {
                    'messaging_product': 'whatsapp',
                    'metadata': {'display_phone_number': '995579370374',
                                 'phone_number_id': '1290221420841082'},
                    'contacts': [{'wa_id': CUSTOMER, 'user_id': 'CN.1118823507483378'}],
                    'message_echoes': [echo],
                },
            }]}],
        }
        with patch('social_integrations.views.send_websocket_notification') as ws:
            resp = self.client.post(WEBHOOK, data=json.dumps(body), content_type='application/json',
                                    HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 200, resp.content)
        return ws

    def _echo(self, msg_type, **payload):
        return {
            'from': '995579370374', 'to': CUSTOMER, 'id': f'wamid.echo-{msg_type}',
            'to_user_id': 'CN.1118823507483378', 'timestamp': '1794819215',
            'type': msg_type, **payload,
        }

    def test_text_echo_is_broadcast_with_inbox_routing_keys(self):
        ws = self._post_echo(self._echo('text', text={'body': 'Do you have this type hair for braids?'}))
        msg = WhatsAppMessage.objects.get(message_id='wamid.echo-text')
        self.assertTrue(msg.is_echo)
        self.assertEqual(msg.source, 'business_app')
        self.assertEqual(msg.to_number, CUSTOMER)

        ws.assert_called_once()
        schema, data, conversation_id, assigned = ws.call_args[0]
        self.assertEqual(conversation_id, CUSTOMER)
        self.assertEqual(data['platform'], 'whatsapp')
        self.assertEqual(data['waba_id'], '1383134697093574')
        self.assertEqual(data['account_id'], '1383134697093574')
        self.assertEqual(data['chat_id'], f'wa_1383134697093574_{CUSTOMER}')
        self.assertEqual(data['to_number'], CUSTOMER)
        self.assertEqual(data['recipient_id'], CUSTOMER)
        self.assertEqual(data['contact_name'], 'Chloe Lee')
        self.assertTrue(data['is_from_business'])
        self.assertTrue(data['is_echo'])
        self.assertEqual(data['message_text'], 'Do you have this type hair for braids?')
        self.assertEqual(data['attachments'], [])

    def test_image_echo_keeps_the_media(self):
        ws = self._post_echo(self._echo('image', image={
            'mime_type': 'image/jpeg', 'id': '1461563759376358',
            'url': 'https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1461563759376358&source=webhook',
        }))
        msg = WhatsAppMessage.objects.get(message_id='wamid.echo-image')
        self.assertEqual(msg.message_type, 'image')
        self.assertEqual(msg.media_mime_type, 'image/jpeg')
        self.assertIn('mid=1461563759376358', msg.media_url)
        self.assertEqual(msg.attachments[0]['type'], 'image')
        self.assertEqual(msg.attachments[0]['media_id'], '1461563759376358')
        self.assertEqual(msg.attachments[0]['mime_type'], 'image/jpeg')

        data = ws.call_args[0][1]
        self.assertEqual(data['attachments'][0]['media_id'], '1461563759376358')
        self.assertEqual(data['message_type'], 'image')

    def test_image_echo_without_url_fetches_it_from_graph(self):
        graph = MagicMock(status_code=200)
        graph.json.return_value = {'url': 'https://lookaside.fbsbx.com/fresh'}
        with patch('social_integrations.views.requests.get', return_value=graph) as get:
            self._post_echo(self._echo('image', image={'mime_type': 'image/jpeg', 'id': '777', 'caption': 'this one'}))
        self.assertIn('/777', get.call_args[0][0])
        msg = WhatsAppMessage.objects.get(message_id='wamid.echo-image')
        self.assertEqual(msg.media_url, 'https://lookaside.fbsbx.com/fresh')
        self.assertEqual(msg.message_text, 'this one')
        self.assertEqual(msg.attachments[0]['url'], 'https://lookaside.fbsbx.com/fresh')

    def test_document_echo_keeps_filename(self):
        self._post_echo(self._echo('document', document={
            'mime_type': 'application/pdf', 'id': '555', 'filename': 'prices.pdf', 'url': 'https://lookaside.fbsbx.com/doc',
        }))
        msg = WhatsAppMessage.objects.get(message_id='wamid.echo-document')
        self.assertEqual(msg.message_text, 'prices.pdf')
        self.assertEqual(msg.attachments[0]['filename'], 'prices.pdf')
        self.assertEqual(msg.attachments[0]['type'], 'document')

    def test_duplicate_echo_is_ignored(self):
        self._post_echo(self._echo('text', text={'body': 'once'}))
        ws = self._post_echo(self._echo('text', text={'body': 'once'}))
        ws.assert_not_called()
        self.assertEqual(WhatsAppMessage.objects.filter(message_id='wamid.echo-text').count(), 1)
