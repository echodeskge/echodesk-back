"""Unit tests for AsteriskStateRouter.

The router decides which DB alias ``asterisk_state.*`` models use, and confines
their migrations to ``asterisk_*`` aliases. Getting this wrong risks either
writing realtime rows into the default app DB or running app migrations into a
tenant's Asterisk DB — so it's worth pinning down. No DB needed: we mock the
alias resolver and pass lightweight model stand-ins.

See docs/ARCHITECTURE_PBX.md §2c.
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from amanati_crm.db_routers import AsteriskStateRouter


def _model(app_label):
    model = MagicMock()
    model._meta.app_label = app_label
    return model


class TestAllowMigrate(SimpleTestCase):

    def setUp(self):
        self.router = AsteriskStateRouter()

    def test_asterisk_models_only_on_asterisk_alias(self):
        self.assertTrue(
            self.router.allow_migrate("asterisk_acme", "asterisk_state")
        )

    def test_asterisk_models_blocked_on_default(self):
        self.assertFalse(self.router.allow_migrate("default", "asterisk_state"))

    def test_other_apps_blocked_on_asterisk_alias(self):
        self.assertFalse(self.router.allow_migrate("asterisk_acme", "crm"))

    def test_other_apps_deferred_on_default(self):
        self.assertIsNone(self.router.allow_migrate("default", "crm"))


class TestDbForReadWrite(SimpleTestCase):

    def setUp(self):
        self.router = AsteriskStateRouter()

    @patch(
        "crm.asterisk_db.get_asterisk_connection_for_current_tenant",
        return_value=("asterisk_acme", object()),
    )
    def test_asterisk_model_routes_to_resolved_alias(self, _resolver):
        self.assertEqual(
            self.router.db_for_write(_model("asterisk_state")), "asterisk_acme"
        )
        self.assertEqual(
            self.router.db_for_read(_model("asterisk_state")), "asterisk_acme"
        )

    def test_non_asterisk_model_returns_none(self):
        self.assertIsNone(self.router.db_for_write(_model("crm")))
        self.assertIsNone(self.router.db_for_read(_model("tickets")))


class TestAllowRelation(SimpleTestCase):

    def setUp(self):
        self.router = AsteriskStateRouter()

    def test_both_asterisk_allowed(self):
        self.assertTrue(
            self.router.allow_relation(_model("asterisk_state"), _model("asterisk_state"))
        )

    def test_one_asterisk_blocked(self):
        self.assertFalse(
            self.router.allow_relation(_model("asterisk_state"), _model("crm"))
        )

    def test_neither_asterisk_deferred(self):
        self.assertIsNone(
            self.router.allow_relation(_model("crm"), _model("tickets"))
        )
