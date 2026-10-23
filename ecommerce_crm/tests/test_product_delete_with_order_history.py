"""
nitchiani (2026-10-21) could not delete an old draft product: a refunded test
order still referenced it and OrderItem.product was PROTECT. Order lines keep
their own snapshot, so finished orders (delivered / cancelled / refunded) no
longer block the delete: the line loses its product link (SET_NULL) and keeps
its name, price and a readable copy of the variant. Orders still in progress
keep blocking it, and the 409 names them.
"""
from decimal import Decimal

from rest_framework import status

from ecommerce_crm.models import (
    AttributeDefinition, Order, OrderItem, Product, ProductVariant, ProductVariantAttributeValue,
)
from ecommerce_crm.tests.base import EcommerceTenantTestCase
from ecommerce_crm.tests.test_order_views import OrderTestMixin

PROD_URL = '/api/ecommerce/admin/products/'
VARIANT_URL = '/api/ecommerce/admin/variants/'
ORDER_URL = '/api/ecommerce/admin/orders/'


class TestProductDeleteWithOrderHistory(OrderTestMixin, EcommerceTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='delete-history-admin@test.com')
        self.colour = AttributeDefinition.objects.create(
            name={'en': 'Colour', 'ka': 'ფერი'}, key='colour', attribute_type='select',
            options=[{'value': 'blonde', 'label': {'en': 'Blonde', 'ka': 'ქერა'}}],
        )

    def _variant(self, product):
        variant = ProductVariant.objects.create(
            product=product, sku=f'OMBRE-{product.pk}', name={'en': 'Blonde', 'ka': 'ქერა'},
            price=Decimal('80.00'), quantity=3,
        )
        ProductVariantAttributeValue.objects.create(variant=variant, attribute=self.colour, value_json='blonde')
        return variant

    def _line(self, product, status_, variant=None):
        order = self._make_order(status=status_)
        item = OrderItem.objects.create(
            order=order, product=product, variant=variant,
            product_name={'en': 'Ombre Blonde'}, quantity=1, price=Decimal('80.00'),
        )
        return order, item

    def test_finished_orders_do_not_block_delete_and_keep_their_lines(self):
        product = self._make_product(status='draft')
        variant = self._variant(product)
        refunded, refunded_item = self._line(product, 'refunded', variant)
        cancelled, _ = self._line(product, 'cancelled')
        delivered, _ = self._line(product, 'delivered', variant)

        resp = self.api_delete(f'{PROD_URL}{product.pk}/', user=self.admin)

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT, resp.content)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())
        self.assertEqual(Order.objects.filter(pk__in=[refunded.pk, cancelled.pk, delivered.pk]).count(), 3)
        refunded_item.refresh_from_db()
        self.assertIsNone(refunded_item.product_id)
        self.assertIsNone(refunded_item.variant_id)
        self.assertEqual(refunded_item.product_name, {'en': 'Ombre Blonde'})
        self.assertEqual(refunded_item.price, Decimal('80.00'))

        # The order page still renders the line, with the variant it sold.
        resp = self.api_get(f'{ORDER_URL}{delivered.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        line = resp.data['items'][0]
        self.assertIsNone(line['product'])
        self.assertIsNone(line['product_image'])
        self.assertEqual(line['product_name'], {'en': 'Ombre Blonde'})
        self.assertEqual(line['variant_details']['name'], {'en': 'Blonde', 'ka': 'ქერა'})
        self.assertEqual(line['variant_details']['attributes'], [{
            'key': 'colour', 'name': {'en': 'Colour', 'ka': 'ფერი'},
            'value': {'en': 'Blonde', 'ka': 'ქერა'},
        }])
        self.assertEqual(self.api_get(ORDER_URL, user=self.admin).status_code, status.HTTP_200_OK)

    def test_open_order_blocks_delete_and_is_named(self):
        product = self._make_product()
        self._line(product, 'refunded')
        open_order, _ = self._line(product, 'confirmed')

        resp = self.api_delete(f'{PROD_URL}{product.pk}/', user=self.admin)

        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT, resp.content)
        self.assertEqual(resp.data['orders'], [open_order.order_number])
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

    def test_variant_delete_follows_the_same_rule(self):
        product = self._make_product()
        variant = self._variant(product)
        open_order, _ = self._line(product, 'shipped', variant)

        resp = self.api_delete(f'{VARIANT_URL}{variant.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT, resp.content)
        self.assertEqual(resp.data['orders'], [open_order.order_number])

        Order.objects.filter(pk=open_order.pk).update(status='delivered')
        resp = self.api_delete(f'{VARIANT_URL}{variant.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT, resp.content)
        item = OrderItem.objects.get(order=open_order)
        self.assertIsNone(item.variant_id)
        self.assertEqual(item.product_id, product.pk)
        self.assertEqual(item.variant_snapshot['sku'], f'OMBRE-{product.pk}')

    def test_cancel_and_refund_skip_restock_for_deleted_products(self):
        product = self._make_product(status='draft')
        order, item = self._line(product, 'delivered')
        product.delete()
        item.refresh_from_db()
        self.assertIsNone(item.product)
        self.assertIn('(deleted product)', str(item))
        order.status = 'confirmed'
        order.save(update_fields=['status'])
        self.assertTrue(order.cancel(reason='test'))
