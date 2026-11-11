"""
Sentry ECHODESK-BACKEND-88: N+1 in booking_management.tasks.send_staff_booking_reminders.

For every booking in the window the task ran an UPDATE to flag it, a
NotificationPreference lookup and a tenant-language lookup. The batch is
now flagged in one statement and preferences / the tenant language are
resolved once for all recipients.
"""
from datetime import datetime, time
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext

from booking_management.tests.test_admin_hardening import HardeningBase
from users.models import Notification, NotificationPreference


def _sql(ctx, needle, prefix=''):
    return [
        q['sql'] for q in ctx.captured_queries
        if needle in q['sql'] and q['sql'].lstrip().upper().startswith(prefix)
    ]


class TestStaffReminderQueries(HardeningBase):

    def setUp(self):
        super().setUp()
        self.staff_users = [self.staff_user]
        self.staffs = [self.staff]
        for i in range(3):
            user = self.create_user(email=f'reminder-staff{i}@test.com', role='agent',
                                    first_name=f'S{i}', last_name='Staff')
            staff = self.create_staff(user=user)
            self.service.staff_members.add(staff)
            self.staff_users.append(user)
            self.staffs.append(staff)
        # One recipient muted the reminder type.
        self.muted = self.staff_users[2]
        NotificationPreference.objects.create(
            user=self.muted, notification_type='booking_reminder', in_app=False,
        )
        starts = [time(9, 50), time(9, 55), time(10, 0), time(10, 5)]
        self.bookings = [
            self.booking(status='confirmed', staff=staff, start_time=start, end_time=time(11, 0))
            for staff, start in zip(self.staffs, starts)
        ]

    def run_task(self):
        from booking_management import tasks
        now = datetime.combine(self.day, time(9, 45))
        with patch('booking_management.utils.tenant_now', return_value=now), \
                patch('users.notification_utils._dispatch_push'), \
                patch('asgiref.sync.async_to_sync', return_value=lambda **kw: None), \
                CaptureQueriesContext(connection) as ctx:
            sent = tasks._send_staff_reminders_for_tenant()
        return sent, ctx

    def test_batch_is_flagged_and_looked_up_once(self):
        sent, ctx = self.run_task()
        self.assertEqual(sent, 3)
        self.assertEqual(len(_sql(ctx, 'UPDATE "bookings"', 'UPDATE')), 1)
        self.assertEqual(len(_sql(ctx, 'users_notificationpreference')), 1)
        # The tenant's fallback language is read once for the batch, not
        # once per booking (the other tenant read is the booking settings').
        language_reads = [s for s in _sql(ctx, 'tenants_tenant', 'SELECT')
                          if s.startswith('SELECT "tenants_tenant"."preferred_language"')]
        self.assertEqual(len(language_reads), 1)
        self.assertLessEqual(len(_sql(ctx, 'tenants_tenant', 'SELECT')), 2)

        for booking in self.bookings:
            booking.refresh_from_db()
            self.assertTrue(booking.staff_reminder_sent)
        notified = {n.user_id for n in Notification.objects.filter(notification_type='booking_reminder')}
        self.assertEqual(notified, {u.id for u in self.staff_users if u != self.muted})
        # Texts are still rendered per recipient (minutes until the start).
        first = self.notes(self.staff_user, 'booking_reminder')[0]
        self.assertEqual(first.title, 'Next client in 5 min')

        # Nothing left to do on the next run.
        sent, ctx = self.run_task()
        self.assertEqual(sent, 0)
        self.assertEqual(_sql(ctx, 'UPDATE "bookings"', 'UPDATE'), [])

    def test_recipient_language_still_wins_over_the_tenant_language(self):
        self.staff_users[1].preferred_language = 'ka'
        self.staff_users[1].save(update_fields=['preferred_language'])
        self.run_task()
        self.assertEqual(self.notes(self.staff_users[1], 'booking_reminder')[0].title,
                         'შემდეგი კლიენტი 10 წუთში')
        self.assertEqual(self.notes(self.staff_user, 'booking_reminder')[0].title,
                         'Next client in 5 min')
