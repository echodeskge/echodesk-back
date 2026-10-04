"""
Tests for the customer-facing booking API behind book.echodesk.ge:
- feature gate and public-page switch
- slots: per-slot end time, lead time on the business's own clock
- guest booking: real staff assignment, no data echo, manage link, cancel
- accounts: register → emailed code → verify → book; password reset
- payment options, BOG payment creation, webhook re-verification, auto-cancel
"""
import re
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from booking_management.models import Booking
from booking_management.tests.conftest import BookingTestCase
from booking_management.tests.test_views import BookingViewTestMixin
from booking_management.utils import (
    available_payment_options, calculate_refund_amount, generate_available_slots, tenant_now,
)
from social_integrations.models import Client

INFO_URL = '/api/bookings/client/info/'
SERVICES_URL = '/api/bookings/client/services/'
GUEST_URL = '/api/bookings/client/guest-bookings/'
MANAGE_URL = '/api/bookings/client/manage/'
BOOKINGS_URL = '/api/bookings/client/bookings/'
REGISTER_URL = '/api/bookings/clients/register/'
LOGIN_URL = '/api/bookings/clients/login/'
VERIFY_URL = '/api/bookings/clients/verify-email/'
REFRESH_URL = '/api/bookings/clients/token/refresh/'
RESET_REQUEST_URL = '/api/bookings/clients/password-reset/request/'
RESET_CONFIRM_URL = '/api/bookings/clients/password-reset/confirm/'
WEBHOOK_URL = '/api/bookings/payment-webhook/'


class PublicBookingTestCase(BookingViewTestMixin, BookingTestCase):
    """A salon with one 60-minute service and two staff working 09:00–17:00 every day."""

    enable_feature = True

    def setUp(self):
        super().setUp()
        cache.clear()  # throttle counters live in the cache
        if self.enable_feature:
            self._ensure_booking_feature()
        self.service = self.create_service(duration_minutes=60, buffer_time_minutes=0, deposit_percentage=20)
        self.staff_a = self.create_staff()
        self.staff_b = self.create_staff()
        self.service.staff_members.set([self.staff_a, self.staff_b])
        for staff in (self.staff_a, self.staff_b):
            for day in range(7):
                self.create_availability(staff, day_of_week=day)
        self.day = date.today() + timedelta(days=7)

    def guest_payload(self, **overrides):
        payload = {
            'service_id': self.service.id,
            'date': self.day.strftime('%Y-%m-%d'),
            'start_time': '10:00',
            'first_name': 'Nino',
            'last_name': 'Guest',
            'phone_number': '+995 555 10 20 30',
            'email': 'nino.guest@test.com',
        }
        payload.update(overrides)
        return payload

    def card_settings(self, **kwargs):
        settings = self.create_settings(**kwargs)
        settings.bog_client_id = 'tenant-bog-id'
        settings.bog_client_secret = 'tenant-bog-secret'
        settings.save()
        return settings

    def client_api(self, access):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        return api


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

class TestFeatureGate(PublicBookingTestCase):
    enable_feature = False

    def test_tenant_without_feature_gets_404(self):
        self.assertEqual(self.api_get(INFO_URL).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.api_get(SERVICES_URL).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.api_post(GUEST_URL, self.guest_payload()).status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(Booking.objects.exists())


class TestPublicInfo(PublicBookingTestCase):

    def test_info_returns_business_and_rules(self):
        self.create_settings(public_address='1 Rustaveli Ave', public_phone='+995322000000',
                             public_description={'en': 'Hair salon', 'ka': 'სალონი'})
        resp = self.api_get(INFO_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['schema_name'], self.tenant.schema_name)
        self.assertEqual(resp.data['address'], '1 Rustaveli Ave')
        self.assertEqual(resp.data['description']['ka'], 'სალონი')
        self.assertEqual(resp.data['timezone'], 'Asia/Tbilisi')
        self.assertEqual(resp.data['payment_options'], ['cash'])
        self.assertFalse(resp.data['card_payment_enabled'])

    def test_public_page_switched_off_gets_404(self):
        self.create_settings(public_page_enabled=False)
        self.assertEqual(self.api_get(INFO_URL).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.api_post(GUEST_URL, self.guest_payload()).status_code, status.HTTP_404_NOT_FOUND)

    def test_services_do_not_expose_staff_email(self):
        resp = self.api_get(SERVICES_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        staff = resp.data[0]['staff_members']
        self.assertEqual(len(staff), 2)
        self.assertNotIn('email', staff[0]['user'])
        self.assertNotIn(self.staff_a.user.email, str(resp.data))


# ---------------------------------------------------------------------------
# Slots and clock
# ---------------------------------------------------------------------------

class TestSlots(PublicBookingTestCase):

    def test_each_slot_has_its_own_end_time_and_lists_free_staff(self):
        slots = generate_available_slots(self.service, self.day)
        by_start = {s['start_time']: s for s in slots}
        self.assertEqual(by_start['09:00']['end_time'], '10:00')
        self.assertEqual(by_start['15:30']['end_time'], '16:30')
        self.assertEqual(len(by_start['10:00']['available_staff']), 2)
        # 16:30 + 60 min would run past 17:00
        self.assertNotIn('16:30', by_start)

    def test_slot_with_one_staff_booked_still_offered_with_the_other(self):
        self.create_booking(self.service, staff=self.staff_a, date=self.day,
                            start_time=time(10, 0), end_time=time(11, 0))
        by_start = {s['start_time']: s for s in generate_available_slots(self.service, self.day)}
        self.assertEqual([s['staff_id'] for s in by_start['10:00']['available_staff']], [self.staff_b.id])

    def test_lead_time_hides_slots_that_are_too_soon(self):
        noon = datetime.combine(self.day, time(12, 0))
        with patch('booking_management.utils.tenant_now', return_value=noon):
            starts = [s['start_time'] for s in generate_available_slots(self.service, self.day)]
        # default lead time is 2 hours → first bookable slot is 14:00
        self.assertEqual(starts[0], '14:00')

    def test_staff_not_assigned_to_service_gets_no_slots(self):
        outsider = self.create_staff()
        self.create_availability(outsider, day_of_week=self.day.weekday())
        self.assertEqual(generate_available_slots(self.service, self.day, outsider), [])
        resp = self.api_get(f'{SERVICES_URL}{self.service.id}/slots/?date={self.day}&staff_id={outsider.id}')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_business_clock_is_tbilisi_not_utc(self):
        late_utc = datetime(2026, 1, 1, 21, 30, tzinfo=dt_timezone.utc)
        with patch('booking_management.utils.timezone.now', return_value=late_utc):
            self.assertEqual(tenant_now(), datetime(2026, 1, 2, 1, 30))


# ---------------------------------------------------------------------------
# Guest booking
# ---------------------------------------------------------------------------

class TestGuestBooking(PublicBookingTestCase):

    def book(self, **overrides):
        with patch('booking_management.tasks.send_booking_email_task.delay') as delay:
            resp = self.api_post(GUEST_URL, self.guest_payload(**overrides))
        return resp, delay

    def test_guest_booking_assigns_real_staff_until_none_left(self):
        first, delay = self.book()
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
        delay.assert_called_once()
        second, _ = self.book(phone_number='+995555000002', email='')
        self.assertEqual(second.status_code, status.HTTP_201_CREATED, second.data)
        third, _ = self.book(phone_number='+995555000003', email='')
        self.assertEqual(third.status_code, status.HTTP_400_BAD_REQUEST)

        staff_ids = set(Booking.objects.values_list('staff_id', flat=True))
        self.assertEqual(staff_ids, {self.staff_a.id, self.staff_b.id})
        self.assertEqual(Booking.objects.count(), 2)

    def test_guest_response_has_manage_token_and_no_client_data(self):
        resp, _ = self.book()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertNotIn('client', resp.data)
        self.assertTrue(resp.data['manage_token'])
        booking = Booking.objects.get(manage_token=resp.data['manage_token'])
        self.assertEqual(booking.payment_method, 'cash')
        self.assertEqual(booking.status, 'pending')
        self.assertEqual(booking.client.phone, '+995 555 10 20 30')
        self.assertEqual(booking.end_time, time(11, 0))

    def test_existing_contact_matched_by_phone_is_not_modified(self):
        existing = Client.objects.create(
            name='Real Person', first_name='Real', last_name='Person',
            phone='+995555999888', email='real@person.ge',
        )
        resp, _ = self.book(phone_number='+995555999888', first_name='Someone', last_name='Else',
                            email='attacker@test.com')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        existing.refresh_from_db()
        self.assertEqual(existing.name, 'Real Person')
        self.assertEqual(existing.email, 'real@person.ge')
        self.assertNotIn('real@person.ge', str(resp.data))
        booking = Booking.objects.get(manage_token=resp.data['manage_token'])
        self.assertEqual(booking.client_id, existing.id)
        self.assertIn('Someone Else', booking.staff_notes)

    def test_specific_staff_double_booking_rejected(self):
        ok, _ = self.book(staff_id=self.staff_a.id)
        self.assertEqual(ok.status_code, status.HTTP_201_CREATED, ok.data)
        clash, _ = self.book(staff_id=self.staff_a.id, phone_number='+995555000009')
        self.assertEqual(clash.status_code, status.HTTP_400_BAD_REQUEST)

    def test_inactive_service_and_foreign_staff_rejected(self):
        self.service.status = 'inactive'
        self.service.save(update_fields=['status'])
        resp, _ = self.book()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        self.service.status = 'active'
        self.service.save(update_fields=['status'])
        outsider = self.create_staff()
        resp, _ = self.book(staff_id=outsider.id)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_too_soon_is_rejected(self):
        noon = datetime.combine(self.day, time(12, 0))
        with patch('booking_management.utils.tenant_now', return_value=noon):
            resp, _ = self.book(start_time='13:00')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_manage_link_shows_and_cancels_booking(self):
        resp, _ = self.book()
        token = resp.data['manage_token']

        shown = self.api_get(f'{MANAGE_URL}{token}/')
        self.assertEqual(shown.status_code, status.HTTP_200_OK)
        self.assertTrue(shown.data['can_cancel'])
        self.assertNotIn('client', shown.data)

        cancelled = self.api_post(f'{MANAGE_URL}{token}/cancel/', {'reason': 'Plans changed'})
        self.assertEqual(cancelled.status_code, status.HTTP_200_OK, cancelled.data)
        self.assertEqual(Booking.objects.get(manage_token=token).status, 'cancelled')

        self.assertEqual(self.api_get(f'{MANAGE_URL}wrong-token/').status_code, status.HTTP_404_NOT_FOUND)

    def test_cancel_refused_inside_notice_period(self):
        resp, _ = self.book()
        token = resp.data['manage_token']
        almost = datetime.combine(self.day, time(9, 0))  # one hour before
        with patch('booking_management.utils.tenant_now', return_value=almost):
            shown = self.api_get(f'{MANAGE_URL}{token}/')
            refused = self.api_post(f'{MANAGE_URL}{token}/cancel/', {})
        self.assertFalse(shown.data['can_cancel'])
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

class TestAccounts(PublicBookingTestCase):

    def register(self, email='anna@test.com'):
        return self.api_post(REGISTER_URL, {
            'email': email, 'phone_number': '+995555123123',
            'first_name': 'Anna', 'last_name': 'Account',
            'password': 'SecurePass1', 'password_confirm': 'SecurePass1',
        })

    def emailed_code(self):
        match = re.search(r'\b(\d{6})\b', mail.outbox[-1].body)
        self.assertIsNotNone(match, mail.outbox[-1].body)
        return match.group(1)

    def verified_access(self, email='anna@test.com'):
        self.assertEqual(self.register(email).status_code, status.HTTP_201_CREATED)
        resp = self.api_post(VERIFY_URL, {'email': email, 'code': self.emailed_code()})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return resp.data

    def test_register_sends_code_and_login_waits_for_verification(self):
        self.assertEqual(self.register().status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['anna@test.com'])

        blocked = self.api_post(LOGIN_URL, {'email': 'anna@test.com', 'password': 'SecurePass1'})
        self.assertEqual(blocked.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('code', blocked.data)

        wrong = self.api_post(VERIFY_URL, {'email': 'anna@test.com', 'code': '000000'})
        if self.emailed_code() != '000000':
            self.assertEqual(wrong.status_code, status.HTTP_400_BAD_REQUEST)

        verified = self.api_post(VERIFY_URL, {'email': 'anna@test.com', 'code': self.emailed_code()})
        self.assertEqual(verified.status_code, status.HTTP_200_OK, verified.data)
        self.assertIn('access', verified.data)

        login = self.api_post(LOGIN_URL, {'email': 'anna@test.com', 'password': 'SecurePass1'})
        self.assertEqual(login.status_code, status.HTTP_200_OK)

    def test_expired_code_is_refused(self):
        self.register()
        code = self.emailed_code()
        client = Client.objects.get(email='anna@test.com')
        client.verification_sent_at = timezone.now() - timedelta(hours=2)
        client.save(update_fields=['verification_sent_at'])
        resp = self.api_post(VERIFY_URL, {'email': 'anna@test.com', 'code': code})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_registering_over_a_guest_contact_does_not_verify_or_rename_it(self):
        guest = Client.objects.create(name='Guest Name', first_name='Guest', last_name='Name',
                                      phone='+995555777666', email='anna@test.com', is_booking_enabled=True)
        self.assertEqual(self.register().status_code, status.HTTP_201_CREATED)
        guest.refresh_from_db()
        self.assertEqual(guest.first_name, 'Guest')
        self.assertFalse(guest.is_verified)
        self.assertEqual(Client.objects.filter(email='anna@test.com').count(), 1)

    def test_account_can_book_list_and_refresh_token(self):
        session = self.verified_access()
        api = self.client_api(session['access'])
        with patch('booking_management.tasks.send_booking_email_task.delay'):
            created = api.post(BOOKINGS_URL, {
                'service_id': self.service.id, 'date': self.day.strftime('%Y-%m-%d'), 'start_time': '11:00',
            }, format='json', HTTP_HOST='tenant.test.com')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertIsNotNone(Booking.objects.get(id=created.data['id']).staff_id)

        listed = api.get(BOOKINGS_URL, HTTP_HOST='tenant.test.com')
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(len(self.get_results(listed)), 1)
        # the list says whether each booking can still be changed online
        self.assertTrue(self.get_results(listed)[0]['can_cancel'])

        # Public endpoints must not choke on a customer token
        self.assertEqual(api.get(SERVICES_URL, HTTP_HOST='tenant.test.com').status_code, status.HTTP_200_OK)
        profile = api.get('/api/bookings/clients/profile/', HTTP_HOST='tenant.test.com')
        self.assertEqual(profile.status_code, status.HTTP_200_OK)
        self.assertEqual(profile.data['email'], 'anna@test.com')

        refreshed = self.api_post(REFRESH_URL, {'refresh': session['refresh']})
        self.assertEqual(refreshed.status_code, status.HTTP_200_OK)
        self.assertIn('access', refreshed.data)

    def test_client_cannot_edit_or_delete_booking_fields(self):
        session = self.verified_access()
        api = self.client_api(session['access'])
        client = Client.objects.get(email='anna@test.com')
        booking = self.create_booking(self.service, client=client, staff=self.staff_a, date=self.day)
        url = f'{BOOKINGS_URL}{booking.id}/'
        patched = api.patch(url, {'status': 'completed', 'paid_amount': '999'}, format='json', HTTP_HOST='tenant.test.com')
        deleted = api.delete(url, HTTP_HOST='tenant.test.com')
        self.assertEqual(patched.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(deleted.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'pending')
        self.assertEqual(booking.paid_amount, Decimal('0.00'))

    def test_reschedule_into_own_overlapping_slot_is_allowed(self):
        session = self.verified_access()
        api = self.client_api(session['access'])
        client = Client.objects.get(email='anna@test.com')
        booking = self.create_booking(self.service, client=client, staff=self.staff_a, date=self.day,
                                      start_time=time(10, 0), end_time=time(11, 0))
        resp = api.post(f'{BOOKINGS_URL}{booking.id}/reschedule/', {
            'date': self.day.strftime('%Y-%m-%d'), 'start_time': '10:30',
        }, format='json', HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        booking.refresh_from_db()
        self.assertEqual((booking.start_time, booking.end_time), (time(10, 30), time(11, 30)))

    def test_password_reset_with_emailed_code(self):
        self.verified_access()
        mail.outbox.clear()

        unknown = self.api_post(RESET_REQUEST_URL, {'email': 'nobody@test.com'})
        known = self.api_post(RESET_REQUEST_URL, {'email': 'anna@test.com'})
        self.assertEqual(unknown.data, known.data)  # doesn't reveal which emails exist
        self.assertEqual(len(mail.outbox), 1)

        code = self.emailed_code()
        done = self.api_post(RESET_CONFIRM_URL, {'email': 'anna@test.com', 'code': code, 'new_password': 'BrandNew22'})
        self.assertEqual(done.status_code, status.HTTP_200_OK, done.data)
        login = self.api_post(LOGIN_URL, {'email': 'anna@test.com', 'password': 'BrandNew22'})
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        reused = self.api_post(RESET_CONFIRM_URL, {'email': 'anna@test.com', 'code': code, 'new_password': 'Another33'})
        self.assertEqual(reused.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------

BOG_CREATED = {'order_id': 'bog-order-1', 'payment_id': 'bog-order-1',
               'payment_url': 'https://payment.bog.ge/?order_id=bog-order-1',
               'amount': 50.0, 'currency': 'GEL', 'status': 'pending', 'metadata': None,
               'details_url': 'https://api.bog.ge/payments/v1/receipt/bog-order-1'}


class TestPayment(PublicBookingTestCase):

    def test_payment_options_follow_settings(self):
        self.assertEqual(available_payment_options(None, self.service), ['cash'])
        manual = self.create_settings()
        self.assertEqual(available_payment_options(manual, self.service), ['cash'])
        manual.delete()

        card = self.card_settings()
        self.assertEqual(available_payment_options(card, self.service), ['cash', 'deposit', 'full'])
        card.require_deposit = True
        self.assertEqual(available_payment_options(card, self.service), ['deposit', 'full'])
        card.require_deposit = False
        card.allow_cash_payment = False
        self.assertEqual(available_payment_options(card, self.service), ['deposit', 'full'])

    def test_card_payment_refused_when_not_offered(self):
        self.create_settings()  # manual transfer, no BOG credentials
        resp = self.api_post(GUEST_URL, self.guest_payload(payment_type='full'))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('payment_type', resp.data)
        self.assertFalse(Booking.objects.exists())

    def test_card_booking_creates_bog_payment_with_tenant_credentials(self):
        self.card_settings()
        seen = {}

        def fake_create(service_self, **kwargs):
            seen['client_id'] = service_self.client_id
            seen['client_secret'] = service_self.client_secret
            seen.update(kwargs)
            return dict(BOG_CREATED)

        with patch('tenants.bog_payment.BOGPaymentService.create_payment', autospec=True, side_effect=fake_create):
            resp = self.api_post(GUEST_URL, self.guest_payload(payment_type='deposit'))

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['payment_url'], BOG_CREATED['payment_url'])
        self.assertEqual(seen['client_id'], 'tenant-bog-id')
        self.assertEqual(seen['client_secret'], 'tenant-bog-secret')
        self.assertEqual(seen['amount'], 10.0)  # 20% deposit of 50.00
        token = resp.data['manage_token']
        self.assertTrue(seen['return_url_success'].endswith(f'/{self.tenant.schema_name}/booking/{token}?paid=1'))
        self.assertTrue(seen['return_url_fail'].endswith(f'/booking/{token}?paid=0'))
        self.assertTrue(seen['callback_url'].endswith('/api/bookings/payment-webhook/'))

        booking = Booking.objects.get(manage_token=token)
        self.assertEqual(booking.payment_method, 'card')
        self.assertEqual(booking.bog_order_id, 'bog-order-1')
        self.assertEqual(booking.deposit_amount, Decimal('10.00'))

    def test_payment_init_failure_cancels_booking_and_reports_error(self):
        self.card_settings()
        with patch('tenants.bog_payment.BOGPaymentService.create_payment', side_effect=ValueError('BOG down')):
            resp = self.api_post(GUEST_URL, self.guest_payload(payment_type='full'))
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertNotIn('BOG down', str(resp.data))
        self.assertEqual(Booking.objects.get().status, 'cancelled')
        # the slot is free again
        self.assertEqual(len({s['start_time']: s for s in generate_available_slots(self.service, self.day)}
                             ['10:00']['available_staff']), 2)

    def _card_booking(self, **kwargs):
        defaults = dict(staff=self.staff_a, date=self.day, payment_method='card',
                        bog_order_id='bog-order-1', deposit_amount=Decimal('50.00'))
        defaults.update(kwargs)
        return self.create_booking(self.service, **defaults)

    def test_webhook_marks_paid_only_after_bog_confirms(self):
        self.card_settings()
        booking = self._card_booking()
        body = {'body': {'external_order_id': booking.booking_number,
                         'order_status': {'key': 'completed'}, 'response_code': '100', 'amount': 5000}}

        # A forged "completed" callback while BOG says the order is still open
        with patch('tenants.bog_payment.BOGPaymentService.check_payment_status',
                   return_value={'status': 'pending', 'bog_status': 'created'}):
            forged = self.api_post(WEBHOOK_URL, body)
        self.assertEqual(forged.status_code, status.HTTP_200_OK)
        booking.refresh_from_db()
        self.assertEqual((booking.payment_status, booking.status, booking.paid_amount),
                         ('pending', 'pending', Decimal('0.00')))

        paid = {'status': 'paid', 'bog_status': 'completed', 'amount': '50.00',
                'transaction_id': 'tx1', 'response_code': '100'}
        with patch('tenants.bog_payment.BOGPaymentService.check_payment_status', return_value=paid) as check:
            self.assertEqual(self.api_post(WEBHOOK_URL, body).status_code, status.HTTP_200_OK)
            self.assertEqual(self.api_post(WEBHOOK_URL, body).status_code, status.HTTP_200_OK)
            self.assertEqual(check.call_count, 1)  # the repeat callback is a no-op
        booking.refresh_from_db()
        self.assertEqual(booking.payment_status, 'fully_paid')
        self.assertEqual(booking.paid_amount, Decimal('50.00'))
        self.assertEqual(booking.status, 'confirmed')

    def test_deposit_payment_is_recorded_as_deposit(self):
        self.card_settings()
        booking = self._card_booking(deposit_amount=Decimal('10.00'))
        paid = {'status': 'paid', 'bog_status': 'completed', 'amount': '10.00'}
        with patch('tenants.bog_payment.BOGPaymentService.check_payment_status', return_value=paid):
            self.api_post(WEBHOOK_URL, {'body': {'external_order_id': booking.booking_number}})
        booking.refresh_from_db()
        self.assertEqual(booking.payment_status, 'deposit_paid')
        self.assertEqual(booking.remaining_amount, Decimal('40.00'))

    def test_auto_cancel_only_touches_abandoned_card_payments(self):
        from booking_management.tasks import _cancel_unpaid_for_tenant
        old = timezone.now() - timedelta(days=2)

        cash = self.create_booking(self.service, staff=self.staff_a, date=self.day, payment_method='cash')
        legacy = self.create_booking(self.service, staff=self.staff_b, date=self.day)
        card = self._card_booking(bog_order_id=None, start_time=time(13, 0), end_time=time(14, 0))
        fresh_card = self._card_booking(bog_order_id=None, start_time=time(15, 0), end_time=time(16, 0))
        Booking.objects.filter(id__in=[cash.id, legacy.id, card.id]).update(created_at=old)

        self.assertEqual(_cancel_unpaid_for_tenant(self.tenant.schema_name), 1)
        statuses = {b.id: b.status for b in Booking.objects.all()}
        self.assertEqual(statuses[card.id], 'cancelled')
        self.assertEqual(statuses[cash.id], 'pending')
        self.assertEqual(statuses[legacy.id], 'pending')
        self.assertEqual(statuses[fresh_card.id], 'pending')

    def test_auto_cancel_keeps_booking_bog_says_was_paid(self):
        from booking_management.tasks import _cancel_unpaid_for_tenant
        self.card_settings()
        card = self._card_booking()
        Booking.objects.filter(id=card.id).update(created_at=timezone.now() - timedelta(hours=3))
        paid = {'status': 'paid', 'bog_status': 'completed', 'amount': '50.00'}
        with patch('tenants.bog_payment.BOGPaymentService.check_payment_status', return_value=paid):
            self.assertEqual(_cancel_unpaid_for_tenant(self.tenant.schema_name), 0)
        card.refresh_from_db()
        self.assertEqual((card.status, card.payment_status), ('confirmed', 'fully_paid'))

    def test_cancel_refunds_by_policy_and_survives_refund_failure(self):
        settings = self.card_settings(refund_policy='partial_50')
        booking = self._card_booking(paid_amount=Decimal('50.00'), payment_status='fully_paid',
                                     status='confirmed', manage_token='tok-refund')
        self.assertEqual(calculate_refund_amount(booking, settings), Decimal('25.00'))

        with patch('tenants.bog_payment.BOGPaymentService.refund_payment', side_effect=ValueError('refused')):
            resp = self.api_post(f'{MANAGE_URL}tok-refund/cancel/', {})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'cancelled')
        self.assertEqual(booking.payment_metadata['refund']['status'], 'manual_required')
        self.assertEqual(booking.payment_metadata['refund']['amount'], '25.00')

    def test_cancel_refund_success_updates_amounts(self):
        self.card_settings(refund_policy='full')
        booking = self._card_booking(paid_amount=Decimal('50.00'), payment_status='fully_paid',
                                     status='confirmed', manage_token='tok-refund-ok')
        with patch('tenants.bog_payment.BOGPaymentService.refund_payment',
                   return_value={'key': 'request_received'}) as refund:
            resp = self.api_post(f'{MANAGE_URL}tok-refund-ok/cancel/', {})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        refund.assert_called_once_with(order_id='bog-order-1', amount=None)  # full refund
        booking.refresh_from_db()
        self.assertEqual((booking.payment_status, booking.paid_amount), ('refunded', Decimal('0.00')))


# ---------------------------------------------------------------------------
# Audit fixes
# ---------------------------------------------------------------------------

class TestAuditFixes(PublicBookingTestCase):

    def register(self, email='anna@test.com', password='SecurePass1'):
        return self.api_post(REGISTER_URL, {
            'email': email, 'phone_number': '+995555123123',
            'first_name': 'Anna', 'last_name': 'Account',
            'password': password, 'password_confirm': password,
        })

    def emailed_code(self):
        return re.search(r'\b(\d{6})\b', mail.outbox[-1].body).group(1)

    def guest(self, **overrides):
        with patch('booking_management.tasks.send_booking_email_task.delay') as delay:
            resp = self.api_post(GUEST_URL, self.guest_payload(**overrides))
        return resp, delay

    # -- privacy / accounts ---------------------------------------------------

    def test_register_reveals_and_changes_nothing_about_an_existing_contact(self):
        contact = Client.objects.create(name='Real Person', first_name='Real', last_name='Person',
                                        phone='+995555999888', email='anna@test.com')
        resp = self.register()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertNotIn('client', resp.data)
        self.assertNotIn('+995555999888', str(resp.data))
        contact.refresh_from_db()
        self.assertEqual((contact.first_name, contact.last_name, contact.phone), ('Real', 'Person', '+995555999888'))

    def test_squatting_an_email_does_not_leave_the_squatters_password(self):
        self.register(password='AttackerPw1')           # never verified
        self.assertEqual(self.register(password='OwnerPass22').status_code, status.HTTP_201_CREATED)
        verified = self.api_post(VERIFY_URL, {'email': 'anna@test.com', 'code': self.emailed_code()})
        self.assertEqual(verified.status_code, status.HTTP_200_OK, verified.data)
        attacker = self.api_post(LOGIN_URL, {'email': 'anna@test.com', 'password': 'AttackerPw1'})
        owner = self.api_post(LOGIN_URL, {'email': 'anna@test.com', 'password': 'OwnerPass22'})
        self.assertEqual(attacker.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(attacker.data['code'][0], 'invalid_credentials')
        self.assertEqual(owner.status_code, status.HTTP_200_OK)

    def test_code_is_discarded_after_five_wrong_guesses(self):
        self.register()
        right = self.emailed_code()
        wrong = '000000' if right != '000000' else '111111'
        for _ in range(5):
            resp = self.api_post(VERIFY_URL, {'email': 'anna@test.com', 'code': wrong})
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(resp.data['code'], 'invalid_code')
        # even the right code no longer works; a new one must be requested
        self.assertEqual(self.api_post(VERIFY_URL, {'email': 'anna@test.com', 'code': right}).status_code,
                         status.HTTP_400_BAD_REQUEST)

    def test_verify_answers_the_same_for_verified_and_unknown_accounts(self):
        self.register()
        self.api_post(VERIFY_URL, {'email': 'anna@test.com', 'code': self.emailed_code()})
        again = self.api_post(VERIFY_URL, {'email': 'anna@test.com', 'code': '123456'})
        unknown = self.api_post(VERIFY_URL, {'email': 'nobody@test.com', 'code': '123456'})
        self.assertEqual((again.status_code, again.data), (unknown.status_code, unknown.data))

    def test_tokens_from_another_tenant_or_the_shop_are_rejected(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        client = Client.objects.create(name='V', email='v@test.com', is_booking_enabled=True, is_verified=True)
        client.set_password('SecurePass1')
        client.save()

        def access(**claims):
            token = RefreshToken()
            token['client_id'] = client.id
            for key, value in claims.items():
                token[key] = value
            return str(token.access_token), str(token)

        for claims in ({}, {'kind': 'booking', 'tenant': 'othersalon'}, {'tenant': self.tenant.schema_name}):
            token, refresh = access(**claims)
            listed = self.client_api(token).get(BOOKINGS_URL, HTTP_HOST='tenant.test.com')
            self.assertEqual(listed.status_code, status.HTTP_401_UNAUTHORIZED, claims)
            self.assertEqual(self.api_post(REFRESH_URL, {'refresh': refresh}).status_code,
                             status.HTTP_401_UNAUTHORIZED, claims)

        good, _ = access(kind='booking', tenant=self.tenant.schema_name)
        self.assertEqual(self.client_api(good).get(BOOKINGS_URL, HTTP_HOST='tenant.test.com').status_code,
                         status.HTTP_200_OK)

    def test_customer_recurring_bookings_are_not_exposed(self):
        resp = self.api_get('/api/bookings/client/recurring-bookings/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    # -- rules ----------------------------------------------------------------

    def test_only_offered_times_can_be_booked(self):
        resp, _ = self.guest(start_time='10:07')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'][0], 'slot_unavailable')
        self.assertFalse(Booking.objects.exists())

    def test_errors_carry_codes_for_translation(self):
        noon = datetime.combine(self.day, time(12, 0))
        with patch('booking_management.utils.tenant_now', return_value=noon):
            soon, _ = self.guest(start_time='13:00')
        self.assertEqual(soon.data['code'][0], 'outside_booking_window')
        self.guest(staff_id=self.staff_a.id)
        clash, _ = self.guest(staff_id=self.staff_a.id, phone_number='+995555000009')
        self.assertEqual(clash.data['code'][0], 'slot_unavailable')

    def test_duplicate_day_off_entries_do_not_break_slots(self):
        for _ in range(2):
            self.create_staff_exception(self.staff_a, exception_date=self.day)
        resp = self.api_get(f'{SERVICES_URL}{self.service.id}/slots/?date={self.day}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(all(len(s['available_staff']) == 1 for s in resp.data['slots']))

    def test_one_customer_cannot_fill_the_calendar(self):
        for hour in (9, 10, 11, 12, 13):
            resp, _ = self.guest(start_time=f'{hour:02d}:00')
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        sixth, _ = self.guest(start_time='14:00')
        self.assertEqual(sixth.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(sixth.data['code'], 'booking_limit')

    def test_names_fall_back_to_the_language_that_exists(self):
        self.service.name = {'ka': 'თმის შეჭრა'}
        self.service.save(update_fields=['name'])
        english = self.api_get(f'{SERVICES_URL}?lang=en')
        self.assertEqual(english.data[0]['name_display'], 'თმის შეჭრა')

    def test_staff_without_a_name_never_shows_their_email(self):
        user = self.staff_a.user
        user.first_name = user.last_name = ''
        user.save()
        services = self.api_get(SERVICES_URL)
        slots = self.api_get(f'{SERVICES_URL}{self.service.id}/slots/?date={self.day}&staff_id={self.staff_a.id}')
        self.assertNotIn(user.email, str(services.data))
        self.assertNotIn(user.email, str(slots.data))

    # -- notices --------------------------------------------------------------

    def test_guest_notice_goes_to_the_email_typed_on_the_booking(self):
        Client.objects.create(name='Returning', phone='+995555999888', email='old@address.ge')
        resp, delay = self.guest(phone_number='+995555999888', email='new@address.ge', lang='ka')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        booking = Booking.objects.get(manage_token=resp.data['manage_token'])
        self.assertEqual((booking.contact_email, booking.contact_language), ('new@address.ge', 'ka'))
        delay.assert_called_once()

        from booking_management.emails import send_booking_email
        self.assertTrue(send_booking_email(booking, 'created', self.tenant.schema_name, None))
        self.assertEqual(mail.outbox[-1].to, ['new@address.ge'])
        self.assertIn('მიღებულია', mail.outbox[-1].subject)

    # -- payment --------------------------------------------------------------

    def _card_booking(self, **kwargs):
        defaults = dict(staff=self.staff_a, date=self.day, payment_method='card',
                        bog_order_id='bog-order-9', deposit_amount=Decimal('50.00'))
        defaults.update(kwargs)
        return self.create_booking(self.service, **defaults)

    def test_declined_card_frees_the_slot_immediately(self):
        self.card_settings()
        booking = self._card_booking()
        with patch('tenants.bog_payment.BOGPaymentService.check_payment_status',
                   return_value={'status': 'failed', 'bog_status': 'rejected'}):
            self.api_post(WEBHOOK_URL, {'body': {'external_order_id': booking.booking_number}})
        booking.refresh_from_db()
        self.assertEqual((booking.status, booking.payment_status), ('cancelled', 'failed'))

    def test_sweeper_does_not_cancel_when_the_bank_cannot_be_asked(self):
        from booking_management.tasks import _cancel_unpaid_for_tenant
        self.card_settings()
        booking = self._card_booking()
        Booking.objects.filter(id=booking.id).update(created_at=timezone.now() - timedelta(hours=3))
        for state in ('error', 'unknown', 'processing'):
            with patch('tenants.bog_payment.BOGPaymentService.check_payment_status',
                       return_value={'status': state}):
                self.assertEqual(_cancel_unpaid_for_tenant(self.tenant.schema_name), 0, state)
        with patch('tenants.bog_payment.BOGPaymentService.check_payment_status',
                   return_value={'status': 'pending'}):
            self.assertEqual(_cancel_unpaid_for_tenant(self.tenant.schema_name), 1)

    def test_payment_arriving_after_cancellation_is_refunded(self):
        self.card_settings()
        booking = self._card_booking(status='cancelled')
        paid = {'status': 'paid', 'bog_status': 'completed', 'amount': '50.00'}
        with patch('tenants.bog_payment.BOGPaymentService.check_payment_status', return_value=paid), \
                patch('tenants.bog_payment.BOGPaymentService.refund_payment',
                      return_value={'key': 'request_received'}) as refund:
            self.api_post(WEBHOOK_URL, {'body': {'external_order_id': booking.booking_number}})
        refund.assert_called_once_with(order_id='bog-order-9', amount=None)
        booking.refresh_from_db()
        self.assertEqual((booking.status, booking.payment_status, booking.paid_amount),
                         ('cancelled', 'refunded', Decimal('0.00')))

    def test_second_cancel_does_not_refund_again(self):
        self.card_settings(refund_policy='partial_50')
        booking = self._card_booking(paid_amount=Decimal('50.00'), payment_status='fully_paid',
                                     status='confirmed', manage_token='tok-twice')
        with patch('tenants.bog_payment.BOGPaymentService.refund_payment',
                   return_value={'key': 'request_received'}) as refund:
            first = self.api_post(f'{MANAGE_URL}tok-twice/cancel/', {})
            second = self.api_post(f'{MANAGE_URL}tok-twice/cancel/', {})
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(refund.call_count, 1)

    def test_saving_settings_with_blank_credentials_keeps_the_stored_ones(self):
        from booking_management.serializers import BookingSettingsSerializer
        settings = self.card_settings()
        serializer = BookingSettingsSerializer(settings, data={'bog_client_id': '', 'bog_client_secret': '',
                                                               'public_address': 'New address'}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        saved = serializer.save()
        saved.refresh_from_db()
        self.assertEqual((saved.bog_client_id, saved.bog_client_secret), ('tenant-bog-id', 'tenant-bog-secret'))
        self.assertEqual(saved.public_address, 'New address')


class TestStaffNotifications(PublicBookingTestCase):

    def test_staff_are_told_about_online_bookings_and_cancellations(self):
        from users.models import Notification
        owner = self.create_admin(email='salon-owner@test.com')
        with patch('booking_management.tasks.send_booking_email_task.delay'):
            resp = self.api_post(GUEST_URL, self.guest_payload(staff_id=self.staff_a.id))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

        created = Notification.objects.filter(notification_type='booking_created')
        notified = set(created.values_list('user_id', flat=True))
        self.assertIn(owner.id, notified)
        self.assertIn(self.staff_a.user_id, notified)
        self.assertNotIn(self.staff_b.user_id, notified)
        note = created.get(user=owner)
        self.assertIn('Nino Guest', note.message)
        self.assertTrue(note.link_url.startswith('/bookings/bookings/'))

        self.api_post(f"{MANAGE_URL}{resp.data['manage_token']}/cancel/", {})
        self.assertTrue(Notification.objects.filter(user=owner, notification_type='booking_cancelled').exists())
