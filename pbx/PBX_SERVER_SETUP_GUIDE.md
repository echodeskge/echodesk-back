# Building an Asterisk PBX for EchoDesk

This guide is for the DevOps engineer who builds and runs the PBX for an EchoDesk
customer (a "tenant"). Follow it top to bottom. At the end:

- staff answer and place calls from the EchoDesk web app (browser softphone);
- extensions, queues, trunks and inbound routes are managed in the EchoDesk UI and
  take effect on your Asterisk within seconds (Asterisk reads them live from a
  database EchoDesk hosts for you);
- each inbound call asks EchoDesk where to go (working hours, queue, extension,
  voicemail…), and call recordings are playable from the call history.

You own the server (OS, Asterisk, certificates, firewall, backups). EchoDesk never
logs into it.

---

## 0. Values you will need

Fill these in before you start. Everywhere below, replace the `<…>` placeholders.

| Placeholder | What it is | Example |
|---|---|---|
| `<tenant>` | The tenant's EchoDesk workspace id — the first label of their EchoDesk address (`acme.echodesk.ge` → `acme`). Ask the tenant admin. | `acme` |
| `<fqdn>` | Public DNS name of the PBX (an `A` record to the server's public IP) | `pbx.acme.ge` |
| `<public_ip>` | The server's public IPv4 | `203.0.113.10` |
| `<private_net>` | The server's private network, if it is behind NAT (otherwise skip `local_net`) | `10.0.0.0/8` |
| `<provider_ips>` | SIP provider's signalling IPs (Magti, Silknet, …) | `89.150.1.11` |

EchoDesk addresses you will see below:

| Address | Used for |
|---|---|
| `https://<tenant>.api.echodesk.ge` | API the PBX calls (call routing, install, recordings) |
| `167.233.54.91` | EchoDesk servers: realtime database (port 5432) and the source of AMI / recording requests to your PBX |

---

## 1. Server

- Ubuntu **24.04 LTS** (22.04 works too), x86_64 or arm64, a public IPv4.
- Small office (≤ 30 concurrent calls with recording): 2 vCPU, 4 GB RAM, 40 GB SSD.
  Recordings take ~1 MB per minute (WAV) — size the disk for your retention.
- Set the timezone to the tenant's (working hours are evaluated by EchoDesk, but logs
  and recordings use the server clock): `timedatectl set-timezone Asia/Tbilisi`.
- Keep the clock in sync (`systemd-timesyncd` is on by default).

## 2. Firewall

Open only what is needed. With `ufw`:

| Port | Proto | From | Why |
|---|---|---|---|
| 22 | tcp | your admin IPs | SSH |
| 80 | tcp | anywhere | Let's Encrypt HTTP challenge (can be closed after issuing if you use DNS challenge) |
| 8089 | tcp | anywhere (or the tenant's office IPs) | Secure WebSocket — the EchoDesk browser softphone connects here |
| 5060 | udp | `<provider_ips>` only | SIP trunk signalling |
| 10000–20000 | udp | anywhere | RTP audio (browser softphones and the provider) |
| 5038 | tcp | `167.233.54.91` only | AMI — EchoDesk reads live call state and controls calls |
| 8443 | tcp | `167.233.54.91` only | Recordings — EchoDesk fetches recordings server-side |

Outbound the server needs: `tcp/5432` to `167.233.54.91` (realtime database),
`tcp/443` to `*.api.echodesk.ge`, and DNS/NTP/apt.

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow from <your_admin_ip> to any port 22 proto tcp
ufw allow 80/tcp
ufw allow 8089/tcp
ufw allow from <provider_ip> to any port 5060 proto udp     # repeat per provider IP
ufw allow 10000:20000/udp
ufw allow from 167.233.54.91 to any port 5038 proto tcp
ufw allow from 167.233.54.91 to any port 8443 proto tcp
ufw enable
```

Never expose 5060 or 5038 to the whole internet — SIP scanners will find them within hours.

## 3. Install Asterisk

EchoDesk needs **Asterisk 18 or newer** (20 LTS recommended) with these modules:
`res_config_pgsql` (realtime database), `res_pjsip*` (SIP + WebRTC), `res_http_websocket`,
`res_http_media_cache` + `res_curl` (prompts are played straight from EchoDesk URLs),
`app_queue`, `app_mixmonitor`, `res_srtp` (WebRTC media encryption), `codec_g722`.

### Option A — Ubuntu packages (simplest, recommended)

Ubuntu 24.04 ships Asterisk **20.6** in `universe` (22.04 ships 18.10); both packages
contain every module EchoDesk needs.

```bash
apt-get update
apt-get install -y asterisk asterisk-modules asterisk-core-sounds-en-wav \
  asterisk-moh-opsound-wav python3 python3-requests postgresql-client curl
asterisk -V    # Asterisk 20.6.0~dfsg+~cs6.13.40431414-2build5 on 24.04
ls /usr/lib/*/asterisk/modules/ | grep -E 'res_config_pgsql|res_http_media_cache|res_curl|res_srtp|codec_g722'
```

All five modules must be listed. If `res_config_pgsql` is missing, use Option B.

The package installs and starts the `asterisk` systemd service. Things that differ from
a source build, which the steps below and the install script take care of:

- the packaged `/etc/asterisk/modules.conf` contains `noload => res_config_pgsql.so`
  (the install script comments it out — `noload` wins over `preload`);
- `astagidir` is `/usr/share/asterisk/agi-bin` (the install script links the routing
  script there);
- `astcachedir` is `/var/cache/asterisk`, which the package does not create (5.8).

### Option B — build Asterisk 20 LTS from source

```bash
apt-get update && apt-get install -y build-essential wget libpq-dev libsrtp2-dev \
  libcurl4-openssl-dev python3 python3-requests postgresql-client curl
cd /usr/src
wget https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-20-current.tar.gz
tar xzf asterisk-20-current.tar.gz && cd asterisk-20.*/
contrib/scripts/install_prereq install
./configure --with-jansson-bundled --with-pjproject-bundled
make menuselect.makeopts
menuselect/menuselect --enable res_config_pgsql --enable res_http_media_cache \
  --enable res_curl --enable res_srtp --enable codec_g722 --enable app_mixmonitor \
  --enable CORE-SOUNDS-EN-WAV --enable MOH-OPSOUND-WAV menuselect.makeopts
make -j"$(nproc)" && make install && make samples && make config
useradd -r -d /var/lib/asterisk -s /usr/sbin/nologin asterisk || true
chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/log/asterisk /var/spool/asterisk /var/run/asterisk 2>/dev/null || true
sed -i 's/^#AST_USER=.*/AST_USER="asterisk"/; s/^#AST_GROUP=.*/AST_GROUP="asterisk"/' /etc/default/asterisk
systemctl enable --now asterisk
```

Make sure Asterisk runs as the `asterisk` user (`ps -o user= -C asterisk`).

## 4. TLS certificate

Browsers only connect to a softphone WebSocket with a valid certificate.

```bash
apt-get install -y certbot
certbot certonly --standalone -d <fqdn> --agree-tos -m <your_email> --non-interactive
mkdir -p /etc/asterisk/keys
cp /etc/letsencrypt/live/<fqdn>/fullchain.pem /etc/asterisk/keys/fullchain.pem
cp /etc/letsencrypt/live/<fqdn>/privkey.pem /etc/asterisk/keys/privkey.pem
chown -R asterisk:asterisk /etc/asterisk/keys && chmod 600 /etc/asterisk/keys/privkey.pem
```

Renewal hook (copies the new cert and reloads Asterisk + nginx):

```bash
cat > /etc/letsencrypt/renewal-hooks/deploy/asterisk.sh <<'EOF'
#!/bin/sh
cp /etc/letsencrypt/live/<fqdn>/fullchain.pem /etc/asterisk/keys/fullchain.pem
cp /etc/letsencrypt/live/<fqdn>/privkey.pem /etc/asterisk/keys/privkey.pem
chown asterisk:asterisk /etc/asterisk/keys/*.pem
asterisk -rx 'module reload http' ; systemctl reload nginx 2>/dev/null || true
EOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/asterisk.sh
```

## 5. Base Asterisk configuration

Back up the sample configs first: `cp -a /etc/asterisk /etc/asterisk.orig`.

### 5.1 `/etc/asterisk/http.conf` — WebSocket for the browser softphone

```ini
[general]
enabled=yes
bindaddr=127.0.0.1
bindport=8088
tlsenable=yes
tlsbindaddr=0.0.0.0:8089
tlscertfile=/etc/asterisk/keys/fullchain.pem
tlsprivatekey=/etc/asterisk/keys/privkey.pem
```

### 5.2 `/etc/asterisk/pjsip.conf` — transports

EchoDesk creates every endpoint (extensions and trunks) in the database. They
reference these two transports **by name**, so keep the names exactly.

```ini
[global]
type=global
user_agent=EchoDesk PBX
endpoint_identifier_order=ip,username,anonymous

[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
external_media_address=<public_ip>
external_signaling_address=<public_ip>
local_net=<private_net>          ; only if the server is behind NAT

[transport-wss]
type=transport
protocol=wss
bind=0.0.0.0
external_media_address=<public_ip>
external_signaling_address=<public_ip>
local_net=<private_net>          ; only if the server is behind NAT
```

Do not define extensions, queues or trunks here — EchoDesk manages them.

### 5.3 `/etc/asterisk/rtp.conf`

```ini
[general]
rtpstart=10000
rtpend=20000
icesupport=yes
stunaddr=stun.l.google.com:19302
```

### 5.4 `/etc/asterisk/manager.conf` — AMI

```ini
[general]
enabled=yes
port=5038
bindaddr=0.0.0.0

#include "manager.d/*.conf"
```

The EchoDesk AMI user itself is written by the install script (step 7) into
`/etc/asterisk/manager.d/echodesk.conf`.

### 5.5 `/etc/asterisk/modules.conf` — make sure these load

These lines must be **inside the `[modules]` section**. Do not append them to the end of
the file: the packaged `modules.conf` ends with a `[global]` section, and lines there are
silently ignored.

```ini
[modules]
autoload=yes
noload => chan_sip.so          ; PJSIP only
load => res_http_media_cache.so
load => res_curl.so
; optional, Ubuntu package only: silence duplicate voicemail modules and sample dialplans
noload => app_voicemail_imap.so
noload => app_voicemail_odbc.so
noload => pbx_lua.so
noload => pbx_ael.so
```

On the Ubuntu package, insert them right under the header:

```bash
sed -i '/^\[modules\]/a noload => chan_sip.so\nload => res_http_media_cache.so\nload => res_curl.so\nnoload => app_voicemail_imap.so\nnoload => app_voicemail_odbc.so\nnoload => pbx_lua.so\nnoload => pbx_ael.so' /etc/asterisk/modules.conf
```

(The install script adds `preload => res_config_pgsql.so` here and comments out the
packaged `noload => res_config_pgsql.so`.)

### 5.6 `/etc/asterisk/queues.conf`

Queues come from the database; keep only the general section:

```ini
[general]
persistentmembers=no
shared_lastcall=yes
```

### 5.7 `/etc/asterisk/musiconhold.conf` — hold music class for queues

EchoDesk queues use the music-on-hold class `queue-hold` unless the tenant sets another
one. Add it (keep the existing `[default]` class):

```ini
[queue-hold]
mode=files
directory=moh
```

Without it callers in a queue hear silence and the log shows
`Music on Hold class 'queue-hold' not found`.

### 5.8 Folders

```bash
# call recordings
mkdir -p /var/spool/asterisk/recording
chown asterisk:asterisk /var/spool/asterisk/recording
# cache for prompts played from EchoDesk URLs (res_http_media_cache downloads here;
# the Ubuntu package does not create it, and without it every prompt fails with
# "Failed to create temporary storage")
mkdir -p /var/cache/asterisk
chown asterisk:asterisk /var/cache/asterisk
```

Then apply everything: `systemctl restart asterisk`.

## 6. Dialplan — `/etc/asterisk/extensions.conf`

EchoDesk puts every extension in context `tenant_<tenant>` and every provider trunk
in context `from-provider-<tenant>`. Replace `<tenant>`, `<fqdn>` and `<trunk>` (see
the note below), keep everything else as is.

```ini
[general]
static=yes
writeprotect=yes

[globals]
ECHODESK_API=https://<tenant>.api.echodesk.ge
RECORDING_BASE=https://<fqdn>:8443/recordings

; ---------------------------------------------------------------------------
; Calls placed by staff (browser softphone)
; ---------------------------------------------------------------------------
[tenant_<tenant>]
; extension to extension (EchoDesk extensions are 3–4 digits, e.g. 100)
exten => _[1-9]XX,1,Goto(internal,${EXTEN},1)
exten => _[1-9]XXX,1,Goto(internal,${EXTEN},1)
; everything longer goes out through the provider trunk
exten => _XXXXX.,1,Goto(outbound,${EXTEN},1)
exten => _+X.,1,Goto(outbound,${EXTEN},1)

[internal]
exten => _X.,1,Dial(PJSIP/${EXTEN},30)
 same => n,Hangup()

[outbound]
exten => _X.,1,Set(REC_FILE=${UNIQUEID}.wav)
 same => n,MixMonitor(/var/spool/asterisk/recording/${REC_FILE},b)
 same => n,Set(CHANNEL(hangup_handler_push)=echodesk-recording,s,1(${EXTEN}))
 same => n,Dial(PJSIP/${EXTEN}@<trunk>,60)
 same => n,Hangup()
exten => _+X.,1,Goto(outbound,${EXTEN:1},1)

; ---------------------------------------------------------------------------
; Calls from the provider — EchoDesk decides where they go
; ---------------------------------------------------------------------------
[from-provider-<tenant>]
exten => _X.,1,Goto(echodesk-inbound,${EXTEN},1)
exten => _+X.,1,Goto(echodesk-inbound,${EXTEN},1)
exten => s,1,Goto(echodesk-inbound,s,1)

[echodesk-inbound]
exten => _[+s0-9]!,1,Answer()
 same => n,Wait(0.5)
 same => n,AGI(echodesk-routing.py)
 same => n,Set(REC_FILE=${UNIQUEID}.wav)
 same => n,MixMonitor(/var/spool/asterisk/recording/${REC_FILE},b)
 same => n,Set(CHANNEL(hangup_handler_push)=echodesk-recording,s,1(${CALLERID(num)}))
 same => n,GotoIf($["${IS_WORKING}" = "false"]?after_hours)
 same => n,ExecIf($["${SOUND_GREETING}" != ""]?Playback(${SOUND_GREETING}))
 same => n,GotoIf($["${ROUTE_ACTION}" = "extension"]?to_ext)
 same => n,GotoIf($["${ROUTE_ACTION}" = "voicemail"]?vm)
 same => n,GotoIf($["${ROUTE_ACTION}" = "hangup"]?bye)
 same => n,GotoIf($["${ROUTE_ACTION}" = "ivr_custom" & "${IVR_CONTEXT}" != ""]?ivr)
 ; default: queue (falls back to "support" if EchoDesk is unreachable)
 same => n,Set(QUEUE_TO_DIAL=${IF($["${QUEUE_NAME}" != ""]?${QUEUE_NAME}:support)})
 same => n,Queue(${QUEUE_TO_DIAL},tT,,,60)
 same => n,ExecIf($["${SOUND_THANK_YOU}" != ""]?Playback(${SOUND_THANK_YOU}))
 same => n,Hangup()
 same => n(to_ext),Dial(PJSIP/${STRREPLACE(DEST_EXTENSIONS,\,,&PJSIP/)},30)
 same => n,Goto(vm)
 same => n(ivr),Goto(${IVR_CONTEXT},s,1)
 same => n(after_hours),ExecIf($["${SOUND_AFTER_HOURS}" != ""]?Playback(${SOUND_AFTER_HOURS}))
 same => n,GotoIf($["${AFTER_HOURS_ACTION}" = "voicemail"]?vm)
 same => n,GotoIf($["${AFTER_HOURS_ACTION}" = "forward" & "${FORWARD_NUMBER}" != ""]?fwd)
 same => n(bye),Hangup()
 same => n(vm),ExecIf($["${SOUND_VOICEMAIL_PROMPT}" != ""]?Playback(${SOUND_VOICEMAIL_PROMPT}))
 same => n,Record(/var/spool/asterisk/recording/vm-${UNIQUEID}.wav,3,120,k)
 same => n,Hangup()
 same => n(fwd),Dial(PJSIP/${FORWARD_NUMBER}@<trunk>,30)
 same => n,Hangup()

; ---------------------------------------------------------------------------
; After every recorded call: tell EchoDesk where the recording is
; ---------------------------------------------------------------------------
[echodesk-recording]
exten => s,1,NoOp(Recording ${REC_FILE} for ${ARG1})
 same => n,System(curl -s -m 10 -X POST -H 'Content-Type: application/json' -d '{"caller_number":"${ARG1}","recording_url":"${RECORDING_BASE}/${REC_FILE}"}' ${ECHODESK_API}/api/webhooks/call-recording-url/ >/dev/null 2>&1 &)
 same => n,Return()
```

Two details in `[echodesk-inbound]` are deliberate: the pattern `_[+s0-9]!` (not `.`)
also matches the bare `s` extension that `from-provider-<tenant>` sends when the provider
gives no number, and the extension route turns EchoDesk's `100,101` into
`PJSIP/100&PJSIP/101` (`STRREPLACE`; `REPLACE` only swaps single characters).

`<trunk>` is the endpoint name EchoDesk gives the provider trunk: `trunk_` plus the
trunk name lower-cased with spaces as `_` (e.g. trunk "Magti" → `trunk_magti`). After
the tenant creates the trunk (step 9) check the exact name with
`asterisk -rx 'pjsip show endpoints'` and put it in the dialplan.

Load it: `asterisk -rx 'dialplan reload'`.

## 7. Connect the server to EchoDesk

1. Send the server's **public IP** to EchoDesk support (support@echodesk.ge) and wait
   for confirmation that it is allowed on the realtime database firewall
   (`167.233.54.91:5432`). The connection is TLS (`sslmode=require`).
2. The tenant admin opens **Settings → PBX → PBX Server** in EchoDesk
   (`https://<tenant>.echodesk.ge/settings/pbx/server/`), clicks **Connect your PBX**
   and enters Name, FQDN = `<fqdn>`, Public IP = `<public_ip>`, AMI port `5038`.
   EchoDesk creates the tenant's realtime database and shows a one-line install command.
3. On the PBX, as root, run that command within **24 hours** (the token expires):

   ```bash
   curl -sSL https://<tenant>.api.echodesk.ge/api/pbx/install/<token>/ | sudo bash
   ```

   The script (safe to re-run; it backs up every file it changes as `*.bak.<timestamp>`):
   - writes `/etc/asterisk/res_pgsql.conf` (database credentials);
   - maps the realtime tables in `extconfig.conf` (`[settings]`) and points PJSIP
     endpoints, auths, AORs and identifies at them in `sorcery.conf`; queues and queue
     members are read through `extconfig.conf`. Contacts stay in Asterisk's local
     database and outbound registrations stay in `pjsip.conf` (see step 9);
   - adds `preload => res_config_pgsql.so` to `modules.conf` and comments out the
     packaged `noload => res_config_pgsql.so`;
   - adds a systemd drop-in (`/etc/systemd/system/asterisk.service.d/echodesk.conf`)
     with `PGSSLMODE=require`, `ECHODESK_API_URL` and `ECHODESK_PBX_TOKEN` (the routing
     script uses them);
   - installs the routing script `/var/lib/asterisk/agi-bin/echodesk-routing.py` (and
     links it into `astagidir` when that is elsewhere, e.g. `/usr/share/asterisk/agi-bin`
     on the Ubuntu package);
   - creates `astcachedir` (prompt cache) if it is missing;
   - creates the EchoDesk AMI user in `/etc/asterisk/manager.d/echodesk.conf`;
   - restarts Asterisk, waits up to 60 s for the database connection and reports back
     to EchoDesk.

   The last lines must read `realtime: Connected to asterisk_<tenant>@…` and
   `==> EchoDesk install complete.` If it prints a `WARNING` instead, fix that (usually
   step 7.1 or the firewall) and re-run the command.
4. The PBX Server page turns **Active** with a fresh *Last seen*.

If the tenant later clicks **Regenerate install token**, the old token stops working
for call routing too — re-run the new install command on the PBX.

## 8. Recordings web server (port 8443)

EchoDesk fetches recordings from `https://<fqdn>:8443/recordings/<file>.wav`
(server-side, only from `167.233.54.91`).

```bash
apt-get install -y nginx
cat > /etc/nginx/sites-available/echodesk-recordings <<'EOF'
server {
    listen 8443 ssl;
    server_name <fqdn>;
    ssl_certificate     /etc/letsencrypt/live/<fqdn>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<fqdn>/privkey.pem;

    allow 167.233.54.91;
    deny all;

    location /recordings/ {
        alias /var/spool/asterisk/recording/;
        types { audio/wav wav; }
        autoindex off;
    }
}
EOF
ln -sf /etc/nginx/sites-available/echodesk-recordings /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
usermod -aG asterisk www-data && chmod 750 /var/spool/asterisk/recording
```

Retention (e.g. keep 180 days):

```bash
echo '0 3 * * * root find /var/spool/asterisk/recording -name "*.wav" -mtime +180 -delete' > /etc/cron.d/echodesk-recordings
```

## 9. Configure telephony in EchoDesk and test

The tenant admin (or you, with an admin account) does this in the EchoDesk UI:

1. **Settings → PBX → Trunks**: add the provider trunk (server, port, username,
   password, DIDs). Then on the PBX: `asterisk -rx 'pjsip show endpoints'` shows
   `trunk_<name>` — put that name into the dialplan (`<trunk>`) and `dialplan reload`.
   - IP-authenticated providers work as soon as the trunk exists.
   - If the provider requires **registration**, add it to `pjsip.conf` yourself and
     `asterisk -rx 'pjsip reload'` (EchoDesk also writes a `ps_registrations` row for
     such trunks, but the install script keeps registrations on `pjsip.conf`, so that row
     is not used):
     ```ini
     [trunk-registration]
     type=registration
     transport=transport-udp
     outbound_auth=<trunk>
     server_uri=sip:<provider_host>:5060
     client_uri=sip:<username>@<provider_host>
     retry_interval=60
     ```
2. **Settings → PBX → Extensions**: assign an extension to each user.
   `asterisk -rx 'pjsip show endpoint 100'` shows it within seconds.
3. **Queues**: create a queue (members come from an EchoDesk group).
   `asterisk -rx 'queue show'` lists it with the members.
4. **Inbound routes**: route each DID to a queue / extension / voicemail and set
   working hours and prompts.

### Acceptance test

| Test | Expected |
|---|---|
| `asterisk -rx 'realtime show pgsql status'` | `Connected to asterisk_<tenant>@167.233.54.91…` |
| `asterisk -rx 'pjsip show transports'` | `transport-udp` and `transport-wss` |
| `asterisk -rx 'http show status'` | `HTTPS Server Enabled and Bound to 0.0.0.0:8089` |
| `asterisk -rx 'pjsip show endpoints'` | the extensions and `trunk_<name>` from EchoDesk |
| `asterisk -rx 'queue show <queue>'` | the queue with its members, marked `(realtime)` |
| `openssl s_client -connect <fqdn>:8089 </dev/null` | valid Let's Encrypt chain, no errors |
| Staff open EchoDesk → phone icon | softphone shows *Registered*; `pjsip show contacts` lists the extension |
| Extension 100 calls 101 | rings in the other browser, two-way audio |
| Staff call a mobile number | goes out through the trunk, two-way audio |
| Call a DID from a mobile | greeting plays, call reaches the queue, staff can answer; `/var/log/asterisk/echodesk-routing.log` shows `tenant=<tenant>` |
| Call outside working hours | after-hours prompt / voicemail as configured |
| After the call | the call appears in EchoDesk call history and the recording plays (admins) |

## 10. Operations

- **Logs**: `/var/log/asterisk/messages.log`, `journalctl -u asterisk`,
  `/var/log/asterisk/echodesk-routing.log` (routing decisions).
- **Asterisk CLI**: `asterisk -rvvv`.
- **Updates**: `apt-get upgrade` (Option A) or rebuild the same 20.x branch (Option B).
  After an Asterisk upgrade re-run the install command if any `/etc/asterisk` file was
  replaced.
- **Backups**: `/etc/asterisk`, `/etc/letsencrypt`, `/var/spool/asterisk/recording`.
  Extensions/queues/trunks live in EchoDesk and need no backup on the PBX.
- **Security**: keep 5060 restricted to provider IPs and 5038/8443 to EchoDesk; install
  `fail2ban` with the `asterisk` jail; never reuse SIP passwords across tenants.

## 11. Troubleshooting

| Symptom | Check |
|---|---|
| Install command prints `Invalid or expired enrollment token` | Token older than 24 h — click **Regenerate install token** and run the new command |
| PBX Server stays *Awaiting install* | Outbound HTTPS to `<tenant>.api.echodesk.ge`; re-run the command and read its output |
| `realtime show pgsql status` → not connected | Your public IP must be allowed by EchoDesk (step 7.1); outbound `tcp/5432` to `167.233.54.91`; `PGSSLMODE=require` in `systemctl show asterisk -p Environment` |
| `realtime show pgsql status` → `No such command` | `res_config_pgsql` is not loaded: `modules.conf` must have `preload => res_config_pgsql.so` and no active `noload => res_config_pgsql.so` — re-run the install command |
| `pjsip show endpoints` → `No such command`; log shows `Wizard 'realtime' failed to open mapping` / `res_pjsip declined to load` | PJSIP could not reach the realtime database at startup — fix the database connection, then `systemctl restart asterisk` |
| Database connection fails only when Asterisk is started by hand (`Failed to connect database … :` with no reason) | Start it with `systemctl`, not from a root shell: with `HOME=/root` the PostgreSQL client cannot read `/root/.postgresql` after Asterisk drops to the `asterisk` user |
| Asterisk takes minutes to start | Same as above — it waits for the database (systemd allows 300 s) |
| Softphone never registers | Certificate chain on 8089; `transport-wss` exists (`pjsip show transports`); port 8089 open |
| One-way or no audio | RTP 10000–20000/udp open; `external_media_address` / `local_net` correct for NAT |
| Inbound calls always go to queue `support` with no greeting | Routing script did not run or could not reach EchoDesk: `Failed to execute '…/echodesk-routing.py': File does not exist` in the log means the script is not in `astagidir` (re-run the install command); otherwise check `/var/log/asterisk/echodesk-routing.log`, `ECHODESK_PBX_TOKEN` in the systemd drop-in, and that the DID is on a trunk/inbound route in EchoDesk |
| Greeting / prompts silent, log shows `Failed to create temporary storage for 'https://…'` | `astcachedir` missing or not writable by `asterisk` (step 5.8) |
| Queue callers hear silence, log shows `Music on Hold class 'queue-hold' not found` | Add the `[queue-hold]` class (step 5.7) and `asterisk -rx 'moh reload'` |
| Recording does not play in EchoDesk | 8443 reachable from `167.233.54.91`; file exists in `/var/spool/asterisk/recording`; nginx `allow` line |
| EchoDesk shows no live call info / transfers fail | AMI: `manager show users` lists the EchoDesk user; port 5038 open from `167.233.54.91` |

On the Ubuntu package some errors at startup are harmless sample-config noise and can be
ignored: `geolocation.conf … cannot be parsed`, `res_config_ldap`, `pbx_dundi`,
`chan_unistim`, `cdr_*`/`cel_*` (`tds`, `radius`, `sqlite3_custom`, `pgsql`) declined,
`res_hep_*` declined and `No configured users for ARI`. Errors from `res_pjsip`,
`res_config_pgsql`, `sorcery`, `pbx_config`, `res_agi` or `res_http_media_cache` are real.

### Disconnecting

Restore the **oldest** backups the install script made (every re-run backs up the
already-modified files, so the newest backup still contains the EchoDesk settings) and
remove the drop-in:

```bash
cd /etc/asterisk
for f in res_pgsql.conf sorcery.conf extconfig.conf modules.conf; do
  b=$(ls -tr $f.bak.* 2>/dev/null | head -1); [ -n "$b" ] && cp "$b" $f
done
rm -f /etc/systemd/system/asterisk.service.d/echodesk.conf /etc/asterisk/manager.d/echodesk.conf
systemctl daemon-reload && systemctl restart asterisk
```

Then delete the PBX server in EchoDesk.
