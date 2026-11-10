"""seed_local_pbx (the local Asterisk harness seed)."""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from crm.management.commands.seed_local_pbx import DEFAULT_EXT_PASSWORD, EXTENSIONS, NAME
from crm.models import PbxServer, SipConfiguration, UserPhoneAssignment
from crm.tests.conftest import CrmTestCase
from users.models import User


class SeedLocalPbxCommandTests(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.users = {}
        for extension, email, _display in EXTENSIONS:
            if extension == '101':
                self.users[extension] = self.create_admin(email=email)
            else:
                self.users[extension] = self.create_user(email=email)

    def run_seed(self, **kwargs):
        out, err = StringIO(), StringIO()
        call_command('seed_local_pbx', '--schema', self.tenant.schema_name,
                     stdout=out, stderr=err, **kwargs)
        return out.getvalue(), err.getvalue()

    @override_settings(DEBUG=False)
    def test_refuses_without_debug(self):
        with self.assertRaises(CommandError) as ctx:
            self.run_seed()
        self.assertIn('DEBUG', str(ctx.exception))
        self.assertFalse(SipConfiguration.objects.exists())
        self.assertFalse(UserPhoneAssignment.objects.exists())
        self.assertFalse(PbxServer.objects.exists())

    @override_settings(DEBUG=True)
    def test_creates_the_harness_rows_and_prints_only_the_token(self):
        out, err = self.run_seed()

        sip = SipConfiguration.objects.get(name=NAME)
        self.assertEqual((sip.sip_server, sip.sip_port, sip.websocket_path), ('localhost', 8088, '/ws'))
        self.assertEqual(sip.realm, 'localhost')
        self.assertEqual(sip.stun_server, '')
        self.assertTrue(sip.is_default)
        self.assertTrue(sip.is_active)
        self.assertEqual(sip.created_by, self.users['101'])

        for extension, email, display_name in EXTENSIONS:
            assignment = UserPhoneAssignment.objects.get(sip_configuration=sip, extension=extension)
            self.assertEqual(assignment.user.email, email)
            self.assertEqual(assignment.extension_password, DEFAULT_EXT_PASSWORD)
            self.assertEqual(assignment.display_name, display_name)
            self.assertTrue(assignment.is_primary)
            self.assertTrue(assignment.is_active)
            self.assertTrue(User.objects.get(email=email).can_make_calls)

        pbx = PbxServer.objects.get(name=NAME)
        self.assertEqual(pbx.status, PbxServer.STATUS_ACTIVE)
        self.assertTrue(pbx.is_active)
        self.assertEqual(pbx.fqdn, 'localhost')
        self.assertEqual((pbx.ami_host, pbx.ami_port), ('127.0.0.1', 5038))
        self.assertEqual((pbx.ami_username, pbx.ami_password), ('echodesk', 'echodesk'))
        self.assertEqual(pbx.wss_url, 'ws://localhost:8088/ws')
        self.assertEqual(pbx.recording_base_url, 'http://localhost:8088/static/recordings/')
        self.assertFalse(pbx.use_tenant_prefix)
        self.assertTrue(pbx.api_token)
        self.assertTrue(pbx.enrollment_token)

        # stdout is the token only; the summary goes to stderr
        self.assertEqual(out, pbx.api_token + '\n')
        self.assertIn('PbxServer', err)
        self.assertNotIn(DEFAULT_EXT_PASSWORD, out + err)

    @override_settings(DEBUG=True)
    def test_is_idempotent_and_keeps_the_token(self):
        first, _ = self.run_seed()
        # drift that a re-run must repair
        SipConfiguration.objects.filter(name=NAME).update(stun_server='stun:x', is_default=False)
        User.objects.filter(email='agent@demo.ge').update(can_make_calls=False)

        second, _ = self.run_seed(ext_password='other-pass')

        self.assertEqual(first, second)
        self.assertEqual(SipConfiguration.objects.count(), 1)
        self.assertEqual(PbxServer.objects.count(), 1)
        self.assertEqual(UserPhoneAssignment.objects.count(), 3)
        sip = SipConfiguration.objects.get(name=NAME)
        self.assertEqual(sip.stun_server, '')
        self.assertTrue(sip.is_default)
        self.assertTrue(User.objects.get(email='agent@demo.ge').can_make_calls)
        self.assertEqual(
            UserPhoneAssignment.objects.get(extension='102').extension_password, 'other-pass',
        )

    @override_settings(DEBUG=True)
    def test_extension_password_from_env(self):
        with patch.dict('os.environ', {'LOCAL_PBX_EXT_PASSWORD': 'from-env'}):
            self.run_seed()
        self.assertEqual(UserPhoneAssignment.objects.get(extension='101').extension_password, 'from-env')

    @override_settings(DEBUG=True)
    def test_becomes_the_default_sip_configuration(self):
        other = self.create_sip_config(name='Old PBX', is_default=True)
        self.run_seed()
        other.refresh_from_db()
        self.assertFalse(other.is_default)
        self.assertTrue(SipConfiguration.objects.get(name=NAME).is_default)

    @override_settings(DEBUG=True)
    def test_missing_user_is_an_error_and_writes_nothing(self):
        self.users['103'].delete()
        with self.assertRaises(CommandError) as ctx:
            self.run_seed()
        self.assertIn('staff1@demo.ge', str(ctx.exception))
        self.assertFalse(SipConfiguration.objects.exists())
        self.assertFalse(PbxServer.objects.exists())

    @override_settings(DEBUG=True)
    def test_unknown_schema_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command('seed_local_pbx', '--schema', 'no_such_tenant', stdout=StringIO(), stderr=StringIO())

