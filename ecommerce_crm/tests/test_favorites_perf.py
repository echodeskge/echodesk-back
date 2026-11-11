"""ECHODESK-BACKEND-6W: the shop client's favorites list ran the review
aggregates and the attribute-definition lookups once per favorited product
(4 queries per product). The products are now loaded through the same
annotated/prefetched queryset the product list uses."""
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from ecommerce_crm.models import FavoriteProduct
from ecommerce_crm.tests.test_storefront_perf import StorefrontTestMixin, _ecommerce_queries
from ecommerce_crm.tokens import issue_shop_tokens
from users.tests.conftest import EchoDeskTenantTestCase

FAV_URL = '/api/ecommerce/client/favorites/'


def _rows(resp):
    data = resp.json()
    return data['results'] if isinstance(data, dict) else data


class TestClientFavoritesQueryCount(StorefrontTestMixin, EchoDeskTenantTestCase):

    def _list(self, shopper):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {issue_shop_tokens(shopper).access_token}')
        with CaptureQueriesContext(connection) as ctx:
            resp = api.get(FAV_URL, HTTP_HOST='tenant.test.com')
        return resp, ctx

    def test_query_count_independent_of_favorite_count(self):
        shopper = self.make_client()
        FavoriteProduct.objects.create(client=shopper, product=self.make_product(1, reviews=[5]))
        resp, ctx_one = self._list(shopper)
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = _rows(resp)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['product']['average_rating'], 5.0)
        self.assertEqual(rows[0]['product']['review_count'], 1)
        self.assertEqual(len(rows[0]['product']['attribute_values']), 1)

        for i in range(2, 7):
            FavoriteProduct.objects.create(
                client=shopper, product=self.make_product(i, reviews=[4, 5], unapproved=[1]),
            )
        resp, ctx_six = self._list(shopper)
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = _rows(resp)
        self.assertEqual(len(rows), 6)
        by_sku = {r['product']['sku']: r['product'] for r in rows}
        self.assertEqual(by_sku['SF-0002']['average_rating'], 4.5)
        self.assertEqual(by_sku['SF-0002']['review_count'], 2)

        self.assertEqual(len(ctx_one), len(ctx_six), [q['sql'] for q in ctx_six.captured_queries])
        for sql in _ecommerce_queries(ctx_six):
            head = sql.lstrip().upper()
            self.assertFalse(
                head.startswith('SELECT AVG')
                or head.startswith('SELECT COUNT(*) AS "__COUNT" FROM "ECOMMERCE_CRM_PRODUCTREVIEW"'),
                f'per-product aggregate query: {sql}',
            )
