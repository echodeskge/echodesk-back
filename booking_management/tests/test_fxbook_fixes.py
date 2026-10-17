"""Review fixes for the bookings parity package (parity-fix/bookings).

1. Refunds only cover money paid online; cash recorded at the desk is kept
   on the booking and flagged for returning by hand.
2. assign_staff / admin slots / admin create refuse staff who are not active
   for bookings.
3. check_in of a pending unpaid online-card booking needs force=true.
4. PATCH total_amount keeps payment_status consistent and can't go below
   what was paid.
5. Invalid date_from / date_to on the list and rows/ is a 400.
6. Staff reminders cross midnight and skip a booking the staff member just
   made for themselves.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.utils import timezone
from rest_framework import status

from booking_management.models import Booking, BookingSettings
from booking_management.tests.test_admin_hardening import ADMIN_SLOTS_URL, HardeningBase
from booking_management.tests.test_views import ADMIN_BOOKING_URL
from booking_management.utils import (
    calculate_manual_refund_due, calculate_refund_amount, flag_manual_refund, manual_paid_amount,
    online_paid_amount,
)

REFUND_PATH = 'tenants.bog_payment.BOGPaymentService.refund_payment'
MANAGE_URL = '/api/bookings/client/manage/'


class FxBase(HardeningBase):

    def card_settings(self, **kwargs):
        BookingSettings.objects.filter(tenant=self.tenant).delete()
        settings = self.create_settings(**kwargs)
        settings.bog_client_id = 'tenant-bog-id'
        settings.bog_client_secret = 'tenant-bog-secret'
        settings.save()
        return settings

    def deposit_plus_cash_booking(self):
        """The review's example: 20 GEL paid online (BOG deposit), 60 GEL cash at the desk."""
        booking = self.booking(
            status='confirmed', total_amount=Decimal('80.00'), deposit_amount=Decimal('20.00'),
            paid_amount=Decimal('20.00'), payment_status='deposit_paid', payment_method='card',
            bog_order_id='bog-order-fx',
        )
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/record_payment/',
                             {'amount': '60', 'method': 'cash'}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['payment_status'], 'fully_paid')
        booking.refresh_from_db()
        return booking


# ============================================================================
# 1. Refunds: online part only, cash flagged
# ============================================================================

class TestRefundOnlineOnly(FxBase):

    def test_amount_helpers(self):
        booking = self.deposit_plus_cash_booking()
        self.assertEqual(manual_paid_amount(booking), Decimal('60.00'))
        self.assertEqual(online_paid_amount(booking), Decimal('20.00'))
        full = self.card_settings(refund_policy='full')
        self.assertEqual(calculate_refund_amount(booking, full), Decimal('20.00'))
        self.assertEqual(calculate_manual_refund_due(booking, full), Decimal('60.00'))
        half = self.card_settings(refund_policy='partial_50')
        self.assertEqual(calculate_refund_amount(booking, half), Decimal('10.00'))
        self.assertEqual(calculate_manual_refund_due(booking, half), Decimal('30.00'))
        none = self.card_settings(refund_policy='no_refund')
        self.assertEqual(calculate_refund_amount(booking, none), Decimal('0'))
        self.assertEqual(calculate_manual_refund_due(booking, none), Decimal('0'))

    def test_garbage_manual_entries_are_ignored(self):
        booking = self.booking(paid_amount=Decimal('30.00'),
                               manual_payments=[{'amount': 'abc'}, {'amount': '10'}, None, {}])
        self.assertEqual(manual_paid_amount(booking), Decimal('10'))
        self.assertEqual(online_paid_amount(booking), Decimal('20.00'))

    def test_full_policy_refunds_only_the_online_deposit(self):
        self.card_settings(refund_policy='full')
        booking = self.deposit_plus_cash_booking()
        with patch(REFUND_PATH, return_value={'key': 'request_received'}) as refund:
            resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/cancel/', {'reason': 'closed'}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        # The whole BOG order (20 GEL) is refunded, not 80
        refund.assert_called_once_with(order_id='bog-order-fx', amount=None)
        self.assertEqual(resp.data['refund'], {'status': 'refunded', 'amount': '20.00'})
        self.assertEqual(resp.data['manual_refund_due'], '60.00')
        self.assertEqual(resp.data['status'], 'cancelled')
        booking.refresh_from_db()
        # The cash stays on record, with a note to give it back by hand
        self.assertEqual(booking.paid_amount, Decimal('60.00'))
        self.assertEqual(len(booking.manual_payments), 1)
        self.assertEqual(booking.payment_metadata['refund']['amount'], '20.00')
        self.assertEqual(booking.payment_metadata['manual_refund'], {'status': 'due', 'amount': '60.00'})
        self.assertIn('60.00 GEL by hand', booking.staff_notes)
        self.assertIn('60.00 GEL by hand', resp.data['staff_notes'])

    def test_partial_50_refunds_half_of_online_not_half_of_everything(self):
        self.card_settings(refund_policy='partial_50')
        booking = self.deposit_plus_cash_booking()
        with patch(REFUND_PATH, return_value={'key': 'request_received'}) as refund:
            resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/cancel/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        # Before the fix this asked BOG for 40 GEL against a 20 GEL order
        refund.assert_called_once_with(order_id='bog-order-fx', amount=Decimal('10.00'))
        self.assertEqual(resp.data['refund'], {'status': 'refunded', 'amount': '10.00'})
        self.assertEqual(resp.data['manual_refund_due'], '30.00')
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal('70.00'))
        self.assertNotEqual(booking.payment_metadata['refund']['status'], 'manual_required')
        self.assertEqual(booking.payment_metadata['manual_refund']['amount'], '30.00')

    def test_cash_only_booking_never_calls_bog(self):
        self.card_settings(refund_policy='full')
        booking = self.booking(status='confirmed', total_amount=Decimal('80.00'))
        self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/record_payment/', {'amount': '80'}, user=self.agent)
        with patch(REFUND_PATH) as refund:
            resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/cancel/', {}, user=self.agent)
        refund.assert_not_called()
        self.assertEqual(resp.data['refund']['status'], 'no_refund')
        self.assertEqual(resp.data['manual_refund_due'], '80.00')
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal('80.00'))

    def test_refund_false_flags_nothing(self):
        self.card_settings(refund_policy='full')
        booking = self.deposit_plus_cash_booking()
        with patch(REFUND_PATH) as refund:
            resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/cancel/', {'refund': False}, user=self.agent)
        refund.assert_not_called()
        self.assertEqual(resp.data['manual_refund_due'], '0.00')
        self.assertEqual(resp.data['refund']['status'], 'no_refund')
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal('80.00'))
        self.assertNotIn('manual_refund', booking.payment_metadata or {})

    def test_nothing_paid_reports_zero(self):
        booking = self.booking(status='confirmed')
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/cancel/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['manual_refund_due'], '0.00')
        self.assertEqual(resp.data['refund'], {'status': 'no_refund', 'amount': '0'})

    def test_bog_failure_still_flags_cash(self):
        self.card_settings(refund_policy='full')
        booking = self.deposit_plus_cash_booking()
        with patch(REFUND_PATH, side_effect=ValueError('refused')):
            resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/cancel/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['refund'], {'status': 'manual_required', 'amount': '20.00'})
        self.assertEqual(resp.data['manual_refund_due'], '60.00')

    def test_flag_is_idempotent(self):
        settings = self.card_settings(refund_policy='full')
        booking = self.booking(paid_amount=Decimal('60.00'), manual_payments=[{'amount': '60.00'}])
        self.assertEqual(flag_manual_refund(booking, settings), Decimal('60.00'))
        self.assertEqual(flag_manual_refund(booking, settings), Decimal('60.00'))
        booking.refresh_from_db()
        self.assertEqual(booking.staff_notes.count('by hand'), 1)

    def test_customer_cancel_flags_cash_too(self):
        self.card_settings(refund_policy='full', cancellation_hours_before=1)
        booking = self.deposit_plus_cash_booking()
        Booking.objects.filter(pk=booking.pk).update(manage_token='tok-fx-cash')
        with patch(REFUND_PATH, return_value={'key': 'request_received'}) as refund:
            resp = self.client.post(f'{MANAGE_URL}tok-fx-cash/cancel/', {}, format='json',
                                    HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, getattr(resp, 'data', None))
        refund.assert_called_once_with(order_id='bog-order-fx', amount=None)
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal('60.00'))
        self.assertEqual(booking.payment_metadata['manual_refund']['amount'], '60.00')


# ============================================================================
# 2. Inactive staff
# ============================================================================

class TestInactiveStaff(FxBase):

    def inactive_colleague(self):
        staff2 = self.create_staff()
        self.service.staff_members.add(staff2)
        self.create_availability(staff2, day_of_week=0, start_time=time(9, 0), end_time=time(17, 0))
        staff2.is_active_for_bookings = False
        staff2.save(update_fields=['is_active_for_bookings'])
        return staff2

    def test_assign_staff_refuses_inactive_staff(self):
        booking = self.booking(status='confirmed')
        staff2 = self.inactive_colleague()
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/assign_staff/', {'staff_id': staff2.id}, user=self.agent)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['error'], 'Staff member is not active for bookings')
        booking.refresh_from_db()
        self.assertEqual(booking.staff_id, self.staff.id)

    def test_assign_staff_to_active_colleague_still_works(self):
        booking = self.booking(status='confirmed')
        staff2 = self.inactive_colleague()
        staff2.is_active_for_bookings = True
        staff2.save(update_fields=['is_active_for_bookings'])
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/assign_staff/', {'staff_id': staff2.id}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['staff']['id'], staff2.id)

    def test_admin_slots_names_the_real_reason(self):
        staff2 = self.inactive_colleague()
        resp = self.api_get(f'{ADMIN_SLOTS_URL}?service={self.service.id}&date={self.day.isoformat()}&staff={staff2.id}',
                            user=self.agent)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['error'], 'Staff member is not active for bookings')
        # A colleague who simply doesn't do the service keeps the old message
        other = self.create_staff()
        resp = self.api_get(f'{ADMIN_SLOTS_URL}?service={self.service.id}&date={self.day.isoformat()}&staff={other.id}',
                            user=self.agent)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('does not provide', resp.data['error'])

    def test_admin_create_refuses_inactive_staff(self):
        staff2 = self.inactive_colleague()
        resp = self.api_post(ADMIN_BOOKING_URL, {
            'service_id': self.service.id, 'staff_id': staff2.id, 'date': self.day.isoformat(),
            'start_time': '10:00', 'first_name': 'Walk', 'phone_number': '555123456',
        }, user=self.agent)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('not active for bookings', str(resp.data['staff_id']))


# ============================================================================
# 3. check_in follows the confirm payment rule
# ============================================================================

class TestCheckInPaymentRule(FxBase):

    def test_pending_unpaid_card_booking_needs_force(self):
        booking = self.booking(status='pending', payment_method='card')
        url = f'{ADMIN_BOOKING_URL}{booking.id}/check_in/'
        resp = self.api_post(url, {}, user=self.agent)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('force=true', resp.data['error'])
        self.assertEqual(self.api_post(url, {'force': False}, user=self.agent).status_code, 400)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'pending')
        resp = self.api_post(url, {'force': True}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'in_progress')

    def test_paid_card_cash_and_confirmed_bookings_need_no_force(self):
        paid = self.booking(status='pending', payment_method='card', payment_status='deposit_paid')
        cash = self.booking(status='pending', payment_method='cash', start_time=time(12, 0), end_time=time(13, 0))
        confirmed_card = self.booking(status='confirmed', payment_method='card',
                                      start_time=time(14, 0), end_time=time(15, 0))
        for booking in (paid, cash, confirmed_card):
            resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/check_in/', {}, user=self.agent)
            self.assertEqual(resp.status_code, 200, (booking.id, resp.data))


# ============================================================================
# 4. PATCH total_amount
# ============================================================================

class TestPatchTotal(FxBase):

    def test_total_below_paid_is_rejected(self):
        booking = self.booking(status='confirmed', total_amount=Decimal('80.00'),
                               paid_amount=Decimal('50.00'), payment_status='deposit_paid')
        resp = self.api_patch(f'{ADMIN_BOOKING_URL}{booking.id}/', {'total_amount': '40.00'}, user=self.agent)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('total_amount', resp.data)
        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, Decimal('80.00'))
        # record_payment still works afterwards (the review's dead end)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/record_payment/', {'amount': '30'}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['payment_status'], 'fully_paid')

    def test_total_equal_to_paid_marks_fully_paid(self):
        booking = self.booking(status='confirmed', total_amount=Decimal('80.00'),
                               paid_amount=Decimal('50.00'), payment_status='deposit_paid')
        resp = self.api_patch(f'{ADMIN_BOOKING_URL}{booking.id}/', {'total_amount': '50.00'}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['payment_status'], 'fully_paid')
        self.assertEqual(resp.data['remaining_amount'], 0.0)

    def test_raising_total_after_full_payment_leaves_something_owed(self):
        booking = self.booking(status='confirmed', total_amount=Decimal('50.00'),
                               paid_amount=Decimal('50.00'), payment_status='fully_paid')
        resp = self.api_patch(f'{ADMIN_BOOKING_URL}{booking.id}/', {'total_amount': '70.00'}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['payment_status'], 'deposit_paid')
        self.assertEqual(resp.data['remaining_amount'], 20.0)

    def test_unpaid_and_refunded_status_untouched(self):
        unpaid = self.booking(status='confirmed', total_amount=Decimal('80.00'))
        resp = self.api_patch(f'{ADMIN_BOOKING_URL}{unpaid.id}/', {'total_amount': '60.00'}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['payment_status'], 'pending')
        refunded = self.booking(status='cancelled', total_amount=Decimal('80.00'), paid_amount=Decimal('10.00'),
                                payment_status='refunded', start_time=time(12, 0), end_time=time(13, 0))
        resp = self.api_patch(f'{ADMIN_BOOKING_URL}{refunded.id}/', {'total_amount': '90.00'}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['payment_status'], 'refunded')

    def test_notes_only_patch_keeps_status(self):
        booking = self.booking(status='confirmed', total_amount=Decimal('80.00'),
                               paid_amount=Decimal('80.00'), payment_status='fully_paid')
        resp = self.api_patch(f'{ADMIN_BOOKING_URL}{booking.id}/', {'staff_notes': 'hi'}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['payment_status'], 'fully_paid')


# ============================================================================
# 5. Invalid date filters
# ============================================================================

class TestDateFilters(FxBase):

    def test_invalid_dates_are_400_on_list_and_rows(self):
        self.booking()
        for path in ('', 'rows/'):
            for param in ('date_from', 'date_to'):
                resp = self.api_get(f'{ADMIN_BOOKING_URL}{path}?{param}=2026-13-45', user=self.agent)
                self.assertEqual(resp.status_code, 400, (path, param))
                self.assertIn(param, resp.data['error'])
                resp = self.api_get(f'{ADMIN_BOOKING_URL}{path}?{param}=nope&lite=1', user=self.agent)
                self.assertEqual(resp.status_code, 400, (path, param))

    def test_valid_and_empty_dates_still_filter(self):
        booking = self.booking()
        day = self.day.isoformat()
        for path in ('', 'rows/'):
            resp = self.api_get(f'{ADMIN_BOOKING_URL}{path}?date_from={day}&date_to={day}', user=self.agent)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual([b['id'] for b in self.get_results(resp)], [booking.id])
            resp = self.api_get(f'{ADMIN_BOOKING_URL}{path}?date_from=&date_to=', user=self.agent)
            self.assertEqual(resp.status_code, 200)

    def test_detail_ignores_date_params(self):
        booking = self.booking()
        resp = self.api_get(f'{ADMIN_BOOKING_URL}{booking.id}/?date_from=nope', user=self.agent)
        self.assertEqual(resp.status_code, 200)


# ============================================================================
# 6. Staff reminders
# ============================================================================

class TestStaffReminderFixes(FxBase):

    def run_task(self, now):
        from booking_management import tasks
        with patch('booking_management.utils.tenant_now', return_value=now):
            return tasks._send_staff_reminders_for_tenant()

    def test_window_crosses_midnight(self):
        # Overnight hours aren't needed: the reminder task doesn't check them.
        sunday = self.day - timedelta(days=1)
        now = datetime.combine(sunday, time(23, 50))
        after_midnight = self.booking(status='confirmed', date=self.day, start_time=time(0, 10), end_time=time(1, 0))
        too_late = self.booking(status='confirmed', date=self.day, start_time=time(0, 30), end_time=time(1, 30))
        before_midnight = self.booking(status='confirmed', date=sunday, start_time=time(23, 55),
                                       end_time=time(23, 59))
        already_started = self.booking(status='confirmed', date=sunday, start_time=time(23, 40),
                                       end_time=time(23, 59))
        self.assertEqual(self.run_task(now), 2)
        ids = sorted(n.metadata['booking_id'] for n in self.notes(self.staff_user, 'booking_reminder'))
        self.assertEqual(ids, sorted([after_midnight.id, before_midnight.id]))
        titles = {n.metadata['booking_id']: n.title for n in self.notes(self.staff_user, 'booking_reminder')}
        self.assertEqual(titles[after_midnight.id], 'Next client in 20 min')
        for b in (too_late, already_started):
            b.refresh_from_db()
            self.assertFalse(b.staff_reminder_sent)

    def test_same_day_window_unchanged(self):
        now = datetime.combine(self.day, time(9, 40))
        soon = self.booking(status='confirmed')  # 10:00
        self.booking(status='confirmed', start_time=time(10, 15), end_time=time(10, 45))  # beyond 10:10
        self.assertEqual(self.run_task(now), 1)
        self.assertEqual([n.metadata['booking_id'] for n in self.notes(self.staff_user, 'booking_reminder')], [soon.id])

    def test_walk_in_booked_by_the_staff_member_is_not_reminded(self):
        resp = self.api_post(ADMIN_BOOKING_URL, {
            'service_id': self.service.id, 'staff_id': self.staff.id, 'date': self.day.isoformat(),
            'start_time': '10:00', 'first_name': 'Walk', 'phone_number': '555123456',
        }, user=self.staff_user)
        self.assertEqual(resp.status_code, 201, resp.data)
        booking = Booking.objects.get(pk=resp.data['id'])
        self.assertEqual(booking.created_by_id, self.staff_user.id)
        self.assertEqual(self.run_task(datetime.combine(self.day, time(9, 50))), 0)
        self.assertEqual(self.notes(self.staff_user, 'booking_reminder'), [])
        booking.refresh_from_db()
        self.assertTrue(booking.staff_reminder_sent)  # not retried later

    def test_own_booking_made_long_ago_is_reminded(self):
        booking = self.booking(status='confirmed', created_by=self.staff_user)
        Booking.objects.filter(pk=booking.pk).update(created_at=timezone.now() - timedelta(minutes=16))
        self.assertEqual(self.run_task(datetime.combine(self.day, time(9, 50))), 1)

    def test_booking_by_a_colleague_is_reminded(self):
        resp = self.api_post(ADMIN_BOOKING_URL, {
            'service_id': self.service.id, 'staff_id': self.staff.id, 'date': self.day.isoformat(),
            'start_time': '10:00', 'first_name': 'Walk', 'phone_number': '555123456',
        }, user=self.agent)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Booking.objects.get(pk=resp.data['id']).created_by_id, self.agent.id)
        self.assertEqual(self.run_task(datetime.combine(self.day, time(9, 50))), 1)
