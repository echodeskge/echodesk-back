"""
Sentry ECHODESK-BACKEND-89 / 8A / 7W / 83: slow conversation COUNT in the
unified inbox (GET /api/social/conversations/).

Every poll ran ``COUNT(*)`` over ``id IN (SELECT DISTINCT ON (page, sender)
… ORDER BY page, sender, timestamp DESC, id DESC)``: a scan of every
Facebook message, with an extra sort step the (page, sender, -timestamp)
index could not serve. Now the head scan is scoped to the tenant's pages and
ordered like the index, and the total is only counted when a platform fills
the requested page — at most once per 30 s per user.
"""
from datetime import timedelta

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from social_integrations.inbox_list import COUNT_CACHE_TTL
from social_integrations.tests.conftest import SocialIntegrationTestCase

URL = '/api/social/conversations/?platforms=facebook'


class InboxCountTests(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.agent = self.create_user(email='count-agent@test.com')
        self.page = self.create_fb_connection(page_id='pgc', page_name='Counted')
        self.now = timezone.now()

    def _fb(self, sender, minutes_ago):
        return self.create_fb_message(page_connection=self.page, sender_id=sender,
                                      timestamp=self.now - timedelta(minutes=minutes_ago))

    def _get(self, query=''):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.api_get(URL + query, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp, [q['sql'] for q in ctx.captured_queries]

    @staticmethod
    def _counts(sqls):
        return [s for s in sqls if s.lstrip().upper().startswith('SELECT COUNT(')]

    def test_short_list_is_counted_from_the_rows_without_a_count_query(self):
        for i in range(3):
            self._fb(f'u{i}', i)
        resp, sqls = self._get('&page_size=50')
        self.assertEqual(resp.data['count'], 3)
        self.assertEqual(len(resp.data['results']), 3)
        self.assertIsNone(resp.data['next'])
        self.assertEqual(self._counts(sqls), [])

    def test_full_page_counts_once_and_reuses_the_total(self):
        for i in range(3):
            self._fb(f'u{i}', i)
        resp, sqls = self._get('&page_size=2')
        self.assertEqual(resp.data['count'], 3)
        self.assertEqual(len(resp.data['results']), 2)
        self.assertIsNotNone(resp.data['next'])
        self.assertEqual(len(self._counts(sqls)), 1)

        # Same user, same filters within the TTL: no COUNT at all.
        resp, sqls = self._get('&page_size=2')
        self.assertEqual(resp.data['count'], 3)
        self.assertEqual(self._counts(sqls), [])

        # A conversation that arrives meanwhile shows up in the rows at once;
        # the total catches up when the cached count expires.
        self._fb('u-new', 0)
        resp, _ = self._get('&page_size=2')
        self.assertEqual(resp.data['results'][0]['sender_id'], 'u-new')
        self.assertEqual(resp.data['count'], 3)
        cache.clear()
        resp, _ = self._get('&page_size=2')
        self.assertEqual(resp.data['count'], 4)
        self.assertGreater(COUNT_CACHE_TTL, 0)

    def test_total_is_cached_per_user_and_filter(self):
        for i in range(3):
            self._fb(f'u{i}', i)
        self._get('&page_size=2')
        other = self.create_user(email='count-other@test.com')
        with CaptureQueriesContext(connection) as ctx:
            resp = self.api_get(URL + '&page_size=2', user=other)
        self.assertEqual(resp.data['count'], 3)
        self.assertEqual(len(self._counts([q['sql'] for q in ctx.captured_queries])), 1)
        # A different search is a different total.
        resp, sqls = self._get('&page_size=2&search=hello')
        self.assertEqual(resp.data['count'], 3)
        self.assertEqual(len(self._counts(sqls)), 1)

    def test_facebook_head_scan_is_scoped_to_pages_and_index_ordered(self):
        self._fb('u0', 1)
        _, sqls = self._get('&page_size=50')
        # The head subquery (alias U0); _build_facebook's per-page DISTINCT ON
        # over the listed senders is a different, bounded query.
        heads = [s for s in sqls if 'SELECT DISTINCT ON (U0.' in s and 'facebookmessage' in s]
        self.assertTrue(heads, sqls)
        for sql in heads:
            self.assertIn('"page_connection_id" IN (', sql)
            self.assertIn('U0."timestamp" DESC', sql)
            self.assertNotIn('U0."id" DESC', sql)

    def test_tenant_without_pages_never_scans_messages(self):
        self.page.delete()
        resp, sqls = self._get('&page_size=50')
        self.assertEqual(resp.data['count'], 0)
        self.assertEqual(resp.data['results'], [])
        self.assertFalse([s for s in sqls if 'social_integrations_facebookmessage' in s], sqls)
