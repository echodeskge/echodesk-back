"""One-line-install endpoints for a tenant's BYO Asterisk server.

Flow:

1. ``GET /api/pbx/install/<token>/`` — returns a self-contained bash
   script. The tenant runs:

       curl -sSL https://api.echodesk.ge/api/pbx/install/<token>/ | sudo bash

2. The script writes ``/etc/asterisk/res_pgsql.conf``, appends sorcery /
   extconfig stanzas, installs the AGI script, restarts Asterisk.

3. On success it ``POST``s to ``/api/pbx/install/<token>/ping`` which
   flips the PbxServer status to ``active`` and records the Asterisk
   version.

The token is a single bearer secret — do not log it. Token is rotated
when the admin clicks "Regenerate token" in the UI.
"""
from __future__ import annotations

import logging

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

log = logging.getLogger(__name__)


# Token → tenant lookup lives in crm.pbx_auth (one scanner for the AGI /
# webhook token and the install token; cached by the token's SHA-256).
from crm.pbx_auth import find_pbx_by_enrollment_token as _find_pbx_by_token


@csrf_exempt
@require_http_methods(["GET"])
def install_script(request, token: str):
    """Return the bash installer for this tenant's PBX.

    Served as ``text/x-shellscript`` so ``curl | bash`` works naturally.
    """
    schema, pbx_id = _find_pbx_by_token(token)
    if not schema:
        return HttpResponse("# Invalid or expired enrollment token\nexit 1\n",
                            status=404, content_type="text/plain")

    from tenant_schemas.utils import schema_context
    with schema_context(schema):
        from crm.models import PbxServer
        pbx = PbxServer.objects.filter(id=pbx_id).first()
        if pbx is None or (
            pbx.enrollment_expires_at and pbx.enrollment_expires_at < timezone.now()
        ):
            return HttpResponse("# Token expired — regenerate in EchoDesk\nexit 1\n",
                                status=410, content_type="text/plain")

        # Build script body with the server's creds baked in. Passwords
        # touch the wire here — that's unavoidable for an install script;
        # the user runs this exactly once and the network is HTTPS.
        script = _render_script(pbx, request)

    return HttpResponse(script, content_type="text/x-shellscript; charset=utf-8")


@csrf_exempt
@require_http_methods(["POST"])
def install_ping(request, token: str):
    """Called from the install script at the end to mark the PbxServer active."""
    schema, pbx_id = _find_pbx_by_token(token)
    if not schema:
        return JsonResponse({"error": "invalid token"}, status=404)

    import json
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        payload = {}

    from tenant_schemas.utils import schema_context
    with schema_context(schema):
        from crm.models import PbxServer
        pbx = PbxServer.objects.filter(id=pbx_id).first()
        if pbx is None:
            return JsonResponse({"error": "not found"}, status=404)

        pbx.status = PbxServer.STATUS_ACTIVE
        pbx.last_seen_at = timezone.now()
        pbx.asterisk_version = str(payload.get("asterisk_version", ""))[:64]
        pbx.save(update_fields=["status", "last_seen_at", "asterisk_version", "updated_at"])

    log.info("PBX install ping ok: tenant=%s pbx=%s", schema, pbx_id)
    return JsonResponse({"ok": True, "status": "active"})


def public_api_base(request) -> str:
    """``https://<host>`` the PBX should call back.

    Caddy terminates TLS and Django isn't told (no SECURE_PROXY_SSL_HEADER),
    so build_absolute_uri says http:// — and a POST to http:// gets a 308
    that curl doesn't follow, so the install ping never landed. Local dev
    hosts keep their scheme.
    """
    base = request.build_absolute_uri("/").rstrip("/")
    host = request.get_host().split(":")[0]
    if base.startswith("http://") and not (host in ("localhost", "127.0.0.1") or host.endswith(".lvh.me") or host == "lvh.me"):
        base = "https://" + base[len("http://"):]
    return base


_INTERNAL_DB_HOSTS = {"postgres", "pgbouncer", "localhost", "127.0.0.1", "db"}


def _public_db_endpoint(pbx):
    """Realtime DB host/port the PBX should dial.

    Provisioning stores the host the backend itself uses; on the Hetzner
    stack that's a docker-internal name the tenant's PBX can't resolve, so
    swap in ``ASTERISK_PUBLIC_DB_HOST`` for internal hosts only (servers
    still on an external cluster keep their stored host).
    """
    from django.conf import settings

    host, port = pbx.realtime_db_host, pbx.realtime_db_port
    public_host = getattr(settings, "ASTERISK_PUBLIC_DB_HOST", "")
    if public_host and (host or "").lower() in _INTERNAL_DB_HOSTS:
        host = public_host
        port = int(getattr(settings, "ASTERISK_PUBLIC_DB_PORT", "") or port or 5432)
    return host, port


def _ami_permit_lines() -> str:
    """``permit=`` lines for the EchoDesk AMI user (``PBX_AMI_PERMIT``)."""
    from django.conf import settings

    entries = [e.strip() for e in getattr(settings, "PBX_AMI_PERMIT", "").split(",") if e.strip()]
    if not entries:
        return "permit = 0.0.0.0/0.0.0.0"
    lines = ["deny = 0.0.0.0/0.0.0.0"]
    for entry in entries:
        lines.append(f"permit = {entry if '/' in entry else entry + '/255.255.255.255'}")
    return "\n".join(lines)


def _agi_script_source() -> str:
    """The AGI shipped in ``pbx/echodesk-routing.py``, newline-terminated."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "pbx" / "echodesk-routing.py").read_text()
    return source if source.endswith("\n") else source + "\n"


def _render_script(pbx, request) -> str:
    """Render the bash installer.

    Heredoc templating — no external template engine. Keep the script
    idempotent and fail-fast (``set -e``). Uses the same config blocks
    we hand-crafted on pbx2 in Phase 1.
    """
    api_base = public_api_base(request)
    ping_url = f"{api_base}/api/pbx/install/{pbx.enrollment_token}/ping/"

    # Shell-escape credentials that may contain special chars.
    import shlex
    db_pass_q = shlex.quote(pbx.realtime_db_password)
    ami_user_q = shlex.quote(pbx.ami_username or "echodesk")
    ami_pass_q = shlex.quote(pbx.ami_password or "")
    api_token_q = shlex.quote(pbx.enrollment_token)
    db_host, db_port = _public_db_endpoint(pbx)
    ami_permit_lines = _ami_permit_lines()
    agi_script = _agi_script_source()

    return f"""#!/bin/bash
# EchoDesk BYO Asterisk install script
# PbxServer: {pbx.name} ({pbx.fqdn})
# Generated: {timezone.now().isoformat()}
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Please run with sudo" >&2
  exit 1
fi

echo "==> EchoDesk Asterisk realtime install on $(hostname)"

# --- 1. Sanity checks -----------------------------------------------------
if ! command -v asterisk >/dev/null 2>&1; then
  echo "ERROR: asterisk binary not found. Install Asterisk 18+ first." >&2
  exit 2
fi

AST_VER=$(asterisk -V | head -1)
echo "  detected: $AST_VER"

# --- 2. Dependencies ------------------------------------------------------
apt-get update -qq
apt-get install -y -qq postgresql-client curl python3 python3-requests

# --- 3. Backups -----------------------------------------------------------
TS=$(date +%Y%m%d-%H%M%S)
for f in res_pgsql.conf sorcery.conf extconfig.conf modules.conf; do
  if [ -f /etc/asterisk/$f ]; then
    cp /etc/asterisk/$f /etc/asterisk/$f.bak.$TS
  fi
done
echo "  backups → /etc/asterisk/*.bak.$TS"

# --- 4. res_pgsql.conf ----------------------------------------------------
cat > /etc/asterisk/res_pgsql.conf <<PGCONF
; Managed by EchoDesk — do not hand-edit.
[general]
dbhost={db_host}
dbport={db_port}
dbname={pbx.realtime_db_name}
dbuser={pbx.realtime_db_user}
dbpass={db_pass_q}
dbappname=echodesk-{pbx.id}
requirements=warn
PGCONF
chown asterisk:asterisk /etc/asterisk/res_pgsql.conf
chmod 640 /etc/asterisk/res_pgsql.conf

# --- 5. extconfig.conf: map the realtime tables in [settings] ------------
# Line-based on purpose: the packaged extconfig.conf has '[' inside comments
# of [settings], which defeated the old "[settings][^[]*" regex and left the
# file silently unchanged (PJSIP then refuses to load).
python3 - <<'PYEOF'
import pathlib
p = pathlib.Path("/etc/asterisk/extconfig.conf")
lines = p.read_text().splitlines() if p.exists() else []
begin = "; === EchoDesk realtime (managed) ==="
end = "; === end EchoDesk realtime ==="
block = [
    begin,
    "ps_endpoints => pgsql,general,ps_endpoints",
    "ps_auths => pgsql,general,ps_auths",
    "ps_aors => pgsql,general,ps_aors",
    "ps_identifies => pgsql,general,ps_identifies",
    "ps_registrations => pgsql,general,ps_registrations",
    "ps_contacts => pgsql,general,ps_contacts",
    "queues => pgsql,general,queues",
    "queue_members => pgsql,general,queue_members",
    end,
]
# Strip any prior EchoDesk block so reruns don't duplicate. Blocks written by
# older installers have no end marker and run to the next section header.
out, skipping = [], None
for line in lines:
    if skipping is None and line.strip().startswith("; === EchoDesk realtime"):
        skipping = "marker"
        continue
    if skipping is not None:
        if line.strip() == end:
            skipping = None
            continue
        if line.lstrip().startswith("[") or not ("=>" in line or line.strip() == ""):
            skipping = None
        else:
            continue
    out.append(line)
# Insert right under the first [settings] header (add the section if missing).
for i, line in enumerate(out):
    if line.strip().lower().startswith("[settings]"):
        out[i + 1:i + 1] = block
        break
else:
    out += ["", "[settings]"] + block
p.write_text("\\n".join(out) + "\\n")
PYEOF

# --- 6. sorcery.conf: append PJSIP realtime wizard blocks -----------------
python3 - <<'PYEOF'
import re, pathlib
p = pathlib.Path("/etc/asterisk/sorcery.conf")
txt = p.read_text()
# Strip any previous EchoDesk block.
txt = re.sub(r"\\n; === EchoDesk realtime.*\\Z", "", txt, flags=re.DOTALL)
txt += \"\"\"
; === EchoDesk realtime (managed) ===
; NOTE: a sorcery memory_cache wizard in front of realtime would save
; ~60 ms per call setup (Tbilisi→Frankfurt RTT) but the
; 'memory_cache/<wizard>,<data>' syntax in Asterisk 18.10's build on
; Ubuntu 22.04 rejects the wrapped form ("Wizard 'memory_cache/realtime'
; could not be applied to object type 'endpoint' as it was not found").
; Deferred — see pbx/FUTURE_MEMORY_CACHE_SYNTAX.md for the investigation
; trail. For tenants whose PBX is colocated with the DO DB region the
; 60 ms saving isn't worth the complexity anyway.
[res_pjsip]
endpoint=realtime,ps_endpoints
endpoint=config,pjsip.conf,criteria=type=endpoint
auth=realtime,ps_auths
auth=config,pjsip.conf,criteria=type=auth
aor=realtime,ps_aors
aor=config,pjsip.conf,criteria=type=aor
system=config,pjsip.conf,criteria=type=system
global=config,pjsip.conf,criteria=type=global
transport=config,pjsip.conf,criteria=type=transport
domain_alias=config,pjsip.conf,criteria=type=domain_alias

[res_pjsip_endpoint_identifier_ip]
identify=realtime,ps_identifies
identify=config,pjsip.conf,criteria=type=identify

[res_pjsip_outbound_registration]
registration=config,pjsip.conf,criteria=type=registration
\"\"\"
p.write_text(txt)
PYEOF

# --- 7. modules.conf preload ---------------------------------------------
# Debian/Ubuntu's packaged modules.conf has "noload => res_config_pgsql.so",
# and noload beats preload — without this PJSIP fails to load at all.
sed -i 's/^[[:space:]]*noload[[:space:]]*=>[[:space:]]*res_config_pgsql\\.so/;&   ; disabled by EchoDesk/' /etc/asterisk/modules.conf
if ! grep -q "preload => res_config_pgsql.so" /etc/asterisk/modules.conf; then
  sed -i "/^\\[modules\\]/a preload => res_config_pgsql.so" /etc/asterisk/modules.conf
fi

# --- 8. systemd drop-ins for SSL + safety ---------------------------------
mkdir -p /etc/systemd/system/asterisk.service.d
cat > /etc/systemd/system/asterisk.service.d/echodesk.conf <<UNIT
[Service]
Environment="PGSSLMODE=require"
Environment="ECHODESK_API_URL={api_base}"
Environment="ECHODESK_PBX_TOKEN={pbx.api_token}"
TimeoutStartSec=300
UNIT
chmod 640 /etc/systemd/system/asterisk.service.d/echodesk.conf

# --- 8b. Inbound routing AGI (asks EchoDesk where each call goes) ---------
mkdir -p /var/lib/asterisk/agi-bin
cat > /var/lib/asterisk/agi-bin/echodesk-routing.py <<'AGIEOF'
{agi_script}AGIEOF
chmod 755 /var/lib/asterisk/agi-bin/echodesk-routing.py
chown asterisk:asterisk /var/lib/asterisk/agi-bin/echodesk-routing.py
# AGI(echodesk-routing.py) resolves against astagidir, which Debian/Ubuntu
# packages set to /usr/share/asterisk/agi-bin — link the script there too.
AGI_DIR=$(sed -n 's/^[[:space:]]*astagidir[[:space:]]*=>*[[:space:]]*//p' /etc/asterisk/asterisk.conf 2>/dev/null | head -1)
if [ -n "$AGI_DIR" ] && [ "$AGI_DIR" != /var/lib/asterisk/agi-bin ]; then
  mkdir -p "$AGI_DIR"
  ln -sf /var/lib/asterisk/agi-bin/echodesk-routing.py "$AGI_DIR/echodesk-routing.py"
fi
# Prompts are played from EchoDesk URLs via res_http_media_cache, which
# downloads into astcachedir; Ubuntu's package points that at
# /var/cache/asterisk but never creates it.
CACHE_DIR=$(sed -n 's/^[[:space:]]*astcachedir[[:space:]]*=>*[[:space:]]*//p' /etc/asterisk/asterisk.conf 2>/dev/null | head -1)
CACHE_DIR=${{CACHE_DIR:-/var/cache/asterisk}}
mkdir -p "$CACHE_DIR"
chown asterisk:asterisk "$CACHE_DIR"

# --- 9. AMI user ---------------------------------------------------------
mkdir -p /etc/asterisk/manager.d
cat > /etc/asterisk/manager.d/echodesk.conf <<AMICONF
[{ami_user_q}]
secret = {ami_pass_q}
{ami_permit_lines}
read = all
write = system,call,command,originate,reporting
AMICONF
chown -R asterisk:asterisk /etc/asterisk/manager.d
chmod 640 /etc/asterisk/manager.d/echodesk.conf
if ! grep -q 'manager.d/' /etc/asterisk/manager.conf 2>/dev/null; then
  printf '\n#include "manager.d/*.conf"\n' >> /etc/asterisk/manager.conf
fi

# --- 10. Reload + verify -------------------------------------------------
systemctl daemon-reload
systemctl reset-failed asterisk
systemctl restart asterisk

# Wait up to 60s for realtime connection to come up.
for i in $(seq 1 30); do
  if asterisk -rx 'realtime show pgsql status' 2>/dev/null | grep -q 'Connected'; then
    break
  fi
  sleep 2
done

STATUS=$(asterisk -rx 'realtime show pgsql status' 2>&1 | head -1)
echo "  realtime: $STATUS"
if ! echo "$STATUS" | grep -q 'Connected'; then
  echo "  WARNING: Asterisk is not connected to the EchoDesk realtime database." >&2
  echo "           Check outbound tcp/5432 and that EchoDesk has allowed this server's IP." >&2
fi
if ! asterisk -rx 'module show like res_pjsip.so' 2>/dev/null | grep -q 'Running'; then
  echo "  WARNING: res_pjsip is not running — see /var/log/asterisk/messages.log." >&2
fi

# --- 11. Ping back -------------------------------------------------------
curl -fsSL -X POST -H 'Content-Type: application/json' \\
  --data "{{\\"asterisk_version\\": \\"$AST_VER\\"}}" \\
  {shlex.quote(ping_url)} >/dev/null
echo "==> EchoDesk install complete."
"""
