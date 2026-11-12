"""Invoices onboarding step: admin gate, apply writes InvoiceSettings and the
Clients list, idempotent, context/prefill, seed_defaults."""
from decimal import Decimal

from invoices.models import InvoiceSettings
from onboarding.models import TenantOnboarding
from onboarding.tests.helpers import enable_features
from tickets.models import ItemList
from users.tests.conftest import EchoDeskTenantTestCase

ONB = '/api/onboarding/'
APPLY = f'{ONB}invoice_management/apply/'


def answers(**overrides):
    base = {
        'company': {
            'name': 'Acme LLC', 'tax_id': '123456789', 'registration_number': 'REG-1',
            'address': 'Tbilisi, Rustaveli 1', 'phone': '+995 32 2 00 00 00',
            'email': 'billing@acme.ge', 'website': 'acme.ge',
        },
        'defaults': {
            'vat_payer': True, 'default_tax_rate': '18.00', 'default_currency': 'GEL',
            'default_due_days': 14, 'invoice_prefix': 'acm', 'starting_number': 100,
        },
        'bank_accounts': [
            {'bank_name': 'Bank of Georgia', 'account_number': 'GE00BG0000000000000000',
             'iban': 'GE00BG0000000000000000', 'swift': 'BAGAGE22', 'is_default': False},
            {'bank_name': 'TBC', 'account_number': 'GE00TB0000000000000000', 'iban': '', 'swift': '', 'is_default': False},
        ],
        'email': {
            'email_from': 'billing@acme.ge', 'email_from_name': 'Acme billing', 'email_cc': 'cfo@acme.ge',
            'email_subject_template': 'Invoice {invoice_number}', 'email_message_template': 'Dear {client_name}, {total_amount} due in {due_days} days',
            'footer_text': 'Thank you', 'default_terms': 'Net 14',
        },
        'clients_source': 'itemlist',
    }
    base.update(overrides)
    return base


class InvoicesStepTests(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        enable_features(self.tenant, 'invoice_management')
        self.owner = self.create_user(email='owner@test.com', role='admin', is_staff=True, is_superuser=True)
        self.agent = self.create_user(email='agent@test.com', role='agent')

    def _apply(self, data, user=None):
        return self.api_post(APPLY, {'answers': data}, user=user or self.owner)

    def test_non_admin_gets_403(self):
        self.assertEqual(self._apply(answers(), user=self.agent).status_code, 403)
        self.assertEqual(self.api_get(ONB, user=self.agent).status_code, 403)

    def test_apply_writes_settings_and_creates_clients_list(self):
        resp = self._apply(answers())
        self.assertEqual(resp.status_code, 200, resp.data)

        s = InvoiceSettings.objects.get()
        self.assertEqual(s.company_name, 'Acme LLC')
        self.assertEqual(s.tax_id, '123456789')
        self.assertEqual(s.website, 'https://acme.ge')
        self.assertEqual(s.default_tax_rate, Decimal('18.00'))
        self.assertEqual(s.default_currency, 'GEL')
        self.assertEqual(s.default_due_days, 14)
        self.assertEqual(s.invoice_prefix, 'ACM')
        self.assertEqual(s.starting_number, 100)
        self.assertEqual(len(s.bank_accounts), 2)
        self.assertTrue(s.bank_accounts[0]['is_default'])      # first becomes default
        self.assertFalse(s.bank_accounts[1]['is_default'])
        self.assertEqual(s.email_from_name, 'Acme billing')
        # The web's placeholder names are rewritten to what the mailer substitutes.
        self.assertEqual(s.email_message_template, 'Dear {client_name}, {total} due in 14 days')
        self.assertEqual(s.default_terms, 'Net 14')

        clients = ItemList.objects.get(title='Clients')          # tenant language is en by default
        self.assertEqual(s.client_itemlist, clients)
        self.assertEqual(clients.created_by, self.owner)
        self.assertEqual(resp.data['result']['created']['itemlists'], 1)
        self.assertEqual(TenantOnboarding.get().status_of('invoice_management'), 'done')
        self.assertIn('client_itemlist', resp.data['result']['updated'])

    def test_apply_twice_creates_nothing_twice(self):
        self.assertEqual(self._apply(answers()).status_code, 200)
        resp = self._apply(answers())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(InvoiceSettings.objects.count(), 1)
        self.assertEqual(ItemList.objects.filter(title__in=['Clients', 'კლიენტები']).count(), 1)
        self.assertEqual(resp.data['result']['created'], {})
        self.assertEqual(resp.data['result']['updated'], [])

    def test_non_vat_payer_gets_zero_rate_and_ecommerce_source_leaves_list_empty(self):
        resp = self._apply(answers(
            defaults={'vat_payer': False, 'default_tax_rate': '18.00'}, clients_source='ecommerce',
        ))
        self.assertEqual(resp.status_code, 200, resp.data)
        s = InvoiceSettings.objects.get()
        self.assertEqual(s.default_tax_rate, Decimal('0.00'))
        self.assertIsNone(s.client_itemlist)
        self.assertFalse(ItemList.objects.exists())
        self.assertTrue(any('online shop' in n for n in resp.data['result']['notes']))

    def test_vat_payer_without_rate_gets_18(self):
        self.assertEqual(self._apply({'defaults': {'vat_payer': True}}).status_code, 200)
        self.assertEqual(InvoiceSettings.objects.get().default_tax_rate, Decimal('18.00'))

    def test_bank_account_validation_like_the_settings_serializer(self):
        resp = self._apply({'bank_accounts': [{'bank_name': 'BOG'}]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('bank_accounts', resp.data['errors'])

    def test_existing_clients_list_is_reused(self):
        existing = ItemList.objects.create(title='კლიენტები', created_by=self.owner)
        resp = self._apply({'clients_source': 'itemlist'})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(InvoiceSettings.objects.get().client_itemlist, existing)
        self.assertEqual(ItemList.objects.count(), 1)

    def test_context_and_prefill(self):
        resp = self.api_get(ONB, user=self.owner)
        self.assertEqual(resp.status_code, 200)
        step = next(s for s in resp.data['steps'] if s['key'] == 'invoice_management')
        self.assertEqual(step['order'], 50)
        ctx = step['context']
        self.assertEqual(ctx['prefill_sources']['tenant_name'], self.tenant.name)
        self.assertIsNone(ctx['prefill_sources']['booking_bank'])
        self.assertFalse(ctx['has_logo'])
        self.assertRegex(ctx['next_number_preview'], r'^INV-\d{4}-0001$')
        self.assertEqual(ctx['itemlists'], [])
        pre = step['prefill']
        self.assertEqual(pre['company']['name'], self.tenant.name)
        self.assertEqual(pre['defaults']['invoice_prefix'], 'INV')
        self.assertEqual(pre['bank_accounts'], [])
        self.assertEqual(ctx['language'], 'en')
        self.assertIn('{total}', ctx['email_placeholders'])
        self.assertEqual(pre['email']['email_subject_template'], 'Invoice {invoice_number} from {company_name}')
        self.assertIn('{client_name}', pre['email']['email_message_template'])
        self.assertIn('30 days', pre['email']['default_terms'])
        self.assertEqual(pre['email']['email_from_name'], self.tenant.name)

        # After an apply the preview follows the stored prefix / number and the
        # prefill reflects the stored settings.
        self._apply(answers())
        step = next(s for s in self.api_get(ONB, user=self.owner).data['steps'] if s['key'] == 'invoice_management')
        self.assertRegex(step['context']['next_number_preview'], r'^ACM-\d{4}-0100$')
        self.assertEqual(step['prefill']['clients_source'], 'itemlist')
        self.assertEqual(step['prefill']['defaults']['vat_payer'], True)
        self.assertEqual([i['title'] for i in step['context']['itemlists']], ['Clients'])

    def test_prefill_uses_booking_bank_fields(self):
        from booking_management.models import BookingSettings
        BookingSettings.objects.create(tenant=self.tenant, bank_name='TBC', bank_iban='GE00TB1', bank_account_holder='Acme')
        step = next(s for s in self.api_get(ONB, user=self.owner).data['steps'] if s['key'] == 'invoice_management')
        self.assertEqual(step['context']['prefill_sources']['booking_bank']['bank_name'], 'TBC')
        self.assertEqual(step['prefill']['bank_accounts'][0]['account_number'], 'GE00TB1')
        self.assertTrue(step['prefill']['bank_accounts'][0]['is_default'])

    def test_prefill_templates_follow_the_tenant_language(self):
        self.tenant.preferred_language = 'ka'
        self.tenant.save(update_fields=['preferred_language'])
        step = next(s for s in self.api_get(ONB, user=self.owner).data['steps'] if s['key'] == 'invoice_management')
        self.assertEqual(step['context']['language'], 'ka')
        self.assertTrue(step['prefill']['email']['email_subject_template'].startswith('ინვოისი'))
        self.assertIn('{due_date}', step['prefill']['email']['email_message_template'])
        self.assertNotIn('{due_days}', step['prefill']['email']['default_terms'])

    def test_seed_defaults_creates_the_row_with_the_tenant_name(self):
        from invoices.onboarding import InvoicesStep
        InvoicesStep().seed_defaults(self.tenant)
        InvoicesStep().seed_defaults(self.tenant)
        self.assertEqual(InvoiceSettings.objects.count(), 1)
        self.assertEqual(InvoiceSettings.objects.get().company_name, self.tenant.name)
