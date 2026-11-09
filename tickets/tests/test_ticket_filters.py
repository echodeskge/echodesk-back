"""A4 filter and ordering parameters on the ticket list and the lite kanban (BE-07).

Every due-date test freezes the clock at 2026-11-09 21:30 UTC, which is
already 2026-11-10 in Tbilisi (R7), and uses absolute dates only.
"""
import datetime
from unittest import mock
from urllib.parse import urlencode

from tickets.models import TicketAssignment
from tickets.tests.conftest import TicketTestCase

FROZEN_NOW = datetime.datetime(2026, 11, 9, 21, 30, tzinfo=datetime.timezone.utc)
D = datetime.date


def frozen():
    return mock.patch('tickets.dates.timezone.now', return_value=FROZEN_NOW)


class FilterFixture(TicketTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='flt-admin@test.com')
        self.agent = self.create_user(email='flt-agent@test.com', first_name='Ann', last_name='Agent')
        self.other = self.create_user(email='flt-other@test.com')
        self.board, self.todo, self.doing, self.done = self.setup_board_with_columns(admin=self.admin)
        self.tag_a = self.create_tag(name='Alpha', created_by=self.admin)
        self.tag_b = self.create_tag(name='Alphabet', created_by=self.admin)

    def ticket(self, title, column=None, **kwargs):
        return self.create_ticket(title=title, column=column or self.todo, created_by=self.admin, **kwargs)

    def list_ids(self, params=None, user=None, board=True):
        query = dict(params or {})
        if board:
            query.setdefault('board', self.board.id)
        query.setdefault('page_size', 100)
        resp = self.api_get(f'/api/tickets/?{urlencode(query)}', user=user or self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        return [row['id'] for row in resp.data['results']]

    def kanban(self, params=None, user=None, board=None):
        query = {'lite': 1, **(params or {})}
        return self.api_get(
            f'/api/boards/{(board or self.board).id}/kanban_board/?{urlencode(query)}',
            user=user or self.admin,
        )

    def kanban_ids(self, params=None, user=None):
        resp = self.kanban(params, user)
        self.assertEqual(resp.status_code, 200, resp.data)
        return [card['id'] for cards in resp.data['tickets_by_column'].values() for card in cards]

    def both(self, params, user=None):
        """(sorted list ids, sorted kanban ids) for the same filter."""
        return sorted(self.list_ids(params, user)), sorted(self.kanban_ids(params, user))

    def assertBoth(self, params, expected, user=None):
        expected = sorted(t.id for t in expected)
        list_ids, kanban_ids = self.both(params, user)
        self.assertEqual(list_ids, expected, f'list {params}')
        self.assertEqual(kanban_ids, expected, f'kanban {params}')


class TestAssigneeAndPriorityFilters(FilterFixture):
    def test_assignee_me_matches_m2m_and_legacy_assigned_to(self):
        legacy = self.ticket('legacy', assigned_to=self.agent)
        m2m = self.ticket('m2m')
        TicketAssignment.objects.create(ticket=m2m, user=self.agent, role='collaborator')
        self.ticket('someone else', assigned_to=self.other)
        self.ticket('nobody')
        self.assertBoth({'assignee': 'me'}, [legacy, m2m], user=self.agent)

    def test_assignee_csv_of_ids(self):
        a = self.ticket('a', assigned_to=self.agent)
        b = self.ticket('b')
        TicketAssignment.objects.create(ticket=b, user=self.other, role='primary')
        self.ticket('c')
        self.assertBoth({'assignee': f'{self.agent.id},{self.other.id}'}, [a, b])

    def test_assignee_with_two_matching_assignees_is_one_row(self):
        t = self.ticket('two', assigned_to=self.agent)
        TicketAssignment.objects.create(ticket=t, user=self.agent, role='primary')
        TicketAssignment.objects.create(ticket=t, user=self.other, role='collaborator')
        self.assertBoth({'assignee': f'{self.agent.id},{self.other.id}'}, [t])

    def test_unassigned(self):
        self.ticket('legacy', assigned_to=self.agent)
        m2m = self.ticket('m2m')
        TicketAssignment.objects.create(ticket=m2m, user=self.agent)
        free = self.ticket('free')
        self.assertBoth({'unassigned': '1'}, [free])
        # unassigned=0 is no filter at all
        self.assertEqual(len(self.list_ids({'unassigned': '0'})), 3)

    def test_priority_in(self):
        crit = self.ticket('c', priority='critical')
        high = self.ticket('h', priority='high')
        self.ticket('m', priority='medium')
        self.ticket('l', priority='low')
        self.assertBoth({'priority__in': 'critical,high'}, [crit, high])

    def test_invalid_values_are_400(self):
        for params in ({'priority__in': 'urgent'}, {'assignee': 'bob'}, {'tags__in': 'x'},
                       {'due': 'later'}, {'due_before': '10/11/2026'}, {'is_closed': 'maybe'}):
            resp = self.api_get(f'/api/tickets/?{urlencode(params)}', user=self.admin)
            self.assertEqual(resp.status_code, 400, params)
            resp = self.kanban(params)
            self.assertEqual(resp.status_code, 400, params)


class TestTagAndTextFilters(FilterFixture):
    def test_tags_in_returns_no_duplicate_when_two_tags_match(self):
        both = self.ticket('both')
        both.tags.add(self.tag_a, self.tag_b)
        one = self.ticket('one')
        one.tags.add(self.tag_b)
        self.ticket('none')
        list_ids, kanban_ids = self.both({'tags__in': f'{self.tag_a.id},{self.tag_b.id}'})
        self.assertEqual(list_ids, sorted([both.id, one.id]))
        self.assertEqual(kanban_ids, sorted([both.id, one.id]))

    def test_q_matching_two_tags_of_one_ticket_is_one_card_and_one_row(self):
        """RM3: 'Alpha' matches both tags of the ticket; no join fan-out."""
        t = self.ticket('Plain title')
        t.tags.add(self.tag_a, self.tag_b)
        self.assertEqual(self.kanban_ids({'q': 'alpha'}), [t.id])
        self.assertEqual(self.list_ids({'search': 'alpha'}), [t.id])

    def test_q_and_search_match_title_description_tag_and_id(self):
        by_title = self.ticket('Printer on fire')
        by_desc = self.ticket('Other', description='<p>The <strong>printer</strong> jams</p>')
        by_tag = self.ticket('Tagged')
        printer_tag = self.create_tag(name='PrinterIssues', created_by=self.admin)
        by_tag.tags.add(printer_tag)
        self.ticket('Unrelated')
        expected = sorted([by_title.id, by_desc.id, by_tag.id])
        for param in ('q', 'search'):
            self.assertEqual(sorted(self.list_ids({param: 'printer'})), expected, param)
            self.assertEqual(sorted(self.kanban_ids({param: 'printer'})), expected, param)

        target = self.ticket('Find me by id')
        for term in (f'#{target.id}', str(target.id)):
            for param in ('q', 'search'):
                self.assertIn(target.id, self.list_ids({param: term}), (param, term))
                self.assertIn(target.id, self.kanban_ids({param: term}), (param, term))

    def test_q_and_search_are_aliases_and_q_wins(self):
        a = self.ticket('apple')
        self.ticket('banana')
        self.assertEqual(self.list_ids({'q': 'apple'}), [a.id])
        self.assertEqual(self.kanban_ids({'search': 'apple'}), [a.id])
        self.assertEqual(self.list_ids({'q': 'apple', 'search': 'banana'}), [a.id])


class TestDueAndClosedFilters(FilterFixture):
    def setUp(self):
        super().setUp()
        # Frozen "today" in Tbilisi is Tue 2026-11-10; the ISO week ends Sun 11-15.
        self.due_yesterday = self.ticket('due 11-09', due_date=D(2026, 11, 9))
        self.due_today = self.ticket('due 11-10', due_date=D(2026, 11, 10))
        self.due_sunday = self.ticket('due 11-15', due_date=D(2026, 11, 15))
        self.due_next_week = self.ticket('due 11-16', due_date=D(2026, 11, 16))
        self.no_due = self.ticket('no due')
        self.closed_overdue = self.ticket('closed', column=self.done, due_date=D(2026, 11, 1))

    def test_due_overdue_uses_the_tbilisi_date(self):
        with frozen():
            self.assertBoth({'due': 'overdue'}, [self.due_yesterday])

    def test_due_week_includes_overdue_and_ends_sunday(self):
        with frozen():
            self.assertBoth({'due': 'week'}, [self.due_yesterday, self.due_today, self.due_sunday])

    def test_due_none(self):
        with frozen():
            self.assertBoth({'due': 'none'}, [self.no_due])

    def test_overdue_flag_on_list_rows_matches_filter(self):
        with frozen():
            resp = self.api_get(f'/api/tickets/?board={self.board.id}&page_size=100', user=self.admin)
        flags = {row['id']: row['is_overdue_task'] for row in resp.data['results']}
        self.assertIs(flags[self.due_yesterday.id], True)
        self.assertIs(flags[self.due_today.id], False)
        self.assertIs(flags[self.closed_overdue.id], False)

    def test_due_before_and_after_are_inclusive(self):
        self.assertBoth({'due_after': '2026-11-10', 'due_before': '2026-11-15'},
                        [self.due_today, self.due_sunday])

    def test_is_closed(self):
        self.assertBoth({'is_closed': 'true'}, [self.closed_overdue])
        self.assertBoth({'is_closed': 'false'}, [
            self.due_yesterday, self.due_today, self.due_sunday, self.due_next_week, self.no_due,
        ])


class TestOrdering(FilterFixture):
    def test_priority_rank_orders_critical_first(self):
        low = self.ticket('l', priority='low')
        crit = self.ticket('c', priority='critical')
        med = self.ticket('m', priority='medium')
        high = self.ticket('h', priority='high')
        self.assertEqual(self.list_ids({'ordering': 'priority_rank'}), [crit.id, high.id, med.id, low.id])
        self.assertEqual(self.list_ids({'ordering': '-priority_rank'}), [low.id, med.id, high.id, crit.id])
        cards = self.kanban({'ordering': 'priority_rank'}).data['tickets_by_column'][self.todo.id]
        self.assertEqual([c['id'] for c in cards], [crit.id, high.id, med.id, low.id])

    def test_due_date_sorts_nulls_last_both_ways(self):
        none = self.ticket('none')
        late = self.ticket('late', due_date=D(2026, 12, 1))
        early = self.ticket('early', due_date=D(2026, 11, 1))
        self.assertEqual(self.list_ids({'ordering': 'due_date'}), [early.id, late.id, none.id])
        self.assertEqual(self.list_ids({'ordering': '-due_date'}), [late.id, early.id, none.id])
        cards = self.kanban({'ordering': 'due_date'}).data['tickets_by_column'][self.todo.id]
        self.assertEqual([c['id'] for c in cards], [early.id, late.id, none.id])

    def test_default_list_order_is_newest_first_with_id_tie_break(self):
        a = self.ticket('a')
        b = self.ticket('b')
        from tickets.models import Ticket
        Ticket.objects.filter(pk__in=[a.pk, b.pk]).update(created_at=a.created_at)
        self.assertEqual(self.list_ids(), [b.id, a.id])

    def test_legacy_ordering_values_still_work(self):
        a = self.ticket('a')
        b = self.ticket('b')
        self.assertEqual(self.list_ids({'ordering': 'created_at'}), [a.id, b.id])
        self.assertEqual(self.list_ids({'ordering': '-updated_at'})[0], b.id)
        # unknown values fall back to the default instead of failing
        self.assertEqual(self.list_ids({'ordering': 'nonsense'}), [b.id, a.id])

    def test_group_column_orders_by_column_position_first(self):
        done = self.ticket('done', column=self.done)
        doing = self.ticket('doing', column=self.doing)
        todo_old = self.ticket('todo old', column=self.todo)
        todo_new = self.ticket('todo new', column=self.todo)
        ids = self.list_ids({'group': 'column'})
        self.assertEqual(ids, [todo_new.id, todo_old.id, doing.id, done.id])
        ids = self.list_ids({'group': 'column', 'ordering': 'created_at'})
        self.assertEqual(ids, [todo_old.id, todo_new.id, doing.id, done.id])

    def test_kanban_manual_order_is_unchanged_without_ordering(self):
        second = self.ticket('second')
        first = self.ticket('first')
        from tickets.models import Ticket
        Ticket.objects.filter(pk=first.pk).update(position_in_column=0)
        Ticket.objects.filter(pk=second.pk).update(position_in_column=1)
        cards = self.kanban().data['tickets_by_column'][self.todo.id]
        self.assertEqual([c['id'] for c in cards], [first.id, second.id])


class TestFiltersNeverWidenVisibility(FilterFixture):
    def setUp(self):
        super().setUp()
        self.restricted = self.create_board(name='Restricted', created_by=self.admin, open=False)
        self.r_col = self.create_column(self.restricted, name='R', created_by=self.admin)
        self.hidden = self.create_ticket(
            title='hidden urgent', column=self.r_col, created_by=self.admin, priority='critical',
        )

    def test_agent_filter_on_restricted_board_is_empty(self):
        for params in ({}, {'priority__in': 'critical'}, {'q': 'hidden'}, {'search': 'hidden'},
                       {'unassigned': '1'}, {'is_closed': 'false'}):
            ids = self.list_ids({**params, 'board': self.restricted.id}, user=self.agent)
            self.assertEqual(ids, [], params)
        resp = self.kanban({'priority__in': 'critical'}, user=self.agent, board=self.restricted)
        self.assertEqual(resp.status_code, 404)

    def test_agent_search_without_board_does_not_find_hidden_ticket(self):
        resp = self.api_get('/api/tickets/?search=hidden&page_size=100', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['results'], [])
        resp = self.api_get(f'/api/tickets/?search=%23{self.hidden.id}', user=self.agent)
        self.assertEqual(resp.data['results'], [])


class TestKanbanFilterShape(FilterFixture):
    def test_filtered_kanban_keeps_every_column_key(self):
        self.ticket('urgent', priority='critical')
        self.ticket('calm', column=self.doing, priority='low')
        resp = self.kanban({'priority__in': 'critical'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.data['tickets_by_column'].keys()), {self.todo.id, self.doing.id, self.done.id})
        self.assertEqual(resp.data['tickets_by_column'][self.doing.id], [])
        self.assertEqual(resp.data['tickets_by_column'][self.done.id], [])
        # columns[].tickets_count stays unfiltered (F20)
        counts = {c['id']: c['tickets_count'] for c in resp.data['columns']}
        self.assertEqual(counts[self.doing.id], 1)

    def test_non_lite_kanban_accepts_filters_too(self):
        hit = self.ticket('urgent', priority='critical')
        self.ticket('calm', priority='low')
        resp = self.api_get(
            f'/api/boards/{self.board.id}/kanban_board/?priority__in=critical', user=self.admin,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([t['id'] for t in resp.data['tickets_by_column'][self.todo.id]], [hit.id])
        self.assertIn('description', resp.data['tickets_by_column'][self.todo.id][0])
