"""Unit tests for webhook → tenant routing.

Covers the resolution branching (O(1) route hit → legacy scan fallback →
self-heal → miss) and the signal handlers, all without a database by mocking
at the module-function boundary.
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from social_integrations import platform_routing as pr


class TestResolveTenant(SimpleTestCase):

    @patch.object(pr, "_scan_tenants")
    @patch.object(pr, "route_lookup", return_value="acme")
    def test_route_hit_skips_scan(self, mock_lookup, mock_scan):
        self.assertEqual(pr.resolve_tenant("facebook", "PAGE1"), "acme")
        mock_lookup.assert_called_once_with("facebook", "PAGE1")
        mock_scan.assert_not_called()

    @patch.object(pr, "route_upsert")
    @patch.object(pr, "_scan_tenants", return_value="beta")
    @patch.object(pr, "route_lookup", return_value=None)
    def test_fallback_scan_self_heals(self, mock_lookup, mock_scan, mock_upsert):
        self.assertEqual(pr.resolve_tenant("whatsapp", "PN1"), "beta")
        mock_scan.assert_called_once_with("whatsapp", "PN1")
        mock_upsert.assert_called_once_with("whatsapp", "PN1", "beta")

    @patch.object(pr, "route_upsert")
    @patch.object(pr, "_scan_tenants", return_value=None)
    @patch.object(pr, "route_lookup", return_value=None)
    def test_miss_returns_none_without_upsert(self, mock_lookup, mock_scan, mock_upsert):
        self.assertIsNone(pr.resolve_tenant("tiktok", "SHOP1"))
        mock_upsert.assert_not_called()

    def test_unknown_platform_raises(self):
        with self.assertRaises(ValueError):
            pr.resolve_tenant("myspace", "x")

    @patch.object(pr, "route_lookup")
    def test_empty_external_id_returns_none(self, mock_lookup):
        self.assertIsNone(pr.resolve_tenant("facebook", ""))
        mock_lookup.assert_not_called()


class TestSignalHandlers(SimpleTestCase):

    @patch.object(pr, "route_upsert")
    @patch.object(pr, "_current_schema", return_value="acme")
    def test_save_handler_upserts_route(self, _schema, mock_upsert):
        handler = pr._make_save_handler("facebook", "page_id")
        instance = MagicMock()
        instance.page_id = "PAGE1"
        instance.is_active = True
        handler(sender=None, instance=instance)
        mock_upsert.assert_called_once_with("facebook", "PAGE1", "acme", is_active=True)

    @patch.object(pr, "route_upsert")
    @patch.object(pr, "_current_schema", return_value=None)
    def test_save_handler_noops_on_public(self, _schema, mock_upsert):
        handler = pr._make_save_handler("facebook", "page_id")
        handler(sender=None, instance=MagicMock(page_id="PAGE1"))
        mock_upsert.assert_not_called()

    @patch.object(pr, "route_deactivate")
    def test_delete_handler_deactivates(self, mock_deactivate):
        handler = pr._make_delete_handler("tiktok", "shop_id")
        instance = MagicMock()
        instance.shop_id = "SHOP1"
        handler(sender=None, instance=instance)
        mock_deactivate.assert_called_once_with("tiktok", "SHOP1")
