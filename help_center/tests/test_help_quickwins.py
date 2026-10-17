"""
Tests for the help-center quick wins (mobile readiness):

- search is public (AllowAny)
- `for_dashboard=true` takes precedence over `for_public=true` everywhere
- search applies the same category-level visibility gating as the list
- category detail embeds articles without an N+1
- article detail carries Last-Modified / honours If-Modified-Since
- OpenAPI schema documents the query params and the bare-list responses
"""
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import include, path
from django.utils.http import http_date
from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase
from help_center.tests.test_help_views import (
    HelpTestMixin,
    PUB_ART_URL,
    PUB_CAT_URL,
    SEARCH_URL,
    _results,
)


def _slugs(resp):
    return [r['slug'] for r in _results(resp)]


# ═══════════════════════════════════════════════════════════
#  Search
# ═══════════════════════════════════════════════════════════

class TestSearchVisibility(HelpTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.cat = self._make_category('qw-search-cat')
        # Visible everywhere
        self._make_article(self.cat, 'qw-both', title={'en': 'Zebrafish both'})
        # Dashboard-only article
        self._make_article(self.cat, 'qw-dash-only', title={'en': 'Zebrafish dashboard'},
                           show_on_public=False, show_in_dashboard=True)
        # Public-only article
        self._make_article(self.cat, 'qw-pub-only', title={'en': 'Zebrafish public'},
                           show_on_public=True, show_in_dashboard=False)

    def test_search_is_public_no_auth_required(self):
        resp = self.api_get(f'{SEARCH_URL}?q=zebrafish')  # no user → anonymous
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsInstance(resp.data, list)
        self.assertIn('qw-both', _slugs(resp))

    def test_search_without_flags_returns_everything_active(self):
        # Backwards-compatible default: no visibility filtering unless asked.
        resp = self.api_get(f'{SEARCH_URL}?q=zebrafish')
        slugs = _slugs(resp)
        self.assertIn('qw-both', slugs)
        self.assertIn('qw-dash-only', slugs)
        self.assertIn('qw-pub-only', slugs)

    def test_search_for_public_only(self):
        resp = self.api_get(f'{SEARCH_URL}?q=zebrafish&for_public=true')
        slugs = _slugs(resp)
        self.assertIn('qw-both', slugs)
        self.assertIn('qw-pub-only', slugs)
        self.assertNotIn('qw-dash-only', slugs)

    def test_search_for_dashboard_only(self):
        resp = self.api_get(f'{SEARCH_URL}?q=zebrafish&for_dashboard=true')
        slugs = _slugs(resp)
        self.assertIn('qw-both', slugs)
        self.assertIn('qw-dash-only', slugs)
        self.assertNotIn('qw-pub-only', slugs)

    def test_search_for_dashboard_wins_over_for_public(self):
        # The web dashboard HelpSearch sends both flags; dashboard must win.
        resp = self.api_get(f'{SEARCH_URL}?q=zebrafish&for_public=true&for_dashboard=true')
        slugs = _slugs(resp)
        self.assertIn('qw-dash-only', slugs)
        self.assertNotIn('qw-pub-only', slugs)

    def test_search_respects_category_dashboard_visibility(self):
        hidden_cat = self._make_category('qw-hidden-dash-cat', show_in_dashboard=False)
        self._make_article(hidden_cat, 'qw-in-hidden-cat', title={'en': 'Zebrafish hidden cat'},
                           show_in_dashboard=True)
        resp = self.api_get(f'{SEARCH_URL}?q=zebrafish&for_dashboard=true')
        self.assertNotIn('qw-in-hidden-cat', _slugs(resp))
        # ...but still found when no visibility flag is sent
        resp = self.api_get(f'{SEARCH_URL}?q=zebrafish')
        self.assertIn('qw-in-hidden-cat', _slugs(resp))

    def test_search_respects_category_public_visibility(self):
        hidden_cat = self._make_category('qw-hidden-pub-cat', show_on_public=False)
        self._make_article(hidden_cat, 'qw-in-hidden-pub-cat', title={'en': 'Zebrafish hidden pub'},
                           show_on_public=True)
        resp = self.api_get(f'{SEARCH_URL}?q=zebrafish&for_public=true')
        self.assertNotIn('qw-in-hidden-pub-cat', _slugs(resp))

    def test_search_localises_results(self):
        self._make_article(self.cat, 'qw-ka', title={'en': 'Zebrafish english', 'ka': 'ზებრა'})
        resp = self.api_get(f'{SEARCH_URL}?q=zebrafish&lang=ka')
        titles = {r['slug']: r['title'] for r in resp.data}
        self.assertEqual(titles['qw-ka'], 'ზებრა')

    def test_search_too_short_still_empty_list_anonymously(self):
        resp = self.api_get(f'{SEARCH_URL}?q=z')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, [])


# ═══════════════════════════════════════════════════════════
#  Visibility precedence on the viewsets
# ═══════════════════════════════════════════════════════════

class TestVisibilityPrecedence(HelpTestMixin, EchoDeskTenantTestCase):

    def test_categories_for_dashboard_wins_over_for_public(self):
        self._make_category('qw-cat-pub-only', show_on_public=True, show_in_dashboard=False)
        self._make_category('qw-cat-dash-only', show_on_public=False, show_in_dashboard=True)
        resp = self.api_get(f'{PUB_CAT_URL}?for_public=true&for_dashboard=true')
        slugs = _slugs(resp)
        self.assertIn('qw-cat-dash-only', slugs)
        self.assertNotIn('qw-cat-pub-only', slugs)

    def test_categories_for_public_alone_unchanged(self):
        self._make_category('qw-cat-pub-only2', show_on_public=True, show_in_dashboard=False)
        self._make_category('qw-cat-dash-only2', show_on_public=False, show_in_dashboard=True)
        resp = self.api_get(f'{PUB_CAT_URL}?for_public=true')
        slugs = _slugs(resp)
        self.assertIn('qw-cat-pub-only2', slugs)
        self.assertNotIn('qw-cat-dash-only2', slugs)

    def test_categories_list_is_paginated_with_int_article_count(self):
        cat = self._make_category('qw-count-cat')
        self._make_article(cat, 'qw-count-1')
        self._make_article(cat, 'qw-count-2', is_active=False)
        resp = self.api_get(f'{PUB_CAT_URL}?for_dashboard=true')
        self.assertIn('results', resp.data)
        row = next(r for r in resp.data['results'] if r['slug'] == 'qw-count-cat')
        self.assertEqual(row['article_count'], 1)
        self.assertIsInstance(row['article_count'], int)

    def test_categories_ordering_param_still_works(self):
        self._make_category('qw-ord-a', position=5)
        self._make_category('qw-ord-b', position=1)
        resp = self.api_get(f'{PUB_CAT_URL}?ordering=-position')
        slugs = _slugs(resp)
        self.assertLess(slugs.index('qw-ord-a'), slugs.index('qw-ord-b'))

    def test_articles_for_dashboard_wins_over_for_public(self):
        cat = self._make_category('qw-art-cat')
        self._make_article(cat, 'qw-art-dash-only', show_on_public=False, show_in_dashboard=True)
        self._make_article(cat, 'qw-art-pub-only', show_on_public=True, show_in_dashboard=False)
        resp = self.api_get(f'{PUB_ART_URL}?for_public=true&for_dashboard=true')
        slugs = _slugs(resp)
        self.assertIn('qw-art-dash-only', slugs)
        self.assertNotIn('qw-art-pub-only', slugs)

    def test_featured_for_dashboard_wins_over_for_public(self):
        cat = self._make_category('qw-feat-cat')
        self._make_article(cat, 'qw-feat-dash-only', is_featured=True,
                           show_on_public=False, show_in_dashboard=True)
        self._make_article(cat, 'qw-feat-pub-only', is_featured=True,
                           show_on_public=True, show_in_dashboard=False)
        resp = self.api_get(f'{PUB_ART_URL}featured/?for_public=true&for_dashboard=true')
        self.assertIsInstance(resp.data, list)
        slugs = [r['slug'] for r in resp.data]
        self.assertIn('qw-feat-dash-only', slugs)
        self.assertNotIn('qw-feat-pub-only', slugs)

    def test_category_detail_embedded_articles_precedence(self):
        cat = self._make_category('qw-detail-cat')
        self._make_article(cat, 'qw-det-dash-only', show_on_public=False, show_in_dashboard=True)
        self._make_article(cat, 'qw-det-pub-only', show_on_public=True, show_in_dashboard=False)
        resp = self.api_get(f'{PUB_CAT_URL}qw-detail-cat/?for_public=true&for_dashboard=true')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = [a['slug'] for a in resp.data['articles']]
        self.assertIn('qw-det-dash-only', slugs)
        self.assertNotIn('qw-det-pub-only', slugs)
        # Embedded rows are list-shaped (category_name / category_slug present)
        self.assertEqual(resp.data['articles'][0]['category_slug'], 'qw-detail-cat')
        self.assertEqual(resp.data['articles'][0]['category_name'], 'Getting Started')


# ═══════════════════════════════════════════════════════════
#  Query efficiency
# ═══════════════════════════════════════════════════════════

class TestQueryCounts(HelpTestMixin, EchoDeskTenantTestCase):

    def _count_queries(self, url):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.api_get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return len(ctx.captured_queries)

    def test_category_detail_has_no_per_article_query(self):
        cat = self._make_category('qw-n1-cat')
        self._make_article(cat, 'qw-n1-a')
        one = self._count_queries(f'{PUB_CAT_URL}qw-n1-cat/?for_dashboard=true')
        for i in range(5):
            self._make_article(cat, f'qw-n1-more-{i}')
        six = self._count_queries(f'{PUB_CAT_URL}qw-n1-cat/?for_dashboard=true')
        self.assertEqual(one, six)

    def test_category_list_query_count_is_flat(self):
        for i in range(3):
            cat = self._make_category(f'qw-flat-{i}')
            self._make_article(cat, f'qw-flat-art-{i}')
        three = self._count_queries(f'{PUB_CAT_URL}?for_dashboard=true')
        for i in range(3, 8):
            cat = self._make_category(f'qw-flat-{i}')
            self._make_article(cat, f'qw-flat-art-{i}')
        eight = self._count_queries(f'{PUB_CAT_URL}?for_dashboard=true')
        self.assertEqual(three, eight)

    def test_category_list_does_not_load_articles(self):
        cat = self._make_category('qw-noprefetch-cat')
        self._make_article(cat, 'qw-noprefetch-art')
        with CaptureQueriesContext(connection) as ctx:
            self.api_get(PUB_CAT_URL)
        article_selects = [
            q['sql'] for q in ctx.captured_queries
            if 'help_center_helparticle' in q['sql'] and q['sql'].lstrip().upper().startswith('SELECT')
            and 'COUNT(' not in q['sql'].upper()
        ]
        self.assertEqual(article_selects, [])


# ═══════════════════════════════════════════════════════════
#  Conditional GET on the article detail
# ═══════════════════════════════════════════════════════════

class TestArticleLastModified(HelpTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.cat = self._make_category('qw-lm-cat')
        self.article = self._make_article(self.cat, 'qw-lm-art')
        self.article.refresh_from_db()

    def _expected_header(self):
        return http_date(int(self.article.updated_at.timestamp()))

    def test_last_modified_header_from_updated_at(self):
        resp = self.api_get(f'{PUB_ART_URL}qw-lm-art/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp['Last-Modified'], self._expected_header())
        self.assertEqual(resp['Cache-Control'], 'no-cache')
        self.assertEqual(resp.data['slug'], 'qw-lm-art')

    def test_if_modified_since_current_returns_304(self):
        resp = self.api_get(f'{PUB_ART_URL}qw-lm-art/',
                            HTTP_IF_MODIFIED_SINCE=self._expected_header())
        self.assertEqual(resp.status_code, status.HTTP_304_NOT_MODIFIED)
        self.assertEqual(resp['Last-Modified'], self._expected_header())
        self.assertEqual(resp.content, b'')

    def test_if_modified_since_stale_returns_full_body(self):
        stale = http_date(int((self.article.updated_at - timedelta(days=1)).timestamp()))
        resp = self.api_get(f'{PUB_ART_URL}qw-lm-art/', HTTP_IF_MODIFIED_SINCE=stale)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['slug'], 'qw-lm-art')

    def test_if_modified_since_garbage_is_ignored(self):
        resp = self.api_get(f'{PUB_ART_URL}qw-lm-art/', HTTP_IF_MODIFIED_SINCE='not a date')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_and_featured_have_no_last_modified(self):
        resp = self.api_get(PUB_ART_URL)
        self.assertFalse(resp.has_header('Last-Modified'))
        resp = self.api_get(f'{PUB_ART_URL}featured/')
        self.assertFalse(resp.has_header('Last-Modified'))

    def test_detail_still_404_for_inactive(self):
        self._make_article(self.cat, 'qw-lm-inactive', is_active=False)
        resp = self.api_get(f'{PUB_ART_URL}qw-lm-inactive/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_detail_localised_guide_and_faq_shapes(self):
        self._make_article(
            self.cat, 'qw-lm-guide', content_type='guide',
            guide_steps=[{'step': 1, 'title': {'en': 'One', 'ka': 'ერთი'},
                          'content': {'en': '<p>a</p>'}, 'image': ''}],
            faq_items=[{'question': {'en': 'Q?'}, 'answer': {'en': '<p>A</p>'}}],
        )
        resp = self.api_get(f'{PUB_ART_URL}qw-lm-guide/?lang=ka')
        self.assertEqual(resp.data['guide_steps'], [
            {'step': 1, 'title': 'ერთი', 'content': '<p>a</p>', 'image': ''}
        ])
        self.assertEqual(resp.data['faq_items'], [{'question': 'Q?', 'answer': '<p>A</p>'}])
        self.assertIsInstance(resp.data['category'], dict)
        self.assertEqual(resp.data['category']['slug'], 'qw-lm-cat')


# ═══════════════════════════════════════════════════════════
#  OpenAPI schema
# ═══════════════════════════════════════════════════════════

class TestHelpSchema(EchoDeskTenantTestCase):
    """The generated client is built from this schema; keep it honest."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from drf_spectacular.generators import SchemaGenerator
        patterns = [path('api/help/', include('help_center.urls'))]
        cls.schema = SchemaGenerator(patterns=patterns).get_schema(request=None, public=True)
        cls.paths = cls.schema['paths']
        cls.components = cls.schema['components']['schemas']

    @staticmethod
    def _param_names(op):
        return {p['name']: p for p in op.get('parameters', [])}

    def _op(self, url, method='get'):
        return self.paths[url][method]

    def test_categories_list_params(self):
        params = self._param_names(self._op('/api/help/public/categories/'))
        for name in ('lang', 'for_dashboard', 'for_public', 'ordering', 'page', 'page_size'):
            self.assertIn(name, params, name)
        self.assertNotIn('search', params)
        self.assertEqual(params['for_dashboard']['schema']['type'], 'boolean')
        self.assertEqual(params['lang']['schema']['enum'], ['en', 'ka', 'ru'])

    def test_category_detail_params_and_articles_array(self):
        op = self._op('/api/help/public/categories/{slug}/')
        params = self._param_names(op)
        for name in ('slug', 'lang', 'for_dashboard', 'for_public'):
            self.assertIn(name, params, name)
        self.assertEqual(params['slug']['schema']['type'], 'string')
        detail = self.components['HelpCategoryDetail']
        self.assertEqual(detail['properties']['articles']['type'], 'array')
        self.assertEqual(detail['properties']['articles']['items']['$ref'],
                         '#/components/schemas/HelpArticleList')
        self.assertEqual(detail['properties']['article_count']['type'], 'integer')
        self.assertEqual(detail['properties']['name']['type'], 'string')

    def test_articles_list_params(self):
        params = self._param_names(self._op('/api/help/public/articles/'))
        for name in ('lang', 'for_dashboard', 'for_public', 'category', 'content_type',
                     'featured', 'ordering', 'page', 'page_size'):
            self.assertIn(name, params, name)
        self.assertEqual(set(params['content_type']['schema']['enum']),
                         {'video', 'article', 'guide', 'faq'})
        self.assertEqual(params['featured']['schema']['type'], 'boolean')
        self.assertNotIn('search', params)

    def test_article_detail_typed_arrays(self):
        op = self._op('/api/help/public/articles/{slug}/')
        params = self._param_names(op)
        for name in ('slug', 'lang', 'for_dashboard', 'for_public'):
            self.assertIn(name, params, name)
        self.assertIn('304', op['responses'])
        detail = self.components['HelpArticleDetail']
        self.assertEqual(detail['properties']['guide_steps']['type'], 'array')
        self.assertEqual(detail['properties']['guide_steps']['items']['$ref'],
                         '#/components/schemas/HelpGuideStep')
        self.assertEqual(detail['properties']['faq_items']['items']['$ref'],
                         '#/components/schemas/HelpFAQItem')
        self.assertEqual(detail['properties']['content']['type'], 'string')
        self.assertEqual(self.components['HelpGuideStep']['properties']['step']['type'], 'integer')
        self.assertEqual(self.components['HelpArticleList']['properties']['title']['type'], 'string')
        self.assertEqual(self.components['HelpArticleList']['properties']['category_name']['type'], 'string')

    def test_featured_is_bare_list(self):
        op = self._op('/api/help/public/articles/featured/')
        params = self._param_names(op)
        for name in ('lang', 'for_dashboard', 'for_public', 'category', 'content_type'):
            self.assertIn(name, params, name)
        body = op['responses']['200']['content']['application/json']['schema']
        self.assertEqual(body['type'], 'array')
        self.assertEqual(body['items']['$ref'], '#/components/schemas/HelpArticleList')

    def test_search_is_documented_bare_list(self):
        op = self._op('/api/help/public/search/')
        params = self._param_names(op)
        for name in ('q', 'lang', 'for_dashboard', 'for_public'):
            self.assertIn(name, params, name)
        # Optional on purpose: the TS generator emits required params after
        # optional ones (invalid TS); the view returns [] without q.
        self.assertFalse(params['q'].get('required', False))
        body = op['responses']['200']['content']['application/json']['schema']
        self.assertEqual(body['type'], 'array')
        self.assertEqual(body['items']['$ref'], '#/components/schemas/HelpArticleList')
        # Public endpoint: no auth requirement advertised
        self.assertIn({}, op.get('security', [{}]))

    def test_operation_ids_for_generated_client(self):
        self.assertEqual(self._op('/api/help/public/categories/')['operationId'],
                         'help_public_categories_list')
        self.assertEqual(self._op('/api/help/public/articles/')['operationId'],
                         'help_public_articles_list')
        self.assertEqual(self._op('/api/help/public/articles/{slug}/')['operationId'],
                         'help_public_articles_retrieve')
        # Bare-list endpoints are named *_list so the generated client reads
        # as a list call (they were *_retrieve before, typed as a single object).
        self.assertEqual(self._op('/api/help/public/articles/featured/')['operationId'],
                         'help_public_articles_featured_list')
        self.assertEqual(self._op('/api/help/public/search/')['operationId'],
                         'help_public_search_list')
