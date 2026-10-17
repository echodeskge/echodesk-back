"""Missed-call notifications.

``users.module_signals.notify_call_events`` calls
:func:`schedule_missed_call_notification` once, when an inbound call log
*transitions* into ``missed``/``no_answer``. The actual fan-out runs later
(Celery countdown of at least one queue "attempt gap") so that we can see
whether a colleague picked the call up in the meantime. The web softphone
logs one row per agent leg, so one queue call is several rows:

* ``ringall``: every agent's leg rings at the same time;
* ``rrmemory`` (the default) and the other sequential strategies: one leg
  rings, times out, Asterisk waits ``retry`` seconds and rings the next
  agent, so the next leg can be answered well after this one ended.

An unanswered leg is "answered elsewhere" (:func:`find_answered_sibling`)
when another leg of the *same queue attempt* was answered after this leg
started. The attempt is the chain of legs from the same caller (same last
7 digits; same queue when both agents' queues are known) in which each leg
started while an earlier one was ringing, or within the attempt gap
(``Queue.timeout_seconds`` + ``retry`` + ring slack, 60 s by default) after
the chain's latest leg ended. Legs answered *before* this leg started (a
call a colleague had already taken, e.g. before blind-transferring it to
us) are not part of the attempt and never match.

An answered-elsewhere leg is re-labelled ``cancelled`` (counted neither as
answered nor as missed) and nobody is notified. The relabel happens in
``end_call`` (ringall: the colleague already answered), when the sibling
leg becomes ``answered`` (:func:`relabel_legs_answered_elsewhere`;
rrmemory: our leg had already timed out), and again in the delayed task,
which re-checks at send time. The task is queued only after the saving
transaction commits.

Recipients, in order:
1. the call's ``handled_by`` user (the agent whose phone rang);
2. otherwise the members of the queue (or the extension owner) that the
   dialled DID routes to through an active ``InboundRoute``;
3. otherwise the tenant's PBX admins (staff, superusers and
   ``manage_settings`` holders, as :func:`crm.permissions.is_pbx_admin`
   defines them), at most 10.
"""

import logging
from datetime import timedelta

from django.conf import settings

from .call_status import ANSWERED_ELSEWHERE_STATUS, FINAL_STATUSES, MISSED_STATUSES, last_digits

logger = logging.getLogger(__name__)

# Minimum seconds to wait before notifying; the real delay is at least one
# attempt gap (see schedule_missed_call_notification).
MISSED_CALL_NOTIFY_DELAY = getattr(settings, 'MISSED_CALL_NOTIFY_DELAY', 15)
# How often the delayed task may postpone itself while the queue attempt is
# still ringing a colleague.
MISSED_CALL_MAX_DEFERS = 5
# A leg that started up to this long before ours counts as ringing at the
# same time when it was still live when ours started (ringall).
SIBLING_WINDOW = timedelta(seconds=120)
# Clock/latency slack when comparing one leg's timestamps with another's.
SIBLING_SLACK = timedelta(seconds=5)
# A browser may log its ringing leg a moment after a colleague's ring-all
# leg was already answered. A leg answered more than this long before ours
# was logged was taken before ours rang (e.g. then blind-transferred to us)
# and is not part of our queue attempt.
RING_START_TOLERANCE = timedelta(seconds=2)
# Asterisk ``retry`` (seconds before the queue rings the next agent); the
# sync layer writes 5 for every queue (crm.asterisk_sync._sync_queue_impl).
QUEUE_RETRY_SECONDS = 5
# Browser / network slack on top of timeout + retry before the next leg is
# logged.
QUEUE_RING_SLACK_SECONDS = 25
# Attempt gap when the agent is in no known queue (the defaults
# Queue.timeout_seconds 30 + retry 5 + slack 25).
DEFAULT_ATTEMPT_GAP = timedelta(seconds=60)
# How long a caller may wait in a queue when no queue is known (Queue default).
DEFAULT_MAX_WAIT = timedelta(seconds=300)
_MAX_CANDIDATES = 200


def _agent_queue_map(user_ids):
    """{user_id: {queue_id, ...}} from the active queue memberships."""
    from .models import QueueMember

    result = {}
    ids = [uid for uid in set(user_ids) if uid]
    if not ids:
        return result
    rows = QueueMember.objects.filter(
        user_phone_assignment__user_id__in=ids,
        is_active=True,
        queue__is_active=True,
    ).values_list('user_phone_assignment__user_id', 'queue_id')
    for user_id, queue_id in rows:
        result.setdefault(user_id, set()).add(queue_id)
    return result


def _attempt_timing(queue_ids):
    """(attempt gap, longest attempt span) for legs of these queues."""
    from .models import Queue

    rows = list(
        Queue.objects.filter(id__in=queue_ids).values_list('timeout_seconds', 'max_wait_seconds')
    ) if queue_ids else []
    if not rows:
        return DEFAULT_ATTEMPT_GAP, DEFAULT_MAX_WAIT + DEFAULT_ATTEMPT_GAP
    timeout = max(max(t or 0, 0) for t, _ in rows)
    max_wait = max(max(w or 0, 0) for _, w in rows)
    gap = timedelta(seconds=timeout + QUEUE_RETRY_SECONDS + QUEUE_RING_SLACK_SECONDS)
    span = timedelta(seconds=max_wait) if max_wait else DEFAULT_MAX_WAIT
    return gap, span + gap


def attempt_gap_for(call_log):
    """The attempt gap (see the module docstring) for ``call_log``'s agent."""
    queue_map = _agent_queue_map([call_log.handled_by_id])
    gap, _ = _attempt_timing(queue_map.get(call_log.handled_by_id, set()))
    return gap


def _same_caller(qs, call_log, digits):
    if len(digits) >= 7:
        return qs.filter(caller_number__endswith=digits)
    return qs.filter(caller_number=call_log.caller_number)


def missed_call_state(call_log, now=None):
    """Classify an unanswered inbound leg as ``(state, sibling)``.

    ``state`` is ``'answered_elsewhere'`` (``sibling`` is the answered leg),
    ``'pending'`` (the queue attempt may still ring a colleague, so it is
    too early to tell) or ``'missed'``.
    """
    from django.utils import timezone

    from .models import CallLog

    now = now or timezone.now()
    digits = last_digits(call_log.caller_number)
    if not digits or not call_log.started_at or call_log.answered_at:
        return 'missed', None

    start = call_log.started_at
    own_end = call_log.ended_at or now
    queue_map = _agent_queue_map([call_log.handled_by_id])
    own_queues = queue_map.get(call_log.handled_by_id, set())
    gap, span = _attempt_timing(own_queues)

    candidates = list(
        _same_caller(
            CallLog.objects.filter(
                direction='inbound',
                started_at__gte=start - SIBLING_WINDOW,
                started_at__lte=own_end + span,
            ),
            call_log, digits,
        ).exclude(pk=call_log.pk).order_by('started_at', 'pk')[:_MAX_CANDIDATES]
    )
    queue_map.update(_agent_queue_map(
        [c.handled_by_id for c in candidates if c.handled_by_id not in queue_map]
    ))

    chain_end = own_end
    still_ringing = False
    for leg in candidates:
        if leg.answered_at is not None and leg.answered_at < start - RING_START_TOLERANCE:
            # Taken before our leg rang (e.g. the call a colleague answered
            # and then blind-transferred to us): not this attempt.
            continue
        leg_end = leg.ended_at or now
        if leg.started_at < start:
            if leg_end < start - SIBLING_SLACK:
                continue  # over before ours rang
        elif leg.started_at > chain_end + gap:
            break  # a new call from the same number, not the next agent
        leg_queues = queue_map.get(leg.handled_by_id, set())
        if own_queues and leg_queues and not (own_queues & leg_queues):
            continue  # the same number calling another queue
        if leg.answered_at is not None:
            return 'answered_elsewhere', leg
        chain_end = max(chain_end, leg_end)
        if leg.ended_at is None and leg.status not in FINAL_STATUSES:
            still_ringing = True

    if still_ringing or now < chain_end + gap:
        return 'pending', None
    return 'missed', None


def find_answered_sibling(call_log):
    """Another leg of the same queue attempt that somebody answered after
    this leg started (see the module docstring), or None."""
    state, sibling = missed_call_state(call_log)
    return sibling if state == 'answered_elsewhere' else None


def _mark_answered_elsewhere(call_log, sibling):
    from .models import CallEvent, CallLog

    # queryset.update() so the post_save signal does not fire again.
    updated = CallLog.objects.filter(
        pk=call_log.pk, answered_at__isnull=True,
    ).exclude(status=ANSWERED_ELSEWHERE_STATUS).update(status=ANSWERED_ELSEWHERE_STATUS)
    if updated:
        CallEvent.objects.create(
            call_log=call_log,
            event_type='ended',
            metadata={
                'answered_elsewhere': True,
                'answered_call_log_id': sibling.pk,
                'previous_status': call_log.status,
            },
        )
    return bool(updated)


def relabel_legs_answered_elsewhere(answered_leg):
    """``answered_leg`` was just answered: re-label the unanswered legs of
    the same queue attempt that already went ``missed``/``no_answer``
    (rrmemory: agents whose leg timed out before this one rang). Returns
    how many legs were re-labelled."""
    from .models import CallLog

    digits = last_digits(answered_leg.caller_number)
    if (answered_leg.direction != 'inbound' or not digits
            or not answered_leg.answered_at or not answered_leg.started_at):
        return 0
    _, span = _attempt_timing(
        _agent_queue_map([answered_leg.handled_by_id]).get(answered_leg.handled_by_id, set())
    )
    legs = _same_caller(
        CallLog.objects.filter(
            direction='inbound',
            answered_at__isnull=True,
            status__in=MISSED_STATUSES,
            started_at__gte=answered_leg.started_at - span,
            started_at__lte=answered_leg.answered_at + RING_START_TOLERANCE,
        ),
        answered_leg, digits,
    ).exclude(pk=answered_leg.pk).order_by('started_at')[:_MAX_CANDIDATES]
    count = 0
    for leg in legs:
        sibling = find_answered_sibling(leg)
        if sibling is not None and _mark_answered_elsewhere(leg, sibling):
            count += 1
    return count


def _pbx_admin_fallback_recipients(limit=10):
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    from .permissions import is_pbx_admin

    User = get_user_model()
    # Same definition as crm.permissions.is_pbx_admin: staff, superuser or
    # manage_settings (the can_manage_settings flag or the admin role).
    candidates = (
        User.objects.filter(is_active=True)
        .filter(
            Q(is_staff=True) | Q(is_superuser=True)
            | Q(can_manage_settings=True) | Q(role='admin')
        )
        .order_by('id')
    )
    recipients = []
    for user in candidates:
        if is_pbx_admin(user):
            recipients.append(user)
            if len(recipients) >= limit:
                break
    return recipients


def resolve_missed_call_recipients(call_log):
    from .models import InboundRoute, QueueMember

    if call_log.handled_by_id and call_log.handled_by.is_active:
        return [call_log.handled_by]

    did = call_log.recipient_number or ''
    clean = ''.join(ch for ch in did if ch.isdigit())
    route = None
    if did:
        route = InboundRoute.objects.filter(did=did, is_active=True).order_by('priority').first()
        if route is None and len(clean) >= 7:
            route = InboundRoute.objects.filter(
                did__endswith=clean[-9:], is_active=True,
            ).order_by('priority').first()

    users = []
    if route is not None:
        if route.destination_type == 'queue' and route.destination_queue_id:
            members = QueueMember.objects.filter(
                queue_id=route.destination_queue_id,
                is_active=True,
                user_phone_assignment__is_active=True,
                user_phone_assignment__user__is_active=True,
            ).select_related('user_phone_assignment__user')
            seen = set()
            for member in members:
                user = member.user_phone_assignment.user
                if user.pk not in seen:
                    seen.add(user.pk)
                    users.append(user)
        elif route.destination_type == 'extension' and route.destination_extension_id:
            assignment = route.destination_extension
            if assignment.is_active and assignment.user.is_active:
                users.append(assignment.user)
    if users:
        return users

    return _pbx_admin_fallback_recipients()


def missed_call_retry_seconds(call_log_id):
    """Seconds after which the delayed task should look again because the
    queue attempt may still be ringing a colleague, or None when it can
    decide now."""
    from .models import CallLog

    call_log = CallLog.objects.filter(pk=call_log_id).first()
    if call_log is None or call_log.direction != 'inbound' or call_log.status not in MISSED_STATUSES:
        return None
    state, _ = missed_call_state(call_log)
    if state != 'pending':
        return None
    return max(MISSED_CALL_NOTIFY_DELAY, int(attempt_gap_for(call_log).total_seconds()))


def send_missed_call_notifications(call_log_id):
    """Fan out the ``call_missed`` notification for one call log.

    Returns the number of notified users. Safe to call more than once: it
    re-checks the status and the queue attempt's other legs every time. A
    leg that is answered elsewhere is re-labelled ``cancelled`` instead.
    """
    from users.notification_texts import T
    from users.notification_utils import create_notification

    from .models import CallLog

    call_log = CallLog.objects.select_related('handled_by').filter(pk=call_log_id).first()
    if call_log is None:
        return 0
    if call_log.direction != 'inbound' or call_log.status not in MISSED_STATUSES:
        return 0

    sibling = find_answered_sibling(call_log)
    if sibling is not None:
        # It rang here but a colleague answered it: not a missed call.
        _mark_answered_elsewhere(call_log, sibling)
        return 0

    recipients = resolve_missed_call_recipients(call_log)
    metadata = {
        'call_id': call_log.pk,
        'call_log_id': call_log.pk,
        'call_uuid': str(call_log.call_id),
        'caller_number': call_log.caller_number,
        'direction': call_log.direction,
        'status': call_log.status,
    }
    link_url = f'/calls?id={call_log.pk}'
    for user in recipients:
        try:
            create_notification(
                user=user,
                notification_type='call_missed',
                title=T('call.missed.title'),
                message=T('call.missed.message', number=call_log.caller_number),
                metadata=dict(metadata),
                link_url=link_url,
            )
        except Exception:  # noqa: BLE001 - one bad recipient must not stop the rest
            logger.exception('call_missed notification failed for user %s', user.pk)
    return len(recipients)


def schedule_missed_call_notification(call_log_id):
    """Queue :func:`send_missed_call_notifications` once the current
    transaction commits (a rolled-back save queues nothing), after at least
    one attempt gap so the next agent of a sequential queue can answer."""
    from django.db import connection, transaction

    from .models import CallLog

    schema = getattr(connection, 'schema_name', None) or 'public'
    call_log = CallLog.objects.filter(pk=call_log_id).only('id', 'handled_by_id').first()
    countdown = MISSED_CALL_NOTIFY_DELAY
    if call_log is not None:
        countdown = max(countdown, int(attempt_gap_for(call_log).total_seconds()))
    transaction.on_commit(
        lambda: _enqueue_missed_call_notification(call_log_id, schema, countdown)
    )


def _enqueue_missed_call_notification(call_log_id, schema, countdown=None):
    from .tasks import notify_missed_call

    try:
        notify_missed_call.apply_async(
            args=[call_log_id, schema],
            countdown=MISSED_CALL_NOTIFY_DELAY if countdown is None else countdown,
        )
    except Exception:  # noqa: BLE001
        if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
            raise
        # Broker unreachable: notify inline rather than lose the alert.
        logger.warning('Celery unavailable, sending call_missed inline', exc_info=True)
        from tenant_schemas.utils import schema_context

        with schema_context(schema):
            send_missed_call_notifications(call_log_id)
