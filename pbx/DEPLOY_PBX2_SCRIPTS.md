# Deploying PBX scripts to pbx2.echodesk.cloud

The AGI script and dialplan in this `pbx/` directory now consume the new
`queue_name` / `queue_slug` / `inbound_route` fields returned by
`/api/pbx/call-routing/` (populated from `InboundRoute` + `Queue` models).

This is a **one-time manual update** on `pbx2.echodesk.cloud` (185.229.109.65).
The ARA (Asterisk Realtime) cutover is still pending, so the queue name sent
by the API is the bare slug (`support`) — which matches what Asterisk has in
`queues.conf` today. After the ARA migration we'll flip
`ASTERISK_SYNC_ENABLED=True` and the API will return `amanati_support`; the
dialplan is already tolerant of either.

## 1. Push the updated AGI script

From your laptop:

```bash
scp echodesk-back/pbx/echodesk-routing.py \
    root@185.229.109.65:/var/lib/asterisk/agi-bin/echodesk-routing.py
ssh root@185.229.109.65 'chmod +x /var/lib/asterisk/agi-bin/echodesk-routing.py'
```

Verify on the server:

```bash
ssh root@185.229.109.65 \
  'head -n 3 /var/lib/asterisk/agi-bin/echodesk-routing.py && \
   grep -c QUEUE_NAME /var/lib/asterisk/agi-bin/echodesk-routing.py'
```

The grep should return `>= 2` lines.

## 2. Update the live dialplan on pbx2

The live dialplan on pbx2 uses `[from-provider]` + `[queue-with-announce]` (not
the `[incoming]` context in this repo's `extensions-incoming.conf`). Apply the
same change directly to `/etc/asterisk/extensions.conf`:

1. SSH in and back up: `cp /etc/asterisk/extensions.conf{,.bak.$(date +%F)}`.
2. Edit the `[from-provider]` context so the `Queue()` application uses
   `${QUEUE_NAME}` with a fallback:

    ```asterisk
    [from-provider]
    ; … existing answer + AGI steps …
    same => n,Set(QUEUE_TO_DIAL=${IF($["${QUEUE_NAME}" != ""]?${QUEUE_NAME}:support)})
    same => n,Goto(queue-with-announce,${EXTEN},1)

    [queue-with-announce]
    exten => _X.,1,NoOp(Entering queue ${QUEUE_TO_DIAL})
    same => n,Queue(${QUEUE_TO_DIAL},t,,,60)
    ; … rest unchanged …
    ```

3. Reload dialplan:

    ```bash
    asterisk -rx 'dialplan reload'
    ```

## 3. Smoke test

```bash
# Simulate the API call the AGI makes
curl -s -H "Authorization: Bearer ${PBX_SHARED_SECRET}" \
  'https://api.echodesk.ge/api/pbx/call-routing/?did=%2B995322421219' | jq .
```

Expected response includes:

```json
{
  "queue_name": "support",
  "queue_slug": "support",
  "tenant_schema": "amanati",
  "inbound_route": {
    "id": <n>,
    "did": "+995322421219",
    "destination_type": "queue",
    "queue_slug": "support",
    "queue_name": "support",
    "priority": 100
  },
  "extensions": ["100", "101", …]
}
```

Place a real inbound call to `+995 32 242 1219` and tail the log:

```bash
ssh root@185.229.109.65 'tail -f /var/log/asterisk/echodesk-routing.log'
```

The log should show `queue=support, tenant=amanati`.

## 4. After-ARA-cutover follow-up

Once `ASTERISK_SYNC_ENABLED=True` + the realtime migration is applied, the
API starts returning `queue_name: amanati_support`. The dialplan change in
step 2 already handles it — no script changes needed at that point.

## 5. Calls integration: PBX token, call ids, new contexts (manual, one-time)

New BYO servers get all of this from the installer (`extensions_echodesk.conf`,
see `PBX_SERVER_SETUP_GUIDE.md` step 6). pbx2's dialplan is hand-edited, so apply
the same changes by hand. Nothing here touches the realtime tables.

Why: the backend now authenticates every PBX webhook and the routing AGI with
the PBX's long-lived **API token** (`PbxServer.api_token`, header `X-PBX-Token`)
instead of the 24 h enrollment token, matches recordings to calls by
`pbx_call_id` (Asterisk `LINKEDID`) before the last-7-digits fallback, and the
browser softphone reads the `X-EchoDesk-Call` SIP header to tell "answered by a
colleague" from "missed". `merge_conference` redirects channels into
`confbridge-dynamic`, which pbx2 never had.

Order matters for the deploy (see the deploy plan): set `PBX_SHARED_SECRET` in
the backend env and give pbx2's curl an `Authorization: Bearer` header **before**
the backend deploy, then switch to the token afterwards. The steps below describe
the end state with the token.

1. Get the API token: Settings → PBX → PBX Server in the Amanati workspace (or
   `PbxServer.objects.get().api_token` in a `schema_context('amanati')` shell).
   Never paste it into chat, tickets or this repo.

2. Put it in the systemd drop-in (`ECHODESK_API_URL` is already there):

    ```bash
    ssh root@185.229.109.65
    cp /etc/systemd/system/asterisk.service.d/echodesk.conf{,.bak.$(date +%F)}
    sed -i 's|^Environment="ECHODESK_PBX_TOKEN=.*|Environment="ECHODESK_PBX_TOKEN=<api_token>"|' \
      /etc/systemd/system/asterisk.service.d/echodesk.conf
    grep -q ECHODESK_PBX_TOKEN /etc/systemd/system/asterisk.service.d/echodesk.conf \
      || sed -i '/^\[Service\]/a Environment="ECHODESK_PBX_TOKEN=<api_token>"' \
           /etc/systemd/system/asterisk.service.d/echodesk.conf
    systemctl daemon-reload
    ```

   The AGI and the hangup handler's curl read it from the Asterisk process
   environment on every call, but Asterisk only picks up a changed environment on
   restart (`systemctl restart asterisk` — do it off-hours). Until then both keep
   sending the old value. Never put the token in `[globals]`: Asterisk writes
   every global's value to the full log on each `dialplan reload`.

3. Edit `/etc/asterisk/extensions.conf` (back it up first:
   `cp /etc/asterisk/extensions.conf{,.bak.$(date +%F)}`):

   a. `[globals]` — nothing to add (keep the existing `ECHODESK_API` /
      `RECORDING_BASE`; the token is **not** a global, see step 2).

   b. Add the two new contexts anywhere in the file:

      ```asterisk
      [echodesk-predial]
      exten => s,1,Set(PJSIP_HEADER(add,X-EchoDesk-Call)=${LINKEDID})
       same => n,Return()

      [confbridge-dynamic]
      exten => _conf_.,1,ConfBridge(${EXTEN})
       same => n,Hangup()
      ```

   c. Make every agent leg run the pre-dial handler so the header is on the INVITE
      (the `b()` option — the Queue `gosub` argument runs after answer, too late):

      ```asterisk
      ; [queue-with-announce]
      same => n,Queue(${QUEUE_TO_DIAL},tTb(echodesk-predial^s^1),,,60)
      ; every Dial(PJSIP/<ext>…) to an extension, e.g. in the internal context
      same => n,Dial(PJSIP/${EXTEN},30,b(echodesk-predial^s^1))
      ```

   d. In the hand-edited `[echodesk-recording]` hangup handler add the token header
      and the call ids to the curl, the same way the generated dialplan does it.
      Capture the SIP Call-ID before the recorded leg dials (PJSIP channels only —
      a Local channel from AMI `Originate` has none), keep the numbers
      digit-filtered — they end up on a shell command line — and log only the HTTP
      status, never the token:

      ```asterisk
      ; before MixMonitor / hangup_handler_push on the recorded leg
      same => n,Set(SIPCALLID=)
      same => n,GotoIf($["${CHANNEL(channeltype)}" != "PJSIP"]?rec)
      same => n,Set(SIPCALLID=${FILTER(a-zA-Z0-9@._-,${CHANNEL(pjsip,call-id)})})
      same => n(rec),MixMonitor(/var/spool/asterisk/recording/${REC_FILE},b)

      ; [echodesk-recording] — ARG1/ARG2 are what pbx2 already passes (caller, dialed number).
      ; $ECHODESK_PBX_TOKEN (no braces) is expanded by the shell, not by Asterisk.
      same => n,Set(WEBHOOK_HTTP=${SHELL(curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST -H 'Content-Type: application/json' -H "X-PBX-Token: $ECHODESK_PBX_TOKEN" -d '{"caller_number":"${FILTER(+0-9,${ARG1})}","recipient_number":"${FILTER(+0-9,${ARG2})}","recording_url":"${RECORDING_BASE}/${REC_FILE}","pbx_call_id":"${LINKEDID}","sip_call_id":"${SIPCALLID}"}' ${ECHODESK_API}/api/webhooks/call-recording-url/)})
      same => n,NoOp(EchoDesk recording webhook -> HTTP ${WEBHOOK_HTTP} for ${RECORDING_BASE}/${REC_FILE})
      ```

      (During the pre-deploy window use `-H "Authorization: Bearer <PBX_SHARED_SECRET>"`
      instead; the current backend ignores it and the new one accepts either.)

4. Reload and verify:

    ```bash
    asterisk -rx 'dialplan reload'
    asterisk -rx 'dialplan show echodesk-predial'
    asterisk -rx 'dialplan show confbridge-dynamic'
    systemctl restart asterisk            # picks up ECHODESK_PBX_TOKEN for the AGI and SHELL()
    systemctl show asterisk -p Environment | grep -c ECHODESK_PBX_TOKEN   # 1
    grep -c "ECHODESK_TOKEN" /etc/asterisk/extensions.conf                # 0: never a global
    ```

5. Smoke test the token without placing a call (the backend must already be
   deployed; `404 No call found` is fine — it means the auth passed):

    ```bash
    curl -s -o /dev/null -w '%{http_code}\n' -X POST \
      -H 'Content-Type: application/json' -H "X-PBX-Token: $(systemctl show asterisk -p Environment | tr ' ' '\n' | sed -n 's/^ECHODESK_PBX_TOKEN=//p')" \
      -d '{"caller_number":"+995000000000","recording_url":"https://pbx2.echodesk.cloud:8443/recordings/x.wav"}' \
      https://amanati.api.echodesk.ge/api/webhooks/call-recording-url/
    curl -s -o /dev/null -w '%{http_code}\n' -H 'X-PBX-Token: wrong' \
      'https://amanati.api.echodesk.ge/api/pbx/call-routing/?did=%2B995322421219'   # 401
    ```

   Then place a call: the browser softphone shows the incoming call with the
   `X-EchoDesk-Call` header (`pbx_call_id` on the call log row), and after hangup
   the recording appears on the row without the last-7-digits fallback.

Do not replace pbx2's dialplan with the generated `extensions_echodesk.conf`: its
`[from-provider]` / `[queue-with-announce]` contexts and queue names are
tenant-prefixed (`amanati_support`) and would clash with the generated
`[from-provider-amanati]` until the `pbx/AMANATI_BYO_MIGRATION.md` cleanup is done.

