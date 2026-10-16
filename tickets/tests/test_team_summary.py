"""Tests for the DB-side time aggregates: team_summary and ticket_summary."""
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from tickets.models import TicketTimeLog
from tickets.tests.conftest import TicketTestCase


def _backdate(log, days_ago, duration=None):
    """entered_at is auto_now_add, so push it back after creation."""
    entered = timezone.now() - timedelta(days=days_ago)
    fields = {'entered_at': entered}
    if duration is not None:
        fields['exited_at'] = entered + timedelta(seconds=duration)
        fields['duration_seconds'] = duration
    TicketTimeLog.objects.filter(pk=log.pk).update(**fields)
    log.refresh_from_db()
    return log


class TestTeamSummaryPermissions(TicketTestCase):

    def test_anonymous_rejected(self):
        resp = self.api_get('/api/time-logs/team_summary/')
        self.assertIn(resp.status_code, (401, 403))

    def test_non_staff_without_feature_forbidden(self):
        user = self.create_user(email='ts-plain@test.com')
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_get('/api/time-logs/team_summary/', user=user)
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_with_advanced_analytics_allowed(self):
        user = self.create_user(email='ts-feature@test.com')
        with patch('users.models.User.has_feature',
                   side_effect=lambda key: key == 'advanced_analytics'):
            resp = self.api_get('/api/time-logs/team_summary/', user=user)
        self.assertEqual(resp.status_code, 200)

    def test_staff_allowed(self):
        admin = self.create_admin()
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_get('/api/time-logs/team_summary/', user=admin)
        self.assertEqual(resp.status_code, 200)


class TestTeamSummaryAggregation(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.alice = self.create_user(email='alice@test.com', first_name='Alice', last_name='A')
        self.bob = self.create_user(email='bob@test.com')
        _, self.col1, self.col2, _ = self.setup_board_with_columns(admin=self.admin)
        self.ticket = self.create_ticket(title='T', column=self.col2, created_by=self.admin)

    def test_per_user_rows_and_totals(self):
        _backdate(self.create_time_log(self.ticket, self.col2, self.alice), 1, duration=3600)
        _backdate(self.create_time_log(self.ticket, self.col2, self.alice), 2, duration=1800)
        _backdate(self.create_time_log(self.ticket, self.col2, self.alice), 0)  # active
        _backdate(self.create_time_log(self.ticket, self.col2, self.bob), 3, duration=600)
        # Outside the window — must be ignored
        _backdate(self.create_time_log(self.ticket, self.col2, self.bob), 40, duration=99999)

        resp = self.api_get('/api/time-logs/team_summary/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        data = resp.data

        self.assertEqual(data['period']['days'], 30)
        self.assertEqual(data['totals'], {'total_seconds': 6000, 'sessions': 3, 'users': 2})

        self.assertEqual([u['user']['id'] for u in data['users']], [self.alice.id, self.bob.id])
        alice = data['users'][0]
        self.assertEqual(alice['user'], {'id': self.alice.id, 'full_name': 'Alice A', 'email': 'alice@test.com'})
        self.assertEqual(alice['total_seconds'], 5400)
        self.assertEqual(alice['sessions'], 2)
        self.assertEqual(alice['active_sessions'], 1)
        self.assertIsNotNone(alice['last_activity'])

        bob = data['users'][1]
        self.assertEqual(bob['user']['full_name'], 'bob@test.com')  # falls back to email
        self.assertEqual(bob['total_seconds'], 600)
        self.assertEqual(bob['active_sessions'], 0)

    def test_users_without_logs_are_not_listed(self):
        _backdate(self.create_time_log(self.ticket, self.col2, self.alice), 1, duration=10)
        resp = self.api_get('/api/time-logs/team_summary/', user=self.admin)
        self.assertEqual([u['user']['id'] for u in resp.data['users']], [self.alice.id])

    def test_empty_period(self):
        resp = self.api_get('/api/time-logs/team_summary/?days=7', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['users'], [])
        self.assertEqual(resp.data['totals'], {'total_seconds': 0, 'sessions': 0, 'users': 0})
        self.assertEqual(resp.data['period']['days'], 7)

    def test_days_is_clamped(self):
        resp = self.api_get('/api/time-logs/team_summary/?days=0', user=self.admin)
        self.assertEqual(resp.data['period']['days'], 1)
        resp = self.api_get('/api/time-logs/team_summary/?days=5000', user=self.admin)
        self.assertEqual(resp.data['period']['days'], 365)

    def test_days_invalid(self):
        resp = self.api_get('/api/time-logs/team_summary/?days=abc', user=self.admin)
        self.assertEqual(resp.status_code, 400)

    def test_explicit_date_range(self):
        _backdate(self.create_time_log(self.ticket, self.col2, self.alice), 10, duration=100)
        _backdate(self.create_time_log(self.ticket, self.col2, self.bob), 2, duration=200)
        today = timezone.now().date()
        start = (today - timedelta(days=12)).isoformat()
        end = (today - timedelta(days=5)).isoformat()
        resp = self.api_get(f'/api/time-logs/team_summary/?start_date={start}&end_date={end}', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([u['user']['id'] for u in resp.data['users']], [self.alice.id])
        self.assertEqual(resp.data['totals']['total_seconds'], 100)

    def test_end_date_includes_whole_day(self):
        _backdate(self.create_time_log(self.ticket, self.col2, self.alice), 0, duration=5)
        today = timezone.now().date().isoformat()
        resp = self.api_get(f'/api/time-logs/team_summary/?start_date={today}&end_date={today}', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['totals']['sessions'], 1)

    def test_start_after_end_rejected(self):
        resp = self.api_get('/api/time-logs/team_summary/?start_date=2026-10-10&end_date=2026-10-01',
                            user=self.admin)
        self.assertEqual(resp.status_code, 400)

    def test_bad_date_rejected(self):
        resp = self.api_get('/api/time-logs/team_summary/?start_date=not-a-date', user=self.admin)
        self.assertEqual(resp.status_code, 400)


class TestTicketSummary(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        _, self.col1, self.col2, self.col3 = self.setup_board_with_columns(admin=self.admin)
        self.ticket = self.create_ticket(title='Summ', column=self.col2, created_by=self.admin)

    def test_summary_shape(self):
        _backdate(self.create_time_log(self.ticket, self.col2, self.admin), 3, duration=100)
        _backdate(self.create_time_log(self.ticket, self.col1, self.admin), 2, duration=50)
        _backdate(self.create_time_log(self.ticket, self.col2, self.admin), 1, duration=200)
        active = _backdate(self.create_time_log(self.ticket, self.col2, self.admin), 0)
        TicketTimeLog.objects.filter(pk=active.pk).update(entered_at=timezone.now() - timedelta(seconds=90))

        resp = self.api_get(f'/api/time-logs/ticket_summary/?ticket={self.ticket.id}', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        data = resp.data
        self.assertEqual(data['ticket_id'], self.ticket.id)
        self.assertEqual(data['total_seconds'], 350)

        self.assertEqual(data['active']['log_id'], active.id)
        self.assertEqual(data['active']['column'], {'id': self.col2.id, 'name': 'In Progress', 'color': self.col2.color})
        self.assertGreaterEqual(data['active']['elapsed_seconds'], 90)

        self.assertEqual(
            data['by_column'],
            [
                {'column_id': self.col2.id, 'column_name': 'In Progress', 'column_color': self.col2.color,
                 'total_seconds': 300, 'sessions': 2},
                {'column_id': self.col1.id, 'column_name': 'To Do', 'column_color': self.col1.color,
                 'total_seconds': 50, 'sessions': 1},
            ],
        )
        self.assertEqual(len(data['logs']), 4)
        self.assertEqual(data['logs'][0]['id'], active.id)  # newest first
        self.assertEqual(data['logs'][0]['ticket_id'], self.ticket.id)
        self.assertIsNone(data['logs'][0]['exited_at'])

    def test_no_logs(self):
        resp = self.api_get(f'/api/time-logs/ticket_summary/?ticket={self.ticket.id}', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['total_seconds'], 0)
        self.assertIsNone(resp.data['active'])
        self.assertEqual(resp.data['by_column'], [])
        self.assertEqual(resp.data['logs'], [])

    def test_logs_capped_at_20(self):
        for i in range(25):
            _backdate(self.create_time_log(self.ticket, self.col2, self.admin), i, duration=1)
        resp = self.api_get(f'/api/time-logs/ticket_summary/?ticket={self.ticket.id}', user=self.admin)
        self.assertEqual(len(resp.data['logs']), 20)
        self.assertEqual(resp.data['total_seconds'], 25)

    def test_ticket_param_required(self):
        resp = self.api_get('/api/time-logs/ticket_summary/', user=self.admin)
        self.assertEqual(resp.status_code, 400)
        resp = self.api_get('/api/time-logs/ticket_summary/?ticket=abc', user=self.admin)
        self.assertEqual(resp.status_code, 400)

    def test_unknown_ticket_404(self):
        resp = self.api_get('/api/time-logs/ticket_summary/?ticket=999999', user=self.admin)
        self.assertEqual(resp.status_code, 404)

    def test_non_staff_sees_own_ticket_but_not_others(self):
        user = self.create_user(email='ts-user@test.com')
        own = self.create_ticket(title='Mine', column=self.col2, created_by=user)
        self.create_time_log(own, self.col2, self.admin)

        resp = self.api_get(f'/api/time-logs/ticket_summary/?ticket={own.id}', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.data['active'])

        resp = self.api_get(f'/api/time-logs/ticket_summary/?ticket={self.ticket.id}', user=user)
        self.assertEqual(resp.status_code, 404)

    def test_non_staff_board_member_sees_ticket(self):
        user = self.create_user(email='ts-board@test.com')
        self.ticket.column.board.board_users.add(user)
        resp = self.api_get(f'/api/time-logs/ticket_summary/?ticket={self.ticket.id}', user=user)
        self.assertEqual(resp.status_code, 200)
