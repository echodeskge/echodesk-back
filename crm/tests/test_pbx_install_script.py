"""The BYO install script a tenant pipes into ``sudo bash``."""
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from crm import views_pbx_install
from crm.pbx_provisioning import _admin_dsn
from crm.tests.conftest import CrmTestCase


def _pbx(**overrides):
    values = dict(
        id=7, name='Main PBX', fqdn='pbx.acme.test',
        realtime_db_host='pgbouncer', realtime_db_port=5432,
        realtime_db_name='asterisk_acme', realtime_db_user='asterisk_rw_acme',
        realtime_db_password="p'w$d", ami_username='echodesk', ami_password='s3cret',
        enrollment_token='tok123', api_token='apitok456',
        recording_base_url='https://pbx.acme.test:8443/recordings/', use_tenant_prefix=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _request():
    return RequestFactory().get('/api/pbx/install/tok123/', HTTP_HOST='api.echodesk.ge')


def _render(pbx, schema='acme', trunk_endpoint='trunk_magti'):
    return views_pbx_install._render_script(pbx, _request(), schema, trunk_endpoint)


def _dialplan(pbx=None, schema='acme', trunk_endpoint='trunk_magti', api_base='https://api.echodesk.ge'):
    return views_pbx_install._render_dialplan(pbx or _pbx(), schema, trunk_endpoint, api_base)


def _step(script, start, end):
    """The installer lines between two ``# --- N.`` step headers."""
    return re.search(rf"\n(# --- {re.escape(start)}\..*?)\n# --- {re.escape(end)}\.", script, re.S).group(1)


class TestInstallScript(SimpleTestCase):

    @override_settings(ASTERISK_PUBLIC_DB_HOST='203.0.113.5', ASTERISK_PUBLIC_DB_PORT='5432')
    def test_internal_db_host_is_replaced_with_the_public_one(self):
        script = _render(_pbx())
        self.assertIn('dbhost=203.0.113.5\n', script)
        self.assertNotIn('dbhost=pgbouncer', script)

    @override_settings(ASTERISK_PUBLIC_DB_HOST='203.0.113.5')
    def test_external_db_host_is_kept(self):
        script = _render(_pbx(realtime_db_host='db.example.com', realtime_db_port=25060))
        self.assertIn('dbhost=db.example.com\ndbport=25060\n', script)

    def test_agi_and_token_are_installed(self):
        script = _render(_pbx())
        self.assertIn('/var/lib/asterisk/agi-bin/echodesk-routing.py', script)
        self.assertIn("headers['X-PBX-Token'] = pbx_token", script)
        # The drop-in carries the long-lived api_token, never the 24h enrollment token.
        self.assertIn('Environment="ECHODESK_PBX_TOKEN=apitok456"', script)
        self.assertNotIn('ECHODESK_PBX_TOKEN=tok123', script)
        self.assertIn('Environment="ECHODESK_API_URL=https://api.echodesk.ge"', script)
        self.assertIn('manager.d/*.conf', script)

    @override_settings(PBX_AMI_PERMIT='167.233.54.91, 10.0.0.0/8')
    def test_ami_permit_is_restricted(self):
        script = _render(_pbx())
        self.assertIn('deny = 0.0.0.0/0.0.0.0\npermit = 167.233.54.91/255.255.255.255\npermit = 10.0.0.0/8', script)

    def test_callbacks_use_https_and_follow_redirects(self):
        script = _render(_pbx())
        self.assertIn("curl -fsSL -X POST", script)
        self.assertIn("https://api.echodesk.ge/api/pbx/install/tok123/ping/", script)

    def test_ubuntu_package_layout_is_handled(self):
        script = _render(_pbx())
        # Ubuntu's modules.conf ships "noload => res_config_pgsql.so"; noload beats preload.
        self.assertIn("noload[[:space:]]*=>[[:space:]]*res_config_pgsql\\.so/;&", script)
        # Ubuntu's astagidir is /usr/share/asterisk/agi-bin and astcachedir isn't created.
        self.assertIn('ln -sf /var/lib/asterisk/agi-bin/echodesk-routing.py "$AGI_DIR/echodesk-routing.py"', script)
        self.assertIn('mkdir -p "$CACHE_DIR"', script)

    def test_extconfig_mapping_lands_in_settings_and_reruns_cleanly(self):
        injector = re.search(r"python3 - <<'PYEOF'\n(.*?)\nPYEOF", _render(_pbx()), re.S).group(1)
        self.assertIn('/etc/asterisk/extconfig.conf', injector)
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / 'extconfig.conf'
            # Like the packaged file: '[' inside the comments of [settings].
            conf.write_text('[settings]\n; file.conf => driver,database[,table[,priority]]\n'
                            ';iaxusers => odbc,asterisk\n\n[other]\nfoo=bar\n')
            code = injector.replace('/etc/asterisk/extconfig.conf', str(conf))
            for _ in range(2):
                subprocess.run([sys.executable, '-c', code], check=True)
            txt = conf.read_text()
        self.assertEqual(txt.count('ps_endpoints => pgsql,general,ps_endpoints'), 1)
        self.assertLess(txt.index('ps_endpoints =>'), txt.index('[other]'))
        self.assertTrue(txt.startswith('[settings]\n; === EchoDesk realtime (managed) ===\n'))

    def test_script_is_valid_bash(self):
        result = subprocess.run(['bash', '-n'], input=_render(_pbx()), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


DIALPLAN_CONTEXTS = (
    '[globals]', '[tenant_acme]', '[internal]', '[echodesk-predial]', '[outbound]',
    '[from-provider-acme]', '[echodesk-inbound]', '[echodesk-recording]', '[confbridge-dynamic]',
)


class TestInstallerDialplan(SimpleTestCase):
    """Step 8c: the installer writes the EchoDesk-managed dialplan."""

    def test_dialplan_file_is_written_with_header_and_backup(self):
        step = _step(_render(_pbx()), '8c', '9')
        self.assertIn("cat > /etc/asterisk/extensions_echodesk.conf <<'DIALPLANEOF'", step)
        self.assertIn('; Managed by EchoDesk', step)
        self.assertIn('cp /etc/asterisk/extensions_echodesk.conf /etc/asterisk/extensions_echodesk.conf.bak.$TS', step)
        self.assertIn('#include "extensions_echodesk.conf"', step)
        self.assertIn("asterisk -rx 'dialplan reload'", step)
        # extensions.conf itself is only ever appended to, never rewritten.
        self.assertNotIn('cat > /etc/asterisk/extensions.conf', step)
        self.assertIn('extensions.conf', _step(_render(_pbx()), '3', '4'))

    def test_include_and_rewrite_are_idempotent(self):
        step = _step(_render(_pbx()), '8c', '9')
        with tempfile.TemporaryDirectory() as tmp:
            etc = Path(tmp) / 'etc'
            etc.mkdir()
            (etc / 'extensions.conf').write_text('[general]\nstatic=yes\n')
            stubs = Path(tmp) / 'bin'
            stubs.mkdir()
            for name in ('asterisk', 'chown'):
                (stubs / name).write_text('#!/bin/sh\nexit 0\n')
                (stubs / name).chmod(0o755)
            code = step.replace('/etc/asterisk', str(etc)).replace(
                '/var/spool/asterisk/recording', str(Path(tmp) / 'rec'))
            for ts in ('1', '2'):
                subprocess.run(['bash', '-euo', 'pipefail', '-c', f'TS={ts}\n{code}'],
                               check=True, env={'PATH': f"{stubs}:/usr/bin:/bin"})
            main = (etc / 'extensions.conf').read_text()
            managed = (etc / 'extensions_echodesk.conf').read_text()
            backups = sorted(p.name for p in etc.glob('extensions_echodesk.conf.bak.*'))
        self.assertEqual(main.count('#include "extensions_echodesk.conf"'), 1)
        self.assertTrue(main.startswith('[general]\nstatic=yes\n'))
        self.assertTrue(managed.startswith('; Managed by EchoDesk'))
        self.assertEqual(managed.count('[echodesk-inbound]'), 1)
        # First run had nothing to back up; the second kept the previous copy.
        self.assertEqual(backups, ['extensions_echodesk.conf.bak.2'])

    def test_all_contexts_present(self):
        text = _dialplan()
        for ctx in DIALPLAN_CONTEXTS:
            self.assertIn(f'\n{ctx}\n', text, ctx)
        self.assertIn('ECHODESK_API=${ENV(ECHODESK_API_URL)}', text)
        self.assertIn('RECORDING_BASE=https://pbx.acme.test:8443/recordings/', text)
        self.assertIn('MAX_WAIT=60\n', text)
        # 3-4 digits stay internal, longer or + goes out ([outbound] strips the +).
        self.assertIn('exten => _[1-9]XX,1,Goto(internal,${EXTEN},1)', text)
        self.assertIn('exten => _[1-9]XXX,1,Goto(internal,${EXTEN},1)', text)
        self.assertIn('exten => _XXXXX.,1,Goto(outbound,${EXTEN},1)', text)
        self.assertIn('exten => _+X.,1,Goto(outbound,${EXTEN},1)', text)
        self.assertIn('exten => _+X.,1,Goto(outbound,${EXTEN:1},1)', text)
        self.assertIn('Dial(PJSIP/${EXTEN},30,b(echodesk-predial^s^1))', text)
        self.assertIn('Set(PJSIP_HEADER(add,X-EchoDesk-Call)=${CHANNEL(linkedid)})', text)
        self.assertIn('Set(REC_FILE=${UNIQUEID}.wav)', text)
        self.assertIn('MixMonitor(/var/spool/asterisk/recording/${REC_FILE},b)', text)
        self.assertIn('Set(CHANNEL(hangup_handler_push)=echodesk-recording,s,1)', text)
        # The SIP Call-ID is captured before the recorded leg dials, PJSIP channels only.
        self.assertEqual(text.count('GotoIf($["${CHANNEL(channeltype)}" != "PJSIP"]?rec)'), 2)
        self.assertEqual(text.count('Set(SIPCALLID=${FILTER(a-zA-Z0-9@._-,${CHANNEL(pjsip,call-id)})})'), 3)
        self.assertIn('Goto(echodesk-inbound,${EXTEN},1)', text)
        self.assertIn('AGI(echodesk-routing.py)', text)
        self.assertIn('Queue(${QUEUE_TO_DIAL},tTb(echodesk-predial^s^1),,,${MAX_WAIT})', text)
        self.assertIn('Dial(PJSIP/${STRREPLACE(DEST_EXTENSIONS,\\,,&PJSIP/)},30,b(echodesk-predial^s^1))', text)
        self.assertIn('VoiceMail(${EXTEN}@default,su)', text)
        self.assertIn('Goto(${IVR_CONTEXT},s,1)', text)
        for var in ('IS_WORKING', 'ROUTE_ACTION', 'AFTER_HOURS_ACTION', 'FORWARD_NUMBER',
                    'SOUND_GREETING', 'SOUND_AFTER_HOURS', 'SOUND_QUEUE_HOLD',
                    'SOUND_VOICEMAIL_PROMPT', 'SOUND_THANK_YOU', 'IVR_CONTEXT', 'QUEUE_NAME'):
            self.assertIn('${' + var + '}', text, var)
        self.assertIn('${STRREPLACE(DEST_EXTENSIONS,', text)
        self.assertIn('exten => _conf_.,1,ConfBridge(${EXTEN})', text)
        self.assertNotIn('@@', text)

    def test_recording_curl_carries_the_pbx_token_and_call_ids(self):
        text = _dialplan()
        handler = text.split('[echodesk-recording]\n', 1)[1].split('\n\n', 1)[0]
        curl = next(l for l in handler.splitlines() if 'SHELL(curl' in l)
        # The token is read from the asterisk process environment by the shell,
        # never from a dialplan variable (Asterisk logs [globals] values on load).
        self.assertIn('-H "X-PBX-Token: $ECHODESK_PBX_TOKEN"', curl)
        self.assertIn('-m 10', curl)
        self.assertIn("-H 'Content-Type: application/json'", curl)
        self.assertIn('${ECHODESK_API}/api/webhooks/call-recording-url/', curl)
        for field in ('"caller_number":"${FILTER(+0-9,${CALLERID(num)})}"',
                      '"recipient_number":"${FILTER(+0-9,${DIALED_NUMBER})}"',
                      '"recording_url":"${RECORDING_BASE}${REC_FILE}"',
                      '"pbx_call_id":"${CHANNEL(linkedid)}"', '"sip_call_id":"${SIPCALLID}"'):
            self.assertIn(field, curl)
        # Only the HTTP status is logged (the smoke greps this line), never the token.
        self.assertIn('NoOp(EchoDesk recording webhook -> HTTP ${WEBHOOK_HTTP} for ${RECORDING_BASE}${REC_FILE})', handler)
        self.assertNotIn('ECHODESK_TOKEN', handler)
        # Legs that did not capture the Call-ID (the harness test numbers) get it here, PJSIP only.
        self.assertIn('GotoIf($["${SIPCALLID}" != "" | "${CHANNEL(channeltype)}" != "PJSIP"]?post)', handler)
        # Nothing in the dialplan starts an inline comment by accident.
        for line in text.splitlines():
            if line.startswith(('exten', ' same')):
                self.assertNotIn(';', line, line)
        # No secret is ever baked into the dialplan text.
        self.assertNotIn('apitok456', _dialplan())
        self.assertNotIn('tok123', _dialplan())

    def test_token_is_never_a_dialplan_variable(self):
        for trunk in ('trunk_magti', ''):
            text = _dialplan(trunk_endpoint=trunk)
            self.assertNotIn('ENV(ECHODESK_PBX_TOKEN)', text)
            self.assertNotIn('ECHODESK_TOKEN', text)
            self.assertNotIn('apitok456', text)
            self.assertNotIn('tok123', text)
            # The only mention is the shell reading its own environment inside SHELL().
            mentions = [l for l in text.splitlines() if 'ECHODESK_PBX_TOKEN' in l and not l.startswith(';')]
            self.assertEqual(len(mentions), 1)
            self.assertIn('${SHELL(curl', mentions[0])
            self.assertIn('$ECHODESK_PBX_TOKEN', mentions[0])
        # ENV() in [globals] needs func_env preloaded before pbx_config parses it.
        self.assertIn('preload => func_env.so', _step(_render(_pbx()), '7', '8'))

    def test_every_agent_leg_runs_the_predial_handler(self):
        text = _dialplan()
        agent_dials = [l for l in text.splitlines()
                       if ('Dial(PJSIP/' in l or 'Queue(' in l) and '@trunk_' not in l]
        self.assertEqual(len(agent_dials), 3, agent_dials)   # internal, queue, to_ext
        for line in agent_dials:
            self.assertIn('b(echodesk-predial^s^1)', line, line)
        # Queue's 8th (gosub) argument runs only after answer, so it is never used.
        self.assertNotIn(',,,echodesk-predial', text)

    def test_recording_base_gets_a_trailing_slash(self):
        text = _dialplan(_pbx(recording_base_url='https://pbx.acme.test:8443/recordings'))
        self.assertIn('RECORDING_BASE=https://pbx.acme.test:8443/recordings/\n', text)

    def test_trunk_endpoint_name_comes_from_the_sync_naming(self):
        from crm import asterisk_sync

        trunk = SimpleNamespace(id=3, name='Magti Main')
        expected = 'trunk_' + asterisk_sync._slugify_trunk(trunk)
        self.assertEqual(expected, 'trunk_magti_main')
        chain = mock.MagicMock()
        chain.filter.return_value.order_by.return_value.first.return_value = trunk
        with mock.patch('crm.models.Trunk.objects', chain):
            self.assertEqual(views_pbx_install._active_trunk_endpoint(_pbx(), 'acme'), expected)
            self.assertEqual(
                views_pbx_install._active_trunk_endpoint(_pbx(use_tenant_prefix=True), 'acme'),
                'acme_trunk_magti_main',
            )
            chain.filter.return_value.order_by.return_value.first.return_value = None
            self.assertEqual(views_pbx_install._active_trunk_endpoint(_pbx(), 'acme'), '')
        chain.filter.assert_called_with(is_active=True)
        chain.filter.return_value.order_by.assert_called_with('-is_default', 'id')

        text = _dialplan(trunk_endpoint=expected)
        self.assertIn('Dial(PJSIP/${EXTEN}@trunk_magti_main,60)', text)
        self.assertIn('Dial(PJSIP/${FORWARD_NUMBER}@trunk_magti_main,30)', text)
        self.assertIn('; Outbound trunk: trunk_magti_main', text)

    def test_legacy_prefixed_server_dials_prefixed_extensions(self):
        text = _dialplan(_pbx(use_tenant_prefix=True), trunk_endpoint='acme_trunk_magti')
        self.assertIn('Dial(PJSIP/acme_${EXTEN},30,b(echodesk-predial^s^1))', text)
        self.assertIn('Dial(PJSIP/acme_${STRREPLACE(DEST_EXTENSIONS,\\,,&PJSIP/acme_)}', text)

    def test_no_trunk_variant_plays_no_service(self):
        text = _dialplan(trunk_endpoint='')
        outbound = text.split('[outbound]\n', 1)[1].split('\n\n', 1)[0]
        self.assertIn('Playback(ss-noservice)', outbound)
        self.assertIn('Hangup()', outbound)
        self.assertIn('re-pull', outbound)
        self.assertNotIn('Dial(', outbound)
        self.assertNotIn('@,', text)
        self.assertIn('; Outbound trunk: (none', text)
        for ctx in DIALPLAN_CONTEXTS:
            self.assertIn(f'\n{ctx}\n', text, ctx)
        # The after-hours forward branch still resolves its label.
        self.assertIn('same => n(fwd),NoOp(EchoDesk: no provider trunk configured', text)

    def test_script_with_dialplan_is_valid_bash_in_both_variants(self):
        for trunk in ('trunk_magti', ''):
            result = subprocess.run(['bash', '-n'], input=_render(_pbx(), trunk_endpoint=trunk),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_render_script_defaults_schema_from_the_connection(self):
        script = views_pbx_install._render_script(_pbx(), _request())
        self.assertIn('[tenant_public]', script)


class TestDialplanEndpoint(CrmTestCase):
    """``GET /api/pbx/install/<token>/dialplan/`` — same token as the installer."""

    def _pbx(self, **kwargs):
        from crm.models import PbxServer

        defaults = {
            'name': 'PBX', 'fqdn': 'pbx.test.com', 'realtime_db_host': 'db',
            'realtime_db_name': 'asterisk_test', 'realtime_db_user': 'rw',
            'status': PbxServer.STATUS_PENDING,
        }
        defaults.update(kwargs)
        return PbxServer.objects.create(**defaults)

    def _get(self, token):
        return self.client.get(f'/api/pbx/install/{token}/dialplan/', HTTP_HOST='tenant.test.com')

    def test_renders_the_tenant_dialplan_with_its_trunk(self):
        from crm.models import Trunk

        pbx = self._pbx()
        Trunk.objects.create(name='Silknet', sip_server='sip.silknet.ge', username='u', password='p',
                             is_active=False)
        Trunk.objects.create(name='Magti', sip_server='sip.magti.ge', username='u', password='p')
        resp = self._get(pbx.enrollment_token)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp['Content-Type'].startswith('text/plain'))
        text = resp.content.decode()
        self.assertTrue(text.startswith('; Managed by EchoDesk'))
        self.assertIn('[tenant_test]', text)
        self.assertIn('[from-provider-test]', text)
        self.assertIn('Dial(PJSIP/${EXTEN}@trunk_magti,60)', text)
        self.assertIn('RECORDING_BASE=https://pbx.test.com:8443/recordings/', text)
        self.assertNotIn(pbx.api_token, text)
        self.assertNotIn(pbx.enrollment_token, text)

    def test_no_trunk_disables_outbound(self):
        pbx = self._pbx()
        text = self._get(pbx.enrollment_token).content.decode()
        self.assertIn('Playback(ss-noservice)', text)
        self.assertNotIn('Dial(PJSIP/${EXTEN}@', text)

    def test_unknown_and_expired_tokens_are_rejected(self):
        from datetime import timedelta

        self.assertEqual(self._get('nope').status_code, 404)
        pbx = self._pbx(enrollment_expires_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(self._get(pbx.enrollment_token).status_code, 410)

    def test_install_script_bakes_api_token_and_trunk(self):
        from crm.models import Trunk

        pbx = self._pbx()
        Trunk.objects.create(name='Magti', sip_server='sip.magti.ge', username='u', password='p')
        resp = self.client.get(f'/api/pbx/install/{pbx.enrollment_token}/', HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 200)
        script = resp.content.decode()
        self.assertIn(f'Environment="ECHODESK_PBX_TOKEN={pbx.api_token}"', script)
        self.assertNotIn(f'ECHODESK_PBX_TOKEN={pbx.enrollment_token}', script)
        self.assertIn('[tenant_test]', script)
        self.assertIn('Dial(PJSIP/${EXTEN}@trunk_magti,60)', script)


class TestAdminDsn(SimpleTestCase):

    @override_settings(ASTERISK_ADMIN_DB_HOST='postgres', ASTERISK_ADMIN_DB_PORT='5432')
    def test_admin_host_override(self):
        dsn = _admin_dsn()
        self.assertEqual((dsn['host'], dsn['port']), ('postgres', 5432))


class TestRealtimeFieldsExist(SimpleTestCase):
    """The sync tests mock the realtime models, so a field the sync writes but
    the model lacks (trust_id_outbound) failed every extension save silently."""

    def test_sync_defaults_are_model_fields(self):
        from asterisk_state.models import PsAor, PsEndpoint
        from crm import asterisk_sync

        endpoint_fields = {f.name for f in PsEndpoint._meta.get_fields()}
        aor_fields = {f.name for f in PsAor._meta.get_fields()}
        for defaults in (asterisk_sync.ENDPOINT_DEFAULTS_WEBRTC, asterisk_sync.ENDPOINT_DEFAULTS_TRUNK):
            self.assertLessEqual(set(defaults), endpoint_fields)
        self.assertIn('trust_id_outbound', endpoint_fields)
        for defaults in (asterisk_sync.AOR_DEFAULTS_WEBRTC, asterisk_sync.AOR_DEFAULTS_TRUNK):
            self.assertLessEqual(set(defaults), aor_fields)
