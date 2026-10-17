"""
Tests for the notification preference endpoints:
  - GET /api/notification-preferences/
  - GET /api/notification-preferences/types/
  - PUT /api/notification-preferences/bulk/
"""
from users.models import Notification, NotificationPreference
from users.tests.conftest import EchoDeskTenantTestCase

LIST_URL = '/api/notification-preferences/'
TYPES_URL = '/api/notification-preferences/types/'
BULK_URL = '/api/notification-preferences/bulk/'


class TestNotificationPreferences(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user = self.create_user(email='prefs@test.com')
        self.other = self.create_user(email='prefs-other@test.com')

    def test_list_only_own_rows(self):
        NotificationPreference.objects.create(user=self.user, notification_type='ticket_assigned', push=False)
        NotificationPreference.objects.create(user=self.other, notification_type='ticket_assigned')
        resp = self.api_get(LIST_URL, user=self.user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]['notification_type'], 'ticket_assigned')
        self.assertFalse(resp.data[0]['push'])
        self.assertEqual(set(resp.data[0]), {'id', 'notification_type', 'in_app', 'sound', 'push'})

    def test_types_lists_every_type_with_effective_values(self):
        NotificationPreference.objects.create(
            user=self.user, notification_type='call_missed', in_app=False, sound=False, push=True,
        )
        resp = self.api_get(TYPES_URL, user=self.user)
        self.assertEqual(resp.status_code, 200)
        keys = [row['notification_type'] for row in resp.data]
        self.assertEqual(keys, [key for key, _ in Notification.NOTIFICATION_TYPES])
        by_key = {row['notification_type']: row for row in resp.data}
        self.assertEqual(by_key['call_missed'], {
            'notification_type': 'call_missed', 'label': 'Missed Call',
            'in_app': False, 'sound': False, 'push': True, 'is_customized': True,
        })
        self.assertEqual(by_key['ticket_assigned']['is_customized'], False)
        self.assertTrue(by_key['ticket_assigned']['in_app'] and by_key['ticket_assigned']['push'])

    def test_bulk_creates_and_updates(self):
        NotificationPreference.objects.create(user=self.user, notification_type='ticket_assigned')
        resp = self.api_put(BULK_URL, [
            {'notification_type': 'ticket_assigned', 'in_app': True, 'sound': False, 'push': False},
            {'notification_type': 'leave_request_approved', 'in_app': False, 'sound': True, 'push': True},
        ], user=self.user)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual([r['notification_type'] for r in resp.data], ['ticket_assigned', 'leave_request_approved'])
        pref = NotificationPreference.objects.get(user=self.user, notification_type='ticket_assigned')
        self.assertEqual((pref.in_app, pref.sound, pref.push), (True, False, False))
        self.assertEqual(NotificationPreference.objects.filter(user=self.user).count(), 2)
        self.assertFalse(NotificationPreference.objects.filter(user=self.other).exists())

    def test_partial_item_keeps_stored_channels(self):
        NotificationPreference.objects.create(
            user=self.user, notification_type='ticket_commented', in_app=False, sound=False, push=False,
        )
        resp = self.api_put(BULK_URL, [{'notification_type': 'ticket_commented', 'push': True}], user=self.user)
        self.assertEqual(resp.status_code, 200)
        pref = NotificationPreference.objects.get(user=self.user, notification_type='ticket_commented')
        self.assertEqual((pref.in_app, pref.sound, pref.push), (False, False, True))

    def test_partial_item_new_row_defaults_true(self):
        resp = self.api_put(BULK_URL, [{'notification_type': 'invoice_paid', 'sound': False}], user=self.user)
        self.assertEqual(resp.status_code, 200)
        pref = NotificationPreference.objects.get(user=self.user, notification_type='invoice_paid')
        self.assertEqual((pref.in_app, pref.sound, pref.push), (True, False, True))

    def test_unknown_type_rejects_whole_request(self):
        resp = self.api_put(BULK_URL, [
            {'notification_type': 'ticket_assigned', 'push': False},
            {'notification_type': 'not_a_type', 'push': False},
        ], user=self.user)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(NotificationPreference.objects.filter(user=self.user).exists())

    def test_missing_type_and_bad_boolean_rejected(self):
        self.assertEqual(self.api_put(BULK_URL, [{'push': False}], user=self.user).status_code, 400)
        self.assertEqual(
            self.api_put(BULK_URL, [{'notification_type': 'ticket_assigned', 'push': 'maybe'}], user=self.user).status_code,
            400,
        )
        self.assertFalse(NotificationPreference.objects.filter(user=self.user).exists())

    def test_non_list_body_rejected(self):
        resp = self.api_put(BULK_URL, {'notification_type': 'ticket_assigned'}, user=self.user)
        self.assertEqual(resp.status_code, 400)

    def test_duplicate_types_last_wins(self):
        resp = self.api_put(BULK_URL, [
            {'notification_type': 'call_missed', 'push': False, 'sound': False},
            {'notification_type': 'call_missed', 'push': True},
        ], user=self.user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        pref = NotificationPreference.objects.get(user=self.user, notification_type='call_missed')
        self.assertEqual((pref.sound, pref.push), (False, True))

    def test_empty_list_is_ok(self):
        resp = self.api_put(BULK_URL, [], user=self.user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_unauthenticated(self):
        self.assertIn(self.api_get(LIST_URL).status_code, (401, 403))
        self.assertIn(self.api_get(TYPES_URL).status_code, (401, 403))
        self.assertIn(self.api_put(BULK_URL, []).status_code, (401, 403))
