"""
Shared test infrastructure for booking management tests.
Extends EchoDeskTenantTestCase with booking-specific helpers.
"""
from decimal import Decimal
from datetime import date, time, timedelta

from django.utils import timezone

from users.tests.conftest import EchoDeskTenantTestCase
from booking_management.models import (
    Service,
    ServiceCategory,
    BookingStaff,
    Booking,
    StaffAvailability,
    StaffException,
    BookingSettings,
    RecurringBooking,
)
from social_integrations.models import Client


class BookingTestCase(EchoDeskTenantTestCase):
    """
    Booking-specific test case with factory helpers for all booking models.
    """

    # Counters to generate unique emails/names across factory calls.
    _staff_counter = 0
    _client_counter = 0
    _category_counter = 0
    _service_counter = 0

    def setUp(self):
        super().setUp()
        # Throttle counters live in the cache and would leak between tests.
        from django.core.cache import cache
        cache.clear()

    @staticmethod
    def get_results(resp):
        """Extract results from a paginated or non-paginated response."""
        if isinstance(resp.data, dict) and 'results' in resp.data:
            return resp.data['results']
        return resp.data

    # ── Category factory ──

    def create_category(self, **kwargs):
        BookingTestCase._category_counter += 1
        n = BookingTestCase._category_counter
        defaults = {
            'name': {'en': f'Category {n}', 'ka': f'კატეგორია {n}'},
            'description': {'en': f'Description {n}'},
            'icon': 'scissors',
            'display_order': n,
            'is_active': True,
        }
        defaults.update(kwargs)
        return ServiceCategory.objects.create(**defaults)

    # ── Service factory ──

    def create_service(self, category=None, **kwargs):
        BookingTestCase._service_counter += 1
        n = BookingTestCase._service_counter
        if category is None:
            category = self.create_category()
        defaults = {
            'name': {'en': f'Service {n}', 'ka': f'სერვისი {n}'},
            'description': {'en': f'Service description {n}'},
            'category': category,
            'base_price': Decimal('50.00'),
            'deposit_percentage': 20,
            'duration_minutes': 60,
            'buffer_time_minutes': 10,
            'booking_type': 'duration_based',
            'status': 'active',
        }
        defaults.update(kwargs)
        # Pop staff_members if provided separately (M2M requires post-create)
        staff_members = defaults.pop('staff_members', None)
        service = Service.objects.create(**defaults)
        if staff_members is not None:
            service.staff_members.set(staff_members)
        return service

    # ── Staff factory ──

    def create_staff(self, user=None, **kwargs):
        BookingTestCase._staff_counter += 1
        n = BookingTestCase._staff_counter
        if user is None:
            user = self.create_user(
                email=f'staff{n}@test.com',
                role='agent',
                first_name=f'Staff',
                last_name=f'Member{n}',
            )
        defaults = {
            'user': user,
            'bio': f'Staff bio {n}',
            'is_active_for_bookings': True,
        }
        defaults.update(kwargs)
        return BookingStaff.objects.create(**defaults)

    # ── Availability factory ──

    def create_availability(self, staff, day_of_week=0, **kwargs):
        defaults = {
            'staff': staff,
            'day_of_week': day_of_week,
            'start_time': time(9, 0),
            'end_time': time(17, 0),
            'is_available': True,
            'break_start': None,
            'break_end': None,
        }
        defaults.update(kwargs)
        return StaffAvailability.objects.create(**defaults)

    # ── Staff exception factory ──

    def create_staff_exception(self, staff, exception_date=None, **kwargs):
        if exception_date is None:
            exception_date = date.today() + timedelta(days=1)
        defaults = {
            'staff': staff,
            'date': exception_date,
            'is_available': False,
            'reason': 'Test exception',
        }
        defaults.update(kwargs)
        return StaffException.objects.create(**defaults)

    # ── Client factory ──

    def create_client(self, **kwargs):
        BookingTestCase._client_counter += 1
        n = BookingTestCase._client_counter
        defaults = {
            'name': f'Client {n}',
            'email': f'client{n}@test.com',
            'phone': f'+99555000{n:04d}',
            'first_name': f'First{n}',
            'last_name': f'Last{n}',
            'is_booking_enabled': True,
        }
        defaults.update(kwargs)
        return Client.objects.create(**defaults)

    # ── Booking factory ──

    def create_booking(self, service, client=None, staff=None, **kwargs):
        if client is None:
            client = self.create_client()
        defaults = {
            'client': client,
            'service': service,
            'staff': staff,
            'date': date.today() + timedelta(days=7),
            'start_time': time(10, 0),
            'end_time': time(11, 0),
            'status': 'pending',
            'payment_status': 'pending',
            'total_amount': service.base_price,
            'deposit_amount': service.calculate_deposit_amount(),
            'paid_amount': Decimal('0.00'),
        }
        defaults.update(kwargs)
        return Booking.objects.create(**defaults)

    # ── Recurring booking factory ──

    def create_recurring_booking(self, service, client=None, staff=None, **kwargs):
        if client is None:
            client = self.create_client()
        defaults = {
            'client': client,
            'service': service,
            'staff': staff,
            'frequency': 'weekly',
            'preferred_day_of_week': 0,
            'preferred_time': time(10, 0),
            'status': 'active',
            'next_booking_date': date.today() + timedelta(days=7),
            'current_occurrences': 0,
        }
        defaults.update(kwargs)
        return RecurringBooking.objects.create(**defaults)

    # ── Settings factory ──

    def create_settings(self, **kwargs):
        defaults = {
            'tenant': self.tenant,
            'payment_method': 'manual_transfer',
            'require_deposit': False,
            'allow_cash_payment': True,
            'allow_card_payment': True,
            'cancellation_hours_before': 24,
            'refund_policy': 'full',
            'auto_confirm_on_deposit': True,
            'auto_confirm_on_full_payment': True,
            'min_hours_before_booking': 2,
            'max_days_advance_booking': 60,
        }
        defaults.update(kwargs)
        return BookingSettings.objects.create(**defaults)
