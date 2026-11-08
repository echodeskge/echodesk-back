"""Personal ticket timer (TicketTimeEntry): service functions and receivers.

Rules (plan E32):
- at most one running entry per user (partial unique constraint
  ``tickets_one_running_timer_per_user``); a second start elsewhere is a 409
  unless ``switch`` is set, which stops the other entry atomically;
- start on a ticket in an ``is_closed_status`` column is a 409;
- ``stop_current`` stops the caller's own running entry whatever ticket it is
  on, with no visibility check, so nobody is stuck with a timer;
- saving a ticket into a closed column stops every running entry on it;
- deleting a user stops their running entry first (rows then keep
  ``user=None`` through SET_NULL).

Every change broadcasts ``ticket_activity {kind:'timer'}`` to the ticket's
board after commit.
"""
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import Min, Q, Sum
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone

from .models import Ticket, TicketTimeEntry

User = get_user_model()

ENTRIES_LIMIT = 50


class TimerConflict(Exception):
    """A 409: ``payload`` is the response body (always carries ``code``)."""

    def __init__(self, payload):
        super().__init__(payload.get('code'))
        self.payload = payload


# ── visibility ──────────────────────────────────────────────────────────────

def _is_staff(user):
    return bool(user.is_staff or user.is_superuser)


def visible_ticket_ids(user):
    """A ``values('pk')`` subquery of the tickets ``user`` may see, or None
    for staff (everything). Built as a subquery so the M2M joins inside
    ``ticket_visibility_q`` never fan out the outer query."""
    if _is_staff(user):
        return None
    from .views import ticket_visibility_q  # lazy: views imports half the app
    return Ticket.objects.filter(ticket_visibility_q(user)).values('pk')


def visible_tickets(user):
    ids = visible_ticket_ids(user)
    qs = Ticket.objects.all()
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    return qs


def visible_entries(user):
    ids = visible_ticket_ids(user)
    qs = TicketTimeEntry.objects.all()
    if ids is not None:
        qs = qs.filter(ticket_id__in=ids)
    return qs


def _ticket_title_if_visible(user, ticket_id):
    return visible_tickets(user).filter(pk=ticket_id).values_list('title', flat=True).first()


# ── broadcasting ────────────────────────────────────────────────────────────

def _queue_timer_broadcast(ticket_id, board_id, actor_id):
    """Send ``ticket_activity {kind:'timer'}`` after the transaction commits
    (immediately in autocommit). Never raises."""
    if not board_id:
        return
    schema = getattr(connection, 'schema_name', 'public')
    payload = {
        'ticket_id': ticket_id,
        'kind': 'timer',
        'actor_id': actor_id,
        'client_mutation_id': None,
    }

    def send():
        try:
            from users.consumers import broadcast_ticket_activity
            async_to_sync(broadcast_ticket_activity)(schema, board_id, payload)
        except Exception as e:  # broadcasting must never break the request
            print(f"[Timer] Error broadcasting ticket_activity: {e}")

    transaction.on_commit(send)


def _board_id_of(ticket_id):
    return (
        Ticket.objects.filter(pk=ticket_id)
        .values_list('column__board_id', flat=True)
        .first()
    )


def _board_id_for(ticket):
    if ticket.column_id is None:
        return None
    if 'column' in ticket._state.fields_cache:
        return ticket.column.board_id if ticket.column else None
    return _board_id_of(ticket.pk)


# ── service functions ───────────────────────────────────────────────────────

def _locked_running(user):
    """The caller's running entry, row-locked (call inside atomic())."""
    return (
        TicketTimeEntry.objects.select_for_update()
        .filter(user=user, ended_at__isnull=True)
        .first()
    )


def _running_payload(user, entry):
    return {
        'code': 'timer_running',
        'running': {
            'id': entry.id,
            'ticket_id': entry.ticket_id,
            'ticket_title': _ticket_title_if_visible(user, entry.ticket_id),
            'started_at': entry.started_at,
        },
    }


def _is_closed(ticket):
    return bool(ticket.column_id and ticket.column.is_closed_status)


def start(user, ticket, switch=False):
    """Start ``user``'s timer on ``ticket``. Returns ``(entry, created)``.

    Idempotent when already running on this ticket (created=False). Raises
    TimerConflict for ``ticket_closed`` and for ``timer_running`` (another
    ticket, ``switch`` not set, or a concurrent start won the race).
    """
    if _is_closed(ticket):
        raise TimerConflict({'code': 'ticket_closed'})

    with transaction.atomic():
        running = _locked_running(user)
        if running is not None and running.ticket_id == ticket.pk:
            return running, False

        now = timezone.now()
        stopped = None
        if running is not None:
            if not switch:
                raise TimerConflict(_running_payload(user, running))
            running.stop(now=now)
            stopped = running

        try:
            with transaction.atomic():  # savepoint: a lost race leaves the outer txn usable
                entry = TicketTimeEntry.objects.create(ticket=ticket, user=user, started_at=now)
        except IntegrityError:
            winner = (
                TicketTimeEntry.objects.filter(user=user, ended_at__isnull=True).first()
            )
            if winner is not None and winner.ticket_id == ticket.pk:
                return winner, False
            if winner is None:
                raise TimerConflict({'code': 'timer_running', 'running': None})
            raise TimerConflict(_running_payload(user, winner))

        if stopped is not None:
            _queue_timer_broadcast(stopped.ticket_id, _board_id_of(stopped.ticket_id), user.pk)
        _queue_timer_broadcast(ticket.pk, _board_id_for(ticket), user.pk)
    return entry, True


def stop_for_ticket(user, ticket):
    """Stop ``user``'s running entry on ``ticket``; 409 when none."""
    with transaction.atomic():
        entry = (
            TicketTimeEntry.objects.select_for_update()
            .filter(user=user, ticket=ticket, ended_at__isnull=True)
            .first()
        )
        if entry is None:
            raise TimerConflict({'code': 'timer_not_running'})
        entry.stop()
        _queue_timer_broadcast(ticket.pk, _board_id_for(ticket), user.pk)
    return entry


def stop_current(user):
    """Stop ``user``'s running entry on any ticket (no visibility check)."""
    with transaction.atomic():
        entry = _locked_running(user)
        if entry is None:
            raise TimerConflict({'code': 'timer_not_running'})
        entry.stop()
        _queue_timer_broadcast(entry.ticket_id, _board_id_of(entry.ticket_id), user.pk)
    return entry


def current(user):
    """The caller's running entry (any ticket) or None."""
    return (
        TicketTimeEntry.objects.select_related('user')
        .filter(user=user, ended_at__isnull=True)
        .first()
    )


def _elapsed(entry_started_at, now):
    return max(0, int((now - entry_started_at).total_seconds()))


def totals(ticket, user):
    """The ``GET tickets/{id}/timer/`` payload (minus serialisation of the
    entries). Three queries whatever the number of entries."""
    now = timezone.now()
    rows = (
        TicketTimeEntry.objects.filter(ticket=ticket)
        .values('user_id', 'user__first_name', 'user__last_name', 'user__email')
        .annotate(
            done=Sum('duration_seconds', filter=Q(ended_at__isnull=False)),
            running_since=Min('started_at', filter=Q(ended_at__isnull=True)),
        )
    )
    by_user = []
    total = 0
    mine = 0
    for row in rows:
        seconds = row['done'] or 0
        if row['running_since'] is not None:
            seconds += _elapsed(row['running_since'], now)
        total += seconds
        if row['user_id'] is not None and row['user_id'] == user.pk:
            mine += seconds
        by_user.append({
            'user': None if row['user_id'] is None else user_brief(
                row['user_id'], row['user__first_name'], row['user__last_name'], row['user__email'],
            ),
            'seconds': seconds,
        })
    by_user.sort(key=lambda r: (-r['seconds'], r['user']['name'] if r['user'] else ''))

    entries = list(
        TicketTimeEntry.objects.filter(ticket=ticket).select_related('user')
        .order_by('-started_at', '-id')[:ENTRIES_LIMIT]
    )
    running = next(
        (e for e in entries if e.user_id == user.pk and e.ended_at is None), None
    )
    if running is None:
        # Older than the latest 50 (a long-running timer on a busy ticket).
        running = (
            TicketTimeEntry.objects.select_related('user')
            .filter(ticket=ticket, user=user, ended_at__isnull=True).first()
            if any(r['user_id'] == user.pk and r['running_since'] for r in rows) else None
        )
    return {
        'running': running,
        'my_total_seconds': mine,
        'total_seconds': total,
        'server_now': now,
        'by_user': by_user,
        'entries': entries,
    }


def user_brief(user_id, first_name, last_name, email):
    name = f'{first_name or ""} {last_name or ""}'.strip() or (email or '')
    parts = [p for p in name.replace('@', ' ').split() if p]
    if len(parts) >= 2 and (first_name or last_name):
        initials = (parts[0][0] + parts[1][0]).upper()
    else:
        initials = (parts[0][:2] if parts else '?').upper()
    return {'id': user_id, 'name': name, 'initials': initials}


# ── receivers ───────────────────────────────────────────────────────────────

@receiver(post_save, sender=Ticket, dispatch_uid='tickets_timer_stop_on_closed')
def stop_timers_on_closed_column(sender, instance, created, raw=False, **kwargs):
    """A ticket saved into a closed column stops every running entry on it."""
    if created or raw or not instance.column_id:
        return
    cache = instance._state.fields_cache
    if 'column' in cache and cache['column'] is not None and not cache['column'].is_closed_status:
        return  # known open column: no query at all
    running = list(
        TicketTimeEntry.objects.filter(
            ticket_id=instance.pk,
            ended_at__isnull=True,
            ticket__column__is_closed_status=True,
        )
    )
    if not running:
        return
    now = timezone.now()
    for entry in running:
        entry.stop(now=now)
    actor = getattr(instance, '_current_user', None)
    _queue_timer_broadcast(
        instance.pk, _board_id_for(instance), getattr(actor, 'pk', None),
    )


@receiver(pre_delete, sender=User, dispatch_uid='tickets_timer_stop_on_user_delete')
def stop_timers_on_user_delete(sender, instance, **kwargs):
    """Stop a user's running entries before SET_NULL detaches them."""
    try:
        with transaction.atomic():  # savepoint: the public schema has no tickets tables
            running = list(
                TicketTimeEntry.objects.filter(user_id=instance.pk, ended_at__isnull=True)
            )
            now = timezone.now()
            for entry in running:
                entry.stop(now=now)
    except DatabaseError:
        return
    for entry in running:
        _queue_timer_broadcast(entry.ticket_id, _board_id_of(entry.ticket_id), None)
