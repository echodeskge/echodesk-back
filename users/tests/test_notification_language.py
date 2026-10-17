"""
Per-recipient notification language (users/notification_texts.py).

- A 'ka' user gets Georgian title/message, an 'en' user English.
- No user language → the tenant's preferred_language → 'ka'.
- A fan-out renders per recipient (mixed languages in one call).
- Web/Expo push payloads carry the same localized texts.
- Batching suffix is localized.
- preferred_language is readable/writable on the profile and user endpoints.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model

from users.models import Notification
from users.notification_texts import (
    CATALOG, LazyText, T, normalize_language, render, tenant_language, user_language,
)
from users.notification_utils import (
    create_notification, create_social_message_notification, team_chat_message_preview,
)
from users.tests.conftest import EchoDeskTenantTestCase

User = get_user_model()

NO_WS = patch('asgiref.sync.async_to_sync', return_value=lambda **kw: None)


class CatalogTests(EchoDeskTenantTestCase):
    def test_every_entry_has_english_and_georgian(self):
        for key, entry in CATALOG.items():
            self.assertTrue(entry.get('en'), key)
            self.assertTrue(entry.get('ka'), key)
            # Same placeholders in both languages.
            import string
            fields = {
                lang: sorted(f for _, f, _, _ in string.Formatter().parse(entry[lang]) if f)
                for lang in ('en', 'ka')
            }
            self.assertEqual(fields['en'], fields['ka'], key)

    def test_render_plain_string_unchanged(self):
        self.assertEqual(render('Free text {x}', 'ka'), 'Free text {x}')

    def test_nested_params_rendered_in_same_language(self):
        text = T('ticket.assigned.message', assigner=T('common.someone'), role=T('ticket.role.reviewer'))
        self.assertEqual(render(text, 'en'), 'Someone assigned you to this ticket as Reviewer')
        self.assertEqual(
            render(text, 'ka'), 'უცნობი მომხმარებელი — დაგნიშნათ ამ ტიკეტზე როლით „შემმოწმებელი“'
        )

    def test_param_values_are_not_reformatted(self):
        self.assertEqual(render(T('call.missed.message', number='{evil}'), 'en'),
                         'Missed inbound call from {evil}')

    def test_normalize_language(self):
        self.assertEqual(normalize_language('ka-GE'), 'ka')
        self.assertEqual(normalize_language('EN_us'), 'en')
        self.assertIsNone(normalize_language('ru'))
        self.assertIsNone(normalize_language(''))


class LanguageResolutionTests(EchoDeskTenantTestCase):
    def setUp(self):
        super().setUp()
        self._original = self.tenant.preferred_language

    def tearDown(self):
        self.tenant.preferred_language = self._original
        self.tenant.save(update_fields=['preferred_language'])
        super().tearDown()

    def _set_tenant_language(self, language):
        self.tenant.preferred_language = language
        self.tenant.save(update_fields=['preferred_language'])

    def test_user_language_wins(self):
        self._set_tenant_language('en')
        user = self.create_user(email='lang-ka@test.com', preferred_language='ka')
        self.assertEqual(user_language(user), 'ka')

    def test_falls_back_to_tenant_language(self):
        user = self.create_user(email='lang-none@test.com')
        self._set_tenant_language('ka')
        self.assertEqual(user_language(user), 'ka')
        self._set_tenant_language('en')
        self.assertEqual(user_language(user), 'en')

    def test_unsupported_tenant_language_falls_back_to_georgian(self):
        self._set_tenant_language('ru')
        self.assertEqual(tenant_language(), 'ka')

    def test_unknown_schema_falls_back_to_georgian(self):
        self.assertEqual(tenant_language('no_such_schema'), 'ka')


@NO_WS
class CreateNotificationLanguageTests(EchoDeskTenantTestCase):
    def setUp(self):
        super().setUp()
        self._original = self.tenant.preferred_language
        self.tenant.preferred_language = 'en'
        self.tenant.save(update_fields=['preferred_language'])
        self.ka_user = self.create_user(email='ka-notif@test.com', preferred_language='ka')
        self.en_user = self.create_user(email='en-notif@test.com', preferred_language='en')
        self.default_user = self.create_user(email='default-notif@test.com')

    def tearDown(self):
        self.tenant.preferred_language = self._original
        self.tenant.save(update_fields=['preferred_language'])
        super().tearDown()

    def _missed_call(self, user, **kwargs):
        return create_notification(
            user=user,
            notification_type='call_missed',
            title=T('call.missed.title'),
            message=T('call.missed.message', number='+995555123456'),
            metadata={'call_log_id': 7},
            link_url='/calls?id=7',
            **kwargs,
        )

    def test_georgian_user_gets_georgian(self, _ws):
        n = self._missed_call(self.ka_user)
        self.assertEqual(n.title, 'გამოტოვებული ზარი')
        self.assertEqual(n.message, 'გამოტოვებული შემომავალი ზარი ნომრიდან +995555123456')
        # Metadata / link untouched.
        self.assertEqual(n.metadata, {'call_log_id': 7})
        self.assertEqual(n.link_url, '/calls?id=7')

    def test_english_user_gets_english(self, _ws):
        n = self._missed_call(self.en_user)
        self.assertEqual(n.title, 'Missed call')
        self.assertEqual(n.message, 'Missed inbound call from +995555123456')

    def test_user_without_language_follows_tenant(self, _ws):
        self.assertEqual(self._missed_call(self.default_user).title, 'Missed call')
        Notification.objects.filter(user=self.default_user).delete()
        self.tenant.preferred_language = 'ka'
        self.tenant.save(update_fields=['preferred_language'])
        self.assertEqual(self._missed_call(self.default_user).title, 'გამოტოვებული ზარი')

    def test_explicit_language_override(self, _ws):
        self.assertEqual(self._missed_call(self.en_user, language='ka').title, 'გამოტოვებული ზარი')

    def test_plain_strings_stored_as_is(self, _ws):
        n = create_notification(self.ka_user, 'system', 'Plain title', 'Plain body')
        self.assertEqual((n.title, n.message), ('Plain title', 'Plain body'))

    def test_batch_suffix_localized(self, _ws):
        self._missed_call(self.ka_user)
        n = self._missed_call(self.ka_user)
        self.assertEqual(n.message, 'გამოტოვებული შემომავალი ზარი ნომრიდან +995555123456 (+1 სხვა)')
        self._missed_call(self.en_user)
        n = self._missed_call(self.en_user)
        self.assertTrue(n.message.endswith('(+1 more)'))

    def test_mixed_language_fan_out(self, _ws):
        sent = []
        with patch('users.notification_utils.social_message_recipients',
                   return_value=[self.ka_user, self.en_user]):
            sent = create_social_message_notification(
                platform='Widget',
                sender_name=T('social.widget.visitor', visitor='abc123'),
                message_text=T('social.widget.attachment'),
                conversation_id='sess-1',
                sender_id='visitor-1',
                account_id='5',
                chat_conversation_key='sess-1',
            )
        by_user = {n.user_id: n for n in sent}
        ka, en = by_user[self.ka_user.id], by_user[self.en_user.id]
        self.assertEqual(ka.title, 'ახალი შეტყობინება (ვებსაიტის ვიჯეტი): ვებსაიტის ვიზიტორი abc123')
        self.assertEqual(ka.message, 'გამოგზავნა ფაილი')
        self.assertEqual(en.title, 'New Widget message from Website visitor abc123')
        self.assertEqual(en.message, 'Sent an attachment')
        # Metadata identical for both (English sender name, as before).
        self.assertEqual(ka.metadata, en.metadata)
        self.assertEqual(ka.metadata['sender_name'], 'Website visitor abc123')
        self.assertEqual(ka.link_url, '/messages/widget_5_sess-1')

    def test_social_user_text_not_translated(self, _ws):
        with patch('users.notification_utils.social_message_recipients', return_value=[self.ka_user]):
            [n] = create_social_message_notification(
                platform='Facebook', sender_name='Nino', message_text='Hello there',
                conversation_id='1', sender_id='1', account_id='9',
            )
        self.assertEqual(n.title, 'ახალი შეტყობინება (Facebook): Nino')
        self.assertEqual(n.message, 'Hello there')

    def test_lazy_text(self, _ws):
        n = create_notification(
            self.ka_user, 'system', LazyText(lambda lang: f'title-{lang}'), 'body',
        )
        self.assertEqual(n.title, 'title-ka')

    def test_push_payload_localized(self, _ws):
        with patch('users.notification_utils._dispatch_push') as push:
            self._missed_call(self.ka_user)
            self._missed_call(self.en_user)
        calls = {c.kwargs['user'].id: c.kwargs for c in push.call_args_list}
        self.assertEqual(calls[self.ka_user.id]['title'], 'გამოტოვებული ზარი')
        self.assertEqual(calls[self.ka_user.id]['body'],
                         'გამოტოვებული შემომავალი ზარი ნომრიდან +995555123456')
        self.assertEqual(calls[self.en_user.id]['title'], 'Missed call')
        self.assertEqual(calls[self.en_user.id]['body'], 'Missed inbound call from +995555123456')

    def test_push_task_receives_localized_texts(self, _ws):
        from notifications.models import DevicePushToken

        DevicePushToken.objects.create(
            user=self.ka_user, token='ExponentPushToken[lang-test]', platform='ios',
        )
        task = MagicMock()
        with patch('notifications.tasks.deliver_push_notification_task', task):
            self._missed_call(self.ka_user)
        kwargs = task.delay.call_args.kwargs
        self.assertEqual(kwargs['title'], 'გამოტოვებული ზარი')
        self.assertEqual(kwargs['body'], 'გამოტოვებული შემომავალი ზარი ნომრიდან +995555123456')


class TeamChatPreviewTests(EchoDeskTenantTestCase):
    def _msg(self, **kw):
        base = {'text': '', 'message_type': 'text', 'file_name': ''}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_previews(self):
        self.assertEqual(team_chat_message_preview(self._msg(message_type='image')), 'Sent a photo')
        self.assertEqual(team_chat_message_preview(self._msg(message_type='image'), 'ka'), 'გამოგზავნა ფოტო')
        self.assertEqual(team_chat_message_preview(self._msg(message_type='voice'), 'ka'),
                         'გამოგზავნა ხმოვანი შეტყობინება')
        self.assertEqual(team_chat_message_preview(self._msg(message_type='file', file_name='a.pdf'), 'ka'),
                         'გამოგზავნა ფაილი: a.pdf')
        self.assertEqual(team_chat_message_preview(self._msg(text='გამარჯობა'), 'en'), 'გამარჯობა')


class PreferredLanguageEndpointTests(EchoDeskTenantTestCase):
    PROFILE_URL = '/api/auth/profile/'
    PROFILE_UPDATE_URL = '/api/auth/profile/update/'

    def test_profile_get_and_patch(self):
        user = self.create_user(email='profile-lang@test.com')
        resp = self.api_get(self.PROFILE_URL, user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data['preferred_language'])

        resp = self.api_patch(self.PROFILE_UPDATE_URL, {'preferred_language': 'ka'}, user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['user']['preferred_language'], 'ka')
        user.refresh_from_db()
        self.assertEqual(user.preferred_language, 'ka')

        # Other fields alone leave the language alone.
        self.api_patch(self.PROFILE_UPDATE_URL, {'first_name': 'Nino'}, user=user)
        user.refresh_from_db()
        self.assertEqual(user.preferred_language, 'ka')

        resp = self.api_patch(self.PROFILE_UPDATE_URL, {'preferred_language': None}, user=user)
        self.assertEqual(resp.status_code, 200)
        user.refresh_from_db()
        self.assertIsNone(user.preferred_language)

    def test_profile_patch_rejects_unsupported_language(self):
        user = self.create_user(email='profile-lang-bad@test.com')
        resp = self.api_patch(self.PROFILE_UPDATE_URL, {'preferred_language': 'ru'}, user=user)
        self.assertEqual(resp.status_code, 400)
        user.refresh_from_db()
        self.assertIsNone(user.preferred_language)

    def test_accept_language_does_not_change_preference(self):
        user = self.create_user(email='profile-lang-hdr@test.com', preferred_language='en')
        self.api_get(self.PROFILE_URL, user=user, HTTP_ACCEPT_LANGUAGE='ka')
        user.refresh_from_db()
        self.assertEqual(user.preferred_language, 'en')

    def test_admin_user_update(self):
        admin = self.create_admin(email='lang-admin@test.com')
        user = self.create_user(email='lang-target@test.com')
        resp = self.api_patch(f'/api/users/{user.id}/', {'preferred_language': 'ka'}, user=admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        user.refresh_from_db()
        self.assertEqual(user.preferred_language, 'ka')
        resp = self.api_get(f'/api/users/{user.id}/', user=admin)
        self.assertEqual(resp.data['preferred_language'], 'ka')
