"""BOG payment details and callback signature, per
https://api.bog.ge/docs/en/payments/standard-process/get-payment-details and
.../callback.

Regression (2026-11-13): ``check_payment_status`` read ``code`` and
``transaction_id`` from the top level of the receipt, where BOG never puts
them, so ``response_code`` was always None and both webhooks "could not
confirm" every real card payment since the re-query was added (July 2026).
"""
import base64
import json
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.test import SimpleTestCase, override_settings

from tenants.bog_payment import BOGPaymentService

# Trimmed from a real receipt of a paid order (GET /payments/v1/receipt/<id>).
PAID_RECEIPT = {
    'order_id': '610985aa-0000-4000-8000-000000000000',
    'industry': 'ecommerce',
    'capture': 'automatic',
    'external_order_id': 'ORD-20261112-R3E7ZW',
    'order_status': {'key': 'completed', 'value': 'Performed'},
    'purchase_units': {
        'request_amount': '79.41', 'transfer_amount': '79.41',
        'refund_amount': None, 'currency_code': 'GEL', 'items': [],
    },
    'payment_detail': {
        'transfer_method': {'key': 'card', 'value': 'Card'},
        'transaction_id': 'TX123456', 'auth_code': '012345', 'card_type': 'visa',
        'code': '100', 'code_description': 'Successful payment',
    },
    'reject_reason': None,
}

REJECTED_RECEIPT = {
    'order_id': 'a3fbd4aa-0000-4000-8000-000000000000',
    'external_order_id': 'ORD-20261111-X99MD5',
    'order_status': {'key': 'rejected', 'value': 'Rejected'},
    'purchase_units': {'request_amount': '30.99', 'transfer_amount': None, 'currency_code': 'GEL'},
    'payment_detail': {'transfer_method': None, 'transaction_id': None, 'code': None, 'code_description': None},
    'reject_reason': 'expiration',
}


def _service():
    svc = BOGPaymentService()
    svc.client_id, svc.client_secret = 'cid', 'secret'
    return svc


def _receipt_response(payload):
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    return resp


class ReceiptParsingTests(SimpleTestCase):

    def test_paid_receipt_yields_code_100_and_transaction(self):
        with patch.object(BOGPaymentService, '_get_access_token', return_value='tok'), \
                patch('tenants.bog_payment.requests.get', return_value=_receipt_response(PAID_RECEIPT)):
            res = _service().check_payment_status('610985aa')
        self.assertEqual(res['status'], 'paid')
        self.assertEqual(res['bog_status'], 'completed')
        self.assertEqual(res['response_code'], '100')
        self.assertEqual(res['transaction_id'], 'TX123456')
        self.assertEqual(res['amount'], '79.41')
        self.assertEqual(res['currency'], 'GEL')
        self.assertEqual(res['transfer_method'], 'card')
        self.assertEqual(res['external_order_id'], 'ORD-20261112-R3E7ZW')
        # What both webhooks check before trusting a callback.
        self.assertTrue(res['bog_status'] == 'completed' and str(res['response_code']) == '100')

    def test_integer_code_is_normalised(self):
        payload = json.loads(json.dumps(PAID_RECEIPT))
        payload['payment_detail']['code'] = 100
        with patch.object(BOGPaymentService, '_get_access_token', return_value='tok'), \
                patch('tenants.bog_payment.requests.get', return_value=_receipt_response(payload)):
            res = _service().check_payment_status('x')
        self.assertEqual(res['response_code'], '100')

    def test_rejected_receipt(self):
        with patch.object(BOGPaymentService, '_get_access_token', return_value='tok'), \
                patch('tenants.bog_payment.requests.get', return_value=_receipt_response(REJECTED_RECEIPT)):
            res = _service().check_payment_status('a3fbd4aa')
        self.assertEqual(res['status'], 'failed')
        self.assertEqual(res['bog_status'], 'rejected')
        self.assertIsNone(res['response_code'])
        self.assertIsNone(res['transaction_id'])
        self.assertEqual(res['reject_reason'], 'expiration')


class CallbackSignatureTests(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_pem = cls.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        cls.body = json.dumps({'event': 'order_payment', 'body': {'order_id': '1'}}).encode()
        cls.signature = base64.b64encode(
            cls.private.sign(cls.body, padding.PKCS1v15(), hashes.SHA256())
        ).decode()

    def test_valid_signature(self):
        self.assertIs(_service().verify_callback_signature(self.body, self.signature, self.public_pem), True)

    def test_tampered_body_fails(self):
        self.assertIs(_service().verify_callback_signature(self.body + b' ', self.signature, self.public_pem), False)

    def test_garbage_signature_fails(self):
        self.assertIs(_service().verify_callback_signature(self.body, 'not-base64!!', self.public_pem), False)

    def test_missing_signature_is_unverifiable_not_rejected(self):
        self.assertIsNone(_service().verify_callback_signature(self.body, '', self.public_pem))
        self.assertTrue(_service().verify_webhook_signature(self.body, '', self.public_pem))

    def test_default_key_comes_from_settings(self):
        with override_settings(BOG_CALLBACK_PUBLIC_KEY=self.public_pem):
            self.assertIs(_service().verify_callback_signature(self.body, self.signature), True)

    def test_shipped_key_is_a_valid_rsa_public_key(self):
        from django.conf import settings
        key = serialization.load_pem_public_key(settings.BOG_CALLBACK_PUBLIC_KEY.encode())
        self.assertEqual(key.key_size, 2048)
