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

## 6. Dialplan — generated by the installer

You do not write the dialplan by hand. The install command (step 7) writes
`/etc/asterisk/extensions_echodesk.conf` (header `; Managed by EchoDesk`, the previous
copy kept as `extensions_echodesk.conf.bak.<timestamp>`), appends
`#include "extensions_echodesk.conf"` to your `extensions.conf` exactly once and runs
`dialplan reload`. Your own `extensions.conf` is never rewritten, so keep whatever else
you need in it (custom IVR contexts for `ivr_custom` routes, for example).

What the generated file contains (`<tenant>` is the tenant's workspace id):

| Context | Purpose |
|---|---|
| `[globals]` | `ECHODESK_API` read from the systemd drop-in (`ECHODESK_API_URL`, via `ENV()` — the installer preloads `func_env.so`); `RECORDING_BASE` = the PBX server's *Recording base URL* in EchoDesk; `MAX_WAIT` queue timeout. The token is deliberately **not** a global (Asterisk logs global values on every reload): the hangup handler's `curl` reads `$ECHODESK_PBX_TOKEN` from the Asterisk process environment |
| `[tenant_<tenant>]` | Where every extension's calls start (EchoDesk sets `context=tenant_<tenant>` on the endpoints). 3–4 digit numbers go to `[internal]`; longer numbers or `+E.164` go to `[outbound]` |
| `[internal]` | `Dial(PJSIP/<ext>,30,b(echodesk-predial^s^1))` |
| `[echodesk-predial]` | Pre-dial handler on every agent leg: adds the SIP header `X-EchoDesk-Call: <LINKEDID>` so all browsers ringing for one call report the same call id |
| `[outbound]` | Records (`MixMonitor`), pushes the `echodesk-recording` hangup handler and dials `PJSIP/<number>@trunk_<name>` (60 s). With **no active trunk** in EchoDesk it plays `ss-noservice` instead — see below |
| `[from-provider-<tenant>]` | Where the provider trunk's calls land (`context=from-provider-<tenant>` on the trunk endpoint) → `[echodesk-inbound]` |
| `[echodesk-inbound]` | `Answer`, `AGI(echodesk-routing.py)`, then follows `ROUTE_ACTION`: queue (`Queue(…,tTb(echodesk-predial^s^1),,,MAX_WAIT)` with the greeting / hold music / thank-you prompts from EchoDesk), extension (`PJSIP/100&PJSIP/101`), voicemail (`VoiceMail(<DID>@default)` — create the mailbox in `voicemail.conf`), `ivr_custom` (`Goto(<IVR_CONTEXT>,s,1)`), hangup; after hours: announcement, voicemail or forward through the trunk |
| `[echodesk-recording]` | Hangup handler: `curl` POST to `${ECHODESK_API}/api/webhooks/call-recording-url/` with the `X-PBX-Token` header and `caller_number`, `recipient_number`, `recording_url`, `pbx_call_id` (LINKEDID), `sip_call_id` |
| `[confbridge-dynamic]` | `ConfBridge(${EXTEN})` — used by EchoDesk's attended-transfer merge |

The outbound trunk's endpoint name is baked into the file (the active trunk in EchoDesk,
the default one first, `trunk_` plus the name lower-cased with spaces as `_`; e.g. trunk
"Magti" → `trunk_magti`). If no trunk exists yet when you run the installer, outbound
calls play "no service" until you **re-pull the dialplan** after creating the trunk:

```bash
curl -fsSL https://<tenant>.api.echodesk.ge/api/pbx/install/<token>/dialplan/ \
  > /etc/asterisk/extensions_echodesk.conf && asterisk -rx 'dialplan reload'
```

`<token>` is the install token from the PBX Server page (the same one as in the install
command; it expires after 24 h — click **Regenerate install token** for a fresh one).
Re-pull the same way after renaming the trunk or switching the default one. Re-running
the whole install command also rewrites the file.

Make sure `extensions.conf` still has its `[general]` section (`static=yes`,
`writeprotect=yes` are fine); the generated file adds its own `[globals]`.

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
   - writes the dialplan `/etc/asterisk/extensions_echodesk.conf` (step 6), adds the
     `#include` to `extensions.conf` once and reloads the dialplan;
   - creates `astcachedir` (prompt cache) if it is missing;
   - creates the EchoDesk AMI user in `/etc/asterisk/manager.d/echodesk.conf`;
   - restarts Asterisk, waits up to 60 s for the database connection and reports back
     to EchoDesk.

   The last lines must read `realtime: Connected to asterisk_<tenant>@…` and
   `==> EchoDesk install complete.` If it prints a `WARNING` instead, fix that (usually
   step 7.1 or the firewall) and re-run the command.
4. The PBX Server page turns **Active** with a fresh *Last seen*.

The drop-in carries the PBX's long-lived **API token** (sent as `X-PBX-Token` by the
routing script and the recording hangup handler). **Regenerate install token** only
changes the 24 h install URL; regenerating the **API token** in EchoDesk invalidates the
one on the PBX — re-run the install command afterwards so the drop-in gets the new one.

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
   `trunk_<name>`; re-pull the dialplan so `[outbound]` dials it (step 6).
   - IP-authenticated providers work as soon as the trunk exists.
   - If the provider requires **registration**, add it to `pjsip.conf` yourself and
     `asterisk -rx 'pjsip reload'` (EchoDesk also writes a `ps_registrations` row for
     such trunks, but the install script keeps registrations on `pjsip.conf`, so that row
     is not used):
     ```ini
     [trunk-registration]
     type=registration
     transport=transport-udp
     outbound_auth=trunk_<name>
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
| Install command (or the dialplan re-pull) prints `Invalid or expired enrollment token` | Token older than 24 h — click **Regenerate install token** and run the new command |
| Outbound calls play "no service" (`ss-noservice`) | The dialplan was generated before the trunk existed — re-pull it (step 6) |
| Recording webhook answers `401` (`/var/log/asterisk/messages.log` from the hangup handler, or `curl -v` by hand) | `ECHODESK_PBX_TOKEN` in the drop-in is not the PBX's current API token — re-run the install command |
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
sed -i '/^#include "extensions_echodesk.conf"$/d' extensions.conf
rm -f extensions_echodesk.conf extensions_echodesk.conf.bak.*
rm -f /etc/systemd/system/asterisk.service.d/echodesk.conf /etc/asterisk/manager.d/echodesk.conf
systemctl daemon-reload && systemctl restart asterisk
```

Then delete the PBX server in EchoDesk.
