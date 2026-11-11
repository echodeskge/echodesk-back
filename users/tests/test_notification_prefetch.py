"""
Sentry ECHODESK-BACKEND-6V / 71: N+1 NotificationPreference lookups when a
social webhook fans one incoming message out to every agent.

``create_notification`` looked the recipient's NotificationPreference up once
per call; the fan-outs now fetch the rows for all recipients in one query
(``notification_preferences``) and hand each one in via ``preference=``.
"""
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext

from users.models import Notification, NotificationPreference
from users.notification_utils import (
    create_ai_handoff_notification,
    create_notification,
    create_social_message_notification,
    notification_preferences,
)
from users.tests.conftest import EchoDeskTenantTestCase

NO_WS = patch('asgiref.sync.async_to_sync', return_value=lambda **kw: None)
NO_PUSH = patch('users.notification_utils._dispatch_push')


def _pref_queries(ctx):
    return [q['sql'] for q in ctx.captured_queries if 'users_notificationpreference' in q['sql']]


@NO_WS
@NO_PUSH
class SocialFanOutPreferenceTests(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.users = [self.create_user(email=f'fanout{i}@test.com') for i in range(4)]
        self.muted = self.users[1]
        NotificationPreference.objects.create(
            user=self.muted, notification_type='message_received', in_app=False,
        )
        NotificationPreference.objects.create(
            user=self.users[2], notification_type='message_received', push=False, sound=False,
        )

    def _fan_out(self):
        with patch('users.notification_utils.social_message_recipients', return_value=self.users):
            return create_social_message_notification(
                platform='Facebook', sender_name='Cust', message_text='hi',
                conversation_id='psid-1', sender_id='psid-1', account_id='pg1',
            )

    def test_one_preference_query_for_all_recipients(self, _push, _ws):
        with CaptureQueriesContext(connection) as ctx:
            created = self._fan_out()
        self.assertEqual(len(_pref_queries(ctx)), 1)
        self.assertEqual({n.user_id for n in created}, {u.id for u in self.users if u != self.muted})
        self.assertFalse(Notification.objects.filter(user=self.muted).exists())

    def test_prefetched_preferences_still_drive_push_and_sound(self, push, _ws):
        self._fan_out()
        pushed = {call.kwargs['user'].id for call in push.call_args_list}
        self.assertEqual(pushed, {self.users[0].id, self.users[3].id})

    def test_ai_handoff_fan_out_uses_one_preference_query(self, _push, _ws):
        NotificationPreference.objects.create(
            user=self.muted, notification_type='ai_handoff_requested', in_app=False,
        )
        with patch('users.notification_utils.social_message_recipients', return_value=self.users), \
                CaptureQueriesContext(connection) as ctx:
            create_ai_handoff_notification(
                platform='Facebook', conversation_id='psid-1', account_id='pg1',
                sender_name='Cust', reason='angry',
            )
        self.assertEqual(len(_pref_queries(ctx)), 1)
        self.assertEqual(
            Notification.objects.filter(notification_type='ai_handoff_requested').count(), 3,
        )


class NotificationPreferencesHelperTests(EchoDeskTenantTestCase):

    def test_empty_recipient_list_makes_no_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(notification_preferences([], 'message_received'), {})

    def test_maps_user_id_to_row_for_the_type_only(self):
        a = self.create_user(email='prefa@test.com')
        b = self.create_user(email='prefb@test.com')
        row = NotificationPreference.objects.create(user=a, notification_type='message_received', in_app=False)
        NotificationPreference.objects.create(user=a, notification_type='ticket_assigned', in_app=False)
        prefs = notification_preferences([a, b], 'message_received')
        self.assertEqual(prefs, {a.id: row})
        self.assertIsNone(prefs.get(b.id))

    @NO_WS
    @NO_PUSH
    def test_explicit_preference_skips_the_lookup(self, _push, _ws):
        user = self.create_user(email='prefc@test.com')
        NotificationPreference.objects.create(user=user, notification_type='message_received', in_app=False)
        with CaptureQueriesContext(connection) as ctx:
            # The caller says "no preference row": default (all enabled) applies
            # and the muted row in the table is never consulted.
            notification = create_notification(
                user=user, notification_type='message_received', title='t', message='m',
                preference=None,
            )
        self.assertIsNotNone(notification)
        self.assertEqual(_pref_queries(ctx), [])
