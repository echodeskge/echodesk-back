"""Board broadcast delivery (E11, E12) and history muting (E15).

Every board WebSocket event the tickets app sends goes through
``queue_event``. It is delivered only once the surrounding transaction has
committed (a rolled-back write never broadcasts), and inside an HTTP request
all events of that request are coalesced into one ``BroadcastBatch`` that
``TicketBroadcastMiddleware`` flushes at the end:

1. ``queue_event(e)``:
   - batch open: ``on_commit(batch.add(e))`` inside ``atomic()``, otherwise
     ``batch.add(e)`` right away;
   - no batch (Celery, admin actions outside a request, the shell):
     ``on_commit(send(e))`` inside ``atomic()``, otherwise send right away.
2. ``batch.add`` merges events keyed by ``(schema, board_id, ticket_id,
   type)``. ``ticket_updated`` events for one ticket merge their ``changes``
   (first ``old``, last ``new``) and carry one fresh ``card``.
3. The middleware flushes in ``finally`` (whatever the status, since only
   committed events are in the batch); inside an outer ``atomic()`` it
   registers the flush as one more on-commit callback, which runs after the
   earlier ``add`` callbacks (on-commit callbacks run first in, first out).
4. An ``add`` after the flush sends directly.
5. Sending goes through the existing ``users.consumers.broadcast_ticket_*``
   helpers, so they stay the single place that builds group messages.

Cards (``ticket_created`` / ``ticket_updated`` / ``ticket_moved``) are built
at send time from ``lite_card_queryset`` (R9), so a merged event carries the
ticket's final state and the card is built once per event.
"""
import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar

from asgiref.sync import async_to_sync
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction

logger = logging.getLogger(__name__)

_batch_var = ContextVar('tickets_broadcast_batch', default=None)
_mutation_var = ContextVar('tickets_client_mutation_id', default=None)
_request_var = ContextVar('tickets_request', default=None)
_muted_var = ContextVar('tickets_history_muted', default=False)

MUTATION_HEADER = 'HTTP_X_CLIENT_MUTATION'
_MUTATION_ID_RE = re.compile(r'^[A-Za-z0-9._:-]{1,64}$')

CARD_EVENTS = ('ticket_created', 'ticket_updated', 'ticket_moved')


# ── request context ─────────────────────────────────────────────────────────

def parse_client_mutation_id(raw):
    """The ``X-Client-Mutation`` header value, or None when it is missing or
    not a plausible id (at most 64 characters of ``[A-Za-z0-9._:-]``)."""
    if not raw:
        return None
    raw = str(raw).strip()
    return raw if _MUTATION_ID_RE.match(raw) else None


def current_client_mutation_id():
    return _mutation_var.get()


def current_request_user():
    """The authenticated user of the request being served, or None (Celery,
    shell, anonymous). DRF writes the authenticated user back onto the
    Django request, so token-authenticated API calls are covered too."""
    request = _request_var.get()
    if request is None:
        return None
    user = getattr(request, 'user', None)
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    return user


def actor_for(instance=None):
    """Who made a change: the view's ``_current_user`` tag on the instance,
    else the request user, else None."""
    user = getattr(instance, '_current_user', None) if instance is not None else None
    return user or current_request_user()


@contextmanager
def request_context(request, client_mutation_id=None):
    """Open a broadcast batch for one request (used by the middleware)."""
    batch = BroadcastBatch()
    tokens = (
        _batch_var.set(batch),
        _mutation_var.set(client_mutation_id),
        _request_var.set(request),
    )
    try:
        yield batch
    finally:
        try:
            if connection.in_atomic_block:
                transaction.on_commit(batch.flush)
            else:
                batch.flush()
        finally:
            _request_var.reset(tokens[2])
            _mutation_var.reset(tokens[1])
            _batch_var.reset(tokens[0])


# ── history muting ──────────────────────────────────────────────────────────

@contextmanager
def history_muted():
    """Suppress the automatic TicketHistory rows (tags, assignees, groups,
    attachments, checklist, field changes) while a ticket is being created;
    the creating code writes one ``created`` row itself."""
    token = _muted_var.set(True)
    try:
        yield
    finally:
        _muted_var.reset(token)


def is_history_muted():
    return _muted_var.get()


# ── payload helpers ─────────────────────────────────────────────────────────

def json_safe(data):
    """Plain dicts / lists / str / numbers only (channel layers use msgpack)."""
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


def card_payload(ticket):
    """Lite card for an already-loaded instance (a bare instance gets the
    count defaults)."""
    from .serializers import TicketCardSerializer
    return json_safe(TicketCardSerializer(ticket).data)


def build_card(ticket_id, fallback=None):
    """Lite card for ``ticket_id`` from ``lite_card_queryset`` (the kanban
    ``?lite=1`` shape). Falls back to ``fallback`` (an instance) when the row
    is gone; None when there is neither."""
    from .querysets import lite_card_queryset
    ticket = lite_card_queryset().filter(pk=ticket_id).first()
    if ticket is None:
        ticket = fallback
    if ticket is None:
        return None
    return card_payload(ticket)


# ── events ──────────────────────────────────────────────────────────────────

class Event:
    __slots__ = ('schema', 'board_id', 'ticket_id', 'type', 'data', 'client_mutation_id')

    def __init__(self, type, schema, board_id, ticket_id, data, client_mutation_id):
        self.type = type
        self.schema = schema
        self.board_id = board_id
        self.ticket_id = ticket_id
        self.data = data
        self.client_mutation_id = client_mutation_id

    @property
    def key(self):
        key = (self.schema, self.board_id, self.ticket_id, self.type)
        # Distinct activity kinds / comments are distinct events
        if self.type == 'ticket_activity':
            key += (self.data.get('kind'),)
        elif self.type == 'ticket_commented':
            key += ((self.data.get('comment') or {}).get('id'),)
        return key

    def merge(self, newer):
        """Fold a newer event with the same key into this one."""
        if self.type == 'ticket_updated':
            changes = dict(self.data.get('changes') or {})
            for field, change in (newer.data.get('changes') or {}).items():
                if field in changes:
                    changes[field] = {'old': changes[field].get('old'), 'new': change.get('new')}
                else:
                    changes[field] = change
            self.data = {**self.data, **newer.data, 'changes': changes}
        elif self.type == 'ticket_moved':
            from_column_id = self.data.get('from_column_id')
            self.data = {**self.data, **newer.data, 'from_column_id': from_column_id}
        else:
            self.data = {**self.data, **newer.data}
        self.client_mutation_id = newer.client_mutation_id or self.client_mutation_id


def _user_id(user):
    return getattr(user, 'pk', None)


def _with_schema(schema, fn):
    if schema and getattr(connection, 'schema_name', None) != schema:
        from tenant_schemas.utils import schema_context
        with schema_context(schema):
            return fn()
    return fn()


def _card_for(event):
    card = event.data.get('card')
    if card is not None:
        return card
    return build_card(event.ticket_id, fallback=event.data.get('instance'))


def _send_now(event):
    """Deliver one event through the users.consumers helpers. Never raises."""
    try:
        _with_schema(event.schema, lambda: _dispatch(event))
    except Exception:  # broadcasting must never break the request
        logger.exception('[tickets.realtime] Error broadcasting %s for ticket %s', event.type, event.ticket_id)


def _dispatch(event):
    from users import consumers

    d = event.data
    common = {'tenant_schema': event.schema, 'board_id': event.board_id}
    mid = event.client_mutation_id

    if event.type == 'ticket_created':
        created_by = d.get('created_by')
        card = _card_for(event)
        if card is None:
            return
        async_to_sync(consumers.broadcast_ticket_created)(
            **common, ticket_data=card, created_by=created_by,
            client_mutation_id=mid, actor_id=_user_id(created_by),
        )
    elif event.type == 'ticket_updated':
        updated_by = d.get('updated_by')
        async_to_sync(consumers.broadcast_ticket_updated)(
            **common, ticket_id=event.ticket_id, changes=json_safe(d.get('changes') or {}),
            updated_by=updated_by,
            card=_card_for(event), actor_id=_user_id(updated_by), client_mutation_id=mid,
        )
    elif event.type == 'ticket_moved':
        updated_by = d.get('updated_by')
        async_to_sync(consumers.broadcast_ticket_moved)(
            **common, ticket_id=event.ticket_id,
            from_column_id=d.get('from_column_id'), to_column_id=d.get('to_column_id'),
            position=d.get('position'), updated_by=updated_by,
            card=_card_for(event), actor_id=_user_id(updated_by), client_mutation_id=mid,
        )
    elif event.type == 'ticket_deleted':
        async_to_sync(consumers.broadcast_ticket_deleted)(
            **common, ticket_id=event.ticket_id, deleted_by=d.get('deleted_by'),
            reason=d.get('reason', 'deleted'), to_board_id=d.get('to_board_id'),
            client_mutation_id=mid,
        )
    elif event.type == 'ticket_commented':
        async_to_sync(consumers.broadcast_ticket_commented)(
            **common, ticket_id=event.ticket_id, comment=d.get('comment'),
            comments_count=d.get('comments_count'), user=d.get('user'),
        )
    elif event.type == 'ticket_activity':
        async_to_sync(consumers.broadcast_ticket_activity)(
            event.schema, event.board_id, {
                'ticket_id': event.ticket_id,
                'kind': d.get('kind'),
                'actor_id': d.get('actor_id'),
                'client_mutation_id': mid,
            },
        )
    else:  # pragma: no cover - programming error
        raise ValueError(f'Unknown board event type {event.type!r}')


class BroadcastBatch:
    """The events of one request, merged and sent once at the end."""

    def __init__(self):
        self._events = {}  # key -> Event, insertion ordered
        self.flushed = False

    def __len__(self):
        return len(self._events)

    def add(self, event):
        if self.flushed:
            _send_now(event)
            return
        ticket_key = (event.schema, event.board_id, event.ticket_id)
        if event.type == 'ticket_updated' and ticket_key + ('ticket_created',) in self._events:
            return  # the created card (built at flush) already shows it
        if event.type == 'ticket_created':
            self._events.pop(ticket_key + ('ticket_updated',), None)
        existing = self._events.get(event.key)
        if existing is None:
            self._events[event.key] = event
        else:
            existing.merge(event)

    def flush(self):
        if self.flushed:
            return
        self.flushed = True
        events, self._events = list(self._events.values()), {}
        for event in events:
            _send_now(event)


def queue_event(type, *, board_id, ticket_id, schema=None, **data):
    """Queue one board event (see the module docstring for the delivery
    rules). ``data`` holds the type's fields: ``created_by`` / ``updated_by``
    / ``deleted_by`` / ``user`` (User or None), ``changes``, ``from_column_id``,
    ``to_column_id``, ``position``, ``reason``, ``to_board_id``, ``comment``,
    ``comments_count``, ``kind``, ``actor_id``, and optionally ``card`` (else
    built at send time) or ``instance`` (card fallback when the row is gone).
    No-op without a board."""
    if not board_id:
        return
    event = Event(
        type,
        schema or getattr(connection, 'schema_name', 'public'),
        board_id,
        ticket_id,
        data,
        current_client_mutation_id(),
    )
    batch = _batch_var.get()
    if batch is not None:
        if connection.in_atomic_block:
            transaction.on_commit(lambda: batch.add(event))
        else:
            batch.add(event)
    elif connection.in_atomic_block:
        transaction.on_commit(lambda: _send_now(event))
    else:
        _send_now(event)
