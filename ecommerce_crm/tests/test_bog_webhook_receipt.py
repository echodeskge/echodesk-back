"""The BOG e-commerce callback, verified against BOG's real receipt layout.

Before 2026-11-13 the receipt re-query returned ``response_code=None`` for
every order (fields read from the wrong level), so a genuine "completed"
callback was answered with "Payment not confirmed by BOG" and the order
stayed pending until a manager pressed Mark as paid — which sends no emails.
"""
import base64
import json
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.test import override_settings

from ecommerce_crm.tests.test_p4_admin_parity import P4Base
from tenants.bog_payment import BOGPaymentService
from tenants.tests.test_bog_callback import PAID_RECEIPT, REJECTED_RECEIPT

BOG_WEBHOOK = '/api/ecommerce/payment-webhook/'


def _receipt(payload, order_number):
    data = json.loads(json.dumps(payload))
    data['external_order_id'] = order_number
    resp = MagicMock(status_code=200)
    resp.json.return_value = data
    return resp


def _callback(order_number, code='100', key='completed'):
    return {
        'event': 'order_payment',
        'zoned_request_time': '2026-11-12T11:58:00.000000Z',
        'body': {
            'order_id': 'bog-610985', 'external_order_id': order_number,
            'order_status': {'key': key}, 'payment_detail': {'code': code},
        },
    }


class BogWebhookReceiptTests(P4Base):

    def _pending_bog_order(self):
        return self._make_order_with_items(
            payment_method='card', payment_provider='bog',
            payment_status='pending', status='pending',
        )

    def _post(self, order, body=None, **extra):
        with patch.object(BOGPaymentService, 'is_configured', return_value=True), \
                patch.object(BOGPaymentService, '_get_access_token', return_value='tok'), \
                patch('tenants.bog_payment.requests.get',
                      return_value=_receipt(PAID_RECEIPT, order.order_number)) as get, \
                patch('ecommerce_crm.tasks.send_order_email.delay') as email, \
                patch('ecommerce_crm.tasks.book_quickshipper_courier.delay'), \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api_post(BOG_WEBHOOK, body or _callback(order.order_number), **extra)
        return resp, get, email

    def test_completed_callback_marks_the_order_paid_and_queues_emails(self):
        order = self._pending_bog_order()
        resp, get, email = self._post(order)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data.get('action'), 'payment_completed')
        self.assertIn('/receipt/bog-610985', get.call_args[0][0])
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
        self.assertEqual(order.status, 'confirmed')
        self.assertIsNotNone(order.paid_at)
        self.assertEqual(order.payment_metadata['transaction_id'], 'TX123456')
        self.assertEqual(order.payment_metadata['response_code'], '100')
        email.assert_called_once()
        self.assertEqual(email.call_args[0][1:], (order.id, 'confirmation'))

    def test_integer_code_in_callback_is_accepted(self):
        order = self._pending_bog_order()
        resp, _, email = self._post(order, body=_callback(order.order_number, code=100))
        self.assertEqual(resp.status_code, 200, resp.data)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
        email.assert_called_once()

    def test_bog_rejected_receipt_keeps_the_order_pending(self):
        order = self._pending_bog_order()
        with patch.object(BOGPaymentService, 'is_configured', return_value=True), \
                patch.object(BOGPaymentService, '_get_access_token', return_value='tok'), \
                patch('tenants.bog_payment.requests.get',
                      return_value=_receipt(REJECTED_RECEIPT, order.order_number)), \
                patch('ecommerce_crm.tasks.send_order_email.delay') as email:
            resp = self.api_post(BOG_WEBHOOK, _callback(order.order_number))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'ignored')
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'pending')
        email.assert_not_called()


class BogWebhookSignatureTests(P4Base):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_pem = cls.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    def _sign(self, raw):
        return base64.b64encode(self.private.sign(raw, padding.PKCS1v15(), hashes.SHA256())).decode()

    def _post_raw(self, order, raw, signature):
        with override_settings(BOG_CALLBACK_PUBLIC_KEY=self.public_pem), \
                patch.object(BOGPaymentService, 'is_configured', return_value=True), \
                patch.object(BOGPaymentService, '_get_access_token', return_value='tok'), \
                patch('tenants.bog_payment.requests.get',
                      return_value=_receipt(PAID_RECEIPT, order.order_number)), \
                patch('ecommerce_crm.tasks.send_order_email.delay'), \
                patch('ecommerce_crm.tasks.book_quickshipper_courier.delay'), \
                self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                BOG_WEBHOOK, raw, content_type='application/json',
                HTTP_HOST='tenant.test.com', HTTP_CALLBACK_SIGNATURE=signature,
            )

    def test_valid_signature_is_accepted(self):
        order = self._make_order_with_items(payment_method='card', payment_provider='bog',
                                            payment_status='pending', status='pending')
        raw = json.dumps(_callback(order.order_number)).encode()
        resp = self._post_raw(order, raw, self._sign(raw))
        self.assertEqual(resp.status_code, 200, resp.content)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')

    def test_bad_signature_is_rejected_before_anything_changes(self):
        order = self._make_order_with_items(payment_method='card', payment_provider='bog',
                                            payment_status='pending', status='pending')
        raw = json.dumps(_callback(order.order_number)).encode()
        resp = self._post_raw(order, raw, self._sign(raw + b'x'))
        self.assertEqual(resp.status_code, 400, resp.content)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'pending')
