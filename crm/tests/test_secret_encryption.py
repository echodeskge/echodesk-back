"""Tests for crm/migrations/0018_calls_integration and crm/secret_migration.

Covers the data step that moves PBX secrets to Fernet at rest (forward,
idempotent, reverse), the ``PbxServer.api_token`` minting/backfill, the
``can_make_calls`` backfill, and that the Asterisk realtime sync still hands
Asterisk the *plaintext* password (PJSIP cannot read Fernet tokens).

The migration helpers take ``(apps, schema_editor)``; here they get a stand-in
whose ``.connection`` is the test connection, which is already bound to the
test tenant schema (``search_path``), exactly like a per-tenant migration run.
"""
import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.apps import apps as django_apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction

from crm.fields import _load_fernets
from crm.models import (
    PbxServer, PbxSettings, SipConfiguration, Trunk, UserPhoneAssignment,
)
from crm.secret_migration import (
    SECRET_COLUMNS, count_undecryptable, decrypt_existing, encrypt_existing,
)
from crm.tests.conftest import CrmTestCase
from users.models import User

migration_0018 = importlib.import_module('crm.migrations.0018_calls_integration')

FAKE_SCHEMA_EDITOR = SimpleNamespace(connection=connection)


def _raw(table, column, row_id):
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT "{column}" FROM "{table}" WHERE id = %s', [row_id])
        return cursor.fetchone()[0]


def _set_raw(table, column, row_id, value):
    with connection.cursor() as cursor:
        cursor.execute(f'UPDATE "{table}" SET "{column}" = %s WHERE id = %s', [value, row_id])


def _is_token(value):
    try:
        _load_fernets().decrypt(value.encode())
        return True
    except Exception:
        return False


class EncryptExistingTests(CrmTestCase):
    """Plaintext rows inserted behind the ORM's back, then migrated."""

    PLAIN = {
        ('crm_sipconfiguration', 'password'): 'sip-plain-1',
        ('crm_sipconfiguration', 'turn_password'): 'turn-plain-1',
        ('crm_userphoneassignment', 'extension_password'): 'ext-plain-1',
        ('crm_trunk', 'password'): 'trunk-plain-1',
        ('crm_pbxsettings', 'sms_api_key'): 'sms-plain-1',
    }

    def setUp(self):
        super().setUp()
        self.sip = self.create_sip_config(password='x', turn_password='y')
        self.assignment = self.create_phone_assignment(sip_config=self.sip, extension_password='x')
        self.trunk = Trunk.objects.create(
            name='Magti', sip_server='sip.magti.ge', username='u', password='x',
        )
        self.settings_row = self.create_pbx_settings(sip_config=self.sip, sms_api_key='x')
        self.ids = {
            'crm_sipconfiguration': self.sip.id,
            'crm_userphoneassignment': self.assignment.id,
            'crm_trunk': self.trunk.id,
            'crm_pbxsettings': self.settings_row.id,
        }
        # Simulate rows written by the pre-0018 plain CharField.
        for (table, column), plain in self.PLAIN.items():
            _set_raw(table, column, self.ids[table], plain)

    def _orm_values(self):
        return {
            ('crm_sipconfiguration', 'password'): SipConfiguration.objects.get(id=self.sip.id).password,
            ('crm_sipconfiguration', 'turn_password'): SipConfiguration.objects.get(id=self.sip.id).turn_password,
            ('crm_userphoneassignment', 'extension_password'):
                UserPhoneAssignment.objects.get(id=self.assignment.id).extension_password,
            ('crm_trunk', 'password'): Trunk.objects.get(id=self.trunk.id).password,
            ('crm_pbxsettings', 'sms_api_key'): PbxSettings.objects.get(id=self.settings_row.id).sms_api_key,
        }

    def test_secret_columns_cover_every_encrypted_product_field(self):
        self.assertEqual(
            SECRET_COLUMNS,
            {
                'crm_sipconfiguration': ('password', 'turn_password'),
                'crm_userphoneassignment': ('extension_password',),
                'crm_trunk': ('password',),
                'crm_pbxsettings': ('sms_api_key',),
            },
        )

    def test_plaintext_rows_read_as_blank_until_encrypted(self):
        # This is the hazard the same-migration data step exists for.
        for key, value in self._orm_values().items():
            self.assertEqual(value, '', key)

    def test_encrypt_existing_writes_ciphertext_and_orm_reads_plaintext(self):
        encrypt_existing(None, FAKE_SCHEMA_EDITOR)

        for (table, column), plain in self.PLAIN.items():
            raw = _raw(table, column, self.ids[table])
            self.assertNotEqual(raw, plain, (table, column))
            self.assertTrue(_is_token(raw), (table, column))
        self.assertEqual(self._orm_values(), self.PLAIN)

    def test_encrypt_existing_second_run_is_a_noop(self):
        encrypt_existing(None, FAKE_SCHEMA_EDITOR)
        first = {k: _raw(k[0], k[1], self.ids[k[0]]) for k in self.PLAIN}

        encrypt_existing(None, FAKE_SCHEMA_EDITOR)
        second = {k: _raw(k[0], k[1], self.ids[k[0]]) for k in self.PLAIN}

        # Fernet tokens carry a fresh IV per encrypt(); identical bytes prove
        # the second pass skipped every already-encrypted value.
        self.assertEqual(first, second)
        self.assertEqual(self._orm_values(), self.PLAIN)

    def test_decrypt_existing_restores_plaintext(self):
        encrypt_existing(None, FAKE_SCHEMA_EDITOR)
        decrypt_existing(None, FAKE_SCHEMA_EDITOR)

        for (table, column), plain in self.PLAIN.items():
            self.assertEqual(_raw(table, column, self.ids[table]), plain, (table, column))

    def test_empty_values_are_left_alone(self):
        _set_raw('crm_sipconfiguration', 'turn_password', self.sip.id, '')
        encrypt_existing(None, FAKE_SCHEMA_EDITOR)
        self.assertEqual(_raw('crm_sipconfiguration', 'turn_password', self.sip.id), '')
        self.assertEqual(SipConfiguration.objects.get(id=self.sip.id).turn_password, '')

    def test_count_undecryptable(self):
        stats = count_undecryptable(connection, 'crm_sipconfiguration', 'password')
        self.assertEqual(stats, {'total': 1, 'undecryptable': 1})
        encrypt_existing(None, FAKE_SCHEMA_EDITOR)
        stats = count_undecryptable(connection, 'crm_sipconfiguration', 'password')
        self.assertEqual(stats, {'total': 1, 'undecryptable': 0})

    def test_orm_round_trip_is_encrypted_at_rest(self):
        self.sip.password = 'fresh-secret'
        self.sip.save()
        raw = _raw('crm_sipconfiguration', 'password', self.sip.id)
        self.assertNotEqual(raw, 'fresh-secret')
        self.assertTrue(_is_token(raw))
        self.assertEqual(SipConfiguration.objects.get(id=self.sip.id).password, 'fresh-secret')


class VerifyPbxSecretsCommandTests(CrmTestCase):

    def test_passes_when_everything_decrypts(self):
        self.create_sip_config(password='ok', turn_password='ok2')
        call_command('verify_pbx_secrets', self.tenant.schema_name, verbosity=0)

    def test_fails_when_a_value_does_not_decrypt(self):
        sip = self.create_sip_config(password='ok')
        _set_raw('crm_sipconfiguration', 'password', sip.id, 'legacy-plaintext')
        with self.assertRaises(CommandError) as ctx:
            call_command('verify_pbx_secrets', self.tenant.schema_name, verbosity=0)
        self.assertIn('1 undecryptable', str(ctx.exception))
        # never prints the value
        self.assertNotIn('legacy-plaintext', str(ctx.exception))


class AsteriskSyncPlaintextTests(CrmTestCase):
    """PJSIP reads ``ps_auths.password`` as-is: the sync must decrypt first."""

    @patch('crm.asterisk_sync.transaction')
    @patch('asterisk_state.models.PsIdentify')
    @patch('asterisk_state.models.PsEndpoint')
    @patch('asterisk_state.models.PsAor')
    @patch('asterisk_state.models.PsAuth')
    def test_endpoint_sync_sends_plaintext_extension_password(
        self, PsAuth, PsAor, PsEndpoint, PsIdentify, _tx,
    ):
        from crm.asterisk_sync import AsteriskStateSync

        assignment = self.create_phone_assignment(extension_password='ext-secret')
        self.assertTrue(_is_token(_raw('crm_userphoneassignment', 'extension_password', assignment.id)))
        assignment = UserPhoneAssignment.objects.select_related('user').get(id=assignment.id)

        pbx = MagicMock(use_tenant_prefix=False)
        with patch('crm.asterisk_sync.register_pbx_alias', return_value='asterisk_test'):
            sync = AsteriskStateSync(self.tenant.schema_name, pbx=pbx)
        sync.sync_endpoint(assignment)

        auth_call = PsAuth.objects.using.return_value.update_or_create.call_args
        self.assertEqual(auth_call.kwargs['defaults']['password'], 'ext-secret')

    @patch('crm.asterisk_sync.transaction')
    @patch('asterisk_state.models.PsRegistration')
    @patch('asterisk_state.models.PsIdentify')
    @patch('asterisk_state.models.PsEndpoint')
    @patch('asterisk_state.models.PsAor')
    @patch('asterisk_state.models.PsAuth')
    def test_trunk_sync_sends_plaintext_password(
        self, PsAuth, PsAor, PsEndpoint, PsIdentify, PsRegistration, _tx,
    ):
        from crm.asterisk_sync import AsteriskStateSync

        trunk = Trunk.objects.create(
            name='Magti', sip_server='sip.magti.ge', username='u', password='trunk-secret',
        )
        self.assertTrue(_is_token(_raw('crm_trunk', 'password', trunk.id)))
        trunk = Trunk.objects.get(id=trunk.id)

        pbx = MagicMock(use_tenant_prefix=False)
        with patch('crm.asterisk_sync.register_pbx_alias', return_value='asterisk_test'):
            sync = AsteriskStateSync(self.tenant.schema_name, pbx=pbx)
        sync.sync_trunk(trunk)

        auth_call = PsAuth.objects.using.return_value.update_or_create.call_args
        self.assertEqual(auth_call.kwargs['defaults']['password'], 'trunk-secret')


class PbxServerApiTokenTests(CrmTestCase):

    def _pbx(self, **kwargs):
        defaults = {
            'name': 'PBX', 'fqdn': 'pbx.test.com',
            'realtime_db_host': 'db', 'realtime_db_name': 'asterisk_test',
            'realtime_db_user': 'rw', 'status': PbxServer.STATUS_PENDING,
        }
        defaults.update(kwargs)
        return PbxServer.objects.create(**defaults)

    def test_api_token_is_minted_on_save_and_unique(self):
        a = self._pbx(name='A')
        b = self._pbx(name='B')
        for pbx in (a, b):
            self.assertTrue(pbx.api_token)
            self.assertGreaterEqual(len(pbx.api_token), 40)
            self.assertLessEqual(len(pbx.api_token), 64)
            self.assertNotEqual(pbx.api_token, pbx.enrollment_token)
        self.assertNotEqual(a.api_token, b.api_token)

    def test_api_token_is_kept_on_resave(self):
        pbx = self._pbx()
        token = pbx.api_token
        pbx.name = 'renamed'
        pbx.save()
        self.assertEqual(PbxServer.objects.get(id=pbx.id).api_token, token)

    def test_regenerate_api_token_changes_it(self):
        pbx = self._pbx()
        old = pbx.api_token
        pbx.regenerate_api_token()
        pbx.save()
        self.assertNotEqual(pbx.api_token, old)
        self.assertEqual(PbxServer.objects.get(id=pbx.id).api_token, pbx.api_token)

    def test_duplicate_api_token_is_rejected(self):
        a = self._pbx(name='A')
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._pbx(name='B', api_token=a.api_token)

    def test_backfill_mints_tokens_for_rows_without_one(self):
        pbx = self._pbx()
        _set_raw('crm_pbxserver', 'api_token', pbx.id, '')
        other = self._pbx(name='other')
        keep = other.api_token

        migration_0018.backfill_api_token(django_apps, FAKE_SCHEMA_EDITOR)

        minted = PbxServer.objects.get(id=pbx.id).api_token
        self.assertTrue(minted)
        self.assertGreaterEqual(len(minted), 40)
        self.assertEqual(PbxServer.objects.get(id=other.id).api_token, keep)


class CanMakeCallsBackfillTests(CrmTestCase):

    def test_users_with_active_assignment_get_make_calls(self):
        sip = self.create_sip_config()
        active = self.create_user(email='active@test.com', can_make_calls=False)
        inactive = self.create_user(email='inactive@test.com', can_make_calls=False)
        self.create_user(email='nobody@test.com', can_make_calls=False)
        self.create_user(email='already@test.com', can_make_calls=True)
        self.create_phone_assignment(user=active, sip_config=sip, extension='101')
        self.create_phone_assignment(user=inactive, sip_config=sip, extension='102', is_active=False)

        migration_0018.grant_make_calls_to_assigned_users(None, FAKE_SCHEMA_EDITOR)

        flags = {u.email: u.can_make_calls for u in User.objects.filter(
            email__in=['active@test.com', 'inactive@test.com', 'nobody@test.com', 'already@test.com'])}
        self.assertEqual(flags, {
            'active@test.com': True,
            'inactive@test.com': False,
            'nobody@test.com': False,
            'already@test.com': True,
        })
