"""One-line-install endpoints for a tenant's BYO Asterisk server.

Flow:

1. ``GET /api/pbx/install/<token>/`` — returns a self-contained bash
   script. The tenant runs:

       curl -sSL https://api.echodesk.ge/api/pbx/install/<token>/ | sudo bash

2. The script writes ``/etc/asterisk/res_pgsql.conf``, appends sorcery /
   extconfig stanzas, installs the AGI script, writes the EchoDesk dialplan
   (``/etc/asterisk/extensions_echodesk.conf``, ``#include``d from
   ``extensions.conf``), restarts Asterisk.

3. On success it ``POST``s to ``/api/pbx/install/<token>/ping`` which
   flips the PbxServer status to ``active`` and records the Asterisk
   version.

4. ``GET /api/pbx/install/<token>/dialplan/`` returns the dialplan alone,
   so the admin can re-pull it after adding or renaming a trunk (the
   outbound trunk's endpoint name is baked into ``[outbound]``).

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
        script = _render_script(pbx, request, schema, _active_trunk_endpoint(pbx, schema))

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


DIALPLAN_PATH = "/etc/asterisk/extensions_echodesk.conf"
DIALPLAN_INCLUDE = '#include "extensions_echodesk.conf"'


def _active_trunk_endpoint(pbx, schema: str) -> str:
    """PJSIP endpoint name of the trunk outbound calls leave through.

    The tenant's active trunks ordered by id (the default trunk first when
    one is flagged); the endpoint name is the one the sync layer wrote to
    ``ps_endpoints`` (``trunk_<slug>``, tenant-prefixed on legacy servers).
    Empty string when the tenant has no active trunk yet.
    """
    from crm.asterisk_sync import AsteriskStateSync, _slugify_trunk
    from crm.models import Trunk

    trunk = Trunk.objects.filter(is_active=True).order_by("-is_default", "id").first()
    if trunk is None:
        return ""
    return AsteriskStateSync.prefix_with(
        schema, f"trunk_{_slugify_trunk(trunk)}", bool(getattr(pbx, "use_tenant_prefix", False)),
    )


_DIALPLAN_TEMPLATE = r"""; Managed by EchoDesk — do not hand-edit; the installer rewrites this file
; (the previous copy is kept as extensions_echodesk.conf.bak.<timestamp>).
; Tenant: @@SCHEMA@@
; PBX: @@PBX_NAME@@ (@@PBX_FQDN@@)
; API: @@API_BASE@@
; Outbound trunk: @@TRUNK_LABEL@@
; Generated: @@GENERATED@@
;
; Re-pull after changing trunks in EchoDesk (the trunk name is baked in):
;   curl -fsSL @@API_BASE@@/api/pbx/install/<install-token>/dialplan/ \
;     > @@DIALPLAN_PATH@@ && asterisk -rx 'dialplan reload'
;
; ECHODESK_API_URL and ECHODESK_PBX_TOKEN come from the systemd drop-in
; (/etc/systemd/system/asterisk.service.d/echodesk.conf) — the same values the
; routing AGI uses. The token is never a dialplan variable: a [globals] entry
; would be written to the log at every dialplan load ("Setting global variable
; ... to '<token>'"), so the curl inside ${SHELL(...)} reads $ECHODESK_PBX_TOKEN
; from the shell environment itself (Asterisk only expands ${...} / $[...]),
; and only the HTTP status is logged. ENV() here needs func_env preloaded
; (modules.conf: the installer adds "preload => func_env.so").

[globals]
ECHODESK_API=${ENV(ECHODESK_API_URL)}
RECORDING_BASE=@@RECORDING_BASE@@
MAX_WAIT=60

; ---------------------------------------------------------------------------
; Calls placed by staff (browser softphone). PJSIP endpoints of this tenant's
; extensions have context=tenant_@@SCHEMA@@ (crm/asterisk_sync.py).
; ---------------------------------------------------------------------------
[tenant_@@SCHEMA@@]
; extension to extension (EchoDesk extensions are 3-4 digits)
exten => _[1-9]XX,1,Goto(internal,${EXTEN},1)
exten => _[1-9]XXX,1,Goto(internal,${EXTEN},1)
; longer numbers / +E.164 go out through the provider trunk
exten => _XXXXX.,1,Goto(outbound,${EXTEN},1)
exten => _+X.,1,Goto(outbound,${EXTEN},1)

[internal]
exten => _X.,1,Dial(PJSIP/@@EXT_PREFIX@@${EXTEN},30,b(echodesk-predial^s^1))
 same => n,Hangup()

; Pre-dial handler on every agent leg (Dial b() / Queue b()), run before the
; INVITE goes out: the browser reads X-EchoDesk-Call and posts it as
; pbx_call_id so sibling ringall legs are matched exactly.
[echodesk-predial]
exten => s,1,Set(PJSIP_HEADER(add,X-EchoDesk-Call)=${LINKEDID})
 same => n,Return()

@@OUTBOUND_BODY@@

; ---------------------------------------------------------------------------
; Calls from the provider: EchoDesk decides where they go. Trunk endpoints
; have context=from-provider-@@SCHEMA@@ (crm/asterisk_sync.py).
; ---------------------------------------------------------------------------
[from-provider-@@SCHEMA@@]
exten => _X.,1,Goto(echodesk-inbound,${EXTEN},1)
exten => _+X.,1,Goto(echodesk-inbound,${EXTEN},1)
exten => s,1,Goto(echodesk-inbound,s,1)

; Channel variables set by AGI(echodesk-routing.py) — see pbx/echodesk-routing.py:
;   IS_WORKING, ROUTE_ACTION (queue|extension|voicemail|ivr_custom|hangup),
;   AFTER_HOURS_ACTION (announcement|voicemail|forward), FORWARD_NUMBER, VM_ENABLED,
;   QUEUE_NAME, QUEUE_SLUG, DEST_EXTENSIONS (comma-joined), IVR_CONTEXT, TENANT_SCHEMA,
;   SOUND_GREETING, SOUND_AFTER_HOURS, SOUND_QUEUE_HOLD, SOUND_VOICEMAIL_PROMPT,
;   SOUND_THANK_YOU, SOUND_TRANSFER_HOLD (URLs played through res_http_media_cache).
; SIPCALLID is read before the leg dials, on PJSIP channels only (Local channels
; from `channel originate` have none), so the hangup handler never touches the
; SIP session after it is gone.
[echodesk-inbound]
; _[+s0-9]! (not _X.) also matches the bare "s" the provider sends with no number
exten => _[+s0-9]!,1,Answer()
 same => n,Wait(0.5)
 same => n,AGI(echodesk-routing.py)
 same => n,Set(DIALED_NUMBER=${EXTEN})
 same => n,Set(REC_FILE=${UNIQUEID}.wav)
 same => n,Set(SIPCALLID=)
 same => n,GotoIf($["${CHANNEL(channeltype)}" != "PJSIP"]?rec)
 same => n,Set(SIPCALLID=${FILTER(a-zA-Z0-9@._-,${CHANNEL(pjsip,call-id)})})
 same => n(rec),MixMonitor(/var/spool/asterisk/recording/${REC_FILE},b)
 same => n,Set(CHANNEL(hangup_handler_push)=echodesk-recording,s,1)
 same => n,GotoIf($["${IS_WORKING}" = "false"]?after_hours)
 same => n,ExecIf($["${SOUND_GREETING}" != ""]?Playback(${SOUND_GREETING}))
 same => n,GotoIf($["${ROUTE_ACTION}" = "extension"]?to_ext)
 same => n,GotoIf($["${ROUTE_ACTION}" = "voicemail"]?vm)
 same => n,GotoIf($["${ROUTE_ACTION}" = "hangup"]?bye)
 same => n,GotoIf($["${ROUTE_ACTION}" = "ivr_custom" & "${IVR_CONTEXT}" != ""]?ivr)
 ; default: queue (falls back to "support" when EchoDesk is unreachable)
 same => n,Set(QUEUE_TO_DIAL=${IF($["${QUEUE_NAME}" != ""]?${QUEUE_NAME}:support)})
 same => n,ExecIf($["${SOUND_QUEUE_HOLD}" != ""]?Set(CHANNEL(musicclass)=echodesk_custom))
 ; b() runs echodesk-predial on each member leg before its INVITE goes out
 same => n,Queue(${QUEUE_TO_DIAL},tTb(echodesk-predial^s^1),,,${MAX_WAIT})
 same => n,ExecIf($["${SOUND_THANK_YOU}" != ""]?Playback(${SOUND_THANK_YOU}))
 same => n,Hangup()
 ; direct extension(s): EchoDesk's "100,101" becomes PJSIP/100&PJSIP/101 (STRREPLACE, not REPLACE)
 same => n(to_ext),Dial(PJSIP/@@EXT_PREFIX@@${STRREPLACE(DEST_EXTENSIONS,\,,&PJSIP/@@EXT_PREFIX@@)},30,b(echodesk-predial^s^1))
 same => n,Goto(vm)
 same => n(ivr),Goto(${IVR_CONTEXT},s,1)
 same => n(after_hours),ExecIf($["${SOUND_AFTER_HOURS}" != ""]?Playback(${SOUND_AFTER_HOURS}))
 same => n,GotoIf($["${AFTER_HOURS_ACTION}" = "voicemail"]?vm)
 same => n,GotoIf($["${AFTER_HOURS_ACTION}" = "forward" & "${FORWARD_NUMBER}" != ""]?fwd)
 same => n(bye),Hangup()
 ; mailbox = the DID, context "default" (voicemail.conf)
 same => n(vm),ExecIf($["${SOUND_VOICEMAIL_PROMPT}" != ""]?Playback(${SOUND_VOICEMAIL_PROMPT}))
 same => n,VoiceMail(${EXTEN}@default,su)
 same => n,Hangup()
@@FORWARD_BODY@@

; ---------------------------------------------------------------------------
; After every recorded call: tell EchoDesk where the recording is.
; Runs as a hangup handler, so ${EXTEN} is "s": the dialed number was saved in
; DIALED_NUMBER before the handler was pushed, SIPCALLID likewise (read here only
; when the leg did not capture it). The numbers are digit-filtered and SIPCALLID
; token-filtered: they end up on a shell command line.
; ---------------------------------------------------------------------------
[echodesk-recording]
exten => s,1,NoOp(EchoDesk: recording ${REC_FILE} for linkedid ${LINKEDID})
 same => n,GotoIf($["${SIPCALLID}" != "" | "${CHANNEL(channeltype)}" != "PJSIP"]?post)
 same => n,Set(SIPCALLID=${FILTER(a-zA-Z0-9@._-,${CHANNEL(pjsip,call-id)})})
 same => n(post),Set(WEBHOOK_HTTP=${SHELL(curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST -H 'Content-Type: application/json' -H "X-PBX-Token: $ECHODESK_PBX_TOKEN" -d '{"caller_number":"${FILTER(+0-9,${CALLERID(num)})}","recipient_number":"${FILTER(+0-9,${DIALED_NUMBER})}","recording_url":"${RECORDING_BASE}${REC_FILE}","pbx_call_id":"${LINKEDID}","sip_call_id":"${SIPCALLID}"}' ${ECHODESK_API}/api/webhooks/call-recording-url/)})
 same => n,NoOp(EchoDesk recording webhook -> HTTP ${WEBHOOK_HTTP} for ${RECORDING_BASE}${REC_FILE})
 same => n,Return()

; merge_conference redirects both legs here (AMI Redirect, exten conf_<id>).
[confbridge-dynamic]
exten => _conf_.,1,ConfBridge(${EXTEN})
 same => n,Hangup()
"""

_OUTBOUND_WITH_TRUNK = r"""[outbound]
exten => _X.,1,Set(DIALED_NUMBER=${EXTEN})
 same => n,Set(REC_FILE=${UNIQUEID}.wav)
 same => n,Set(SIPCALLID=)
 same => n,GotoIf($["${CHANNEL(channeltype)}" != "PJSIP"]?rec)
 same => n,Set(SIPCALLID=${FILTER(a-zA-Z0-9@._-,${CHANNEL(pjsip,call-id)})})
 same => n(rec),MixMonitor(/var/spool/asterisk/recording/${REC_FILE},b)
 same => n,Set(CHANNEL(hangup_handler_push)=echodesk-recording,s,1)
 same => n,Dial(PJSIP/${EXTEN}@@@TRUNK@@,60)
 same => n,Hangup()
exten => _+X.,1,Goto(outbound,${EXTEN:1},1)"""

_OUTBOUND_NO_TRUNK = r"""; No active provider trunk existed in EchoDesk when this dialplan was generated,
; so outbound calls play "no service". Create the trunk (Settings -> PBX -> Trunks)
; and then re-pull this file (see the header) or re-run the install command.
[outbound]
exten => _X.,1,NoOp(EchoDesk: no provider trunk configured, outbound ${EXTEN} refused - re-pull the dialplan after creating one)
 same => n,Answer()
 same => n,Playback(ss-noservice)
 same => n,Hangup()
exten => _+X.,1,Goto(outbound,${EXTEN:1},1)"""

_FORWARD_WITH_TRUNK = r""" same => n(fwd),Dial(PJSIP/${FORWARD_NUMBER}@@@TRUNK@@,30)
 same => n,Hangup()"""

_FORWARD_NO_TRUNK = r""" ; after-hours forwarding needs a provider trunk: none was configured at generation time
 same => n(fwd),NoOp(EchoDesk: no provider trunk configured, cannot forward to ${FORWARD_NUMBER} - re-pull the dialplan after creating one)
 same => n,Hangup()"""


def _render_dialplan(pbx, schema: str, trunk_endpoint: str, api_base: str) -> str:
    """The EchoDesk-managed dialplan (``/etc/asterisk/extensions_echodesk.conf``).

    Pure text: no secrets. The API URL and the PBX token are read at load
    time from the environment the systemd drop-in sets, so the same file
    is valid on every server of the tenant. ``trunk_endpoint`` is the PJSIP
    endpoint outbound calls use (``""`` when the tenant has no trunk yet).
    ``schema`` is the tenant schema the contexts are named after.
    """
    recording_base = (getattr(pbx, "recording_base_url", "") or "").strip()
    if recording_base and not recording_base.endswith("/"):
        recording_base += "/"
    ext_prefix = f"{schema}_" if getattr(pbx, "use_tenant_prefix", False) else ""

    outbound = _OUTBOUND_WITH_TRUNK if trunk_endpoint else _OUTBOUND_NO_TRUNK
    forward = _FORWARD_WITH_TRUNK if trunk_endpoint else _FORWARD_NO_TRUNK
    text = (
        _DIALPLAN_TEMPLATE
        .replace("@@OUTBOUND_BODY@@", outbound)
        .replace("@@FORWARD_BODY@@", forward)
    )
    replacements = {
        "@@SCHEMA@@": schema,
        "@@PBX_NAME@@": str(getattr(pbx, "name", "") or ""),
        "@@PBX_FQDN@@": str(getattr(pbx, "fqdn", "") or ""),
        "@@API_BASE@@": api_base,
        "@@TRUNK_LABEL@@": trunk_endpoint or "(none — outbound disabled)",
        "@@TRUNK@@": trunk_endpoint,
        "@@EXT_PREFIX@@": ext_prefix,
        "@@RECORDING_BASE@@": recording_base,
        "@@DIALPLAN_PATH@@": DIALPLAN_PATH,
        "@@GENERATED@@": timezone.now().isoformat(),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text if text.endswith("\n") else text + "\n"


def _render_script(pbx, request, schema: str = "", trunk_endpoint: str = "") -> str:
    """Render the bash installer.

    Heredoc templating — no external template engine. Keep the script
    idempotent and fail-fast (``set -e``). Uses the same config blocks
    we hand-crafted on pbx2 in Phase 1.

    ``schema`` names the tenant contexts in the generated dialplan (defaults
    to the schema of the current connection) and ``trunk_endpoint`` is the
    outbound trunk endpoint (``_active_trunk_endpoint``); callers outside a
    tenant context pass both explicitly.
    """
    api_base = public_api_base(request)
    ping_url = f"{api_base}/api/pbx/install/{pbx.enrollment_token}/ping/"
    if not schema:
        from django.db import connection
        schema = getattr(connection, "schema_name", "") or "public"

    # Shell-escape credentials that may contain special chars.
    import shlex
    db_pass_q = shlex.quote(pbx.realtime_db_password)
    ami_user_q = shlex.quote(pbx.ami_username or "echodesk")
    ami_pass_q = shlex.quote(pbx.ami_password or "")
    db_host, db_port = _public_db_endpoint(pbx)
    ami_permit_lines = _ami_permit_lines()
    agi_script = _agi_script_source()
    dialplan = _render_dialplan(pbx, schema, trunk_endpoint, api_base)

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
for f in res_pgsql.conf sorcery.conf extconfig.conf modules.conf extensions.conf; do
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
# The EchoDesk dialplan's [globals] uses ${{ENV(ECHODESK_API_URL)}}; pbx_config
# parses it before func_env would autoload, so preload that too.
if ! grep -q "preload => func_env.so" /etc/asterisk/modules.conf; then
  sed -i "/^\\[modules\\]/a preload => func_env.so" /etc/asterisk/modules.conf
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

# --- 8c. Dialplan: EchoDesk-managed contexts ------------------------------
# Written to its own file and #included from extensions.conf, so a hand-
# written extensions.conf is never rewritten. No secrets inside: the API
# URL and token are read from the drop-in (step 8) at load time.
if [ -f {DIALPLAN_PATH} ]; then
  cp {DIALPLAN_PATH} {DIALPLAN_PATH}.bak.$TS
fi
cat > {DIALPLAN_PATH} <<'DIALPLANEOF'
{dialplan}DIALPLANEOF
chown asterisk:asterisk {DIALPLAN_PATH}
chmod 640 {DIALPLAN_PATH}
mkdir -p /var/spool/asterisk/recording
chown asterisk:asterisk /var/spool/asterisk/recording
touch /etc/asterisk/extensions.conf
if ! grep -q '{DIALPLAN_INCLUDE}' /etc/asterisk/extensions.conf; then
  printf '\\n%s\\n' '{DIALPLAN_INCLUDE}' >> /etc/asterisk/extensions.conf
fi
# Pick the new contexts up right away when Asterisk is already running; the
# restart in step 10 loads them otherwise.
asterisk -rx 'dialplan reload' >/dev/null 2>&1 || true

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


@csrf_exempt
@require_http_methods(["GET"])
def install_dialplan(request, token: str):
    """Return the rendered ``extensions_echodesk.conf`` for this PBX.

    Same token as the install script. The outbound trunk's endpoint name is
    baked into the dialplan, so after adding or renaming a trunk in EchoDesk
    the admin re-pulls it::

        curl -fsSL https://<tenant>.api.echodesk.ge/api/pbx/install/<token>/dialplan/ \\
          > /etc/asterisk/extensions_echodesk.conf && asterisk -rx 'dialplan reload'
    """
    schema, pbx_id = _find_pbx_by_token(token)
    if not schema:
        return HttpResponse("; Invalid or expired enrollment token\n",
                            status=404, content_type="text/plain")

    from tenant_schemas.utils import schema_context
    with schema_context(schema):
        from crm.models import PbxServer
        pbx = PbxServer.objects.filter(id=pbx_id).first()
        if pbx is None or (
            pbx.enrollment_expires_at and pbx.enrollment_expires_at < timezone.now()
        ):
            return HttpResponse("; Token expired — regenerate in EchoDesk\n",
                                status=410, content_type="text/plain")
        dialplan = _render_dialplan(
            pbx, schema, _active_trunk_endpoint(pbx, schema), public_api_base(request),
        )

    return HttpResponse(dialplan, content_type="text/plain; charset=utf-8")
