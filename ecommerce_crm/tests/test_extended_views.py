"""
Tests for ecommerce admin API endpoints not covered in test_ecommerce_views.py:
- Cart operations      (CRUD via /api/ecommerce/admin/cart/ and /api/ecommerce/admin/cart-items/)
- Favorites            (CRUD, toggle, is_favorited via /api/ecommerce/admin/favorites/)
- Product filtering    (by status, search, featured, low stock)
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from rest_framework import status

from ecommerce_crm.tests.base import EcommerceTenantTestCase as EchoDeskTenantTestCase
from ecommerce_crm.models import (
    Product,
    EcommerceClient,
    Cart,
    CartItem,
    FavoriteProduct,
)

User = get_user_model()

# ── URL constants ──
CART_URL = '/api/ecommerce/admin/cart/'
CART_ITEM_URL = '/api/ecommerce/admin/cart-items/'
FAV_URL = '/api/ecommerce/admin/favorites/'
PROD_URL = '/api/ecommerce/admin/products/'


def _results(resp):
    """Extract results from paginated or plain response."""
    if isinstance(resp.data, dict) and 'results' in resp.data:
        return resp.data['results']
    return resp.data


class EcommerceExtendedTestMixin:
    """Shared helpers for ecommerce extended tests."""

    def _make_client(self, email='eclient@test.com', **kw):
        defaults = {
            'first_name': 'Test',
            'last_name': 'Client',
            'email': email,
            'phone_number': f'+99555{EcommerceClient.objects.count():06d}',
            'password': make_password('clientpass123'),
            'is_active': True,
            'is_verified': True,
        }
        defaults.update(kw)
        return EcommerceClient.objects.create(**defaults)

    def _make_product(self, sku=None, **kw):
        if sku is None:
            sku = f'SKU-{Product.objects.count() + 1:04d}'
        defaults = {
            'sku': sku,
            'name': {'en': 'Test Product', 'ka': 'ტესტ პროდუქტი'},
            'price': Decimal('49.99'),
            'status': 'active',
            'slug': sku.lower(),
            'quantity': 100,
            'track_inventory': True,
            'low_stock_threshold': 10,
        }
        defaults.update(kw)
        return Product.objects.create(**defaults)


# ═══════════════════════════════════════════════════════════
#  Cart operations
# ═══════════════════════════════════════════════════════════

class TestCartCRUD(EcommerceExtendedTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='cart-admin@test.com')
        self.eclient = self._make_client()

    def test_create_cart(self):
        resp = self.api_post(CART_URL, {
            'client': self.eclient.pk,
            'status': 'active',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_carts(self):
        Cart.objects.create(client=self.eclient, status='active')
        resp = self.api_get(CART_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(_results(resp)), 1)

    def test_retrieve_cart(self):
        cart = Cart.objects.create(client=self.eclient, status='active')
        resp = self.api_get(f'{CART_URL}{cart.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_update_cart(self):
        cart = Cart.objects.create(client=self.eclient, status='active')
        resp = self.api_patch(
            f'{CART_URL}{cart.pk}/',
            {'notes': 'Rush order'},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_cart(self):
        cart = Cart.objects.create(client=self.eclient, status='active')
        resp = self.api_delete(f'{CART_URL}{cart.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_unauthenticated_denied(self):
        resp = self.api_get(CART_URL)
        self.assertIn(resp.status_code, [401, 403])


# ═══════════════════════════════════════════════════════════
#  Cart Items
# ═══════════════════════════════════════════════════════════

class TestCartItemCRUD(EcommerceExtendedTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='cartitem-admin@test.com')
        self.eclient = self._make_client(email='cartitem-client@test.com')
        self.product = self._make_product(sku='CART-PROD-001')
        self.cart = Cart.objects.create(client=self.eclient, status='active')

    def test_add_item_to_cart(self):
        resp = self.api_post(CART_ITEM_URL, {
            'cart': self.cart.pk,
            'product': self.product.pk,
            'quantity': 2,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_update_cart_item_quantity(self):
        item = CartItem.objects.create(
            cart=self.cart,
            product=self.product,
            quantity=1,
            price_at_add=self.product.price,
        )
        resp = self.api_patch(
            f'{CART_ITEM_URL}{item.pk}/',
            {'quantity': 5},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_remove_item_from_cart(self):
        item = CartItem.objects.create(
            cart=self.cart,
            product=self.product,
            quantity=1,
            price_at_add=self.product.price,
        )
        resp = self.api_delete(f'{CART_ITEM_URL}{item.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_list_cart_items(self):
        CartItem.objects.create(
            cart=self.cart,
            product=self.product,
            quantity=3,
            price_at_add=self.product.price,
        )
        resp = self.api_get(CART_ITEM_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(_results(resp)), 1)


# ═══════════════════════════════════════════════════════════
#  Favorites
# ═══════════════════════════════════════════════════════════

class TestFavoriteCRUD(EcommerceExtendedTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='fav-admin@test.com')
        self.eclient = self._make_client(email='fav-client@test.com')
        self.product = self._make_product(sku='FAV-PROD-001')

    def test_add_favorite(self):
        resp = self.api_post(FAV_URL, {
            'client': self.eclient.pk,
            'product': self.product.pk,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_favorites(self):
        FavoriteProduct.objects.create(client=self.eclient, product=self.product)
        resp = self.api_get(FAV_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(_results(resp)), 1)

    def test_remove_favorite(self):
        fav = FavoriteProduct.objects.create(client=self.eclient, product=self.product)
        resp = self.api_delete(f'{FAV_URL}{fav.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_is_favorited_endpoint(self):
        FavoriteProduct.objects.create(client=self.eclient, product=self.product)
        resp = self.api_get(
            f'{FAV_URL}is_favorited/?client={self.eclient.pk}&product={self.product.pk}',
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['is_favorited'])

    def test_is_favorited_false(self):
        resp = self.api_get(
            f'{FAV_URL}is_favorited/?client={self.eclient.pk}&product={self.product.pk}',
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['is_favorited'])

    def test_is_favorited_missing_params(self):
        resp = self.api_get(f'{FAV_URL}is_favorited/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_toggle_adds_favorite(self):
        resp = self.api_post(
            f'{FAV_URL}toggle/',
            {'client': self.eclient.pk, 'product': self.product.pk},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(resp.data['is_favorited'])

    def test_toggle_removes_favorite(self):
        FavoriteProduct.objects.create(client=self.eclient, product=self.product)
        resp = self.api_post(
            f'{FAV_URL}toggle/',
            {'client': self.eclient.pk, 'product': self.product.pk},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['is_favorited'])

    def test_toggle_missing_params(self):
        resp = self.api_post(f'{FAV_URL}toggle/', {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ═══════════════════════════════════════════════════════════
#  Product filtering
# ═══════════════════════════════════════════════════════════

class TestProductFiltering(EcommerceExtendedTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='filter-admin@test.com')

    def test_filter_by_status(self):
        self._make_product(sku='FILT-ACT', status='active')
        self._make_product(sku='FILT-DRF', status='draft')
        resp = self.api_get(f'{PROD_URL}?status=draft', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        for r in results:
            self.assertEqual(r['status'], 'draft')

    def test_search_by_sku(self):
        self._make_product(sku='SEARCH-XYZ')
        resp = self.api_get(f'{PROD_URL}?search=SEARCH-XYZ', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        self.assertGreaterEqual(len(results), 1)

    def test_featured_products(self):
        self._make_product(sku='FEAT-001', is_featured=True)
        self._make_product(sku='NONFEAT-001', is_featured=False)
        resp = self.api_get(f'{PROD_URL}featured/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        for r in results:
            self.assertTrue(r.get('is_featured'))

    def test_low_stock_products(self):
        self._make_product(
            sku='LOW-001',
            quantity=5,
            track_inventory=True,
            low_stock_threshold=10,
        )
        self._make_product(
            sku='HIGH-001',
            quantity=100,
            track_inventory=True,
            low_stock_threshold=10,
        )
        resp = self.api_get(f'{PROD_URL}low_stock/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        for r in results:
            self.assertLessEqual(r['quantity'], r.get('low_stock_threshold', 10))

    def test_default_list_returns_every_status(self):
        self._make_product(sku='DEF-ACT', status='active')
        self._make_product(sku='DEF-DRF', status='draft')
        resp = self.api_get(PROD_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        statuses = {r['status'] for r in results}
        # The admin list shows every status unless ?status narrows it
        # (the web "All" filter sends no status).
        self.assertIn('draft', statuses)
        self.assertIn('active', statuses)
