"""Signal wiring for the Django → Asterisk realtime sync layer.

Every product-model mutation that should reflect in Asterisk flows through
this module. Handlers:

* ``post_save`` / ``post_delete`` on :class:`crm.models.UserPhoneAssignment`
  → sync/tombstone the pjsip endpoint + auth + aor rows.
* ``post_save`` / ``post_delete`` on :class:`crm.models.Trunk`
  → sync/tombstone the provider-trunk endpoint + registration rows.
* ``post_save`` / ``post_delete`` on :class:`crm.models.Queue`
  → sync/tombstone the queue + its members.
* ``post_save`` / ``post_delete`` on :class:`crm.models.InboundRoute`
  → no-op at DB level (see ``sync_inbound_route`` docstring), but the signal
  is still wired so a later realtime dialplan layer only needs to flip the
  service method, not re-hook signals.
* ``m2m_changed`` on ``User.tenant_groups.through`` → resync queue members
  for every queue backed by the affected group.
* ``post_save`` on :class:`crm.models.CallLog` → ``call_event`` push on
  the notifications WebSocket (see :mod:`crm.call_realtime`).

All handlers resolve the current tenant via ``connection.schema_name`` (set
by the tenant-schemas middleware), build an ``AsteriskStateSync`` and call
the matching method. Errors are swallowed inside ``AsteriskStateSync._run``
so product CRUD never crashes because of a realtime-DB hiccup.
"""
from __future__ import annotations

import logging

from django.db import connection
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from crm.asterisk_sync import AsteriskStateSync, _slugify_trunk
from crm.call_realtime import on_call_log_saved
from crm.models import CallLog, InboundRoute, Queue, Trunk, UserPhoneAssignment

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CallLog → realtime call_event (create / status change only)
# ---------------------------------------------------------------------------

post_save.connect(on_call_log_saved, sender=CallLog, dispatch_uid='crm.call_realtime.call_log')


def _sync_for_current_tenant() -> AsteriskStateSync | None:
    """Return an ``AsteriskStateSync`` bound to the current tenant schema.

    Returns ``None`` when the connection is on the public schema — product
    models don't run there, so a signal firing in public means something
    upstream went sideways and we should bail quietly instead of writing
    ``public_<ext>`` rows into the realtime DB.
    """
    schema = getattr(connection, "schema_name", None)
    if not schema or schema == "public":
        return None
    return AsteriskStateSync(schema)


# ---------------------------------------------------------------------------
# UserPhoneAssignment
# ---------------------------------------------------------------------------


@receiver(post_save, sender=UserPhoneAssignment)
def _on_assignment_saved(sender, instance: UserPhoneAssignment, **kwargs):
    sync = _sync_for_current_tenant()
    if sync is None:
        return
    if not instance.is_active:
        # Inactive assignments should not have an Asterisk endpoint.
        sync.tombstone_endpoint(instance.id, instance.extension)
        return
    sync.sync_endpoint(instance)
    # If this extension is referenced by any queue, refresh its membership —
    # membership is derived from (group ∩ active assignments), so flipping
    # an assignment's ``is_active`` has knock-on queue-level effects.
    _resync_queues_for_user(sync, instance.user_id)


@receiver(post_delete, sender=UserPhoneAssignment)
def _on_assignment_deleted(sender, instance: UserPhoneAssignment, **kwargs):
    sync = _sync_for_current_tenant()
    if sync is None:
        return
    sync.tombstone_endpoint(instance.id, instance.extension)
    _resync_queues_for_user(sync, instance.user_id)


# ---------------------------------------------------------------------------
# Trunk
# ---------------------------------------------------------------------------


@receiver(post_save, sender=Trunk)
def _on_trunk_saved(sender, instance: Trunk, **kwargs):
    sync = _sync_for_current_tenant()
    if sync is None:
        return
    if not instance.is_active:
        sync.tombstone_trunk(instance.id, slug=_slugify_trunk(instance))
        return
    sync.sync_trunk(instance)


@receiver(post_delete, sender=Trunk)
def _on_trunk_deleted(sender, instance: Trunk, **kwargs):
    sync = _sync_for_current_tenant()
    if sync is None:
        return
    sync.tombstone_trunk(instance.id, slug=_slugify_trunk(instance))


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


@receiver(post_save, sender=Queue)
def _on_queue_saved(sender, instance: Queue, **kwargs):
    sync = _sync_for_current_tenant()
    if sync is None:
        return
    if not instance.is_active:
        sync.tombstone_queue(instance.id, instance.slug)
        return
    sync.sync_queue(instance)


@receiver(post_delete, sender=Queue)
def _on_queue_deleted(sender, instance: Queue, **kwargs):
    sync = _sync_for_current_tenant()
    if sync is None:
        return
    sync.tombstone_queue(instance.id, instance.slug)


# ---------------------------------------------------------------------------
# InboundRoute (no-op at DB layer, hook kept for future realtime dialplan)
# ---------------------------------------------------------------------------


@receiver(post_save, sender=InboundRoute)
def _on_inbound_route_saved(sender, instance: InboundRoute, **kwargs):
    sync = _sync_for_current_tenant()
    if sync is None:
        return
    sync.sync_inbound_route(instance)


@receiver(post_delete, sender=InboundRoute)
def _on_inbound_route_deleted(sender, instance: InboundRoute, **kwargs):
    sync = _sync_for_current_tenant()
    if sync is None:
        return
    sync.tombstone_inbound_route(instance.id)


# ---------------------------------------------------------------------------
# Group membership → queue-member resync
#
# ``User.tenant_groups`` is a ManyToManyField. Django fires ``m2m_changed``
# on the through model with ``action`` ∈ {pre_add, post_add, pre_remove,
# post_remove, pre_clear, post_clear}. We only care about the ``post_*``
# actions. For post_add/post_remove, ``pk_set`` tells us which group(s) or
# user(s) changed (depending on ``reverse``). For post_clear, ``pk_set`` is
# empty and we have to fall back to resyncing every queue.
# ---------------------------------------------------------------------------


def _resync_queues_for_user(sync: AsteriskStateSync, user_id: int) -> None:
    """Resync member rows for every queue whose group includes ``user_id``."""
    affected = Queue.objects.filter(
        group__members__id=user_id, is_active=True
    ).distinct()
    for queue in affected:
        sync.sync_queue_members(queue)


def _resync_queues_for_group(sync: AsteriskStateSync, group_id: int) -> None:
    affected = Queue.objects.filter(group_id=group_id, is_active=True)
    for queue in affected:
        sync.sync_queue_members(queue)


def _resync_all_queues(sync: AsteriskStateSync) -> None:
    for queue in Queue.objects.filter(is_active=True):
        sync.sync_queue_members(queue)


def _tenant_groups_m2m_changed(sender, instance, action, reverse, pk_set, **kwargs):
    """Handle ``User.tenant_groups.through`` membership changes.

    We only act on the post_* actions. ``instance`` is either a ``User`` (when
    the M2M is accessed as ``user.tenant_groups``) or a ``TenantGroup`` (when
    accessed as ``group.members``) depending on ``reverse``.
    """
    if action not in {"post_add", "post_remove", "post_clear"}:
        return
    sync = _sync_for_current_tenant()
    if sync is None:
        return

    if action == "post_clear" or not pk_set:
        # Can't tell which groups were affected → resync everything tenant-wide.
        _resync_all_queues(sync)
        return

    if reverse:
        # instance is a TenantGroup; pk_set is user ids that were added/removed.
        _resync_queues_for_group(sync, instance.id)
    else:
        # instance is a User; pk_set is group ids.
        for group_id in pk_set:
            _resync_queues_for_group(sync, group_id)


def register_group_membership_signal():
    """Attach the m2m handler to ``User.tenant_groups.through``.

    Importing the through model requires the users app to be loaded, which is
    only guaranteed in ``AppConfig.ready()``. Called from ``crm.apps.CrmConfig.ready``.
    """
    from users.models import User

    through = User.tenant_groups.through
    m2m_changed.connect(_tenant_groups_m2m_changed, sender=through)
