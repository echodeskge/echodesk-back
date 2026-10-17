"""Celery tasks for the CRM / PBX app.

Currently this module only houses the full-tenant Asterisk resync task,
which doubles as a nightly cron target and a manual admin-button target.
Other CRM-scoped background work (transcriptions, recording archival, etc.)
can land here as it appears.
"""
from __future__ import annotations

import logging
from typing import Dict

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="crm.rebuild_tenant_asterisk_state")
def rebuild_tenant_asterisk_state(tenant_schema: str) -> Dict[str, int]:
    """Full resync of a tenant's PBX state into the Asterisk realtime DB.

    Kicks off :meth:`crm.asterisk_sync.AsteriskStateSync.full_resync` inside
    the target tenant's schema context so tenant-scoped models resolve
    correctly. Errors inside the service are swallowed and logged (the
    service is designed to never crash the caller); the return value is a
    best-effort summary of how many rows we *attempted* to sync.
    """
    from tenant_schemas.utils import schema_context

    from crm.asterisk_sync import AsteriskStateSync

    try:
        with schema_context(tenant_schema):
            summary = AsteriskStateSync(tenant_schema).full_resync()
    except Exception:  # noqa: BLE001
        logger.exception(
            "rebuild_tenant_asterisk_state failed for tenant=%s", tenant_schema
        )
        return {"trunks": 0, "extensions": 0, "queues": 0, "inbound_routes": 0}

    logger.info(
        "rebuild_tenant_asterisk_state: tenant=%s summary=%s", tenant_schema, summary
    )
    return summary


@shared_task(name="crm.notify_missed_call")
def notify_missed_call(call_log_id: int, tenant_schema: str) -> int:
    """Send the ``call_missed`` notification for one call log (see
    :mod:`crm.call_notifications`). Runs inside the tenant's schema."""
    from tenant_schemas.utils import schema_context

    from crm.call_notifications import send_missed_call_notifications

    with schema_context(tenant_schema):
        return send_missed_call_notifications(call_log_id)
