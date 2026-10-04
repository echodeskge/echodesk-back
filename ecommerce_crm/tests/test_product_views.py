"""
Extended tests for ecommerce admin Product API endpoints:
- Paginated listing
- Create with all required fields
- PATCH update
- Soft delete / hard delete
- Filter by category (attributes), status, search
- Product variants (CRUD)
- Product images (add/remove)
- Bulk product status update
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase
from ecommerce_crm.models import (
    Product,
    ProductImage,
    ProductVariant,
    AttributeDefinition,
    ProductAttributeValue,
)

User = get_user_model()

PROD_URL = '/api/ecommerce/admin/products/'
VARIANT_URL = '/api/ecommerce/admin/variants/'
IMAGE_URL = '/api/ecommerce/admin/images/'


def _results(resp):
    """Extract results from paginated or plain response."""
    if isinstance(resp.data, dict) and 'results' in resp.data:
        return resp.data['results']
    return resp.data


class ProductViewTestMixin:
    """Shared helpers for product view tests."""

    def _make_product(self, sku=None, **kw):
        if sku is None:
            sku = f'PV-{Product.objects.count() + 1:04d}'
        defaults = {
            'sku': sku,
            'name': {'en': 'Test Product', 'ka': 'ტესტი'},
            'description': {'en': 'A test product'},
            'price': Decimal('19.99'),
            'status': 'active',
            'quantity': 50,
            'track_inventory': True,
            'low_stock_threshold': 10,
            'created_by': self.admin,
        }
        defaults.update(kw)
        return Product.objects.create(**defaults)


# ============================================================================
# Paginated listing
# ============================================================================

class TestListProducts(ProductViewTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='list-prod-admin@test.com')

    def test_list_products_returns_200(self):
        self._make_product('LIST-1')
        self._make_product('LIST-2')
        resp = self.api_get(PROD_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_products_paginated(self):
        """Default list returns paginated results if pagination is configured."""
        for i in range(5):
            self._make_product(f'PAGE-{i:03d}')
        resp = self.api_get(PROD_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        self.assertGreaterEqual(len(results), 5)

    def test_list_products_default_active_only(self):
        """Default list filters to active products only."""
        self._make_product('ACTIVE-1', status='active')
        self._make_product('DRAFT-1', status='draft')
        resp = self.api_get(PROD_URL, user=self.admin)
        results = _results(resp)
        for r in results:
            self.assertEqual(r['status'], 'active')

    def test_unauthenticated_denied(self):
        resp = self.api_get(PROD_URL)
        self.assertIn(resp.status_code, [401, 403])


# ============================================================================
# Create product
# ============================================================================

class TestCreateProduct(ProductViewTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='create-prod-admin@test.com')

    def test_create_product_with_required_fields(self):
        resp = self.api_post(PROD_URL, {
            'sku': 'CREATE-001',
            'name': {'en': 'Brand New', 'ka': 'ახალი'},
            'description': {'en': 'Full description'},
            'price': '39.99',
            'status': 'draft',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Product.objects.filter(sku='CREATE-001').exists())

    def test_create_product_auto_generates_slug(self):
        resp = self.api_post(PROD_URL, {
            'sku': 'SLUG-GEN-001',
            'name': {'en': 'Slug Test'},
            'price': '10.00',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        product = Product.objects.get(sku='SLUG-GEN-001')
        self.assertTrue(product.slug)
        self.assertIn('slug-gen-001', product.slug.lower())

    def test_create_product_with_compare_at_price(self):
        resp = self.api_post(PROD_URL, {
            'sku': 'COMPARE-001',
            'name': {'en': 'Discounted'},
            'price': '80.00',
            'compare_at_price': '100.00',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_create_product_with_inventory(self):
        resp = self.api_post(PROD_URL, {
            'sku': 'INV-001',
            'name': {'en': 'Tracked'},
            'price': '25.00',
            'track_inventory': True,
            'quantity': 100,
            'low_stock_threshold': 15,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        p = Product.objects.get(sku='INV-001')
        self.assertTrue(p.track_inventory)
        self.assertEqual(p.quantity, 100)


# ============================================================================
# Update product
# ============================================================================

class TestUpdateProduct(ProductViewTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='update-prod-admin@test.com')

    def test_patch_updates_price(self):
        prod = self._make_product('UPD-PRICE')
        resp = self.api_patch(f'{PROD_URL}{prod.id}/', {
            'price': '99.99',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        prod.refresh_from_db()
        self.assertEqual(prod.price, Decimal('99.99'))

    def test_patch_updates_name(self):
        prod = self._make_product('UPD-NAME')
        resp = self.api_patch(f'{PROD_URL}{prod.id}/', {
            'name': {'en': 'Updated Name', 'ka': 'განახლებული'},
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        prod.refresh_from_db()
        self.assertEqual(prod.get_name('en'), 'Updated Name')

    def test_patch_updates_status(self):
        prod = self._make_product('UPD-STATUS', status='draft')
        resp = self.api_patch(f'{PROD_URL}{prod.id}/', {
            'status': 'active',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        prod.refresh_from_db()
        self.assertEqual(prod.status, 'active')

    def test_patch_updates_quantity(self):
        prod = self._make_product('UPD-QTY', quantity=10)
        resp = self.api_patch(f'{PROD_URL}{prod.id}/', {
            'quantity': 200,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        prod.refresh_from_db()
        self.assertEqual(prod.quantity, 200)


# ============================================================================
# Delete product
# ============================================================================

class TestDeleteProduct(ProductViewTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='del-prod-admin@test.com')

    def test_delete_product(self):
        prod = self._make_product('DEL-001')
        resp = self.api_delete(f'{PROD_URL}{prod.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Product.objects.filter(sku='DEL-001').exists())

    def test_delete_nonexistent_product_returns_404(self):
        resp = self.api_delete(f'{PROD_URL}99999/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ============================================================================
# Filter by status
# ============================================================================

class TestFilterByStatus(ProductViewTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='status-filter-admin@test.com')

    def test_filter_active(self):
        self._make_product('FILT-ACT-1', status='active')
        self._make_product('FILT-DRF-1', status='draft')
        resp = self.api_get(f'{PROD_URL}?status=active', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        for r in results:
            self.assertEqual(r['status'], 'active')

    def test_filter_draft(self):
        self._make_product('FILT-DRF-2', status='draft')
        resp = self.api_get(f'{PROD_URL}?status=draft', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        for r in results:
            self.assertEqual(r['status'], 'draft')

    def test_filter_inactive(self):
        self._make_product('FILT-INA-1', status='inactive')
        resp = self.api_get(f'{PROD_URL}?status=inactive', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        for r in results:
            self.assertEqual(r['status'], 'inactive')


# ============================================================================
# Search products
# ============================================================================

class TestSearchProducts(ProductViewTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='search-prod-admin@test.com')

    def test_search_by_sku(self):
        self._make_product('SEARCH-UNIQUE-XYZ')
        resp = self.api_get(f'{PROD_URL}?search=SEARCH-UNIQUE-XYZ', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        self.assertGreaterEqual(len(results), 1)
        skus = [r['sku'] for r in results]
        self.assertIn('SEARCH-UNIQUE-XYZ', skus)

    def test_search_no_match_returns_empty(self):
        resp = self.api_get(f'{PROD_URL}?search=NONEXISTENT-ABC-999', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        self.assertEqual(len(results), 0)

    def test_search_partial_sku(self):
        self._make_product('PARTIAL-SRCH-001')
        resp = self.api_get(f'{PROD_URL}?search=PARTIAL-SRCH', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        self.assertGreaterEqual(len(results), 1)


# ============================================================================
# Product variants
# ============================================================================

class TestProductVariants(ProductViewTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='variant-view-admin@test.com')
        self.product = self._make_product('VAR-PARENT-001')

    def test_list_variants(self):
        ProductVariant.objects.create(
            product=self.product, sku='VAR-LIST-001',
            name={'en': 'Variant A'}, quantity=10,
        )
        ProductVariant.objects.create(
            product=self.product, sku='VAR-LIST-002',
            name={'en': 'Variant B'}, quantity=5,
        )
        resp = self.api_get(f'{VARIANT_URL}?product={self.product.pk}', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        self.assertGreaterEqual(len(results), 2)

    def test_update_variant(self):
        v = ProductVariant.objects.create(
            product=self.product, sku='VAR-UPD-001',
            name={'en': 'Original'}, price=Decimal('30.00'), quantity=5,
        )
        resp = self.api_patch(f'{VARIANT_URL}{v.pk}/', {
            'price': '35.00',
            'quantity': 15,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        v.refresh_from_db()
        self.assertEqual(v.price, Decimal('35.00'))
        self.assertEqual(v.quantity, 15)

    def test_delete_variant(self):
        v = ProductVariant.objects.create(
            product=self.product, sku='VAR-DEL-001',
            name={'en': 'Disposable'}, quantity=1,
        )
        resp = self.api_delete(f'{VARIANT_URL}{v.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_variant_effective_price_with_own_price(self):
        v = ProductVariant.objects.create(
            product=self.product, sku='VAR-EP-001',
            name={'en': 'Custom Price'}, price=Decimal('99.99'), quantity=1,
        )
        self.assertEqual(v.effective_price, Decimal('99.99'))

    def test_variant_effective_price_inherits_product(self):
        v = ProductVariant.objects.create(
            product=self.product, sku='VAR-EP-002',
            name={'en': 'No Price'}, price=None, quantity=1,
        )
        self.assertEqual(v.effective_price, self.product.price)


# ============================================================================
# Product images
# ============================================================================

class TestProductImages(ProductViewTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='img-view-admin@test.com')
        self.product = self._make_product('IMG-PARENT-001')

    def test_add_image_via_product_action(self):
        resp = self.api_post(f'{PROD_URL}{self.product.pk}/add_image/', {
            'image': 'https://example.com/image2.jpg',
            'sort_order': 1,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_remove_image_via_product_action(self):
        img = ProductImage.objects.create(
            product=self.product,
            image='https://example.com/to-delete.jpg',
            sort_order=0,
        )
        resp = self.api_delete(
            f'{PROD_URL}{self.product.pk}/remove_image/{img.id}/',
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ProductImage.objects.filter(pk=img.pk).exists())

    def test_remove_nonexistent_image_returns_404(self):
        resp = self.api_delete(
            f'{PROD_URL}{self.product.pk}/remove_image/99999/',
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_list_images_for_product(self):
        ProductImage.objects.create(
            product=self.product,
            image='https://example.com/a.jpg',
            sort_order=0,
        )
        ProductImage.objects.create(
            product=self.product,
            image='https://example.com/b.jpg',
            sort_order=1,
        )
        resp = self.api_get(f'{IMAGE_URL}?product={self.product.pk}', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        self.assertEqual(len(results), 2)

    def test_delete_image_via_viewset(self):
        img = ProductImage.objects.create(
            product=self.product,
            image='https://example.com/del.jpg',
            sort_order=0,
        )
        resp = self.api_delete(f'{IMAGE_URL}{img.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)


# ============================================================================
# Bulk product update
# ============================================================================

class TestBulkProductUpdate(ProductViewTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bulk-prod-admin@test.com')

    def test_bulk_update_status(self):
        p1 = self._make_product('BULK-1', status='draft')
        p2 = self._make_product('BULK-2', status='draft')
        resp = self.api_post(f'{PROD_URL}bulk-update/', {
            'product_ids': [p1.pk, p2.pk],
            'status': 'active',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['updated'], 2)
        p1.refresh_from_db()
        p2.refresh_from_db()
        self.assertEqual(p1.status, 'active')
        self.assertEqual(p2.status, 'active')

    def test_bulk_update_missing_ids(self):
        resp = self.api_post(f'{PROD_URL}bulk-update/', {
            'status': 'active',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_update_missing_status(self):
        p1 = self._make_product('BULK-NO-STATUS')
        resp = self.api_post(f'{PROD_URL}bulk-update/', {
            'product_ids': [p1.pk],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_update_empty_ids(self):
        resp = self.api_post(f'{PROD_URL}bulk-update/', {
            'product_ids': [],
            'status': 'active',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestVariantAdminApi(EchoDeskTenantTestCase):
    """Bug report #225: adding a variant to a product did not save."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='variant-admin@test.com')
        from ecommerce_crm.models import Product
        self.product = Product.objects.create(
            sku='VAR-PARENT', name={'en': 'Parent'}, price='80.00', status='active',
            quantity=5, created_by=self.admin,
        )

    def test_create_variant_for_a_product(self):
        resp = self.api_post('/api/ecommerce/admin/variants/', {
            'product': self.product.id, 'sku': 'VAR-PARENT-OMBRE',
            'name': {'en': 'ombre', 'ka': 'ombre'}, 'price': '80', 'quantity': 1, 'is_active': True,
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['product'], self.product.id)
        self.assertEqual(self.product.variants.count(), 1)

    def test_variant_without_price_uses_product_price_and_product_is_required(self):
        ok = self.api_post('/api/ecommerce/admin/variants/', {
            'product': self.product.id, 'sku': 'VAR-NOPRICE', 'name': {'en': 'x'}, 'price': None, 'quantity': 0,
        }, user=self.admin)
        self.assertEqual(ok.status_code, 201, ok.data)
        missing = self.api_post('/api/ecommerce/admin/variants/', {'sku': 'VAR-ORPHAN', 'name': {'en': 'x'}}, user=self.admin)
        self.assertEqual(missing.status_code, 400)
        self.assertIn('product', missing.data)

    def test_inactive_variant_stays_visible_and_can_be_reactivated(self):
        from ecommerce_crm.models import ProductVariant
        variant = ProductVariant.objects.create(product=self.product, sku='VAR-OFF', name={'en': 'off'}, is_active=False)
        listed = self.api_get(f'/api/ecommerce/admin/variants/?product={self.product.id}', user=self.admin)
        self.assertEqual([v['id'] for v in listed.data['results']], [variant.id])
        back_on = self.api_patch(f'/api/ecommerce/admin/variants/{variant.id}/', {'is_active': True}, user=self.admin)
        self.assertEqual(back_on.status_code, 200, back_on.data)
