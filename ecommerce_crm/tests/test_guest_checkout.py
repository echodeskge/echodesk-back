"""
Tests for the public guest-checkout endpoint's courier/pickup handling:
- Quickshipper courier selection persists to Order.payment_metadata and
  drives shipping_cost, and survives the BOG card-payment metadata write.
- Pickup is always free — a stale quote or shipping method never charges.
- Pickup is gated by the tenant's allow_pickup setting.
- Requests without the new fields still work (backward compatible).
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase
from ecommerce_crm.models import (
    ClientAddress,
    EcommerceClient,
    EcommerceSettings,
    Order,
    Product,
)

GUEST_URL = '/api/ecommerce/client/guest-checkout/'


class GuestCheckoutTestMixin:
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='guest-co-admin@test.com')
        self.product = Product.objects.create(
            sku='GUEST-PROD-1',
            name={'en': 'Guest Product'},
            price=Decimal('40.00'),
            status='active',
            quantity=50,
            track_inventory=True,
            low_stock_threshold=5,
            created_by=self.admin,
        )

    def _settings(self, **kw):
        defaults = {'tenant': self.tenant, 'tax_rate': Decimal('0')}
        defaults.update(kw)
        return EcommerceSettings.objects.create(**defaults)

    def _payload(self, **overrides):
        payload = {
            'email': 'guest-buyer@test.com',
            'first_name': 'Guest',
            'last_name': 'Buyer',
            'phone': '+995555001122',
            'address': {
                'address': '1 Rustaveli Ave',
                'city': 'Tbilisi',
                'latitude': 41.7151,
                'longitude': 44.8271,
            },
            'items': [{'product_id': self.product.id, 'quantity': 1}],
        }
        payload.update(overrides)
        return payload

    def _quickshipper_fields(self):
        return {
            'quickshipper_provider_id': 7,
            'quickshipper_provider_fee_id': 'asap',
            'quickshipper_parcel_dimensions_id': 3,
            'quickshipper_price': '9.50',
            'quickshipper_provider_name': 'Wolt',
        }


def _mock_bog():
    """Patch BOGPaymentService so the card path doesn't hit the network."""
    instance = MagicMock()
    instance.create_payment.return_value = {
        'order_id': 'bog-guest-1',
        'payment_url': 'https://pay.bog.test/guest-1',
    }
    return patch('tenants.bog_payment.BOGPaymentService', return_value=instance)


class TestGuestCourierSelection(GuestCheckoutTestMixin, EchoDeskTenantTestCase):
    def test_courier_selection_persists_and_survives_payment(self):
        payload = self._payload(
            payment_method='card',
            delivery_method='courier',
            **self._quickshipper_fields(),
        )
        with _mock_bog():
            resp = self.api_post(GUEST_URL, payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

        order = Order.objects.get(order_number=resp.data['order_number'])
        meta = order.payment_metadata
        # Quote preserved despite the BOG payment_result write.
        self.assertIn('quickshipper_quote', meta)
        self.assertEqual(meta['quickshipper_quote']['provider_id'], 7)
        self.assertEqual(meta['quickshipper_quote']['provider_fee_id'], 'asap')
        self.assertEqual(meta['quickshipper_quote']['parcel_dimensions_id'], 3)
        self.assertEqual(meta['delivery_method'], 'courier')
        # BOG fields are still merged in.
        self.assertEqual(meta['order_id'], 'bog-guest-1')
        # Shipping cost tracks the quoted courier price.
        self.assertEqual(order.shipping_cost, Decimal('9.50'))

    def test_courier_address_latlng_saved(self):
        payload = self._payload(payment_method='card', delivery_method='courier',
                                **self._quickshipper_fields())
        with _mock_bog():
            resp = self.api_post(GUEST_URL, payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        order = Order.objects.get(order_number=resp.data['order_number'])
        addr = order.delivery_address
        self.assertEqual(float(addr.latitude), 41.7151)
        self.assertEqual(float(addr.longitude), 44.8271)


class TestGuestPickup(GuestCheckoutTestMixin, EchoDeskTenantTestCase):
    def test_pickup_is_free_even_with_stale_quote(self):
        self._settings(allow_pickup=True)
        # COD is allowed for pickup; include a stale quote that must be ignored.
        payload = self._payload(
            payment_method='cash_on_delivery',
            delivery_method='pickup',
            **self._quickshipper_fields(),
        )
        with patch('ecommerce_crm.tasks.send_order_email.delay'):
            resp = self.api_post(GUEST_URL, payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

        order = Order.objects.get(order_number=resp.data['order_number'])
        self.assertEqual(order.shipping_cost, Decimal('0'))
        self.assertEqual(order.payment_metadata['delivery_method'], 'pickup')
        # The stale courier quote must NOT have been applied.
        self.assertNotIn('quickshipper_quote', order.payment_metadata)

    def test_pickup_rejected_when_not_allowed(self):
        self._settings(allow_pickup=False)
        payload = self._payload(
            payment_method='cash_on_delivery', delivery_method='pickup',
        )
        resp = self.api_post(GUEST_URL, payload)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestGuestBackwardCompat(GuestCheckoutTestMixin, EchoDeskTenantTestCase):
    def test_request_without_new_fields_still_creates_order(self):
        # No delivery_method / quickshipper fields — defaults to courier, so
        # pay by card (COD+courier is rejected by existing business rule).
        payload = self._payload(payment_method='card')
        with _mock_bog():
            resp = self.api_post(GUEST_URL, payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

        order = Order.objects.get(order_number=resp.data['order_number'])
        self.assertEqual(order.payment_metadata.get('delivery_method'), 'courier')
        self.assertNotIn('quickshipper_quote', order.payment_metadata)
        self.assertEqual(order.shipping_cost, Decimal('0'))

    def test_cod_courier_still_rejected(self):
        payload = self._payload(
            payment_method='cash_on_delivery', delivery_method='courier',
        )
        resp = self.api_post(GUEST_URL, payload)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
