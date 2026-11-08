"""Personal ticket timer (BE-03: A14, E32)."""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.db import IntegrityError, connection
from django.utils import timezone

from tickets import timer
from tickets.models import Ticket, TicketTimeEntry
from tickets.tests.conftest import (
    TicketTestCase, assert_constant_queries, capture_queries,
)

BROADCAST = 'users.consumers.broadcast_ticket_activity'


class TimerTestBase(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.agent = self.create_user(email='timer-agent@test.com', first_name='Nino', last_name='Beridze')
        self.board, self.col_todo, self.col_progress, self.col_done = \
            self.setup_board_with_columns(admin=self.admin)
        # The agent created both tickets, so both are visible to them.
        self.t1 = self.create_ticket('First', column=self.col_todo, created_by=self.agent)
        self.t2 = self.create_ticket('Second', column=self.col_todo, created_by=self.agent)

    # url helpers
    def start(self, ticket, user=None, **body):
        return self.api_post(f'/api/tickets/{ticket.id}/timer/start/', body, user=user or self.agent)

    def stop(self, ticket, user=None):
        return self.api_post(f'/api/tickets/{ticket.id}/timer/stop/', {}, user=user or self.agent)

    def get_timer(self, ticket, user=None):
        return self.api_get(f'/api/tickets/{ticket.id}/timer/', user=user or self.agent)

    def hide_from_agent(self, ticket):
        """Transfer ``ticket`` to a restricted board and drop the agent's
        authorship, so the agent can no longer see it."""
        restricted = self.create_board('Restricted', created_by=self.admin, open=False)
        col = self.create_column(restricted, 'R', created_by=self.admin)
        Ticket.objects.filter(pk=ticket.pk).update(column=col, created_by=self.admin)
        ticket.refresh_from_db()
        return ticket


class TestStartStop(TimerTestBase):

    def test_start_creates_running_entry(self):
        resp = self.start(self.t1)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(resp.data['is_running'])
        self.assertEqual(resp.data['ticket_id'], self.t1.id)
        self.assertIsNone(resp.data['ended_at'])
        self.assertEqual(resp.data['user'], {'id': self.agent.id, 'name': 'Nino Beridze', 'initials': 'NB'})
        self.assertEqual(set(resp.data), {
            'id', 'ticket_id', 'user', 'started_at', 'ended_at', 'duration_seconds', 'is_running'})
        self.assertEqual(TicketTimeEntry.objects.filter(user=self.agent, ended_at__isnull=True).count(), 1)

    def test_start_again_same_ticket_is_idempotent(self):
        first = self.start(self.t1)
        again = self.start(self.t1)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data['id'], first.data['id'])
        self.assertEqual(TicketTimeEntry.objects.count(), 1)

    def test_start_on_other_ticket_conflicts(self):
        first = self.start(self.t1)
        resp = self.start(self.t2)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'timer_running')
        self.assertEqual(resp.data['running']['id'], first.data['id'])
        self.assertEqual(resp.data['running']['ticket_id'], self.t1.id)
        self.assertEqual(resp.data['running']['ticket_title'], 'First')
        self.assertIn('started_at', resp.data['running'])
        self.assertEqual(TicketTimeEntry.objects.count(), 1)

    def test_conflict_hides_title_of_invisible_ticket(self):
        self.start(self.t1)
        self.hide_from_agent(self.t1)
        resp = self.start(self.t2)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['running']['ticket_id'], self.t1.id)
        self.assertIsNone(resp.data['running']['ticket_title'])

    def test_switch_stops_old_and_starts_new(self):
        old = TicketTimeEntry.objects.create(
            ticket=self.t1, user=self.agent, started_at=timezone.now() - timedelta(minutes=5))
        resp = self.start(self.t2, switch=True)
        self.assertEqual(resp.status_code, 201)
        old.refresh_from_db()
        self.assertIsNotNone(old.ended_at)
        self.assertGreaterEqual(old.duration_seconds, 299)
        self.assertEqual(resp.data['ticket_id'], self.t2.id)
        self.assertTrue(resp.data['is_running'])

    def test_switch_works_when_old_ticket_is_invisible(self):
        old = TicketTimeEntry.objects.create(
            ticket=self.t1, user=self.agent, started_at=timezone.now() - timedelta(seconds=30))
        self.hide_from_agent(self.t1)
        resp = self.start(self.t2, switch=True)
        self.assertEqual(resp.status_code, 201)
        old.refresh_from_db()
        self.assertFalse(old.is_running)
        self.assertGreaterEqual(old.duration_seconds, 29)

    def test_stop_returns_duration(self):
        entry = TicketTimeEntry.objects.create(
            ticket=self.t1, user=self.agent, started_at=timezone.now() - timedelta(seconds=90))
        resp = self.stop(self.t1)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], entry.id)
        self.assertFalse(resp.data['is_running'])
        self.assertGreaterEqual(resp.data['duration_seconds'], 89)
        self.assertIsNotNone(resp.data['ended_at'])

    def test_stop_with_nothing_running(self):
        resp = self.stop(self.t1)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data, {'code': 'timer_not_running'})

    def test_stop_with_timer_on_other_ticket_is_409(self):
        self.start(self.t1)
        resp = self.stop(self.t2)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'timer_not_running')
        self.assertTrue(TicketTimeEntry.objects.get(ticket=self.t1).is_running)

    def test_cannot_stop_someone_elses_entry(self):
        other = self.create_user(email='other-timer@test.com')
        self.t1.assigned_to = other
        self.t1.save()
        theirs = TicketTimeEntry.objects.create(ticket=self.t1, user=other)
        resp = self.stop(self.t1)  # agent: no own entry here
        self.assertEqual(resp.status_code, 409)
        resp = self.api_post('/api/time-entries/current/stop/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 409)
        theirs.refresh_from_db()
        self.assertTrue(theirs.is_running)

    def test_start_on_closed_column_is_409(self):
        closed = self.create_ticket('Done', column=self.col_done, created_by=self.agent)
        resp = self.start(closed)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data, {'code': 'ticket_closed'})
        self.assertFalse(TicketTimeEntry.objects.exists())

    def test_start_body_may_be_empty(self):
        resp = self.api_post(f'/api/tickets/{self.t1.id}/timer/start/', None, user=self.agent)
        self.assertEqual(resp.status_code, 201)

    def test_unauthenticated_is_rejected(self):
        resp = self.api_post(f'/api/tickets/{self.t1.id}/timer/start/', {})
        self.assertIn(resp.status_code, (401, 403))


class TestVisibility(TimerTestBase):

    def test_invisible_ticket_is_404(self):
        hidden = self.create_ticket('Hidden', column=self.col_todo, created_by=self.admin)
        restricted = self.create_board('Private', created_by=self.admin, open=False)
        col = self.create_column(restricted, 'P', created_by=self.admin)
        hidden.column = col
        hidden.save()
        self.assertEqual(self.start(hidden).status_code, 404)
        self.assertEqual(self.stop(hidden).status_code, 404)
        self.assertEqual(self.get_timer(hidden).status_code, 404)
        self.assertFalse(TicketTimeEntry.objects.exists())

    def test_stop_on_ticket_that_became_invisible_is_404(self):
        self.start(self.t1)
        self.hide_from_agent(self.t1)
        self.assertEqual(self.stop(self.t1).status_code, 404)

    def test_staff_can_use_any_ticket(self):
        hidden = self.hide_from_agent(self.t1)
        self.assertEqual(self.start(hidden, user=self.admin).status_code, 201)
        self.assertEqual(self.get_timer(hidden, user=self.admin).status_code, 200)

    def test_assignee_and_board_user_can_start(self):
        assignee = self.create_user(email='assignee-timer@test.com')
        t = self.create_ticket('Assigned', column=self.col_todo, created_by=self.admin, assigned_to=assignee)
        self.assertEqual(self.start(t, user=assignee).status_code, 201)
        member = self.create_user(email='member-timer@test.com')
        board = self.create_board('Members', created_by=self.admin, users=[member])
        col = self.create_column(board, 'M', created_by=self.admin)
        t2 = self.create_ticket('Board ticket', column=col, created_by=self.admin)
        self.assertEqual(self.start(t2, user=member).status_code, 201)

    def test_unknown_ticket_is_404(self):
        resp = self.api_post('/api/tickets/999999/timer/start/', {}, user=self.admin)
        self.assertEqual(resp.status_code, 404)


class TestCurrent(TimerTestBase):

    def test_current_idle(self):
        resp = self.api_get('/api/time-entries/current/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data['running'])
        self.assertIn('server_now', resp.data)
        self.assertEqual(set(resp.data), {'running', 'server_now'})

    def test_current_running(self):
        entry = self.create_time_entry(self.t2, self.agent)
        resp = self.api_get('/api/time-entries/current/', user=self.agent)
        self.assertEqual(resp.data['running']['id'], entry.id)
        self.assertEqual(resp.data['running']['ticket_id'], self.t2.id)

    def test_current_stop_on_invisible_ticket(self):
        entry = TicketTimeEntry.objects.create(
            ticket=self.t1, user=self.agent, started_at=timezone.now() - timedelta(seconds=10))
        self.hide_from_agent(self.t1)
        resp = self.api_post('/api/time-entries/current/stop/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], entry.id)
        self.assertGreaterEqual(resp.data['duration_seconds'], 9)
        entry.refresh_from_db()
        self.assertFalse(entry.is_running)

    def test_current_stop_nothing_running(self):
        resp = self.api_post('/api/time-entries/current/stop/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data, {'code': 'timer_not_running'})


class TestTotals(TimerTestBase):

    def test_totals_include_running_and_sort_by_user(self):
        now = timezone.now()
        other = self.create_user(email='tot-other@test.com', first_name='Ana', last_name='K')
        self.t1.assigned_to = other
        self.t1.save()
        TicketTimeEntry.objects.create(
            ticket=self.t1, user=self.agent, started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=1), duration_seconds=3600)
        TicketTimeEntry.objects.create(
            ticket=self.t1, user=self.agent, started_at=now - timedelta(seconds=60))
        TicketTimeEntry.objects.create(
            ticket=self.t1, user=other, started_at=now - timedelta(hours=3),
            ended_at=now - timedelta(hours=1), duration_seconds=7200)
        resp = self.get_timer(self.t1)
        self.assertEqual(resp.status_code, 200)
        d = resp.data
        self.assertEqual(set(d), {'running', 'my_total_seconds', 'total_seconds', 'server_now', 'by_user', 'entries'})
        self.assertIsNotNone(d['running'])
        self.assertTrue(d['running']['is_running'])
        self.assertGreaterEqual(d['my_total_seconds'], 3660)
        self.assertLess(d['my_total_seconds'], 3700)
        self.assertGreaterEqual(d['total_seconds'], 3600 + 60 + 7200)
        self.assertEqual([r['user']['id'] for r in d['by_user']], [other.id, self.agent.id])
        self.assertEqual(d['by_user'][0]['seconds'], 7200)
        self.assertEqual(d['by_user'][0]['user'], {'id': other.id, 'name': 'Ana K', 'initials': 'AK'})
        self.assertEqual(len(d['entries']), 3)
        self.assertTrue(d['server_now'])

    def test_running_is_only_the_callers(self):
        other = self.create_user(email='tot-other2@test.com')
        self.create_time_entry(self.t1, other)
        resp = self.get_timer(self.t1)
        self.assertIsNone(resp.data['running'])
        self.assertEqual(resp.data['my_total_seconds'], 0)

    def test_deleted_user_bucket_is_null(self):
        now = timezone.now()
        TicketTimeEntry.objects.create(
            ticket=self.t1, user=None, started_at=now - timedelta(minutes=2),
            ended_at=now - timedelta(minutes=1), duration_seconds=60)
        resp = self.get_timer(self.t1)
        self.assertEqual(resp.data['by_user'], [{'user': None, 'seconds': 60}])
        self.assertIsNone(resp.data['entries'][0]['user'])

    def test_entries_capped_at_50(self):
        now = timezone.now()
        TicketTimeEntry.objects.bulk_create([
            TicketTimeEntry(ticket=self.t1, user=self.agent, started_at=now - timedelta(hours=i + 1),
                            ended_at=now - timedelta(hours=i + 1) + timedelta(minutes=1),
                            duration_seconds=60)
            for i in range(55)
        ])
        resp = self.get_timer(self.t1)
        self.assertEqual(len(resp.data['entries']), 50)
        self.assertEqual(resp.data['total_seconds'], 55 * 60)

    def test_query_count_constant(self):
        now = timezone.now()
        users = [self.create_user(email=f'qc{i}@test.com') for i in range(5)]
        self.create_time_entry(self.t1, self.agent, started_at=now - timedelta(minutes=1),
                               ended_at=now, duration_seconds=60)
        for i in range(20):
            self.create_time_entry(self.t2, users[i % 5], started_at=now - timedelta(hours=i + 1),
                                   ended_at=now - timedelta(hours=i), duration_seconds=3600)
        n_small, _ = assert_constant_queries(
            lambda: self.assertEqual(self.get_timer(self.t1, user=self.admin).status_code, 200),
            lambda: self.assertEqual(self.get_timer(self.t2, user=self.admin).status_code, 200),
        )
        self.assertLessEqual(n_small, 10)


class TestTimeEntriesList(TimerTestBase):

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.hidden = self.create_ticket('Hidden', column=self.col_todo, created_by=self.admin)
        restricted = self.create_board('Private list', created_by=self.admin, open=False)
        col = self.create_column(restricted, 'P', created_by=self.admin)
        Ticket.objects.filter(pk=self.hidden.pk).update(column=col)
        self.other = self.create_user(email='list-other@test.com')
        self.e1 = self.create_time_entry(self.t1, self.agent, started_at=now - timedelta(days=2),
                                         ended_at=now - timedelta(days=2) + timedelta(minutes=1),
                                         duration_seconds=60)
        self.e2 = self.create_time_entry(self.t2, self.other, started_at=now - timedelta(hours=1),
                                         ended_at=now, duration_seconds=3600)
        self.e3 = self.create_time_entry(self.hidden, self.admin, started_at=now - timedelta(minutes=5))

    def ids(self, resp):
        self.assertEqual(resp.status_code, 200, resp.data)
        return [r['id'] for r in self.get_results(resp)]

    def test_paginated(self):
        resp = self.api_get('/api/time-entries/', user=self.admin)
        self.assertIn('count', resp.data)
        self.assertEqual(resp.data['count'], 3)

    def test_staff_sees_all_newest_first(self):
        self.assertEqual(self.ids(self.api_get('/api/time-entries/', user=self.admin)),
                         [self.e3.id, self.e2.id, self.e1.id])

    def test_non_staff_scoped_to_visible_tickets(self):
        self.assertEqual(self.ids(self.api_get('/api/time-entries/', user=self.agent)),
                         [self.e2.id, self.e1.id])
        resp = self.api_get(f'/api/time-entries/{self.e3.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 404)

    def test_filter_ticket_and_user(self):
        self.assertEqual(self.ids(self.api_get(f'/api/time-entries/?ticket={self.t1.id}', user=self.admin)),
                         [self.e1.id])
        self.assertEqual(self.ids(self.api_get(f'/api/time-entries/?user={self.other.id}', user=self.admin)),
                         [self.e2.id])

    def test_filter_started_range(self):
        after = (timezone.now() - timedelta(days=1)).isoformat()
        resp = self.api_get('/api/time-entries/', user=self.admin, data={'started_after': after})
        self.assertEqual(self.ids(resp), [self.e3.id, self.e2.id])
        resp = self.api_get('/api/time-entries/', user=self.admin, data={'started_before': after})
        self.assertEqual(self.ids(resp), [self.e1.id])

    def test_read_only(self):
        resp = self.api_post('/api/time-entries/', {'ticket': self.t1.id}, user=self.admin)
        self.assertEqual(resp.status_code, 405)


class TestClosedColumnAndDeletes(TimerTestBase):

    def test_move_into_closed_column_stops_running_entries(self):
        other = self.create_user(email='closed-other@test.com')
        a = TicketTimeEntry.objects.create(
            ticket=self.t1, user=self.agent, started_at=timezone.now() - timedelta(seconds=40))
        b = TicketTimeEntry.objects.create(
            ticket=self.t1, user=other, started_at=timezone.now() - timedelta(seconds=20))
        with patch(BROADCAST, new_callable=AsyncMock) as bc, \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api_patch(f'/api/tickets/{self.t1.id}/move_to_column/',
                                  {'column_id': self.col_done.id}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_running)
        self.assertFalse(b.is_running)
        self.assertGreaterEqual(a.duration_seconds, 39)
        self.assertGreaterEqual(b.duration_seconds, 19)
        timer_calls = [c for c in bc.call_args_list if c.args[2]['kind'] == 'timer']
        self.assertEqual(len(timer_calls), 1)
        schema, board_id, payload = timer_calls[0].args
        self.assertEqual(board_id, self.board.id)
        self.assertEqual(payload, {'ticket_id': self.t1.id, 'kind': 'timer',
                                   'actor_id': self.admin.id, 'client_mutation_id': None})

    def test_plain_save_into_closed_column_stops_entries(self):
        e = self.create_time_entry(self.t1, self.agent)
        ticket = Ticket.objects.get(pk=self.t1.pk)  # column not cached
        ticket.column_id = self.col_done.id
        ticket.save()
        e.refresh_from_db()
        self.assertFalse(e.is_running)

    def test_save_in_open_column_keeps_entries_running(self):
        e = self.create_time_entry(self.t1, self.agent)
        self.t1.title = 'Renamed'
        self.t1.column = self.col_progress
        self.t1.save()
        e.refresh_from_db()
        self.assertTrue(e.is_running)

    def test_save_with_known_open_column_runs_no_timer_query(self):
        self.t1.column  # cache the (open) column
        with capture_queries() as ctx:
            self.t1.save()
        self.assertFalse([q for q in ctx.captured_queries if 'tickettimeentry' in q['sql']])

    def test_ticket_delete_cascades(self):
        self.create_time_entry(self.t1, self.agent)
        self.t1.delete()
        self.assertFalse(TicketTimeEntry.objects.exists())

    def test_user_delete_stops_and_keeps_rows(self):
        leaver = self.create_user(email='leaver@test.com')
        t = self.create_ticket('Theirs', column=self.col_todo, created_by=self.admin)
        e = TicketTimeEntry.objects.create(
            ticket=t, user=leaver, started_at=timezone.now() - timedelta(seconds=15))
        leaver.delete()
        e.refresh_from_db()
        self.assertIsNone(e.user_id)
        self.assertFalse(e.is_running)
        self.assertGreaterEqual(e.duration_seconds, 14)


class TestBroadcastsAndConcurrency(TimerTestBase):

    def test_start_and_stop_broadcast_timer_activity(self):
        with patch(BROADCAST, new_callable=AsyncMock) as bc, \
                self.captureOnCommitCallbacks(execute=True):
            self.start(self.t1)
        bc.assert_awaited_once()
        schema, board_id, payload = bc.call_args.args
        self.assertEqual(schema, connection.schema_name)
        self.assertEqual(board_id, self.board.id)
        self.assertEqual(payload['kind'], 'timer')
        self.assertEqual(payload['ticket_id'], self.t1.id)
        self.assertEqual(payload['actor_id'], self.agent.id)
        with patch(BROADCAST, new_callable=AsyncMock) as bc, \
                self.captureOnCommitCallbacks(execute=True):
            self.stop(self.t1)
        bc.assert_awaited_once()

    def test_switch_broadcasts_to_both_tickets(self):
        self.create_time_entry(self.t1, self.agent)
        with patch(BROADCAST, new_callable=AsyncMock) as bc, \
                self.captureOnCommitCallbacks(execute=True):
            self.start(self.t2, switch=True)
        self.assertEqual(sorted(c.args[2]['ticket_id'] for c in bc.call_args_list),
                         sorted([self.t1.id, self.t2.id]))

    def test_no_broadcast_on_conflict(self):
        self.create_time_entry(self.t1, self.agent)
        with patch(BROADCAST, new_callable=AsyncMock) as bc, \
                self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.start(self.t2).status_code, 409)
        bc.assert_not_awaited()

    def test_broadcast_failure_does_not_break_request(self):
        with patch(BROADCAST, new_callable=AsyncMock, side_effect=RuntimeError('redis down')), \
                self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.start(self.t1).status_code, 201)

    def test_race_real_constraint_is_409_and_txn_usable(self):
        """Another request inserted a running entry after our locked read:
        the real partial unique index fires inside the savepoint."""
        winner = self.create_time_entry(self.t1, self.agent)
        with patch.object(timer, '_locked_running', return_value=None):
            resp = self.start(self.t2)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'timer_running')
        self.assertEqual(resp.data['running']['id'], winner.id)
        # The test transaction is still usable.
        self.assertEqual(TicketTimeEntry.objects.count(), 1)

    def test_race_patched_insert_is_409_and_txn_usable(self):
        real_create = TicketTimeEntry.objects.create

        def racing_create(**kwargs):
            raise IntegrityError('duplicate key value violates unique constraint '
                                 '"tickets_one_running_timer_per_user"')

        with patch.object(TicketTimeEntry.objects, 'create', side_effect=racing_create):
            resp = self.start(self.t2)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'timer_running')
        real_create(ticket=self.t1, user=self.agent)  # the outer transaction is still usable
        self.assertEqual(TicketTimeEntry.objects.count(), 1)

    def test_race_lost_to_same_ticket_returns_200(self):
        winner = self.create_time_entry(self.t2, self.agent)
        with patch.object(timer, '_locked_running', return_value=None):
            resp = self.start(self.t2)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], winner.id)

    def test_switch_uses_select_for_update(self):
        self.create_time_entry(self.t1, self.agent)
        with capture_queries() as ctx:
            resp = self.start(self.t2, switch=True)
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(any('FOR UPDATE' in q['sql'] for q in ctx.captured_queries),
                        [q['sql'] for q in ctx.captured_queries])


class TestTimerSchema(TicketTestCase):

    def test_new_operations_documented(self):
        from amanati_crm.schema import TenantAwareSchemaGenerator
        schema = TenantAwareSchemaGenerator().get_schema(request=None, public=True)
        paths = schema['paths']
        expected = {
            ('/api/tickets/{id}/timer/', 'get'): 'tickets_timer_retrieve',
            ('/api/tickets/{id}/timer/start/', 'post'): 'tickets_timer_start',
            ('/api/tickets/{id}/timer/stop/', 'post'): 'tickets_timer_stop',
            ('/api/time-entries/', 'get'): 'time_entries_list',
            ('/api/time-entries/current/', 'get'): 'time_entries_current_retrieve',
            ('/api/time-entries/current/stop/', 'post'): 'time_entries_current_stop',
        }
        for (path, method), op_id in expected.items():
            self.assertIn(path, paths)
            self.assertEqual(paths[path][method]['operationId'], op_id)
        start = paths['/api/tickets/{id}/timer/start/']['post']
        self.assertEqual(set(start['responses']) >= {'200', '201', '409'}, True)
        id_param = [p for p in start['parameters'] if p['name'] == 'id'][0]
        self.assertEqual(id_param['schema']['type'], 'string')
        list_params = [p['name'] for p in paths['/api/time-entries/']['get']['parameters']]
        for name in ('ticket', 'user', 'started_after', 'started_before', 'page', 'page_size'):
            self.assertIn(name, list_params)
