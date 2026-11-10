"""Realtime ``call_event`` pushes on the notifications socket.

Whenever a :class:`crm.models.CallLog` is created or changes status, the
users involved (``handled_by`` and ``transferred_to_user``) get a
``call_event`` frame on their notifications WebSocket
(``notifications_{schema}_{user_id}``, see
:class:`users.consumers.NotificationConsumer.call_event`)::

    {"type": "call_event",
     "call": {"id", "call_id", "pbx_call_id", "sip_call_id", "status",
              "direction", "caller_number", "recipient_number",
              "answered_at", "ended_at", "handled_by", "updated_at"}}

The push is scheduled with ``transaction.on_commit`` (a rolled-back
save announces nothing) and every failure is swallowed and logged: a
Redis outage must never fail the API call that saved the row.

:func:`broadcast_call_event` is the public entry point for code paths
that update rows without ``save()`` (``queryset.update()`` fires no
signal), e.g. the ringall legs re-labelled ``cancelled`` when a sibling
leg is answered.
"""
from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from django.db import connection, transaction

logger = logging.getLogger(__name__)

CALL_EVENT_TYPE = 'call_event'


def notifications_group(schema: str, user_id: int) -> str:
    return f'notifications_{schema}_{user_id}'


def _iso(value):
    return value.isoformat() if value is not None else None


def call_event_payload(call_log) -> dict:
    """The ``call`` object of a ``call_event`` frame for ``call_log``."""
    return {
        'id': call_log.pk,
        'call_id': str(call_log.call_id) if call_log.call_id else None,
        'pbx_call_id': call_log.pbx_call_id or '',
        'sip_call_id': call_log.sip_call_id or '',
        'status': call_log.status,
        'direction': call_log.direction,
        'caller_number': call_log.caller_number or '',
        'recipient_number': call_log.recipient_number or '',
        'answered_at': _iso(call_log.answered_at),
        'ended_at': _iso(call_log.ended_at),
        'handled_by': call_log.handled_by_id,
        'updated_at': _iso(call_log.updated_at),
    }


def call_event_recipients(call_log) -> list[int]:
    """User ids that follow ``call_log``: its handler and, for a transfer,
    the user it was transferred to (deduplicated, no ``None``)."""
    ids = []
    for user_id in (call_log.handled_by_id, call_log.transferred_to_user_id):
        if user_id and user_id not in ids:
            ids.append(user_id)
    return ids


def send_call_event(schema: str, user_ids, payload: dict) -> int:
    """Deliver ``payload`` to each user's notifications group right now.
    Returns how many groups were sent to; never raises."""
    if not schema or not user_ids:
        return 0
    try:
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            return 0
        sent = 0
        for user_id in user_ids:
            async_to_sync(layer.group_send)(
                notifications_group(schema, user_id),
                {'type': CALL_EVENT_TYPE, 'call': payload},
            )
            sent += 1
        return sent
    except Exception:  # noqa: BLE001 - realtime is best effort
        logger.warning(
            'call_event push failed for call %s (schema=%s)',
            payload.get('id'), schema, exc_info=True,
        )
        return 0


def broadcast_call_event(call_log, *, schema: str | None = None) -> None:
    """Push a ``call_event`` for ``call_log`` to its handler and transfer
    target once the current transaction commits (immediately when there
    is none). Safe to call from anywhere: the payload is captured now,
    the send happens after commit, and errors are logged, never raised.

    ``schema`` defaults to the current tenant schema; pass it explicitly
    when calling from outside a tenant context (Celery, ``schema_context``
    loops).
    """
    try:
        schema = schema or getattr(connection, 'schema_name', None)
        if not schema or schema == 'public':
            return
        recipients = call_event_recipients(call_log)
        if not recipients:
            return
        payload = call_event_payload(call_log)
        transaction.on_commit(lambda: send_call_event(schema, recipients, payload))
    except Exception:  # noqa: BLE001
        logger.warning('call_event scheduling failed for call %s', getattr(call_log, 'pk', None),
                       exc_info=True)


def on_call_log_saved(sender, instance, created, **kwargs):
    """``post_save`` receiver: broadcast on create and on status change
    only. ``_old_status`` is set by the ``pre_save`` in
    :mod:`users.module_signals` (``None`` for new rows, the stored status
    otherwise; equal to the new status when ``update_fields`` leaves
    ``status`` out). Notes-only / recording-url saves stay silent."""
    if kwargs.get('raw'):
        return
    if not created:
        if not hasattr(instance, '_old_status'):
            # No pre_save ran (users signals not loaded): we cannot tell
            # whether the status changed, so stay quiet rather than spam.
            return
        if instance._old_status == instance.status:
            return
    broadcast_call_event(instance)
