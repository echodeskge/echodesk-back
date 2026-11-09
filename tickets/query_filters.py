"""Shared ticket filter and ordering parameters (plan A4).

One parser for every read endpoint that narrows tickets: the ticket list
(``tickets/``), the kanban (``boards/{id}/kanban_board/``), ``view_counts``,
``column_counts`` and ``navigation``. None of these parameters are documented
in OpenAPI on the existing operations (E10): the generated clients take their
query parameters positionally, so adding any would shift their arguments.

Filters never widen visibility: they are applied on top of a queryset that is
already limited by ``tickets.visibility``.

Text search: ``q`` and ``search`` are aliases on every endpoint (``q`` wins
when both are sent). The web sends ``search`` to the DRF list and ``q`` to the
kanban, counts and navigation; either works everywhere.

Everything that touches a child table is an ``Exists`` subquery, so a ticket
matching two tags (or two assignees) still yields one row (RM3).
"""
import datetime
import re

from django.db.models import Case, Exists, F, IntegerField, OuterRef, Q, Value, When
from rest_framework.exceptions import ValidationError

from .dates import end_of_iso_week, tickets_today
from .models import Tag, Ticket, TicketAssignment

PRIORITY_VALUES = ('low', 'medium', 'high', 'critical')
PRIORITY_RANK = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
DUE_VALUES = ('overdue', 'week', 'none')
TRUE_VALUES = ('1', 'true', 'yes', 'on')
FALSE_VALUES = ('0', 'false', 'no', 'off')

# Accepted ``ordering`` values. ``priority``/``-priority`` are the pre-redesign
# list values (alphabetical on the stored string) and keep working.
ORDERING_VALUES = (
    'priority_rank', '-priority_rank',
    'due_date', '-due_date',
    'created_at', '-created_at',
    'updated_at', '-updated_at',
    'position_in_column',
    'priority', '-priority',
)

_ID_RE = re.compile(r'^#?(\d{1,18})$')


# ── parsing helpers ──────────────────────────────────────────────────────────

def _param(params, name):
    value = params.get(name)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _csv(raw):
    return [part.strip() for part in str(raw).split(',') if part.strip()]


def _csv_ints(name, raw):
    out = []
    for part in _csv(raw):
        try:
            out.append(int(part))
        except ValueError:
            raise ValidationError({name: f'Expected a comma-separated list of ids, got {part!r}.'})
    return out


def _bool(name, raw):
    value = raw.lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ValidationError({name: 'Expected true or false.'})


def _date(name, raw):
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        raise ValidationError({name: 'Expected an ISO date (YYYY-MM-DD).'})


def search_term(params):
    """The text search term: ``q``, else ``search`` (aliases, see module doc)."""
    return _param(params, 'q') or _param(params, 'search')


# ── reusable Q objects ───────────────────────────────────────────────────────

def open_ticket_q():
    """Tickets whose column is not a closed-status column (or that have no column)."""
    return Q(column__isnull=True) | Q(column__is_closed_status=False)


def assignee_q(user_ids):
    """Assigned to any of ``user_ids``: in ``assigned_users`` (TicketAssignment)
    or the legacy primary ``assigned_to``."""
    return (
        Q(assigned_to_id__in=user_ids)
        | Q(Exists(TicketAssignment.objects.filter(ticket_id=OuterRef('pk'), user_id__in=user_ids)))
    )


def unassigned_q():
    """No primary assignee and no TicketAssignment row."""
    return Q(assigned_to__isnull=True) & ~Q(Exists(TicketAssignment.objects.filter(ticket_id=OuterRef('pk'))))


def urgent_q():
    return Q(priority__in=('critical', 'high'))


def due_q(value, today=None):
    """``due=overdue|week|none``. ``overdue`` and ``week`` count open tickets
    only; ``week`` runs to the end of the current ISO week and includes the
    overdue ones."""
    today = today or tickets_today()
    if value == 'overdue':
        return Q(due_date__lt=today) & open_ticket_q()
    if value == 'week':
        return Q(due_date__lte=end_of_iso_week(today)) & open_ticket_q()
    if value == 'none':
        return Q(due_date__isnull=True)
    raise ValidationError({'due': f'Expected one of {", ".join(DUE_VALUES)}.'})


def text_search_q(term):
    """Title, description (HTML included), any tag name (``Exists``, never a
    join) and, for ``123``/``#123``, the ticket id."""
    tag_through = Ticket.tags.through
    q = (
        Q(title__icontains=term)
        | Q(description__icontains=term)
        | Q(Exists(tag_through.objects.filter(
            ticket_id=OuterRef('pk'),
            tag_id__in=Tag.objects.filter(name__icontains=term).values('pk'),
        )))
    )
    match = _ID_RE.match(term)
    if match:
        q |= Q(pk=int(match.group(1)))
    return q


# ── filters ──────────────────────────────────────────────────────────────────

def ticket_filter_q(params, user, *, include_board=True, today=None):
    """The combined A4 filter for ``params`` (a QueryDict or dict) as one ``Q``.

    Raises ``ValidationError`` (400) on malformed values. ``include_board``
    is False where the board comes from the URL (kanban)."""
    q = Q()

    if include_board:
        board = _param(params, 'board')
        if board is not None:
            try:
                q &= Q(column__board_id=int(board))
            except ValueError:
                raise ValidationError({'board': 'Expected a board id.'})

    assignee = _param(params, 'assignee')
    if assignee is not None:
        ids = []
        for part in _csv(assignee):
            if part.lower() == 'me':
                ids.append(user.id)
            else:
                try:
                    ids.append(int(part))
                except ValueError:
                    raise ValidationError({'assignee': "Expected 'me' or a comma-separated list of user ids."})
        if ids:
            q &= assignee_q(ids)

    unassigned = _param(params, 'unassigned')
    if unassigned is not None and _bool('unassigned', unassigned):
        q &= unassigned_q()

    priorities = _param(params, 'priority__in')
    if priorities is not None:
        values = [p.lower() for p in _csv(priorities)]
        bad = [p for p in values if p not in PRIORITY_VALUES]
        if bad:
            raise ValidationError({'priority__in': f'Unknown priority {bad[0]!r}; expected {", ".join(PRIORITY_VALUES)}.'})
        if values:
            q &= Q(priority__in=values)

    tags = _param(params, 'tags__in')
    if tags is not None:
        tag_ids = _csv_ints('tags__in', tags)
        if tag_ids:
            q &= Q(Exists(Ticket.tags.through.objects.filter(ticket_id=OuterRef('pk'), tag_id__in=tag_ids)))

    due = _param(params, 'due')
    if due is not None:
        q &= due_q(due.lower(), today=today)

    due_before = _param(params, 'due_before')
    if due_before is not None:
        q &= Q(due_date__lte=_date('due_before', due_before))

    due_after = _param(params, 'due_after')
    if due_after is not None:
        q &= Q(due_date__gte=_date('due_after', due_after))

    is_closed = _param(params, 'is_closed')
    if is_closed is not None:
        q &= Q(column__is_closed_status=True) if _bool('is_closed', is_closed) else open_ticket_q()

    term = search_term(params)
    if term is not None:
        q &= text_search_q(term)

    return q


def apply_ticket_filters(qs, params, user, *, include_board=True, today=None):
    """``qs`` narrowed by the A4 filters in ``params``."""
    q = ticket_filter_q(params, user, include_board=include_board, today=today)
    return qs.filter(q) if q else qs


def has_ticket_filters(params, *, include_board=True):
    """Whether ``params`` carries any A4 filter (ordering does not count)."""
    names = ['assignee', 'unassigned', 'priority__in', 'tags__in', 'due',
             'due_before', 'due_after', 'is_closed', 'q', 'search']
    if include_board:
        names.append('board')
    return any(_param(params, n) is not None for n in names)


# ── ordering ─────────────────────────────────────────────────────────────────

def priority_rank_expression():
    """critical 0, high 1, medium 2, low 3 (anything else 4)."""
    return Case(
        *[When(priority=p, then=Value(rank)) for p, rank in PRIORITY_RANK.items()],
        default=Value(4),
        output_field=IntegerField(),
    )


def parse_ordering(raw):
    """The validated ``ordering`` value, or ``None`` when absent or unknown
    (unknown values fall back to the endpoint's default, as DRF's
    ``OrderingFilter`` does)."""
    if raw is None:
        return None
    value = str(raw).strip()
    return value if value in ORDERING_VALUES else None


def ordering_expressions(value):
    """The ``order_by`` expressions for one ordering value (no tie-break)."""
    if value is None:
        return []
    desc = value.startswith('-')
    field = value.lstrip('-')
    if field == 'priority_rank':
        expr = priority_rank_expression()
        return [expr.desc() if desc else expr.asc()]
    if field == 'due_date':
        # Tickets without a due date sort last in both directions.
        return [F('due_date').desc(nulls_last=True) if desc else F('due_date').asc(nulls_last=True)]
    if field == 'position_in_column':
        return [F('position_in_column').asc(), F('created_at').desc()]
    return [F(field).desc() if desc else F(field).asc()]


# Kanban order within a column (R14); navigation uses the same keys.
MANUAL_ORDER = [F('position_in_column').asc(), F('created_at').desc(), F('id').desc()]


def column_ticket_order(raw_ordering):
    """Order of the tickets inside one kanban column: manual by default,
    otherwise the requested key, always ending ``-id``."""
    value = parse_ordering(raw_ordering)
    if value is None or value == 'position_in_column':
        return list(MANUAL_ORDER)
    return ordering_expressions(value) + [F('id').desc()]


def list_ticket_order(raw_ordering, *, group_by_column=False, default='-created_at'):
    """Order of the ticket list: optionally grouped by column (column position,
    then column id), then the requested key (default ``-created_at``), then
    ``-id``."""
    value = parse_ordering(raw_ordering) or default
    keys = []
    if group_by_column:
        keys += [F('column__position').asc(nulls_last=True), F('column_id').asc(nulls_last=True)]
    return keys + ordering_expressions(value) + [F('id').desc()]
