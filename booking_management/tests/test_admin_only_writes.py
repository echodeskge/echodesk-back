"""Admin-only writes: staff hours and exceptions, recorded payments and
recurring series are changed by booking admins (admin/manager/superuser)
only; every user with the feature still reads them.
"""
from datetime import timedelta
from decimal import Decimal

from rest_framework import status

from booking_management.models import RecurringBooking, StaffAvailability, StaffException
from booking_management.tests.conftest import BookingTestCase
from booking_management.tests.test_admin_hardening import (
    ADMIN_EXCEPTION_URL, ADMIN_RECURRING_URL, HardeningBase,
)
from booking_management.tests.test_views import ADMIN_BOOKING_URL

ADMIN_AVAILABILITY_URL = '/api/bookings/admin/availability/'


class TestAvailabilityWrites(HardeningBase):

    def row(self, **extra):
        data = {'staff': self.staff.id, 'day_of_week': 2, 'start_time': '10:00', 'end_time': '18:00'}
        data.update(extra)
        return data

    def test_agent_reads(self):
        avail = self.staff.availability.get(day_of_week=0)
        resp = self.api_get(ADMIN_AVAILABILITY_URL, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([a['id'] for a in BookingTestCase.get_results(resp)], [avail.id])
        self.assertEqual(self.api_get(f'{ADMIN_AVAILABILITY_URL}{avail.id}/', user=self.agent).status_code, 200)

    def test_agent_cannot_write(self):
        avail = self.staff.availability.get(day_of_week=0)
        resp = self.api_post(ADMIN_AVAILABILITY_URL, self.row(), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_patch(f'{ADMIN_AVAILABILITY_URL}{avail.id}/', {'end_time': '12:00'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_put(f'{ADMIN_AVAILABILITY_URL}{avail.id}/', self.row(day_of_week=0), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_delete(f'{ADMIN_AVAILABILITY_URL}{avail.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        avail.refresh_from_db()
        self.assertEqual(avail.end_time.hour, 17)
        self.assertEqual(StaffAvailability.objects.filter(staff=self.staff).count(), 1)

    def test_own_hours_are_not_an_exception(self):
        # Being the staff member in question doesn't make one an admin
        avail = self.staff.availability.get(day_of_week=0)
        resp = self.api_patch(f'{ADMIN_AVAILABILITY_URL}{avail.id}/', {'end_time': '12:00'}, user=self.staff_user)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_and_manager_write(self):
        resp = self.api_post(ADMIN_AVAILABILITY_URL, self.row(), user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        resp = self.api_patch(f"{ADMIN_AVAILABILITY_URL}{resp.data['id']}/", {'end_time': '12:00'}, user=self.manager)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['end_time'], '12:00:00')
        resp = self.api_post(ADMIN_AVAILABILITY_URL, self.row(day_of_week=3), user=self.manager)
        self.assertEqual(resp.status_code, 201, resp.data)
        resp = self.api_delete(f"{ADMIN_AVAILABILITY_URL}{resp.data['id']}/", user=self.admin)
        self.assertEqual(resp.status_code, 204)


class TestExceptionWrites(HardeningBase):

    def row(self, **extra):
        data = {'staff': self.staff.id, 'date': (self.day + timedelta(days=3)).isoformat(),
                'is_available': False, 'reason': 'Holiday'}
        data.update(extra)
        return data

    def test_agent_reads(self):
        exc = self.create_staff_exception(self.staff, exception_date=self.day + timedelta(days=1))
        resp = self.api_get(ADMIN_EXCEPTION_URL, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([e['id'] for e in BookingTestCase.get_results(resp)], [exc.id])
        self.assertEqual(self.api_get(f'{ADMIN_EXCEPTION_URL}{exc.id}/', user=self.agent).status_code, 200)

    def test_agent_cannot_write(self):
        exc = self.create_staff_exception(self.staff, exception_date=self.day + timedelta(days=1))
        resp = self.api_post(ADMIN_EXCEPTION_URL, self.row(), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_patch(f'{ADMIN_EXCEPTION_URL}{exc.id}/', {'reason': 'Changed'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_delete(f'{ADMIN_EXCEPTION_URL}{exc.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        exc.refresh_from_db()
        self.assertEqual(exc.reason, 'Test exception')
        self.assertEqual(StaffException.objects.filter(staff=self.staff).count(), 1)

    def test_admin_and_manager_write(self):
        resp = self.api_post(ADMIN_EXCEPTION_URL, self.row(), user=self.manager)
        self.assertEqual(resp.status_code, 201, resp.data)
        resp = self.api_patch(f"{ADMIN_EXCEPTION_URL}{resp.data['id']}/", {'reason': 'Dentist'}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['reason'], 'Dentist')
        resp = self.api_delete(f"{ADMIN_EXCEPTION_URL}{resp.data['id']}/", user=self.manager)
        self.assertEqual(resp.status_code, 204)


class TestRecordPaymentGate(HardeningBase):

    def test_agent_cannot_record_a_payment(self):
        booking = self.booking(status='confirmed', total_amount=Decimal('80.00'))
        url = f'{ADMIN_BOOKING_URL}{booking.id}/record_payment/'
        resp = self.api_post(url, {'amount': '30', 'method': 'cash'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_post(url, {'amount': '30', 'method': 'cash'}, user=self.staff_user)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal('0'))
        self.assertEqual(booking.payment_status, 'pending')
        self.assertEqual(booking.manual_payments, [])

    def test_manager_records_a_payment(self):
        booking = self.booking(status='confirmed', total_amount=Decimal('80.00'))
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/record_payment/',
                             {'amount': '80', 'method': 'card'}, user=self.manager)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['payment_status'], 'fully_paid')
        self.assertEqual(resp.data['manual_payments'][0]['recorded_by'], self.manager.id)

    def test_agent_still_works_the_booking(self):
        booking = self.booking(status='pending')
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/confirm/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/check_in/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/complete/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'completed')


class TestRecurringWrites(HardeningBase):

    def payload(self, **extra):
        data = {'client_id': self.create_client().id, 'service_id': self.service.id, 'staff_id': self.staff.id,
                'frequency': 'weekly', 'preferred_day_of_week': 0, 'preferred_time': '10:00'}
        data.update(extra)
        return data

    def test_agent_reads(self):
        recurring = self.create_recurring_booking(self.service, staff=self.staff)
        resp = self.api_get(ADMIN_RECURRING_URL, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r['id'] for r in BookingTestCase.get_results(resp)], [recurring.id])
        self.assertEqual(self.api_get(f'{ADMIN_RECURRING_URL}{recurring.id}/', user=self.agent).status_code, 200)

    def test_agent_cannot_write(self):
        recurring = self.create_recurring_booking(self.service, staff=self.staff)
        resp = self.api_post(ADMIN_RECURRING_URL, self.payload(), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_patch(f'{ADMIN_RECURRING_URL}{recurring.id}/', {'frequency': 'monthly'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        for name in ('pause', 'resume', 'cancel'):
            resp = self.api_post(f'{ADMIN_RECURRING_URL}{recurring.id}/{name}/', {}, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, name)
        resp = self.api_delete(f'{ADMIN_RECURRING_URL}{recurring.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        recurring.refresh_from_db()
        self.assertEqual(recurring.status, 'active')
        self.assertEqual(recurring.frequency, 'weekly')
        self.assertEqual(RecurringBooking.objects.count(), 1)

    def test_manager_writes(self):
        resp = self.api_post(ADMIN_RECURRING_URL, self.payload(), user=self.manager)
        self.assertEqual(resp.status_code, 201, resp.data)
        rid = resp.data['id']
        resp = self.api_post(f'{ADMIN_RECURRING_URL}{rid}/pause/', {}, user=self.manager)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'paused')
        resp = self.api_post(f'{ADMIN_RECURRING_URL}{rid}/resume/', {}, user=self.admin)
        self.assertEqual(resp.data['status'], 'active')
        resp = self.api_post(f'{ADMIN_RECURRING_URL}{rid}/cancel/', {}, user=self.manager)
        self.assertEqual(resp.data['status'], 'cancelled')
        resp = self.api_delete(f'{ADMIN_RECURRING_URL}{rid}/', user=self.manager)
        self.assertEqual(resp.status_code, 204)
