"""GET /api/bookings/admin/calendar/ — the range feed for the calendar views.

Shape, inclusive range limit, staff filter, exceptions limited to the range,
constant query count, and the feature gate.
"""
from datetime import date, time, timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from booking_management.tests.test_admin_hardening import HardeningBase
from crm.tests.conftest import assertAppQueries

CALENDAR_URL = '/api/bookings/admin/calendar/'


def _app_query_count(ctx):
    return len([
        q['sql'] for q in ctx.captured_queries
        if not q['sql'].lstrip().upper().startswith(('SET ', 'SHOW ', 'SAVEPOINT', 'RELEASE SAVEPOINT'))
    ])


class CalendarBase(HardeningBase):

    def calendar(self, user=None, **params):
        query = '&'.join(f'{k}={v}' for k, v in params.items())
        return self.api_get(f'{CALENDAR_URL}?{query}', user=user or self.agent)

    def week(self):
        """Monday (self.day) .. Sunday."""
        return {'date_from': self.day.isoformat(), 'date_to': (self.day + timedelta(days=6)).isoformat()}


class TestCalendarShape(CalendarBase):

    def test_response_contract(self):
        avail = self.staff.availability.get(day_of_week=0)
        avail.break_start, avail.break_end = time(13, 0), time(14, 0)
        avail.save()
        exc = self.create_staff_exception(self.staff, exception_date=self.day + timedelta(days=2), reason='Dentist')
        client = self.create_client(first_name='Nino', last_name='K', phone='+995599000111', email='nino@test.com')
        booking = self.booking(client=client, status='confirmed', staff_notes='VIP', client_notes='Window seat',
                               payment_status='deposit_paid', paid_amount=Decimal('20.00'))

        resp = self.calendar(**self.week())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(set(resp.data.keys()), {'date_from', 'date_to', 'timezone', 'staff', 'bookings'})
        self.assertEqual(resp.data['date_from'], self.day.isoformat())
        self.assertEqual(resp.data['date_to'], (self.day + timedelta(days=6)).isoformat())
        self.assertEqual(resp.data['timezone'], 'Asia/Tbilisi')

        # Staff roster with weekly hours and the exceptions of the range
        staff = next(s for s in resp.data['staff'] if s['id'] == self.staff.id)
        self.assertEqual(
            set(staff.keys()),
            {'id', 'user_id', 'full_name', 'profile_image', 'is_active_for_bookings', 'availability', 'exceptions'},
        )
        self.assertEqual(staff['user_id'], self.staff_user.id)
        self.assertEqual(staff['full_name'], 'Sam Staff')
        self.assertTrue(staff['is_active_for_bookings'])
        self.assertEqual(len(staff['availability']), 1)
        monday = staff['availability'][0]
        self.assertEqual(
            set(monday.keys()),
            {'id', 'day_of_week', 'start_time', 'end_time', 'is_available', 'break_start', 'break_end'},
        )
        self.assertEqual(monday['day_of_week'], 0)
        self.assertEqual(monday['start_time'], '09:00:00')
        self.assertEqual(monday['break_start'], '13:00:00')
        self.assertEqual(len(staff['exceptions']), 1)
        self.assertEqual(
            set(staff['exceptions'][0].keys()), {'id', 'date', 'start_time', 'end_time', 'is_available', 'reason'},
        )
        self.assertEqual(staff['exceptions'][0]['id'], exc.id)
        self.assertEqual(staff['exceptions'][0]['reason'], 'Dentist')
        self.assertFalse(staff['exceptions'][0]['is_available'])

        # Booking rows: BookingRow fields + staff_id + staff_notes, buffer on the service
        self.assertEqual(len(resp.data['bookings']), 1)
        row = resp.data['bookings'][0]
        self.assertEqual(set(row.keys()), {
            'id', 'booking_number', 'client', 'service', 'staff_id', 'date', 'start_time', 'end_time',
            'status', 'payment_status', 'payment_method', 'total_amount', 'deposit_amount', 'paid_amount',
            'client_notes', 'staff_notes', 'created_at',
        })
        self.assertEqual(row['id'], booking.id)
        self.assertEqual(row['staff_id'], self.staff.id)
        self.assertEqual(row['status'], 'confirmed')
        self.assertEqual(row['payment_status'], 'deposit_paid')
        self.assertEqual(Decimal(row['total_amount']), Decimal('80.00'))
        self.assertEqual(Decimal(row['paid_amount']), Decimal('20.00'))
        self.assertEqual(row['staff_notes'], 'VIP')
        self.assertEqual(row['client_notes'], 'Window seat')
        self.assertEqual(row['client'], {
            'id': client.id, 'full_name': client.full_name, 'phone_number': '+995599000111', 'email': 'nino@test.com',
        })
        self.assertEqual(set(row['service'].keys()), {'id', 'name_display', 'duration_minutes', 'buffer_time_minutes'})
        self.assertEqual(row['service']['id'], self.service.id)
        self.assertEqual(row['service']['buffer_time_minutes'], 0)

    def test_every_status_included_and_ordered(self):
        later = self.booking(status='cancelled', date=self.day + timedelta(days=1), start_time=time(9, 0), end_time=time(10, 0))
        no_show = self.booking(status='no_show', start_time=time(15, 0), end_time=time(16, 0))
        first = self.booking(status='completed', start_time=time(9, 0), end_time=time(10, 0))
        unassigned = self.booking(staff=None, status='pending', start_time=time(12, 0), end_time=time(13, 0))
        resp = self.calendar(**self.week())
        self.assertEqual(
            [b['id'] for b in resp.data['bookings']], [first.id, unassigned.id, no_show.id, later.id],
        )
        self.assertIsNone(resp.data['bookings'][1]['staff_id'])

    def test_range_is_inclusive(self):
        inside_first = self.booking(start_time=time(9, 0), end_time=time(10, 0))
        inside_last = self.booking(date=self.day + timedelta(days=6), start_time=time(9, 0), end_time=time(10, 0))
        self.booking(date=self.day - timedelta(days=1), start_time=time(9, 0), end_time=time(10, 0))
        self.booking(date=self.day + timedelta(days=7), start_time=time(9, 0), end_time=time(10, 0))
        resp = self.calendar(**self.week())
        self.assertEqual([b['id'] for b in resp.data['bookings']], [inside_first.id, inside_last.id])

    def test_lang_localises_service_name(self):
        self.service.name = {'en': 'Haircut', 'ka': 'თმის შეჭრა'}
        self.service.save()
        self.booking()
        resp = self.calendar(lang='ka', **self.week())
        self.assertEqual(resp.data['bookings'][0]['service']['name_display'], 'თმის შეჭრა')
        resp = self.calendar(**self.week())
        self.assertEqual(resp.data['bookings'][0]['service']['name_display'], 'Haircut')

    def test_timezone_from_settings(self):
        self.create_settings(timezone='Europe/Berlin')
        resp = self.calendar(**self.week())
        self.assertEqual(resp.data['timezone'], 'Europe/Berlin')

    def test_inactive_staff_listed_with_flag(self):
        inactive = self.create_staff(is_active_for_bookings=False)
        resp = self.calendar(**self.week())
        row = next(s for s in resp.data['staff'] if s['id'] == inactive.id)
        self.assertFalse(row['is_active_for_bookings'])
        self.assertEqual(row['availability'], [])


class TestCalendarRangeLimit(CalendarBase):

    def test_sixty_two_days_ok_sixty_three_refused(self):
        start = date(2026, 3, 1)
        resp = self.calendar(date_from=start.isoformat(), date_to=(start + timedelta(days=61)).isoformat())
        self.assertEqual(resp.status_code, 200, resp.data)
        resp = self.calendar(date_from=start.isoformat(), date_to=(start + timedelta(days=62)).isoformat())
        self.assertEqual(resp.status_code, 400)
        self.assertIn('62', resp.data['error'])

    def test_single_day_ok(self):
        resp = self.calendar(date_from=self.day.isoformat(), date_to=self.day.isoformat())
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_reversed_missing_or_bad_dates(self):
        resp = self.calendar(date_from=(self.day + timedelta(days=1)).isoformat(), date_to=self.day.isoformat())
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.calendar(date_from=self.day.isoformat()).status_code, 400)
        self.assertEqual(self.calendar(date_to=self.day.isoformat()).status_code, 400)
        self.assertEqual(self.calendar().status_code, 400)
        self.assertEqual(self.calendar(date_from='01-03-2026', date_to='2026-03-07').status_code, 400)
        self.assertEqual(self.calendar(date_from='2026-03-01', date_to='next week').status_code, 400)


class TestCalendarStaffFilter(CalendarBase):

    def setUp(self):
        super().setUp()
        self.other = self.create_staff()
        self.service.staff_members.add(self.other)
        self.create_availability(self.other, day_of_week=1)
        self.mine = self.booking(start_time=time(9, 0), end_time=time(10, 0))
        self.theirs = self.booking(staff=self.other, start_time=time(9, 0), end_time=time(10, 0))
        self.unassigned = self.booking(staff=None, start_time=time(11, 0), end_time=time(12, 0))

    def test_no_filter_returns_everyone(self):
        resp = self.calendar(**self.week())
        self.assertEqual({s['id'] for s in resp.data['staff']}, {self.staff.id, self.other.id})
        self.assertEqual({b['id'] for b in resp.data['bookings']}, {self.mine.id, self.theirs.id, self.unassigned.id})

    def test_one_staff(self):
        resp = self.calendar(staff=self.staff.id, **self.week())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual([s['id'] for s in resp.data['staff']], [self.staff.id])
        self.assertEqual([b['id'] for b in resp.data['bookings']], [self.mine.id])

    def test_several_staff(self):
        resp = self.calendar(staff=f'{self.staff.id},{self.other.id}', **self.week())
        self.assertEqual({s['id'] for s in resp.data['staff']}, {self.staff.id, self.other.id})
        self.assertEqual({b['id'] for b in resp.data['bookings']}, {self.mine.id, self.theirs.id})

    def test_unknown_staff_is_empty_not_error(self):
        resp = self.calendar(staff=999999, **self.week())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['staff'], [])
        self.assertEqual(resp.data['bookings'], [])

    def test_bad_staff_param(self):
        self.assertEqual(self.calendar(staff='a,b', **self.week()).status_code, 400)
        self.assertEqual(self.calendar(staff='1;2', **self.week()).status_code, 400)


class TestCalendarExceptions(CalendarBase):

    def test_only_exceptions_inside_the_range(self):
        inside = self.create_staff_exception(self.staff, exception_date=self.day)
        last_day = self.create_staff_exception(self.staff, exception_date=self.day + timedelta(days=6),
                                               is_available=True, start_time=time(12, 0), end_time=time(18, 0))
        self.create_staff_exception(self.staff, exception_date=self.day - timedelta(days=1))
        self.create_staff_exception(self.staff, exception_date=self.day + timedelta(days=7))
        resp = self.calendar(**self.week())
        staff = next(s for s in resp.data['staff'] if s['id'] == self.staff.id)
        self.assertEqual([e['id'] for e in staff['exceptions']], [inside.id, last_day.id])
        self.assertEqual(staff['exceptions'][1]['start_time'], '12:00:00')
        self.assertTrue(staff['exceptions'][1]['is_available'])

    def test_weekly_hours_are_not_range_limited(self):
        for day in range(1, 7):
            self.create_availability(self.staff, day_of_week=day, is_available=(day < 5))
        resp = self.calendar(date_from=self.day.isoformat(), date_to=self.day.isoformat())
        staff = next(s for s in resp.data['staff'] if s['id'] == self.staff.id)
        self.assertEqual([a['day_of_week'] for a in staff['availability']], [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual([a['is_available'] for a in staff['availability']][5:], [False, False])


class TestCalendarQueries(CalendarBase):

    def test_query_count_does_not_grow_with_data(self):
        self.booking()
        self.create_staff_exception(self.staff, exception_date=self.day + timedelta(days=1))
        with CaptureQueriesContext(connection) as ctx:
            resp = self.calendar(**self.week())
        self.assertEqual(resp.status_code, 200, resp.data)
        baseline = _app_query_count(ctx)

        # Three more staff members, each with hours, exceptions and bookings
        for n in range(3):
            staff = self.create_staff()
            self.service.staff_members.add(staff)
            for day in range(7):
                self.create_availability(staff, day_of_week=day)
            self.create_staff_exception(staff, exception_date=self.day + timedelta(days=n))
            for hour in (9, 11, 14):
                self.booking(staff=staff, start_time=time(hour, 0), end_time=time(hour + 1, 0),
                             client=self.create_client())
        with assertAppQueries(self, baseline):
            resp = self.calendar(**self.week())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(len(resp.data['staff']), 4)
        self.assertEqual(len(resp.data['bookings']), 10)


class TestCalendarGate(CalendarBase):

    def test_any_booking_user_may_read(self):
        for user in (self.agent, self.manager, self.admin, self.staff_user):
            self.assertEqual(self.calendar(user=user, **self.week()).status_code, 200, user.email)

    def test_without_feature_is_refused(self):
        outsider = self.create_user(email='cal-outsider@test.com', role='admin')
        outsider.has_feature = lambda key: False
        self.assertEqual(self.calendar(user=outsider, **self.week()).status_code, 403)

    def test_unauthenticated(self):
        resp = self.api_get(f"{CALENDAR_URL}?date_from={self.day.isoformat()}&date_to={self.day.isoformat()}")
        self.assertIn(resp.status_code, (401, 403))
