"""
Tests for ecommerce admin API endpoints:
- Languages   (CRUD /api/ecommerce/admin/languages/)
- Attributes  (CRUD /api/ecommerce/admin/attributes/)
- Products    (CRUD /api/ecommerce/admin/products/)
- Clients     (CRUD /api/ecommerce/admin/clients/)
- Orders      (CRUD /api/ecommerce/admin/orders/)
- Settings    (CRUD /api/ecommerce/admin/settings/)
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status

from ecommerce_crm.tests.base import EcommerceTenantTestCase as EchoDeskTenantTestCase
from ecommerce_crm.models import (
    Language,
    AttributeDefinition,
    Product,
    EcommerceClient,
    EcommerceSettings,
)

User = get_user_model()

# ── URL constants ──
LANG_URL = '/api/ecommerce/admin/languages/'
ATTR_URL = '/api/ecommerce/admin/attributes/'
PROD_URL = '/api/ecommerce/admin/products/'
CLIENT_URL = '/api/ecommerce/admin/clients/'
ORDER_URL = '/api/ecommerce/admin/orders/'
SETTINGS_URL = '/api/ecommerce/admin/settings/'


def _results(resp):
    """Extract results from paginated or plain response."""
    if isinstance(resp.data, dict) and 'results' in resp.data:
        return resp.data['results']
    return resp.data


# ═══════════════════════════════════════════════════════════
#  Languages
# ═══════════════════════════════════════════════════════════

class TestLanguageCRUD(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='ecom-admin@test.com')

    # ── helpers ──
    def _make_lang(self, code='en', **kw):
        defaults = {
            'code': code,
            'name': {'en': 'English', 'ka': 'ინგლისური'},
            'is_default': False,
            'is_active': True,
        }
        defaults.update(kw)
        return Language.objects.create(**defaults)

    # ── tests ──
    def test_list_languages(self):
        # 'en' and 'ka' are seeded by data migration 0004
        count_before = Language.objects.count()
        self._make_lang('de', name={'en': 'German', 'ka': 'გერმანული'})
        resp = self.api_get(LANG_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(_results(resp)), count_before + 1)

    def test_create_language(self):
        resp = self.api_post(LANG_URL, {
            'code': 'de',
            'name': {'en': 'German', 'ka': 'გერმანული'},
            'is_active': True,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Language.objects.filter(code='de').exists())

    def test_retrieve_language(self):
        lang = self._make_lang('fr', name={'en': 'French'})
        resp = self.api_get(f'{LANG_URL}{lang.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['code'], 'fr')

    def test_update_language(self):
        lang = self._make_lang('es', name={'en': 'Spanish'})
        resp = self.api_patch(f'{LANG_URL}{lang.id}/', {
            'is_active': False,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        lang.refresh_from_db()
        self.assertFalse(lang.is_active)

    def test_delete_language(self):
        lang = self._make_lang('it', name={'en': 'Italian'})
        resp = self.api_delete(f'{LANG_URL}{lang.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Language.objects.filter(code='it').exists())

    def test_unauthenticated_denied(self):
        resp = self.api_get(LANG_URL)
        self.assertIn(resp.status_code, [401, 403])


# ═══════════════════════════════════════════════════════════
#  Attributes
# ═══════════════════════════════════════════════════════════

class TestAttributeCRUD(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='attr-admin@test.com')

    def _make_attr(self, key='color', **kw):
        defaults = {
            'name': {'en': 'Color', 'ka': 'ფერი'},
            'key': key,
            'attribute_type': 'multiselect',
            'is_active': True,
        }
        defaults.update(kw)
        return AttributeDefinition.objects.create(**defaults)

    def test_list_attributes(self):
        self._make_attr('color')
        self._make_attr('size', name={'en': 'Size'})
        resp = self.api_get(ATTR_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(_results(resp)), 2)

    def test_create_attribute(self):
        resp = self.api_post(ATTR_URL, {
            'name': {'en': 'Material'},
            'key': 'material',
            'attribute_type': 'multiselect',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(AttributeDefinition.objects.filter(key='material').exists())

    def test_retrieve_attribute(self):
        attr = self._make_attr('weight', name={'en': 'Weight'}, attribute_type='number')
        resp = self.api_get(f'{ATTR_URL}{attr.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['key'], 'weight')

    def test_update_attribute(self):
        attr = self._make_attr('style')
        resp = self.api_patch(f'{ATTR_URL}{attr.id}/', {
            'is_filterable': False,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attr.refresh_from_db()
        self.assertFalse(attr.is_filterable)

    def test_delete_attribute(self):
        attr = self._make_attr('temp')
        resp = self.api_delete(f'{ATTR_URL}{attr.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_unauthenticated_denied(self):
        resp = self.api_get(ATTR_URL)
        self.assertIn(resp.status_code, [401, 403])


# ═══════════════════════════════════════════════════════════
#  Products
# ═══════════════════════════════════════════════════════════

class TestProductCRUD(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='prod-admin@test.com')

    def _make_product(self, sku='PROD-001', **kw):
        defaults = {
            'sku': sku,
            'name': {'en': 'Test Product'},
            'description': {'en': 'A test product'},
            'price': Decimal('19.99'),
            'status': 'active',
            'created_by': self.admin,
        }
        defaults.update(kw)
        return Product.objects.create(**defaults)

    def test_list_products(self):
        self._make_product('SKU-1')
        self._make_product('SKU-2', name={'en': 'Second'})
        resp = self.api_get(PROD_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(_results(resp)), 2)

    def test_create_product(self):
        resp = self.api_post(PROD_URL, {
            'sku': 'NEW-001',
            'name': {'en': 'New Product'},
            'description': {'en': 'Brand new'},
            'price': '29.99',
            'status': 'draft',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Product.objects.filter(sku='NEW-001').exists())

    def test_retrieve_product(self):
        prod = self._make_product('DET-001')
        resp = self.api_get(f'{PROD_URL}{prod.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['sku'], 'DET-001')

    def test_update_product_price(self):
        prod = self._make_product('UPD-001')
        resp = self.api_patch(f'{PROD_URL}{prod.id}/', {
            'price': '49.99',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        prod.refresh_from_db()
        self.assertEqual(prod.price, Decimal('49.99'))

    def test_delete_product(self):
        prod = self._make_product('DEL-001')
        resp = self.api_delete(f'{PROD_URL}{prod.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Product.objects.filter(sku='DEL-001').exists())

    def test_filter_by_status(self):
        self._make_product('ACT-1', status='active')
        self._make_product('DRF-1', status='draft')
        resp = self.api_get(f'{PROD_URL}?status=active', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        self.assertTrue(all(r['status'] == 'active' for r in results))

    def test_featured_action(self):
        self._make_product('FEAT-1', is_featured=True)
        self._make_product('NORM-1', is_featured=False)
        resp = self.api_get(f'{PROD_URL}featured/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        self.assertTrue(all(r.get('is_featured', True) for r in results))

    def test_unauthenticated_denied(self):
        resp = self.api_get(PROD_URL)
        self.assertIn(resp.status_code, [401, 403])


# ═══════════════════════════════════════════════════════════
#  Ecommerce Clients
# ═══════════════════════════════════════════════════════════

class TestEcommerceClientCRUD(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='client-admin@test.com')

    def _make_client(self, email='buyer@example.com', **kw):
        defaults = {
            'first_name': 'Test',
            'last_name': 'Buyer',
            'email': email,
            'phone_number': kw.pop('phone_number', '+995555000001'),
            'is_active': True,
            'is_verified': True,
        }
        defaults.update(kw)
        client = EcommerceClient(**defaults)
        client.set_password('pass1234')
        client.save()
        return client

    def test_list_clients(self):
        self._make_client('a@ex.com', phone_number='+995555000010')
        self._make_client('b@ex.com', phone_number='+995555000011')
        resp = self.api_get(CLIENT_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(_results(resp)), 2)

    def test_retrieve_client(self):
        c = self._make_client('det@ex.com', phone_number='+995555000020')
        resp = self.api_get(f'{CLIENT_URL}{c.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['email'], 'det@ex.com')

    def test_filter_by_verified(self):
        self._make_client('v@ex.com', phone_number='+995555000030', is_verified=True)
        self._make_client('nv@ex.com', phone_number='+995555000031', is_verified=False)
        resp = self.api_get(f'{CLIENT_URL}?is_verified=true', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for r in _results(resp):
            self.assertTrue(r['is_verified'])

    def test_unauthenticated_denied(self):
        resp = self.api_get(CLIENT_URL)
        self.assertIn(resp.status_code, [401, 403])


# ═══════════════════════════════════════════════════════════
#  Ecommerce Settings
# ═══════════════════════════════════════════════════════════

class TestEcommerceSettings(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='settings-admin@test.com')

    def test_list_settings(self):
        resp = self.api_get(SETTINGS_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_create_settings(self):
        resp = self.api_post(SETTINGS_URL, {
            'store_name': 'My Shop',
            'store_email': 'shop@example.com',
            'enable_card_payment': False,
            'enable_cash_on_delivery': True,
        }, user=self.admin)
        self.assertIn(resp.status_code, [200, 201])
        self.assertTrue(EcommerceSettings.objects.filter(store_name='My Shop').exists())

    def test_unauthenticated_denied(self):
        resp = self.api_get(SETTINGS_URL)
        self.assertIn(resp.status_code, [401, 403])


# ═══════════════════════════════════════════════════════════
#  Product Model Properties
# ═══════════════════════════════════════════════════════════

class TestProductModel(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='model-admin@test.com')

    def test_is_in_stock_true(self):
        p = Product.objects.create(
            sku='STOCK-1', name={'en': 'In Stock'},
            price=Decimal('10'), quantity=5, created_by=self.admin,
        )
        self.assertTrue(p.is_in_stock)

    def test_is_in_stock_false(self):
        p = Product.objects.create(
            sku='STOCK-0', name={'en': 'No Stock'},
            price=Decimal('10'), quantity=0, created_by=self.admin,
        )
        self.assertFalse(p.is_in_stock)

    def test_is_low_stock(self):
        p = Product.objects.create(
            sku='LOW-1', name={'en': 'Low Stock'},
            price=Decimal('10'), quantity=2, low_stock_threshold=5,
            track_inventory=True, created_by=self.admin,
        )
        self.assertTrue(p.is_low_stock)

    def test_discount_percentage(self):
        p = Product.objects.create(
            sku='DISC-1', name={'en': 'Discounted'},
            price=Decimal('80'), compare_at_price=Decimal('100'),
            created_by=self.admin,
        )
        self.assertEqual(p.discount_percentage, 20)

    def test_discount_percentage_no_compare(self):
        p = Product.objects.create(
            sku='NODISC-1', name={'en': 'Full Price'},
            price=Decimal('50'), created_by=self.admin,
        )
        self.assertEqual(p.discount_percentage, 0)

    def test_slug_auto_generated_from_sku(self):
        p = Product.objects.create(
            sku='AUTO-SLUG-1', name={'en': 'Auto Slug'},
            price=Decimal('15'), created_by=self.admin,
        )
        self.assertTrue(p.slug)
        self.assertIn('auto-slug-1', p.slug.lower())
