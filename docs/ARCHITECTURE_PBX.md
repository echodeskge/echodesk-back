# PBX / Asterisk integration — engineering architecture

> **Audience:** engineers working on calling, or anyone doing technical
> due-diligence on the codebase. This is the *code-side* companion to the
> operational runbooks in [`pbx/`](../pbx/README.md). The runbooks tell you
> how to provision and cut over a tenant's Asterisk; this document explains
> how the Django code keeps Asterisk in sync and how to operate it.
>
> Read [`pbx/CUTOVER_RUNBOOK.md`](../pbx/CUTOVER_RUNBOOK.md) for *why each
> Asterisk config line exists*; read this for *what the Django side does*.

---

## 1. One-paragraph mental model

EchoDesk does **not** edit Asterisk config files at runtime. Instead, Asterisk
18 is configured for **realtime (ARA)** — it reads PJSIP endpoints, auth, AORs,
registrations, and queues out of a Postgres database via `res_config_pgsql` +
sorcery. EchoDesk *owns the writes* to that database. When a product model
changes (a user gets an extension, a trunk is added, a queue's membership
shifts), a Django signal materialises the change into rows in that Postgres DB.
Asterisk picks them up on its next lookup. **Each tenant has its own dedicated
Asterisk Postgres database** ("BYO Asterisk", Phase 2), reached through a
DB alias that is built *at runtime* from credentials stored on the tenant's
`PbxServer` row.

```
 Product model save (UserPhoneAssignment / Trunk / Queue / group membership)
        │
        ▼  Django post_save / post_delete / m2m_changed
 crm/signals.py  ──►  _sync_for_current_tenant()  ──►  AsteriskStateSync(schema)
        │                                                    │
        │                          register_pbx_alias()  ◄───┘  (inject asterisk_<schema>
        │                                                        into connections.databases)
        ▼
 sync_endpoint / sync_trunk / sync_queue ... .objects.using("asterisk_<schema>")
        │
        ▼  AsteriskStateRouter routes asterisk_state.* models
 Per-tenant Postgres DB  (ps_endpoints, ps_auths, ps_aors, ps_identifies,
        │                 ps_registrations, queues, queue_members)
        ▼  res_config_pgsql + sorcery realtime
 Asterisk 18 on pbx*.echodesk.cloud  ──►  live calls
```

---

## 2. The three moving parts

### 2a. The sync service — `crm/asterisk_sync.py`

`AsteriskStateSync` is the **single chokepoint** that turns product models into
realtime rows. Never write to `asterisk_state.*` models from anywhere else.

- Constructed per tenant: `AsteriskStateSync(tenant_schema)`. In its `__init__`
  it resolves the tenant's active `PbxServer` and calls `register_pbx_alias`
  (see §2b), storing the resulting alias on `self.alias`.
- Methods come in `sync_*` / `tombstone_*` pairs:
  - `sync_endpoint(assignment)` → upserts `PsAuth`, `PsAor`, `PsEndpoint` (and
    clears any stale `PsIdentify`) for a user extension (WebRTC defaults).
  - `sync_trunk(trunk)` → upserts the provider-trunk endpoint/auth/aor/identify
    rows, and a `PsRegistration` row when `trunk.register` is set.
  - `sync_queue(queue)` → upserts the `queues` row, then `sync_queue_members`.
  - `sync_queue_members(queue)` → recomputes membership from the
    *group ∩ active assignments* intersection, writes `AsteriskQueueMember`
    rows, and mirrors them into the local `crm.QueueMember` table for the UI.
  - `sync_inbound_route(route)` → **intentional no-op** at the DB level. Inbound
    DID dispatch lives in the dialplan (`extensions_custom.conf`) which calls
    the routing AGI; the hook exists only for signal symmetry.
- `full_resync()` runs every `sync_*` for the tenant and returns a summary dict
  (`{"trunks": …, "extensions": …, "queues": …}`). This is what the Celery task
  and admin "resync" button call.

**Three invariants worth knowing (documented in the module docstring):**
1. **Tenant prefix everywhere.** Realtime tables share one namespace, so IDs are
   prefixed `{schema}_{name}` — but *only* when the bound `PbxServer` has
   `use_tenant_prefix=True` (legacy shared-DB). For a dedicated BYO DB the bare
   name is used. `AsteriskStateSync.prefix()` is the only place this lives.
2. **Never crash the caller.** Every write is wrapped in `_run()` (try/except +
   `logger.exception`). A realtime-DB outage must never block product CRUD.
3. **Kill-switchable.** `settings.ASTERISK_SYNC_ENABLED=False` makes every
   method a no-op. So does the absence of an active `PbxServer` for the tenant
   (`_enabled()` returns `False`).

### 2b. The runtime DB-alias mechanism — `crm/asterisk_db.py`  ⚠️ *highest-complexity area*

> This is the most unusual part of the codebase and the part most worth
> understanding before you touch calling. There is **no static
> `DATABASES['asterisk']`**. Aliases are created by mutating
> `django.db.connections.databases` at runtime.

- `alias_for_schema(schema)` → the deterministic alias name `asterisk_<schema>`.
- `_build_db_config(pbx_server)` → a Django `DATABASES`-style dict built from the
  encrypted `realtime_db_*` fields on the `PbxServer` row (host, name, user,
  password, sslmode). Note `connect_timeout: 5` — a request pod must not hang
  ~75 s on an unreachable PBX DB.
- `register_pbx_alias(pbx_server, schema_name=…)` → **idempotently injects the
  alias into `connections.databases`.** If the alias already targets the same DB
  name, it refreshes credentials in place (lets an admin rotate creds with no
  restart). If it targets a *different* DB, it closes the stale connection,
  drops the cached `DatabaseWrapper` (`delattr(connections._connections, alias)`
  — an internal-API touch; revisit if upgrading Django), then registers the new
  config. Returns the alias name.
- `get_active_pbx_for_current_tenant()` → the active `PbxServer` for the current
  `connection.schema_name`, or `None` (public schema / no row / not active).
- `get_asterisk_connection_for_current_tenant()` → `(alias, pbx)`, registering
  the alias on demand. Returns `(None, None)` when there's no active PBX — the
  caller MUST treat this as "skip" and never fall back to the default DB.
- `warm_aliases_for_all_tenants()` → registers every tenant's alias at process
  boot (called from `CrmConfig.ready()`, `crm/apps.py`). Pure latency
  optimisation; aliases are also registered lazily on first use.

### 2c. The router — `amanati_crm/db_routers.py::AsteriskStateRouter`

- `db_for_read` / `db_for_write`: any model with `app_label == "asterisk_state"`
  is routed to `_resolve_alias()` (which calls
  `get_asterisk_connection_for_current_tenant`). Everything else returns `None`
  (defer to the tenant-schemas router on `default`).
- **Fail-closed:** if there's no active `PbxServer`, `_resolve_alias()` returns
  `None` → Django raises rather than silently writing to `default`.
- `allow_migrate`: `asterisk_state` migrations run **only** on `asterisk_*`
  aliases; no other app is ever migrated into an asterisk DB, and `asterisk_*`
  aliases never receive non-asterisk migrations.

---

## 3. The shadow models — `asterisk_state/models.py`

These mirror Asterisk 18's realtime table schemas. **Django owns writes;
Asterisk owns reads.** Column names/types are kept aligned with the official
Asterisk 18 realtime schema. All carry `app_label="asterisk_state"` so the
router can target them without per-query hints.

| Model | `db_table` | Managed? | Purpose |
|---|---|---|---|
| `PsEndpoint` | `ps_endpoints` | ✅ | PJSIP endpoint (extension or trunk) |
| `PsAuth` | `ps_auths` | ✅ | Endpoint credentials |
| `PsAor` | `ps_aors` | ✅ | Address-of-record / registration target |
| `PsIdentify` | `ps_identifies` | ✅ | IP-based identify (provider trunks) |
| `PsRegistration` | `ps_registrations` | ✅ | Outbound REGISTER (trunk → provider) |
| `AsteriskQueue` | `queues` | ✅ | `app_queue` queue definition |
| `AsteriskQueueMember` | `queue_members` | ✅ | Queue agent (`PJSIP/<endpoint>`) |
| `PsContact` | `ps_contacts` | ❌ **unmanaged** | Written by Asterisk on SIP register; read-only from Django |

`PsContact` is `managed=False` precisely because Asterisk owns its lifecycle —
the migration that creates the realtime schema must not touch it.

**Schema changes:** add a migration under `asterisk_state/migrations/` and ship
it via `python manage.py migrate_asterisk --all` (or `--database
asterisk_<tenant>`) in `build_production.sh`. `migrate_schemas` does **not**
touch these tables — the router's `allow_migrate` confines them to `asterisk_*`
aliases.

---

## 4. What triggers a sync

| Trigger | Path | When |
|---|---|---|
| **Signals** (real-time) | `crm/signals.py` `post_save`/`post_delete` on `UserPhoneAssignment`, `Trunk`, `Queue`, `InboundRoute`; `m2m_changed` on `User.tenant_groups` | Every product-model write. Resolves tenant via `connection.schema_name`; bails on `public`. |
| **Celery** (batch) | `crm.rebuild_tenant_asterisk_state(tenant_schema)` → `full_resync()` inside `schema_context` | Admin "resync" button, nightly cron, or after a bulk import. |
| **Startup warm** | `warm_aliases_for_all_tenants()` from `CrmConfig.ready()` | Registers aliases on worker boot (latency only — not a sync). |

Group-membership changes are the subtle one: editing `User.tenant_groups` fires
`m2m_changed`, which resyncs `queue_members` for every queue backed by the
affected group (`_tenant_groups_m2m_changed` in `crm/signals.py`).

---

## 5. Live call control — the AMI client

Separate from realtime provisioning, live call actions (list active channels,
redirect/transfer to a conference) use **AMI over a raw TCP socket**,
hand-rolled in `crm/views.py` (`_ami_connect_and_login`, `_ami_send_action`,
`_ami_get_channels` via `CoreShowChannels`, `_ami_redirect_to_confbridge`).
Credentials come from the current tenant's `PbxServer` (AMI user installed at
`/etc/asterisk/manager.d/echodesk.conf`).

> **Tech-debt note:** this is a from-scratch AMI implementation. A maintained
> library (`panoramisk`, `asterisk-ami`, or ARI via `ari-py`) is the natural
> replacement — it would add reconnection, robust parsing, and non-blocking IO.
> Out of scope for the current feature-freeze; flagged here so the next person
> doesn't re-derive the conclusion.

---

## 6. Operate & debug

**Inspect a tenant's realtime rows (from Django):**
```python
# manage.py shell
from tenant_schemas.utils import schema_context
from crm.asterisk_db import get_asterisk_connection_for_current_tenant
from asterisk_state.models import PsEndpoint, AsteriskQueueMember
with schema_context("amanati"):
    alias, pbx = get_asterisk_connection_for_current_tenant()
    print(alias, pbx)                                   # None,None ⇒ no active PbxServer
    print(list(PsEndpoint.objects.using(alias).values_list("id", flat=True)))
    print(list(AsteriskQueueMember.objects.using(alias).values("queue_name", "interface")))
```

**Force a full resync for a tenant:**
```python
from crm.tasks import rebuild_tenant_asterisk_state
rebuild_tenant_asterisk_state.delay("amanati")          # or .apply() to run inline
```

**Verify from the Asterisk side:** `asterisk -rx 'realtime show pgsql status'`,
`pjsip show endpoints`, `queue show` (see `pbx/BYO_PROVISIONING.md` §Verifying).

**Common failure modes:**
| Symptom | Likely cause | Where to look |
|---|---|---|
| Sync silently does nothing | No active `PbxServer`, or `ASTERISK_SYNC_ENABLED=False` | `_enabled()` returns False; check `PbxServer.status` |
| `the DB alias 'asterisk_x' isn't an allowed database` / write errors | Router returned `None` (no active PbxServer) | `get_active_pbx_for_current_tenant()` for that schema |
| Rows written but Asterisk ignores them | sorcery/`extconfig`/`res_pgsql` misconfig on the PBX | `pbx/CUTOVER_RUNBOOK.md`, `realtime show pgsql status` |
| Stale connection after cred rotation | alias cache | `register_pbx_alias` refreshes in place; restart the pod if in doubt |
| Endpoint IDs collide across tenants | `use_tenant_prefix` mismatch | `PbxServer.use_tenant_prefix`; see `prefix()` |

**Kill switch (incident):** set `ASTERISK_SYNC_ENABLED=False` to stop all
realtime writes without touching code; product CRUD continues unaffected.

---

## 7. Key-person-risk notes (for whoever inherits this)

The pieces that are *not* obvious from the code and would otherwise need to be
reverse-engineered:
- The per-tenant alias is **mutated into `connections.databases` at runtime** —
  there is no static config. Start at `crm/asterisk_db.py::register_pbx_alias`.
- The router **fails closed** (returns `None`) so a missing PBX never corrupts
  the default DB. This is deliberate, not a bug.
- `PsContact` is the **only** unmanaged shadow model — don't add it to a
  migration.
- Realtime provisioning (this doc) and **live call control (AMI, §5) are
  separate paths**; a problem in one does not imply a problem in the other.
- Related future-work decisions already investigated: dedicated DB cluster
  (`pbx/FUTURE_OPTION_B_DEDICATED_CLUSTER.md`) and sorcery memory cache
  (`pbx/FUTURE_MEMORY_CACHE_SYNTAX.md`).
