"""
Tests for the AI companion service, the debounced reply task, and the API.

Provider calls are always patched — these tests exercise gating, state,
persistence, and dispatch, not the LLMs.
"""
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
from rest_framework import status

from social_integrations.models import (
    AICompanionChannel,
    AICompanionRun,
    AICompanionSettings,
    AIConversationState,
    ConversationSummary,
)
from social_integrations.services import ai_companion
from social_integrations.services.ai_providers import AICompanionError
from social_integrations.tasks import ai_companion_reply_task
from .conftest import SocialIntegrationTestCase


def enable_companion(**kwargs):
    defaults = {'is_enabled': True, 'provider': 'anthropic'}
    defaults.update(kwargs)
    settings_obj, _ = AICompanionSettings.objects.get_or_create(defaults=defaults)
    for key, value in defaults.items():
        setattr(settings_obj, key, value)
    settings_obj.save()
    return settings_obj


def enable_channel(platform='facebook', account_id='', **kwargs):
    defaults = {'enabled': True}
    defaults.update(kwargs)
    channel, _ = AICompanionChannel.objects.update_or_create(
        platform=platform, account_id=account_id, defaults=defaults,
    )
    return channel


class AICompanionTestCase(SocialIntegrationTestCase):
    """Base: patches the tenant-subscription feature check to True."""

    def setUp(self):
        super().setUp()
        self._feature = patch(
            'social_integrations.services.ai_companion.tenant_has_ai_companion',
            return_value=True,
        )
        self._feature.start()

    def tearDown(self):
        self._feature.stop()
        super().tearDown()


class TestChannelResolution(AICompanionTestCase):
    def test_exact_row_beats_wildcard(self):
        enable_channel('facebook', '', guidance_prompt='wildcard')
        enable_channel('facebook', 'page_9', guidance_prompt='exact')
        channel = ai_companion.resolve_channel('facebook', 'page_9')
        self.assertEqual(channel.guidance_prompt, 'exact')

    def test_wildcard_fallback(self):
        enable_channel('facebook', '', guidance_prompt='wildcard')
        channel = ai_companion.resolve_channel('facebook', 'page_other')
        self.assertEqual(channel.guidance_prompt, 'wildcard')

    def test_no_channel(self):
        self.assertIsNone(ai_companion.resolve_channel('facebook', 'page_x'))


class TestIsAiActive(AICompanionTestCase):
    def test_disabled_without_settings(self):
        self.assertFalse(ai_companion.is_ai_active('facebook', 'p1', 'c1'))

    def test_disabled_master_toggle(self):
        enable_companion(is_enabled=False)
        enable_channel('facebook')
        self.assertFalse(ai_companion.is_ai_active('facebook', 'p1', 'c1'))

    def test_enabled(self):
        enable_companion()
        enable_channel('facebook')
        self.assertTrue(ai_companion.is_ai_active('facebook', 'p1', 'c1'))

    def test_channel_must_be_enabled(self):
        enable_companion()
        enable_channel('facebook', enabled=False)
        self.assertFalse(ai_companion.is_ai_active('facebook', 'p1', 'c1'))

    def test_non_ai_mode_blocks(self):
        enable_companion()
        enable_channel('facebook')
        AIConversationState.objects.create(
            platform='facebook', conversation_id='c1', account_id='p1',
            mode='needs_human',
        )
        self.assertFalse(ai_companion.is_ai_active('facebook', 'p1', 'c1'))

    def test_email_platform_never_replies(self):
        enable_companion()
        enable_channel('email')
        self.assertFalse(ai_companion.is_ai_active('email', '1', 'thread'))


class TestAutoReplyReplacement(AICompanionTestCase):
    def test_welcome_suppressed_when_ai_enabled(self):
        from social_integrations.views import process_auto_reply
        self.create_settings(auto_reply_settings={
            'facebook': {'welcome_enabled': True, 'welcome_message': 'Welcome!',
                         'away_enabled': False, 'away_message': ''},
        })
        conn = self.create_fb_connection()
        enable_companion()
        enable_channel('facebook')

        with patch('social_integrations.views.send_auto_reply') as mock_send:
            result = process_auto_reply('facebook', conn.page_id, 'psid_ar', 'Nino', conn)
        self.assertFalse(result)
        mock_send.assert_not_called()

    def test_welcome_still_sent_when_ai_disabled(self):
        from social_integrations.views import process_auto_reply
        self.create_settings(auto_reply_settings={
            'facebook': {'welcome_enabled': True, 'welcome_message': 'Welcome!',
                         'away_enabled': False, 'away_message': ''},
        })
        conn = self.create_fb_connection()

        with patch('social_integrations.views.send_auto_reply', return_value=True) as mock_send:
            result = process_auto_reply('facebook', conn.page_id, 'psid_ar2', 'Nino', conn)
        self.assertTrue(result)
        mock_send.assert_called_once()


class TestGenerateSummary(AICompanionTestCase):
    def test_persists_summary_and_run(self):
        conn = self.create_fb_connection()
        self.create_fb_message(
            page_connection=conn, sender_id='psid_s1',
            message_text='I want a cake for Friday', is_from_page=False,
        )
        payload = {
            'summary': 'Customer wants a cake for Friday.',
            'customer_intent': 'Order a cake.',
            'open_items': 'Confirm pickup time.',
        }
        with patch.object(
            ai_companion.ai_providers, 'generate_structured',
            return_value=(payload, {'prompt_tokens': 10, 'completion_tokens': 5}),
        ):
            summary = ai_companion.generate_summary('facebook', conn.page_id, 'psid_s1')

        self.assertIn('cake for Friday', summary.summary_text)
        self.assertIn('Confirm pickup time', summary.summary_text)
        run = AICompanionRun.objects.get(kind='summary')
        self.assertTrue(run.success)
        self.assertEqual(run.prompt_tokens, 10)

    def test_provider_error_records_failed_run(self):
        conn = self.create_fb_connection()
        self.create_fb_message(
            page_connection=conn, sender_id='psid_s2',
            message_text='hello', is_from_page=False,
        )
        with patch.object(
            ai_companion.ai_providers, 'generate_structured',
            side_effect=AICompanionError('boom'),
        ):
            with self.assertRaises(AICompanionError):
                ai_companion.generate_summary('facebook', conn.page_id, 'psid_s2')

        run = AICompanionRun.objects.get(kind='summary')
        self.assertFalse(run.success)
        self.assertIn('boom', run.error_message)
        self.assertEqual(ConversationSummary.objects.count(), 0)

    def test_empty_conversation_raises_value_error(self):
        conn = self.create_fb_connection()
        with self.assertRaises(ValueError):
            ai_companion.generate_summary('facebook', conn.page_id, 'psid_none')


class TestReplyTask(AICompanionTestCase):
    def setUp(self):
        super().setUp()
        self.conn = self.create_fb_connection()
        self.settings_obj = enable_companion()
        enable_channel('facebook')
        self.decision_patch = patch.object(
            ai_companion, 'run_decision',
            return_value={'action': 'reply', 'reply_text': 'AI says hi', 'reason': 'greeting'},
        )

    def _inbound(self, mid_suffix='', **kwargs):
        return self.create_fb_message(
            page_connection=self.conn, sender_id='psid_task',
            message_text=f'hello {mid_suffix}', is_from_page=False, **kwargs
        )

    def _run(self, trigger_pk):
        return ai_companion_reply_task(
            self.tenant.schema_name, 'facebook', self.conn.page_id,
            'psid_task', trigger_pk,
        )

    def test_happy_path_reply(self):
        msg = self._inbound()
        with self.decision_patch, patch.object(
            ai_companion, 'send_ai_reply', return_value=True
        ) as mock_send:
            result = self._run(msg.pk)
        self.assertEqual(result, 'done:reply')
        mock_send.assert_called_once_with(
            'facebook', self.conn.page_id, 'psid_task', 'AI says hi'
        )
        state = AIConversationState.objects.get(
            platform='facebook', conversation_id='psid_task',
        )
        self.assertEqual(state.daily_reply_count, 1)
        self.assertEqual(state.total_ai_replies, 1)
        self.assertIsNotNone(state.last_ai_reply_at)

    def test_superseded_by_newer_message(self):
        old = self._inbound('old')
        self._inbound('new', timestamp=timezone.now() + timedelta(seconds=5))
        with self.decision_patch as mock_decide:
            result = self._run(old.pk)
        self.assertEqual(result, 'skip:superseded')
        mock_decide.assert_not_called()

    def test_active_assignment_suppresses(self):
        msg = self._inbound()
        agent = self.create_user(email='typer@test.com')
        self.create_chat_assignment(
            user=agent, platform='facebook',
            conversation_id='psid_task', account_id=self.conn.page_id,
            status='in_session',
        )
        with self.decision_patch as mock_decide:
            result = self._run(msg.pk)
        self.assertEqual(result, 'skip:assigned')
        mock_decide.assert_not_called()

    def test_mode_needs_human_skips(self):
        msg = self._inbound()
        AIConversationState.objects.create(
            platform='facebook', conversation_id='psid_task',
            account_id=self.conn.page_id, mode='needs_human',
        )
        result = self._run(msg.pk)
        self.assertEqual(result, 'skip:mode_needs_human')

    def test_meta_window_expired_skips(self):
        msg = self._inbound(timestamp=timezone.now() - timedelta(hours=25))
        with self.decision_patch as mock_decide:
            result = self._run(msg.pk)
        self.assertEqual(result, 'skip:window_expired')
        mock_decide.assert_not_called()

    def test_conversation_daily_cap(self):
        msg = self._inbound()
        AIConversationState.objects.create(
            platform='facebook', conversation_id='psid_task',
            account_id=self.conn.page_id, mode='ai',
            daily_reply_count=30, daily_count_date=timezone.now().date(),
        )
        result = self._run(msg.pk)
        self.assertEqual(result, 'skip:conversation_cap')

    def test_tenant_daily_cap(self):
        msg = self._inbound()
        self.settings_obj.max_replies_per_day = 0
        self.settings_obj.save()
        result = self._run(msg.pk)
        self.assertEqual(result, 'skip:tenant_cap')

    def test_disabled_settings_skip(self):
        msg = self._inbound()
        self.settings_obj.is_enabled = False
        self.settings_obj.save()
        result = self._run(msg.pk)
        self.assertEqual(result, 'skip:disabled')

    def test_handoff_flips_state_and_notifies(self):
        msg = self._inbound()
        with patch.object(
            ai_companion, 'run_decision',
            return_value={'action': 'handoff', 'reason': 'Customer wants a call with the owner'},
        ), patch(
            'users.notification_utils.create_ai_handoff_notification'
        ) as mock_notify, patch.object(ai_companion, '_broadcast_state') as mock_ws:
            result = self._run(msg.pk)

        self.assertEqual(result, 'done:handoff')
        state = AIConversationState.objects.get(
            platform='facebook', conversation_id='psid_task',
        )
        self.assertEqual(state.mode, 'needs_human')
        self.assertIn('call with the owner', state.reason)
        self.assertIsNotNone(state.escalated_at)
        mock_notify.assert_called_once()
        mock_ws.assert_called_once()

    def test_ignore_action(self):
        msg = self._inbound()
        with patch.object(
            ai_companion, 'run_decision',
            return_value={'action': 'ignore', 'reason': 'just a thanks'},
        ):
            result = self._run(msg.pk)
        self.assertEqual(result, 'done:ignore')
        self.assertEqual(AIConversationState.objects.count(), 0)

    def test_decision_error_does_not_raise(self):
        msg = self._inbound()
        with patch.object(
            ai_companion, 'run_decision', side_effect=AICompanionError('api down'),
        ):
            result = self._run(msg.pk)
        self.assertEqual(result, 'error:decision')


class TestAiViews(AICompanionTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='ai-admin@test.com')
        self.agent = self.create_user(email='ai-agent@test.com')

    def test_settings_get_creates_singleton(self):
        resp = self.api_get('/api/social/ai/settings/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['is_enabled'])
        self.assertFalse(resp.data['has_api_key'])
        self.assertEqual(AICompanionSettings.objects.count(), 1)

    def test_settings_patch_with_channels_and_key(self):
        resp = self.api_patch('/api/social/ai/settings/', {
            'is_enabled': True,
            'provider': 'openai',
            'guidance_prompt': 'Be nice.',
            'api_key': 'sk-secret',
            'channels': [
                {'platform': 'telegram', 'account_id': '', 'enabled': True},
            ],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data['is_enabled'])
        self.assertTrue(resp.data['has_api_key'])
        # Key must never be echoed back.
        self.assertNotIn('api_key', resp.data)
        self.assertEqual(len(resp.data['channels']), 1)
        self.assertEqual(resp.data['channels'][0]['platform'], 'telegram')
        obj = AICompanionSettings.objects.get()
        self.assertEqual(obj.api_key, 'sk-secret')

    def test_settings_write_requires_admin(self):
        resp = self.api_patch('/api/social/ai/settings/', {'is_enabled': True},
                              user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_summarize_endpoint(self):
        conn = self.create_fb_connection()
        self.create_fb_message(
            page_connection=conn, sender_id='psid_v1',
            message_text='order please', is_from_page=False,
        )
        payload = {'summary': 'Customer placed an order.'}
        with patch.object(
            ai_companion.ai_providers, 'generate_structured',
            return_value=(payload, {'prompt_tokens': 3, 'completion_tokens': 2}),
        ):
            resp = self.api_post('/api/social/ai/summarize/', {
                'platform': 'facebook',
                'conversation_id': 'psid_v1',
                'account_id': conn.page_id,
            }, user=self.agent)

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertIn('order', resp.data['summary']['summary_text'])
        self.assertEqual(resp.data['summary']['requested_by_name'], self.agent.email)

        listing = self.api_get(
            f'/api/social/ai/summaries/?platform=facebook'
            f'&conversation_id=psid_v1&account_id={conn.page_id}',
            user=self.agent,
        )
        self.assertEqual(len(listing.data['results']), 1)

    def test_summarize_provider_error_422(self):
        conn = self.create_fb_connection()
        self.create_fb_message(
            page_connection=conn, sender_id='psid_v2',
            message_text='hi', is_from_page=False,
        )
        with patch.object(
            ai_companion.ai_providers, 'generate_structured',
            side_effect=AICompanionError('quota'),
        ):
            resp = self.api_post('/api/social/ai/summarize/', {
                'platform': 'facebook',
                'conversation_id': 'psid_v2',
                'account_id': conn.page_id,
            }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_summarize_empty_conversation_400(self):
        resp = self.api_post('/api/social/ai/summarize/', {
            'platform': 'facebook',
            'conversation_id': 'nobody',
            'account_id': 'nopage',
        }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_state_default_and_override(self):
        params = 'platform=facebook&conversation_id=c9&account_id=p9'
        resp = self.api_get(f'/api/social/ai/state/?{params}', user=self.agent)
        self.assertEqual(resp.data['mode'], 'ai')

        with patch.object(ai_companion, '_broadcast_state'):
            resp = self.api_post('/api/social/ai/state/', {
                'platform': 'facebook', 'conversation_id': 'c9',
                'account_id': 'p9', 'mode': 'off',
            }, user=self.agent)
        self.assertEqual(resp.data['mode'], 'off')

        with patch.object(ai_companion, '_broadcast_state'):
            resp = self.api_post('/api/social/ai/state/', {
                'platform': 'facebook', 'conversation_id': 'c9',
                'account_id': 'p9', 'mode': 'ai',
            }, user=self.agent)
        self.assertEqual(resp.data['mode'], 'ai')
        self.assertEqual(resp.data['reason'], '')
