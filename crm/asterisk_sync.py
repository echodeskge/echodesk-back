"""Django → Asterisk realtime sync service.

This module is the single chokepoint for turning EchoDesk product models
(``UserPhoneAssignment``, ``Trunk``, ``Queue``, ``InboundRoute``) into rows
inside the shared Asterisk realtime DB (models in the ``asterisk_state`` app).

Design rules
------------
1. **Tenant prefix everywhere.** Asterisk's realtime tables share one
   namespace across all tenants. We prefix every endpoint/auth/aor/queue id
   with the tenant's schema name (``{schema}_{local_name}``). The helper
   :meth:`AsteriskStateSync.prefix` is the ONLY place this format lives.
2. **Never crash the caller.** Signals must not block or fail product CRUD
   just because the realtime DB is unreachable. Every DB write is guarded
   by try/except + ``logger.exception``. A future admin-visible health
   indicator can scrape the log.
3. **Kill-switchable.** When ``settings.ASTERISK_SYNC_ENABLED`` is ``False``
   every method no-ops and logs at DEBUG level. This lets developers run
   the codebase with no asterisk DB configured.
4. **Inbound routes are dialplan, not pjsip.** Asterisk 18 has no canonical
   "inbound route" realtime table — inbound DID dispatch stays in
   ``extensions_custom.conf`` + the existing AGI that calls
   ``/api/pbx/call-routing/``. So ``sync_inbound_route`` is intentionally a
   no-op at the DB level; the route lookup happens at call time.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Optional

from django.conf import settings
from django.db import InterfaceError, OperationalError, transaction

from crm.asterisk_db import get_active_pbx_for_current_tenant, register_pbx_alias

if TYPE_CHECKING:
    from crm.models import InboundRoute, PbxServer, Queue, Trunk, UserPhoneAssignment


logger = logging.getLogger(__name__)


def _is_db_unreachable(exc: BaseException) -> bool:
    """True for a connection-level failure of the realtime DB.

    psycopg2 raises those (DNS, refused, timeout, server closed the
    connection) as ``OperationalError``/``InterfaceError`` **without** a
    SQLSTATE, whereas deadlocks and serialization failures carry one
    (``40P01``/``40001``) and still deserve a traceback.
    """
    if not isinstance(exc, (OperationalError, InterfaceError)):
        return False
    return getattr(exc.__cause__, "pgcode", None) is None


# WebRTC extension defaults. Mirror the working static config on pbx2 for
# extensions 100/101 so the first rollout is byte-for-byte compatible.
ENDPOINT_DEFAULTS_WEBRTC = {
    "transport": "transport-wss",
    "aors": None,  # set per-endpoint to the endpoint id
    "context": None,  # set per-tenant
    "disallow": "all",
    # Opus deliberately omitted: pbx2 doesn't ship `codec_opus.so` (only the
    # format-attribute helpers), so a browser that picks Opus and a trunk
    # that only speaks ulaw/alaw end up with `No path to translate from
    # opus to ulaw` and the call drops the moment the agent picks up.
    # Keeping the list aligned with the trunk's `allow` forces SDP
    # negotiation down to a common transcodable codec (almost always ulaw).
    "allow": "ulaw,alaw,g722",
    "direct_media": "no",
    "dtmf_mode": "rfc4733",
    "force_rport": "yes",
    "ice_support": "yes",
    "identify_by": "username",
    "rewrite_contact": "yes",
    "rtp_symmetric": "yes",
    "use_avpf": "yes",
    "media_encryption": "dtls",
    "media_use_received_transport": "yes",
    "rtcp_mux": "yes",
    "webrtc": "yes",
    "dtls_auto_generate_cert": "yes",
    "media_encryption_optimistic": "yes",
    "timers": "no",
    "trust_id_inbound": "yes",
}

AOR_DEFAULTS_WEBRTC = {
    "max_contacts": 5,
    "remove_existing": "yes",
    "qualify_frequency": 0,
    "default_expiration": 3600,
    "maximum_expiration": 7200,
    "minimum_expiration": 60,
}

AUTH_DEFAULTS = {
    "auth_type": "userpass",
}

# Provider-trunk endpoint defaults (SIP over UDP, no WebRTC bits).
ENDPOINT_DEFAULTS_TRUNK = {
    "transport": "transport-udp",
    "context": None,  # set per-tenant (uses ``from-provider-<schema>`` below)
    "disallow": "all",
    "allow": "alaw,ulaw,g722",
    "direct_media": "no",
    "dtmf_mode": "rfc4733",
    "force_rport": "yes",
    "ice_support": "no",
    "rtp_symmetric": "yes",
    "identify_by": "ip,username",
    "rewrite_contact": "no",
    "send_rpid": "yes",
    "trust_id_inbound": "yes",
}

AOR_DEFAULTS_TRUNK = {
    "max_contacts": 1,
    "qualify_frequency": 60,
}


class AsteriskStateSync:
    """Service that materialises Django product models into Asterisk realtime rows.

    Usage:

        sync = AsteriskStateSync(tenant_schema='acme')
        sync.sync_endpoint(user_phone_assignment)
        sync.tombstone_endpoint(assignment_id=5, extension='100')

    All methods are safe to call with ``settings.ASTERISK_SYNC_ENABLED=False``
    — they short-circuit with a DEBUG log.
    """

    def __init__(self, tenant_schema: str, pbx: Optional["PbxServer"] = None):
        self.tenant_schema = tenant_schema
        # Resolve the active PbxServer once per sync instance. ``pbx=None``
        # is the signal to every sync_* method that the tenant hasn't
        # registered a BYO server yet — methods will no-op instead of
        # trying to write to a non-existent realtime DB.
        if pbx is None:
            pbx = get_active_pbx_for_current_tenant()
        self.pbx: Optional["PbxServer"] = pbx
        # Register (or refresh) the per-tenant asterisk DB alias so that
        # ``.using(self.alias)`` works immediately. Only register when we
        # actually have a PbxServer — otherwise leave ``self.alias = None``
        # and rely on ``_enabled()`` to short-circuit callers.
        self.alias: Optional[str] = None
        if self.pbx is not None:
            try:
                self.alias = register_pbx_alias(self.pbx, schema_name=tenant_schema)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to register asterisk DB alias for tenant=%s",
                    tenant_schema,
                )
                self.pbx = None  # degrade to no-op mode rather than crash

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def prefix(self, tenant_schema: str, name: str) -> str:
        """Return the Asterisk realtime ID for ``name`` in this tenant.

        When the bound PbxServer has ``use_tenant_prefix=True`` (legacy
        shared-DB deployments), the schema name is prepended to keep IDs
        globally unique:

            >>> sync.prefix('amanati', '100')  # use_tenant_prefix=True
            'amanati_100'

        For BYO PBXs with their own dedicated DB, the prefix is redundant
        (no other tenant shares the row namespace), so we return the bare
        name:

            >>> sync.prefix('acme', '100')  # use_tenant_prefix=False
            '100'

        ``tenant_schema`` stays in the signature for backwards compatibility
        with call sites that still pass it explicitly. Static callers (tests,
        migrations) can use :meth:`prefix_with` which takes the flag
        directly.
        """
        if self.pbx is not None and not self.pbx.use_tenant_prefix:
            return name
        return f"{tenant_schema}_{name}"

    @staticmethod
    def prefix_with(tenant_schema: str, name: str, use_tenant_prefix: bool) -> str:
        """Static variant of :meth:`prefix` for callers outside a tenant context."""
        if not use_tenant_prefix:
            return name
        return f"{tenant_schema}_{name}"

    @property
    def _tenant_context(self) -> str:
        """Asterisk dialplan context name for this tenant's internal calls."""
        return f"tenant_{self.tenant_schema}"

    @property
    def _provider_context(self) -> str:
        """Asterisk dialplan context name for inbound calls from provider trunks."""
        return f"from-provider-{self.tenant_schema}"

    def _enabled(self) -> bool:
        """Return True when this sync instance can actually write.

        Writes require **both** the global ``ASTERISK_SYNC_ENABLED`` flag
        (always True in Phase 2; kept as a kill-switch for emergencies)
        **and** a bound PbxServer for the current tenant. If no PbxServer
        is registered yet, signals still fire but every sync method
        short-circuits — the tenant just hasn't enrolled a BYO server yet.
        """
        if not getattr(settings, "ASTERISK_SYNC_ENABLED", True):
            logger.debug(
                "AsteriskStateSync no-op (ASTERISK_SYNC_ENABLED=False) for tenant=%s",
                self.tenant_schema,
            )
            return False
        if self.pbx is None or self.alias is None:
            logger.debug(
                "AsteriskStateSync no-op (no active PbxServer) for tenant=%s",
                self.tenant_schema,
            )
            return False
        return True

    @staticmethod
    def _safe(op_name: str):
        """Decorator-like helper: wrap a callable and swallow DB errors.

        We intentionally never let a sync error propagate to the caller — the
        product CRUD must succeed even when the asterisk DB is unreachable.
        """
        # Implemented as a small wrapper used inline via _run().
        raise NotImplementedError  # pragma: no cover — not used; see _run()

    def _run(self, op_name: str, func, *args, **kwargs):
        """Execute ``func`` guarded by try/except + log, return None on error."""
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — we genuinely want to swallow everything
            if _is_db_unreachable(exc):
                # The realtime DB lives on another host; a DNS blip, failover
                # or pooler restart is infrastructure, not an application
                # fault. The product write already succeeded — the tenant's
                # Asterisk state just needs a resync to catch up.
                logger.warning(
                    "AsteriskStateSync.%s skipped for tenant=%s: realtime DB unreachable "
                    "(%s). Run `manage.py sync_tenant_asterisk %s` to catch up.",
                    op_name, self.tenant_schema, exc, self.tenant_schema,
                )
                return None
            logger.exception(
                "AsteriskStateSync.%s failed for tenant=%s", op_name, self.tenant_schema
            )
            return None

    # ------------------------------------------------------------------
    # Endpoint (UserPhoneAssignment) sync
    # ------------------------------------------------------------------

    def sync_endpoint(self, assignment: "UserPhoneAssignment") -> None:
        """Upsert the 4 pjsip rows (endpoint, auth, aor, identify) for a user extension."""
        if not self._enabled():
            return
        self._run("sync_endpoint", self._sync_endpoint_impl, assignment)

    def _sync_endpoint_impl(self, assignment: "UserPhoneAssignment") -> None:
        from asterisk_state.models import PsAor, PsAuth, PsEndpoint, PsIdentify

        endpoint_id = self.prefix(self.tenant_schema, str(assignment.extension))
        caller_id = assignment.display_name or assignment.user.email
        callerid = f'"{caller_id}" <{assignment.extension}>'

        endpoint_fields = {
            **ENDPOINT_DEFAULTS_WEBRTC,
            "aors": endpoint_id,
            "auth": endpoint_id,
            "context": self._tenant_context,
            "callerid": callerid,
            # Deliberately NOT setting `from_user` here. Asterisk treats
            # `from_user` as a hard override on the From URI's user portion
            # for every INVITE *to* this endpoint — including the inbound
            # leg of a queued call — so the agent's softphone (which reads
            # `invitation.remoteIdentity.uri.user` for caller display) ends
            # up showing the agent's own extension number instead of the
            # actual caller's CLI. Leaving it unset lets Asterisk fall back
            # to CALLERID(num) on the bridged channel, which is the real
            # phone number from the trunk.
            "trust_id_outbound": "yes",
        }
        aor_fields = {**AOR_DEFAULTS_WEBRTC}
        auth_fields = {
            **AUTH_DEFAULTS,
            "username": assignment.extension,
            "password": assignment.extension_password,
            "realm": getattr(settings, "PBX_REALM", "asterisk"),
        }

        with transaction.atomic(using=self.alias):
            PsAuth.objects.using(self.alias).update_or_create(
                id=endpoint_id, defaults=auth_fields
            )
            PsAor.objects.using(self.alias).update_or_create(
                id=endpoint_id, defaults=aor_fields
            )
            PsEndpoint.objects.using(self.alias).update_or_create(
                id=endpoint_id, defaults=endpoint_fields
            )
            # Identify by username — pjsip already uses identify_by=username
            # on the endpoint so this row is only needed when we later layer
            # IP-based identify. For WebRTC endpoints we ensure no stale row.
            PsIdentify.objects.using(self.alias).filter(id=endpoint_id).delete()

    def tombstone_endpoint(self, assignment_id: int, extension: str) -> None:
        """Delete all 4 pjsip rows for an extension (on extension deletion)."""
        if not self._enabled():
            return
        self._run("tombstone_endpoint", self._tombstone_endpoint_impl, extension)

    def _tombstone_endpoint_impl(self, extension: str) -> None:
        from asterisk_state.models import PsAor, PsAuth, PsEndpoint, PsIdentify

        endpoint_id = self.prefix(self.tenant_schema, str(extension))
        with transaction.atomic(using=self.alias):
            PsEndpoint.objects.using(self.alias).filter(id=endpoint_id).delete()
            PsIdentify.objects.using(self.alias).filter(id=endpoint_id).delete()
            PsAor.objects.using(self.alias).filter(id=endpoint_id).delete()
            PsAuth.objects.using(self.alias).filter(id=endpoint_id).delete()

    # ------------------------------------------------------------------
    # Trunk sync
    # ------------------------------------------------------------------

    def sync_trunk(self, trunk: "Trunk") -> None:
        """Upsert the pjsip rows (and optional registration) for a provider trunk."""
        if not self._enabled():
            return
        self._run("sync_trunk", self._sync_trunk_impl, trunk)

    def _sync_trunk_impl(self, trunk: "Trunk") -> None:
        from asterisk_state.models import (
            PsAor,
            PsAuth,
            PsEndpoint,
            PsIdentify,
            PsRegistration,
        )

        slug = _slugify_trunk(trunk)
        endpoint_id = self.prefix(self.tenant_schema, f"trunk_{slug}")
        codecs = _format_codecs(trunk.codecs) or "alaw,ulaw,g722"
        callerid = ""
        if trunk.caller_id_number:
            callerid = f'"{trunk.name}" <{trunk.caller_id_number}>'

        endpoint_fields = {
            **ENDPOINT_DEFAULTS_TRUNK,
            "aors": endpoint_id,
            "auth": endpoint_id,
            "context": self._provider_context,
            "outbound_auth": endpoint_id,
            "allow": codecs,
            "callerid": callerid,
            "from_user": trunk.username or None,
            "from_domain": trunk.realm or trunk.sip_server or None,
        }
        aor_fields = {
            **AOR_DEFAULTS_TRUNK,
            "contact": f"sip:{trunk.sip_server}:{trunk.sip_port}",
        }
        auth_fields = {
            **AUTH_DEFAULTS,
            "username": trunk.username,
            "password": trunk.password,
            "realm": trunk.realm or None,
        }
        identify_fields = {
            "endpoint": endpoint_id,
            "match": trunk.sip_server,
        }

        with transaction.atomic(using=self.alias):
            PsAuth.objects.using(self.alias).update_or_create(
                id=endpoint_id, defaults=auth_fields
            )
            PsAor.objects.using(self.alias).update_or_create(
                id=endpoint_id, defaults=aor_fields
            )
            PsEndpoint.objects.using(self.alias).update_or_create(
                id=endpoint_id, defaults=endpoint_fields
            )
            PsIdentify.objects.using(self.alias).update_or_create(
                id=endpoint_id, defaults=identify_fields
            )

            if trunk.register:
                reg_fields = {
                    "server_uri": f"sip:{trunk.sip_server}:{trunk.sip_port}",
                    "client_uri": (
                        f"sip:{trunk.username}@{trunk.realm or trunk.sip_server}"
                    ),
                    "contact_user": trunk.username,
                    "expiration": 3600,
                    "retry_interval": 60,
                    "forbidden_retry_interval": 600,
                    "fatal_retry_interval": 600,
                    "outbound_auth": endpoint_id,
                    "transport": "transport-udp",
                    "max_retries": 10000,
                    "auth_rejection_permanent": "no",
                    "support_path": "no",
                }
                PsRegistration.objects.using(self.alias).update_or_create(
                    id=endpoint_id, defaults=reg_fields
                )
            else:
                PsRegistration.objects.using(self.alias).filter(id=endpoint_id).delete()

    def tombstone_trunk(self, trunk_id: int, slug: Optional[str] = None) -> None:
        """Delete all pjsip + registration rows for a deleted trunk.

        ``slug`` is required because after a ``post_delete`` signal the Trunk
        instance is gone — the caller must pass the slug they computed while
        the row still existed.
        """
        if not self._enabled():
            return
        if not slug:
            logger.warning(
                "tombstone_trunk called without slug for trunk_id=%s (tenant=%s)",
                trunk_id,
                self.tenant_schema,
            )
            return
        self._run("tombstone_trunk", self._tombstone_trunk_impl, slug)

    def _tombstone_trunk_impl(self, slug: str) -> None:
        from asterisk_state.models import (
            PsAor,
            PsAuth,
            PsEndpoint,
            PsIdentify,
            PsRegistration,
        )

        endpoint_id = self.prefix(self.tenant_schema, f"trunk_{slug}")
        with transaction.atomic(using=self.alias):
            PsRegistration.objects.using(self.alias).filter(id=endpoint_id).delete()
            PsEndpoint.objects.using(self.alias).filter(id=endpoint_id).delete()
            PsIdentify.objects.using(self.alias).filter(id=endpoint_id).delete()
            PsAor.objects.using(self.alias).filter(id=endpoint_id).delete()
            PsAuth.objects.using(self.alias).filter(id=endpoint_id).delete()

    # ------------------------------------------------------------------
    # Queue sync
    # ------------------------------------------------------------------

    def sync_queue(self, queue: "Queue") -> None:
        """Upsert the ``queues`` row and then resync membership."""
        if not self._enabled():
            return
        self._run("sync_queue", self._sync_queue_impl, queue)
        # Member sync is idempotent so re-running after a queue update is safe.
        self._run("sync_queue_members", self._sync_queue_members_impl, queue)

    def _sync_queue_impl(self, queue: "Queue") -> None:
        from asterisk_state.models import AsteriskQueue

        queue_name = self.prefix(self.tenant_schema, queue.slug)
        fields = {
            "strategy": queue.strategy,
            "timeout": queue.timeout_seconds,
            "wrapuptime": queue.wrapup_time,
            "maxlen": queue.max_len,
            "musicclass": queue.music_on_hold or "default",
            "announce_position": "yes" if queue.announce_position else "no",
            "announce_holdtime": "yes" if queue.announce_holdtime else "no",
            "joinempty": queue.joinempty,
            "leavewhenempty": queue.leavewhenempty,
            "retry": 5,
            "ringinuse": "no",
            "autopause": "no",
            "autofill": "yes",
            "eventmemberstatus": "yes",
            "eventwhencalled": "yes",
            "reportholdtime": "no",
            "context": self._tenant_context,
        }
        AsteriskQueue.objects.using(self.alias).update_or_create(
            name=queue_name, defaults=fields
        )

    def sync_queue_members(self, queue: "Queue") -> None:
        """Recompute the queue's membership from the Django group → assignments intersection.

        Also mirrors rows into the local ``crm.QueueMember`` table so the UI
        can display who *should* be receiving calls without querying
        Asterisk.
        """
        if not self._enabled():
            return
        self._run("sync_queue_members", self._sync_queue_members_impl, queue)

    def _sync_queue_members_impl(self, queue: "Queue") -> None:
        from asterisk_state.models import AsteriskQueueMember
        from crm.models import QueueMember, UserPhoneAssignment

        queue_name = self.prefix(self.tenant_schema, queue.slug)

        # Source of truth: group members who have an active primary assignment.
        group_user_ids = set(queue.group.members.values_list("id", flat=True))
        active_assignments = list(
            UserPhoneAssignment.objects.filter(
                user_id__in=group_user_ids, is_active=True
            ).select_related("user")
        )

        # --- Asterisk realtime side ---
        desired_interfaces: Dict[str, "UserPhoneAssignment"] = {}
        for assignment in active_assignments:
            endpoint_id = self.prefix(self.tenant_schema, str(assignment.extension))
            desired_interfaces[f"PJSIP/{endpoint_id}"] = assignment

        # Upsert each desired interface.
        for interface, assignment in desired_interfaces.items():
            AsteriskQueueMember.objects.using(self.alias).update_or_create(
                queue_name=queue_name,
                interface=interface,
                defaults={
                    "membername": assignment.display_name or assignment.user.email,
                    "state_interface": interface,
                    "penalty": 0,
                    "paused": 0,
                    "wrapuptime": queue.wrapup_time,
                },
            )
        # Drop rows that are no longer in the desired set.
        AsteriskQueueMember.objects.using(self.alias).filter(
            queue_name=queue_name
        ).exclude(interface__in=list(desired_interfaces.keys())).delete()

        # --- Local product-side mirror (crm.QueueMember) ---
        desired_assignment_ids = {a.id for a in active_assignments}
        existing = {
            qm.user_phone_assignment_id: qm
            for qm in QueueMember.objects.filter(queue=queue)
        }
        for assignment in active_assignments:
            if assignment.id in existing:
                qm = existing[assignment.id]
                if not qm.is_active:
                    qm.is_active = True
                    qm.save(update_fields=["is_active", "synced_at"])
            else:
                QueueMember.objects.create(
                    queue=queue,
                    user_phone_assignment=assignment,
                    penalty=0,
                    paused=False,
                    is_active=True,
                )
        # Remove mirror rows that no longer belong.
        stale_ids = [
            qm_id
            for assignment_id, qm in existing.items()
            for qm_id in [qm.id]
            if assignment_id not in desired_assignment_ids
        ]
        if stale_ids:
            QueueMember.objects.filter(id__in=stale_ids).delete()

    def tombstone_queue(self, queue_id: int, slug: str) -> None:
        """Delete the queue row + all its members."""
        if not self._enabled():
            return
        self._run("tombstone_queue", self._tombstone_queue_impl, slug)

    def _tombstone_queue_impl(self, slug: str) -> None:
        from asterisk_state.models import AsteriskQueue, AsteriskQueueMember

        queue_name = self.prefix(self.tenant_schema, slug)
        with transaction.atomic(using=self.alias):
            AsteriskQueueMember.objects.using(self.alias).filter(
                queue_name=queue_name
            ).delete()
            AsteriskQueue.objects.using(self.alias).filter(name=queue_name).delete()

    # ------------------------------------------------------------------
    # Inbound routes (intentionally no-op at the DB level)
    # ------------------------------------------------------------------

    def sync_inbound_route(self, route: "InboundRoute") -> None:
        """No-op at the DB level.

        Inbound DID → destination dispatch lives in the dialplan on pbx2
        (``extensions_custom.conf``), which hits the existing AGI at
        ``/api/pbx/call-routing/?did=...``. That endpoint queries the
        ``InboundRoute`` table directly, so writing it to ``asterisk_state``
        would double-store the data with no benefit. We keep the method on
        the service for signal-symmetry and for future work that may want to
        warm a realtime dialplan cache.
        """
        if not self._enabled():
            return
        logger.debug(
            "sync_inbound_route: no-op for route_id=%s tenant=%s (dialplan handles routing)",
            getattr(route, "id", None),
            self.tenant_schema,
        )

    def tombstone_inbound_route(self, route_id: int) -> None:
        """No-op — see :meth:`sync_inbound_route`."""
        if not self._enabled():
            return
        logger.debug(
            "tombstone_inbound_route: no-op for route_id=%s tenant=%s",
            route_id,
            self.tenant_schema,
        )

    # ------------------------------------------------------------------
    # Full tenant resync (Celery-driven)
    # ------------------------------------------------------------------

    def full_resync(self) -> Dict[str, int]:
        """Run every sync_* method for the current tenant.

        Returns a summary dict usable by the Celery task / management command
        for logging (e.g. ``{"trunks": 2, "extensions": 18, "queues": 3}``).
        Failures on individual rows are swallowed via ``_run``; the count we
        return is the number of rows we *attempted* to sync, not the number
        that succeeded.
        """
        summary = {"trunks": 0, "extensions": 0, "queues": 0, "inbound_routes": 0}
        if not self._enabled():
            logger.info(
                "full_resync no-op (ASTERISK_SYNC_ENABLED=False) for tenant=%s",
                self.tenant_schema,
            )
            return summary

        from crm.models import InboundRoute, Queue, Trunk, UserPhoneAssignment

        for trunk in Trunk.objects.filter(is_active=True):
            self.sync_trunk(trunk)
            summary["trunks"] += 1
        for assignment in UserPhoneAssignment.objects.filter(is_active=True):
            self.sync_endpoint(assignment)
            summary["extensions"] += 1
        for queue in Queue.objects.filter(is_active=True):
            self.sync_queue(queue)
            summary["queues"] += 1
        for route in InboundRoute.objects.filter(is_active=True):
            self.sync_inbound_route(route)
            summary["inbound_routes"] += 1

        logger.info(
            "full_resync complete for tenant=%s summary=%s", self.tenant_schema, summary
        )
        return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _slugify_trunk(trunk: "Trunk") -> str:
    """Produce a stable, safe slug for a Trunk for use in Asterisk IDs.

    Asterisk's realtime keys should stay ASCII and free of whitespace, so we
    normalise the trunk name: lowercase, spaces → underscores, keep only
    ``[a-z0-9_-]``. Falls back to ``trunk<id>`` if the name produces nothing
    usable.
    """
    import re

    raw = (trunk.name or "").strip().lower()
    slug = re.sub(r"[^a-z0-9_-]+", "_", raw).strip("_")
    return slug or f"trunk{trunk.id}"


def _format_codecs(codecs) -> str:
    """Turn a JSONField list like ['g722','alaw'] into 'g722,alaw'."""
    if not codecs:
        return ""
    if isinstance(codecs, str):
        return codecs
    try:
        return ",".join(str(c).strip() for c in codecs if str(c).strip())
    except TypeError:
        return ""
