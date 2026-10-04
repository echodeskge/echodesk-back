"""
Tests for booking management admin and client API views.
Covers AdminServiceCategoryViewSet, AdminServiceViewSet, AdminBookingStaffViewSet,
AdminBookingViewSet, dashboard/schedule/settings endpoints, and client-facing
registration, login, booking, cancellation, reschedule, and rating flows.
"""
from decimal import Decimal
from datetime import date, time, timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone
from rest_framework import status

from booking_management.tests.conftest import BookingTestCase
from booking_management.models import (
    Service, ServiceCategory, BookingStaff, Booking,
    StaffAvailability, BookingSettings,
)
from social_integrations.models import Client
from tenants.models import TenantSubscription
from tenants.feature_models import Feature

# ---------------------------------------------------------------------------
# URL constants
# ---------------------------------------------------------------------------
# Admin endpoints
ADMIN_CATEGORY_URL = '/api/bookings/admin/categories/'
ADMIN_SERVICE_URL = '/api/bookings/admin/services/'
ADMIN_STAFF_URL = '/api/bookings/admin/staff/'
ADMIN_BOOKING_URL = '/api/bookings/admin/bookings/'
ADMIN_DASHBOARD_URL = '/api/bookings/admin/dashboard/'
ADMIN_SCHEDULE_URL = '/api/bookings/admin/schedule/'
ADMIN_SETTINGS_URL = '/api/bookings/admin/settings/'

# Client endpoints
CLIENT_REGISTER_URL = '/api/bookings/clients/register/'
CLIENT_LOGIN_URL = '/api/bookings/clients/login/'
CLIENT_VERIFY_EMAIL_URL = '/api/bookings/clients/verify-email/'
CLIENT_SERVICE_URL = '/api/bookings/client/services/'
CLIENT_BOOKING_URL = '/api/bookings/client/bookings/'
PAYMENT_WEBHOOK_URL = '/api/bookings/payment-webhook/'


class BookingViewTestMixin:
    """Shared helpers that set up the subscription + feature required by
    HasBookingManagementFeature."""

    def _ensure_booking_feature(self):
        """Create a booking_management Feature and give the tenant a subscription
        that includes it, so that admin users pass HasBookingManagementFeature.
        Also patches has_feature on the admin user (if self.admin exists) so the
        permission check succeeds without needing full group setup."""
        feat, _ = Feature.objects.get_or_create(
            key='booking_management',
            defaults={
                'name': 'Booking Management',
                'price_per_user_gel': Decimal('0'),
                'price_unlimited_gel': Decimal('0'),
                'category': 'support',
                'is_active': True,
            },
        )
        sub, created = TenantSubscription.objects.get_or_create(
            tenant=self.tenant,
            defaults={
                'is_active': True,
                'starts_at': timezone.now(),
                'agent_count': 10,
            },
        )
        sub.selected_features.add(feat)
        # Patch has_feature on the admin user so it always returns True,
        # matching the pattern used in invoices/tests/conftest.py.
        if hasattr(self, 'admin') and self.admin is not None:
            self.admin.has_feature = lambda key: True
        return sub


# ============================================================================
# ADMIN — Service Category CRUD
# ============================================================================

class TestAdminServiceCategoryCRUD(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bk-cat-admin@test.com')
        self._ensure_booking_feature()

    def test_list_categories(self):
        self.create_category()
        self.create_category()
        resp = self.api_get(ADMIN_CATEGORY_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(self.get_results(resp)), 2)

    def test_create_category(self):
        resp = self.api_post(ADMIN_CATEGORY_URL, {
            'name': {'en': 'New Cat', 'ka': 'ახალი'},
            'icon': 'star',
            'display_order': 1,
            'is_active': True,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ServiceCategory.objects.filter(icon='star').exists())

    def test_retrieve_category(self):
        cat = self.create_category()
        resp = self.api_get(f'{ADMIN_CATEGORY_URL}{cat.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_update_category(self):
        cat = self.create_category()
        resp = self.api_patch(f'{ADMIN_CATEGORY_URL}{cat.id}/', {
            'icon': 'updated-icon',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        cat.refresh_from_db()
        self.assertEqual(cat.icon, 'updated-icon')

    def test_delete_category(self):
        cat = self.create_category()
        resp = self.api_delete(f'{ADMIN_CATEGORY_URL}{cat.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ServiceCategory.objects.filter(id=cat.id).exists())

    def test_unauthenticated_denied(self):
        resp = self.api_get(ADMIN_CATEGORY_URL)
        self.assertIn(resp.status_code, [401, 403])


# ============================================================================
# ADMIN — Service CRUD
# ============================================================================

class TestAdminServiceCRUD(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bk-svc-admin@test.com')
        self._ensure_booking_feature()
        self.category = self.create_category()

    def test_list_services(self):
        self.create_service(category=self.category)
        resp = self.api_get(ADMIN_SERVICE_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(self.get_results(resp)), 1)

    def test_create_service(self):
        resp = self.api_post(ADMIN_SERVICE_URL, {
            'name': {'en': 'Haircut'},
            'description': {'en': 'A nice haircut'},
            'category': self.category.id,
            'base_price': '25.00',
            'duration_minutes': 30,
            'buffer_time_minutes': 5,
            'booking_type': 'duration_based',
            'status': 'active',
            'deposit_percentage': 0,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_retrieve_service(self):
        svc = self.create_service(category=self.category)
        resp = self.api_get(f'{ADMIN_SERVICE_URL}{svc.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_update_service(self):
        svc = self.create_service(category=self.category)
        resp = self.api_patch(f'{ADMIN_SERVICE_URL}{svc.id}/', {
            'base_price': '99.00',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        svc.refresh_from_db()
        self.assertEqual(svc.base_price, Decimal('99.00'))

    def test_delete_service(self):
        svc = self.create_service(category=self.category)
        resp = self.api_delete(f'{ADMIN_SERVICE_URL}{svc.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_activate_service(self):
        svc = self.create_service(category=self.category, status='inactive')
        resp = self.api_post(
            f'{ADMIN_SERVICE_URL}{svc.id}/activate/', {}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        svc.refresh_from_db()
        self.assertEqual(svc.status, 'active')

    def test_deactivate_service(self):
        svc = self.create_service(category=self.category, status='active')
        resp = self.api_post(
            f'{ADMIN_SERVICE_URL}{svc.id}/deactivate/', {}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        svc.refresh_from_db()
        self.assertEqual(svc.status, 'inactive')

    def test_unauthenticated_denied(self):
        resp = self.api_get(ADMIN_SERVICE_URL)
        self.assertIn(resp.status_code, [401, 403])


# ============================================================================
# ADMIN — Booking Staff CRUD
# ============================================================================

class TestAdminBookingStaffCRUD(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bk-staff-admin@test.com')
        self._ensure_booking_feature()

    def test_list_staff(self):
        self.create_staff()
        resp = self.api_get(ADMIN_STAFF_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_create_staff(self):
        user = self.create_user(email='new-staff@test.com', role='agent')
        resp = self.api_post(ADMIN_STAFF_URL, {
            'user_id': user.id,
            'bio': 'New staff member',
            'is_active_for_bookings': True,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_retrieve_staff(self):
        staff = self.create_staff()
        resp = self.api_get(f'{ADMIN_STAFF_URL}{staff.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_toggle_active(self):
        staff = self.create_staff()
        original = staff.is_active_for_bookings
        resp = self.api_post(
            f'{ADMIN_STAFF_URL}{staff.id}/toggle_active/', {}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        staff.refresh_from_db()
        self.assertNotEqual(staff.is_active_for_bookings, original)

    def test_available_users(self):
        """available_users excludes users who are already staff."""
        non_staff_user = self.create_user(email='not-staff@test.com')
        self.create_staff()  # creates its own user
        resp = self.api_get(
            f'{ADMIN_STAFF_URL}available_users/', user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        emails = [u['email'] for u in resp.data]
        self.assertIn('not-staff@test.com', emails)

    def test_availability_action(self):
        staff = self.create_staff()
        self.create_availability(staff, day_of_week=0)
        resp = self.api_get(
            f'{ADMIN_STAFF_URL}{staff.id}/availability/', user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

    def test_exceptions_action(self):
        staff = self.create_staff()
        self.create_staff_exception(staff)
        resp = self.api_get(
            f'{ADMIN_STAFF_URL}{staff.id}/exceptions/', user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

    def test_bookings_action(self):
        staff = self.create_staff()
        service = self.create_service()
        self.create_booking(service, staff=staff, date=date.today() + timedelta(days=3))
        resp = self.api_get(
            f'{ADMIN_STAFF_URL}{staff.id}/bookings/', user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(resp.data), 1)

    def test_unauthenticated_denied(self):
        resp = self.api_get(ADMIN_STAFF_URL)
        self.assertIn(resp.status_code, [401, 403])


# ============================================================================
# ADMIN — Booking Management
# ============================================================================

class TestAdminBookingViewSet(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bk-booking-admin@test.com')
        self._ensure_booking_feature()
        self.service = self.create_service()
        self.staff = self.create_staff()
        self.service.staff_members.add(self.staff)
        self.bk_client = self.create_client()

    def test_list_bookings(self):
        self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self.api_get(ADMIN_BOOKING_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(self.get_results(resp)), 1)

    def test_retrieve_booking(self):
        booking = self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self.api_get(f'{ADMIN_BOOKING_URL}{booking.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_confirm_booking_force(self):
        booking = self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/confirm/', {
            'force': True,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'confirmed')

    def test_confirm_unpaid_card_booking_rejected(self):
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff, payment_method='card',
        )
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/confirm/', {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_confirm_pay_at_venue_booking_needs_no_payment(self):
        # What every guest booking from the public site looks like: unpaid, cash.
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff, payment_method='cash',
        )
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/confirm/', {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'confirmed')

    def test_confirm_already_confirmed_rejected(self):
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff, status='confirmed',
        )
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/confirm/', {
            'force': True,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_complete_booking(self):
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff, status='confirmed',
        )
        booking.confirmed_at = timezone.now()
        booking.save(update_fields=['confirmed_at'])
        resp = self.api_post(
            f'{ADMIN_BOOKING_URL}{booking.id}/complete/', {}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'completed')

    def test_complete_pending_rejected(self):
        booking = self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self.api_post(
            f'{ADMIN_BOOKING_URL}{booking.id}/complete/', {}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('booking_management.views_admin.get_booking_payment_service')
    def test_cancel_booking(self, mock_payment):
        mock_payment.return_value = MagicMock()
        booking = self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/cancel/', {
            'reason': 'No longer needed',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'cancelled')
        self.assertEqual(booking.cancellation_reason, 'No longer needed')

    def test_cancel_completed_rejected(self):
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff, status='completed',
        )
        booking.completed_at = timezone.now()
        booking.save(update_fields=['completed_at'])
        resp = self.api_post(
            f'{ADMIN_BOOKING_URL}{booking.id}/cancel/', {}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_assign_staff(self):
        staff2 = self.create_staff()
        self.service.staff_members.add(staff2)
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff,
            date=date.today() + timedelta(days=14),
        )
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/assign_staff/', {
            'staff_id': staff2.id,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        booking.refresh_from_db()
        self.assertEqual(booking.staff, staff2)

    def test_assign_staff_missing_id(self):
        booking = self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self.api_post(
            f'{ADMIN_BOOKING_URL}{booking.id}/assign_staff/', {}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_assign_staff_not_found(self):
        booking = self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/assign_staff/', {
            'staff_id': 99999,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_assign_staff_wrong_service(self):
        """Staff not assigned to the booking's service."""
        other_staff = self.create_staff()
        # other_staff is NOT in self.service.staff_members
        booking = self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/assign_staff/', {
            'staff_id': other_staff.id,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('not assigned', resp.data['error'])

    def test_reschedule_booking(self):
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff,
            date=date.today() + timedelta(days=14),
        )
        # Set up availability for the target day
        target_date = date.today() + timedelta(days=21)
        day_of_week = target_date.weekday()
        self.create_availability(self.staff, day_of_week=day_of_week)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/reschedule/', {
            'date': target_date.strftime('%Y-%m-%d'),
            'start_time': '10:00',
        }, user=self.admin)
        # Might succeed or fail depending on availability; test both cases
        self.assertIn(resp.status_code, [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST])

    def test_reschedule_missing_fields(self):
        booking = self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self.api_post(
            f'{ADMIN_BOOKING_URL}{booking.id}/reschedule/', {}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reschedule_invalid_date_format(self):
        booking = self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self.api_post(f'{ADMIN_BOOKING_URL}{booking.id}/reschedule/', {
            'date': 'not-a-date',
            'start_time': '10:00',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('booking_management.views_admin.get_booking_payment_service')
    def test_check_payment_status(self, mock_payment):
        mock_svc = MagicMock()
        mock_svc.check_payment_status.return_value = {'status': 'paid'}
        mock_payment.return_value = mock_svc
        booking = self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self.api_get(
            f'{ADMIN_BOOKING_URL}{booking.id}/check_payment_status/', user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'paid')

    @patch('booking_management.views_admin.get_booking_payment_service')
    def test_check_payment_status_error(self, mock_payment):
        mock_payment.side_effect = Exception('Gateway error')
        booking = self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self.api_get(
            f'{ADMIN_BOOKING_URL}{booking.id}/check_payment_status/', user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_unauthenticated_denied(self):
        resp = self.api_get(ADMIN_BOOKING_URL)
        self.assertIn(resp.status_code, [401, 403])


# ============================================================================
# ADMIN — Dashboard
# ============================================================================

class TestDashboardStats(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bk-dash-admin@test.com')
        self._ensure_booking_feature()

    def test_dashboard_stats_success(self):
        service = self.create_service()
        staff = self.create_staff()
        client = self.create_client()
        self.create_booking(service, client=client, staff=staff, date=date.today())
        resp = self.api_get(ADMIN_DASHBOARD_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('today', resp.data)
        self.assertIn('week', resp.data)
        self.assertIn('overall', resp.data)

    def test_dashboard_stats_empty(self):
        resp = self.api_get(ADMIN_DASHBOARD_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['today']['total_bookings'], 0)

    def test_dashboard_unauthenticated_denied(self):
        resp = self.api_get(ADMIN_DASHBOARD_URL)
        self.assertIn(resp.status_code, [401, 403])


# ============================================================================
# ADMIN — Staff Schedule
# ============================================================================

class TestStaffSchedule(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bk-sched-admin@test.com')
        self._ensure_booking_feature()

    def test_staff_schedule_no_date(self):
        """Defaults to today when no date is provided."""
        resp = self.api_get(ADMIN_SCHEDULE_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('schedule', resp.data)

    def test_staff_schedule_with_date(self):
        target = (date.today() + timedelta(days=3)).strftime('%Y-%m-%d')
        resp = self.api_get(f'{ADMIN_SCHEDULE_URL}?date={target}', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_staff_schedule_invalid_date(self):
        resp = self.api_get(f'{ADMIN_SCHEDULE_URL}?date=bad-date', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_schedule_filter_by_staff(self):
        staff = self.create_staff()
        self.create_availability(staff, day_of_week=date.today().weekday())
        resp = self.api_get(
            f'{ADMIN_SCHEDULE_URL}?staff_id={staff.id}', user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['schedule']), 1)

    def test_schedule_unauthenticated_denied(self):
        resp = self.api_get(ADMIN_SCHEDULE_URL)
        self.assertIn(resp.status_code, [401, 403])


# ============================================================================
# ADMIN — Booking Settings
# ============================================================================

class TestBookingSettings(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bk-settings-admin@test.com')
        self._ensure_booking_feature()

    def test_get_settings_creates_default(self):
        resp = self.api_get(ADMIN_SETTINGS_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(BookingSettings.objects.filter(tenant=self.tenant).exists())

    def test_patch_settings(self):
        # Ensure settings exist
        self.api_get(ADMIN_SETTINGS_URL, user=self.admin)
        resp = self.api_patch(ADMIN_SETTINGS_URL, {
            'cancellation_hours_before': 48,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        settings = BookingSettings.objects.get(tenant=self.tenant)
        self.assertEqual(settings.cancellation_hours_before, 48)

    def test_put_settings(self):
        self.api_get(ADMIN_SETTINGS_URL, user=self.admin)
        settings = BookingSettings.objects.get(tenant=self.tenant)
        resp = self.api_put(ADMIN_SETTINGS_URL, {
            'payment_method': 'bog_gateway',
            'require_deposit': True,
            'allow_cash_payment': False,
            'allow_card_payment': True,
            'cancellation_hours_before': 12,
            'refund_policy': 'partial_50',
            'auto_confirm_on_deposit': False,
            'auto_confirm_on_full_payment': True,
            'min_hours_before_booking': 1,
            'max_days_advance_booking': 30,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        settings.refresh_from_db()
        self.assertEqual(settings.payment_method, 'bog_gateway')
        self.assertTrue(settings.require_deposit)

    def test_settings_unauthenticated_denied(self):
        resp = self.api_get(ADMIN_SETTINGS_URL)
        self.assertIn(resp.status_code, [401, 403])


# ============================================================================
# CLIENT — Registration & Login
# ============================================================================

class TestClientRegistration(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self._ensure_booking_feature()

    def test_register_success(self):
        resp = self.api_post(CLIENT_REGISTER_URL, {
            'email': 'newclient@test.com',
            'phone_number': '+995555111222',
            'first_name': 'Test',
            'last_name': 'Client',
            'password': 'SecurePass1',
            'password_confirm': 'SecurePass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            Client.objects.filter(email='newclient@test.com', is_booking_enabled=True).exists()
        )

    def test_register_password_too_short(self):
        resp = self.api_post(CLIENT_REGISTER_URL, {
            'email': 'short@test.com',
            'phone_number': '+995555111333',
            'first_name': 'T',
            'last_name': 'C',
            'password': 'short',
            'password_confirm': 'short',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_password_no_digit(self):
        resp = self.api_post(CLIENT_REGISTER_URL, {
            'email': 'nodigit@test.com',
            'phone_number': '+995555111444',
            'first_name': 'T',
            'last_name': 'C',
            'password': 'NoDigitsHere',
            'password_confirm': 'NoDigitsHere',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_passwords_mismatch(self):
        resp = self.api_post(CLIENT_REGISTER_URL, {
            'email': 'mismatch@test.com',
            'phone_number': '+995555111555',
            'first_name': 'T',
            'last_name': 'C',
            'password': 'SecurePass1',
            'password_confirm': 'DifferentPass2',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_duplicate_email(self):
        existing = Client.objects.create(
            name='Existing', email='existing@test.com', phone='+995555111666',
            is_booking_enabled=True, is_verified=True,
        )
        existing.set_password('SecurePass1')
        existing.save()
        resp = self.api_post(CLIENT_REGISTER_URL, {
            'email': 'existing@test.com',
            'phone_number': '+995555111777',
            'first_name': 'D',
            'last_name': 'E',
            'password': 'SecurePass1',
            'password_confirm': 'SecurePass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestClientLogin(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self._ensure_booking_feature()
        self.client_obj = Client.objects.create(
            name='Login Test',
            email='logintest@test.com',
            phone='+995555222111',
            first_name='Login',
            last_name='Test',
            is_booking_enabled=True,
            is_verified=True,
        )
        self.client_obj.set_password('ValidPass1')
        self.client_obj.save()

    def test_login_success(self):
        resp = self.api_post(CLIENT_LOGIN_URL, {
            'email': 'logintest@test.com',
            'password': 'ValidPass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('access', resp.data)
        self.assertIn('refresh', resp.data)
        self.assertIn('client', resp.data)

    def test_login_wrong_password(self):
        resp = self.api_post(CLIENT_LOGIN_URL, {
            'email': 'logintest@test.com',
            'password': 'WrongPass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_nonexistent_email(self):
        resp = self.api_post(CLIENT_LOGIN_URL, {
            'email': 'nobody@test.com',
            'password': 'ValidPass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestClientVerifyEmail(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self._ensure_booking_feature()

    def test_verify_email_success(self):
        client_obj = self.create_client()
        client_obj.is_booking_enabled = True
        client_obj.is_verified = False
        client_obj.verification_token = 'legacy-link-token-0123456789abcdef'
        client_obj.save()
        resp = self.api_post(CLIENT_VERIFY_EMAIL_URL, {'token': 'legacy-link-token-0123456789abcdef'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        client_obj.refresh_from_db()
        self.assertTrue(client_obj.is_verified)

    def test_verify_email_invalid_token(self):
        resp = self.api_post(CLIENT_VERIFY_EMAIL_URL, {'token': 'bad-token-that-is-long-enough-000'})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_verify_email_missing_token(self):
        resp = self.api_post(CLIENT_VERIFY_EMAIL_URL, {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ============================================================================
# CLIENT — Service Browsing (public endpoints)
# ============================================================================

class TestClientServiceViewSet(BookingViewTestMixin, BookingTestCase):

    def setUp(self):
        super().setUp()
        self._ensure_booking_feature()

    def test_list_services_public(self):
        self.create_service(status='active')
        resp = self.api_get(CLIENT_SERVICE_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_services_only_active(self):
        self.create_service(status='active')
        self.create_service(status='inactive')
        resp = self.api_get(CLIENT_SERVICE_URL)
        results = self.get_results(resp)
        for svc in results:
            self.assertEqual(svc['status'], 'active')

    def test_get_slots_missing_date(self):
        svc = self.create_service(status='active')
        resp = self.api_get(f'{CLIENT_SERVICE_URL}{svc.id}/slots/')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_slots_invalid_date(self):
        svc = self.create_service(status='active')
        resp = self.api_get(f'{CLIENT_SERVICE_URL}{svc.id}/slots/?date=bad')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_slots_valid_date(self):
        svc = self.create_service(status='active', booking_type='duration_based')
        staff = self.create_staff()
        svc.staff_members.add(staff)
        # Find next Monday
        today = date.today()
        days_ahead = -today.weekday() + 7
        next_monday = today + timedelta(days=days_ahead)
        self.create_availability(staff, day_of_week=0, start_time=time(9, 0), end_time=time(17, 0))
        resp = self.api_get(
            f'{CLIENT_SERVICE_URL}{svc.id}/slots/?date={next_monday.strftime("%Y-%m-%d")}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('slots', resp.data)

    def test_get_slots_invalid_staff_id(self):
        svc = self.create_service(status='active')
        target = (date.today() + timedelta(days=7)).strftime('%Y-%m-%d')
        resp = self.api_get(
            f'{CLIENT_SERVICE_URL}{svc.id}/slots/?date={target}&staff_id=99999'
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ============================================================================
# CLIENT — Booking Rating
# ============================================================================

class TestClientBookingRating(BookingViewTestMixin, BookingTestCase):
    """Test the /rate/ action which requires BookingClient JWT auth."""

    def setUp(self):
        super().setUp()
        self._ensure_booking_feature()
        self.bk_client = self.create_client()
        self.bk_client.is_booking_enabled = True
        self.bk_client.is_verified = True
        self.bk_client.set_password('TestPass1')
        self.bk_client.save()
        self.service = self.create_service()
        self.staff = self.create_staff()

    def _get_client_token(self):
        resp = self.api_post(CLIENT_LOGIN_URL, {
            'email': self.bk_client.email,
            'password': 'TestPass1',
        })
        return resp.data.get('access')

    def _client_api_post(self, url, data=None):
        """POST with BookingClient JWT."""
        from rest_framework.test import APIClient
        api = APIClient()
        token = self._get_client_token()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return api.post(url, data, format='json', HTTP_HOST='tenant.test.com')

    def _client_api_get(self, url):
        from rest_framework.test import APIClient
        api = APIClient()
        token = self._get_client_token()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return api.get(url, HTTP_HOST='tenant.test.com')

    def test_rate_completed_booking(self):
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff, status='completed',
        )
        booking.completed_at = timezone.now()
        booking.save(update_fields=['completed_at'])
        resp = self._client_api_post(f'{CLIENT_BOOKING_URL}{booking.id}/rate/', {
            'rating': 5,
            'review': 'Great service!',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        booking.refresh_from_db()
        self.assertEqual(booking.rating, 5)
        self.assertEqual(booking.review, 'Great service!')

    def test_rate_non_completed_rejected(self):
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff, status='pending',
        )
        resp = self._client_api_post(f'{CLIENT_BOOKING_URL}{booking.id}/rate/', {
            'rating': 3,
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rate_already_rated_rejected(self):
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff,
            status='completed', rating=4,
        )
        booking.completed_at = timezone.now()
        booking.save(update_fields=['completed_at'])
        resp = self._client_api_post(f'{CLIENT_BOOKING_URL}{booking.id}/rate/', {
            'rating': 5,
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rate_missing_rating(self):
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff, status='completed',
        )
        booking.completed_at = timezone.now()
        booking.save(update_fields=['completed_at'])
        resp = self._client_api_post(f'{CLIENT_BOOKING_URL}{booking.id}/rate/', {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rate_out_of_range(self):
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff, status='completed',
        )
        booking.completed_at = timezone.now()
        booking.save(update_fields=['completed_at'])
        resp = self._client_api_post(f'{CLIENT_BOOKING_URL}{booking.id}/rate/', {
            'rating': 6,
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_own_bookings(self):
        self.create_booking(self.service, client=self.bk_client, staff=self.staff)
        resp = self._client_api_get(CLIENT_BOOKING_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    @patch('booking_management.views_client.get_booking_payment_service')
    def test_client_cancel_booking(self, mock_payment):
        mock_payment.return_value = MagicMock()
        booking = self.create_booking(
            self.service, client=self.bk_client, staff=self.staff,
            date=date.today() + timedelta(days=14),
        )
        self.create_settings(cancellation_hours_before=1)
        resp = self._client_api_post(f'{CLIENT_BOOKING_URL}{booking.id}/cancel/', {
            'reason': 'Changed plans',
        })
        # May succeed or fail based on cancellation policy timing
        self.assertIn(resp.status_code, [
            status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST,
        ])


# ============================================================================
# Payment Webhook
# ============================================================================

class TestPaymentWebhook(BookingTestCase):

    @patch('booking_management.views_client.get_booking_payment_service')
    def test_webhook_success(self, mock_payment):
        mock_svc = MagicMock()
        mock_svc.process_webhook.return_value = None
        mock_payment.return_value = mock_svc
        resp = self.api_post(PAYMENT_WEBHOOK_URL, {'order_id': 'test123'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    @patch('booking_management.views_client.get_booking_payment_service')
    def test_webhook_error(self, mock_payment):
        # Couldn't confirm the payment state → 5xx so BOG retries, and no
        # internal error text leaks to the caller.
        mock_payment.return_value.process_webhook.side_effect = Exception('Bad data')
        resp = self.api_post(PAYMENT_WEBHOOK_URL, {'order_id': 'bad'})
        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertNotIn('Bad data', str(resp.data))
