"""
Selling a variant decrements both the variant's stock and the product total;
cancel/refund put both back (#240: the product total dropped to 7 but the
sold black variant still showed 2).
"""
from decimal import Decimal
from unittest.mock import patch

from rest_framework import status

from ecommerce_crm.models import EcommerceSettings, Order, Product, ProductVariant
from ecommerce_crm.tests.base import EcommerceTenantTestCase
from ecommerce_crm.tests.test_guest_checkout import GUEST_URL, GuestCheckoutTestMixin


class TestVariantStock(GuestCheckoutTestMixin, EcommerceTenantTestCase):

    def setUp(self):
        super().setUp()
        self.product.quantity = 3
        self.product.save(update_fields=['quantity'])
        self.black = ProductVariant.objects.create(
            product=self.product, sku='DURAG-BLACK', name={'en': 'Black'}, quantity=2,
        )
        self.red = ProductVariant.objects.create(
            product=self.product, sku='DURAG-RED', name={'en': 'Red'}, quantity=1,
        )
        EcommerceSettings.objects.create(tenant=self.tenant, tax_rate=Decimal('0'), allow_pickup=True)

    def _buy(self, variant, quantity=1):
        payload = self._payload(
            items=[{'product_id': self.product.id, 'variant_id': variant.id, 'quantity': quantity}],
            delivery_method='pickup',
            payment_method='cash_on_delivery',
        )
        with patch('ecommerce_crm.tasks.send_order_email.delay'):
            return self.api_post(GUEST_URL, payload)

    def test_variant_sale_decrements_variant_and_product(self):
        resp = self._buy(self.black)
        self.assertIn(resp.status_code, (status.HTTP_201_CREATED,), resp.data)
        self.product.refresh_from_db()
        self.black.refresh_from_db()
        self.red.refresh_from_db()
        self.assertEqual(self.product.quantity, 2)
        self.assertEqual(self.black.quantity, 1)
        self.assertEqual(self.red.quantity, 1)

    def test_variant_oversell_rejected(self):
        resp = self._buy(self.red, quantity=2)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.product.refresh_from_db()
        self.red.refresh_from_db()
        self.assertEqual(self.product.quantity, 3)
        self.assertEqual(self.red.quantity, 1)

    def test_cancel_restores_variant_and_product(self):
        resp = self._buy(self.black)
        self.assertIn(resp.status_code, (status.HTTP_201_CREATED,), resp.data)
        order = Order.objects.latest('id')
        self.assertTrue(order.cancel(reason='test'))
        self.product.refresh_from_db()
        self.black.refresh_from_db()
        self.assertEqual(self.product.quantity, 3)
        self.assertEqual(self.black.quantity, 2)
