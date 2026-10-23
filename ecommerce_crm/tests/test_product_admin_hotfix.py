"""
Regression tests for two product-admin 500s seen in production (2026-10-21):
- DELETE of a product that orders still reference raised ProtectedError.
- PATCH with an attribute whose number input was left empty
  ({'value_number': ''}) raised a DecimalField ValidationError.
"""
from decimal import Decimal

from rest_framework import status

from ecommerce_crm.tests.base import EcommerceTenantTestCase
from ecommerce_crm.models import AttributeDefinition, OrderItem, Product
from ecommerce_crm.tests.test_order_views import OrderTestMixin

PROD_URL = '/api/ecommerce/admin/products/'


# main gates admin endpoints on the ecommerce_crm feature.
class TestProductAdminHotfix(OrderTestMixin, EcommerceTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='hotfix-prod-admin@test.com')

    def test_delete_product_with_orders_returns_409(self):
        product = self._make_product()
        order = self._make_order()
        OrderItem.objects.create(
            order=order, product=product,
            product_name={'en': 'P'}, quantity=1, price=Decimal('10.00'),
        )
        resp = self.api_delete(f'{PROD_URL}{product.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT, resp.content)
        self.assertIn('deactivate', resp.data['error'].lower())
        self.assertEqual(resp.data['orders'], [order.order_number])
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

    def test_delete_product_without_orders_still_works(self):
        product = self._make_product()
        resp = self.api_delete(f'{PROD_URL}{product.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())

    def test_patch_with_empty_number_attribute_is_skipped(self):
        product = self._make_product()
        weight = AttributeDefinition.objects.create(
            name={'en': 'Weight'}, key='weight', attribute_type='number',
        )
        colour = AttributeDefinition.objects.create(
            name={'en': 'Colour'}, key='colour', attribute_type='text',
        )
        resp = self.api_patch(f'{PROD_URL}{product.pk}/', {
            'price': '80.00',
            'attributes': [
                {'attribute_id': weight.pk, 'value_number': ''},
                {'attribute_id': colour.pk, 'value_text': 'Blonde'},
            ],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        values = {v.attribute.key: v for v in product.attribute_values.select_related('attribute')}
        self.assertNotIn('weight', values)
        self.assertEqual(values['colour'].value_text, 'Blonde')

    def test_patch_with_number_attribute_still_saved(self):
        product = self._make_product()
        weight = AttributeDefinition.objects.create(
            name={'en': 'Weight'}, key='weight', attribute_type='number',
        )
        resp = self.api_patch(f'{PROD_URL}{product.pk}/', {
            'attributes': [{'attribute_id': weight.pk, 'value_number': '2.5'}],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(product.attribute_values.get().value_number, Decimal('2.5'))
