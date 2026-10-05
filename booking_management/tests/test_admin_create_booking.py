"""Bookings entered by the business itself (phone, walk-in, calendar click)."""
from datetime import date, time, timedelta
from decimal import Decimal

from rest_framework import status

from booking_management.models import Booking
from booking_management.tests.conftest import BookingTestCase
from booking_management.tests.test_views import ADMIN_BOOKING_URL, BookingViewTestMixin
from social_integrations.models import Client


def next_monday():
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


class TestAdminCreateBooking(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bk-create-admin@test.com')
        self._ensure_booking_feature()
        self.service = self.create_service(base_price=Decimal('80.00'), duration_minutes=60, buffer_time_minutes=0)
        self.staff = self.create_staff()
        self.service.staff_members.add(self.staff)
        self.create_availability(self.staff, day_of_week=0, start_time=time(9, 0), end_time=time(18, 0))
        self.day = next_monday()

    def _payload(self, **extra):
        data = {
            'service_id': self.service.id, 'staff_id': self.staff.id,
            'date': self.day.isoformat(), 'start_time': '10:17',
            'first_name': 'Ana', 'last_name': 'G.', 'phone_number': '599 12 34 56',
        }
        data.update(extra)
        return data

    def test_creates_confirmed_cash_booking_for_new_contact(self):
        resp = self.api_post(ADMIN_BOOKING_URL, self._payload(), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        booking = Booking.objects.get(id=resp.data['id'])
        self.assertEqual(booking.status, 'confirmed')
        self.assertEqual(booking.payment_method, 'cash')
        self.assertEqual(booking.total_amount, Decimal('80.00'))
        # Any minute is allowed (no slot grid) and the end follows the duration
        self.assertEqual(booking.start_time, time(10, 17))
        self.assertEqual(booking.end_time, time(11, 17))
        self.assertEqual(booking.staff, self.staff)
        self.assertEqual(booking.client.first_name, 'Ana')
        self.assertTrue(booking.client.is_booking_enabled)

    def test_reuses_contact_with_same_phone_digits(self):
        existing = Client.objects.create(name='Ana Old', first_name='Ana', last_name='Old',
                                         phone='+995599123456', email='ana@example.com')
        resp = self.api_post(ADMIN_BOOKING_URL, self._payload(first_name='Somebody'), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        booking = Booking.objects.get(id=resp.data['id'])
        self.assertEqual(booking.client, existing)
        existing.refresh_from_db()
        self.assertEqual(existing.first_name, 'Ana')  # never renamed by a booking
        self.assertEqual(booking.contact_email, 'ana@example.com')

    def test_existing_client_by_id_and_price_override(self):
        client = self.create_client()
        resp = self.api_post(ADMIN_BOOKING_URL, self._payload(
            client_id=client.id, first_name='', phone_number='', total_amount='50',
        ), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        booking = Booking.objects.get(id=resp.data['id'])
        self.assertEqual(booking.client, client)
        self.assertEqual(booking.total_amount, Decimal('50'))

    def test_requires_name_and_phone_without_client(self):
        resp = self.api_post(ADMIN_BOOKING_URL, self._payload(first_name='', phone_number=''), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('first_name', resp.data)
        resp = self.api_post(ADMIN_BOOKING_URL, self._payload(phone_number='12'), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('phone_number', resp.data)

    def test_rejects_busy_slot_and_non_working_time(self):
        self.create_booking(self.service, client=self.create_client(), staff=self.staff,
                            date=self.day, start_time=time(10, 0), end_time=time(11, 0), status='confirmed')
        resp = self.api_post(ADMIN_BOOKING_URL, self._payload(start_time='10:30'), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('start_time', resp.data)
        # Day off (Tuesday has no hours)
        resp = self.api_post(ADMIN_BOOKING_URL, self._payload(date=(self.day + timedelta(days=1)).isoformat()),
                             user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('start_time', resp.data)
        # Would run past 18:00
        resp = self.api_post(ADMIN_BOOKING_URL, self._payload(start_time='17:30'), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_not_assigned_to_service(self):
        other = self.create_staff(user=self.create_admin(email='other-staff@test.com'))
        resp = self.api_post(ADMIN_BOOKING_URL, self._payload(staff_id=other.id), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('staff_id', resp.data)

    def test_ignores_customer_lead_time_window(self):
        # Customers can't book inside min_hours_before_booking; staff can book
        # the very next minute that is free.
        from booking_management.models import BookingSettings
        s = BookingSettings.objects.get_or_create(tenant=self.tenant)[0]
        s.min_hours_before_booking = 24 * 30
        s.save()
        resp = self.api_post(ADMIN_BOOKING_URL, self._payload(), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
