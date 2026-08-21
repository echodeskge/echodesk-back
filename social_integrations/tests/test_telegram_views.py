"""
Tests for the Telegram (MTProto) integration.

Telethon and the telegram-worker are always mocked — no network. Covers:
- login flow (send code / verify / 2FA / error branches)
- session stored encrypted at rest
- send flow (worker HTTP contract + post-send extras + failure modes)
- unified conversations block + unread counts + mark-read branch
- platform routing registration
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.db import connection as db_connection
from django.utils import timezone
from rest_framework import status

from social_integrations.models import (
    ConversationArchive,
    TelegramAccount,
    TelegramMessage,
)
from .conftest import SocialIntegrationTestCase


def make_account(**kwargs):
    n = SocialIntegrationTestCase._next()
    defaults = {
        'telegram_user_id': 100000 + n,
        'phone_number': f'+9955990000{n:02d}',
        'first_name': 'Support',
        'username': f'support_{n}',
        'session_string': f'1SessionString{n}==',
        'is_active': True,
    }
    defaults.update(kwargs)
    return TelegramAccount.objects.create(**defaults)


def make_message(account, **kwargs):
    n = SocialIntegrationTestCase._next()
    defaults = {
        'account': account,
        'message_id': f'{account.telegram_user_id}_{n}',
        'telegram_msg_id': n,
        'peer_id': 555000 + (kwargs.get('peer_n') or 1),
        'peer_access_hash': 987654321,
        'peer_name': 'Customer',
        'message_text': f'hello {n}',
        'timestamp': timezone.now(),
        'is_from_business': False,
    }
    kwargs.pop('peer_n', None)
    defaults.update(kwargs)
    return TelegramMessage.objects.create(**defaults)


class TelegramTestCase(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email=f'tg-admin-{self._next()}@test.com')
        self.agent = self.create_user(email=f'tg-agent-{self._next()}@test.com')


class TestTelegramLogin(TelegramTestCase):
    CONNECT_URL = '/api/social/telegram/connect/'
    VERIFY_URL = '/api/social/telegram/verify/'

    def _configured(self):
        return patch.dict(
            'django.conf.settings.SOCIAL_INTEGRATIONS',
            {'TELEGRAM_API_ID': '12345', 'TELEGRAM_API_HASH': 'abcdef'},
        )

    def test_connect_sends_code_and_returns_token(self):
        with self._configured(), \
             patch('social_integrations.telegram_views._run_async',
                   return_value=('1SessionABC==', 'hash123')):
            resp = self.api_post(self.CONNECT_URL, {'phone_number': '+995599123456'},
                                 user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'code_sent')
        self.assertTrue(resp.data['login_token'])
        # State cached, session encrypted (not stored as plaintext)
        state = cache.get(f"tg-login:{resp.data['login_token']}")
        self.assertIsNotNone(state)
        self.assertNotEqual(state['session'], '1SessionABC==')

    def test_connect_rejects_bad_phone(self):
        with self._configured():
            resp = self.api_post(self.CONNECT_URL, {'phone_number': 'not-a-phone'},
                                 user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_connect_unconfigured_is_503(self):
        with patch.dict('django.conf.settings.SOCIAL_INTEGRATIONS',
                        {'TELEGRAM_API_ID': '', 'TELEGRAM_API_HASH': ''}):
            resp = self.api_post(self.CONNECT_URL, {'phone_number': '+995599123456'},
                                 user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_verify_expired_token_is_410(self):
        with self._configured():
            resp = self.api_post(self.VERIFY_URL,
                                 {'login_token': 'gone', 'code': '12345'},
                                 user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_410_GONE)

    def _seed_login_state(self, token='tok1'):
        from crm.fields import _load_fernets
        cache.set(f'tg-login:{token}', {
            'session': _load_fernets().encrypt(b'1PartialSession==').decode(),
            'phone': '+995599123456',
            'phone_code_hash': 'hash123',
        }, 600)
        return token

    def test_verify_success_persists_encrypted_account(self):
        token = self._seed_login_state()
        result = {
            'status': 'connected',
            'session': '1FinalSession==',
            'me': {'id': 424242, 'first_name': 'Gio', 'last_name': '',
                   'username': 'gio_support', 'phone': '+995599123456'},
        }
        with self._configured(), \
             patch('social_integrations.telegram_views._run_async', return_value=result), \
             patch('social_integrations.telegram_views._nudge_worker') as mock_nudge:
            resp = self.api_post(self.VERIFY_URL,
                                 {'login_token': token, 'code': '12345'},
                                 user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'connected')

        account = TelegramAccount.objects.get(telegram_user_id=424242)
        self.assertTrue(account.is_active)
        self.assertEqual(account.session_string, '1FinalSession==')
        self.assertEqual(account.connected_by, self.admin)
        # At rest the session must be ciphertext, not the plaintext value.
        with db_connection.cursor() as cur:
            cur.execute(
                'SELECT session_string FROM social_integrations_telegramaccount WHERE id = %s',
                [account.id],
            )
            raw = cur.fetchone()[0]
        self.assertNotEqual(raw, '1FinalSession==')
        self.assertNotIn('1FinalSession', raw)
        # Login state cleaned up + worker nudged
        self.assertIsNone(cache.get(f'tg-login:{token}'))
        mock_nudge.assert_called_once()
        # Serializer must never leak the session
        self.assertNotIn('session_string', resp.data['account'])

    def test_verify_2fa_branch_keeps_state(self):
        token = self._seed_login_state()
        with self._configured(), \
             patch('social_integrations.telegram_views._run_async',
                   return_value={'status': 'password_required'}):
            resp = self.api_post(self.VERIFY_URL,
                                 {'login_token': token, 'code': '12345'},
                                 user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'password_required')
        self.assertIsNotNone(cache.get(f'tg-login:{token}'))

    def test_agent_cannot_connect(self):
        resp = self.api_post(self.CONNECT_URL, {'phone_number': '+995599123456'},
                             user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestTelegramStatusAndDisconnect(TelegramTestCase):
    def test_status_lists_accounts_without_session(self):
        make_account()
        resp = self.api_get('/api/social/telegram/status/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['connected'])
        self.assertEqual(len(resp.data['accounts']), 1)
        self.assertNotIn('session_string', resp.data['accounts'][0])

    def test_disconnect_deactivates_and_revokes(self):
        account = make_account()
        with patch('social_integrations.telegram_views._run_async') as mock_run, \
             patch('social_integrations.telegram_views._nudge_worker') as mock_nudge:
            resp = self.api_post('/api/social/telegram/disconnect/',
                                 {'account_id': account.id}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        account.refresh_from_db()
        self.assertFalse(account.is_active)
        self.assertEqual(account.deactivation_reason, 'manual')
        mock_run.assert_called_once()   # log_out attempted
        mock_nudge.assert_called_once()  # worker told to drop the client


class TestTelegramSend(TelegramTestCase):
    URL = '/api/social/telegram/send-message/'

    def _worker_ok(self, message_payload=None):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            'success': True,
            'message': message_payload or {'id': 1, 'message_id': 'x_1', 'message_text': 'hi'},
        }
        return resp

    def test_send_happy_path_runs_post_send_extras(self):
        account = make_account()
        msg = make_message(account)
        ConversationArchive.objects.create(
            platform='telegram', conversation_id=str(msg.peer_id),
            account_id=str(account.telegram_user_id),
        )
        with patch('social_integrations.telegram_views.social_http.post',
                   return_value=self._worker_ok()) as mock_post:
            resp = self.api_post(self.URL, {
                'account_id': str(account.telegram_user_id),
                'peer_id': str(msg.peer_id),
                'message': 'hello back',
            }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        body = mock_post.call_args.kwargs['json']
        self.assertEqual(body['peer_id'], msg.peer_id)
        self.assertEqual(body['account_id'], account.id)
        self.assertEqual(body['sent_by_id'], self.agent.id)
        # Post-send extras: archive row cleared
        self.assertFalse(ConversationArchive.objects.filter(
            platform='telegram', conversation_id=str(msg.peer_id),
        ).exists())

    def test_send_requires_existing_conversation(self):
        account = make_account()
        resp = self.api_post(self.URL, {
            'account_id': str(account.telegram_user_id),
            'peer_id': '999999',
            'message': 'hi',
        }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['error_code'], 'no_conversation')

    def test_send_worker_down_is_503(self):
        account = make_account()
        msg = make_message(account)
        with patch('social_integrations.telegram_views.social_http.post',
                   side_effect=ConnectionError('down')):
            resp = self.api_post(self.URL, {
                'account_id': str(account.telegram_user_id),
                'peer_id': str(msg.peer_id),
                'message': 'hi',
            }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_send_relays_worker_error(self):
        account = make_account()
        msg = make_message(account)
        worker_resp = MagicMock(status_code=429)
        worker_resp.json.return_value = {'error': 'flood', 'error_code': 'flood_wait',
                                         'retry_after': 30}
        with patch('social_integrations.telegram_views.social_http.post',
                   return_value=worker_resp):
            resp = self.api_post(self.URL, {
                'account_id': str(account.telegram_user_id),
                'peer_id': str(msg.peer_id),
                'message': 'hi',
            }, user=self.agent)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data['error_code'], 'flood_wait')


class TestTelegramUnifiedInbox(TelegramTestCase):
    def test_conversations_include_telegram(self):
        account = make_account()
        make_message(account, peer_name='Nino', message_text='გამარჯობა')
        resp = self.api_get('/api/social/conversations/?platforms=telegram',
                            user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        self.assertEqual(len(results), 1)
        conv = results[0]
        self.assertEqual(conv['platform'], 'telegram')
        self.assertTrue(conv['conversation_id'].startswith(
            f'tg_{account.telegram_user_id}_'))
        self.assertEqual(conv['unread_count'], 1)

    def test_unread_count_includes_telegram(self):
        account = make_account()
        make_message(account)
        resp = self.api_get('/api/social/unread-count/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('telegram', resp.data)
        self.assertEqual(resp.data['telegram'], 1)
        self.assertGreaterEqual(resp.data['total'], 1)

    def test_mark_conversation_read(self):
        account = make_account()
        msg = make_message(account)
        resp = self.api_post('/api/social/mark-read/', {
            'platform': 'telegram',
            'conversation_id': str(msg.peer_id),
        }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msg.refresh_from_db()
        self.assertTrue(msg.is_read_by_staff)

    def test_telegram_messages_endpoint(self):
        account = make_account()
        msg = make_message(account)
        resp = self.api_get(
            f'/api/social/telegram-messages/?account_id={account.telegram_user_id}'
            f'&peer_id={msg.peer_id}',
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 1)
        row = resp.data['results'][0]
        self.assertEqual(row['message_id'], msg.message_id)
        self.assertNotIn('session_string', row)


class TestTelegramRouting(TelegramTestCase):
    def test_account_save_upserts_route(self):
        from social_integrations.platform_routing import route_lookup

        account = make_account()
        self.assertEqual(
            route_lookup('telegram', str(account.telegram_user_id)),
            self.tenant.schema_name,
        )
