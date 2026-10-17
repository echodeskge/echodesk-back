"""Missed-call notifications.

``users.module_signals.notify_call_events`` calls
:func:`schedule_missed_call_notification` once, when an inbound call log
*transitions* into ``missed``/``no_answer``. The actual fan-out runs a few
seconds later (Celery countdown) so that, with a ring-all queue where every
ringing browser logged its own row, we can see whether a colleague picked
the call up in the meantime. If one did, the row is re-labelled
``cancelled`` (it rang here but was answered elsewhere; counted neither as
answered nor as missed) and nobody is notified. The task is queued only
after the saving transaction commits.

Recipients, in order:
1. the call's ``handled_by`` user (the agent whose phone rang);
2. otherwise the members of the queue (or the extension owner) that the
   dialled DID routes to through an active ``InboundRoute``;
3. otherwise the tenant's PBX admins (staff / superusers), at most 10.
"""

import logging
from datetime import timedelta

from django.conf import settings

from .call_status import ANSWERED_ELSEWHERE_STATUS, MISSED_STATUSES, last_digits

logger = logging.getLogger(__name__)

# Seconds to wait before notifying, so a colleague's "answered" update for
# the same ring-all call has time to arrive.
MISSED_CALL_NOTIFY_DELAY = getattr(settings, 'MISSED_CALL_NOTIFY_DELAY', 15)
# Two inbound rows from the same caller started this close together are
# treated as legs of the same ring-all call.
SIBLING_WINDOW = timedelta(seconds=120)
# Clock/latency slack when comparing one leg's timestamps with another's.
SIBLING_SLACK = timedelta(seconds=5)


def find_answered_sibling(call_log):
    """Another inbound leg of the same call that somebody answered.

    A leg counts only when it was still live while this one rang: it had
    not ended before this leg started, and it was answered no later than
    this leg ended. So a caller who is answered, hangs up and calls back
    unanswered within the window still gets a missed call.
    """
    from django.db.models import Q
    from django.utils import timezone

    from .models import CallLog

    digits = last_digits(call_log.caller_number)
    if not digits or not call_log.started_at:
        return None
    qs = CallLog.objects.filter(
        direction='inbound',
        answered_at__isnull=False,
        started_at__gte=call_log.started_at - SIBLING_WINDOW,
        started_at__lte=call_log.started_at + SIBLING_WINDOW,
    ).filter(
        Q(ended_at__isnull=True) | Q(ended_at__gte=call_log.started_at - SIBLING_SLACK),
        answered_at__lte=(call_log.ended_at or timezone.now()) + SIBLING_SLACK,
    ).exclude(pk=call_log.pk)
    if len(digits) >= 7:
        qs = qs.filter(caller_number__endswith=digits)
    else:
        qs = qs.filter(caller_number=call_log.caller_number)
    return qs.order_by('answered_at').first()


def resolve_missed_call_recipients(call_log):
    from django.contrib.auth import get_user_model

    from .models import InboundRoute, QueueMember

    User = get_user_model()

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

    from django.db.models import Q
    return list(
        User.objects.filter(is_active=True)
        .filter(Q(is_staff=True) | Q(is_superuser=True))
        .order_by('id')[:10]
    )


def send_missed_call_notifications(call_log_id):
    """Fan out the ``call_missed`` notification for one call log.

    Returns the number of notified users. Safe to call more than once: it
    re-checks the status and the ring-all siblings every time.
    """
    from users.notification_utils import create_notification

    from .models import CallEvent, CallLog

    call_log = CallLog.objects.select_related('handled_by').filter(pk=call_log_id).first()
    if call_log is None:
        return 0
    if call_log.direction != 'inbound' or call_log.status not in MISSED_STATUSES:
        return 0

    sibling = find_answered_sibling(call_log)
    if sibling is not None:
        # It rang here but a colleague answered it: not a missed call.
        # queryset.update() so the post_save signal does not fire again.
        CallLog.objects.filter(pk=call_log.pk).update(status=ANSWERED_ELSEWHERE_STATUS)
        CallEvent.objects.create(
            call_log=call_log,
            event_type='ended',
            metadata={
                'answered_elsewhere': True,
                'answered_call_log_id': sibling.pk,
                'previous_status': call_log.status,
            },
        )
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
                title='Missed call',
                message=f'Missed inbound call from {call_log.caller_number}',
                metadata=dict(metadata),
                link_url=link_url,
            )
        except Exception:  # noqa: BLE001 - one bad recipient must not stop the rest
            logger.exception('call_missed notification failed for user %s', user.pk)
    return len(recipients)


def schedule_missed_call_notification(call_log_id):
    """Queue :func:`send_missed_call_notifications` after a short delay,
    once the current transaction commits (a rolled-back save queues
    nothing)."""
    from django.db import connection, transaction

    schema = getattr(connection, 'schema_name', None) or 'public'
    transaction.on_commit(lambda: _enqueue_missed_call_notification(call_log_id, schema))


def _enqueue_missed_call_notification(call_log_id, schema):
    from .tasks import notify_missed_call

    try:
        notify_missed_call.apply_async(
            args=[call_log_id, schema], countdown=MISSED_CALL_NOTIFY_DELAY,
        )
    except Exception:  # noqa: BLE001
        if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
            raise
        # Broker unreachable: notify inline rather than lose the alert.
        logger.warning('Celery unavailable, sending call_missed inline', exc_info=True)
        from tenant_schemas.utils import schema_context

        with schema_context(schema):
            send_missed_call_notifications(call_log_id)
