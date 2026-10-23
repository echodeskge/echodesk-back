"""Bookings API hardening for the staff app (parity package P2).

Role gate for destructive / catalog / settings writes, admin slot lookup,
check-in / no-show / record-payment actions, writable staff notes, list
filters + slim rows, recurring create, service category/staff writes, and
staff notifications (fan-out, no self-notify, no double notify, reminders).
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from rest_framework import status

from booking_management.models import Booking, BookingStaff, RecurringBooking, Service
from booking_management.tests.conftest import BookingTestCase
from booking_management.tests.test_views import (
    ADMIN_BOOKING_URL, ADMIN_CATEGORY_URL, ADMIN_SERVICE_URL, ADMIN_SETTINGS_URL, ADMIN_STAFF_URL,
    ADMIN_DASHBOARD_URL, ADMIN_SCHEDULE_URL, BookingViewTestMixin,
)
from users.models import Notification

ADMIN_SLOTS_URL = '/api/bookings/admin/slots/'
ADMIN_RECURRING_URL = '/api/bookings/admin/recurring-bookings/'
ADMIN_EXCEPTION_URL = '/api/bookings/admin/exceptions/'
ADMIN_TEST_SMS_URL = '/api/bookings/admin/settings/test-sms/'


def next_monday():
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


class HardeningBase(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='p2-admin@test.com')
        self._ensure_booking_feature()
        # An agent who has the bookings feature but runs nothing
        self.agent = self.create_user(email='p2-agent@test.com', role='agent', first_name='Ann', last_name='Agent')
        self.agent.has_feature = lambda key: True
        self.manager = self.create_manager(email='p2-manager@test.com')
        self.manager.has_feature = lambda key: True

        self.service = self.create_service(duration_minutes=60, buffer_time_minutes=0, base_price=Decimal('80.00'))
        self.staff_user = self.create_user(email='p2-staff@test.com', role='agent', first_name='Sam', last_name='Staff')
        self.staff_user.has_feature = lambda key: True
        self.staff = self.create_staff(user=self.staff_user)
        self.service.staff_members.add(self.staff)
        self.day = next_monday()
        self.create_availability(self.staff, day_of_week=0, start_time=time(9, 0), end_time=time(17, 0))

    def booking(self, **kwargs):
        defaults = dict(staff=self.staff, date=self.day, start_time=time(10, 0), end_time=time(11, 0))
        defaults.update(kwargs)
        return self.create_booking(self.service, **defaults)

    def notes(self, user, notification_type=None):
        qs = Notification.objects.filter(user=user)
        if notification_type:
            qs = qs.filter(notification_type=notification_type)
        return list(qs.order_by('id'))


# ============================================================================
# Permissions
# ============================================================================

class TestRoleGate(HardeningBase):

    def test_agent_cannot_hard_delete_booking(self):
        booking = self.booking()
        resp = self.api_delete(f'{ADMIN_BOOKING_URL}{booking.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Booking.objects.filter(pk=booking.pk).exists())

    def test_manager_and_admin_can_hard_delete(self):
        booking = self.booking()
        resp = self.api_delete(f'{ADMIN_BOOKING_URL}{booking.id}/', user=self.manager)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        booking = self.booking()
        resp = self.api_delete(f'{ADMIN_BOOKING_URL}{booking.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_agent_keeps_reads_and_booking_actions(self):
        booking = self.booking()
        self.assertEqual(self.api_get(ADMIN_BOOKING_URL, user=self.agent).status_code, 200)
        self.assertEqual(self.api_get(ADMIN_SERVICE_URL, user=self.agent).status_code, 200)
        self.assertEqual(self.api_get(ADMIN_CATEGORY_URL, user=self.agent).status_code, 200)
        self.assertEqual(self.api_get(ADMIN_STAFF_URL, user=self.agent).status_code, 200)
        self.assertEqual(self.api_get(f'{ADMIN_STAFF_URL}available_users/', user=self.agent).status_code, 200)
        self.assertEqual(self.api_get(ADMIN_SETTINGS_URL, user=self.agent).status_code, 200)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/confirm/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/cancel/', {'reason': 'x'}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_agent_cannot_write_catalog(self):
        category = self.create_category()
        cases = [
            self.api_post(ADMIN_CATEGORY_URL, {'name': {'en': 'X'}}, user=self.agent),
            self.api_patch(f'{ADMIN_CATEGORY_URL}{category.id}/', {'is_active': False}, user=self.agent),
            self.api_delete(f'{ADMIN_CATEGORY_URL}{category.id}/', user=self.agent),
            self.api_post(ADMIN_SERVICE_URL, {'name': {'en': 'S'}, 'base_price': '10', 'duration_minutes': 30},
                          user=self.agent),
            self.api_patch(f'{ADMIN_SERVICE_URL}{self.service.id}/', {'base_price': '1'}, user=self.agent),
            self.api_post(f'{ADMIN_SERVICE_URL}{self.service.id}/deactivate/', {}, user=self.agent),
            self.api_delete(f'{ADMIN_SERVICE_URL}{self.service.id}/', user=self.agent),
            self.api_post(ADMIN_STAFF_URL, {'user_id': self.agent.id}, user=self.agent),
            self.api_patch(f'{ADMIN_STAFF_URL}{self.staff.id}/', {'bio': 'x'}, user=self.agent),
            self.api_post(f'{ADMIN_STAFF_URL}{self.staff.id}/toggle_active/', {}, user=self.agent),
            self.api_delete(f'{ADMIN_STAFF_URL}{self.staff.id}/', user=self.agent),
        ]
        for resp in cases:
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.wsgi_request.path)
        self.service.refresh_from_db()
        self.assertEqual(self.service.status, 'active')
        self.assertEqual(self.service.base_price, Decimal('80.00'))
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.is_active_for_bookings)

    def test_manager_can_write_catalog(self):
        resp = self.api_post(ADMIN_CATEGORY_URL, {'name': {'en': 'Hair'}}, user=self.manager)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        resp = self.api_post(f'{ADMIN_SERVICE_URL}{self.service.id}/deactivate/', {}, user=self.manager)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'inactive')

    def test_agent_cannot_change_settings_or_send_test_sms(self):
        resp = self.api_patch(ADMIN_SETTINGS_URL, {'public_phone': '123'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_put(ADMIN_SETTINGS_URL, {'public_phone': '123'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        with patch('booking_management.sms.send_text') as send:
            resp = self.api_post(ADMIN_TEST_SMS_URL, {'phone': '599123456'}, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
            send.assert_not_called()

    def test_admin_can_change_settings(self):
        resp = self.api_patch(ADMIN_SETTINGS_URL, {'public_phone': '555'}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['public_phone'], '555')

    def test_agent_cannot_delete_recurring_series(self):
        recurring = self.create_recurring_booking(self.service, staff=self.staff)
        resp = self.api_delete(f'{ADMIN_RECURRING_URL}{recurring.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_post(f'{ADMIN_RECURRING_URL}{recurring.id}/pause/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200)

    def test_staff_without_feature_is_still_refused(self):
        outsider = self.create_user(email='p2-outsider@test.com', role='admin')
        outsider.has_feature = lambda key: False
        resp = self.api_get(ADMIN_BOOKING_URL, user=outsider)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# ============================================================================
# Admin slots
# ============================================================================

class TestAdminSlots(HardeningBase):

    def slots(self, user=None, **params):
        query = '&'.join(f'{k}={v}' for k, v in params.items())
        return self.api_get(f'{ADMIN_SLOTS_URL}?{query}', user=user or self.agent)

    def starts(self, resp):
        return [s['start_time'] for s in resp.data['slots']]

    def test_requires_service_and_date(self):
        self.assertEqual(self.slots(date=self.day.isoformat()).status_code, 400)
        self.assertEqual(self.slots(service=self.service.id).status_code, 400)
        self.assertEqual(self.slots(service=self.service.id, date='31-12-2030').status_code, 400)
        self.assertEqual(self.slots(service=999999, date=self.day.isoformat()).status_code, 400)

    def test_honours_hours_breaks_bookings_and_duration(self):
        avail = self.staff.availability.get(day_of_week=0)
        avail.break_start, avail.break_end = time(12, 0), time(13, 0)
        avail.save()
        self.booking(status='confirmed')  # 10:00-11:00
        resp = self.slots(service=self.service.id, date=self.day.isoformat(), staff=self.staff.id)
        self.assertEqual(resp.status_code, 200, resp.data)
        starts = self.starts(resp)
        self.assertEqual(starts[0], '09:00')
        self.assertEqual(starts[-1], '16:00')  # 60 min must end by 17:00
        for busy in ('09:30', '10:00', '10:30', '11:30', '12:00', '12:30'):
            self.assertNotIn(busy, starts)
        self.assertIn('11:00', starts)
        self.assertIn('13:00', starts)
        self.assertEqual(resp.data['staff_id'], self.staff.id)
        slot = resp.data['slots'][0]
        self.assertEqual(slot['end_time'], '10:00')
        self.assertEqual(slot['available_staff'][0]['staff_id'], self.staff.id)
        self.assertEqual(slot['staff_name'], 'Sam Staff')

    def test_ignores_customer_lead_time(self):
        settings = self.create_settings(min_hours_before_booking=24 * 60, max_days_advance_booking=1)
        self.assertTrue(settings.pk)
        resp = self.slots(service=self.service.id, date=self.day.isoformat())
        self.assertEqual(resp.status_code, 200)
        self.assertIn('09:00', self.starts(resp))

    def test_day_off_exception_and_no_hours(self):
        self.create_staff_exception(self.staff, exception_date=self.day, is_available=False)
        resp = self.slots(service=self.service.id, date=self.day.isoformat())
        self.assertEqual(resp.data['slots'], [])
        # Tuesday: no weekly hours
        resp = self.slots(service=self.service.id, date=(self.day + timedelta(days=1)).isoformat())
        self.assertEqual(resp.data['slots'], [])

    def test_special_hours_exception(self):
        self.create_staff_exception(self.staff, exception_date=self.day, is_available=True,
                                    start_time=time(14, 0), end_time=time(16, 0))
        resp = self.slots(service=self.service.id, date=self.day.isoformat())
        self.assertEqual(self.starts(resp), ['14:00', '14:30', '15:00'])

    def test_exclude_booking_frees_its_own_time(self):
        booking = self.booking(status='confirmed')
        resp = self.slots(service=self.service.id, date=self.day.isoformat(), exclude_booking=booking.id)
        self.assertIn('10:00', self.starts(resp))

    def test_no_show_and_cancelled_free_the_time(self):
        self.booking(status='no_show')
        self.booking(status='cancelled', start_time=time(13, 0), end_time=time(14, 0))
        starts = self.starts(self.slots(service=self.service.id, date=self.day.isoformat()))
        self.assertIn('10:00', starts)
        self.assertIn('13:00', starts)

    def test_staff_must_provide_service(self):
        other = self.create_staff()
        resp = self.slots(service=self.service.id, date=self.day.isoformat(), staff=other.id)
        self.assertEqual(resp.status_code, 400)

    def test_any_staff_groups_by_time(self):
        staff2 = self.create_staff()
        self.service.staff_members.add(staff2)
        self.create_availability(staff2, day_of_week=0, start_time=time(15, 0), end_time=time(18, 0))
        resp = self.slots(service=self.service.id, date=self.day.isoformat())
        by_time = {s['start_time']: s for s in resp.data['slots']}
        self.assertEqual(len(by_time['15:00']['available_staff']), 2)
        self.assertEqual(by_time['17:00']['staff_id'], staff2.id)

    def test_step_and_past_times(self):
        resp = self.slots(service=self.service.id, date=self.day.isoformat(), step=15)
        self.assertIn('09:15', self.starts(resp))
        self.assertEqual(self.slots(service=self.service.id, date=self.day.isoformat(), step=1).status_code, 400)
        # A past day lists nothing unless include_past
        past = self.day - timedelta(days=14)
        self.assertEqual(self.slots(service=self.service.id, date=past.isoformat()).data['slots'], [])
        resp = self.slots(service=self.service.id, date=past.isoformat(), include_past='true')
        self.assertIn('09:00', self.starts(resp))


# ============================================================================
# Booking actions / update / list
# ============================================================================

class TestBookingActions(HardeningBase):

    def test_check_in_then_complete(self):
        booking = self.booking(status='confirmed')
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/check_in/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'in_progress')
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/check_in/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 400)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/complete/', {}, user=self.agent)
        self.assertEqual(resp.data['status'], 'completed')

    def test_no_show(self):
        booking = self.booking(status='confirmed')
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/no_show/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'no_show')
        # Over: can't cancel, move, check in or mark again
        for path in ('no_show', 'check_in', 'cancel'):
            resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/{path}/', {}, user=self.agent)
            self.assertEqual(resp.status_code, 400, path)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/reschedule/',
                             {'date': self.day.isoformat(), 'start_time': '14:00'}, user=self.agent)
        self.assertEqual(resp.status_code, 400)

    def test_record_payment_partial_then_full(self):
        booking = self.booking(status='confirmed', total_amount=Decimal('80.00'))
        url = f'{ADMIN_BOOKING_URL}{booking.id}/record_payment/'
        resp = self.api_post(url, {'amount': '30', 'method': 'cash', 'note': 'deposit at desk'}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['payment_status'], 'deposit_paid')
        self.assertEqual(Decimal(resp.data['paid_amount']), Decimal('30'))
        self.assertEqual(resp.data['remaining_amount'], 50.0)
        self.assertEqual(len(resp.data['manual_payments']), 1)
        entry = resp.data['manual_payments'][0]
        self.assertEqual(entry['method'], 'cash')
        self.assertEqual(entry['amount'], '30.00')
        self.assertEqual(entry['recorded_by'], self.agent.id)
        self.assertEqual(entry['note'], 'deposit at desk')

        # More than what is owed
        resp = self.api_post(url, {'amount': '60'}, user=self.agent)
        self.assertEqual(resp.status_code, 400)
        resp = self.api_post(url, {'amount': '50', 'method': 'card'}, user=self.agent)
        self.assertEqual(resp.data['payment_status'], 'fully_paid')
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal('80.00'))
        self.assertEqual(len(booking.manual_payments), 2)

    def test_record_payment_validation(self):
        booking = self.booking(status='cancelled')
        url = f'{ADMIN_BOOKING_URL}{booking.id}/record_payment/'
        self.assertEqual(self.api_post(url, {'amount': '10'}, user=self.agent).status_code, 400)
        booking = self.booking(status='confirmed', start_time=time(14, 0), end_time=time(15, 0))
        url = f'{ADMIN_BOOKING_URL}{booking.id}/record_payment/'
        self.assertEqual(self.api_post(url, {'amount': '0'}, user=self.agent).status_code, 400)
        self.assertEqual(self.api_post(url, {'amount': '5', 'method': 'bitcoin'}, user=self.agent).status_code, 400)
        self.assertEqual(self.api_post(url, {}, user=self.agent).status_code, 400)

    def test_recorded_cash_counts_as_revenue(self):
        booking = self.booking(status='confirmed', date=date.today())
        self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/record_payment/', {'amount': '80'}, user=self.agent)
        resp = self.api_get(ADMIN_DASHBOARD_URL, user=self.agent)
        self.assertEqual(resp.data['overall']['total_revenue'], 80.0)

    def test_patch_staff_notes_only(self):
        booking = self.booking(status='pending')
        resp = self.api_patch(f'{ADMIN_BOOKING_URL}{booking.id}/', {
            'staff_notes': 'Prefers the window seat', 'status': 'completed', 'date': '2000-01-01',
        }, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['staff_notes'], 'Prefers the window seat')
        booking.refresh_from_db()
        self.assertEqual(booking.staff_notes, 'Prefers the window seat')
        self.assertEqual(booking.status, 'pending')  # status only via actions
        self.assertEqual(booking.date, self.day)
        # Response is the detail shape (a superset of the old list shape)
        for key in ('id', 'client', 'service', 'staff', 'status', 'remaining_amount', 'manual_payments'):
            self.assertIn(key, resp.data)

    def test_reschedule_and_assign_require_active_booking(self):
        booking = self.booking(status='cancelled')
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/reschedule/',
                             {'date': self.day.isoformat(), 'start_time': '14:00'}, user=self.agent)
        self.assertEqual(resp.status_code, 400)
        staff2 = self.create_staff()
        self.service.staff_members.add(staff2)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/assign_staff/', {'staff_id': staff2.id}, user=self.agent)
        self.assertEqual(resp.status_code, 400)

    def test_reschedule_accepts_seconds(self):
        booking = self.booking(status='confirmed')
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/reschedule/',
                             {'date': self.day.isoformat(), 'start_time': '14:00:00'}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['start_time'], '14:00:00')

    def test_assign_staff_checks_working_hours(self):
        booking = self.booking(status='confirmed')
        staff2 = self.create_staff()
        self.service.staff_members.add(staff2)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/assign_staff/', {'staff_id': staff2.id}, user=self.agent)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('not available', resp.data['error'])

    def test_cancel_with_empty_reason_gets_default(self):
        booking = self.booking(status='confirmed')
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/cancel/', {'reason': ''}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['cancellation_reason'], 'Cancelled by admin')

    def test_confirm_card_booking_needs_force(self):
        booking = self.booking(status='pending', payment_method='card')
        url = f'{ADMIN_BOOKING_URL}{booking.id}/confirm/'
        self.assertEqual(self.api_post(url, {}, user=self.agent).status_code, 400)
        self.assertEqual(self.api_post(url, {'force': False}, user=self.agent).status_code, 400)
        resp = self.api_post(url, {'force': True}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'confirmed')


class TestBookingLists(HardeningBase):

    def test_date_range_and_mine(self):
        mine = self.booking()
        other_staff = self.create_staff()
        self.service.staff_members.add(other_staff)
        theirs = self.booking(staff=other_staff, date=self.day + timedelta(days=7))
        resp = self.api_get(f'{ADMIN_BOOKING_URL}?mine=true', user=self.staff_user)
        self.assertEqual([b['id'] for b in self.get_results(resp)], [mine.id])
        resp = self.api_get(f'{ADMIN_BOOKING_URL}?date_from={(self.day + timedelta(days=1)).isoformat()}',
                            user=self.agent)
        self.assertEqual([b['id'] for b in self.get_results(resp)], [theirs.id])
        resp = self.api_get(f'{ADMIN_BOOKING_URL}?date_to={self.day.isoformat()}', user=self.agent)
        self.assertEqual([b['id'] for b in self.get_results(resp)], [mine.id])

    def test_lite_rows(self):
        booking = self.booking()
        full = self.get_results(self.api_get(ADMIN_BOOKING_URL, user=self.agent))[0]
        self.assertIn('staff_members', full['service'])  # default shape unchanged
        lite = self.get_results(self.api_get(f'{ADMIN_BOOKING_URL}?lite=1', user=self.agent))[0]
        self.assertEqual(set(lite['service'].keys()), {'id', 'name_display', 'duration_minutes'})
        self.assertEqual(lite['staff'], {
            'id': self.staff.id, 'user_id': self.staff_user.id, 'full_name': 'Sam Staff', 'profile_image': None,
        })
        self.assertEqual(set(lite['client'].keys()), {'id', 'full_name', 'phone_number', 'email'})
        rows = self.api_get(f'{ADMIN_BOOKING_URL}rows/?status=pending', user=self.agent)
        self.assertEqual(rows.status_code, 200)
        self.assertIn('count', rows.data)
        self.assertEqual(rows.data['results'][0]['id'], booking.id)
        self.assertEqual(rows.data['results'][0]['service'], lite['service'])

    def test_schedule_and_dashboard_lite(self):
        self.booking(date=date.today(), status='confirmed')
        resp = self.api_get(f'{ADMIN_SCHEDULE_URL}?date={date.today().isoformat()}&lite=1', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        entry = [e for e in resp.data['schedule'] if e['staff']['id'] == self.staff.id][0]
        self.assertEqual(entry['booking_count'], 1)
        self.assertNotIn('staff_members', entry['bookings'][0]['service'])
        resp = self.api_get(f'{ADMIN_DASHBOARD_URL}?lite=1', user=self.agent)
        self.assertNotIn('staff_members', resp.data['today']['bookings'][0]['service'])

    def test_dashboard_popular_services_is_this_week_only(self):
        far = date.today() + timedelta(days=60)
        self.booking(date=far)
        self.booking(date=date.today(), status='cancelled')
        resp = self.api_get(ADMIN_DASHBOARD_URL, user=self.agent)
        self.assertEqual(resp.data['week']['popular_services'], [])

    def test_staff_me(self):
        resp = self.api_get(f'{ADMIN_STAFF_URL}me/', user=self.staff_user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], self.staff.id)
        resp = self.api_get(f'{ADMIN_STAFF_URL}me/', user=self.agent)
        self.assertEqual(resp.status_code, 404)

    def test_staff_bookings_bad_date(self):
        resp = self.api_get(f'{ADMIN_STAFF_URL}{self.staff.id}/bookings/?date=nope', user=self.agent)
        self.assertEqual(resp.status_code, 400)

    def test_exceptions_show_past(self):
        past = self.create_staff_exception(self.staff, exception_date=date.today() - timedelta(days=10))
        future = self.create_staff_exception(self.staff, exception_date=date.today() + timedelta(days=10))
        ids = [e['id'] for e in self.get_results(self.api_get(ADMIN_EXCEPTION_URL, user=self.agent))]
        self.assertEqual(ids, [future.id])
        ids = [e['id'] for e in self.get_results(self.api_get(f'{ADMIN_EXCEPTION_URL}?show_past=true', user=self.agent))]
        self.assertEqual(ids, [past.id, future.id])


# ============================================================================
# Catalog writes, recurring create
# ============================================================================

class TestCatalogAndRecurring(HardeningBase):

    def test_service_create_with_category_and_staff(self):
        category = self.create_category()
        resp = self.api_post(ADMIN_SERVICE_URL, {
            'name': {'en': 'Beard', 'ka': 'წვერი'}, 'base_price': '25.00', 'duration_minutes': 30,
            'category_id': category.id, 'staff_ids': [self.staff.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['category']['id'], category.id)
        self.assertEqual([s['id'] for s in resp.data['staff_members']], [self.staff.id])
        self.assertIn('id', resp.data)
        service_id = resp.data['id']

        # Clear the category, keep staff
        resp = self.api_patch(f'{ADMIN_SERVICE_URL}{service_id}/', {'category_id': None}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIsNone(resp.data['category'])
        self.assertEqual(len(resp.data['staff_members']), 1)
        resp = self.api_patch(f'{ADMIN_SERVICE_URL}{service_id}/', {'staff_ids': []}, user=self.admin)
        self.assertEqual(resp.data['staff_members'], [])

    def test_service_write_validates_ids(self):
        resp = self.api_patch(f'{ADMIN_SERVICE_URL}{self.service.id}/', {'category_id': 999999}, user=self.admin)
        self.assertEqual(resp.status_code, 400)
        resp = self.api_patch(f'{ADMIN_SERVICE_URL}{self.service.id}/', {'staff_ids': [999999]}, user=self.admin)
        self.assertEqual(resp.status_code, 400)

    def test_service_plain_string_name_still_accepted(self):
        # The web services page sends a plain string name
        resp = self.api_patch(f'{ADMIN_SERVICE_URL}{self.service.id}/', {'name': 'Cut', 'base_price': '30'},
                              user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['name_display'], 'Cut')
        self.assertIsNotNone(resp.data['category'])  # untouched

    def test_staff_create_returns_id(self):
        user = self.create_user(email='p2-newstaff@test.com')
        resp = self.api_post(ADMIN_STAFF_URL, {'user_id': user.id, 'service_ids': [self.service.id]}, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['id'], BookingStaff.objects.get(user=user).id)
        self.assertEqual(resp.data['user']['id'], user.id)
        self.assertEqual(resp.data['services_count'], 1)

    def test_recurring_create(self):
        client = self.create_client(is_booking_enabled=False)
        resp = self.api_post(ADMIN_RECURRING_URL, {
            'client_id': client.id, 'service_id': self.service.id, 'staff_id': self.staff.id,
            'frequency': 'weekly', 'preferred_day_of_week': 0, 'preferred_time': '10:00',
        }, user=self.agent)
        self.assertEqual(resp.status_code, 201, resp.data)
        recurring = RecurringBooking.objects.get(id=resp.data['id'])
        self.assertEqual(recurring.client, client)
        self.assertEqual(recurring.next_booking_date.weekday(), 0)
        self.assertGreater(recurring.next_booking_date, date.today())
        self.assertEqual(resp.data['service']['id'], self.service.id)
        client.refresh_from_db()
        self.assertTrue(client.is_booking_enabled)

    def test_recurring_create_validation(self):
        base = {'client_id': self.create_client().id, 'service_id': self.service.id,
                'frequency': 'weekly', 'preferred_day_of_week': 0, 'preferred_time': '10:00'}
        self.assertEqual(self.api_post(ADMIN_RECURRING_URL, dict(base, client_id=999999), user=self.agent).status_code, 400)
        self.assertEqual(self.api_post(ADMIN_RECURRING_URL, dict(base, service_id=999999), user=self.agent).status_code, 400)
        other = self.create_staff()
        self.assertEqual(self.api_post(ADMIN_RECURRING_URL, dict(base, staff_id=other.id), user=self.agent).status_code, 400)
        self.assertEqual(self.api_post(ADMIN_RECURRING_URL, dict(base, preferred_day_of_week=9), user=self.agent).status_code, 400)
        self.assertEqual(self.api_post(ADMIN_RECURRING_URL, dict(base, end_date='2000-01-01'), user=self.agent).status_code, 400)

    def test_admin_create_by_client_id_enables_booking_client(self):
        client = self.create_client(is_booking_enabled=False)
        resp = self.api_post(ADMIN_BOOKING_URL, {
            'service_id': self.service.id, 'staff_id': self.staff.id, 'date': self.day.isoformat(),
            'start_time': '14:00', 'client_id': client.id,
        }, user=self.agent)
        self.assertEqual(resp.status_code, 201, resp.data)
        client.refresh_from_db()
        self.assertTrue(client.is_booking_enabled)


# ============================================================================
# Staff notifications
# ============================================================================

class TestStaffNotifications(HardeningBase):

    def assert_booking_note(self, note, booking, notification_type):
        self.assertEqual(note.notification_type, notification_type)
        self.assertEqual(note.link_url, f'/bookings/bookings/{booking.id}')
        self.assertEqual(note.metadata['booking_id'], booking.id)
        self.assertEqual(note.metadata['booking_number'], booking.booking_number)

    def test_colleague_creates_booking_notifies_assigned_staff_once(self):
        resp = self.api_post(ADMIN_BOOKING_URL, {
            'service_id': self.service.id, 'staff_id': self.staff.id, 'date': self.day.isoformat(),
            'start_time': '14:00', 'first_name': 'Ana', 'phone_number': '599123456', 'status': 'confirmed',
        }, user=self.agent)
        self.assertEqual(resp.status_code, 201, resp.data)
        booking = Booking.objects.get(id=resp.data['id'])
        notes = self.notes(self.staff_user)
        self.assertEqual(len(notes), 1)  # 'created' only, no extra 'confirmed'
        self.assert_booking_note(notes[0], booking, 'booking_created')
        self.assertEqual(self.notes(self.agent), [])

    def test_pending_booking_by_colleague_notifies_staff(self):
        resp = self.api_post(ADMIN_BOOKING_URL, {
            'service_id': self.service.id, 'staff_id': self.staff.id, 'date': self.day.isoformat(),
            'start_time': '14:00', 'first_name': 'Ana', 'phone_number': '599123456', 'status': 'pending',
        }, user=self.agent)
        booking = Booking.objects.get(id=resp.data['id'])
        self.assert_booking_note(self.notes(self.staff_user, 'booking_created')[0], booking, 'booking_created')

    def test_staff_booking_their_own_client_is_not_notified(self):
        resp = self.api_post(ADMIN_BOOKING_URL, {
            'service_id': self.service.id, 'staff_id': self.staff.id, 'date': self.day.isoformat(),
            'start_time': '14:00', 'first_name': 'Ana', 'phone_number': '599123456',
        }, user=self.staff_user)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(self.notes(self.staff_user), [])

    def test_confirm_and_cancel_notify_staff_but_not_actor(self):
        booking = self.booking(status='pending')
        self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/confirm/', {}, user=self.agent)
        self.assert_booking_note(self.notes(self.staff_user, 'booking_confirmed')[0], booking, 'booking_confirmed')
        # The assigned staff member cancels it themselves: no notice to them
        self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/cancel/', {}, user=self.staff_user)
        self.assertEqual(self.notes(self.staff_user, 'booking_cancelled'), [])

    def test_cancel_by_colleague_notifies_staff(self):
        booking = self.booking(status='confirmed')
        self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/cancel/', {'reason': 'ill'}, user=self.agent)
        notes = self.notes(self.staff_user, 'booking_cancelled')
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].metadata['cancelled_by'], 'admin')

    def test_reschedule_notifies_staff(self):
        booking = self.booking(status='confirmed')
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/reschedule/',
                             {'date': self.day.isoformat(), 'start_time': '15:00'}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        notes = self.notes(self.staff_user, 'booking_rescheduled')
        self.assertEqual(len(notes), 1)
        self.assert_booking_note(notes[0], booking, 'booking_rescheduled')
        self.assertEqual(notes[0].metadata['start_time'], '15:00')

    def test_assign_staff_notifies_new_and_previous(self):
        booking = self.booking(status='confirmed', staff_reminder_sent=True)
        new_user = self.create_user(email='p2-new@test.com', first_name='Nia', last_name='New')
        staff2 = self.create_staff(user=new_user)
        self.service.staff_members.add(staff2)
        self.create_availability(staff2, day_of_week=0)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/assign_staff/', {'staff_id': staff2.id}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['staff']['id'], staff2.id)
        self.assert_booking_note(self.notes(new_user, 'booking_assigned')[0], booking, 'booking_assigned')
        old = self.notes(self.staff_user, 'booking_assigned')
        self.assertEqual(len(old), 1)
        self.assertIn('Nia New', old[0].message)
        self.assertEqual(old[0].metadata['kind'], 'unassigned')
        booking.refresh_from_db()
        self.assertFalse(booking.staff_reminder_sent)  # the new staff member gets their own reminder

    def test_customer_cancel_notifies_assigned_staff_once(self):
        from booking_management.views_client import _cancel_booking
        booking = self.booking(status='confirmed')
        _cancel_booking(booking, 'changed my mind')
        notes = self.notes(self.staff_user, 'booking_cancelled')
        self.assertEqual(len(notes), 1)
        # ...and the business runners got it too
        self.assertEqual(len(self.notes(self.admin, 'booking_cancelled')), 1)

    def test_notifications_for_two_bookings_are_not_merged(self):
        first = self.booking(status='pending')
        second = self.booking(status='pending', start_time=time(14, 0), end_time=time(15, 0))
        self.api_post(f'{ADMIN_BOOKING_URL}{first.id}/confirm/', {}, user=self.agent)
        self.api_post(f'{ADMIN_BOOKING_URL}{second.id}/confirm/', {}, user=self.agent)
        notes = self.notes(self.staff_user, 'booking_confirmed')
        self.assertEqual([n.metadata['booking_id'] for n in notes], [first.id, second.id])
        self.assertEqual(notes[1].link_url, f'/bookings/bookings/{second.id}')

    def test_push_payload_carries_booking_id(self):
        booking = self.booking(status='pending')
        with patch('users.notification_utils._dispatch_push') as push:
            self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/confirm/', {}, user=self.agent)
        push.assert_called_once()
        data = push.call_args.kwargs['data']
        self.assertEqual(data['booking_id'], booking.id)
        self.assertEqual(data['link_url'], f'/bookings/bookings/{booking.id}')
        self.assertEqual(data['notification_type'], 'booking_confirmed')

    def test_ka_texts(self):
        self.tenant.preferred_language = 'ka'
        self.tenant.save(update_fields=['preferred_language'])
        try:
            booking = self.booking(status='pending')
            self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/confirm/', {}, user=self.agent)
            self.assertEqual(self.notes(self.staff_user, 'booking_confirmed')[0].title, 'ჯავშანი დადასტურდა')
        finally:
            self.tenant.preferred_language = 'en'
            self.tenant.save(update_fields=['preferred_language'])


class TestStaffReminders(HardeningBase):

    def run_task(self, now):
        from booking_management import tasks
        with patch('booking_management.utils.tenant_now', return_value=now):
            return tasks._send_staff_reminders_for_tenant()

    def test_reminds_once_within_window(self):
        now = datetime.combine(self.day, time(9, 40))
        soon = self.booking(status='confirmed')  # 10:00
        later = self.booking(status='confirmed', start_time=time(12, 0), end_time=time(13, 0))
        cancelled = self.booking(status='cancelled', start_time=time(10, 15), end_time=time(11, 15))
        self.assertEqual(self.run_task(now), 1)
        notes = self.notes(self.staff_user, 'booking_reminder')
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].metadata['booking_id'], soon.id)
        self.assertEqual(notes[0].title, 'Next client in 20 min')
        self.assertEqual(notes[0].link_url, f'/bookings/bookings/{soon.id}')
        # Not again
        self.assertEqual(self.run_task(now), 0)
        soon.refresh_from_db()
        later.refresh_from_db()
        cancelled.refresh_from_db()
        self.assertTrue(soon.staff_reminder_sent)
        self.assertFalse(later.staff_reminder_sent)
        self.assertFalse(cancelled.staff_reminder_sent)

    def test_reschedule_resets_the_reminder(self):
        booking = self.booking(status='confirmed', staff_reminder_sent=True)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/reschedule/',
                             {'date': self.day.isoformat(), 'start_time': '15:00'}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        booking.refresh_from_db()
        self.assertFalse(booking.staff_reminder_sent)

    def test_unassigned_booking_is_skipped(self):
        self.booking(status='confirmed', staff=None)
        self.assertEqual(self.run_task(datetime.combine(self.day, time(9, 45))), 0)

    def test_task_iterates_tenants(self):
        from booking_management import tasks
        with patch.object(tasks, '_send_staff_reminders_for_tenant', return_value=2) as inner:
            total = tasks.send_staff_booking_reminders()
        self.assertGreaterEqual(inner.call_count, 1)
        self.assertEqual(total, 2 * inner.call_count)


# ============================================================================
# Deleting rows that bookings still reference
# ============================================================================

class TestProtectedDeletes(HardeningBase):

    def test_service_with_bookings_answers_409(self):
        self.booking()
        resp = self.api_delete(f'{ADMIN_SERVICE_URL}{self.service.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('error', resp.json())
        self.assertTrue(Service.objects.filter(pk=self.service.pk).exists())

    def test_service_without_bookings_is_deleted(self):
        resp = self.api_delete(f'{ADMIN_SERVICE_URL}{self.service.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Service.objects.filter(pk=self.service.pk).exists())

    def test_social_client_with_bookings_answers_409(self):
        booking = self.booking()
        resp = self.api_delete(f'/api/social/clients/{booking.client_id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(Booking.objects.filter(pk=booking.pk).exists())
