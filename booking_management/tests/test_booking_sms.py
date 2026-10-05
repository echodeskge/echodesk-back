"""SMS notices and reminders for bookings."""
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from booking_management import sms
from booking_management.models import BookingSettings, BookingSmsLog
from booking_management.tasks import _send_reminders_for_tenant, due_reminder, send_booking_email_task
from booking_management.tests.conftest import BookingTestCase
from booking_management.tests.test_views import ADMIN_SETTINGS_URL, BookingViewTestMixin

OK = {'messageId': 'm-1'}
TEST_SMS_URL = '/api/bookings/admin/settings/test-sms/'


class SmsTextTests(BookingTestCase):

    def test_segments(self):
        self.assertEqual(sms.sms_segments(''), 0)
        self.assertEqual(sms.sms_segments('a' * 160), 1)
        self.assertEqual(sms.sms_segments('a' * 161), 2)
        self.assertEqual(sms.sms_segments('a' * 306), 2)
        self.assertEqual(sms.sms_segments('a' * 307), 3)
        # Georgian is Unicode: 70 per SMS, 67 when split
        self.assertEqual(sms.sms_segments('ა' * 70), 1)
        self.assertEqual(sms.sms_segments('ა' * 71), 2)
        self.assertEqual(sms.sms_segments('ა' * 134), 2)
        self.assertEqual(sms.sms_segments('ა' * 135), 3)
        # GSM extension characters cost two
        self.assertEqual(sms.sms_segments('€' * 80), 1)
        self.assertEqual(sms.sms_segments('€' * 81), 2)

    def test_render_keeps_unknown_placeholders(self):
        self.assertEqual(
            sms.render_template('Hi {name}, {oops} at {time}', {'name': 'Ana', 'time': '14:00'}),
            'Hi Ana, {oops} at 14:00',
        )

    def test_custom_template_wins_and_empty_falls_back(self):
        s = BookingSettings.objects.create(tenant=self.tenant, sms_templates={'reminder': {'ka': 'ჩემი {time}', 'en': '  '}})
        self.assertEqual(sms.template_for(s, 'reminder', 'ka'), 'ჩემი {time}')
        self.assertEqual(sms.template_for(s, 'reminder', 'en'), sms.DEFAULT_TEMPLATES['reminder']['en'])
        self.assertEqual(sms.template_for(s, 'confirmed', 'ru'), sms.DEFAULT_TEMPLATES['confirmed']['en'])

    def test_defaults_exist_for_every_kind(self):
        for kind in sms.SMS_KINDS:
            for language in ('ka', 'en'):
                self.assertIn('{business}', sms.DEFAULT_TEMPLATES[kind][language])


class SmsSendTests(BookingTestCase):

    def setUp(self):
        super().setUp()
        self.settings_row = BookingSettings.objects.create(tenant=self.tenant, sms_enabled=True)
        self.service = self.create_service()
        self.staff = self.create_staff()
        self.client_row = self.create_client(phone='+995599123456')
        self.booking = self.create_booking(self.service, client=self.client_row, staff=self.staff, status='confirmed')

    def test_off_by_default_and_per_kind_switch(self):
        self.settings_row.sms_enabled = False
        self.settings_row.save()
        with patch('crm.sms_utils.send_sms', return_value=OK) as send:
            self.assertEqual(sms.send_booking_sms(self.booking, 'confirmed'), 'off')
            self.settings_row.sms_enabled = True
            self.settings_row.sms_on_confirmed = False
            self.settings_row.save()
            self.assertEqual(sms.send_booking_sms(self.booking, 'confirmed'), 'off')
            # "created" is off unless switched on
            self.assertEqual(sms.send_booking_sms(self.booking, 'created'), 'off')
        send.assert_not_called()
        self.assertEqual(BookingSmsLog.objects.count(), 0)

    def test_no_account_is_logged_as_skipped(self):
        with patch('crm.sms_utils.send_sms', return_value=OK) as send:
            self.assertEqual(sms.send_booking_sms(self.booking, 'confirmed'), 'skipped')
        send.assert_not_called()
        log = BookingSmsLog.objects.get()
        self.assertEqual(log.status, 'skipped')
        self.assertIn('sender.ge', log.error)

    def test_own_key_is_used_and_logged(self):
        self.settings_row.sms_api_key = 'own-key'
        self.settings_row.save()
        self.settings_row.refresh_from_db()
        self.assertEqual(self.settings_row.sms_api_key, 'own-key')  # stored encrypted, read back
        with patch('crm.sms_utils.send_sms', return_value=OK) as send:
            self.assertEqual(sms.send_booking_sms(self.booking, 'confirmed', 'en'), 'sent')
        key, phone, text = send.call_args[0]
        self.assertEqual(key, 'own-key')
        self.assertEqual(phone, '+995599123456')
        self.assertIn(self.booking.start_time.strftime('%H:%M'), text)
        self.assertNotIn('{', text)
        log = BookingSmsLog.objects.get()
        self.assertEqual((log.status, log.account, log.kind, log.provider_message_id), ('sent', 'own', 'confirmed', 'm-1'))
        self.assertEqual(log.booking, self.booking)

    @override_settings(SENDER_GE_API_KEY='platform-key', BOOKING_SMS_PLATFORM_MONTHLY_LIMIT=2)
    def test_platform_account_and_monthly_cap(self):
        with patch('crm.sms_utils.send_sms', return_value=OK) as send:
            self.assertEqual(sms.send_booking_sms(self.booking, 'confirmed', 'en'), 'sent')
            self.assertEqual(send.call_args[0][0], 'platform-key')
            self.assertEqual(sms.send_booking_sms(self.booking, 'cancelled', 'en'), 'sent')
            # third one is over the limit of 2
            self.assertEqual(sms.send_booking_sms(self.booking, 'reminder', 'en'), 'skipped')
            self.assertEqual(send.call_count, 2)
            # a salon-specific limit overrides the platform default
            self.settings_row.sms_platform_monthly_limit = 5
            self.settings_row.save()
            self.assertEqual(sms.send_booking_sms(self.booking, 'reminder', 'en'), 'sent')
            # own key is never capped
            self.settings_row.sms_platform_monthly_limit = 0
            self.settings_row.sms_api_key = 'own-key'
            self.settings_row.save()
            self.assertEqual(sms.send_booking_sms(self.booking, 'reminder', 'en'), 'sent')
            self.assertEqual(send.call_args[0][0], 'own-key')
        self.assertEqual(sms.sent_this_month('platform'), 3)
        self.assertEqual(sms.sent_this_month(), 4)

    def test_provider_error_and_bad_number(self):
        self.settings_row.sms_api_key = 'own-key'
        self.settings_row.save()
        with patch('crm.sms_utils.send_sms', return_value={'error': 'bad key'}):
            self.assertEqual(sms.send_booking_sms(self.booking, 'confirmed'), 'failed')
        with patch('crm.sms_utils.send_sms', side_effect=RuntimeError('boom')):
            self.assertEqual(sms.send_booking_sms(self.booking, 'confirmed'), 'failed')
        self.client_row.phone = '+44 20 7946 0000'
        self.client_row.save()
        with patch('crm.sms_utils.send_sms', return_value=OK) as send:
            self.assertEqual(sms.send_booking_sms(self.booking, 'confirmed'), 'skipped')
        send.assert_not_called()
        self.assertEqual(list(BookingSmsLog.objects.order_by('id').values_list('status', flat=True)), ['failed', 'failed', 'skipped'])

    def test_notice_task_texts_a_customer_without_email(self):
        self.settings_row.sms_api_key = 'own-key'
        self.settings_row.save()
        self.client_row.email = None
        self.client_row.save()
        self.booking.contact_email = ''
        self.booking.contact_language = 'ka'
        self.booking.save()
        with patch('crm.sms_utils.send_sms', return_value=OK) as send:
            result = send_booking_email_task(self.tenant.schema_name, self.booking.id, 'cancelled')
        self.assertTrue(result)
        self.assertIn('გაუქმდა', send.call_args[0][2])


class ReminderDueTests(BookingTestCase):
    """due_reminder on a fixed clock (business local time, naive)."""

    def setUp(self):
        super().setUp()
        self.settings_row = BookingSettings.objects.create(tenant=self.tenant, timezone='UTC')
        self.service = self.create_service()
        self.staff = self.create_staff()
        self.now = datetime(2030, 5, 10, 12, 0)

    def _booking(self, start, created_hours_ago=200, **kwargs):
        booking = self.create_booking(
            self.service, client=self.create_client(), staff=self.staff,
            date=start.date(), start_time=start.time(), end_time=(start + timedelta(hours=1)).time(),
            status=kwargs.pop('status', 'confirmed'), **kwargs,
        )
        created = timezone.make_aware(self.now - timedelta(hours=created_hours_ago), timezone.utc)
        type(booking).objects.filter(pk=booking.pk).update(created_at=created)
        booking.refresh_from_db()
        return booking

    def test_first_reminder_window(self):
        self.assertIsNone(due_reminder(self._booking(self.now + timedelta(hours=25)), self.now, self.settings_row))
        self.assertEqual(due_reminder(self._booking(self.now + timedelta(hours=24)), self.now, self.settings_row), 'first')
        self.assertEqual(due_reminder(self._booking(self.now + timedelta(hours=3)), self.now, self.settings_row), 'first')

    def test_not_for_past_pending_or_already_reminded(self):
        soon = self.now + timedelta(hours=5)
        self.assertIsNone(due_reminder(self._booking(self.now - timedelta(hours=1)), self.now, self.settings_row))
        self.assertIsNone(due_reminder(self._booking(soon, status='pending'), self.now, self.settings_row))
        self.assertIsNone(due_reminder(self._booking(soon, reminder_sent=True), self.now, self.settings_row))

    def test_booked_inside_the_window_gets_no_first_reminder(self):
        booking = self._booking(self.now + timedelta(hours=5), created_hours_ago=1)
        self.assertIsNone(due_reminder(booking, self.now, self.settings_row))

    def test_salon_chooses_hours(self):
        self.settings_row.reminder_hours_before = 48
        booking = self._booking(self.now + timedelta(hours=40))
        self.assertEqual(due_reminder(booking, self.now, self.settings_row), 'first')
        self.settings_row.reminder_hours_before = 3
        self.assertIsNone(due_reminder(booking, self.now, self.settings_row))

    def test_second_reminder(self):
        self.settings_row.second_reminder_hours_before = 2
        far = self._booking(self.now + timedelta(hours=5), reminder_sent=True)
        self.assertIsNone(due_reminder(far, self.now, self.settings_row))
        near = self._booking(self.now + timedelta(hours=2), reminder_sent=True)
        self.assertEqual(due_reminder(near, self.now, self.settings_row), 'second')
        near.second_reminder_sent = True
        self.assertIsNone(due_reminder(near, self.now, self.settings_row))
        # First never went out and we are already inside the second window: one message, not two
        both = self._booking(self.now + timedelta(hours=1))
        self.assertEqual(due_reminder(both, self.now, self.settings_row), 'second')
        # Booked 15 minutes ago for 90 minutes from now: inside both windows, nothing
        late = self._booking(self.now + timedelta(minutes=90), created_hours_ago=0.25)
        self.assertIsNone(due_reminder(late, self.now, self.settings_row))

    def test_quiet_hours(self):
        night = datetime(2030, 5, 10, 23, 30)
        booking = self._booking(night + timedelta(hours=10))
        self.assertIsNone(due_reminder(booking, night, self.settings_row))
        self.assertIsNone(due_reminder(booking, datetime(2030, 5, 11, 8, 59), self.settings_row))
        self.assertEqual(due_reminder(booking, datetime(2030, 5, 11, 9, 0), self.settings_row), 'first')


class ReminderJobTests(BookingTestCase):

    def setUp(self):
        super().setUp()
        self.settings_row = BookingSettings.objects.create(tenant=self.tenant, timezone='UTC', sms_enabled=True)
        self.settings_row.sms_api_key = 'own-key'
        self.settings_row.save()
        self.service = self.create_service()
        self.staff = self.create_staff()
        self.now = datetime(2030, 5, 10, 12, 0)

    def _booking(self, hours_ahead=5, email='r@example.com', phone='+995599123456', **kwargs):
        start = self.now + timedelta(hours=hours_ahead)
        client = self.create_client(email=email, phone=phone)
        booking = self.create_booking(
            self.service, client=client, staff=self.staff, date=start.date(), start_time=start.time(),
            end_time=(start + timedelta(hours=1)).time(), status='confirmed', **kwargs,
        )
        type(booking).objects.filter(pk=booking.pk).update(
            created_at=timezone.make_aware(self.now - timedelta(days=5), timezone.utc))
        return booking

    def _run(self, sms_result=OK, email_result=True):
        with patch('booking_management.utils.tenant_now', return_value=self.now), \
                patch('booking_management.emails.send_booking_email', return_value=email_result) as email, \
                patch('crm.sms_utils.send_sms', return_value=sms_result) as text:
            count = _send_reminders_for_tenant(self.tenant.schema_name)
        return count, email, text

    def test_sends_email_and_sms_once(self):
        booking = self._booking()
        count, email, text = self._run()
        self.assertEqual((count, email.call_count, text.call_count), (1, 1, 1))
        booking.refresh_from_db()
        self.assertTrue(booking.reminder_sent)
        count, email, text = self._run()
        self.assertEqual((count, email.call_count, text.call_count), (0, 0, 0))

    def test_phone_only_customer_gets_sms(self):
        booking = self._booking(email=None)
        count, email, text = self._run()
        self.assertEqual((count, email.call_count, text.call_count), (1, 0, 1))
        booking.refresh_from_db()
        self.assertTrue(booking.reminder_sent)

    def test_nothing_to_send_to_is_closed_without_counting(self):
        booking = self._booking(email=None, phone='')
        count, email, text = self._run()
        self.assertEqual((count, email.call_count, text.call_count), (0, 0, 0))
        booking.refresh_from_db()
        self.assertTrue(booking.reminder_sent)  # no point looking at it every ten minutes

    def test_failed_send_is_retried_then_given_up(self):
        booking = self._booking(email=None)
        for _ in range(2):
            count, _email, text = self._run(sms_result={'error': 'down'})
            self.assertEqual((count, text.call_count), (0, 1))
            booking.refresh_from_db()
            self.assertFalse(booking.reminder_sent)
        self._run(sms_result={'error': 'down'})
        booking.refresh_from_db()
        self.assertTrue(booking.reminder_sent)

    def test_second_reminder_runs_after_first(self):
        self.settings_row.second_reminder_hours_before = 2
        self.settings_row.save()
        booking = self._booking(hours_ahead=5)
        self.assertEqual(self._run()[0], 1)
        self.assertEqual(self._run()[0], 0)
        self.now = self.now + timedelta(hours=3, minutes=10)
        self.assertEqual(self._run()[0], 1)
        booking.refresh_from_db()
        self.assertTrue(booking.second_reminder_sent)
        self.assertEqual(self._run()[0], 0)

    def test_quiet_hours_send_nothing(self):
        self.now = datetime(2030, 5, 10, 22, 0)
        self._booking(hours_ahead=12)
        self.assertEqual(self._run()[0], 0)


class SmsSettingsApiTests(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='sms-admin@test.com')
        self._ensure_booking_feature()

    def test_key_is_write_only_kept_and_clearable(self):
        resp = self.api_patch(ADMIN_SETTINGS_URL, {'sms_enabled': True, 'sms_api_key': ' secret-key '}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertNotIn('sms_api_key', resp.data)
        self.assertNotIn('secret-key', str(resp.data))
        self.assertTrue(resp.data['has_sms_api_key'])
        self.assertEqual(BookingSettings.objects.get().sms_api_key, 'secret-key')
        # Posting it empty keeps the stored key
        resp = self.api_patch(ADMIN_SETTINGS_URL, {'sms_api_key': '', 'sms_on_created': True}, user=self.admin)
        self.assertTrue(resp.data['has_sms_api_key'])
        self.assertTrue(resp.data['sms_on_created'])
        resp = self.api_patch(ADMIN_SETTINGS_URL, {'sms_api_key_clear': True}, user=self.admin)
        self.assertFalse(resp.data['has_sms_api_key'])

    def test_reports_usage_defaults_and_platform(self):
        resp = self.api_get(ADMIN_SETTINGS_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['sms_enabled'])
        self.assertFalse(resp.data['sms_platform_available'])
        self.assertEqual(resp.data['sms_platform_limit'], 200)
        self.assertEqual(resp.data['sms_sent_this_month'], 0)
        self.assertEqual(resp.data['reminder_hours_before'], 24)
        self.assertIn('reminder', resp.data['sms_default_templates'])
        self.assertNotIn('sms_platform_monthly_limit', resp.data)  # ours to set, not the salon's

    def test_validates_hours_and_templates(self):
        resp = self.api_patch(ADMIN_SETTINGS_URL, {'reminder_hours_before': 0}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        resp = self.api_patch(ADMIN_SETTINGS_URL, {'reminder_hours_before': 24, 'second_reminder_hours_before': 24}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        resp = self.api_patch(ADMIN_SETTINGS_URL, {
            'reminder_hours_before': 24, 'second_reminder_hours_before': 2,
            'sms_templates': {'reminder': {'ka': ' ტექსტი {time} ', 'en': ''}, 'bogus': {'ka': 'x'}},
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['sms_templates'], {'reminder': {'ka': 'ტექსტი {time}'}})
        resp = self.api_patch(ADMIN_SETTINGS_URL, {'sms_templates': {'reminder': {'ka': 'x' * 601}}}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_test_sms(self):
        resp = self.api_post(TEST_SMS_URL, {'phone': '599123456', 'kind': 'reminder', 'language': 'en'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)  # no account yet
        self.assertIn('sender.ge', resp.data['error'])
        self.api_patch(ADMIN_SETTINGS_URL, {'sms_api_key': 'k'}, user=self.admin)
        with patch('crm.sms_utils.send_sms', return_value=OK) as send:
            resp = self.api_post(TEST_SMS_URL, {'phone': '599123456', 'kind': 'reminder', 'language': 'en',
                                                'text': 'Try {name} at {time}'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['text'], 'Try Ana at 14:00')
        self.assertEqual(send.call_args[0][2], 'Try Ana at 14:00')
        self.assertEqual(BookingSmsLog.objects.filter(status='sent', kind='test_reminder').count(), 1)

    def test_test_sms_requires_login(self):
        resp = self.api_post(TEST_SMS_URL, {'phone': '599123456'})
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
