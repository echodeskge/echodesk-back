"""Inbox onboarding step: permissions, context (connections without tokens,
presets), prefill, apply (settings, away hours, auto-replies, assignment,
rating, quick replies, AI), idempotency — and the social / AI settings
endpoints being open to tenant admins."""
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlparse

from django.conf import settings as dj_settings
from django.test import override_settings

from onboarding.models import TenantOnboarding
from onboarding.tests.helpers import enable_features
from social_integrations.models import (
    AICompanionChannel,
    AICompanionSettings,
    EmailConnection,
    FacebookPageConnection,
    QuickReply,
    SocialIntegrationSettings,
)
from social_integrations.oauth_state import make_oauth_state, oauth_result_redirect, read_oauth_state
from social_integrations.onboarding import QUICK_REPLY_PRESETS, SocialStep
from tenants.models import Feature, Tenant
from users.tests.conftest import EchoDeskTenantTestCase

ONB = '/api/onboarding/'
APPLY = f'{ONB}social_integrations/apply/'

HOURS = {
    'mon': {'from': '09:00', 'to': '18:00', 'break_from': '13:00', 'break_to': '14:00'},
    'tue': {'from': '09:00', 'to': '18:30', 'break_from': None, 'break_to': None},
    'wed': {'from': '10:00', 'to': '17:00'},
    'thu': {'from': '09:00', 'to': '18:00'},
    'fri': {'from': '09:00', 'to': '15:00'},
    'sat': None,
    # sun missing = closed
}


def answers(**overrides):
    base = {
        'channels': ['facebook', 'email', 'widget', 'facebook'],
        'timezone': 'Asia/Tbilisi',
        'hours': HOURS,
        'welcome_message': {'ka': 'გამარჯობა!', 'en': 'Hello!'},
        'away_message': {'ka': '', 'en': 'We are closed.'},
        'assignment': 'claim',
        'collect_rating': True,
        'quick_replies': [
            {'title': 'Thanks', 'message': {'ka': 'მადლობა', 'en': 'Thank you'}},
            {'title': 'Hours', 'message': {'ka': '', 'en': 'Mon-Fri 9-18'}},
        ],
        'ai': {'enabled': True, 'company_description': 'We sell shoes.', 'language': 'en'},
    }
    base.update(overrides)
    return base


class SocialStepTests(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        enable_features(self.tenant, 'social_integrations')
        self.owner = self.create_user(
            email='owner@test.com', role='admin', is_staff=True, is_superuser=True,
        )
        self.agent = self.create_user(email='agent@test.com')

    def _apply(self, data, user=None):
        return self.api_post(APPLY, {'answers': data}, user=user or self.owner)

    def _step(self):
        for step in self.api_get(ONB, user=self.owner).data['steps']:
            if step['key'] == 'social_integrations':
                return step
        self.fail('social step not listed')

    def _page(self, page_id='p1', name='Shoes', active=True):
        return FacebookPageConnection.objects.create(
            page_id=page_id, page_name=name, page_access_token='SECRET-TOKEN', is_active=active,
            connected_by=self.owner,
        )

    def _email(self):
        return EmailConnection.objects.create(
            email_address='hello@shoes.ge', display_name='Shoes', imap_server='imap.x', smtp_server='smtp.x',
            username='hello@shoes.ge', encrypted_password='x',
        )

    # ── permissions ──

    def test_non_admin_gets_403(self):
        self.assertEqual(self._apply(answers(), user=self.agent).status_code, 403)

    # ── read ──

    def test_context_shape_without_tokens(self):
        self._page()
        self._page('p2', 'Old', active=False)
        self._email()
        step = self._step()
        self.assertEqual(step['order'], 20)
        ctx = step['context']
        self.assertEqual(set(ctx.keys()), {'users', 'connections', 'oauth', 'has_ai_feature', 'quick_reply_presets'})
        self.assertEqual(
            ctx['connections']['facebook'],
            [
                {'id': FacebookPageConnection.objects.get(page_id='p2').id, 'page_id': 'p2', 'name': 'Old', 'is_active': False},
                {'id': FacebookPageConnection.objects.get(page_id='p1').id, 'page_id': 'p1', 'name': 'Shoes', 'is_active': True},
            ],
        )
        self.assertNotIn('SECRET-TOKEN', str(ctx))
        self.assertEqual(ctx['connections']['email'][0]['email'], 'hello@shoes.ge')
        self.assertEqual(ctx['connections']['instagram'], [])
        self.assertEqual(ctx['connections']['whatsapp'], [])
        self.assertIsNone(ctx['connections']['telegram'])
        self.assertEqual(ctx['connections']['widget'], [])
        self.assertEqual(ctx['oauth']['facebook_start'], '/api/social/facebook/oauth/start/')
        self.assertFalse(ctx['has_ai_feature'])
        presets = ctx['quick_reply_presets']
        self.assertEqual(set(presets.keys()), set(QUICK_REPLY_PRESETS.keys()))
        for industry, rows in presets.items():
            self.assertEqual(len(rows), 3, industry)
            for row in rows:
                self.assertIsInstance(row['title'], str)
                self.assertTrue(row['message']['ka'] and row['message']['en'])
                self.assertEqual(set(row['title_i18n'].keys()), {'ka', 'en'})
        self.assertEqual(presets['salon'][0]['title'], 'Book a visit')

    def test_prefill_shape_and_defaults(self):
        self._page()
        prefill = self._step()['prefill']
        self.assertEqual(prefill['channels'], ['facebook'])
        self.assertEqual(prefill['timezone'], 'Asia/Tbilisi')
        self.assertEqual(prefill['hours']['mon']['from'], '09:00')
        self.assertIsNone(prefill['hours']['sun'])
        self.assertTrue(prefill['welcome_message']['ka'] and prefill['welcome_message']['en'])
        self.assertEqual(prefill['assignment'], 'shared')
        self.assertFalse(prefill['collect_rating'])
        self.assertEqual(len(prefill['quick_replies']), 3)
        self.assertIsNone(prefill['ai'])

    def test_prefill_reflects_applied_settings(self):
        self.assertEqual(self._apply(answers()).status_code, 200)
        prefill = self._step()['prefill']
        self.assertEqual(prefill['assignment'], 'claim')
        self.assertTrue(prefill['collect_rating'])
        self.assertEqual(prefill['hours']['mon'], {'from': '09:00', 'to': '18:00', 'break_from': None, 'break_to': None})
        self.assertEqual(prefill['hours']['tue']['to'], '19:00')   # 18:30 rounds up to the hour grid
        self.assertIsNone(prefill['hours']['sat'])
        self.assertEqual([q['title'] for q in prefill['quick_replies']], ['Thanks', 'Hours'])
        self.assertEqual(prefill['welcome_message']['en'], 'Hello!')

    # ── apply ──

    def test_apply_writes_settings(self):
        resp = self._apply(answers())
        self.assertEqual(resp.status_code, 200, resp.data)
        result = resp.data['result']
        self.assertEqual(result['created']['settings'], 1)
        self.assertEqual(result['created']['quick_replies'], 2)
        self.assertIn('AI assistant answers ignored: the ai_companion feature is not enabled.', result['notes'])
        self.assertNotIn('ai_settings', result['created'])

        s = SocialIntegrationSettings.objects.get()
        self.assertEqual(s.timezone, 'Asia/Tbilisi')
        self.assertTrue(s.away_hours_enabled)
        self.assertEqual(s.away_hours_schedule['monday'], list(range(0, 9)) + list(range(18, 24)))
        self.assertEqual(s.away_hours_schedule['tuesday'], list(range(0, 9)) + list(range(19, 24)))
        self.assertEqual(s.away_hours_schedule['wednesday'], list(range(0, 10)) + list(range(17, 24)))
        self.assertEqual(s.away_hours_schedule['friday'], list(range(0, 9)) + list(range(15, 24)))
        self.assertEqual(s.away_hours_schedule['saturday'], list(range(24)))
        self.assertEqual(s.away_hours_schedule['sunday'], list(range(24)))
        self.assertEqual(set(s.away_hours_schedule.keys()), {
            'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
        })

        for platform in ('facebook', 'instagram', 'whatsapp'):
            entry = s.auto_reply_settings[platform]
            self.assertEqual(entry['welcome_message'], 'Hello!', platform)     # tenant language en
            self.assertTrue(entry['welcome_enabled'])
            self.assertEqual(entry['away_message'], 'We are closed.')
            self.assertTrue(entry['away_enabled'])
        self.assertNotIn('email', s.auto_reply_settings)

        self.assertTrue(s.chat_assignment_enabled)
        self.assertTrue(s.session_management_enabled)
        self.assertTrue(s.hide_assigned_chats)
        self.assertTrue(s.stale_assignment_reminder_enabled)
        self.assertEqual(s.stale_assignment_reminder_minutes, 60)
        self.assertTrue(s.collect_customer_rating)
        self.assertTrue(s.link_based_rating_enabled)
        self.assertTrue(s.widget_enabled)

        thanks = QuickReply.objects.get(title='Thanks')
        self.assertEqual(thanks.message, 'Thank you')
        self.assertEqual(thanks.platforms, [])
        self.assertEqual(thanks.created_by, self.owner)
        self.assertEqual(QuickReply.objects.get(title='Hours').message, 'Mon-Fri 9-18')

        self.assertFalse(AICompanionSettings.objects.exists())
        stored = TenantOnboarding.get().answers_of('social_integrations')
        self.assertEqual(stored['channels'], ['facebook', 'email', 'widget'])
        self.assertIsNone(stored['hours']['sun'])

    def test_language_fallback_in_messages(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(preferred_language='ka')
        self._apply({
            'welcome_message': {'ka': 'გამარჯობა!', 'en': 'Hello!'},
            'away_message': {'ka': '', 'en': 'Closed.'},
            'quick_replies': [{'title': 'Hours', 'message': {'ka': '', 'en': 'Mon-Fri'}}],
        })
        s = SocialIntegrationSettings.objects.get()
        self.assertEqual(s.auto_reply_settings['whatsapp']['welcome_message'], 'გამარჯობა!')
        self.assertEqual(s.auto_reply_settings['whatsapp']['away_message'], 'Closed.')
        self.assertEqual(QuickReply.objects.get(title='Hours').message, 'Mon-Fri')

    def test_shared_assignment_and_empty_messages(self):
        self._apply(answers())
        resp = self._apply({'assignment': 'shared', 'collect_rating': False, 'welcome_message': {'ka': '', 'en': ''}})
        self.assertEqual(resp.status_code, 200, resp.data)
        s = SocialIntegrationSettings.objects.get()
        self.assertFalse(s.chat_assignment_enabled)
        self.assertFalse(s.hide_assigned_chats)
        self.assertFalse(s.collect_customer_rating)
        self.assertFalse(s.auto_reply_settings['facebook']['welcome_enabled'])
        self.assertEqual(s.auto_reply_settings['facebook']['welcome_message'], '')
        self.assertTrue(s.auto_reply_settings['facebook']['away_enabled'])   # untouched
        self.assertTrue(s.away_hours_enabled)                                # untouched

    def test_apply_twice_creates_nothing_new(self):
        self.assertEqual(self._apply(answers()).status_code, 200)
        resp = self._apply(answers())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['result']['created'].get('settings', 0), 0)
        self.assertEqual(resp.data['result']['created']['quick_replies'], 0)
        self.assertEqual(SocialIntegrationSettings.objects.count(), 1)
        self.assertEqual(QuickReply.objects.count(), 2)

    def test_ai_applied_only_with_feature(self):
        enable_features(self.tenant, 'ai_companion')
        page = self._page()
        self._page('p2', 'Old', active=False)
        self._email()
        self.assertTrue(self._step()['context']['has_ai_feature'])
        self.assertIsNotNone(self._step()['prefill']['ai'])

        resp = self._apply(answers())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['result']['created']['ai_settings'], 1)
        self.assertEqual(resp.data['result']['created']['ai_channels'], 1)
        ai = AICompanionSettings.objects.get()
        self.assertTrue(ai.is_enabled)
        self.assertEqual(ai.guidance_prompt, 'We sell shoes.')
        self.assertEqual(ai.language, 'en')
        channel = AICompanionChannel.objects.get()
        self.assertEqual((channel.platform, channel.account_id, channel.enabled), ('facebook', page.page_id, True))

        # Re-apply disabled: same rows, flipped.
        resp = self._apply({'ai': {'enabled': False, 'company_description': 'We sell shoes.', 'language': 'ka'}})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['result']['created']['ai_settings'], 0)
        self.assertEqual(AICompanionSettings.objects.count(), 1)
        self.assertEqual(AICompanionChannel.objects.count(), 1)
        self.assertFalse(AICompanionSettings.objects.get().is_enabled)
        self.assertFalse(AICompanionChannel.objects.get().enabled)
        self.assertEqual(self._step()['prefill']['ai'], {'enabled': False, 'company_description': 'We sell shoes.', 'language': 'ka'})

    def test_validation_errors(self):
        resp = self._apply({'timezone': 'Mars/Olympus'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('timezone', resp.data['errors'])
        resp = self._apply({'hours': {'mon': {'from': '18:00', 'to': '09:00'}}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('hours', resp.data['errors'])
        resp = self._apply({'quick_replies': [{'title': 'A', 'message': {'ka': '', 'en': ''}}]})
        self.assertEqual(resp.status_code, 400)
        resp = self._apply({'channels': ['tiktok']})
        self.assertEqual(resp.status_code, 400)

    def test_seed_defaults_is_a_no_op(self):
        SocialStep().seed_defaults(self.tenant)
        SocialStep().seed_defaults(self.tenant)
        self.assertFalse(SocialIntegrationSettings.objects.exists())


class SocialSettingsAccessTests(EchoDeskTenantTestCase):
    """The web opens the assignment / auto-reply / AI tabs of settings/social
    to tenant admins: `/api/social/settings/` and `/api/social/ai/settings/`
    must be readable and writable by an admin-role user (not staff) and by a
    user holding `manage_social_settings`, given the module features."""

    def setUp(self):
        super().setUp()
        enable_features(self.tenant, 'social_integrations', 'ai_companion')
        group = self.create_tenant_group(name='Inbox')
        group.features.add(*Feature.objects.filter(key__in=['social_integrations', 'ai_companion']))
        self.role_admin = self.create_user(email='roleadmin@test.com', role='admin')
        self.settings_user = self.create_user(email='social@test.com', can_manage_social_settings=True)
        self.agent = self.create_user(email='agent@test.com')
        for user in (self.role_admin, self.settings_user, self.agent):
            user.tenant_groups.add(group)

    def test_tenant_admins_can_read_and_write_social_settings(self):
        for user in (self.role_admin, self.settings_user):
            self.assertEqual(self.api_get('/api/social/settings/', user=user).status_code, 200, user.email)
            resp = self.api_patch('/api/social/settings/', {'chat_assignment_enabled': True}, user=user)
            self.assertEqual(resp.status_code, 200, (user.email, resp.data))
        self.assertTrue(SocialIntegrationSettings.objects.get().chat_assignment_enabled)

    def test_tenant_admins_can_read_and_write_ai_settings(self):
        for user in (self.role_admin, self.settings_user):
            self.assertEqual(self.api_get('/api/social/ai/settings/', user=user).status_code, 200, user.email)
            resp = self.api_patch('/api/social/ai/settings/', {'language': 'en'}, user=user)
            self.assertEqual(resp.status_code, 200, (user.email, resp.data))
        self.assertEqual(AICompanionSettings.objects.get().language, 'en')

    def test_agents_can_read_but_not_write(self):
        self.assertEqual(self.api_get('/api/social/settings/', user=self.agent).status_code, 200)
        self.assertEqual(self.api_patch('/api/social/settings/', {'chat_assignment_enabled': True}, user=self.agent).status_code, 403)
        self.assertEqual(self.api_get('/api/social/ai/settings/', user=self.agent).status_code, 200)
        self.assertEqual(self.api_patch('/api/social/ai/settings/', {'language': 'en'}, user=self.agent).status_code, 403)


class OAuthReturnToOnboardingTests(EchoDeskTenantTestCase):
    """The wizard starts the Facebook / WhatsApp OAuth flows with
    ``return_to=onboarding``; the callbacks then land on
    ``<tenant frontend>/onboarding?step=social_integrations`` (same
    mechanism as ``return_to=app``)."""

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()
        self.admin = self.create_user(email='owner@test.com', role='admin', is_staff=True, is_superuser=True)
        self._feature = patch('users.models.User.has_feature', return_value=True)
        self._feature.start()

    def tearDown(self):
        self._feature.stop()
        super().tearDown()

    @staticmethod
    def _state_from(oauth_url):
        return unquote(parse_qs(urlparse(oauth_url).query)['state'][0])

    def _social_settings(self, **extra):
        return dict(dj_settings.SOCIAL_INTEGRATIONS, FACEBOOK_APP_ID='123', WHATSAPP_APP_ID='123', **extra)

    def test_facebook_start_signs_return_to_onboarding(self):
        with override_settings(SOCIAL_INTEGRATIONS=self._social_settings()):
            resp = self.api_get('/api/social/facebook/oauth/start/?return_to=onboarding', user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        state = read_oauth_state(self._state_from(resp.data['oauth_url']))
        self.assertEqual(state['tenant'], 'test')
        self.assertEqual(state['return_to'], 'onboarding')

    def test_whatsapp_start_signs_return_to_onboarding(self):
        with override_settings(SOCIAL_INTEGRATIONS=self._social_settings()):
            resp = self.api_get('/api/social/whatsapp/oauth/start/?return_to=onboarding', user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        state = read_oauth_state(self._state_from(resp.data['oauth_url']))
        self.assertEqual(state['return_to'], 'onboarding')

    def test_unknown_return_to_falls_back_to_the_dashboard(self):
        with override_settings(SOCIAL_INTEGRATIONS=self._social_settings()):
            resp = self.api_get('/api/social/facebook/oauth/start/?return_to=evil', user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIsNone(read_oauth_state(self._state_from(resp.data['oauth_url']))['return_to'])

    def test_facebook_callback_redirects_to_the_wizard(self):
        state = make_oauth_state('test', user_id=self.admin.id, return_to='onboarding')
        resp = self.client.get(
            '/api/social/facebook/oauth/callback/',
            {'error': 'access_denied', 'error_description': 'denied', 'state': state},
            HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, 302)
        loc = urlparse(resp['Location'])
        self.assertEqual((loc.scheme, loc.netloc, loc.path), ('https', f'test.{dj_settings.MAIN_DOMAIN}', '/onboarding'))
        q = parse_qs(loc.query)
        self.assertEqual(q['step'], ['social_integrations'])
        self.assertEqual(q['facebook_status'], ['error'])
        self.assertIn('message', q)

    def test_result_redirect_success_shape(self):
        state = read_oauth_state(make_oauth_state('test', return_to='onboarding'))
        resp = oauth_result_redirect(platform='whatsapp', ok=True, state=state)
        self.assertEqual(
            resp['Location'],
            f'https://test.{dj_settings.MAIN_DOMAIN}/onboarding?step=social_integrations&whatsapp_status=connected',
        )
        # The dashboard return is unchanged.
        plain = read_oauth_state(make_oauth_state('test'))
        self.assertEqual(
            oauth_result_redirect(platform='whatsapp', ok=True, state=plain)['Location'],
            f'https://test.{dj_settings.MAIN_DOMAIN}/social/connections?whatsapp_status=connected',
        )
