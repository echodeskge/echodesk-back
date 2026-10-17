"""Unified inbox list (GET /api/social/conversations/): shape, merge order,
filters and bounded query count (package p1)."""
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from tenant_schemas.utils import schema_context

from social_integrations.models import (
    AIConversationState, ConversationArchive, TelegramAccount, TelegramMessage,
    WidgetMessage, WidgetSession,
)
from social_integrations.tests.conftest import SocialIntegrationTestCase

URL = '/api/social/conversations/'


class InboxListTests(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='inbox-agent@test.com')
        self.other = self.create_user(email='inbox-other@test.com')
        self.admin = self.create_admin(email='inbox-admin@test.com')
        self.now = timezone.now()
        self.page = self.create_fb_connection(page_id='pg1', page_name='Page One')
        self.ig = self.create_ig_connection(instagram_account_id='ig1', username='brand')
        self.waba = self.create_wa_account(waba_id='waba1', phone_number='+15550001111')
        self.tg = TelegramAccount.objects.create(
            telegram_user_id=777, phone_number='+995599000000', username='tgsupport',
            session_string='s', is_active=True,
        )
        from widget_registry.models import WidgetConnection
        with schema_context('public'):
            self.wconn = WidgetConnection.objects.create(
                tenant_schema=connection.schema_name if connection.schema_name != 'public' else 'test',
                widget_token=f'tok-{self._next()}', label='Site chat',
            )

    def tearDown(self):
        from widget_registry.models import WidgetConnection
        with schema_context('public'):
            WidgetConnection.objects.filter(pk=self.wconn.pk).delete()
        super().tearDown()

    def _ago(self, minutes):
        return self.now - timedelta(minutes=minutes)

    def _fb(self, sender, minutes, **kw):
        return self.create_fb_message(page_connection=self.page, sender_id=sender,
                                      timestamp=self._ago(minutes), **kw)

    def _seed_all(self):
        self._fb('fbuser', 10, sender_name='Fb Customer', message_text='old fb')
        self._fb('fbuser', 1, sender_name='Fb Customer', message_text='new fb', is_from_page=False)
        self.create_ig_message(account_connection=self.ig, sender_id='iguser',
                               sender_username='ig_customer', timestamp=self._ago(2))
        # WhatsApp: customer message, then the business reply is newer.
        self.create_wa_message(business_account=self.waba, from_number='995500', contact_name='Wa Cust',
                               timestamp=self._ago(9), is_from_business=False)
        self.create_wa_message(business_account=self.waba, from_number='+15550001111', to_number='995500',
                               message_text='reply', timestamp=self._ago(3), is_from_business=True)
        TelegramMessage.objects.create(
            account=self.tg, message_id='777_1', telegram_msg_id=1, peer_id=4242, peer_name='Tg Cust',
            message_text='hi tg', timestamp=self._ago(4), is_from_business=False,
        )
        self.session = WidgetSession.objects.create(
            connection_id=self.wconn.id, session_id='sess-1', visitor_id='visitor-1',
            visitor_name='Web Visitor',
        )
        WidgetMessage.objects.create(session=self.session, message_id='w1', message_text='widget hi',
                                     is_from_visitor=True, timestamp=self._ago(5))

    def _ids(self, resp):
        return [r['conversation_id'] for r in resp.data['results']]

    def test_merges_all_platforms_newest_first(self):
        self._seed_all()
        resp = self.api_get(URL + '?platforms=facebook,instagram,whatsapp,widget,telegram', user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['count'], 5)
        self.assertEqual(self._ids(resp), [
            'fb_pg1_fbuser', 'ig_ig1_iguser', 'wa_waba1_995500', 'tg_777_4242',
            f'widget_{self.wconn.id}_sess-1',
        ])
        rows = {r['platform']: r for r in resp.data['results']}
        self.assertEqual(rows['facebook']['last_message']['text'], 'new fb')
        self.assertEqual(rows['facebook']['unread_count'], 2)
        self.assertEqual(rows['facebook']['sender_name'], 'Fb Customer')
        self.assertEqual(rows['whatsapp']['last_message']['text'], 'reply')
        self.assertTrue(rows['whatsapp']['last_message']['is_from_business'])
        self.assertEqual(rows['whatsapp']['sender_name'], 'Wa Cust')
        self.assertEqual(rows['whatsapp']['unread_count'], 1)
        self.assertEqual(rows['telegram']['sender_id'], '4242')
        self.assertEqual(rows['widget']['sender_id'], 'visitor-1')
        self.assertEqual(rows['widget']['account_name'], 'Site chat')
        self.assertIn('session_ended_by', rows['widget'])
        for r in resp.data['results']:
            for key in ('assigned_user_id', 'is_archived', 'archived_at', 'ai_state', 'session_ended_at'):
                self.assertIn(key, r)

    def test_pagination_pages_are_disjoint_and_count_total(self):
        for i in range(7):
            self._fb(f'u{i}', i + 1)
        first = self.api_get(URL + '?platforms=facebook&page_size=3', user=self.agent)
        second = self.api_get(URL + '?platforms=facebook&page_size=3&page=2', user=self.agent)
        third = self.api_get(URL + '?platforms=facebook&page_size=3&page=3', user=self.agent)
        self.assertEqual(first.data['count'], 7)
        self.assertEqual(self._ids(first), ['fb_pg1_u0', 'fb_pg1_u1', 'fb_pg1_u2'])
        self.assertEqual(self._ids(second), ['fb_pg1_u3', 'fb_pg1_u4', 'fb_pg1_u5'])
        self.assertEqual(self._ids(third), ['fb_pg1_u6'])
        self.assertIsNotNone(first.data['next'])
        self.assertIsNone(third.data['next'])
        self.assertIsNotNone(second.data['previous'])

    def test_bad_page_params_do_not_500(self):
        self._fb('u1', 1)
        resp = self.api_get(URL + '?platforms=facebook&page=abc&page_size=-4', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 1)

    def test_query_count_is_independent_of_conversation_count(self):
        self._seed_all()

        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                resp = self.api_get(URL + '?page_size=5', user=self.agent)
            self.assertEqual(resp.status_code, 200)
            return len(ctx.captured_queries)

        baseline = count_queries()
        for i in range(25):
            self._fb(f'bulk{i}', 20 + i)
            self.create_wa_message(business_account=self.waba, from_number=f'99577{i:04d}',
                                   timestamp=self._ago(30 + i))
            s = WidgetSession.objects.create(connection_id=self.wconn.id, session_id=f'bs{i}',
                                             visitor_id=f'bv{i}')
            WidgetMessage.objects.create(session=s, message_id=f'bw{i}', message_text='x',
                                         is_from_visitor=True, timestamp=self._ago(40 + i))
        # 75 more conversations: page 1 (every platform present) costs
        # exactly the same number of queries.
        with self.assertNumQueries(baseline):
            resp = self.api_get(URL + '?page_size=5', user=self.agent)
        self.assertEqual(resp.data['count'], 80)
        # A deeper page only touches a subset of the platforms.
        with CaptureQueriesContext(connection) as ctx:
            resp = self.api_get(URL + '?page_size=5&page=9', user=self.agent)
        self.assertEqual(len(resp.data['results']), 5)
        self.assertLessEqual(len(ctx.captured_queries), baseline)

    def test_archived_filter_and_widget_archive_by_session(self):
        self._seed_all()
        ConversationArchive.objects.create(platform='facebook', conversation_id='fbuser', account_id='pg1')
        ConversationArchive.objects.create(platform='widget', conversation_id='sess-1',
                                           account_id=str(self.wconn.id))
        active = self.api_get(URL + '?platforms=facebook,widget,telegram', user=self.agent)
        self.assertEqual(self._ids(active), ['tg_777_4242'])
        history = self.api_get(URL + '?platforms=facebook,widget,telegram&archived=true', user=self.agent)
        self.assertEqual(set(self._ids(history)), {'fb_pg1_fbuser', f'widget_{self.wconn.id}_sess-1'})
        self.assertTrue(all(r['is_archived'] for r in history.data['results']))

    def test_assigned_tab_and_hide_assigned_include_widget_visitor_key(self):
        self._seed_all()
        self.create_settings(chat_assignment_enabled=True, hide_assigned_chats=True)
        self.create_chat_assignment(user=self.agent, platform='widget', conversation_id='visitor-1',
                                    account_id=str(self.wconn.id), status='in_session')
        self.create_chat_assignment(user=self.other, platform='facebook', conversation_id='fbuser',
                                    account_id='pg1', status='active')
        mine = self.api_get(URL + '?assigned=true', user=self.agent)
        self.assertEqual(self._ids(mine), [f'widget_{self.wconn.id}_sess-1'])
        self.assertEqual(mine.data['results'][0]['assigned_user_id'], self.agent.id)

        visible = self.api_get(URL + '?platforms=facebook,widget', user=self.agent)
        self.assertNotIn('fb_pg1_fbuser', self._ids(visible))
        other_view = self.api_get(URL + '?platforms=facebook,widget', user=self.other)
        self.assertEqual(self._ids(other_view), ['fb_pg1_fbuser'])
        staff_view = self.api_get(URL + '?platforms=facebook,widget', user=self.admin)
        self.assertEqual(len(self._ids(staff_view)), 2)

    def test_widget_session_end_survives_enrichment(self):
        self._seed_all()
        self.session.ended_at = self.now
        self.session.ended_by = 'visitor'
        self.session.save()
        resp = self.api_get(URL + '?platforms=widget', user=self.agent)
        row = resp.data['results'][0]
        self.assertIsNotNone(row['session_ended_at'])
        self.assertEqual(row['session_ended_by'], 'visitor')

    def test_ai_state_for_widget_uses_session_key(self):
        self._seed_all()
        AIConversationState.objects.create(platform='widget', conversation_id='sess-1',
                                           account_id=str(self.wconn.id), mode='needs_human')
        resp = self.api_get(URL + '?platforms=widget', user=self.agent)
        self.assertEqual(resp.data['results'][0]['ai_state'], 'needs_human')

    def test_search_matches_any_message(self):
        self._seed_all()
        resp = self.api_get(URL + '?search=old fb', user=self.agent)
        self.assertEqual(self._ids(resp), ['fb_pg1_fbuser'])
        resp = self.api_get(URL + '?search=web visitor', user=self.agent)
        self.assertEqual(self._ids(resp), [f'widget_{self.wconn.id}_sess-1'])
        resp = self.api_get(URL + '?search=995500', user=self.agent)
        self.assertEqual(self._ids(resp), ['wa_waba1_995500'])

    def test_requires_feature(self):
        self._feature_patcher.stop()
        try:
            resp = self.api_get(URL, user=self.agent)
        finally:
            self._feature_patcher.start()
        self.assertEqual(resp.status_code, 403)
