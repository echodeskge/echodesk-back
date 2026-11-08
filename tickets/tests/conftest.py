"""
Shared test infrastructure for tickets app tests.
Extends EchoDeskTenantTestCase with ticket-specific helpers.
"""
from contextlib import contextmanager
from decimal import Decimal
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from users.tests.conftest import EchoDeskTenantTestCase
from tickets.models import (
    Board, TicketColumn, Ticket, Tag, ChecklistItem, TicketComment,
    TicketAssignment, TicketTimeLog, TicketPayment,
    ItemList, ListItem, TicketForm, TicketFormSubmission, TicketAttachment,
    TicketTimeEntry,
)


@contextmanager
def capture_queries():
    """Capture the SQL queries run inside the block (a thin wrapper around
    CaptureQueriesContext; deliberately not a pytest fixture, since these are
    unittest-style TestCases).

        with capture_queries() as ctx:
            ...
        len(ctx.captured_queries)
    """
    with CaptureQueriesContext(connection) as ctx:
        yield ctx


def assert_constant_queries(fn_small, fn_large, msg=None):
    """Run ``fn_small`` then ``fn_large`` and assert both issue the same
    number of queries (no N+1 as the data set grows). Returns the two
    counts as a tuple."""
    with capture_queries() as small:
        fn_small()
    with capture_queries() as large:
        fn_large()
    n_small, n_large = len(small.captured_queries), len(large.captured_queries)
    if n_small != n_large:
        detail = '\n'.join(q['sql'] for q in large.captured_queries)
        raise AssertionError(
            (msg or 'Query count grew with data size')
            + f': small={n_small}, large={n_large}\nLarge run queries:\n{detail}'
        )
    return n_small, n_large


class TicketTestCase(EchoDeskTenantTestCase):
    """
    Ticket-specific test case with factory helpers for all ticket models.
    """

    @staticmethod
    def get_results(resp):
        """Extract results from a paginated or non-paginated response."""
        if isinstance(resp.data, dict) and 'results' in resp.data:
            return resp.data['results']
        return resp.data

    def create_board(self, name='Test Board', created_by=None, *, open=None,
                     groups=(), users=(), order_users=(), color=None, **kwargs):
        """Create a board.

        - ``groups`` / ``users`` / ``order_users`` fill board_groups,
          board_users and order_users.
        - ``open=True`` asserts the board is open (no access lists at all).
        - ``open=False`` guarantees a restricted board: if no access list was
          given, the board creator is added to ``board_users``.
        - ``open=None`` (default, legacy behaviour) adds nothing beyond the
          lists given, so a plain ``create_board()`` is an open board.
        - ``color`` sets Board.color (otherwise the model default).
        """
        if created_by is None:
            created_by = self.create_admin(email=f'board-admin-{Board.objects.count()}@test.com')
        if color is not None:
            kwargs['color'] = color
        if open is True and (groups or users or order_users):
            raise ValueError('An open board cannot have groups, users or order_users')
        board = Board.objects.create(name=name, created_by=created_by, **kwargs)
        if groups:
            board.board_groups.add(*groups)
        if users:
            board.board_users.add(*users)
        if order_users:
            board.order_users.add(*order_users)
        if open is False and not (groups or users or order_users):
            board.board_users.add(created_by)
        return board

    def create_column(self, board, name='To Do', position=0, created_by=None, **kwargs):
        if created_by is None:
            created_by = board.created_by
        return TicketColumn.objects.create(
            board=board, name=name, position=position, created_by=created_by, **kwargs
        )

    def create_ticket(self, title='Test Ticket', column=None, created_by=None, **kwargs):
        if created_by is None:
            created_by = self.create_user(email=f'ticket-user-{Ticket.objects.count()}@test.com')
        return Ticket.objects.create(title=title, column=column, created_by=created_by, **kwargs)

    def create_client(self, name='Test Client', email=None, phone='', **kwargs):
        """Create a social_integrations.Client (a ticket customer)."""
        from social_integrations.models import Client
        return Client.objects.create(name=name, email=email, phone=phone or '', **kwargs)

    def make_inactive_group(self, name=None, **kwargs):
        """Create a TenantGroup with is_active=False."""
        from users.models import TenantGroup
        if name is None:
            name = f'Inactive Group {TenantGroup.objects.count()}'
        return self.create_tenant_group(name=name, is_active=False, **kwargs)

    def create_time_entry(self, ticket, user, **kwargs):
        return TicketTimeEntry.objects.create(ticket=ticket, user=user, **kwargs)

    def create_tag(self, name='Bug', **kwargs):
        return Tag.objects.create(name=name, **kwargs)

    def create_checklist_item(self, ticket, text='Item 1', created_by=None, **kwargs):
        if created_by is None:
            created_by = ticket.created_by
        return ChecklistItem.objects.create(ticket=ticket, text=text, created_by=created_by, **kwargs)

    def create_comment(self, ticket, user, comment='Test comment'):
        return TicketComment.objects.create(ticket=ticket, user=user, comment=comment)

    def create_assignment(self, ticket, user, role='collaborator', assigned_by=None):
        return TicketAssignment.objects.create(
            ticket=ticket, user=user, role=role, assigned_by=assigned_by
        )

    def create_time_log(self, ticket, column, user, **kwargs):
        return TicketTimeLog.objects.create(ticket=ticket, column=column, user=user, **kwargs)

    def create_payment(self, ticket, amount=Decimal('10.00'), **kwargs):
        return TicketPayment.objects.create(
            ticket=ticket, amount=amount, currency=ticket.currency,
            payment_method='manual', **kwargs
        )

    def create_item_list(self, title='Test List', created_by=None, **kwargs):
        if created_by is None:
            created_by = self.create_admin(email=f'list-admin-{ItemList.objects.count()}@test.com')
        return ItemList.objects.create(title=title, created_by=created_by, **kwargs)

    def create_list_item(self, item_list, label='Item 1', created_by=None, **kwargs):
        if created_by is None:
            created_by = item_list.created_by
        return ListItem.objects.create(item_list=item_list, label=label, created_by=created_by, **kwargs)

    def create_ticket_form(self, title='Test Form', created_by=None, **kwargs):
        if created_by is None:
            created_by = self.create_admin(email=f'form-admin-{TicketForm.objects.count()}@test.com')
        return TicketForm.objects.create(title=title, created_by=created_by, **kwargs)

    def create_attachment(self, ticket, uploaded_by=None, **kwargs):
        if uploaded_by is None:
            uploaded_by = ticket.created_by
        defaults = {
            'filename': 'test.txt',
            'file_size': 100,
            'content_type': 'text/plain',
        }
        defaults.update(kwargs)
        from django.core.files.uploadedfile import SimpleUploadedFile
        if 'file' not in defaults:
            defaults['file'] = SimpleUploadedFile('test.txt', b'test content', content_type='text/plain')
        return TicketAttachment.objects.create(ticket=ticket, uploaded_by=uploaded_by, **defaults)

    def setup_board_with_columns(self, admin=None):
        """Create a board with 3 columns: To Do, In Progress, Done. Returns (board, col1, col2, col3)."""
        if admin is None:
            admin = self.create_admin(email=f'setup-admin-{Board.objects.count()}@test.com')
        board = self.create_board(name='Main Board', created_by=admin)
        col_todo = self.create_column(board, name='To Do', position=0, is_default=True, created_by=admin)
        col_progress = self.create_column(board, name='In Progress', position=1, track_time=True, created_by=admin)
        col_done = self.create_column(board, name='Done', position=2, is_closed_status=True, created_by=admin)
        return board, col_todo, col_progress, col_done
