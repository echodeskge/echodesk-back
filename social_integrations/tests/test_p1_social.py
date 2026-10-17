"""Package p1 (social inbox + social admin parity) behaviour tests."""
import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, unquote, urlparse

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.core.management import call_command
from django.conf import settings as dj_settings
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from tenant_schemas.utils import schema_context

from social_integrations.models import (
    AIConversationState, ChatAssignment, ConversationArchive, FacebookMessage,
    SocialClientCustomField, TelegramAccount, TelegramMessage, WhatsAppMessageTemplate,
    WidgetMessage, WidgetSession,
)
from social_integrations.oauth_state import make_oauth_state, read_oauth_state
from social_integrations.tests.conftest import SocialIntegrationTestCase
from tenants.models import Feature, TenantSubscription
from users.models import Notification


def grant_social_feature(tenant, *users, key='social_integrations'):
    """Real subscription → group → user feature wiring (no has_feature patch)."""
    from users.models import TenantGroup
    feature, _ = Feature.objects.get_or_create(
        key=key,
        defaults={
            'name': key, 'price_per_user_gel': Decimal('0'), 'price_unlimited_gel': Decimal('0'),
            'category': 'support', 'is_active': True,
        },
    )
    subscription, _ = TenantSubscription.objects.get_or_create(
        tenant=tenant, defaults={'is_active': True, 'starts_at': timezone.now(), 'agent_count': 10},
    )
    subscription.selected_features.add(feature)
    group = TenantGroup.objects.create(name=f'Social {TenantGroup.objects.count()}')
    group.features.add(feature)
    for user in users:
        user.tenant_groups.add(group)
    return feature


class WidgetFixtureMixin:
    def make_widget_chat(self, connection_id=7, session_id='sess-a', visitor_id='visitor-a', **kw):
        session = WidgetSession.objects.create(
            connection_id=connection_id, session_id=session_id, visitor_id=visitor_id,
            visitor_name=kw.pop('visitor_name', 'Nino'), **kw,
        )
        WidgetMessage.objects.create(
            session=session, message_id=f'm-{session_id}', message_text='hello',
            is_from_visitor=True, timestamp=timezone.now(),
        )
        return session


# ── Assignments: widget key normalisation + transfer ownership ─────────────

class AssignmentNormalisationTests(WidgetFixtureMixin, SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='p1-agent@test.com')
        self.other = self.create_user(email='p1-other@test.com')
        self.staff = self.create_admin(email='p1-staff@test.com')
        self.create_settings(chat_assignment_enabled=True, session_management_enabled=True)
        self.session = self.make_widget_chat()

    def _body(self, conv):
        return {'platform': 'widget', 'conversation_id': conv, 'account_id': '7'}

    @patch('social_integrations.views._broadcast_assignment_change')
    def test_assign_by_session_or_list_id_is_stored_under_visitor_id(self, _b):
        resp = self.api_post('/api/social/assignments/assign/', self._body('widget_7_sess-a'), user=self.agent)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['conversation_id'], 'visitor-a')
        # Same chat via the session id → "already yours", not a duplicate row.
        resp = self.api_post('/api/social/assignments/assign/', self._body('sess-a'), user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ChatAssignment.objects.filter(platform='widget').count(), 1)
        # Another agent is refused whatever id form they use.
        resp = self.api_post('/api/social/assignments/assign/', self._body('visitor-a'), user=self.other)
        self.assertEqual(resp.status_code, 400)

    @patch('social_integrations.views._broadcast_assignment_change')
    def test_status_resolves_session_id_to_visitor_assignment(self, _b):
        self.create_chat_assignment(user=self.agent, platform='widget', conversation_id='visitor-a',
                                    account_id='7', status='in_session')
        ConversationArchive.objects.create(platform='widget', conversation_id='sess-a', account_id='7')
        resp = self.api_get('/api/social/assignments/status/?platform=widget&conversation_id=sess-a&account_id=7',
                            user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['assignment']['assigned_user'], self.agent.id)
        resp = self.api_get('/api/social/assignments/status/?platform=widget&conversation_id=visitor-a&account_id=7',
                            user=self.agent)
        self.assertTrue(resp.data['is_archived'])

    @patch('social_integrations.views._broadcast_archive_change')
    @patch('social_integrations.views._broadcast_assignment_change')
    def test_end_session_by_visitor_id_closes_widget_session_and_archives_it(self, _a, _b):
        self.create_chat_assignment(user=self.agent, platform='widget', conversation_id='visitor-a',
                                    account_id='7', status='in_session', session_started_at=timezone.now())
        resp = self.api_post('/api/social/assignments/end-session/', self._body('visitor-a'), user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.ended_at)
        self.assertEqual(self.session.ended_by, 'agent')
        self.assertTrue(ConversationArchive.objects.filter(
            platform='widget', conversation_id='sess-a', account_id='7').exists())

    @patch('social_integrations.views._broadcast_assignment_change')
    def test_transfer_requires_holder_or_staff(self, _b):
        self.create_chat_assignment(user=self.agent, platform='facebook', conversation_id='psid',
                                    account_id='pg', status='in_session')
        body = {'platform': 'facebook', 'conversation_id': 'psid', 'account_id': 'pg',
                'target_user_id': self.other.id}
        resp = self.api_post('/api/social/assignments/transfer/', body, user=self.other)
        self.assertEqual(resp.status_code, 400)  # cannot transfer to yourself
        third = self.create_user(email='p1-third@test.com')
        resp = self.api_post('/api/social/assignments/transfer/', body, user=third)
        self.assertEqual(resp.status_code, 403)
        resp = self.api_post('/api/social/assignments/transfer/', body, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(ChatAssignment.objects.get(platform='facebook').assigned_user, self.other)
        body['target_user_id'] = third.id
        resp = self.api_post('/api/social/assignments/transfer/', body, user=self.staff)
        self.assertEqual(resp.status_code, 200)

    @patch('social_integrations.views._broadcast_assignment_change')
    def test_transfer_of_unassigned_chat_is_allowed(self, _b):
        body = {'platform': 'widget', 'conversation_id': 'sess-a', 'account_id': '7',
                'target_user_id': self.other.id}
        resp = self.api_post('/api/social/assignments/transfer/', body, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(ChatAssignment.objects.get(platform='widget').conversation_id, 'visitor-a')

    def test_ai_state_widget_visitor_id_reads_session_state(self):
        AIConversationState.objects.create(platform='widget', conversation_id='sess-a', account_id='7',
                                           mode='needs_human', reason='asked for human')
        resp = self.api_get('/api/social/ai/state/?platform=widget&conversation_id=visitor-a&account_id=7',
                            user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['mode'], 'needs_human')


class MarkReadScopeTests(SocialIntegrationTestCase):

    @patch('social_integrations.views._broadcast_read_state')
    def test_account_id_limits_mark_read_to_one_page(self, broadcast):
        agent = self.create_user(email='p1-reader@test.com')
        page_a = self.create_fb_connection(page_id='pa')
        page_b = self.create_fb_connection(page_id='pb')
        self.create_fb_message(page_connection=page_a, sender_id='same')
        self.create_fb_message(page_connection=page_b, sender_id='same')
        resp = self.api_post('/api/social/mark-read/',
                             {'platform': 'facebook', 'conversation_id': 'same', 'account_id': 'pa'}, user=agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['messages_marked_read'], 1)
        self.assertTrue(FacebookMessage.objects.get(page_connection=page_a).is_read_by_staff)
        self.assertFalse(FacebookMessage.objects.get(page_connection=page_b).is_read_by_staff)
        self.assertEqual(broadcast.call_args.kwargs['account_id'], 'pa')
        # Without account_id both pages are marked (unchanged behaviour).
        FacebookMessage.objects.update(is_read_by_staff=False)
        resp = self.api_post('/api/social/mark-read/', {'platform': 'facebook', 'conversation_id': 'same'}, user=agent)
        self.assertEqual(resp.data['messages_marked_read'], 2)


# ── Notifications ───────────────────────────────────────────────────────────

class SocialNotificationTests(WidgetFixtureMixin, SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self._feature_patcher.stop()  # use the real feature wiring
        self.inbox_a = self.create_user(email='p1-inbox-a@test.com')
        self.inbox_b = self.create_user(email='p1-inbox-b@test.com')
        self.no_inbox = self.create_user(email='p1-noinbox@test.com')
        self.inactive = self.create_user(email='p1-inactive@test.com', is_active=False)
        grant_social_feature(self.tenant, self.inbox_a, self.inbox_b, self.inactive)

    def tearDown(self):
        self._feature_patcher.start()
        super().tearDown()

    def _notified(self, notification_type='message_received'):
        return set(Notification.objects.filter(notification_type=notification_type)
                   .values_list('user__email', flat=True))

    def test_unassigned_message_notifies_only_users_with_the_inbox(self):
        from users.notification_utils import create_social_message_notification
        create_social_message_notification(
            platform='Telegram', sender_name='Gio', message_text='hi', conversation_id='4242',
            sender_id='4242', account_id='777',
        )
        self.assertEqual(self._notified(), {'p1-inbox-a@test.com', 'p1-inbox-b@test.com'})
        n = Notification.objects.filter(user=self.inbox_a).get()
        self.assertEqual(n.link_url, '/messages/tg_777_4242')
        self.assertEqual(n.metadata['chat_id'], 'tg_777_4242')
        self.assertEqual(n.metadata['platform_key'], 'telegram')

    def test_assigned_message_notifies_the_assignee_with_assigned_tab(self):
        from users.notification_utils import create_social_message_notification
        create_social_message_notification(
            platform='WhatsApp', sender_name='Ana', message_text='hey', conversation_id='+995555',
            sender_id='+995555', account_id='waba9', assigned_user_id=self.inbox_b.id,
        )
        self.assertEqual(self._notified(), {'p1-inbox-b@test.com'})
        self.assertEqual(Notification.objects.get().link_url, '/messages/wa_waba9_995555?tab=assigned')

    def test_push_data_carries_platform_account_and_chat_id(self):
        from users.notification_utils import create_social_message_notification
        with patch('users.notification_utils._dispatch_push') as push:
            create_social_message_notification(
                platform='Facebook', sender_name='Lika', message_text='yo', conversation_id='psid1',
                sender_id='psid1', account_id='page1', assigned_user_id=self.inbox_a.id,
            )
        data = push.call_args.kwargs['data']
        self.assertEqual(data['platform'], 'facebook')
        self.assertEqual(data['account_id'], 'page1')
        self.assertEqual(data['chat_id'], 'fb_page1_psid1')
        self.assertEqual(data['conversation_id'], 'psid1')
        self.assertEqual(push.call_args.kwargs['url'], '/messages/fb_page1_psid1?tab=assigned')

    def test_widget_visitor_message_notifies_with_deep_link(self):
        from widget_registry.models import WidgetConnection
        with schema_context('public'):
            conn = WidgetConnection.objects.create(tenant_schema='test', widget_token='p1-tok-notify',
                                                   label='Site')
        try:
            session = WidgetSession.objects.create(connection_id=conn.id, session_id='wsess',
                                                   visitor_id='wvisitor', visitor_name='Visitor')
            with patch('channels.layers.get_channel_layer', return_value=None):
                resp = self.client.post(
                    '/api/widget/public/messages/',
                    data=json.dumps({'token': 'p1-tok-notify', 'session_id': 'wsess', 'message_text': 'help'}),
                    content_type='application/json', HTTP_HOST='tenant.test.com',
                )
            self.assertEqual(resp.status_code, 201, getattr(resp, 'data', resp.content))
            self.assertEqual(self._notified(), {'p1-inbox-a@test.com', 'p1-inbox-b@test.com'})
            n = Notification.objects.filter(user=self.inbox_a).get()
            self.assertEqual(n.link_url, f'/messages/widget_{conn.id}_wsess')
            self.assertEqual(n.metadata['conversation_id'], 'wsess')
            # mark-read with the session id clears it again
            self._feature_patcher.start()
            try:
                self.api_post('/api/social/mark-read/', {'platform': 'widget', 'conversation_id': 'wvisitor',
                                                         'account_id': str(conn.id)}, user=self.inbox_a)
            finally:
                self._feature_patcher.stop()
            n.refresh_from_db()
            self.assertTrue(n.is_read)
            self.assertTrue(WidgetMessage.objects.get(session=session).is_read_by_staff)
        finally:
            with schema_context('public'):
                WidgetConnection.objects.filter(pk=conn.pk).delete()

    def test_ai_handoff_notifies_inbox_users_with_widget_link(self):
        from users.notification_utils import create_ai_handoff_notification
        create_ai_handoff_notification('widget', 'sess-z', '7', 'Nino', reason='human please')
        self.assertEqual(self._notified('ai_handoff_requested'), {'p1-inbox-a@test.com', 'p1-inbox-b@test.com'})
        self.assertEqual(Notification.objects.filter(user=self.inbox_a).get().link_url, '/messages/widget_7_sess-z')

    def _auto_settings(self):
        from social_integrations.models import AutoPostSettings
        from datetime import time
        AutoPostSettings.objects.create(pk=1, is_enabled=True, posting_time=time(timezone.now().hour, 0),
                                        timezone='UTC', require_approval=True, max_posts_per_day=5)

    def test_generated_draft_notifies_social_admins(self):
        admin = self.create_admin(email='p1-social-admin@test.com')
        grant_social_feature(self.tenant, admin)
        self._auto_settings()
        draft = self.create_auto_post_content(status='draft')
        import sys
        import types
        import datetime as dt
        fake = types.ModuleType('social_integrations.services.ai_content_service')
        fake.AIContentService = MagicMock()
        fake.AIContentService.return_value.generate_post_for_tenant.return_value = draft
        modules = {'social_integrations.services.ai_content_service': fake}
        try:
            import pytz  # noqa: F401
        except ImportError:  # minimal local envs; production has pytz
            fake_pytz = types.ModuleType('pytz')
            fake_pytz.UTC = dt.timezone.utc
            fake_pytz.timezone = lambda name: dt.timezone.utc
            fake_pytz.exceptions = types.SimpleNamespace(UnknownTimeZoneError=KeyError)
            modules['pytz'] = fake_pytz
            sys.modules.pop('social_integrations.management.commands.generate_daily_posts', None)
        with patch.dict(sys.modules, modules):
            call_command('generate_daily_posts', '--tenant', 'test', stdout=StringIO(), stderr=StringIO())
        self.assertEqual(self._notified('auto_post_draft'), {'p1-social-admin@test.com'})
        n = Notification.objects.get(notification_type='auto_post_draft')
        self.assertEqual(n.link_url, f'/settings/social/auto-posting?post={draft.id}')
        self.assertEqual(n.metadata['auto_post_id'], draft.id)

    def test_failed_publish_notifies_social_admins(self):
        admin = self.create_admin(email='p1-social-admin2@test.com')
        grant_social_feature(self.tenant, admin)
        post = self.create_auto_post_content(status='approved', scheduled_for=timezone.now() - timedelta(minutes=5),
                                             target_instagram=False)
        with patch('social_integrations.services.facebook_publisher.FacebookPublisher') as pub:
            pub.return_value.publish.side_effect = RuntimeError('token expired')
            call_command('publish_approved_posts', '--tenant', 'test', stdout=StringIO(), stderr=StringIO())
        post.refresh_from_db()
        self.assertEqual(post.status, 'failed')
        self.assertEqual(self._notified('auto_post_failed'), {'p1-social-admin2@test.com'})


# ── WhatsApp template status webhook + sync paging ─────────────────────────

class WhatsAppTemplateStatusTests(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.creator = self.create_user(email='p1-tpl-creator@test.com')
        self.waba = self.create_wa_account(waba_id='waba-tpl')
        self.template = WhatsAppMessageTemplate.objects.create(
            business_account=self.waba, template_id='999', name='order_ready', language='en_US',
            status='PENDING', created_by=self.creator,
        )

    def _post(self, event, **headers):
        body = {
            'object': 'whatsapp_business_account',
            'entry': [{'id': 'waba-tpl', 'changes': [{
                'field': 'message_template_status_update',
                'value': {'event': event, 'message_template_id': 999,
                          'message_template_name': 'order_ready', 'message_template_language': 'en_US',
                          'reason': 'NONE'},
            }]}],
        }
        return self.client.post('/api/social/whatsapp/webhook/', data=json.dumps(body),
                                content_type='application/json', HTTP_HOST='tenant.test.com', **headers)

    def test_approval_updates_status_and_notifies_creator(self):
        with patch('users.notification_utils.social_admin_recipients', return_value=[]):
            resp = self._post('APPROVED')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(json.loads(resp.content)['templates_updated'], 1)
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, 'APPROVED')
        n = Notification.objects.get(notification_type='whatsapp_template_status')
        self.assertEqual(n.user, self.creator)
        self.assertEqual(n.link_url, f'/social/templates?id={self.template.id}')
        self.assertEqual(n.metadata['status'], 'APPROVED')
        self.assertEqual(n.metadata['previous_status'], 'PENDING')

    def test_unknown_event_is_ignored(self):
        resp = self._post('SOMETHING_NEW')
        self.assertEqual(resp.status_code, 200)
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, 'PENDING')

    def test_bad_signature_is_rejected_when_app_secret_is_set(self):
        from django.conf import settings as dj_settings
        social = dict(dj_settings.SOCIAL_INTEGRATIONS, FACEBOOK_APP_SECRET='s3cret')
        with override_settings(SOCIAL_INTEGRATIONS=social):
            resp = self._post('APPROVED', HTTP_X_HUB_SIGNATURE_256='sha256=deadbeef')
        self.assertEqual(resp.status_code, 403)
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, 'PENDING')

    def test_sync_follows_paging_and_returns_synced_and_templates(self):
        admin = self.create_admin(email='p1-tpl-admin@test.com')

        def page(data, nxt=None):
            r = MagicMock(status_code=200)
            r.json.return_value = {'data': data, 'paging': {'next': nxt} if nxt else {}}
            return r

        first = page([{'id': '1', 'name': 'a', 'language': 'en', 'status': 'APPROVED'}], nxt='https://graph/next')
        second = page([{'id': '2', 'name': 'b', 'language': 'en', 'status': 'REJECTED'}])
        with patch('social_integrations.views.requests.get', side_effect=[first, second]) as get:
            resp = self.api_post('/api/social/whatsapp/waba-tpl/templates/sync/', user=admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['synced'], 2)
        self.assertEqual({t['name'] for t in resp.data['templates']}, {'a', 'b', 'order_ready'})
        self.assertEqual(get.call_args_list[1].args[0], 'https://graph/next')


# ── OAuth signed state ─────────────────────────────────────────────────────

class OAuthStateTests(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.admin = self.create_admin(email='p1-oauth-admin@test.com')

    def _state_from(self, oauth_url):
        return unquote(parse_qs(urlparse(oauth_url).query)['state'][0])

    def test_start_signs_tenant_user_and_return_to(self):
        from django.conf import settings as dj_settings
        social = dict(dj_settings.SOCIAL_INTEGRATIONS, FACEBOOK_APP_ID='123')
        with override_settings(SOCIAL_INTEGRATIONS=social):
            resp = self.api_get('/api/social/facebook/oauth/start/?return_to=app', user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        state = read_oauth_state(self._state_from(resp.data['oauth_url']))
        self.assertEqual(state['tenant'], 'test')
        self.assertEqual(state['user_id'], self.admin.id)
        self.assertEqual(state['return_to'], 'app')
        self.assertFalse(state['publishing'])

    def test_state_is_single_use_and_tamper_proof(self):
        token = make_oauth_state('test')
        self.assertIsNone(read_oauth_state(token[:-2] + 'xx'))
        self.assertIsNone(read_oauth_state('tenant=test'))
        self.assertIsNotNone(read_oauth_state(token))
        self.assertIsNone(read_oauth_state(token))  # replay

    def test_forged_state_never_reaches_a_tenant(self):
        with patch('social_integrations.views.requests.get') as get:
            resp = self.client.get('/api/social/facebook/oauth/callback/?code=abc&state=tenant%3Dtest',
                                   HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith(f'https://{dj_settings.MAIN_DOMAIN}/social/connections?facebook_status=error'), resp['Location'])
        get.assert_not_called()

    def test_app_return_redirects_to_the_app_scheme(self):
        state = make_oauth_state('test', user_id=self.admin.id, return_to='app')
        resp = self.client.get('/api/social/facebook/oauth/callback/',
                               {'error': 'access_denied', 'error_description': 'denied', 'state': state},
                               HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 302)
        loc = urlparse(resp['Location'])
        self.assertEqual((loc.scheme, loc.netloc, loc.path), ('echodesk', 'social', '/connections'))
        q = parse_qs(loc.query)
        self.assertEqual(q['platform'], ['facebook'])
        self.assertEqual(q['status'], ['error'])

    def test_web_return_keeps_the_dashboard_redirect(self):
        state = make_oauth_state('test')
        resp = self.client.get('/api/social/facebook/oauth/callback/', {'error': 'denied', 'state': state},
                               HTTP_HOST='tenant.test.com')
        self.assertTrue(resp['Location'].startswith(f'https://test.{dj_settings.MAIN_DOMAIN}/social/connections?facebook_status=error'))

    def test_tiktok_callback_success_to_app(self):
        state = make_oauth_state('test', return_to='app')
        tokens = {
            'access_token': 'a', 'refresh_token': 'r', 'open_id': 'open-1', 'seller_name': 'Shop',
            'access_expires_at': timezone.now() + timedelta(days=1),
            'refresh_expires_at': timezone.now() + timedelta(days=30),
        }
        with patch('social_integrations.tiktok_utils.exchange_code_for_token', return_value=tokens), \
                patch('social_integrations.tiktok_utils.get_authorized_shops', return_value=[]):
            resp = self.client.get('/api/social/tiktok/oauth/callback/', {'code': 'c', 'state': state},
                                   HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], 'echodesk://social/connections?platform=tiktok&status=ok')

    def test_whatsapp_get_callback_rejects_unsigned_state(self):
        resp = self.client.get('/api/social/whatsapp/embedded-signup/callback/',
                               {'code': 'c', 'state': 'tenant=test'}, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('whatsapp_status=error', resp['Location'])

    def test_whatsapp_post_requires_auth_and_own_tenant(self):
        resp = self.client.post('/api/social/whatsapp/embedded-signup/callback/',
                                {'code': 'c', 'tenant': 'test'}, format='json', HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 401)
        resp = self.api_post('/api/social/whatsapp/embedded-signup/callback/',
                             {'code': 'c', 'tenant': 'someone-else'}, user=self.admin)
        self.assertEqual(resp.status_code, 400)
        agent = self.create_user(email='p1-oauth-agent@test.com')
        resp = self.api_post('/api/social/whatsapp/embedded-signup/callback/', {'code': 'c'}, user=agent)
        self.assertEqual(resp.status_code, 403)


# ── Clients ────────────────────────────────────────────────────────────────

class SocialClientParityTests(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='p1-client-agent@test.com')
        self.admin = self.create_admin(email='p1-client-admin@test.com')

    def test_custom_fields_route_is_not_shadowed(self):
        SocialClientCustomField.objects.create(name='vip', label='VIP', field_type='boolean')
        resp = self.api_get('/api/social/clients/custom-fields/', user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        rows = resp.data['results'] if isinstance(resp.data, dict) else resp.data
        self.assertEqual([r['name'] for r in rows], ['vip'])

    def test_custom_field_writes_need_admin(self):
        body = {'name': 'tier', 'label': 'Tier', 'field_type': 'string'}
        self.assertEqual(self.api_post('/api/social/clients/custom-fields/', body, user=self.agent).status_code, 403)
        self.assertEqual(self.api_post('/api/social/clients/custom-fields/', body, user=self.admin).status_code, 201)

    def test_create_returns_id_and_delete_needs_admin(self):
        resp = self.api_post('/api/social/clients/', {'name': 'Nika'}, user=self.agent)
        self.assertEqual(resp.status_code, 201, resp.data)
        client_id = resp.data['id']
        self.assertEqual(self.api_delete(f'/api/social/clients/{client_id}/', user=self.agent).status_code, 403)
        self.assertEqual(self.api_delete(f'/api/social/clients/{client_id}/', user=self.admin).status_code, 204)

    def test_patch_enabling_booking_keeps_existing_email(self):
        client = self.create_client(name='Mari', email='mari@test.com')
        resp = self.api_patch(f'/api/social/clients/{client.id}/', {'is_booking_enabled': True}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_clients_require_the_social_feature(self):
        self._feature_patcher.stop()
        try:
            resp = self.api_get('/api/social/clients/', user=self.agent)
        finally:
            self._feature_patcher.start()
        self.assertEqual(resp.status_code, 403)


# ── Message viewsets: widget paging + hide-assigned ────────────────────────

class MessageViewsetTests(WidgetFixtureMixin, SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='p1-mv-agent@test.com')
        self.other = self.create_user(email='p1-mv-other@test.com')
        self.session = WidgetSession.objects.create(connection_id=7, session_id='long', visitor_id='v-long')
        base = timezone.now() - timedelta(hours=1)
        for i in range(120):
            WidgetMessage.objects.create(session=self.session, message_id=f'lm{i}', message_text=f'msg {i}',
                                         is_from_visitor=bool(i % 2), timestamp=base + timedelta(seconds=i))

    def test_newest_first_paging(self):
        resp = self.api_get('/api/widget/admin/messages/?session_id=widget_7_long&ordering=-timestamp&page_size=50',
                            user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['count'], 120)
        self.assertEqual(resp.data['results'][0]['message_text'], 'msg 119')
        resp2 = self.api_get('/api/widget/admin/messages/?session_id=long&ordering=-timestamp&page_size=50&page=3',
                             user=self.agent)
        self.assertEqual([m['message_text'] for m in resp2.data['results']][-1], 'msg 0')

    def test_default_order_is_unchanged_and_500_page_size_allowed(self):
        resp = self.api_get('/api/widget/admin/messages/?session_id=long&page_size=500', user=self.agent)
        self.assertEqual(len(resp.data['results']), 120)
        self.assertEqual(resp.data['results'][0]['message_text'], 'msg 0')

    def test_hide_assigned_hides_other_agents_widget_and_telegram_chats(self):
        self.create_settings(hide_assigned_chats=True, chat_assignment_enabled=True)
        self.create_chat_assignment(user=self.other, platform='widget', conversation_id='v-long',
                                    account_id='7', status='in_session')
        resp = self.api_get('/api/widget/admin/messages/?session_id=long', user=self.agent)
        self.assertEqual(resp.data['count'], 0)
        resp = self.api_get('/api/widget/admin/messages/?session_id=long', user=self.other)
        self.assertEqual(resp.data['count'], 120)

        tg = TelegramAccount.objects.create(telegram_user_id=555, phone_number='+99550', session_string='s')
        TelegramMessage.objects.create(account=tg, message_id='555_1', telegram_msg_id=1, peer_id=9,
                                       message_text='x', timestamp=timezone.now())
        self.create_chat_assignment(user=self.other, platform='telegram', conversation_id='9',
                                    account_id='555', status='active')
        self.assertEqual(self.api_get('/api/social/telegram-messages/?account_id=555', user=self.agent).data['count'], 0)
        self.assertEqual(self.api_get('/api/social/telegram-messages/?account_id=555', user=self.other).data['count'], 1)

    def test_widget_messages_require_the_feature(self):
        self._feature_patcher.stop()
        try:
            resp = self.api_get('/api/widget/admin/messages/?session_id=long', user=self.agent)
        finally:
            self._feature_patcher.start()
        self.assertEqual(resp.status_code, 403)


# ── Rating sessions: batched lookups + optional paging ─────────────────────

class RatingSessionsTests(SocialIntegrationTestCase):

    def test_query_count_independent_of_rating_count_and_paging(self):
        from django.test.utils import CaptureQueriesContext
        staff = self.create_admin(email='p1-rating-staff@test.com')
        agent = self.create_user(email='p1-rated@test.com')
        page = self.create_fb_connection()

        def add(n):
            for i in range(n):
                sid = f'cust{ChatAssignment.objects.count()}_{i}_{timezone.now().timestamp()}'
                self.create_fb_message(page_connection=page, sender_id=sid, sender_name=f'Cust {i}')
                self.create_chat_rating(rated_user=agent, platform='facebook', conversation_id=sid,
                                        account_id=page.page_id, rating=4)

        url = f'/api/social/rating-statistics/user/{agent.id}/'
        add(2)
        with CaptureQueriesContext(connection) as small:
            resp = self.api_get(url, user=staff)
        self.assertEqual(resp.status_code, 200)
        add(10)
        with CaptureQueriesContext(connection) as big:
            resp = self.api_get(url, user=staff)
        self.assertEqual(len(big.captured_queries), len(small.captured_queries))
        self.assertEqual(resp.data['total_sessions'], 12)
        self.assertTrue(all(s['customer_name'].startswith('Cust') for s in resp.data['sessions']))
        self.assertNotIn('page', resp.data)

        resp = self.api_get(url + '?page=2&page_size=5', user=staff)
        self.assertEqual(len(resp.data['sessions']), 5)
        self.assertEqual(resp.data['total_sessions'], 12)
        self.assertTrue(resp.data['has_next'])


# ── WebSocket consumer: feature gate + hide-assigned filtering ─────────────

IN_MEMORY_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}


class MessagesConsumerTests(SocialIntegrationTestCase):

    def _communicator(self, user, access):
        from channels.testing import WebsocketCommunicator
        from social_integrations.consumers import MessagesConsumer

        async def load_access(consumer_self):
            return access

        async def fetch_hide(consumer_self):
            return access['hide_assigned']

        app = MessagesConsumer.as_asgi()
        comm = WebsocketCommunicator(app, '/ws/messages/test/')
        comm.scope['user'] = user
        comm.scope['url_route'] = {'kwargs': {'tenant_schema': 'test'}}
        patches = [
            patch.object(MessagesConsumer, '_load_access', load_access),
            patch.object(MessagesConsumer, '_fetch_hide_assigned', fetch_hide),
        ]
        return comm, patches

    def _run(self, user, access, frames):
        from channels.layers import get_channel_layer
        comm, patches = self._communicator(user, access)
        for p in patches:
            p.start()

        async def scenario():
            connected, _ = await comm.connect()
            assert connected
            hello = await comm.receive_json_from()
            layer = get_channel_layer()
            for frame in frames:
                await layer.group_send('messages_test', frame)
            received = []
            while not await comm.receive_nothing(timeout=0.2):
                received.append(await comm.receive_json_from())
            await comm.disconnect()
            return hello, received

        try:
            with override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYERS):
                return async_to_sync(scenario)()
        finally:
            for p in patches:
                p.stop()

    def _frame(self, assigned_user_id):
        return {'type': 'new_message', 'message': {'platform': 'facebook', 'message_text': 'secret'},
                'conversation_id': 'psid', 'timestamp': 't', 'assigned_user_id': assigned_user_id}

    def test_user_without_feature_gets_no_feed(self):
        user = self.create_user(email='p1-ws-nofeature@test.com')
        hello, received = self._run(user, {'has_feature': False, 'is_privileged': False, 'hide_assigned': False},
                                    [self._frame(None)])
        self.assertEqual(hello['type'], 'connection')
        self.assertFalse(hello['social_access'])
        self.assertEqual(received, [])

    def test_hide_assigned_filters_other_agents_chats(self):
        me = self.create_user(email='p1-ws-me@test.com')
        access = {'has_feature': True, 'is_privileged': False, 'hide_assigned': True}
        hello, received = self._run(me, access, [self._frame(me.id + 999), self._frame(None), self._frame(me.id)])
        self.assertTrue(hello['social_access'])
        self.assertEqual([f['assigned_user_id'] for f in received], [None, me.id])

    def test_staff_and_hide_off_receive_everything(self):
        me = self.create_user(email='p1-ws-staff@test.com')
        _, received = self._run(me, {'has_feature': True, 'is_privileged': True, 'hide_assigned': True},
                                [self._frame(me.id + 999)])
        self.assertEqual(len(received), 1)
        _, received = self._run(me, {'has_feature': True, 'is_privileged': False, 'hide_assigned': False},
                                [self._frame(me.id + 999)])
        self.assertEqual(len(received), 1)

    def test_load_access_reads_feature_and_setting(self):
        from social_integrations.consumers import MessagesConsumer
        self.create_settings(hide_assigned_chats=True)
        consumer = MessagesConsumer()
        consumer.tenant_schema = 'test'
        consumer.user = self.create_user(email='p1-ws-load@test.com')
        access = MessagesConsumer.__dict__['_load_access'].func(consumer)
        self.assertEqual(access, {'has_feature': True, 'is_privileged': False, 'hide_assigned': True})
