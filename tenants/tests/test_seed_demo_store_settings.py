"""
``seed_demo_tenant`` store settings: the demo tenant's EcommerceSettings row
is filled in (web /settings/ecommerce and the mobile Store settings screen
read ``GET /api/ecommerce/admin/settings/`` -> ``results[0]``), without any
payment-gateway credential, idempotently, and tenants seeded before the
``store_settings`` module existed are topped up on the next run.
"""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test.utils import override_settings
from rest_framework.test import APIClient
from tenant_schemas.utils import schema_context

from tenants.models import Tenant
from users.tests.conftest import EchoDeskTenantTestCase

SETTINGS_URL = '/api/ecommerce/admin/settings/'
DEMO_HOST = 'demo.api.lvh.me'


def _seed():
    out = StringIO()
    with override_settings(DEBUG=True):
        call_command('seed_demo_tenant', stdout=out, stderr=StringIO())
    return out.getvalue()


class SeedDemoStoreSettingsTests(EchoDeskTenantTestCase):

    def _settings(self):
        from ecommerce_crm.models import EcommerceSettings
        tenant = Tenant.objects.get(schema_name='demo')
        with schema_context('demo'):
            return EcommerceSettings.objects.get(tenant=tenant)

    def _api(self, method, url, data=None):
        from users.models import User
        with schema_context('demo'):
            admin = User.objects.get(email='admin@demo.ge')
        client = APIClient()
        client.force_authenticate(user=admin)
        with override_settings(ALLOWED_HOSTS=['*']):
            kwargs = {'HTTP_HOST': DEMO_HOST}
            if data is not None:
                kwargs['format'] = 'json'
                return getattr(client, method)(url, data, **kwargs)
            return getattr(client, method)(url, **kwargs)

    def test_seed_fills_store_settings_idempotently_and_api_returns_them(self):
        out = _seed()
        self.assertIn('store_settings: seeded', out)
        first = self._settings()

        self.assertEqual(first.store_name, 'EchoDesk Demo Store')
        self.assertEqual(first.store_email, 'shop@demo.ge')
        self.assertTrue(first.store_phone)
        self.assertTrue(first.enable_cash_on_delivery)
        self.assertFalse(first.enable_card_payment)
        self.assertTrue(first.allow_pickup)
        self.assertFalse(first.quickshipper_enabled)
        self.assertEqual(first.quickshipper_pickup_city, 'Tbilisi')
        self.assertEqual(first.tax_rate, Decimal('18.00'))
        self.assertTrue(first.tax_inclusive)
        self.assertEqual(first.tax_label, 'VAT')
        self.assertEqual(first.active_payment_providers, ['cash'])
        self.assertTrue(first.bog_return_url_success.endswith('/payment/success'))
        self.assertTrue(first.bog_return_url_fail.endswith('/payment/fail'))
        # No gateway credential of any kind.
        for field in ('bog_client_id', 'tbc_client_id', 'tbc_api_key', 'flitt_merchant_id',
                      'paddle_webhook_secret', 'paddle_client_token', 'google_maps_api_key'):
            self.assertEqual(getattr(first, field), '', field)
        self.assertFalse(first.has_bog_credentials)
        self.assertFalse(first.has_tbc_credentials)
        self.assertFalse(first.has_flitt_credentials)
        self.assertFalse(first.has_paddle_credentials)
        self.assertFalse(first.has_quickshipper_credentials)

        # Second run: skipped, row untouched, still exactly one row.
        out2 = _seed()
        self.assertIn('store_settings: already seeded', out2)
        second = self._settings()
        self.assertEqual((second.pk, second.updated_at), (first.pk, first.updated_at))
        from tenants.demo_seed import demo_counts
        with schema_context('demo'):
            self.assertEqual(demo_counts(Tenant.objects.get(schema_name='demo'))['ecommerce_settings'], 1)

        # The endpoint both clients read returns the populated singleton.
        res = self._api('get', SETTINGS_URL)
        self.assertEqual(res.status_code, 200, res.content)
        results = res.json()['results'] if isinstance(res.json(), dict) else res.json()
        self.assertEqual(len(results), 1)
        data = results[0]
        self.assertEqual(data['store_name'], 'EchoDesk Demo Store')
        self.assertEqual(data['store_email'], 'shop@demo.ge')
        self.assertEqual(Decimal(data['tax_rate']), Decimal('18.00'))
        self.assertTrue(data['allow_pickup'])
        self.assertFalse(data['has_bog_credentials'])

        # Editing from the phone works: turning card payments on passes the
        # serializer's "return URLs required" check thanks to the seeded URLs.
        res = self._api('patch', f'{SETTINGS_URL}{data["id"]}/',
                        {'store_name': 'Demo Shop', 'enable_card_payment': True})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self._settings().store_name, 'Demo Shop')

    def test_older_demo_tenant_with_blank_settings_is_topped_up(self):
        _seed()
        # What a demo tenant seeded before this module looks like: the
        # ecommerce module created a bare row with model defaults.
        from ecommerce_crm.models import EcommerceSettings
        tenant = Tenant.objects.get(schema_name='demo')
        with schema_context('demo'):
            EcommerceSettings.objects.filter(tenant=tenant).delete()
            EcommerceSettings.objects.create(tenant=tenant)

        out = _seed()
        self.assertIn('ecommerce: already seeded', out)
        self.assertIn('store_settings: seeded', out)
        settings_row = self._settings()
        self.assertEqual(settings_row.store_name, 'EchoDesk Demo Store')
        self.assertEqual(settings_row.tax_rate, Decimal('18.00'))
