"""The BYO install script a tenant pipes into ``sudo bash``."""
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.test import RequestFactory, SimpleTestCase, override_settings

from crm import views_pbx_install
from crm.pbx_provisioning import _admin_dsn


def _pbx(**overrides):
    values = dict(
        id=7, name='Main PBX', fqdn='pbx.acme.test',
        realtime_db_host='pgbouncer', realtime_db_port=5432,
        realtime_db_name='asterisk_acme', realtime_db_user='asterisk_rw_acme',
        realtime_db_password="p'w$d", ami_username='echodesk', ami_password='s3cret',
        enrollment_token='tok123',
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _render(pbx):
    request = RequestFactory().get('/api/pbx/install/tok123/', HTTP_HOST='api.echodesk.ge')
    return views_pbx_install._render_script(pbx, request)


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
        self.assertIn('Environment="ECHODESK_PBX_TOKEN=tok123"', script)
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
