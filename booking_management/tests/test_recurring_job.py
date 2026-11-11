"""The recurring-bookings job: tenant-local day, buffered end time, conflict
check, and the status/payment rules of the other create paths.
"""
from datetime import datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from booking_management.models import Booking, BookingSettings
from booking_management.tasks import _create_recurring_bookings_for_tenant, create_recurring_bookings
from booking_management.tests.conftest import BookingTestCase
from booking_management.utils import get_booking_settings, tenant_now


class RecurringJobBase(BookingTestCase):

    def setUp(self):
        super().setUp()
        BookingSettings.objects.filter(tenant=self.tenant).delete()
        self.settings = self.create_settings()  # Asia/Tbilisi
        self.today = tenant_now(get_booking_settings()).date()
        self.service = self.create_service(duration_minutes=60, buffer_time_minutes=15, base_price=Decimal('80.00'),
                                           deposit_percentage=25)
        self.staff = self.create_staff()
        self.service.staff_members.add(self.staff)
        self.bk_client = self.create_client(email='weekly@test.com')

    def series(self, **kwargs):
        defaults = dict(client=self.bk_client, staff=self.staff, preferred_time=time(10, 0),
                        next_booking_date=self.today)
        defaults.update(kwargs)
        return self.create_recurring_booking(self.service, **defaults)

    def run_job(self):
        return _create_recurring_bookings_for_tenant(self.tenant.schema_name)


class TestRecurringJobCreates(RecurringJobBase):

    def test_confirmed_pay_at_venue_booking_with_buffer(self):
        recurring = self.series()
        self.assertEqual(self.run_job(), 1)

        booking = Booking.objects.get(client=self.bk_client)
        self.assertEqual(booking.date, self.today)
        self.assertEqual(booking.start_time, time(10, 0))
        self.assertEqual(booking.end_time, time(11, 15))  # 60 min + 15 min buffer
        self.assertEqual(booking.status, 'confirmed')
        self.assertIsNotNone(booking.confirmed_at)
        self.assertEqual(booking.payment_method, 'cash')
        self.assertEqual(booking.payment_status, 'pending')
        self.assertEqual(booking.total_amount, Decimal('80.00'))
        self.assertEqual(booking.deposit_amount, Decimal('0'))
        self.assertEqual(booking.staff, self.staff)
        self.assertEqual(booking.contact_email, 'weekly@test.com')
        self.assertIn(f'#{recurring.id}', booking.client_notes)

        recurring.refresh_from_db()
        self.assertEqual(recurring.last_created_booking, booking)
        self.assertEqual(recurring.current_occurrences, 1)
        self.assertEqual(recurring.next_booking_date, self.today + timedelta(days=7))
        self.assertEqual(recurring.status, 'active')

        # Nothing more to do today
        self.assertEqual(self.run_job(), 0)
        self.assertEqual(Booking.objects.filter(client=self.bk_client).count(), 1)

    def test_pending_card_booking_when_the_business_requires_a_deposit(self):
        self.settings.require_deposit = True
        self.settings.allow_card_payment = True
        self.settings.bog_client_id = 'bog-id'
        self.settings.bog_client_secret = 'bog-secret'
        self.settings.save()
        self.series()
        self.assertEqual(self.run_job(), 1)
        booking = Booking.objects.get(client=self.bk_client)
        self.assertEqual(booking.status, 'pending')
        self.assertIsNone(booking.confirmed_at)
        self.assertEqual(booking.payment_method, 'card')
        self.assertEqual(booking.deposit_amount, Decimal('20.00'))  # 25% of 80
        self.assertEqual(booking.end_time, time(11, 15))

    def test_series_without_staff(self):
        self.series(staff=None)
        self.assertEqual(self.run_job(), 1)
        booking = Booking.objects.get(client=self.bk_client)
        self.assertIsNone(booking.staff)
        self.assertEqual(booking.status, 'confirmed')

    def test_completes_at_max_occurrences(self):
        recurring = self.series(max_occurrences=1)
        self.assertEqual(self.run_job(), 1)
        recurring.refresh_from_db()
        self.assertEqual(recurring.status, 'completed')
        self.assertEqual(recurring.current_occurrences, 1)

    def test_future_series_is_left_alone(self):
        self.series(next_booking_date=self.today + timedelta(days=1))
        self.assertEqual(self.run_job(), 0)
        self.assertFalse(Booking.objects.filter(client=self.bk_client).exists())

    def test_celery_task_covers_the_tenant(self):
        self.series()
        self.assertGreaterEqual(create_recurring_bookings(), 1)
        self.assertEqual(Booking.objects.filter(client=self.bk_client).count(), 1)


class TestRecurringJobConflicts(RecurringJobBase):

    def test_taken_slot_is_skipped_and_logged(self):
        other = self.create_client()
        self.create_booking(self.service, client=other, staff=self.staff, date=self.today,
                            start_time=time(10, 0), end_time=time(11, 15), status='confirmed')
        recurring = self.series()
        with self.assertLogs('booking_management.tasks', level='WARNING') as logs:
            self.assertEqual(self.run_job(), 0)
        self.assertTrue(any('already booked' in line for line in logs.output), logs.output)

        self.assertFalse(Booking.objects.filter(client=self.bk_client).exists())
        recurring.refresh_from_db()
        self.assertEqual(recurring.next_booking_date, self.today + timedelta(days=7))
        self.assertEqual(recurring.current_occurrences, 0)
        self.assertIsNone(recurring.last_created_booking)
        self.assertEqual(recurring.status, 'active')

    def test_conflict_counts_the_buffer(self):
        # 10:00 + 60 min + 15 min buffer reaches 11:15; a booking at 11:00 overlaps
        other = self.create_client()
        self.create_booking(self.service, client=other, staff=self.staff, date=self.today,
                            start_time=time(11, 0), end_time=time(12, 15), status='pending')
        self.series()
        with self.assertLogs('booking_management.tasks', level='WARNING'):
            self.assertEqual(self.run_job(), 0)

    def test_cancelled_booking_does_not_block(self):
        other = self.create_client()
        self.create_booking(self.service, client=other, staff=self.staff, date=self.today,
                            start_time=time(10, 0), end_time=time(11, 15), status='cancelled')
        self.series()
        self.assertEqual(self.run_job(), 1)

    def test_other_staff_does_not_block(self):
        colleague = self.create_staff()
        other = self.create_client()
        self.create_booking(self.service, client=other, staff=colleague, date=self.today,
                            start_time=time(10, 0), end_time=time(11, 15), status='confirmed')
        self.series()
        self.assertEqual(self.run_job(), 1)


class TestRecurringJobTenantDay(RecurringJobBase):
    """At 21:30 UTC it is already 01:30 the next day in Tbilisi: the job must
    use the business's day, as the model's own check does."""

    LATE_UTC = datetime(2026, 11, 10, 21, 30, tzinfo=dt_timezone.utc)

    def test_job_uses_the_business_day(self):
        recurring = self.series(next_booking_date=datetime(2026, 11, 11).date())
        with patch('django.utils.timezone.now', return_value=self.LATE_UTC):
            self.assertEqual(self.run_job(), 1)
        booking = Booking.objects.get(client=self.bk_client)
        self.assertEqual(booking.date.isoformat(), '2026-11-11')
        recurring.refresh_from_db()
        self.assertEqual(recurring.next_booking_date.isoformat(), '2026-11-18')

    def test_model_check_uses_the_business_day(self):
        recurring = self.series(next_booking_date=datetime(2026, 11, 11).date())
        with patch('django.utils.timezone.now', return_value=self.LATE_UTC):
            self.assertTrue(recurring.should_create_booking())
            # end_date is judged on the same clock: 2026-11-10 is already over in Tbilisi
            recurring.end_date = datetime(2026, 11, 10).date()
            self.assertFalse(recurring.should_create_booking())

    def test_utc_business_waits_for_its_own_midnight(self):
        self.settings.timezone = 'UTC'
        self.settings.save()
        self.series(next_booking_date=datetime(2026, 11, 11).date())
        with patch('django.utils.timezone.now', return_value=self.LATE_UTC):
            self.assertEqual(self.run_job(), 0)
        self.assertFalse(Booking.objects.filter(client=self.bk_client).exists())
