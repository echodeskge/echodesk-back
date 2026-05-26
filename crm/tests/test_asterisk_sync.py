"""Unit tests for the Django → Asterisk realtime sync service.

These tests exercise ``AsteriskStateSync`` **without a real Asterisk DB**: the
per-tenant alias is stubbed (``register_pbx_alias`` patched), the shadow-model
managers are mocked, and ``transaction.atomic`` is a no-op MagicMock context
manager. We assert the service computes the correct realtime IDs (the tenant
prefix convention) and issues the expected ``update_or_create`` / ``delete``
calls — the logic most likely to silently break and hardest to debug live.

See docs/ARCHITECTURE_PBX.md for the system this guards.
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from crm.asterisk_sync import AsteriskStateSync, _format_codecs, _slugify_trunk


def _make_assignment(extension="100", display_name="Test Agent"):
    assignment = MagicMock()
    assignment.extension = extension
    assignment.extension_password = "extpass123"
    assignment.display_name = display_name
    assignment.user.email = "agent@test.com"
    return assignment


class TestPrefixAndHelpers(SimpleTestCase):
    """Pure-logic helpers — no tenant, no DB."""

    def test_prefix_with_applies_schema_when_enabled(self):
        self.assertEqual(
            AsteriskStateSync.prefix_with("amanati", "100", True), "amanati_100"
        )

    def test_prefix_with_returns_bare_name_when_disabled(self):
        self.assertEqual(
            AsteriskStateSync.prefix_with("acme", "100", False), "100"
        )

    def test_slugify_trunk_normalises_name(self):
        trunk = MagicMock()
        trunk.name = "Magti Main Trunk!"
        trunk.id = 7
        self.assertEqual(_slugify_trunk(trunk), "magti_main_trunk")

    def test_slugify_trunk_falls_back_to_id(self):
        trunk = MagicMock()
        trunk.name = "!!!"
        trunk.id = 7
        self.assertEqual(_slugify_trunk(trunk), "trunk7")

    def test_format_codecs_joins_list(self):
        self.assertEqual(_format_codecs(["g722", "alaw"]), "g722,alaw")

    def test_format_codecs_passthrough_string(self):
        self.assertEqual(_format_codecs("ulaw,alaw"), "ulaw,alaw")

    def test_format_codecs_empty(self):
        self.assertEqual(_format_codecs(None), "")
        self.assertEqual(_format_codecs([]), "")


def _make_sync(use_tenant_prefix=False, schema="testschema"):
    """Build an AsteriskStateSync with a stubbed PbxServer + alias (no DB)."""
    pbx = MagicMock()
    pbx.use_tenant_prefix = use_tenant_prefix
    with patch("crm.asterisk_sync.register_pbx_alias", return_value="asterisk_test"):
        return AsteriskStateSync(schema, pbx=pbx)


class TestSyncEndpoint(SimpleTestCase):

    @patch("crm.asterisk_sync.transaction")
    @patch("asterisk_state.models.PsIdentify")
    @patch("asterisk_state.models.PsEndpoint")
    @patch("asterisk_state.models.PsAor")
    @patch("asterisk_state.models.PsAuth")
    def test_sync_endpoint_upserts_four_rows_unprefixed(
        self, PsAuth, PsAor, PsEndpoint, PsIdentify, _tx
    ):
        sync = _make_sync(use_tenant_prefix=False)
        sync.sync_endpoint(_make_assignment(extension="100"))

        # endpoint_id is the bare extension when prefixing is off
        auth_call = PsAuth.objects.using.return_value.update_or_create.call_args
        self.assertEqual(auth_call.kwargs["id"], "100")
        self.assertEqual(auth_call.kwargs["defaults"]["username"], "100")
        self.assertEqual(auth_call.kwargs["defaults"]["password"], "extpass123")

        ep_call = PsEndpoint.objects.using.return_value.update_or_create.call_args
        self.assertEqual(ep_call.kwargs["id"], "100")
        self.assertEqual(ep_call.kwargs["defaults"]["context"], "tenant_testschema")
        self.assertEqual(ep_call.kwargs["defaults"]["aors"], "100")
        self.assertEqual(ep_call.kwargs["defaults"]["auth"], "100")

        PsAor.objects.using.return_value.update_or_create.assert_called_once()
        # WebRTC endpoints clear any stale identify row
        PsIdentify.objects.using.return_value.filter.return_value.delete.assert_called_once()

    @patch("crm.asterisk_sync.transaction")
    @patch("asterisk_state.models.PsIdentify")
    @patch("asterisk_state.models.PsEndpoint")
    @patch("asterisk_state.models.PsAor")
    @patch("asterisk_state.models.PsAuth")
    def test_sync_endpoint_prefixes_id_when_enabled(
        self, PsAuth, PsAor, PsEndpoint, PsIdentify, _tx
    ):
        sync = _make_sync(use_tenant_prefix=True, schema="amanati")
        sync.sync_endpoint(_make_assignment(extension="100"))

        ep_call = PsEndpoint.objects.using.return_value.update_or_create.call_args
        self.assertEqual(ep_call.kwargs["id"], "amanati_100")
        self.assertEqual(ep_call.kwargs["defaults"]["context"], "tenant_amanati")

    @patch("crm.asterisk_sync.transaction")
    @patch("asterisk_state.models.PsIdentify")
    @patch("asterisk_state.models.PsEndpoint")
    @patch("asterisk_state.models.PsAor")
    @patch("asterisk_state.models.PsAuth")
    def test_tombstone_endpoint_deletes_all_rows(
        self, PsAuth, PsAor, PsEndpoint, PsIdentify, _tx
    ):
        sync = _make_sync(use_tenant_prefix=False)
        sync.tombstone_endpoint(assignment_id=5, extension="100")

        for model in (PsEndpoint, PsIdentify, PsAor, PsAuth):
            filter_call = model.objects.using.return_value.filter.call_args
            self.assertEqual(filter_call.kwargs.get("id"), "100")
            model.objects.using.return_value.filter.return_value.delete.assert_called_once()


class TestSyncNoOps(SimpleTestCase):
    """When there is no active PbxServer, every method must no-op (no writes)."""

    @patch("asterisk_state.models.PsEndpoint")
    @patch("crm.asterisk_sync.get_active_pbx_for_current_tenant", return_value=None)
    def test_no_pbx_means_no_writes(self, _get_pbx, PsEndpoint):
        sync = AsteriskStateSync("testschema")  # pbx resolves to None → disabled
        self.assertIsNone(sync.alias)
        self.assertFalse(sync._enabled())

        sync.sync_endpoint(_make_assignment())
        PsEndpoint.objects.using.assert_not_called()

    @patch("asterisk_state.models.PsEndpoint")
    def test_kill_switch_disables_writes(self, PsEndpoint):
        sync = _make_sync(use_tenant_prefix=False)
        with self.settings(ASTERISK_SYNC_ENABLED=False):
            self.assertFalse(sync._enabled())
            sync.sync_endpoint(_make_assignment())
        PsEndpoint.objects.using.assert_not_called()


class TestSyncTrunk(SimpleTestCase):

    def _make_trunk(self, register=False):
        trunk = MagicMock()
        trunk.name = "Magti"
        trunk.id = 3
        trunk.codecs = ["alaw", "ulaw"]
        trunk.caller_id_number = "+995322421219"
        trunk.username = "trunkuser"
        trunk.password = "trunkpass"
        trunk.realm = "magti.ge"
        trunk.sip_server = "sip.magti.ge"
        trunk.sip_port = 5060
        trunk.register = register
        return trunk

    @patch("crm.asterisk_sync.transaction")
    @patch("asterisk_state.models.PsRegistration")
    @patch("asterisk_state.models.PsIdentify")
    @patch("asterisk_state.models.PsEndpoint")
    @patch("asterisk_state.models.PsAor")
    @patch("asterisk_state.models.PsAuth")
    def test_sync_trunk_without_registration_deletes_reg_row(
        self, PsAuth, PsAor, PsEndpoint, PsIdentify, PsRegistration, _tx
    ):
        sync = _make_sync(use_tenant_prefix=False)
        sync.sync_trunk(self._make_trunk(register=False))

        ep_call = PsEndpoint.objects.using.return_value.update_or_create.call_args
        self.assertEqual(ep_call.kwargs["id"], "trunk_magti")
        # register=False → registration row removed, not created
        PsRegistration.objects.using.return_value.update_or_create.assert_not_called()
        PsRegistration.objects.using.return_value.filter.return_value.delete.assert_called_once()

    @patch("crm.asterisk_sync.transaction")
    @patch("asterisk_state.models.PsRegistration")
    @patch("asterisk_state.models.PsIdentify")
    @patch("asterisk_state.models.PsEndpoint")
    @patch("asterisk_state.models.PsAor")
    @patch("asterisk_state.models.PsAuth")
    def test_sync_trunk_with_registration_creates_reg_row(
        self, PsAuth, PsAor, PsEndpoint, PsIdentify, PsRegistration, _tx
    ):
        sync = _make_sync(use_tenant_prefix=False)
        sync.sync_trunk(self._make_trunk(register=True))

        PsRegistration.objects.using.return_value.update_or_create.assert_called_once()
        reg_call = PsRegistration.objects.using.return_value.update_or_create.call_args
        self.assertEqual(reg_call.kwargs["id"], "trunk_magti")
        self.assertEqual(
            reg_call.kwargs["defaults"]["server_uri"], "sip:sip.magti.ge:5060"
        )
