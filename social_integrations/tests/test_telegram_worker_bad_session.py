"""A corrupt Telegram session string disables the account instead of retrying forever."""
import asyncio
from unittest import mock

from social_integrations.services import telegram_worker as tw


def test_invalid_session_string_disables_account_and_stops():
    account = mock.Mock(is_active=True, telegram_user_id=1, session_string='not-a-session')
    calls = []

    async def fake_db(schema, fn, *args):
        calls.append(fn)
        if fn is tw._load_account:
            return account
        return None

    sup = tw.ManagedClient.__new__(tw.ManagedClient)
    assert sup is not None, 'supervisor class name changed'
    sup.schema, sup.account_id, sup.stopping, sup.client = 'demo', 1, False, None
    sup.app = mock.Mock(registry={('demo', 1): sup}, api_id=1, api_hash='x')
    with mock.patch.object(tw, 'db', fake_db), \
            mock.patch('telethon.TelegramClient', side_effect=AssertionError('must not build a client')):
        asyncio.run(sup.supervise())
    assert tw._disable_account in calls
    assert ('demo', 1) not in sup.app.registry
