"""Review fixes on the p1 social parity package (parity-fix/social)."""
import json
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from tenant_schemas.utils import schema_context

from social_integrations.models import (
    ChatAssignment, ChatRating, FacebookMessage, WhatsAppMessageTemplate, WidgetMessage, WidgetSession,
)
from social_integrations.tests.conftest import SocialIntegrationTestCase
from users.models import Notification

LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                            'LOCATION': 'fxsocial'}}
IN_MEMORY_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}


def _widget_chat(connection_id, session_id, visitor_id, n=1, visitor_name='Nino'):
    session = WidgetSession.objects.create(
        connection_id=connection_id, session_id=session_id, visitor_id=visitor_id, visitor_name=visitor_name,
    )
    base = timezone.now() - timedelta(minutes=10)
    for i in range(n):
        WidgetMessage.objects.create(
            session=session, message_id=f'{session_id}-m{i}', message_text=f'{session_id} text {i}',
            is_from_visitor=True, timestamp=base + timedelta(seconds=i),
        )
    return session


# ── 1. Widget transcript: chat key required, default backends, page size ───

class WidgetTranscriptTests(SocialIntegrationTestCase):
    URL = '/api/widget/admin/messages/'

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='fx-wt-agent@test.com')
        self.a = _widget_chat(7, 'sess-a', 'vis-a', n=30)
        self.b = _widget_chat(7, 'sess-b', 'vis-b', n=3)

    def test_list_without_a_chat_key_is_400_not_every_message(self):
        resp = self.api_get(self.URL, user=self.agent)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn('session_id', resp.data['error'])
        self.assertNotIn('results', resp.data)
        # blank values do not count
        self.assertEqual(self.api_get(self.URL + '?session_id=%20', user=self.agent).status_code, 400)

    def test_retrieve_still_works_without_session_id(self):
        msg = WidgetMessage.objects.filter(session=self.b).first()
        resp = self.api_get(f'{self.URL}{msg.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['message_id'], msg.message_id)

    def test_default_page_size_is_20_and_500_is_allowed(self):
        resp = self.api_get(self.URL + '?session_id=sess-a', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 30)
        self.assertEqual(len(resp.data['results']), 20)
        self.assertEqual(resp.data['results'][0]['message_text'], 'sess-a text 0')
        resp = self.api_get(self.URL + '?session_id=sess-a&page_size=500', user=self.agent)
        self.assertEqual(len(resp.data['results']), 30)
        resp = self.api_get(self.URL + '?session_id=sess-a&page_size=5000', user=self.agent)
        self.assertEqual(len(resp.data['results']), 30)  # capped at 500, not an error

    def test_visitor_id_and_conversation_id_select_the_chat(self):
        resp = self.api_get(self.URL + '?visitor_id=vis-b', user=self.agent)
        self.assertEqual(resp.data['count'], 3)
        resp = self.api_get(self.URL + '?conversation_id=widget_7_sess-b', user=self.agent)
        self.assertEqual(resp.data['count'], 3)
        resp = self.api_get(self.URL + '?conversation_id=vis-b', user=self.agent)
        self.assertEqual(resp.data['count'], 3)
        # list id with the wrong connection matches nothing
        resp = self.api_get(self.URL + '?conversation_id=widget_8_sess-b', user=self.agent)
        self.assertEqual(resp.data['count'], 0)

    def test_search_filter_is_back(self):
        resp = self.api_get(self.URL + '?session_id=sess-a&search=text%2029', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([m['message_text'] for m in resp.data['results']], ['sess-a text 29'])

    def test_schema_keeps_search_and_ordering_params(self):
        from drf_spectacular.generators import SchemaGenerator
        schema = SchemaGenerator().get_schema(request=None, public=True)
        params = {p['name'] for p in schema['paths']['/api/widget/admin/messages/']['get']['parameters']}
        self.assertTrue({'search', 'ordering', 'session_id', 'visitor_id', 'conversation_id',
                         'page', 'page_size'} <= params, params)


# ── 2. Widget connection writes: staff / superuser bypass ─────────────────

class WidgetConnectionPermissionTests(SocialIntegrationTestCase):
    URL = '/api/widget/admin/connections/'

    def _create(self, user):
        return self.api_post(self.URL, {'label': 'Site', 'allowed_origins': []}, user=user)

    def test_plain_agent_cannot_write(self):
        agent = self.create_user(email='fx-wc-agent@test.com')
        self.assertEqual(self._create(agent).status_code, 403)
        self.assertEqual(self.api_get(self.URL, user=agent).status_code, 200)  # reads need the feature only

    def test_staff_without_admin_role_can_write(self):
        staff = self.create_user(email='fx-wc-staff@test.com', role='agent', is_staff=True)
        resp = self._create(staff)
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_superuser_can_write_even_without_the_feature(self):
        su = self.create_user(email='fx-wc-su@test.com', role='agent', is_superuser=True)
        self._feature_patcher.stop()
        try:
            resp = self._create(su)
        finally:
            self._feature_patcher.start()
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_staff_bypass_does_not_cover_anonymous(self):
        resp = self.client.post(self.URL, {'label': 'x'}, format='json', HTTP_HOST='tenant.test.com')
        self.assertIn(resp.status_code, (401, 403))

    def test_admin_role_and_permission_still_work(self):
        admin_role = self.create_user(email='fx-wc-role@test.com', role='admin')
        self.assertEqual(self._create(admin_role).status_code, 201)
        flagged = self.create_user(email='fx-wc-flag@test.com', can_manage_social_connections=True)
        self.assertEqual(self._create(flagged).status_code, 201)


# ── 3. WebSocket: hide_assigned covers every chat-scoped frame ─────────────

class MessagesConsumerIsolationTests(SocialIntegrationTestCase):
    """Uses the same harness as test_p1_social.MessagesConsumerTests;
    _fetch_holder is patched with an in-memory assignment table because the
    consumer's DB thread cannot see the test transaction."""

    def _run(self, user, access, holders, frames, client_frames=()):
        from channels.layers import get_channel_layer
        from channels.testing import WebsocketCommunicator
        from social_integrations.consumers import MessagesConsumer

        async def load_access(consumer_self):
            return access

        async def fetch_hide(consumer_self):
            return access['hide_assigned']

        lookups = []

        async def fetch_holder(consumer_self, platform, conversation_id, account_id):
            lookups.append((platform, conversation_id, account_id))
            return holders.get(conversation_id)

        comm = WebsocketCommunicator(MessagesConsumer.as_asgi(), '/ws/messages/test/')
        comm.scope['user'] = user
        comm.scope['url_route'] = {'kwargs': {'tenant_schema': 'test'}}
        patches = [
            patch.object(MessagesConsumer, '_load_access', load_access),
            patch.object(MessagesConsumer, '_fetch_hide_assigned', fetch_hide),
            patch.object(MessagesConsumer, '_fetch_holder', fetch_holder),
        ]

        async def scenario():
            connected, _ = await comm.connect()
            assert connected
            await comm.receive_json_from()
            replies = []
            for frame in client_frames:
                await comm.send_json_to(frame)
                replies.append(await comm.receive_json_from())
            layer = get_channel_layer()
            for group, frame in frames:
                await layer.group_send(group, frame)
            received = []
            while not await comm.receive_nothing(timeout=0.2):
                received.append(await comm.receive_json_from())
            await comm.disconnect()
            return replies, received

        for p in patches:
            p.start()
        try:
            with override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYERS):
                replies, received = async_to_sync(scenario)()
        finally:
            for p in patches:
                p.stop()
        return replies, received, lookups

    @staticmethod
    def chat_frames(conv):
        base = {'platform': 'facebook', 'conversation_id': conv, 'account_id': 'page1'}
        return [
            ('messages_test', dict(base, type='read_state_update', unread_count=0)),
            ('messages_test', dict(base, type='archive_update', archived=True)),
            ('messages_test', dict(base, type='ai_state_update', mode='off')),
            ('messages_test', dict(base, type='message_status_update', message_ids=[1], status='read')),
            ('messages_test', dict(base, type='reaction_update', message_id='m', reaction_emoji='x')),
        ]

    def test_reproduction_other_agents_chat_frames_are_not_delivered(self):
        me = self.create_user(email='fx-ws-me@test.com')
        other_id = me.id + 1000
        access = {'has_feature': True, 'is_privileged': False, 'hide_assigned': True}
        holders = {'theirs': other_id, 'mine': me.id}
        frames = self.chat_frames('theirs') + self.chat_frames('mine') + self.chat_frames('free')
        _, received, _ = self._run(me, access, holders, frames)
        self.assertEqual({f['conversation_id'] for f in received}, {'mine', 'free'})
        self.assertEqual(len(received), 10)

    def test_assignment_frames(self):
        me = self.create_user(email='fx-ws-assign@test.com')
        other_id = me.id + 1000
        access = {'has_feature': True, 'is_privileged': False, 'hide_assigned': True}

        def frame(conv, assignee, status='in_session', by=None):
            return ('messages_test', {'type': 'assignment_update', 'platform': 'facebook',
                                      'conversation_id': conv, 'account_id': 'p',
                                      'assigned_user_id': assignee, 'status': status, 'by_user_id': by})

        frames = [
            frame('to-other', other_id),                   # hidden
            frame('to-me', me.id),                         # delivered
            frame('released', None, status=None),          # unassigned: delivered
            frame('i-transferred', other_id, by=me.id),    # my own transfer: delivered
        ]
        _, received, _ = self._run(me, access, {}, frames)
        self.assertEqual([f['conversation_id'] for f in received], ['to-me', 'released', 'i-transferred'])

    def test_new_message_without_assignee_key_is_resolved(self):
        me = self.create_user(email='fx-ws-out@test.com')
        access = {'has_feature': True, 'is_privileged': False, 'hide_assigned': True}
        outbound = {'type': 'new_message', 'message': {'platform': 'facebook', 'message_text': 'agent reply'},
                    'conversation_id': 'theirs', 'timestamp': 't'}
        inbound_unassigned = dict(outbound, conversation_id='free', assigned_user_id=None)
        _, received, lookups = self._run(
            me, access, {'theirs': me.id + 1000},
            [('messages_test', outbound), ('messages_test', inbound_unassigned)],
        )
        self.assertEqual([f['conversation_id'] for f in received], ['free'])
        # an explicit "unassigned" (None) needs no lookup
        self.assertEqual(lookups, [('facebook', 'theirs', None)])

    def test_subscribe_conversation_to_other_agents_chat_is_refused(self):
        me = self.create_user(email='fx-ws-sub@test.com')
        access = {'has_feature': True, 'is_privileged': False, 'hide_assigned': True}
        group_frame = {'type': 'new_message', 'message': {'message_text': 'secret'},
                       'conversation_id': 'theirs', 'timestamp': 't'}
        replies, received, _ = self._run(
            me, access, {'theirs': me.id + 1000},
            [('conversation_test_theirs', group_frame)],
            client_frames=[{'type': 'subscribe_conversation', 'conversation_id': 'theirs'},
                           {'type': 'subscribe_conversation', 'conversation_id': 'free'}],
        )
        self.assertEqual(replies[0]['type'], 'error')
        self.assertEqual(replies[0]['code'], 'FORBIDDEN')
        self.assertEqual(replies[1], {'type': 'subscription', 'status': 'subscribed', 'conversation_id': 'free'})
        self.assertEqual(received, [])

    def test_staff_and_hide_off_get_everything_without_lookups(self):
        me = self.create_user(email='fx-ws-staff@test.com')
        holders = {'theirs': me.id + 1000}
        for access in ({'has_feature': True, 'is_privileged': True, 'hide_assigned': True},
                       {'has_feature': True, 'is_privileged': False, 'hide_assigned': False}):
            replies, received, lookups = self._run(
                me, access, holders, self.chat_frames('theirs'),
                client_frames=[{'type': 'subscribe_conversation', 'conversation_id': 'theirs'}],
            )
            self.assertEqual(replies[0]['type'], 'subscription')
            self.assertEqual(len(received), 5)
            self.assertEqual(lookups, [])


class ChatHolderLookupTests(SocialIntegrationTestCase):

    def test_holder_resolution(self):
        from social_integrations.consumers import chat_holder_id, split_chat_id
        other = self.create_user(email='fx-holder@test.com')
        self.create_chat_assignment(user=other, platform='facebook', conversation_id='psid1',
                                    account_id='page1', status='in_session')
        self.create_chat_assignment(user=other, platform='whatsapp', conversation_id='995555',
                                    account_id='waba1', status='active')
        self.create_chat_assignment(user=other, platform='facebook', conversation_id='done',
                                    account_id='page1', status='completed')
        WidgetSession.objects.create(connection_id=3, session_id='ws-1', visitor_id='wv-1')
        self.create_chat_assignment(user=other, platform='widget', conversation_id='wv-1',
                                    account_id='3', status='active')

        self.assertEqual(chat_holder_id('facebook', 'psid1', 'page1'), other.id)
        self.assertIsNone(chat_holder_id('facebook', 'psid1', 'page2'))
        self.assertEqual(chat_holder_id(None, 'psid1', None), other.id)
        self.assertEqual(chat_holder_id(None, 'fb_page1_psid1', None), other.id)
        self.assertEqual(chat_holder_id('whatsapp', '+995555', 'waba1'), other.id)
        self.assertIsNone(chat_holder_id('facebook', 'done', 'page1'))
        self.assertEqual(chat_holder_id('widget', 'ws-1', '3'), other.id)  # session id → visitor key
        self.assertEqual(chat_holder_id(None, 'widget_3_ws-1', None), other.id)
        self.assertIsNone(chat_holder_id(None, '', None))
        self.assertEqual(split_chat_id('wa_waba1_995555'), ('whatsapp', 'waba1', '995555'))
        self.assertEqual(split_chat_id('plainid'), (None, None, 'plainid'))


class TypingConsumerIsolationTests(SocialIntegrationTestCase):

    def _run(self, user, hidden):
        from channels.layers import get_channel_layer
        from channels.testing import WebsocketCommunicator
        from social_integrations.consumers import TypingConsumer

        async def hidden_for_me(consumer_self):
            return hidden

        comm = WebsocketCommunicator(TypingConsumer.as_asgi(), '/ws/typing/test/psid1/')
        comm.scope['user'] = user
        comm.scope['url_route'] = {'kwargs': {'tenant_schema': 'test', 'conversation_id': 'psid1'}}

        async def scenario():
            connected, _ = await comm.connect()
            assert connected
            hello = await comm.receive_json_from()
            await comm.send_json_to({'type': 'typing_start'})  # must not crash when hidden
            await get_channel_layer().group_send('typing_test_psid1', {
                'type': 'user_started_typing', 'user_id': 'someone', 'user_name': 'B', 'timestamp': 1,
            })
            received = []
            while not await comm.receive_nothing(timeout=0.2):
                received.append(await comm.receive_json_from())
            await comm.disconnect()
            return hello, received

        with patch.object(TypingConsumer, '_hidden_for_me', hidden_for_me), \
                override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYERS):
            return async_to_sync(scenario)()

    def test_hidden_chat_gets_no_typing_frames(self):
        user = self.create_user(email='fx-typing@test.com')
        hello, received = self._run(user, hidden=True)
        self.assertEqual(hello['type'], 'connection')
        self.assertEqual(received, [])

    def test_visible_chat_gets_typing_frames(self):
        user = self.create_user(email='fx-typing2@test.com')
        _, received = self._run(user, hidden=False)
        self.assertEqual([f['type'] for f in received], ['typing_start'])

    def test_hidden_for_me_rule(self):
        from social_integrations.consumers import TypingConsumer
        me = self.create_user(email='fx-typing-me@test.com')
        other = self.create_user(email='fx-typing-other@test.com')
        staff = self.create_user(email='fx-typing-staff@test.com', is_staff=True)
        self.create_chat_assignment(user=other, platform='facebook', conversation_id='psid9',
                                    account_id='p', status='active')

        def hidden(user, conv):
            consumer = TypingConsumer()
            consumer.tenant_schema = 'test'
            consumer.user = user
            consumer.conversation_id = conv
            return TypingConsumer.__dict__['_hidden_for_me'].func(consumer)

        self.assertFalse(hidden(me, 'psid9'))  # hide_assigned off
        self.create_settings(hide_assigned_chats=True)
        self.assertTrue(hidden(me, 'psid9'))
        self.assertTrue(hidden(me, 'fb_p_psid9'))
        self.assertFalse(hidden(other, 'psid9'))
        self.assertFalse(hidden(staff, 'psid9'))
        self.assertFalse(hidden(me, 'unassigned'))


# ── 4. Template status webhook: owner resolved by WABA id, cached ──────────

@override_settings(CACHES=LOCMEM_CACHE)
class TemplateStatusTenantResolutionTests(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()
        self.creator = self.create_user(email='fx-tpl@test.com')
        self.waba = self.create_wa_account(waba_id='fx-waba')
        self.template = WhatsAppMessageTemplate.objects.create(
            business_account=self.waba, template_id='777', name='fx_tpl', language='en_US',
            status='PENDING', created_by=self.creator,
        )

    def _value(self, event='APPROVED'):
        return {'event': event, 'message_template_id': 777, 'message_template_name': 'fx_tpl',
                'message_template_language': 'en_US', 'reason': 'NONE'}

    def test_resolves_owner_once_and_caches_it(self):
        from social_integrations import views
        calls = []
        real = views._schema_has_waba

        def spy(schema_name, waba_id):
            calls.append(schema_name)
            return real(schema_name, waba_id)

        with patch.object(views, '_schema_has_waba', side_effect=spy):
            self.assertEqual(views.resolve_tenant_by_waba_id('fx-waba'), 'test')
            first = list(calls)
            calls.clear()
            self.assertEqual(views.resolve_tenant_by_waba_id('fx-waba'), 'test')
        self.assertEqual(first, ['test'])  # the WhatsApp route tenant is tried first
        self.assertEqual(calls, ['test'])   # cached: only the cached schema is re-checked

    def test_unknown_waba_is_negative_cached(self):
        from social_integrations import views
        self.assertIsNone(views.resolve_tenant_by_waba_id('nobody'))
        with patch.object(views, '_schema_has_waba') as spy:
            self.assertIsNone(views.resolve_tenant_by_waba_id('nobody'))
        spy.assert_not_called()
        with patch('users.notification_utils.social_admin_recipients', return_value=[]):
            self.assertEqual(views.handle_whatsapp_template_status_update('nobody', self._value()), 0)

    def test_stale_cache_entry_heals(self):
        from django.core.cache import cache
        from social_integrations import views
        cache.set(views._waba_cache_key('fx-waba'), 'gone_tenant', 60)
        self.assertEqual(views.resolve_tenant_by_waba_id('fx-waba'), 'test')
        self.assertEqual(cache.get(views._waba_cache_key('fx-waba')), 'test')

    def test_update_does_not_scan_every_tenant(self):
        from social_integrations import views
        from tenants.models import Tenant
        with patch('users.notification_utils.social_admin_recipients', return_value=[]):
            self.assertEqual(views.handle_whatsapp_template_status_update('fx-waba', self._value()), 1)
            self.template.refresh_from_db()
            self.assertEqual(self.template.status, 'APPROVED')
            # Second event: owner comes from the cache, the tenant list is never read.
            with patch.object(Tenant.objects, 'exclude', side_effect=AssertionError('tenant scan')):
                self.assertEqual(views.handle_whatsapp_template_status_update('fx-waba', self._value('PAUSED')), 1)
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, 'PAUSED')
        self.assertTrue(Notification.objects.filter(notification_type='whatsapp_template_status',
                                                    user=self.creator).exists())

    def test_webhook_end_to_end(self):
        body = {'object': 'whatsapp_business_account', 'entry': [{'id': 'fx-waba', 'changes': [{
            'field': 'message_template_status_update', 'value': self._value('REJECTED')}]}]}
        with patch('users.notification_utils.social_admin_recipients', return_value=[]):
            resp = self.client.post('/api/social/whatsapp/webhook/', data=json.dumps(body),
                                    content_type='application/json', HTTP_HOST='tenant.test.com')
        self.assertEqual(json.loads(resp.content)['templates_updated'], 1)
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, 'REJECTED')


# ── 5. Rating sessions: DB paging + names keyed by (account, sender) ───────

class RatingSessionsPagingTests(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.staff = self.create_admin(email='fx-rs-staff@test.com')
        self.agent = self.create_user(email='fx-rs-agent@test.com')
        self.url = f'/api/social/rating-statistics/user/{self.agent.id}/'

    def test_same_sender_on_two_pages_gets_its_own_pages_name(self):
        page_a = self.create_fb_connection(page_id='fx-page-a')
        page_b = self.create_fb_connection(page_id='fx-page-b')
        self.create_fb_message(page_connection=page_a, sender_id='shared', sender_name='Name on A')
        self.create_fb_message(page_connection=page_b, sender_id='shared', sender_name='Name on B')
        ra = self.create_chat_rating(rated_user=self.agent, platform='facebook',
                                     conversation_id='shared', account_id='fx-page-a')
        rb = self.create_chat_rating(rated_user=self.agent, platform='facebook',
                                     conversation_id='shared', account_id='fx-page-b')
        rc = self.create_chat_rating(rated_user=self.agent, platform='facebook',
                                     conversation_id='shared', account_id='fx-page-none')
        resp = self.api_get(self.url, user=self.staff)
        names = {s['id']: s['customer_name'] for s in resp.data['sessions']}
        self.assertEqual(names[ra.id], 'Name on A')
        self.assertEqual(names[rb.id], 'Name on B')
        self.assertIn(names[rc.id], ('Name on A', 'Name on B'))  # no own-page message: newest anywhere

    def test_widget_names_keyed_by_connection(self):
        WidgetSession.objects.create(connection_id=1, session_id='fxw-1', visitor_id='fxv', visitor_name='On 1')
        WidgetSession.objects.create(connection_id=2, session_id='fxw-2', visitor_id='fxv', visitor_name='On 2')
        r1 = self.create_chat_rating(rated_user=self.agent, platform='widget', conversation_id='fxv', account_id='1')
        r2 = self.create_chat_rating(rated_user=self.agent, platform='widget', conversation_id='fxv', account_id='2')
        names = {s['id']: s['customer_name'] for s in self.api_get(self.url, user=self.staff).data['sessions']}
        self.assertEqual((names[r1.id], names[r2.id]), ('On 1', 'On 2'))

    def test_paging_happens_in_the_db(self):
        page = self.create_fb_connection(page_id='fx-page-p')
        now = timezone.now()
        ids = []
        for i in range(12):
            sid = f'fxp{i}'
            self.create_fb_message(page_connection=page, sender_id=sid, sender_name=f'Cust {i}')
            r = self.create_chat_rating(rated_user=self.agent, platform='facebook',
                                        conversation_id=sid, account_id='fx-page-p', rating=4)
            ChatRating.objects.filter(id=r.id).update(created_at=now - timedelta(minutes=i))
            ids.append(r.id)  # ids[0] newest

        resp = self.api_get(self.url + '?page=2&page_size=5', user=self.staff)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([s['id'] for s in resp.data['sessions']], ids[5:10])
        self.assertEqual(resp.data['total_sessions'], 12)
        self.assertTrue(resp.data['has_next'])
        self.assertEqual(resp.data['sessions'][0]['customer_name'], 'Cust 5')
        last = self.api_get(self.url + '?page=3&page_size=5', user=self.staff)
        self.assertEqual([s['id'] for s in last.data['sessions']], ids[10:])
        self.assertFalse(last.data['has_next'])

        # Only the page's rows are loaded: the names query is limited to them.
        with CaptureQueriesContext(connection) as ctx:
            self.api_get(self.url + '?page=1&page_size=2', user=self.staff)
        name_sql = [q['sql'] for q in ctx.captured_queries if 'social_integrations_facebookmessage' in q['sql']]
        self.assertEqual(len(name_sql), 1)
        self.assertIn("'fxp0'", name_sql[0])
        self.assertNotIn("'fxp5'", name_sql[0])

        full = self.api_get(self.url, user=self.staff)  # web: full list, no paging keys
        self.assertEqual(len(full.data['sessions']), 12)
        self.assertNotIn('page', full.data)

    def test_non_staff_is_denied(self):
        self.assertEqual(self.api_get(self.url + '?page=1', user=self.agent).status_code, 403)


# ── 6. social_admin_recipients in constant queries ─────────────────────────

class SocialAdminRecipientsTests(SocialIntegrationTestCase):

    def test_constant_queries_and_same_rule(self):
        from social_integrations.tests.test_p1_social import grant_social_feature
        from users.notification_utils import social_admin_recipients
        self._feature_patcher.stop()
        try:
            counter = iter(range(10_000))

            def make(n, **kw):
                return [self.create_user(email=f'fx-sar-{next(counter)}@test.com', **kw) for _ in range(n)]

            plain = make(2)
            admins = make(1, role='admin')
            staff = make(1, is_staff=True)
            flagged = make(1, can_manage_social_settings=True)
            manager = make(1, role='manager')
            no_feature_admin = self.create_user(email='fx-sar-nofeat@test.com', role='admin')
            grant_social_feature(self.tenant, *plain, *admins, *staff, *flagged, *manager)

            with CaptureQueriesContext(connection) as small:
                got = {u.id for u in social_admin_recipients('manage_social_settings')}
            self.assertEqual(got, {u.id for u in admins + staff + flagged})
            self.assertNotIn(no_feature_admin.id, got)

            more = make(8, role='admin')
            grant_social_feature(self.tenant, *more, *make(8))
            with CaptureQueriesContext(connection) as big:
                got = social_admin_recipients('manage_social_settings')
            self.assertEqual(len(got), 3 + 8)
            self.assertEqual(len(big.captured_queries), len(small.captured_queries))

            # matches has_permission for every user
            for perm in ('manage_social_settings', 'manage_social_connections', 'send_social_messages'):
                expected = {
                    u.id for u in plain + admins + staff + flagged + manager + more
                    if u.is_staff or u.is_superuser or u.role == 'admin' or u.has_permission(perm)
                }
                self.assertTrue(expected <= {u.id for u in social_admin_recipients(perm)}, perm)
                self.assertEqual({u.id for u in social_admin_recipients(perm)} & {u.id for u in plain}, set())
        finally:
            self._feature_patcher.start()


# ── 7. Unified list: widget fallback-label search + page cap ───────────────

class UnifiedListSearchAndPageCapTests(SocialIntegrationTestCase):
    URL = '/api/social/conversations/'

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='fx-ul-agent@test.com')

    def _widget_connection(self):
        from widget_registry.models import WidgetConnection
        with schema_context('public'):
            return WidgetConnection.objects.create(
                tenant_schema='test', label='Site', widget_token=WidgetConnection.generate_token(),
            )

    def test_search_matches_the_website_visitor_fallback_label(self):
        conn = self._widget_connection()
        _widget_chat(conn.id, 'fx-anon', 'abc123xyz', visitor_name='')
        _widget_chat(conn.id, 'fx-named', 'zzz999', visitor_name='Nino')
        resp = self.api_get(self.URL + '?platforms=widget&search=website%20visitor%20abc1', user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual([r['sender_name'] for r in resp.data['results']], ['Website visitor abc123'])
        resp = self.api_get(self.URL + '?platforms=widget&search=nino', user=self.agent)
        self.assertEqual([r['sender_name'] for r in resp.data['results']], ['Nino'])
        # a named visitor is not matched by the fallback label of its id
        resp = self.api_get(self.URL + '?platforms=widget&search=website%20visitor%20zzz', user=self.agent)
        self.assertEqual(resp.data['results'], [])

    def test_page_beyond_the_cap_is_400(self):
        resp = self.api_get(self.URL + '?page=101', user=self.agent)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.data)
        self.assertEqual(self.api_get(self.URL + '?page=100', user=self.agent).status_code, 200)

    def test_last_allowed_page_has_no_next_link(self):
        page = self.create_fb_connection(page_id='fx-ul-page')
        for i in range(3):
            self.create_fb_message(page_connection=page, sender_id=f'fx-ul-{i}')
        from social_integrations import views
        with patch.object(views, 'UNIFIED_LIST_MAX_PAGE', 2):
            resp = self.api_get(self.URL + '?platforms=facebook&page=1&page_size=1', user=self.agent)
            self.assertIsNotNone(resp.data['next'])
            resp = self.api_get(self.URL + '?platforms=facebook&page=2&page_size=1', user=self.agent)
            self.assertEqual(resp.status_code, 200)
            self.assertIsNone(resp.data['next'])  # infinite scroll stops instead of hitting 400
            self.assertEqual(self.api_get(self.URL + '?page=3&page_size=1', user=self.agent).status_code, 400)
