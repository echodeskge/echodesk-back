"""
Tests for 0-based, renumbered ticket positions.

Covers move_to_column / reorder_in_column (index semantics, clamping,
append-when-omitted) and the invariant that after every operation the
positions in each touched column are exactly 0..n-1 in display order.
"""
from tickets.models import Ticket, TicketHistory
from tickets.tests.conftest import TicketTestCase


class PositionTestCase(TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='pos-admin@test.com')
        self.board, self.col_a, self.col_b, self.col_done = (
            self.setup_board_with_columns(admin=self.admin)
        )

    # ── Helpers ──
    def make_tickets(self, column, n, prefix='T'):
        """Create n tickets in display order; returns them in that order."""
        return [
            self.create_ticket(title=f'{prefix}{i}', column=column, created_by=self.admin)
            for i in range(n)
        ]

    def order_of(self, column):
        """Ticket ids in the column, in the order the kanban board shows them."""
        return list(
            Ticket.objects.filter(column=column)
            .order_by('position_in_column', '-created_at')
            .values_list('id', flat=True)
        )

    def assert_renumbered(self, column):
        """Positions in the column are exactly 0..n-1 with no gaps or duplicates."""
        positions = list(
            Ticket.objects.filter(column=column)
            .order_by('position_in_column')
            .values_list('position_in_column', flat=True)
        )
        self.assertEqual(positions, list(range(len(positions))))

    def move(self, ticket, column, position='omit'):
        payload = {'column_id': column.id}
        if position != 'omit':
            payload['position_in_column'] = position
        return self.api_patch(f'/api/tickets/{ticket.id}/move_to_column/', payload, user=self.admin)

    def reorder(self, ticket, position):
        return self.api_patch(
            f'/api/tickets/{ticket.id}/reorder_in_column/',
            {'position_in_column': position}, user=self.admin,
        )


class TestCreatePositions(PositionTestCase):

    def test_create_appends_zero_based(self):
        t0, t1, t2 = self.make_tickets(self.col_a, 3)
        self.assertEqual([t0.position_in_column, t1.position_in_column, t2.position_in_column], [0, 1, 2])
        self.assert_renumbered(self.col_a)

    def test_create_after_renumbered_column_appends_at_n(self):
        a = self.make_tickets(self.col_a, 3)
        b = self.make_tickets(self.col_b, 2)
        # Shuffle both columns through a move so they get renumbered
        self.assertEqual(self.move(a[2], self.col_b, 0).status_code, 200)
        new_b = self.create_ticket(title='NewB', column=self.col_b, created_by=self.admin)
        new_a = self.create_ticket(title='NewA', column=self.col_a, created_by=self.admin)
        self.assertEqual(new_b.position_in_column, 3)
        self.assertEqual(new_a.position_in_column, 2)
        self.assertEqual(self.order_of(self.col_b), [a[2].id, b[0].id, b[1].id, new_b.id])
        self.assert_renumbered(self.col_a)
        self.assert_renumbered(self.col_b)

    def test_update_does_not_reappend_position_zero(self):
        t0, t1 = self.make_tickets(self.col_a, 2)
        # Simulate a plain edit on the ticket sitting at index 0
        t0.title = 'Edited'
        t0.save()
        t0.refresh_from_db()
        self.assertEqual(t0.position_in_column, 0)


class TestMoveToColumn(PositionTestCase):

    def test_index_zero_lands_first_and_old_first_becomes_one(self):
        a = self.make_tickets(self.col_a, 1)
        b = self.make_tickets(self.col_b, 3)
        resp = self.move(a[0], self.col_b, 0)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['position_in_column'], 0)
        self.assertEqual(self.order_of(self.col_b), [a[0].id, b[0].id, b[1].id, b[2].id])
        b[0].refresh_from_db()
        self.assertEqual(b[0].position_in_column, 1)
        self.assert_renumbered(self.col_a)
        self.assert_renumbered(self.col_b)

    def test_middle_index(self):
        a = self.make_tickets(self.col_a, 1)
        b = self.make_tickets(self.col_b, 3)
        self.assertEqual(self.move(a[0], self.col_b, 2).status_code, 200)
        self.assertEqual(self.order_of(self.col_b), [b[0].id, b[1].id, a[0].id, b[2].id])
        self.assert_renumbered(self.col_b)

    def test_index_past_end_appends(self):
        a = self.make_tickets(self.col_a, 1)
        b = self.make_tickets(self.col_b, 3)
        self.assertEqual(self.move(a[0], self.col_b, 99).status_code, 200)
        self.assertEqual(self.order_of(self.col_b), [b[0].id, b[1].id, b[2].id, a[0].id])
        a[0].refresh_from_db()
        self.assertEqual(a[0].position_in_column, 3)
        self.assert_renumbered(self.col_b)

    def test_index_equal_to_len_appends(self):
        a = self.make_tickets(self.col_a, 1)
        b = self.make_tickets(self.col_b, 2)
        self.assertEqual(self.move(a[0], self.col_b, 2).status_code, 200)
        self.assertEqual(self.order_of(self.col_b), [b[0].id, b[1].id, a[0].id])
        self.assert_renumbered(self.col_b)

    def test_omitted_position_appends(self):
        a = self.make_tickets(self.col_a, 1)
        b = self.make_tickets(self.col_b, 2)
        self.assertEqual(self.move(a[0], self.col_b).status_code, 200)
        self.assertEqual(self.order_of(self.col_b), [b[0].id, b[1].id, a[0].id])
        self.assert_renumbered(self.col_b)

    def test_null_position_appends(self):
        a = self.make_tickets(self.col_a, 1)
        b = self.make_tickets(self.col_b, 2)
        self.assertEqual(self.move(a[0], self.col_b, None).status_code, 200)
        self.assertEqual(self.order_of(self.col_b), [b[0].id, b[1].id, a[0].id])

    def test_negative_index_clamps_to_zero(self):
        a = self.make_tickets(self.col_a, 1)
        b = self.make_tickets(self.col_b, 2)
        self.assertEqual(self.move(a[0], self.col_b, -5).status_code, 200)
        self.assertEqual(self.order_of(self.col_b), [a[0].id, b[0].id, b[1].id])
        self.assert_renumbered(self.col_b)

    def test_non_integer_position_is_400(self):
        a = self.make_tickets(self.col_a, 1)
        resp = self.move(a[0], self.col_b, 'top')
        self.assertEqual(resp.status_code, 400)

    def test_source_column_closes_gap(self):
        a = self.make_tickets(self.col_a, 4)
        self.assertEqual(self.move(a[1], self.col_b, 0).status_code, 200)
        self.assertEqual(self.order_of(self.col_a), [a[0].id, a[2].id, a[3].id])
        self.assert_renumbered(self.col_a)
        self.assert_renumbered(self.col_b)

    def test_move_within_same_column_reorders(self):
        a = self.make_tickets(self.col_a, 3)
        self.assertEqual(self.move(a[2], self.col_a, 0).status_code, 200)
        self.assertEqual(self.order_of(self.col_a), [a[2].id, a[0].id, a[1].id])
        self.assert_renumbered(self.col_a)

    def test_sequence_of_moves_keeps_both_columns_renumbered(self):
        a = self.make_tickets(self.col_a, 3, prefix='A')
        b = self.make_tickets(self.col_b, 3, prefix='B')
        self.move(a[0], self.col_b, 1)
        self.move(b[2], self.col_a, 0)
        self.move(a[1], self.col_b)
        self.move(b[0], self.col_a, 50)
        self.assert_renumbered(self.col_a)
        self.assert_renumbered(self.col_b)
        self.assertEqual(self.order_of(self.col_a), [b[2].id, a[2].id, b[0].id])
        self.assertEqual(self.order_of(self.col_b), [a[0].id, b[1].id, a[1].id])

    def test_move_records_history_user(self):
        a = self.make_tickets(self.col_a, 1)
        self.move(a[0], self.col_b, 0)
        entry = TicketHistory.objects.filter(ticket=a[0], field_name='column_id').first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.user_id, self.admin.id)


class TestReorderInColumn(PositionTestCase):

    def test_reorder_to_zero(self):
        a = self.make_tickets(self.col_a, 3)
        resp = self.reorder(a[2], 0)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['position_in_column'], 0)
        self.assertEqual(self.order_of(self.col_a), [a[2].id, a[0].id, a[1].id])
        self.assert_renumbered(self.col_a)

    def test_reorder_down(self):
        a = self.make_tickets(self.col_a, 4)
        self.assertEqual(self.reorder(a[0], 2).status_code, 200)
        self.assertEqual(self.order_of(self.col_a), [a[1].id, a[2].id, a[0].id, a[3].id])
        self.assert_renumbered(self.col_a)

    def test_reorder_past_end_clamps(self):
        a = self.make_tickets(self.col_a, 3)
        self.assertEqual(self.reorder(a[0], 10).status_code, 200)
        self.assertEqual(self.order_of(self.col_a), [a[1].id, a[2].id, a[0].id])
        self.assert_renumbered(self.col_a)

    def test_reorder_same_index_is_noop(self):
        a = self.make_tickets(self.col_a, 3)
        self.assertEqual(self.reorder(a[1], 1).status_code, 200)
        self.assertEqual(self.order_of(self.col_a), [a[0].id, a[1].id, a[2].id])
        self.assert_renumbered(self.col_a)

    def test_reorder_requires_position(self):
        a = self.make_tickets(self.col_a, 1)
        resp = self.api_patch(f'/api/tickets/{a[0].id}/reorder_in_column/', {}, user=self.admin)
        self.assertEqual(resp.status_code, 400)

    def test_reorder_non_integer_is_400(self):
        a = self.make_tickets(self.col_a, 1)
        self.assertEqual(self.reorder(a[0], 'x').status_code, 400)

    def test_reorder_fixes_legacy_gapped_positions(self):
        # Legacy rows may carry 1-based / gapped positions; one reorder normalises them.
        a = self.make_tickets(self.col_a, 3)
        Ticket.objects.filter(pk=a[0].pk).update(position_in_column=1)
        Ticket.objects.filter(pk=a[1].pk).update(position_in_column=5)
        Ticket.objects.filter(pk=a[2].pk).update(position_in_column=9)
        self.assertEqual(self.reorder(a[2], 1).status_code, 200)
        self.assertEqual(self.order_of(self.col_a), [a[0].id, a[2].id, a[1].id])
        self.assert_renumbered(self.col_a)


class TestKanbanOrder(PositionTestCase):

    def test_kanban_board_lists_tickets_in_position_order(self):
        a = self.make_tickets(self.col_a, 3)
        self.reorder(a[2], 0)
        resp = self.api_get(f'/api/boards/{self.board.id}/kanban_board/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        ids = [t['id'] for t in resp.data['tickets_by_column'][self.col_a.id]]
        self.assertEqual(ids, [a[2].id, a[0].id, a[1].id])


class TestReorderResponse(PositionTestCase):

    def test_reorder_response_keeps_count_annotations(self):
        # The row is re-fetched under FOR UPDATE (no aggregates allowed), so
        # the response must be re-loaded through get_queryset() afterwards
        a = self.make_tickets(self.col_a, 2)
        self.create_comment(a[0], self.admin, 'c1')
        self.create_checklist_item(a[0], text='done', created_by=self.admin, is_checked=True)
        self.create_checklist_item(a[0], text='todo', created_by=self.admin)
        resp = self.reorder(a[0], 1)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['position_in_column'], 1)
        self.assertEqual(resp.data['comments_count'], 1)
        self.assertEqual(resp.data['checklist_items_count'], 2)
        self.assertEqual(resp.data['completed_checklist_items_count'], 1)
