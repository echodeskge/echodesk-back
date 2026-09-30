"""
Per-order card provider choice (BOG / TBC / Flitt) on storefront checkout:
- payment_provider routes the charge to the chosen gateway and is stored on
  the order; omitting it keeps BOG.
- A provider the shop hasn't enabled or configured is a 400 — never a silent
  fallback — and no order is created.
- The Flitt webhook marks the order paid and queues the confirmation email.
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase
from ecommerce_crm.models import EcommerceSettings, Order, Product
from ecommerce_crm.payment_utils import PaymentProviderError, resolve_card_provider
from ecommerce_crm.tests.test_guest_checkout import GUEST_URL, GuestCheckoutTestMixin, _mock_bog
from tenants.payment_providers.base import PaymentResult

FLITT_WEBHOOK_URL = '/api/ecommerce/payment-webhook/flitt/'


def _mock_provider(name, url):
    """Patch the TBC/Flitt provider factory so no network call is made."""
    provider = MagicMock()
    provider.create_payment.side_effect = lambda **kw: PaymentResult(
        provider=name,
        provider_order_id=f'{name}-pay-1',
        external_order_id=kw['external_order_id'],
        amount=kw['amount'],
        payment_url=url,
        requires_redirect=True,
    )
    return provider, patch(
        'tenants.payment_providers.factory._get_provider_instance', return_value=provider
    )


class TestGuestPaymentProvider(GuestCheckoutTestMixin, EchoDeskTenantTestCase):
    def _flitt_settings(self, active=('bog', 'flitt')):
        return self._settings(
            active_payment_providers=list(active),
            flitt_merchant_id='1549901',
            flitt_password_encrypted=b'encrypted',
        )

    def test_no_provider_defaults_to_bog(self):
        self._flitt_settings()
        with _mock_bog():
            resp = self.api_post(GUEST_URL, self._payload(payment_method='card'))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['payment_provider'], 'bog')
        self.assertEqual(resp.data['bog_order_id'], 'bog-guest-1')
        order = Order.objects.get(order_number=resp.data['order_number'])
        self.assertEqual(order.payment_provider, 'bog')

    def test_flitt_charges_through_flitt(self):
        self._flitt_settings()
        provider, patcher = _mock_provider('flitt', 'https://pay.flitt.com/checkout/abc')
        with patcher:
            resp = self.api_post(GUEST_URL, self._payload(
                payment_method='card', payment_provider='flitt',
                **self._quickshipper_fields(),
            ))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['payment_url'], 'https://pay.flitt.com/checkout/abc')
        self.assertEqual(resp.data['payment_provider'], 'flitt')

        kwargs = provider.create_payment.call_args.kwargs
        self.assertTrue(kwargs['callback_url'].endswith('/api/ecommerce/payment-webhook/flitt/'))
        order = Order.objects.get(order_number=resp.data['order_number'])
        self.assertEqual(kwargs['external_order_id'], order.order_number)
        self.assertEqual(order.payment_provider, 'flitt')
        self.assertEqual(order.payment_method, 'card')
        self.assertIsNone(order.bog_order_id)
        self.assertEqual(order.payment_metadata['provider_order_id'], 'flitt-pay-1')
        # Courier selection survives the payment metadata write.
        self.assertEqual(order.payment_metadata['quickshipper_quote']['provider_id'], 7)

    def test_payment_method_alias_routes_to_flitt(self):
        # The exact request from order #43: payment_method="flitt".
        self._flitt_settings()
        provider, patcher = _mock_provider('flitt', 'https://pay.flitt.com/checkout/abc')
        with patcher:
            resp = self.api_post(GUEST_URL, self._payload(payment_method='flitt'))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['payment_provider'], 'flitt')
        provider.create_payment.assert_called_once()

    def test_inactive_provider_rejected_without_creating_order(self):
        self._flitt_settings(active=('bog', 'flitt'))
        before = Product.objects.get(pk=self.product.pk).quantity
        with _mock_bog() as bog:
            resp = self.api_post(GUEST_URL, self._payload(payment_method='card', payment_provider='tbc'))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('not enabled', resp.data['error'])
        self.assertFalse(Order.objects.exists())
        self.assertEqual(Product.objects.get(pk=self.product.pk).quantity, before)
        bog.return_value.create_payment.assert_not_called()

    def test_unconfigured_provider_rejected(self):
        self._settings(active_payment_providers=['bog', 'tbc'])  # no TBC credentials
        resp = self.api_post(GUEST_URL, self._payload(payment_method='card', payment_provider='tbc'))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('not configured', resp.data['error'])
        self.assertFalse(Order.objects.exists())

    def test_cod_ignores_provider(self):
        self._settings(allow_pickup=True, active_payment_providers=['bog'])
        with patch('ecommerce_crm.tasks.send_order_email.delay'):
            resp = self.api_post(GUEST_URL, self._payload(
                payment_method='cash_on_delivery', delivery_method='pickup', payment_provider='tbc',
            ))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        order = Order.objects.get(order_number=resp.data['order_number'])
        self.assertEqual(order.payment_provider, '')


class TestResolveCardProvider(EchoDeskTenantTestCase):
    def _settings(self, **kw):
        return EcommerceSettings(tenant=self.tenant, **kw)

    def test_default_is_bog(self):
        self.assertEqual(resolve_card_provider({'payment_method': 'card'}, self._settings()), 'bog')

    def test_unknown_provider(self):
        with self.assertRaises(PaymentProviderError):
            resolve_card_provider({'payment_provider': 'paddle'}, self._settings())

    def test_empty_active_list_allows_configured_provider(self):
        s = self._settings(tbc_client_id='id', tbc_client_secret_encrypted=b'x', tbc_api_key='k')
        self.assertEqual(resolve_card_provider({'payment_provider': 'TBC'}, s), 'tbc')


class TestFlittWebhookConfirmation(GuestCheckoutTestMixin, EchoDeskTenantTestCase):
    def test_approved_marks_paid_and_sends_confirmation(self):
        self._settings()
        from ecommerce_crm.models import ClientAddress, EcommerceClient
        client = EcommerceClient.objects.create(
            email='flitt-buyer@test.com', first_name='F', last_name='B', phone_number='+995555000000',
        )
        address = ClientAddress.objects.create(client=client, address='1 Rustaveli Ave', city='Tbilisi')
        order = Order.objects.create(
            order_number='ORD-FLITT-1', client=client, delivery_address=address,
            total_amount=Decimal('40.00'),
            payment_method='card', payment_provider='flitt',
        )
        # A genuinely signed callback — exercises the real signature check.
        from tenants.payment_providers.flitt import _compute_signature
        body = {'order_id': order.order_number, 'order_status': 'approved', 'payment_id': '999'}
        body['signature'] = _compute_signature('flitt-secret', body)
        with patch('tenants.payment_providers.flitt.FlittPaymentProvider._get_credentials',
                   return_value={'merchant_id': '1549901', 'password': 'flitt-secret'}), \
                patch('ecommerce_crm.tasks.send_order_email.delay') as email, \
                patch('ecommerce_crm.tasks.book_quickshipper_courier.delay'), \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api_post(FLITT_WEBHOOK_URL, body)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
        email.assert_called_once_with(self.tenant.schema_name, order.id, 'confirmation')

    def test_forged_signature_rejected(self):
        self._settings()
        with patch('tenants.payment_providers.flitt.FlittPaymentProvider._get_credentials',
                   return_value={'merchant_id': '1549901', 'password': 'flitt-secret'}):
            resp = self.api_post(FLITT_WEBHOOK_URL, {
                'order_id': 'ORD-X', 'order_status': 'approved', 'payment_id': '1', 'signature': 'forged',
            })
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
