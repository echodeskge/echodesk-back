"""
Storefront (public client API) performance guards:

* bounded query counts for product list/detail (no per-product N+1),
* response payloads identical to the legacy, un-annotated serializer path,
* the short-lived response cache (hits, key isolation, auth bypass,
  eager invalidation, cache-outage fall-through).
"""
import json
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from rest_framework.renderers import JSONRenderer

from users.tests.conftest import EchoDeskTenantTestCase
from ecommerce_crm import caching
from ecommerce_crm.models import (
    AttributeDefinition,
    EcommerceClient,
    Product,
    ProductAttributeValue,
    ProductImage,
    ProductReview,
    ProductVariant,
    ProductVariantAttributeValue,
    ShippingMethod,
)
from ecommerce_crm.serializers import ProductDetailSerializer, ProductListSerializer

LIST_URL = '/api/ecommerce/client/products/'
SHIPPING_URL = '/api/ecommerce/client/shipping-methods/'
THEME_URL = '/api/ecommerce/client/theme/'


def detail_url(pk):
    return f'{LIST_URL}{pk}/'


def reviews_url(pk):
    return f'{LIST_URL}{pk}/reviews/'


def _ecommerce_queries(ctx):
    return [q['sql'] for q in ctx.captured_queries if 'ecommerce_crm_' in q['sql']]


class StorefrontTestMixin:
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='sf-admin@test.com')
        self.color = AttributeDefinition.objects.create(
            name={'en': 'Color'}, key='color', attribute_type='select',
            options=[{'en': 'Red', 'value': 'red'}], is_filterable=True,
        )
        self._client_seq = 0

    def make_client(self):
        self._client_seq += 1
        return EcommerceClient.objects.create(
            email=f'shopper{self._client_seq}@test.com', first_name=f'Shopper{self._client_seq}',
            last_name='T', phone_number=f'+99555500{self._client_seq:04d}', password='',
        )

    def make_product(self, idx, reviews=(), unapproved=()):
        product = Product.objects.create(
            sku=f'SF-{idx:04d}',
            name={'en': f'Product {idx}', 'ka': f'პროდუქტი {idx}'},
            description={'en': 'desc'},
            short_description={'en': 'short'},
            price=Decimal('19.99'),
            compare_at_price=Decimal('29.99'),
            status='active',
            quantity=50,
            image='https://cdn.test/a.jpg, https://cdn.test/b.jpg',
            created_by=self.admin,
        )
        ProductImage.objects.create(product=product, image=f'https://cdn.test/{idx}-1.jpg', sort_order=1)
        ProductImage.objects.create(product=product, image=f'https://cdn.test/{idx}-0.jpg', sort_order=0)
        ProductAttributeValue.objects.create(product=product, attribute=self.color, value_text='red')
        for v in range(2):
            variant = ProductVariant.objects.create(
                product=product, sku=f'SF-{idx:04d}-V{v}', name={'en': f'V{v}'},
                price=None if v == 0 else Decimal('24.50'), quantity=5,
            )
            ProductVariantAttributeValue.objects.create(variant=variant, attribute=self.color, value_json='red')
        for rating in reviews:
            ProductReview.objects.create(product=product, client=self.make_client(), rating=rating)
        for rating in unapproved:
            ProductReview.objects.create(
                product=product, client=self.make_client(), rating=rating, is_approved=False,
            )
        return product

    def fresh_get(self, url, **kwargs):
        """GET bypassing any cached entry (new version) and count queries."""
        caching.bump_version(connection.schema_name)
        with CaptureQueriesContext(connection) as ctx:
            resp = self.api_get(url, **kwargs)
        return resp, ctx


class TestStorefrontQueryCounts(StorefrontTestMixin, EchoDeskTenantTestCase):

    def test_list_query_count_independent_of_product_count(self):
        self.make_product(1, reviews=[5])
        resp, ctx_one = self.fresh_get(LIST_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['count'], 1)

        for i in range(2, 11):
            self.make_product(i, reviews=[4, 5], unapproved=[1])
        resp, ctx_ten = self.fresh_get(LIST_URL)
        self.assertEqual(resp.json()['count'], 10)

        self.assertEqual(len(ctx_one), len(ctx_ten), [q['sql'] for q in ctx_ten.captured_queries])
        self.assertLessEqual(len(_ecommerce_queries(ctx_ten)), 5)
        for sql in _ecommerce_queries(ctx_ten):
            head = sql.lstrip().upper()
            self.assertFalse(
                head.startswith('SELECT AVG')
                or head.startswith('SELECT COUNT(*) AS "__COUNT" FROM "ECOMMERCE_CRM_PRODUCTREVIEW"'),
                f'per-product aggregate query: {sql}',
            )

    def test_detail_query_count_bounded(self):
        product = self.make_product(1, reviews=[3, 4, 5])
        resp, ctx = self.fresh_get(detail_url(product.pk))
        self.assertEqual(resp.status_code, 200)
        # product (+users join) / images / attribute values / attributes /
        # variants / variant attribute values / their attributes
        self.assertLessEqual(len(_ecommerce_queries(ctx)), 7, _ecommerce_queries(ctx))

        # Adding reviews/variants doesn't add queries.
        for _ in range(3):
            ProductReview.objects.create(product=product, client=self.make_client(), rating=2)
        resp2, ctx2 = self.fresh_get(detail_url(product.pk))
        self.assertEqual(len(ctx), len(ctx2))

    def test_attribute_filter_does_not_skew_rating(self):
        self.make_product(1, reviews=[5, 4, 4])
        resp, _ = self.fresh_get(LIST_URL + '?attr_color=red')
        row = resp.json()['results'][0]
        self.assertEqual(row['average_rating'], 4.3)
        self.assertEqual(row['review_count'], 3)


class TestStorefrontResponseShape(StorefrontTestMixin, EchoDeskTenantTestCase):
    """The annotated fast path must render exactly what the legacy
    per-object path (no annotations) rendered."""

    @staticmethod
    def _legacy(serializer_cls, product, language='en'):
        plain = Product.objects.get(pk=product.pk)  # no annotations / prefetch
        assert not hasattr(plain, '_avg_rating')
        data = serializer_cls(plain, context={'language': language}).data
        return json.loads(JSONRenderer().render(data))

    def test_list_payload_matches_legacy(self):
        rated = self.make_product(1, reviews=[5, 4, 4], unapproved=[1])
        unrated = self.make_product(2)
        resp, _ = self.fresh_get(LIST_URL)
        by_id = {row['id']: row for row in resp.json()['results']}
        rows = [by_id[rated.pk], by_id[unrated.pk]]
        self.assertEqual(rows[0], self._legacy(ProductListSerializer, rated))
        self.assertEqual(rows[1], self._legacy(ProductListSerializer, unrated))

        self.assertEqual(list(rows[0].keys()), [
            'id', 'sku', 'slug', 'name', 'short_description', 'price', 'compare_at_price',
            'discount_percentage', 'image', 'images', 'quantity', 'status', 'is_featured',
            'is_low_stock', 'is_in_stock', 'attribute_values', 'average_rating',
            'review_count', 'created_at', 'updated_at',
        ])
        self.assertEqual(rows[0]['average_rating'], 4.3)
        self.assertIsInstance(rows[0]['average_rating'], float)
        self.assertEqual(rows[0]['review_count'], 3)
        self.assertIsNone(rows[1]['average_rating'])
        self.assertEqual(rows[1]['review_count'], 0)
        self.assertEqual(rows[0]['price'], '19.99')
        self.assertEqual(rows[0]['image'], 'https://cdn.test/a.jpg')
        self.assertEqual(
            [i['image'] for i in rows[0]['images']],
            ['https://cdn.test/a.jpg', 'https://cdn.test/b.jpg',
             'https://cdn.test/1-0.jpg', 'https://cdn.test/1-1.jpg'],
        )

    def test_detail_payload_matches_legacy(self):
        product = self.make_product(1, reviews=[5, 5, 4])
        resp, _ = self.fresh_get(detail_url(product.pk))
        body = resp.json()
        self.assertEqual(body, self._legacy(ProductDetailSerializer, product))
        self.assertEqual(body['average_rating'], 4.7)
        self.assertEqual(body['review_count'], 3)
        self.assertEqual(len(body['variants']), 2)
        self.assertEqual(body['variants'][0]['effective_price'], '19.99')


class TestStorefrontCache(StorefrontTestMixin, EchoDeskTenantTestCase):

    def test_cache_hit_serves_without_db_queries(self):
        self.make_product(1, reviews=[5])
        first = self.api_get(LIST_URL)
        self.assertEqual(first['X-Storefront-Cache'], 'MISS')
        with CaptureQueriesContext(connection) as ctx:
            second = self.api_get(LIST_URL)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second['X-Storefront-Cache'], 'HIT')
        self.assertEqual(second.content, first.content)
        self.assertEqual(second['Content-Type'], first['Content-Type'])
        self.assertEqual(_ecommerce_queries(ctx), [])
        self.assertIn('max-age=30', second['Cache-Control'])

    def test_every_public_endpoint_is_cached(self):
        product = self.make_product(1, reviews=[4])
        ShippingMethod.objects.create(name={'en': 'Std'}, price=Decimal('5.00'))
        for url in (LIST_URL, detail_url(product.pk), reviews_url(product.pk), SHIPPING_URL, THEME_URL):
            self.assertEqual(self.api_get(url)['X-Storefront-Cache'], 'MISS', url)
            self.assertEqual(self.api_get(url)['X-Storefront-Cache'], 'HIT', url)

    def test_query_string_and_language_separate_entries(self):
        self.make_product(1)
        self.api_get(LIST_URL + '?language=en&page=1')
        # Same params in another order -> same entry.
        self.assertEqual(self.api_get(LIST_URL + '?page=1&language=en')['X-Storefront-Cache'], 'HIT')
        self.assertEqual(self.api_get(LIST_URL + '?language=ka&page=1')['X-Storefront-Cache'], 'MISS')
        self.assertEqual(
            self.api_get(LIST_URL + '?language=en&page=1', HTTP_ACCEPT_LANGUAGE='ka')['X-Storefront-Cache'],
            'MISS',
        )

    def test_repeated_param_order_is_preserved_in_key(self):
        rf = RequestFactory()
        a = rf.get('/x/?ordering=price&ordering=-price', HTTP_HOST='tenant.test.com')
        b = rf.get('/x/?ordering=-price&ordering=price', HTTP_HOST='tenant.test.com')
        self.assertNotEqual(
            caching.build_cache_key(a, schema='s', version='1'),
            caching.build_cache_key(b, schema='s', version='1'),
        )

    def test_cache_key_separates_tenants_and_languages(self):
        rf = RequestFactory()
        req = rf.get('/api/ecommerce/client/products/?a=1', HTTP_HOST='tenant.test.com')
        key_a = caching.build_cache_key(req, schema='tenant_a', version='1')
        key_b = caching.build_cache_key(req, schema='tenant_b', version='1')
        self.assertNotEqual(key_a, key_b)
        self.assertIn(':tenant_a:', key_a)
        req_ka = rf.get('/api/ecommerce/client/products/?a=1', HTTP_HOST='tenant.test.com',
                        HTTP_ACCEPT_LANGUAGE='ka')
        self.assertNotEqual(key_a, caching.build_cache_key(req_ka, schema='tenant_a', version='1'))
        # Versions are per tenant: bumping one leaves the other untouched.
        va = caching.get_version('tenant_a')
        vb = caching.get_version('tenant_b')
        caching.bump_version('tenant_b')
        self.assertEqual(caching.get_version('tenant_a'), va)
        self.assertNotEqual(caching.get_version('tenant_b'), vb)

    def test_authenticated_requests_bypass_cache(self):
        self.make_product(1)
        auth = self.api_get(LIST_URL, HTTP_AUTHORIZATION='Bearer abc')
        self.assertEqual(auth.status_code, 200)
        self.assertFalse(auth.has_header('X-Storefront-Cache'))
        # Nothing was stored by the authenticated request.
        self.assertEqual(self.api_get(LIST_URL)['X-Storefront-Cache'], 'MISS')
        # A primed entry is not served to authenticated callers either.
        with CaptureQueriesContext(connection) as ctx:
            auth2 = self.api_get(LIST_URL, HTTP_AUTHORIZATION='Bearer abc')
        self.assertFalse(auth2.has_header('X-Storefront-Cache'))
        self.assertTrue(_ecommerce_queries(ctx))
        # Session cookie (staff browsing) bypasses too.
        self.client.cookies['sessionid'] = 'whatever'
        self.assertFalse(self.api_get(LIST_URL).has_header('X-Storefront-Cache'))

    def test_non_200_not_cached(self):
        for _ in range(2):
            resp = self.api_get(detail_url(999999))
            self.assertEqual(resp.status_code, 404)
            self.assertFalse(resp.has_header('X-Storefront-Cache'))

    def test_product_edit_bumps_version_next_get_is_fresh(self):
        product = self.make_product(1)
        self.api_get(detail_url(product.pk))
        self.assertEqual(self.api_get(detail_url(product.pk))['X-Storefront-Cache'], 'HIT')

        with self.captureOnCommitCallbacks(execute=True):
            product.price = Decimal('11.00')
            product.save()
        resp = self.api_get(detail_url(product.pk))
        self.assertEqual(resp['X-Storefront-Cache'], 'MISS')
        self.assertEqual(resp.json()['price'], '11.00')

    def test_related_writes_bump_version(self):
        product = self.make_product(1)
        schema = connection.schema_name
        writes = [
            lambda: ProductVariant.objects.filter(product=product).first().save(),
            lambda: ProductImage.objects.filter(product=product).first().delete(),
            lambda: ProductReview.objects.create(product=product, client=self.make_client(), rating=3),
            lambda: ShippingMethod.objects.create(name={'en': 'Fast'}, price=Decimal('9.00')),
            lambda: Product.objects.filter(pk=product.pk).exists() and self.api_post(
                '/api/ecommerce/admin/products/bulk-update/',
                {'product_ids': [product.pk], 'status': 'draft'}, user=self.admin,
            ),
        ]
        for write in writes:
            before = caching.get_version(schema)
            write()
            self.assertNotEqual(caching.get_version(schema), before)

    def test_bulk_status_update_hides_product(self):
        product = self.make_product(1)
        self.assertEqual(self.api_get(LIST_URL).json()['count'], 1)
        resp = self.api_post(
            '/api/ecommerce/admin/products/bulk-update/',
            {'product_ids': [product.pk], 'status': 'draft'}, user=self.admin,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.api_get(LIST_URL).json()['count'], 0)

    def test_cache_backend_failure_falls_through(self):
        self.make_product(1, reviews=[5])
        boom = ConnectionError('redis down')
        with mock.patch.object(caching.cache, 'get', side_effect=boom), \
                mock.patch.object(caching.cache, 'set', side_effect=boom), \
                mock.patch.object(caching.cache, 'add', side_effect=boom):
            resp = self.api_get(LIST_URL)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()['count'], 1)
            self.assertFalse(resp.has_header('X-Storefront-Cache'))
            # Writes keep working when the version bump can't reach Redis.
            Product.objects.filter(sku='SF-0001').first().save()
            self.assertEqual(self.api_get(detail_url(Product.objects.get(sku='SF-0001').pk)).status_code, 200)

    def test_store_failure_still_returns_response(self):
        self.make_product(1)
        with mock.patch.object(caching.cache, 'set', side_effect=ConnectionError('down')):
            resp = self.api_get(LIST_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['count'], 1)
