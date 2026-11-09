"""Single definition of who may see and change boards and tickets (plan E2-E4).

Every endpoint that resolves a board or a ticket on a user's behalf goes
through this module: the ticket and board viewsets, kanban, comments,
attachments, checklist, assignments, form submissions, time logs, the
personal timer, payments and the board WebSocket.

Rules:

- **Staff and superusers** see every board and every ticket.
- **Boards** (``accessible_boards``) a non-staff user may see: open boards
  (no ``board_users``, ``board_groups`` or ``order_users``), boards where the
  user is in ``board_users`` or ``order_users``, and boards linked to one of
  the user's *active* groups.
- **Tickets** a non-staff user may see: tickets on an accessible board, or
  tickets the user created, is ``assigned_to``, is in ``assigned_users`` of,
  or that are assigned to one of the user's groups (any group, active or
  not, as before).

Everything is built from ``Exists``/``IN`` subqueries, so filtering never
fans out the outer query and no ``.distinct()`` is needed.
"""
from django.db.models import Exists, OuterRef, Q

from .models import Board, Ticket, TicketAssignment


def is_staff_user(user):
    """Staff and superusers bypass every visibility rule."""
    return bool(
        user is not None
        and getattr(user, 'is_authenticated', False)
        and (user.is_staff or user.is_superuser)
    )


def _user_group_ids(user, active_only):
    """A ``values('tenantgroup_id')`` subquery of the user's groups."""
    through = type(user).tenant_groups.through
    qs = through.objects.filter(user_id=user.id)
    if active_only:
        qs = qs.filter(tenantgroup__is_active=True)
    return qs.values('tenantgroup_id')


# ── boards ──────────────────────────────────────────────────────────────────

def accessible_boards(user, base_qs=None):
    """Boards ``user`` may see (all of them for staff/superusers)."""
    qs = Board.objects.all() if base_qs is None else base_qs
    if user is None or not getattr(user, 'is_authenticated', False):
        return qs.none()
    if is_staff_user(user):
        return qs

    board_users = Board.board_users.through
    board_groups = Board.board_groups.through
    order_users = Board.order_users.through

    return qs.filter(
        Exists(board_users.objects.filter(board_id=OuterRef('pk'), user_id=user.id))
        | Exists(order_users.objects.filter(board_id=OuterRef('pk'), user_id=user.id))
        | Exists(board_groups.objects.filter(
            board_id=OuterRef('pk'),
            tenantgroup_id__in=_user_group_ids(user, active_only=True),
        ))
        | (
            ~Exists(board_users.objects.filter(board_id=OuterRef('pk')))
            & ~Exists(board_groups.objects.filter(board_id=OuterRef('pk')))
            & ~Exists(order_users.objects.filter(board_id=OuterRef('pk')))
        )
    )


def can_access_board(user, board_id):
    """Whether ``user`` may see the board with id ``board_id``."""
    try:
        board_id = int(board_id)
    except (TypeError, ValueError):
        return False
    return accessible_boards(user).filter(pk=board_id).exists()


# ── tickets ─────────────────────────────────────────────────────────────────

def ticket_visibility_q(user):
    """A ``Q`` limiting a ``Ticket`` queryset to what ``user`` may see.

    Kept under its historical name (it used to live in ``tickets.views``) so
    existing callers keep working. Returns an empty ``Q()`` (everything) for
    staff and a ``Q`` matching nothing for anonymous users.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return Q(pk__in=[])
    if is_staff_user(user):
        return Q()
    return (
        Q(column__board_id__in=accessible_boards(user).values('pk'))
        | Q(created_by_id=user.id)
        | Q(assigned_to_id=user.id)
        | Q(Exists(TicketAssignment.objects.filter(ticket_id=OuterRef('pk'), user_id=user.id)))
        | Q(Exists(Ticket.assigned_groups.through.objects.filter(
            ticket_id=OuterRef('pk'),
            tenantgroup_id__in=_user_group_ids(user, active_only=False),
        )))
    )


def visible_tickets(user, qs=None):
    """``qs`` (default: every ticket) limited to the tickets ``user`` may see."""
    qs = Ticket.objects.all() if qs is None else qs
    if is_staff_user(user):
        return qs
    return qs.filter(ticket_visibility_q(user))


def visible_ticket_ids(user):
    """A ``values('pk')`` subquery of the visible tickets, or ``None`` for staff.

    Use it to scope child models (``ticket_id__in=...``) without a join."""
    if is_staff_user(user):
        return None
    return Ticket.objects.filter(ticket_visibility_q(user)).values('pk')


def scope_to_visible_tickets(user, qs, field='ticket_id'):
    """Limit a child-model queryset (comments, attachments, ...) to rows whose
    ticket ``user`` may see."""
    ids = visible_ticket_ids(user)
    if ids is None:
        return qs
    return qs.filter(**{f'{field}__in': ids})


def can_view_ticket(user, ticket):
    """Whether ``user`` may see ``ticket`` (an instance or a primary key)."""
    if is_staff_user(user):
        return True
    pk = getattr(ticket, 'pk', ticket)
    return visible_tickets(user).filter(pk=pk).exists()


def user_can_edit_ticket(user, ticket):
    """Non-staff users may edit tickets they created, are assigned to (primary
    or in ``assigned_users``), or that are assigned to one of their groups."""
    if is_staff_user(user):
        return True
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if ticket.created_by_id == user.id or ticket.assigned_to_id == user.id:
        return True
    return Ticket.objects.filter(pk=ticket.pk).filter(
        Q(Exists(TicketAssignment.objects.filter(ticket_id=OuterRef('pk'), user_id=user.id)))
        | Q(Exists(Ticket.assigned_groups.through.objects.filter(
            ticket_id=OuterRef('pk'),
            tenantgroup_id__in=_user_group_ids(user, active_only=False),
        )))
    ).exists()


def user_can_transfer(user, ticket):
    """Moving a ticket to another board needs edit rights (or staff)."""
    return user_can_edit_ticket(user, ticket)


def check_column_write(user, column, ticket=None):
    """Raise ``PermissionDenied`` unless ``user`` may put ``ticket`` (``None``
    on create) into ``column`` (plan E4).

    - a move within the ticket's current board is always allowed here (the
      caller's right to see or edit the ticket is checked by the view);
    - otherwise the column's board must be accessible;
    - moving to another board needs ``user_can_transfer``.
    """
    from rest_framework.exceptions import PermissionDenied

    if column is None or is_staff_user(user):
        return
    if ticket is not None and ticket.pk is not None:
        old_board_id = None
        if ticket.column_id:
            old_board_id = (
                ticket.column.board_id if 'column' in ticket._state.fields_cache and ticket.column
                else Ticket.objects.filter(pk=ticket.pk).values_list('column__board_id', flat=True).first()
            )
        if old_board_id is not None and old_board_id == column.board_id:
            # A move within the ticket's own board exposes nothing new: the
            # caller already sees (moves) or edits (PATCH) the ticket. Users
            # who see a ticket only through an assignment, on a board they are
            # not a member of, could always move it between that board's
            # columns; E2's only intended narrowing is inactive groups.
            return
    if column.board_id is None or not can_access_board(user, column.board_id):
        raise PermissionDenied('You do not have access to the board of this column.')
    if ticket is not None and ticket.pk is not None:
        if not user_can_transfer(user, ticket):
            raise PermissionDenied('You do not have permission to move this ticket to another board.')


# ── per-viewer permissions ──────────────────────────────────────────────────

def user_can_view_customer(user, request=None):
    """``Ticket.customer`` is shown/settable only with ``social_integrations``.
    Cached on ``request`` when one is given."""
    if request is not None:
        cached = getattr(request, '_tickets_can_view_customer', None)
        if cached is not None:
            return cached
    allowed = bool(
        user is not None and getattr(user, 'is_authenticated', False)
        and user.has_feature('social_integrations')
    )
    if request is not None:
        try:
            request._tickets_can_view_customer = allowed
        except AttributeError:
            pass
    return allowed


def user_can_manage_payments(user):
    return is_staff_user(user) or bool(user is not None and user.has_permission('access_orders'))


def user_can_delete_ticket(user, ticket):
    """Ticket deletion is staff-only on the server.

    ``TicketViewSet.destroy`` also checks the tenant's
    ``only_superadmin_can_delete_tickets`` flag, but ``TicketPermission``
    already refuses DELETE to every non-staff user, so the flag never widens
    access (pre-existing behaviour, kept as is). This reports exactly what the
    server allows."""
    return is_staff_user(user)


def ticket_viewer_permissions(user, ticket, request=None):
    """What ``user`` may do with ``ticket``; the web hides controls from this
    instead of re-implementing the rules (WS10). Assumes ``ticket`` is visible.
    """
    staff = is_staff_user(user)
    can_edit = user_can_edit_ticket(user, ticket)
    return {
        'can_edit': can_edit,
        'can_delete': user_can_delete_ticket(user, ticket),
        # move_to_column lets anyone who can see the ticket move it into a
        # closed column (E4 status quo, confirmed in §13 Q1).
        'can_move_to_closed': True,
        'can_assign': staff,
        'can_change_groups': staff,
        'can_transfer': staff or can_edit,
        'can_manage_payments': user_can_manage_payments(user),
        'can_view_customer': user_can_view_customer(user, request),
    }
