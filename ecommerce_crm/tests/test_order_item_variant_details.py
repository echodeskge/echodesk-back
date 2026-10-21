"""
Order lines expose readable variant details (name, SKU, attribute labels) so
the dashboard shows "ყავისფერი · ფერი: Brown" instead of "Variant ID: 3".
"""
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status

from ecommerce_crm.models import (
    AttributeDefinition, OrderItem, ProductVariant, ProductVariantAttributeValue,
)
from ecommerce_crm.tests.base import EcommerceTenantTestCase
from ecommerce_crm.tests.test_order_views import OrderTestMixin

ORDER_URL = '/api/ecommerce/admin/orders/'


class TestOrderItemVariantDetails(OrderTestMixin, EcommerceTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='variant-details-admin@test.com')
        self.colour = AttributeDefinition.objects.create(
            name={'en': 'Colour', 'ka': 'ფერი'}, key='colour', attribute_type='select',
            options=[{'value': 'brown', 'label': {'en': 'Brown', 'ka': 'ყავისფერი'}}],
        )

    def _variant(self, product, sku, value='brown'):
        variant = ProductVariant.objects.create(
            product=product, sku=sku, name={'en': 'Brown', 'ka': 'ყავისფერი'},
            price=Decimal('10.00'), quantity=5, image='https://cdn.example.com/brown.jpg',
        )
        ProductVariantAttributeValue.objects.create(variant=variant, attribute=self.colour, value_json=value)
        return variant

    def _order_with_variants(self, count):
        product = self._make_product()
        order = self._make_order()
        for i in range(count):
            OrderItem.objects.create(
                order=order, product=product, variant=self._variant(product, f'SATIN-{order.pk}-{i}'),
                product_name={'en': 'Satin Durag'}, quantity=1, price=Decimal('10.00'),
            )
        return order

    def test_detail_returns_variant_name_sku_and_attribute_labels(self):
        order = self._order_with_variants(1)
        resp = self.api_get(f'{ORDER_URL}{order.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        details = resp.data['items'][0]['variant_details']
        self.assertEqual(details['name'], {'en': 'Brown', 'ka': 'ყავისფერი'})
        self.assertTrue(details['sku'].startswith('SATIN-'))
        self.assertEqual(details['image'], 'https://cdn.example.com/brown.jpg')
        self.assertEqual(details['attributes'], [{
            'key': 'colour',
            'name': {'en': 'Colour', 'ka': 'ფერი'},
            'value': {'en': 'Brown', 'ka': 'ყავისფერი'},
        }])
        # The id stays for existing clients.
        self.assertEqual(resp.data['items'][0]['variant'], details['id'])

    def test_unknown_option_value_falls_back_to_raw_value(self):
        product = self._make_product()
        order = self._make_order()
        OrderItem.objects.create(
            order=order, product=product, variant=self._variant(product, 'SATIN-RAW', value='teal'),
            product_name={'en': 'Satin Durag'}, quantity=1, price=Decimal('10.00'),
        )
        resp = self.api_get(f'{ORDER_URL}{order.pk}/', user=self.admin)
        self.assertEqual(resp.data['items'][0]['variant_details']['attributes'][0]['value'], 'teal')

    def test_item_without_variant_has_null_details(self):
        product = self._make_product()
        order = self._make_order()
        OrderItem.objects.create(
            order=order, product=product, product_name={'en': 'Plain'}, quantity=1, price=Decimal('10.00'),
        )
        resp = self.api_get(f'{ORDER_URL}{order.pk}/', user=self.admin)
        self.assertIsNone(resp.data['items'][0]['variant_details'])

    def test_query_count_does_not_grow_with_variant_lines(self):
        def count(order):
            with CaptureQueriesContext(connection) as ctx:
                self.api_get(f'{ORDER_URL}{order.pk}/', user=self.admin)
            return len(ctx.captured_queries)
        self.assertEqual(count(self._order_with_variants(1)), count(self._order_with_variants(4)))
