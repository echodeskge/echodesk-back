"""
Sentry ECHODESK-BACKEND-6Z / 70: "Order not found for failed payment".

add_client_card starts a 0.01 GEL card-validation charge under the order id
``card_{client}_{rand}``; there is no Order for it. A declined validation
reached the BOG webhook's 'rejected' branch, which looked the id up as an
Order and logged an error. It is now acknowledged at info level, like the
'completed' branch handles the same prefix.
"""
from users.tests.conftest import EchoDeskTenantTestCase

BOG_WEBHOOK = '/api/ecommerce/payment-webhook/'
LOGGER = 'ecommerce_crm.views'


def _rejected(external_order_id):
    return {
        'event': 'order_payment',
        'body': {
            'order_id': 'bog-1',
            'external_order_id': external_order_id,
            'order_status': {'key': 'rejected'},
            'payment_detail': {'code': '116'},
        },
    }


class RejectedCardValidationWebhookTests(EchoDeskTenantTestCase):

    def _post(self, external_order_id):
        with self.assertLogs(LOGGER, level='INFO') as logs:
            resp = self.api_post(BOG_WEBHOOK, _rejected(external_order_id))
        return resp, logs

    def test_rejected_card_validation_is_acknowledged_without_error(self):
        resp, logs = self._post('card_42_0123456789ab')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data, {'status': 'received', 'action': 'card_validation_rejected'})
        self.assertFalse([r for r in logs.records if r.levelno >= 40], logs.output)
        self.assertTrue(any('Card validation rejected' in line for line in logs.output))

    def test_rejected_unknown_order_is_acknowledged_at_warning(self):
        resp, logs = self._post('ORD-does-not-exist')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {'status': 'received'})
        self.assertFalse([r for r in logs.records if r.levelno >= 40], logs.output)
        self.assertTrue(any('Order not found for failed payment' in line for line in logs.output))
