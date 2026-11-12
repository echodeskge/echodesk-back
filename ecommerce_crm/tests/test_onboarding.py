"""Shop onboarding step: admin gate, apply writes EcommerceSettings /
ShippingMethod / categories / homepage sections, idempotent, deploy guard,
context/prefill, seed_defaults."""
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings

from ecommerce_crm.models import EcommerceSettings, HomepageSection, ShippingMethod
from onboarding.models import TenantOnboarding
from onboarding.tests.helpers import enable_features
from tickets.models import ItemList
from users.tests.conftest import EchoDeskTenantTestCase

ONB = '/api/onboarding/'
APPLY = f'{ONB}ecommerce_crm/apply/'


def answers(**overrides):
    base = {
        'store': {'name': 'Acme Shop', 'email': 'shop@acme.ge', 'phone': '+995 555 00 00 00'},
        'categories': [{'ka': 'ელექტრონიკა', 'en': 'Electronics'}, {'ka': '', 'en': 'Sale'}, {'ka': 'ელექტრონიკა', 'en': 'Electronics'}],
        'payment': {
            'cash_on_delivery': True, 'card': True, 'provider': 'bog',
            'bog_client_id': 'bog-client', 'bog_client_secret': 'bog-secret-value',
        },
        'delivery': {
            'allow_pickup': True, 'pickup_address': 'Tbilisi, Chavchavadze 10',
            'methods': [
                {'name': {'ka': 'კურიერი', 'en': 'Courier'}, 'price': '5.00', 'free_shipping_threshold': '100.00', 'estimated_days': 2},
                {'name': {'ka': 'რეგიონები', 'en': 'Regions'}, 'price': '10.00', 'free_shipping_threshold': None, 'estimated_days': 4},
            ],
            'quickshipper': {'enabled': True, 'api_key': 'qs-key', 'use_production': False,
                             'pickup': {'contact_name': 'Nino', 'phone': '+995555', 'address': 'Chavchavadze 10',
                                        'city': 'Tbilisi', 'latitude': '41.709', 'longitude': '44.780', 'extra_instructions': ''}},
        },
        'tax': {'rate': '18.00', 'inclusive': True, 'label': 'დღგ'},
        'look': {'template': 'voltage', 'preset': 'cobalt', 'color_mode': 'dark', 'primary_color': None},
        'analytics': {'google_analytics_id': 'G-123', 'google_ads_conversion_id': 'AW-1', 'google_ads_purchase_label': 'buy', 'clarity_project_id': 'clar'},
        'deploy_now': False,
    }
    base.update(overrides)
    return base


class ShopStepTests(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        enable_features(self.tenant, 'ecommerce_crm')
        self.owner = self.create_user(email='owner@test.com', role='admin', is_staff=True, is_superuser=True)
        self.agent = self.create_user(email='agent@test.com', role='agent')

    def _apply(self, data, user=None):
        return self.api_post(APPLY, {'answers': data}, user=user or self.owner)

    def test_non_admin_gets_403(self):
        self.assertEqual(self._apply(answers(), user=self.agent).status_code, 403)

    def test_apply_writes_everything(self):
        resp = self._apply(answers())
        self.assertEqual(resp.status_code, 200, resp.data)

        s = EcommerceSettings.objects.get(tenant=self.tenant)
        self.assertEqual(s.store_name, 'Acme Shop')
        self.assertEqual(s.store_email, 'shop@acme.ge')
        # Payment: provider list + legacy flags the way the settings page computes them.
        self.assertEqual(s.active_payment_providers, ['cash', 'bog'])
        self.assertTrue(s.enable_card_payment)
        self.assertTrue(s.enable_cash_on_delivery)
        self.assertEqual(s.ecommerce_payment_provider, 'bog')
        self.assertEqual(s.bog_client_id, 'bog-client')
        self.assertEqual(s.get_bog_secret(), 'bog-secret-value')
        # Delivery
        self.assertTrue(s.allow_pickup)
        self.assertEqual(s.quickshipper_pickup_address, 'Chavchavadze 10')   # explicit pickup wins over pickup_address
        self.assertTrue(s.quickshipper_enabled)
        self.assertEqual(s.get_quickshipper_api_key(), 'qs-key')
        self.assertEqual(s.quickshipper_pickup_city, 'Tbilisi')
        self.assertEqual(s.quickshipper_pickup_latitude, Decimal('41.709'))
        methods = list(ShippingMethod.objects.order_by('position'))
        self.assertEqual([m.name['en'] for m in methods], ['Courier', 'Regions'])
        self.assertEqual(methods[0].price, Decimal('5.00'))
        self.assertEqual(methods[0].free_shipping_threshold, Decimal('100.00'))
        self.assertIsNone(methods[1].free_shipping_threshold)
        # Tax / look / analytics
        self.assertEqual(s.tax_rate, Decimal('18.00'))
        self.assertTrue(s.tax_inclusive)
        self.assertEqual(s.tax_label, 'დღგ')
        self.assertEqual(s.storefront_template, 'voltage')
        self.assertEqual(s.voltage_theme_preset, 'cobalt')
        self.assertEqual(s.voltage_color_mode, 'dark')
        self.assertEqual(s.google_analytics_id, 'G-123')
        self.assertEqual(s.clarity_project_id, 'clar')
        # Categories → ItemList + ListItems (duplicates collapsed) + hero / category grid.
        categories = ItemList.objects.get(title='Categories')
        self.assertTrue(categories.is_public)
        self.assertEqual(sorted(categories.items.values_list('label', flat=True)), ['Electronics', 'Sale'])
        item = categories.items.get(label='Electronics')
        self.assertEqual(item.custom_data['name'], {'ka': 'ელექტრონიკა', 'en': 'Electronics'})
        sections = list(HomepageSection.objects.order_by('position'))
        self.assertEqual([x.section_type for x in sections], ['hero_banner', 'category_grid'])
        self.assertEqual(sections[1].item_list, categories)
        self.assertEqual(s.homepage_variant, 'classic')

        created = resp.data['result']['created']
        self.assertEqual(created['shipping_methods'], 2)
        self.assertEqual(created['categories'], 2)
        self.assertEqual(created['homepage_sections'], 2)
        self.assertEqual(created['itemlists'], 1)
        self.assertEqual(TenantOnboarding.get().status_of('ecommerce_crm'), 'done')

    def test_apply_twice_creates_nothing_twice(self):
        self.assertEqual(self._apply(answers()).status_code, 200)
        resp = self._apply(answers())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(EcommerceSettings.objects.count(), 1)
        self.assertEqual(ShippingMethod.objects.count(), 2)
        self.assertEqual(ItemList.objects.count(), 1)
        self.assertEqual(ItemList.objects.get().items.count(), 2)
        self.assertEqual(HomepageSection.objects.count(), 2)
        self.assertEqual(resp.data['result']['created'], {})
        self.assertEqual(
            [f for f in resp.data['result']['updated'] if f not in ('bog_client_secret', 'quickshipper_api_key')], [],
        )

    def test_card_off_and_classic_look(self):
        resp = self._apply({
            'payment': {'cash_on_delivery': True, 'card': False},
            'look': {'template': 'classic', 'preset': 'rounded', 'primary_color': '#112233'},
        })
        self.assertEqual(resp.status_code, 200, resp.data)
        s = EcommerceSettings.objects.get(tenant=self.tenant)
        self.assertEqual(s.active_payment_providers, ['cash'])
        self.assertFalse(s.enable_card_payment)
        self.assertEqual(s.theme_preset, 'rounded')
        self.assertEqual(s.theme_primary_color, '#112233')
        self.assertEqual(s.storefront_template, 'classic')

    def test_card_without_provider_is_rejected(self):
        resp = self._apply({'payment': {'card': True}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('provider', resp.data['errors']['payment'])

    def test_existing_shipping_method_matched_by_either_language(self):
        ShippingMethod.objects.create(name={'en': 'courier', 'ka': ''}, price=Decimal('3.00'))
        resp = self._apply({'delivery': {'methods': [{'name': {'ka': 'კურიერი', 'en': 'Courier'}, 'price': '5.00'}]}})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(ShippingMethod.objects.count(), 1)
        self.assertEqual(ShippingMethod.objects.get().price, Decimal('5.00'))

    def test_categories_do_not_add_sections_when_some_exist(self):
        HomepageSection.objects.create(title={'en': 'Hi'}, section_type='featured_products', position=0)
        resp = self._apply({'categories': [{'ka': 'ახალი', 'en': 'New'}]})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(HomepageSection.objects.count(), 1)

    @override_settings(VERCEL_TOKEN='', VERCEL_SHARED_ECOMMERCE_PROJECT_ID='')
    def test_deploy_now_skipped_when_vercel_not_configured(self):
        with patch('ecommerce_crm.services.vercel_deployment.deploy_tenant_frontend') as deploy:
            resp = self._apply({'deploy_now': True})
        self.assertEqual(resp.status_code, 200, resp.data)
        deploy.assert_not_called()
        self.assertTrue(any('Vercel is not configured' in n for n in resp.data['result']['notes']))
        self.assertEqual(EcommerceSettings.objects.get(tenant=self.tenant).deployment_status, 'pending')

    @override_settings(VERCEL_TOKEN='t', VERCEL_SHARED_ECOMMERCE_PROJECT_ID='prj_1')
    def test_deploy_now_calls_the_deploy_service_once(self):
        outcome = {'success': True, 'url': 'https://test.ecommerce.echodesk.ge', 'project_id': 'prj_1'}
        with patch('ecommerce_crm.services.vercel_deployment.deploy_tenant_frontend', return_value=outcome) as deploy:
            resp = self._apply({'deploy_now': True})
            self.assertEqual(resp.status_code, 200, resp.data)
            self.assertEqual(deploy.call_count, 1)
            s = EcommerceSettings.objects.get(tenant=self.tenant)
            self.assertEqual(s.deployment_status, 'deployed')
            self.assertEqual(s.ecommerce_frontend_url, outcome['url'])
            self.assertEqual(resp.data['result']['created']['deployment'], 1)
            # Already deployed → the service is not called again.
            resp = self._apply({'deploy_now': True})
            self.assertEqual(deploy.call_count, 1)
            self.assertTrue(any('already deployed' in n for n in resp.data['result']['notes']))

    def test_context_and_prefill(self):
        state = TenantOnboarding.get()
        state.industry = 'salon'
        state.save()
        resp = self.api_get(ONB, user=self.owner)
        step = next(s for s in resp.data['steps'] if s['key'] == 'ecommerce_crm')
        self.assertEqual(step['order'], 40)
        ctx = step['context']
        self.assertEqual(ctx['industry'], 'salon')
        self.assertEqual(ctx['deployment'], {'status': 'pending', 'url': None})
        self.assertEqual(ctx['languages'], ['ka', 'en'])
        self.assertEqual(ctx['existing'], {'products': 0, 'shipping_methods': 0, 'categories': 0, 'homepage_sections': 0})
        self.assertIn('salon', ctx['presets'])
        self.assertTrue(all(p['ka'] and p['en'] for p in ctx['presets']['salon']))
        pre = step['prefill']
        self.assertEqual(pre['store']['name'], self.tenant.name)
        self.assertEqual(pre['categories'], ctx['presets']['salon'])
        self.assertEqual(pre['tax']['rate'], '18.00')
        self.assertTrue(pre['deploy_now'])

        self._apply(answers())
        step = next(s for s in self.api_get(ONB, user=self.owner).data['steps'] if s['key'] == 'ecommerce_crm')
        self.assertEqual(step['context']['existing']['shipping_methods'], 2)
        self.assertEqual(step['context']['existing']['categories'], 2)
        self.assertEqual(step['prefill']['payment']['provider'], 'bog')
        self.assertTrue(step['prefill']['payment']['card'])
        self.assertEqual(step['prefill']['delivery']['methods'][0]['name']['en'], 'Courier')
        self.assertEqual(step['prefill']['look']['preset'], 'cobalt')
        self.assertIsNone(step['prefill']['delivery']['quickshipper']['api_key'])   # secrets never come back

    def test_seed_defaults_creates_the_settings_row(self):
        from ecommerce_crm.onboarding import ShopStep
        ShopStep().seed_defaults(self.tenant)
        ShopStep().seed_defaults(self.tenant)
        s = EcommerceSettings.objects.get(tenant=self.tenant)
        self.assertEqual(s.store_name, self.tenant.name)
        self.assertEqual(s.store_email, self.tenant.admin_email)
