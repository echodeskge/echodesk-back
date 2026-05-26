# PBX integration — docs index

Start here if you're figuring out how EchoDesk talks to Asterisk.

## By audience

### Tenants / customers
- **[TENANT_CONNECT_GUIDE.md](TENANT_CONNECT_GUIDE.md)** — step-by-step
  "connect your own Asterisk to EchoDesk". Prereqs, UI walkthrough,
  install one-liner, troubleshooting, rollback.

### Support / internal ops
- **[BYO_PROVISIONING.md](BYO_PROVISIONING.md)** — what happens under
  the hood when a tenant clicks *Connect your PBX*. DB + role creation,
  install-ping flow, diagnostics.
- **[AMANATI_BYO_MIGRATION.md](AMANATI_BYO_MIGRATION.md)** — the first
  retrofit (pbx2.echodesk.cloud → `asterisk_amanati` DB with unprefixed
  IDs). Includes leftover cleanup SQL that runs after ~1 week of
  stability.

### Engineering / architecture
- **[../docs/ARCHITECTURE_PBX.md](../docs/ARCHITECTURE_PBX.md)** — the
  **code-side** architecture: how Django keeps Asterisk in sync
  (`AsteriskStateSync`), the runtime per-tenant DB-alias mechanism, the
  `asterisk_state` shadow models, what triggers a sync, the AMI client, and
  an operate/debug section. Start here if you're changing calling code.
- **[CUTOVER_RUNBOOK.md](CUTOVER_RUNBOOK.md)** — the Phase 1 shared-
  schema realtime cutover. Still the canonical reference for *why each
  config line exists* (sorcery blocks, extconfig `[settings]` merge,
  preload directive, PGSSLMODE, …). The tenant install script in
  Phase 2 generates exactly these files programmatically.
- **[DEPLOY_PBX2_SCRIPTS.md](DEPLOY_PBX2_SCRIPTS.md)** — the pre-Phase-2
  runbook for copying the AGI + dialplan edits onto pbx2. Superseded by
  the install script for new tenants, kept for reference on the AGI
  template itself.
- **[FUTURE_OPTION_B_DEDICATED_CLUSTER.md](FUTURE_OPTION_B_DEDICATED_CLUSTER.md)**
  — we currently co-locate all `asterisk_<tenant>` databases on the
  main app cluster (Option A). This doc records the trigger conditions
  + 30-minute migration recipe for splitting them onto a dedicated
  realtime cluster (Option B) when we hit ~10 tenants or compliance
  requires it.
- **[FUTURE_MEMORY_CACHE_SYNTAX.md](FUTURE_MEMORY_CACHE_SYNTAX.md)** —
  investigation trail on sorcery memory cache. Would save ~60 ms per
  call setup for tenants with cross-region DB latency (Tbilisi→Frankfurt
  today). The `memory_cache/realtime` wrapping syntax doesn't load on
  our Ubuntu 22.04 Asterisk 18.10 build — deferred until we have more
  tenants or a concrete latency complaint.

### Scripts shipped to the Asterisk server
- **echodesk-routing.py** — AGI called from `[from-provider]` dialplan.
  Queries `/api/pbx/call-routing/?did=` and exports channel variables
  (`QUEUE_NAME`, `TENANT_SCHEMA`, `DEST_EXTENSIONS`, sound URLs, etc.).
  Deployed to `/var/lib/asterisk/agi-bin/`.
- **extensions-incoming.conf** — template dialplan used by the install
  script. Uses `${QUEUE_TO_DIAL}` with a `support` fallback, so the
  dialplan is identical before and after the ARA cutover.

## By journey

### "I'm a new EchoDesk customer and want to connect my PBX."
Read `TENANT_CONNECT_GUIDE.md`. If anything fails, email
support@echodesk.ge with `/var/log/asterisk/messages` tail attached.

### "A tenant's install is stuck — what do I check?"
Read `BYO_PROVISIONING.md` § Troubleshooting. Common causes:
- Public IP not whitelisted on our DO Postgres.
- Their firewall blocks outbound 25060 or 443.
- Token expired (24h TTL).

### "I'm building a new feature that touches the realtime tables."
1. Read **[../docs/ARCHITECTURE_PBX.md](../docs/ARCHITECTURE_PBX.md)** for the
   full code-side flow (signals → `AsteriskStateSync` → runtime alias → shadow
   tables → router), then `CLAUDE.md` § *Asterisk realtime (ARA)* in the repo
   root for the `AsteriskStateSync.prefix()` convention.
2. For a schema change, add a migration under
   `echodesk-back/asterisk_state/migrations/` and ship it via
   `python manage.py migrate_asterisk --all` in `build_production.sh`.
3. Django signals in `crm/signals.py` already handle fan-out on writes
   to `UserPhoneAssignment` / `Queue` / `Trunk` / group membership.
4. Never bypass `AsteriskStateSync` — it's the single chokepoint that
   applies per-tenant DB routing and prefix toggling.

### "Amanati is misbehaving — where do I look first?"
1. Check `/settings/pbx/server/` in the EchoDesk UI — green badge +
   recent *Last seen*?
2. SSH to pbx2 and run `asterisk -rx 'realtime show pgsql status'`.
3. If not connected, check DO Postgres Trusted Sources and pbx2's
   outbound firewall.
4. `/var/log/asterisk/messages` + `journalctl -u asterisk` on pbx2.
5. Worst-case: restore `/etc/asterisk/res_pgsql.conf.bak.*` and
   `systemctl restart asterisk` to fall back to the pre-cutover
   state.

## Reference: what lives where

| File on the Asterisk server | Purpose | Installed by |
|---|---|---|
| `/etc/asterisk/res_pgsql.conf` | DB connection params for realtime | install script |
| `/etc/asterisk/sorcery.conf` | Routes pjsip objects to realtime | install script (appends) |
| `/etc/asterisk/extconfig.conf` | Maps realtime families to tables | install script (merges into `[settings]`) |
| `/etc/asterisk/modules.conf` | `preload => res_config_pgsql.so` | install script |
| `/etc/asterisk/manager.d/echodesk.conf` | AMI user used by EchoDesk | install script |
| `/etc/systemd/system/asterisk.service.d/echodesk.conf` | `PGSSLMODE=require`, start timeout | install script |
| `/var/lib/asterisk/agi-bin/echodesk-routing.py` | Inbound call routing AGI | manual copy (template in repo) |

| Postgres object | Created by |
|---|---|
| Database `asterisk_<tenant>` | `pbx_provisioning.provision_pbx_server_db` |
| Role `asterisk_rw_<tenant>` (LOGIN, encrypted pass) | same as above |
| 7 realtime tables in that DB's `public` schema | `manage.py migrate_asterisk --database asterisk_<tenant>` |
