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

from ecommerce_crm.tests.base import EcommerceTenantTestCase as EchoDeskTenantTestCase
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


class TestGuestContactDetails(GuestCheckoutTestMixin, EchoDeskTenantTestCase):
    """A guest order under an email someone used before keeps the contact
    details typed at checkout instead of showing the earlier customer's."""

    def setUp(self):
        super().setUp()
        self._settings(allow_pickup=True)
        self.earlier = EcommerceClient.objects.create(
            email='shared@test.com', first_name='Ana', last_name='First',
            phone_number='+995512343434', password='', is_verified=True,
        )

    def _place(self):
        payload = self._payload(
            email='Shared@test.com', first_name='Nini', last_name='Second',
            phone='+995571575151', payment_method='cash_on_delivery',
            delivery_method='pickup',
        )
        with patch('ecommerce_crm.tasks.send_order_email.delay'):
            resp = self.api_post(GUEST_URL, payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return Order.objects.get(order_number=resp.data['order_number'])

    def test_order_keeps_typed_contact(self):
        order = self._place()
        self.assertEqual(order.client_id, self.earlier.id)
        self.assertEqual(order.contact_name, 'Nini Second')
        self.assertEqual(order.contact_email, 'shared@test.com')
        self.assertEqual(order.contact_phone, '+995571575151')

    def test_admin_list_and_detail_show_typed_contact(self):
        order = self._place()
        resp = self.api_get('/api/ecommerce/admin/orders/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        rows = resp.data['results'] if isinstance(resp.data, dict) else resp.data
        row = next(r for r in rows if r['id'] == order.id)
        self.assertEqual(row['client_name'], 'Nini Second')

        resp = self.api_get(f'/api/ecommerce/admin/orders/{order.id}/', user=self.admin)
        self.assertEqual(resp.data['client_details']['full_name'], 'Nini Second')
        self.assertEqual(resp.data['client_details']['phone_number'], '+995571575151')

    def test_older_orders_fall_back_to_client(self):
        order = self._place()
        Order.objects.filter(id=order.id).update(
            customer_first_name='', customer_last_name='', customer_email='', customer_phone='',
        )
        order.refresh_from_db()
        self.assertEqual(order.contact_name, 'Ana First')
        self.assertEqual(order.contact_phone, '+995512343434')

    def test_courier_gets_typed_contact(self):
        from ecommerce_crm.tasks import book_quickshipper_courier

        EcommerceSettings.objects.filter(tenant=self.tenant).update(quickshipper_enabled=True)
        payload = self._payload(
            email='shared@test.com', first_name='Nini', last_name='Second',
            phone='+995571575151', payment_method='card',
            **self._quickshipper_fields(),
        )
        with _mock_bog():
            resp = self.api_post(GUEST_URL, payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        order = Order.objects.get(order_number=resp.data['order_number'])

        courier = MagicMock()
        courier.create_order.return_value = {'orderId': 555}
        with patch('ecommerce_crm.services.quickshipper.client_from_settings', return_value=courier):
            book_quickshipper_courier.run(self.tenant.schema_name, order.id)

        drop_off = courier.create_order.call_args[0][0]['dropOffInfo']
        self.assertEqual(drop_off['name'], 'Nini Second')
        self.assertEqual(drop_off['phone'], '+995571575151')
        order.refresh_from_db()
        self.assertEqual(order.tracking_number, '555')

    def _booked_order(self):
        EcommerceSettings.objects.filter(tenant=self.tenant).update(quickshipper_enabled=True)
        payload = self._payload(
            email='shared@test.com', first_name='Nini', last_name='Second',
            phone='+995571575151', payment_method='card',
            **self._quickshipper_fields(),
        )
        with _mock_bog():
            resp = self.api_post(GUEST_URL, payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return Order.objects.get(order_number=resp.data['order_number'])

    def test_courier_order_is_confirmed_after_booking(self):
        """Quickshipper keeps a new order as Draft until /v1/Order/confirm
        is called (ticket #242), so booking must send both requests."""
        from ecommerce_crm.tasks import book_quickshipper_courier

        order = self._booked_order()
        courier = MagicMock()
        courier.create_order.return_value = {'orderId': 555, 'providerName': 'Deliverers'}
        courier.confirm_order.return_value = {'orderId': 555}
        with patch('ecommerce_crm.services.quickshipper.client_from_settings', return_value=courier):
            book_quickshipper_courier.run(self.tenant.schema_name, order.id)

        courier.confirm_order.assert_called_once_with(555)
        order.refresh_from_db()
        self.assertTrue(order.payment_metadata['quickshipper']['confirmed'])

        # Running again (e.g. a second paid webhook) neither re-books nor re-confirms.
        with patch('ecommerce_crm.services.quickshipper.client_from_settings', return_value=courier):
            book_quickshipper_courier.run(self.tenant.schema_name, order.id)
        courier.create_order.assert_called_once()
        courier.confirm_order.assert_called_once()

    def test_failed_confirm_is_finished_on_retry_without_rebooking(self):
        from ecommerce_crm.tasks import book_quickshipper_courier
        from ecommerce_crm.services.quickshipper import QuickshipperError

        order = self._booked_order()
        courier = MagicMock()
        courier.create_order.return_value = {'orderId': 777}
        courier.confirm_order.side_effect = QuickshipperError('temporary')
        with patch('ecommerce_crm.services.quickshipper.client_from_settings', return_value=courier), \
                patch.object(book_quickshipper_courier, 'retry', side_effect=RuntimeError('retry')):
            with self.assertRaises(RuntimeError):
                book_quickshipper_courier.run(self.tenant.schema_name, order.id)
        order.refresh_from_db()
        self.assertEqual(order.tracking_number, '777')
        self.assertFalse(order.payment_metadata['quickshipper'].get('confirmed'))

        courier.confirm_order.side_effect = None
        courier.confirm_order.return_value = {'orderId': 777}
        with patch('ecommerce_crm.services.quickshipper.client_from_settings', return_value=courier):
            book_quickshipper_courier.run(self.tenant.schema_name, order.id)
        courier.create_order.assert_called_once()
        self.assertEqual(courier.confirm_order.call_count, 2)
        order.refresh_from_db()
        self.assertTrue(order.payment_metadata['quickshipper']['confirmed'])

    def test_retried_courier_failure_is_a_warning_not_an_error(self):
        """A failed attempt that will be retried ("Not enough balance" until
        the merchant tops up) must not page as an error on every retry
        (ECHODESK-BACKEND-78); only the final attempt is an error."""
        from ecommerce_crm.tasks import book_quickshipper_courier
        from ecommerce_crm.services.quickshipper import QuickshipperError

        order = self._booked_order()
        courier = MagicMock()
        courier.create_order.side_effect = QuickshipperError('Not enough balance')
        with patch('ecommerce_crm.services.quickshipper.client_from_settings', return_value=courier), \
                self.assertLogs('ecommerce_crm.tasks', level='WARNING') as logs, \
                self.assertRaises(QuickshipperError):
            # Called directly (no worker): retry() re-raises the original error.
            book_quickshipper_courier.run(self.tenant.schema_name, order.id)
        self.assertEqual(
            [r.levelname for r in logs.records if 'book_quickshipper_courier failed' in r.getMessage()],
            ['WARNING'],
        )


class QuickshipperConfirmClientTest(EchoDeskTenantTestCase):
    def test_confirm_posts_order_id(self):
        from ecommerce_crm.services.quickshipper import QuickshipperClient

        session = MagicMock()
        token = MagicMock(status_code=200, content=b'x')
        token.json.return_value = {'access_token': 'jwt', 'expires_in': 3600}
        session.post.return_value = token
        ok = MagicMock(status_code=200, content=b'x')
        ok.json.return_value = {'success': True, 'orderId': 42}
        session.request.return_value = ok

        QuickshipperClient('key', session=session).confirm_order(42)
        method, url = session.request.call_args[0]
        self.assertEqual(method, 'POST')
        self.assertTrue(url.endswith('/v1/Order/confirm'))
        self.assertEqual(session.request.call_args[1]['json'], {'orderId': 42})
