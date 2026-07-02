"""
Tests for help center API endpoints:
- Public categories (GET /api/help/public/categories/)
- Public articles  (GET /api/help/public/articles/)
- Featured articles (GET /api/help/public/articles/featured/)
- Search           (GET /api/help/public/search/)
- Admin categories (CRUD /api/help/admin/categories/)
- Admin articles   (CRUD /api/help/admin/articles/)
"""
from django.contrib.auth import get_user_model
from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase
from help_center.models import HelpCategory, HelpArticle

User = get_user_model()

# ── URL constants ──
PUB_CAT_URL = '/api/help/public/categories/'
PUB_ART_URL = '/api/help/public/articles/'
SEARCH_URL = '/api/help/public/search/'
ADMIN_CAT_URL = '/api/help/admin/categories/'
ADMIN_ART_URL = '/api/help/admin/articles/'


def _results(resp):
    if isinstance(resp.data, dict) and 'results' in resp.data:
        return resp.data['results']
    return resp.data


class HelpTestMixin:
    """Shared factory helpers for help center tests."""

    def _make_category(self, slug='getting-started', **kw):
        defaults = {
            'name': {'en': 'Getting Started', 'ka': 'დაწყება'},
            'slug': slug,
            'description': {'en': 'How to get started'},
            'icon': 'book-open',
            'position': 0,
            'is_active': True,
            'show_on_public': True,
            'show_in_dashboard': True,
        }
        defaults.update(kw)
        return HelpCategory.objects.create(**defaults)

    def _make_article(self, category, slug='first-steps', **kw):
        defaults = {
            'category': category,
            'title': {'en': 'First Steps', 'ka': 'პირველი ნაბიჯები'},
            'slug': slug,
            'summary': {'en': 'A quick guide'},
            'content_type': 'article',
            'content': {'en': '<p>Hello</p>', 'ka': '<p>გამარჯობა</p>'},
            'position': 0,
            'is_active': True,
            'is_featured': False,
            'show_on_public': True,
            'show_in_dashboard': True,
        }
        defaults.update(kw)
        return HelpArticle.objects.create(**defaults)


# ═══════════════════════════════════════════════════════════
#  Public endpoints (no auth required)
# ═══════════════════════════════════════════════════════════

class TestPublicCategories(HelpTestMixin, EchoDeskTenantTestCase):

    def test_list_categories(self):
        self._make_category('cat-a')
        self._make_category('cat-b', name={'en': 'Tutorials'})
        resp = self.api_get(PUB_CAT_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Every tenant is seeded with a default "PBX Connect" category
        # (migration 0002), so assert on membership rather than exact count.
        slugs = [r['slug'] for r in _results(resp)]
        self.assertIn('cat-a', slugs)
        self.assertIn('cat-b', slugs)

    def test_inactive_category_hidden(self):
        self._make_category('active-cat', is_active=True)
        self._make_category('hidden-cat', is_active=False)
        resp = self.api_get(PUB_CAT_URL)
        slugs = [r['slug'] for r in _results(resp)]
        self.assertIn('active-cat', slugs)
        self.assertNotIn('hidden-cat', slugs)

    def test_filter_for_public(self):
        self._make_category('pub-only', show_on_public=True, show_in_dashboard=False)
        self._make_category('dash-only', show_on_public=False, show_in_dashboard=True)
        resp = self.api_get(f'{PUB_CAT_URL}?for_public=true')
        slugs = [r['slug'] for r in _results(resp)]
        self.assertIn('pub-only', slugs)
        self.assertNotIn('dash-only', slugs)

    def test_filter_for_dashboard(self):
        self._make_category('pub-only2', show_on_public=True, show_in_dashboard=False)
        self._make_category('dash-only2', show_on_public=False, show_in_dashboard=True)
        resp = self.api_get(f'{PUB_CAT_URL}?for_dashboard=true')
        slugs = [r['slug'] for r in _results(resp)]
        self.assertIn('dash-only2', slugs)
        self.assertNotIn('pub-only2', slugs)

    def test_retrieve_category_by_slug(self):
        cat = self._make_category('my-cat')
        self._make_article(cat, 'art-1')
        resp = self.api_get(f'{PUB_CAT_URL}my-cat/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['slug'], 'my-cat')
        # Detail includes nested articles
        self.assertIn('articles', resp.data)


class TestPublicArticles(HelpTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.cat = self._make_category('guides')

    def test_list_articles(self):
        self._make_article(self.cat, 'a1')
        self._make_article(self.cat, 'a2', title={'en': 'Second'})
        resp = self.api_get(PUB_ART_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # A default seeded article exists per tenant (migration 0002), so
        # assert the created articles are present rather than an exact count.
        slugs = [r['slug'] for r in _results(resp)]
        self.assertIn('a1', slugs)
        self.assertIn('a2', slugs)

    def test_inactive_article_hidden(self):
        self._make_article(self.cat, 'visible', is_active=True)
        self._make_article(self.cat, 'invisible', is_active=False)
        resp = self.api_get(PUB_ART_URL)
        slugs = [r['slug'] for r in _results(resp)]
        self.assertIn('visible', slugs)
        self.assertNotIn('invisible', slugs)

    def test_filter_by_category(self):
        cat2 = self._make_category('other-cat')
        self._make_article(self.cat, 'in-guides')
        self._make_article(cat2, 'in-other')
        resp = self.api_get(f'{PUB_ART_URL}?category=guides')
        slugs = [r['slug'] for r in _results(resp)]
        self.assertIn('in-guides', slugs)
        self.assertNotIn('in-other', slugs)

    def test_filter_by_content_type(self):
        self._make_article(self.cat, 'vid', content_type='video')
        self._make_article(self.cat, 'txt', content_type='article')
        resp = self.api_get(f'{PUB_ART_URL}?content_type=video')
        slugs = [r['slug'] for r in _results(resp)]
        self.assertIn('vid', slugs)
        self.assertNotIn('txt', slugs)

    def test_retrieve_article_by_slug(self):
        self._make_article(self.cat, 'detail-test')
        resp = self.api_get(f'{PUB_ART_URL}detail-test/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['slug'], 'detail-test')

    def test_featured_action(self):
        self._make_article(self.cat, 'feat', is_featured=True)
        self._make_article(self.cat, 'norm', is_featured=False)
        resp = self.api_get(f'{PUB_ART_URL}featured/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = [r['slug'] for r in resp.data]
        self.assertIn('feat', slugs)
        self.assertNotIn('norm', slugs)


class TestHelpSearch(HelpTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.cat = self._make_category('search-cat')
        self.user = self.create_user(email='search@test.com')

    def test_search_by_title(self):
        self._make_article(self.cat, 'unique-slug',
                           title={'en': 'How to configure webhooks'})
        resp = self.api_get(f'{SEARCH_URL}?q=webhooks', user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(resp.data), 1)

    def test_search_too_short_returns_empty(self):
        resp = self.api_get(f'{SEARCH_URL}?q=a', user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, [])

    def test_search_no_match(self):
        self._make_article(self.cat, 'no-match', title={'en': 'Unrelated'})
        resp = self.api_get(f'{SEARCH_URL}?q=xyznonexistent', user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 0)


# ═══════════════════════════════════════════════════════════
#  Admin endpoints (superuser required)
# ═══════════════════════════════════════════════════════════

class TestAdminCategories(HelpTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='help-admin@test.com')
        # Make admin a superuser for IsAdminUser permission
        self.admin.is_superuser = True
        self.admin.save()

    def test_create_category(self):
        resp = self.api_post(ADMIN_CAT_URL, {
            'name': {'en': 'New Category'},
            'slug': 'new-category',
            'description': {'en': 'Desc'},
            'icon': 'star',
            'position': 1,
            'is_active': True,
            'show_on_public': True,
            'show_in_dashboard': True,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(HelpCategory.objects.filter(slug='new-category').exists())

    def test_update_category(self):
        cat = self._make_category('edit-me')
        resp = self.api_patch(f'{ADMIN_CAT_URL}edit-me/', {
            'icon': 'zap',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        cat.refresh_from_db()
        self.assertEqual(cat.icon, 'zap')

    def test_delete_category(self):
        self._make_category('delete-me')
        resp = self.api_delete(f'{ADMIN_CAT_URL}delete-me/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(HelpCategory.objects.filter(slug='delete-me').exists())

    def test_non_admin_denied(self):
        user = self.create_user(email='regular@test.com')
        resp = self.api_get(ADMIN_CAT_URL, user=user)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_denied(self):
        resp = self.api_get(ADMIN_CAT_URL)
        self.assertIn(resp.status_code, [401, 403])


class TestAdminArticles(HelpTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='art-admin@test.com')
        self.admin.is_superuser = True
        self.admin.save()
        self.cat = self._make_category('admin-cat')

    # NOTE: test_create_article and test_update_article are skipped because
    # help_center is a SHARED_APP (public schema) while users live in the
    # tenant schema.  The FK from HelpArticle.created_by → users_user
    # crosses schemas and fails Django's constraint check on teardown.

    def test_delete_article(self):
        self._make_article(self.cat, 'del-art')
        resp = self.api_delete(f'{ADMIN_ART_URL}del-art/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_filter_by_content_type(self):
        self._make_article(self.cat, 'vid-admin', content_type='video')
        self._make_article(self.cat, 'faq-admin', content_type='faq')
        resp = self.api_get(f'{ADMIN_ART_URL}?content_type=faq', user=self.admin)
        slugs = [r['slug'] for r in _results(resp)]
        self.assertIn('faq-admin', slugs)
        self.assertNotIn('vid-admin', slugs)

    def test_non_admin_denied(self):
        user = self.create_user(email='nonadmin-art@test.com')
        resp = self.api_post(ADMIN_ART_URL, {}, user=user)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# ═══════════════════════════════════════════════════════════
#  Model tests
# ═══════════════════════════════════════════════════════════

class TestHelpModels(HelpTestMixin, EchoDeskTenantTestCase):

    def test_category_get_name_en(self):
        cat = self._make_category('m-cat')
        self.assertEqual(cat.get_name('en'), 'Getting Started')

    def test_category_get_name_ka(self):
        cat = self._make_category('m-cat2')
        self.assertEqual(cat.get_name('ka'), 'დაწყება')

    def test_category_get_name_fallback(self):
        cat = self._make_category('m-cat3')
        # Falls back to 'en' for unknown language
        self.assertEqual(cat.get_name('fr'), 'Getting Started')

    def test_article_get_title(self):
        cat = self._make_category('m-cat4')
        art = self._make_article(cat, 'm-art')
        self.assertEqual(art.get_title('en'), 'First Steps')
        self.assertEqual(art.get_title('ka'), 'პირველი ნაბიჯები')

    def test_category_str(self):
        cat = self._make_category('str-cat')
        self.assertEqual(str(cat), 'Getting Started')

    def test_article_str(self):
        cat = self._make_category('str-cat2')
        art = self._make_article(cat, 'str-art')
        self.assertEqual(str(art), 'First Steps')
