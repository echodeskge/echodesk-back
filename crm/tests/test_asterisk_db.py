"""Unit tests for the runtime per-tenant Asterisk DB-alias mechanism.

``crm/asterisk_db.py`` is the highest-complexity area of the calling code: it
mutates ``django.db.connections.databases`` at runtime to register a
``asterisk_<schema>`` alias built from a tenant's ``PbxServer`` row. These
tests cover the pure config-building + idempotency logic without connecting to
any database (registration only mutates the in-memory dict). Each test cleans
up any alias it injects so global connection state isn't leaked between tests.

See docs/ARCHITECTURE_PBX.md §2b.
"""
from unittest.mock import MagicMock

from django.db import connections
from django.test import SimpleTestCase

from crm.asterisk_db import _build_db_config, alias_for_schema, register_pbx_alias


def _make_pbx(name="asterisk_acme", host="db.example.com"):
    pbx = MagicMock()
    pbx.realtime_db_name = name
    pbx.realtime_db_user = "asterisk_rw_acme"
    pbx.realtime_db_password = "secret"
    pbx.realtime_db_host = host
    pbx.realtime_db_port = 25060
    pbx.realtime_db_sslmode = "require"
    return pbx


class TestAliasForSchema(SimpleTestCase):

    def test_deterministic_name(self):
        self.assertEqual(alias_for_schema("amanati"), "asterisk_amanati")


class TestBuildDbConfig(SimpleTestCase):

    def test_maps_pbx_fields_to_django_config(self):
        cfg = _build_db_config(_make_pbx())
        self.assertEqual(cfg["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(cfg["NAME"], "asterisk_acme")
        self.assertEqual(cfg["USER"], "asterisk_rw_acme")
        self.assertEqual(cfg["PASSWORD"], "secret")
        self.assertEqual(cfg["HOST"], "db.example.com")
        self.assertEqual(cfg["PORT"], "25060")
        self.assertEqual(cfg["OPTIONS"]["sslmode"], "require")
        # short connect timeout so a request pod never hangs on an unreachable DB
        self.assertEqual(cfg["OPTIONS"]["connect_timeout"], 5)


class TestRegisterPbxAlias(SimpleTestCase):

    def setUp(self):
        self._aliases_to_clean = []

    def tearDown(self):
        for alias in self._aliases_to_clean:
            connections.databases.pop(alias, None)

    def test_registers_alias_into_connections(self):
        alias = register_pbx_alias(_make_pbx(), schema_name="acme")
        self._aliases_to_clean.append(alias)

        self.assertEqual(alias, "asterisk_acme")
        self.assertIn("asterisk_acme", connections.databases)
        self.assertEqual(connections.databases["asterisk_acme"]["NAME"], "asterisk_acme")

    def test_idempotent_same_db_refreshes_in_place(self):
        register_pbx_alias(_make_pbx(), schema_name="acme")
        self._aliases_to_clean.append("asterisk_acme")

        # Re-register with new credentials but the same DB name → updated in place
        pbx2 = _make_pbx()
        pbx2.realtime_db_password = "rotated"
        register_pbx_alias(pbx2, schema_name="acme")

        self.assertEqual(connections.databases["asterisk_acme"]["PASSWORD"], "rotated")

    def test_rejects_public_schema(self):
        with self.assertRaises(ValueError):
            register_pbx_alias(_make_pbx(), schema_name="public")
