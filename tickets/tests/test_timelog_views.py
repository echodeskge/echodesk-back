"""Tests for TicketTimeLogViewSet."""
from django.utils import timezone
from datetime import timedelta
from tickets.models import TicketTimeLog
from tickets.tests.conftest import TicketTestCase


class TestTimeLogRead(TicketTestCase):

    def test_list_time_logs(self):
        admin = self.create_admin()
        board, col1, col2, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        self.create_time_log(ticket, col1, admin)
        resp = self.api_get('/api/time-logs/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(self.get_results(resp)), 1)

    def test_log_row_carries_ids_and_title(self):
        admin = self.create_admin()
        board, col1, col2, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='Deep link me', column=col2, created_by=admin)
        log = self.create_time_log(ticket, col2, admin)
        resp = self.api_get(f'/api/time-logs/{log.id}/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['ticket'], 'Deep link me')  # legacy title field untouched
        self.assertEqual(resp.data['ticket_id'], ticket.id)
        self.assertEqual(resp.data['ticket_title'], 'Deep link me')
        self.assertEqual(resp.data['column_id'], col2.id)
        self.assertEqual(resp.data['column']['id'], col2.id)
        self.assertIsNone(resp.data['exited_at'])
        self.assertIsNone(resp.data['duration_seconds'])
        self.assertIsInstance(resp.data['duration_display'], str)

    def test_filter_by_ticket(self):
        admin = self.create_admin()
        board, col1, col2, _ = self.setup_board_with_columns(admin=admin)
        t1 = self.create_ticket(title='T1', column=col1, created_by=admin)
        t2 = self.create_ticket(title='T2', column=col1, created_by=admin)
        self.create_time_log(t1, col1, admin)
        self.create_time_log(t2, col1, admin)
        resp = self.api_get(f'/api/time-logs/?ticket={t1.id}', user=admin)
        self.assertEqual(len(self.get_results(resp)), 1)

    def test_filter_by_user(self):
        admin = self.create_admin()
        user = self.create_user(email='tlu@test.com')
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        self.create_time_log(ticket, col1, admin)
        self.create_time_log(ticket, col1, user)
        resp = self.api_get(f'/api/time-logs/?user={user.id}', user=admin)
        self.assertEqual(len(self.get_results(resp)), 1)


class TestTimeLogPermissions(TicketTestCase):

    def test_staff_sees_all_time_logs(self):
        admin = self.create_admin()
        user = self.create_user(email='tlu2@test.com')
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=user)
        self.create_time_log(ticket, col1, user)
        resp = self.api_get('/api/time-logs/', user=admin)
        self.assertGreaterEqual(len(self.get_results(resp)), 1)

    def test_non_staff_sees_own_ticket_logs(self):
        admin = self.create_admin()
        user = self.create_user(email='tlu3@test.com')
        other = self.create_user(email='tlu4@test.com')
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        t_own = self.create_ticket(title='Own', column=col1, created_by=user)
        t_other = self.create_ticket(title='Other', column=col1, created_by=other)
        self.create_time_log(t_own, col1, user)
        self.create_time_log(t_other, col1, other)
        resp = self.api_get('/api/time-logs/', user=user)
        ticket_titles = [log['ticket'] for log in self.get_results(resp)]
        self.assertTrue(any('Own' in t for t in ticket_titles))

    def test_read_only(self):
        admin = self.create_admin()
        resp = self.api_post('/api/time-logs/', {'ticket': 1}, user=admin)
        self.assertEqual(resp.status_code, 405)


class TestMyTimeSummary(TicketTestCase):

    def test_my_time_summary(self):
        admin = self.create_admin()
        board, col1, _, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col1, created_by=admin)
        now = timezone.now()
        self.create_time_log(
            ticket, col1, admin,
            exited_at=now, duration_seconds=3600
        )
        resp = self.api_get('/api/time-logs/my_time_summary/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('total_time_seconds', resp.data)

    def test_my_time_summary_invalid_days(self):
        """Validates Bug 4 fix: ?days=abc returns 400."""
        admin = self.create_admin()
        resp = self.api_get('/api/time-logs/my_time_summary/?days=abc', user=admin)
        self.assertEqual(resp.status_code, 400)

    def test_my_time_summary_days_clamped(self):
        admin = self.create_admin()
        resp = self.api_get('/api/time-logs/my_time_summary/?days=-5', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['period_days'], 1)
        resp = self.api_get('/api/time-logs/my_time_summary/?days=100000', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['period_days'], 365)
        resp = self.api_get('/api/time-logs/my_time_summary/', user=admin)
        self.assertEqual(resp.data['period_days'], 30)

    def test_my_time_summary_typed_children(self):
        admin = self.create_admin()
        board, col1, col2, _ = self.setup_board_with_columns(admin=admin)
        ticket = self.create_ticket(title='T', column=col2, created_by=admin)
        now = timezone.now()
        self.create_time_log(ticket, col2, admin, exited_at=now, duration_seconds=120)
        self.create_time_log(ticket, col2, admin)  # active
        resp = self.api_get('/api/time-logs/my_time_summary/', user=admin)
        self.assertEqual(resp.status_code, 200)
        row = resp.data['time_by_column'][0]
        self.assertEqual(row['column__name'], 'In Progress')
        self.assertEqual(row['total_seconds'], 120)
        self.assertEqual(row['session_count'], 1)
        day = resp.data['daily_breakdown'][0]
        self.assertEqual(day['total_seconds'], 120)
        self.assertEqual(day['session_count'], 1)
        self.assertEqual(resp.data['active_sessions'][0]['ticket_id'], ticket.id)
        self.assertEqual(resp.data['recent_activity'][0]['ticket_id'], ticket.id)


class TestTicketUpdateTimeLogs(TicketTestCase):
    """PATCH /api/tickets/{id}/ with column_id must track time like move_to_column."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        _, self.col_todo, self.col_track, self.col_done = self.setup_board_with_columns(admin=self.admin)

    def test_patch_into_tracked_column_opens_log(self):
        ticket = self.create_ticket(title='T', column=self.col_todo, created_by=self.admin)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/', {'column_id': self.col_track.id}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['column']['id'], self.col_track.id)
        logs = TicketTimeLog.objects.filter(ticket=ticket)
        self.assertEqual(logs.count(), 1)
        log = logs.get()
        self.assertEqual(log.column, self.col_track)
        self.assertEqual(log.user, self.admin)
        self.assertIsNone(log.exited_at)

    def test_patch_out_of_tracked_column_closes_log(self):
        ticket = self.create_ticket(title='T', column=self.col_track, created_by=self.admin)
        log = self.create_time_log(ticket, self.col_track, self.admin)
        TicketTimeLog.objects.filter(pk=log.pk).update(entered_at=timezone.now() - timedelta(minutes=5))

        resp = self.api_patch(f'/api/tickets/{ticket.id}/', {'column_id': self.col_done.id}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        log.refresh_from_db()
        self.assertIsNotNone(log.exited_at)
        self.assertGreaterEqual(log.duration_seconds, 299)
        # Done column does not track time: no new log
        self.assertEqual(TicketTimeLog.objects.filter(ticket=ticket).count(), 1)

    def test_patch_between_tracked_columns_closes_and_opens(self):
        other_track = self.create_column(
            self.col_track.board, name='Review', position=5, track_time=True, created_by=self.admin
        )
        ticket = self.create_ticket(title='T', column=self.col_track, created_by=self.admin)
        first = self.create_time_log(ticket, self.col_track, self.admin)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/', {'column_id': other_track.id}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        first.refresh_from_db()
        self.assertIsNotNone(first.exited_at)
        self.assertTrue(
            TicketTimeLog.objects.filter(ticket=ticket, column=other_track, exited_at__isnull=True).exists()
        )

    def test_patch_without_column_change_leaves_logs_alone(self):
        ticket = self.create_ticket(title='T', column=self.col_track, created_by=self.admin)
        log = self.create_time_log(ticket, self.col_track, self.admin)
        resp = self.api_patch(f'/api/tickets/{ticket.id}/', {'title': 'Renamed'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        log.refresh_from_db()
        self.assertIsNone(log.exited_at)
        self.assertEqual(TicketTimeLog.objects.filter(ticket=ticket).count(), 1)

        # Same column id sent explicitly is also a no-op
        resp = self.api_patch(f'/api/tickets/{ticket.id}/', {'column_id': self.col_track.id}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        log.refresh_from_db()
        self.assertIsNone(log.exited_at)
        self.assertEqual(TicketTimeLog.objects.filter(ticket=ticket).count(), 1)

    def test_move_to_column_still_tracks(self):
        ticket = self.create_ticket(title='T', column=self.col_track, created_by=self.admin)
        log = self.create_time_log(ticket, self.col_track, self.admin)
        resp = self.api_patch(
            f'/api/tickets/{ticket.id}/move_to_column/', {'column_id': self.col_done.id}, user=self.admin
        )
        self.assertEqual(resp.status_code, 200)
        log.refresh_from_db()
        self.assertIsNotNone(log.exited_at)
        self.assertIsNotNone(log.duration_seconds)
