"""Unit tests for the payment-provider abstraction.

Money paths are exactly where an acquirer's technical diligence looks hardest,
and they had no unit coverage. These tests pin the deterministic, no-network
parts of the abstraction:

* the factory's provider selection + instance caching,
* each provider's capability flags (recurring / redirect),
* the capability contract (which providers support manual recurring charges),
* Paddle's HMAC-SHA256 webhook signature verification (self-contained).

External HTTP is never exercised — we only construct providers and call pure
methods. Provider ``__init__`` reads config lazily, so no live credentials are
required.
"""
import hashlib
import hmac
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from tenants.payment_providers.bog import BOGPaymentProvider
from tenants.payment_providers.factory import get_payment_provider
from tenants.payment_providers.flitt import FlittPaymentProvider
from tenants.payment_providers.paddle import PaddlePaymentProvider
from tenants.payment_providers.tbc import TBCPaymentProvider


class TestFactorySelection(SimpleTestCase):

    def test_explicit_name_selects_provider(self):
        self.assertIsInstance(get_payment_provider(provider_name="bog"), BOGPaymentProvider)
        self.assertIsInstance(get_payment_provider(provider_name="paddle"), PaddlePaymentProvider)
        self.assertIsInstance(get_payment_provider(provider_name="tbc"), TBCPaymentProvider)
        self.assertIsInstance(get_payment_provider(provider_name="flitt"), FlittPaymentProvider)

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            get_payment_provider(provider_name="bitcoin")

    def test_instances_are_cached_singletons(self):
        first = get_payment_provider(provider_name="bog")
        second = get_payment_provider(provider_name="bog")
        self.assertIs(first, second)

    def test_tenant_field_selects_provider(self):
        tenant = MagicMock()
        tenant.payment_provider = "paddle"
        self.assertIsInstance(get_payment_provider(tenant=tenant), PaddlePaymentProvider)

    def test_default_is_bog(self):
        self.assertIsInstance(get_payment_provider(), BOGPaymentProvider)


class TestProviderCapabilityFlags(SimpleTestCase):

    def test_bog_flags(self):
        bog = BOGPaymentProvider()
        self.assertEqual(bog.name, "bog")
        self.assertFalse(bog.manages_recurring_billing)  # EchoDesk charges via cron
        self.assertTrue(bog.requires_redirect)           # hosted payment page

    def test_paddle_flags(self):
        paddle = PaddlePaymentProvider()
        self.assertEqual(paddle.name, "paddle")
        self.assertTrue(paddle.manages_recurring_billing)  # Paddle bills automatically
        self.assertFalse(paddle.requires_redirect)         # overlay checkout


class TestRecurringChargeContract(SimpleTestCase):
    """Providers that manage billing automatically must reject manual charges."""

    def test_paddle_manual_charge_not_supported(self):
        with self.assertRaises(NotImplementedError):
            PaddlePaymentProvider().charge_recurring(parent_order_id="abc")


class TestPaddleWebhookVerification(SimpleTestCase):

    def _signed_headers(self, secret, body, ts="1700000000"):
        signed_payload = f"{ts}:{body.decode('utf-8')}"
        digest = hmac.new(
            secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {"Paddle-Signature": f"ts={ts};h1={digest}"}

    def test_valid_signature_passes(self):
        provider = PaddlePaymentProvider(webhook_secret="testsecret")
        body = b'{"event":"transaction.completed"}'
        headers = self._signed_headers("testsecret", body)
        self.assertTrue(provider.verify_webhook(headers, body))

    def test_tampered_body_fails(self):
        provider = PaddlePaymentProvider(webhook_secret="testsecret")
        body = b'{"event":"transaction.completed"}'
        headers = self._signed_headers("testsecret", body)
        self.assertFalse(provider.verify_webhook(headers, b'{"event":"tampered"}'))

    def test_wrong_secret_fails(self):
        provider = PaddlePaymentProvider(webhook_secret="testsecret")
        body = b'{"event":"x"}'
        headers = self._signed_headers("othersecret", body)
        self.assertFalse(provider.verify_webhook(headers, body))

    def test_missing_header_fails(self):
        provider = PaddlePaymentProvider(webhook_secret="testsecret")
        self.assertFalse(provider.verify_webhook({}, b"{}"))

    def test_malformed_header_fails(self):
        provider = PaddlePaymentProvider(webhook_secret="testsecret")
        self.assertFalse(
            provider.verify_webhook({"Paddle-Signature": "garbage"}, b"{}")
        )
