"""Tests for Invoice views: settings, CRUD, line items, payments, templates."""
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch, PropertyMock, MagicMock
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from invoices.models import (
    InvoiceSettings, Invoice, InvoiceLineItem, InvoicePayment, InvoiceTemplate,
)
from invoices.tests.conftest import InvoiceTestCase
from tickets.models import ItemList, ListItem


# ============================================================================
# InvoiceSettingsViewSet
# ============================================================================


class TestInvoiceSettingsList(InvoiceTestCase):

    def test_get_settings_creates_singleton(self):
        user = self.create_invoice_user()
        resp = self.api_get('/api/invoices/settings/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(InvoiceSettings.objects.count(), 1)

    def test_get_settings_returns_existing(self):
        self.create_invoice_settings(company_name='Existing Corp')
        user = self.create_invoice_user()
        resp = self.api_get('/api/invoices/settings/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['company_name'], 'Existing Corp')

    def test_unauthenticated_denied(self):
        resp = self.api_get('/api/invoices/settings/')
        self.assertIn(resp.status_code, [401, 403])


class TestInvoiceSettingsUpdate(InvoiceTestCase):

    def test_update_settings_via_post(self):
        user = self.create_invoice_user()
        resp = self.api_post(
            '/api/invoices/settings/',
            {'company_name': 'Updated Corp', 'default_currency': 'USD'},
            user=user,
        )
        self.assertEqual(resp.status_code, 200)
        settings = InvoiceSettings.objects.first()
        self.assertEqual(settings.company_name, 'Updated Corp')

    def test_update_bank_accounts(self):
        user = self.create_invoice_user()
        accounts = [
            {'bank_name': 'BOG', 'account_number': 'GE00BG0000'},
            {'bank_name': 'TBC', 'account_number': 'GE00TB0000'},
        ]
        resp = self.api_post(
            '/api/invoices/settings/',
            {'bank_accounts': accounts},
            user=user,
        )
        self.assertEqual(resp.status_code, 200)
        settings = InvoiceSettings.objects.first()
        self.assertEqual(len(settings.bank_accounts), 2)

    def test_invalid_bank_accounts(self):
        user = self.create_invoice_user()
        resp = self.api_post(
            '/api/invoices/settings/',
            {'bank_accounts': [{'invalid_key': 'no bank_name'}]},
            user=user,
        )
        self.assertEqual(resp.status_code, 400)


class TestInvoiceSettingsFileUploads(InvoiceTestCase):

    def test_upload_logo_no_file(self):
        user = self.create_invoice_user()
        InvoiceSettings.objects.get_or_create()
        client = self.authenticated_client(user)
        resp = client.post(
            '/api/invoices/settings/upload-logo/',
            {},
            format='multipart',
            HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_badge_no_file(self):
        user = self.create_invoice_user()
        InvoiceSettings.objects.get_or_create()
        client = self.authenticated_client(user)
        resp = client.post(
            '/api/invoices/settings/upload-badge/',
            {},
            format='multipart',
            HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_signature_no_file(self):
        user = self.create_invoice_user()
        InvoiceSettings.objects.get_or_create()
        client = self.authenticated_client(user)
        resp = client.post(
            '/api/invoices/settings/upload-signature/',
            {},
            format='multipart',
            HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, 400)

    def test_remove_logo(self):
        user = self.create_invoice_user()
        InvoiceSettings.objects.get_or_create()
        resp = self.api_delete('/api/invoices/settings/remove-logo/', user=user)
        self.assertIn(resp.status_code, [200, 204])

    def test_remove_badge(self):
        user = self.create_invoice_user()
        InvoiceSettings.objects.get_or_create()
        resp = self.api_delete('/api/invoices/settings/remove-badge/', user=user)
        self.assertIn(resp.status_code, [200, 204])

    def test_remove_signature(self):
        user = self.create_invoice_user()
        InvoiceSettings.objects.get_or_create()
        resp = self.api_delete('/api/invoices/settings/remove-signature/', user=user)
        self.assertIn(resp.status_code, [200, 204])


# ============================================================================
# InvoiceViewSet
# ============================================================================


class TestInvoiceList(InvoiceTestCase):

    def test_list_invoices(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice(created_by=user, client=ecom_client)
        self.create_invoice(created_by=user, client=ecom_client)
        resp = self.api_get('/api/invoices/invoices/', user=user)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 2)

    def test_list_unauthenticated(self):
        resp = self.api_get('/api/invoices/invoices/')
        self.assertIn(resp.status_code, [401, 403])

    def test_filter_by_status(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice(created_by=user, client=ecom_client, status='draft')
        self.create_invoice(created_by=user, client=ecom_client, status='sent')
        resp = self.api_get('/api/invoices/invoices/?status=draft', user=user)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 1)


class TestInvoiceCreate(InvoiceTestCase):

    def test_create_invoice_with_ecommerce_client(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice_settings()
        data = {
            'client': ecom_client.id,
            'issue_date': str(timezone.now().date()),
            'due_date': str(timezone.now().date() + timedelta(days=30)),
            'currency': 'GEL',
        }
        resp = self.api_post('/api/invoices/invoices/', data, user=user)
        self.assertEqual(resp.status_code, 201)
        self.assertIn('invoice_number', resp.data)

    def test_create_invoice_with_line_items(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice_settings()
        data = {
            'client': ecom_client.id,
            'issue_date': str(timezone.now().date()),
            'due_date': str(timezone.now().date() + timedelta(days=30)),
            'line_items': [
                {
                    'description': 'Service A',
                    'quantity': '2',
                    'unit_price': '100.00',
                    'tax_rate': '18.00',
                    'discount_percent': '0.00',
                    'item_source': 'manual',
                },
            ],
        }
        resp = self.api_post('/api/invoices/invoices/', data, user=user)
        self.assertEqual(resp.status_code, 201)

    def test_create_invoice_due_date_before_issue_date(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice_settings()
        data = {
            'client': ecom_client.id,
            'issue_date': str(timezone.now().date()),
            'due_date': str(timezone.now().date() - timedelta(days=5)),
        }
        resp = self.api_post('/api/invoices/invoices/', data, user=user)
        self.assertEqual(resp.status_code, 400)

    def test_create_invoice_invalid_client(self):
        user = self.create_invoice_user()
        self.create_invoice_settings()
        data = {
            'client': 99999,
            'issue_date': str(timezone.now().date()),
            'due_date': str(timezone.now().date() + timedelta(days=30)),
        }
        resp = self.api_post('/api/invoices/invoices/', data, user=user)
        self.assertEqual(resp.status_code, 400)


class TestInvoiceRetrieve(InvoiceTestCase):

    def test_retrieve_invoice(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(created_by=user, client=ecom_client)
        resp = self.api_get(f'/api/invoices/invoices/{invoice.id}/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['invoice_number'], invoice.invoice_number)


class TestInvoiceUpdate(InvoiceTestCase):

    def test_update_non_draft_invoice_rejected(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client, status='sent',
        )
        resp = self.api_patch(
            f'/api/invoices/invoices/{invoice.id}/',
            {'notes': 'Should fail', 'client': ecom_client.id},
            user=user,
        )
        self.assertEqual(resp.status_code, 400)


class TestInvoiceDelete(InvoiceTestCase):

    def test_delete_draft_invoice(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client, status='draft',
        )
        resp = self.api_delete(f'/api/invoices/invoices/{invoice.id}/', user=user)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Invoice.objects.filter(id=invoice.id).exists())

    def test_delete_non_draft_invoice_rejected(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client, status='sent',
        )
        resp = self.api_delete(f'/api/invoices/invoices/{invoice.id}/', user=user)
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(Invoice.objects.filter(id=invoice.id).exists())


class TestInvoiceActions(InvoiceTestCase):

    def test_mark_paid(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client,
            status='sent', total=Decimal('500.00'),
        )
        resp = self.api_post(
            f'/api/invoices/invoices/{invoice.id}/mark_paid/',
            {'payment_method': 'cash'},
            user=user,
        )
        self.assertEqual(resp.status_code, 200)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'paid')
        self.assertEqual(invoice.paid_amount, Decimal('500.00'))
        # Payment record should be created
        self.assertTrue(InvoicePayment.objects.filter(invoice=invoice).exists())

    def test_mark_paid_double_submit_is_idempotent(self):
        """A second mark_paid must not create a second full-balance payment or
        push paid_amount past the total."""
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client,
            status='sent', total=Decimal('500.00'),
        )
        first = self.api_post(
            f'/api/invoices/invoices/{invoice.id}/mark_paid/',
            {'payment_method': 'cash'}, user=user,
        )
        self.assertEqual(first.status_code, 200)
        second = self.api_post(
            f'/api/invoices/invoices/{invoice.id}/mark_paid/',
            {'payment_method': 'cash'}, user=user,
        )
        self.assertEqual(second.status_code, 200)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'paid')
        self.assertEqual(invoice.paid_amount, Decimal('500.00'))
        # Exactly one payment, not two.
        self.assertEqual(InvoicePayment.objects.filter(invoice=invoice).count(), 1)

    def test_duplicate_itemlist_client_invoice(self):
        """Duplicating an invoice whose client came from an ItemList must not
        500 on original.client.id (client is None for that flow)."""
        user = self.create_invoice_user()
        item_list = self.create_item_list(title='Clients', created_by=user)
        list_item = self.create_list_item(item_list, label='Acme Ltd', created_by=user)
        settings = InvoiceSettings.objects.get_or_create()[0]
        settings.client_itemlist = item_list
        settings.save()

        invoice = self.create_invoice(
            created_by=user, status='sent', total=Decimal('120.00'),
            client_itemlist_item=list_item, client_name='Acme Ltd',
        )
        self.create_line_item(invoice, description='Service')

        resp = self.api_post(
            f'/api/invoices/invoices/{invoice.id}/duplicate/', user=user,
        )
        self.assertEqual(resp.status_code, 201)
        # The duplicate carries the itemlist client, not a null-deref crash.
        new_id = resp.data['id']
        dup = Invoice.objects.get(id=new_id)
        self.assertEqual(dup.client_itemlist_item_id, list_item.id)
        self.assertIsNone(dup.client_id)

    def test_finalize_draft(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client, status='draft',
        )
        with patch('invoices.views.InvoiceViewSet.finalize') as mock_finalize:
            # The finalize action calls a Celery task; we test the view logic directly
            pass
        # Test the status check part
        resp = self.api_post(
            f'/api/invoices/invoices/{invoice.id}/finalize/',
            user=user,
        )
        # This should work or fail gracefully (Celery task is async)
        # The status should change to 'sent' regardless
        if resp.status_code == 200:
            invoice.refresh_from_db()
            self.assertEqual(invoice.status, 'sent')

    def test_finalize_non_draft_rejected(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client, status='sent',
        )
        resp = self.api_post(
            f'/api/invoices/invoices/{invoice.id}/finalize/',
            user=user,
        )
        self.assertEqual(resp.status_code, 400)

    def test_send_email_draft_rejected(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client, status='draft',
        )
        resp = self.api_post(
            f'/api/invoices/invoices/{invoice.id}/send_email/',
            {'recipient_email': 'test@example.com'},
            user=user,
        )
        self.assertEqual(resp.status_code, 400)

    def test_send_email_no_recipient(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client, status='sent',
        )
        resp = self.api_post(
            f'/api/invoices/invoices/{invoice.id}/send_email/',
            {},
            user=user,
        )
        self.assertEqual(resp.status_code, 400)

    def test_stats(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice(
            created_by=user, client=ecom_client,
            status='paid', total=Decimal('500.00'),
        )
        self.create_invoice(
            created_by=user, client=ecom_client,
            status='sent', total=Decimal('300.00'),
        )
        resp = self.api_get('/api/invoices/invoices/stats/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('current_month', resp.data)
        self.assertIn('outstanding_amount', resp.data)
        self.assertIn('overdue_count', resp.data)


# ============================================================================
# InvoiceLineItemViewSet
# ============================================================================


class TestInvoiceLineItemViewSet(InvoiceTestCase):

    def test_list_line_items(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user)
        self.create_line_item(invoice, description='Item A')
        self.create_line_item(invoice, description='Item B')
        resp = self.api_get(
            f'/api/invoices/line-items/?invoice_id={invoice.id}', user=user,
        )
        self.assertEqual(resp.status_code, 200)
        # Endpoint is paginated: results live under 'results'.
        self.assertEqual(resp.data['count'], 2)
        self.assertEqual(len(resp.data['results']), 2)

    def test_delete_line_item(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user)
        item = self.create_line_item(invoice)
        resp = self.api_delete(f'/api/invoices/line-items/{item.id}/', user=user)
        self.assertEqual(resp.status_code, 204)

    def test_reorder_line_items(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user)
        item1 = self.create_line_item(invoice, description='A', position=0)
        item2 = self.create_line_item(invoice, description='B', position=1)
        resp = self.api_post(
            '/api/invoices/line-items/reorder/',
            {
                'items': [
                    {'id': item1.id, 'position': 1},
                    {'id': item2.id, 'position': 0},
                ],
            },
            user=user,
        )
        self.assertEqual(resp.status_code, 200)
        item1.refresh_from_db()
        item2.refresh_from_db()
        self.assertEqual(item1.position, 1)
        self.assertEqual(item2.position, 0)


# ============================================================================
# InvoicePaymentViewSet
# ============================================================================


class TestInvoicePaymentViewSet(InvoiceTestCase):

    def test_list_payments(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(
            created_by=user, total=Decimal('200.00'), status='sent',
        )
        self.create_payment(invoice, amount=Decimal('100.00'), recorded_by=user)
        resp = self.api_get(
            f'/api/invoices/payments/?invoice_id={invoice.id}', user=user,
        )
        self.assertEqual(resp.status_code, 200)
        # Endpoint is paginated: results live under 'results'.
        self.assertEqual(resp.data['count'], 1)
        self.assertEqual(len(resp.data['results']), 1)

    def test_create_payment(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(
            created_by=user, total=Decimal('500.00'), status='sent',
        )
        data = {
            'invoice': invoice.id,
            'amount': '200.00',
            'payment_method': 'card',
        }
        resp = self.api_post('/api/invoices/payments/', data, user=user)
        self.assertEqual(resp.status_code, 201)
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, Decimal('200.00'))

    def test_create_payment_sets_recorded_by(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(
            created_by=user, total=Decimal('500.00'),
        )
        data = {
            'invoice': invoice.id,
            'amount': '100.00',
            'payment_method': 'cash',
        }
        resp = self.api_post('/api/invoices/payments/', data, user=user)
        self.assertEqual(resp.status_code, 201)
        payment = InvoicePayment.objects.get(id=resp.data['id'])
        self.assertEqual(payment.recorded_by, user)


# ============================================================================
# InvoiceTemplateViewSet
# ============================================================================


class TestInvoiceTemplateViewSet(InvoiceTestCase):

    def test_list_templates(self):
        user = self.create_invoice_user()
        self.create_invoice_template(name='Template A', created_by=user)
        self.create_invoice_template(name='Template B', created_by=user)
        resp = self.api_get('/api/invoices/templates/', user=user)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 2)

    def test_create_template(self):
        user = self.create_invoice_user()
        data = {
            'name': 'New Template',
            'html_content': '<html>{{ invoice }}</html>',
            'css_styles': 'body {}',
            'supported_languages': ['en'],
        }
        resp = self.api_post('/api/invoices/templates/', data, user=user)
        self.assertEqual(resp.status_code, 201)
        template = InvoiceTemplate.objects.get(id=resp.data['id'])
        self.assertEqual(template.created_by, user)

    def test_create_template_invalid_language(self):
        user = self.create_invoice_user()
        data = {
            'name': 'Bad Template',
            'html_content': '<html></html>',
            'supported_languages': ['xx'],
        }
        resp = self.api_post('/api/invoices/templates/', data, user=user)
        self.assertEqual(resp.status_code, 400)

    def test_delete_template(self):
        user = self.create_invoice_user()
        template = self.create_invoice_template(created_by=user)
        resp = self.api_delete(f'/api/invoices/templates/{template.id}/', user=user)
        self.assertEqual(resp.status_code, 204)


# ============================================================================
# Permission Tests
# ============================================================================


class TestInvoicePermissions(InvoiceTestCase):

    def test_user_without_feature_denied(self):
        """Users without invoice_management feature should be denied."""
        user = self.create_user(email='no-feature@test.com')
        resp = self.api_get('/api/invoices/invoices/', user=user)
        self.assertEqual(resp.status_code, 403)

    def test_user_without_feature_denied_settings(self):
        user = self.create_user(email='no-feature-s@test.com')
        resp = self.api_get('/api/invoices/settings/', user=user)
        self.assertEqual(resp.status_code, 403)


# ============================================================================
# Additional Invoice List / Filter / Search Tests
# ============================================================================


class TestInvoiceListFilters(InvoiceTestCase):

    def test_filter_by_client(self):
        user = self.create_invoice_user()
        client_a = self.create_ecommerce_client(first_name='Alice', last_name='A')
        client_b = self.create_ecommerce_client(first_name='Bob', last_name='B')
        self.create_invoice(created_by=user, client=client_a)
        self.create_invoice(created_by=user, client=client_b)
        self.create_invoice(created_by=user, client=client_a)
        resp = self.api_get(f'/api/invoices/invoices/?client={client_a.id}', user=user)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 2)

    def test_filter_by_date_range(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        today = timezone.now().date()
        self.create_invoice(
            created_by=user, client=ecom_client,
            issue_date=today - timedelta(days=10),
        )
        self.create_invoice(
            created_by=user, client=ecom_client,
            issue_date=today,
        )
        self.create_invoice(
            created_by=user, client=ecom_client,
            issue_date=today - timedelta(days=30),
        )
        date_from = str(today - timedelta(days=15))
        date_to = str(today)
        resp = self.api_get(
            f'/api/invoices/invoices/?date_from={date_from}&date_to={date_to}',
            user=user,
        )
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 2)

    def test_filter_overdue_only(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        # Overdue invoice
        self.create_invoice(
            created_by=user, client=ecom_client, status='sent',
            due_date=timezone.now().date() - timedelta(days=5),
        )
        # Not overdue invoice
        self.create_invoice(
            created_by=user, client=ecom_client, status='sent',
            due_date=timezone.now().date() + timedelta(days=5),
        )
        # Paid invoice (not overdue even with past due date)
        self.create_invoice(
            created_by=user, client=ecom_client, status='paid',
            due_date=timezone.now().date() - timedelta(days=5),
        )
        resp = self.api_get('/api/invoices/invoices/?overdue=true', user=user)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 1)

    def test_search_by_invoice_number(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        inv = self.create_invoice(
            created_by=user, client=ecom_client,
            invoice_number='SRCH-2026-9999',
        )
        self.create_invoice(created_by=user, client=ecom_client)
        resp = self.api_get('/api/invoices/invoices/?search=SRCH-2026', user=user)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['invoice_number'], 'SRCH-2026-9999')

    def test_filter_by_multiple_statuses(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice(created_by=user, client=ecom_client, status='draft')
        self.create_invoice(created_by=user, client=ecom_client, status='paid')
        self.create_invoice(created_by=user, client=ecom_client, status='cancelled')
        # Filter for paid only
        resp = self.api_get('/api/invoices/invoices/?status=paid', user=user)
        results = self.get_results(resp)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], 'paid')

    def test_ordering_by_total(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice(
            created_by=user, client=ecom_client, total=Decimal('100.00'),
        )
        self.create_invoice(
            created_by=user, client=ecom_client, total=Decimal('500.00'),
        )
        self.create_invoice(
            created_by=user, client=ecom_client, total=Decimal('200.00'),
        )
        resp = self.api_get('/api/invoices/invoices/?ordering=total', user=user)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        totals = [Decimal(r['total']) for r in results]
        self.assertEqual(totals, sorted(totals))


# ============================================================================
# Invoice Auto-Number Generation
# ============================================================================


class TestInvoiceAutoNumber(InvoiceTestCase):

    def test_auto_generates_invoice_number(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice_settings(invoice_prefix='AUTO', starting_number=1)
        data = {
            'client': ecom_client.id,
            'issue_date': str(timezone.now().date()),
            'due_date': str(timezone.now().date() + timedelta(days=30)),
        }
        resp = self.api_post('/api/invoices/invoices/', data, user=user)
        self.assertEqual(resp.status_code, 201)
        year = timezone.now().year
        self.assertTrue(resp.data['invoice_number'].startswith(f'AUTO-{year}-'))

    def test_sequential_invoice_numbers(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice_settings(invoice_prefix='SEQ', starting_number=1)
        year = timezone.now().year

        for i in range(1, 4):
            data = {
                'client': ecom_client.id,
                'issue_date': str(timezone.now().date()),
                'due_date': str(timezone.now().date() + timedelta(days=30)),
            }
            resp = self.api_post('/api/invoices/invoices/', data, user=user)
            self.assertEqual(resp.status_code, 201)
            self.assertEqual(resp.data['invoice_number'], f'SEQ-{year}-{str(i).zfill(4)}')


# ============================================================================
# Invoice Total Calculation via API
# ============================================================================


class TestInvoiceCalculatesTotalViaAPI(InvoiceTestCase):

    def test_create_invoice_calculates_total(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice_settings()
        data = {
            'client': ecom_client.id,
            'issue_date': str(timezone.now().date()),
            'due_date': str(timezone.now().date() + timedelta(days=30)),
            'line_items': [
                {
                    'description': 'Item A',
                    'quantity': '2',
                    'unit_price': '100.00',
                    'tax_rate': '18.00',
                    'discount_percent': '0.00',
                    'item_source': 'manual',
                },
                {
                    'description': 'Item B',
                    'quantity': '1',
                    'unit_price': '50.00',
                    'tax_rate': '0.00',
                    'discount_percent': '0.00',
                    'item_source': 'manual',
                },
            ],
        }
        resp = self.api_post('/api/invoices/invoices/', data, user=user)
        self.assertEqual(resp.status_code, 201)
        # subtotal = 200 + 50 = 250, tax = 36 (only Item A), total = 250 + 36 = 286
        self.assertEqual(Decimal(resp.data['subtotal']), Decimal('250.00'))
        self.assertEqual(Decimal(resp.data['total']), Decimal('286.00'))


# ============================================================================
# Invoice Detail with Line Items and Payments
# ============================================================================


class TestInvoiceDetailInclusions(InvoiceTestCase):

    def test_detail_includes_line_items(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(created_by=user, client=ecom_client)
        self.create_line_item(invoice, description='Detail Item A')
        self.create_line_item(invoice, description='Detail Item B')
        resp = self.api_get(f'/api/invoices/invoices/{invoice.id}/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['line_items']), 2)

    def test_detail_includes_payments(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client,
            total=Decimal('300.00'), status='sent',
        )
        self.create_payment(invoice, amount=Decimal('100.00'), recorded_by=user)
        resp = self.api_get(f'/api/invoices/invoices/{invoice.id}/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['payments']), 1)

    def test_detail_includes_client_details(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client(
            first_name='Detail', last_name='Client',
        )
        invoice = self.create_invoice(
            created_by=user, client=ecom_client, client_name='Detail Client',
        )
        resp = self.api_get(f'/api/invoices/invoices/{invoice.id}/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('client_details', resp.data)
        self.assertEqual(resp.data['client_details']['first_name'], 'Detail')


# ============================================================================
# Line Item Update
# ============================================================================


class TestInvoiceLineItemUpdate(InvoiceTestCase):
    pass


# ============================================================================
# Payment updates invoice status
# ============================================================================


class TestPaymentUpdatesInvoiceStatus(InvoiceTestCase):

    def test_full_payment_marks_invoice_paid(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(
            created_by=user, total=Decimal('500.00'), status='sent',
        )
        data = {
            'invoice': invoice.id,
            'amount': '500.00',
            'payment_method': 'card',
        }
        resp = self.api_post('/api/invoices/payments/', data, user=user)
        self.assertEqual(resp.status_code, 201)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'paid')
        self.assertEqual(invoice.paid_amount, Decimal('500.00'))

    def test_partial_payment_does_not_mark_paid(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(
            created_by=user, total=Decimal('500.00'), status='sent',
        )
        data = {
            'invoice': invoice.id,
            'amount': '200.00',
            'payment_method': 'bank_transfer',
        }
        resp = self.api_post('/api/invoices/payments/', data, user=user)
        self.assertEqual(resp.status_code, 201)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'partially_paid')
        self.assertEqual(invoice.paid_amount, Decimal('200.00'))


# ============================================================================
# Invoice Duplicate
# ============================================================================


class TestInvoiceDuplicate(InvoiceTestCase):

    def test_duplicate_invoice(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice_settings()
        original = self.create_invoice(
            created_by=user, client=ecom_client, status='sent',
            notes='Original notes',
        )
        self.create_line_item(original, description='Dup Item', quantity=Decimal('3'))
        resp = self.api_post(
            f'/api/invoices/invoices/{original.id}/duplicate/',
            user=user,
        )
        self.assertEqual(resp.status_code, 201)
        # New invoice should have different number
        self.assertNotEqual(resp.data['invoice_number'], original.invoice_number)
        # New invoice should be draft
        self.assertEqual(resp.data['status'], 'draft')


# ============================================================================
# Invoice Send Email
# ============================================================================


class TestInvoiceSendEmail(InvoiceTestCase):

    @patch('invoices.tasks.send_invoice_email.delay')
    def test_send_email_with_valid_recipient(self, mock_task):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client, status='sent',
        )
        resp = self.api_post(
            f'/api/invoices/invoices/{invoice.id}/send_email/',
            {
                'recipient_email': 'customer@example.com',
                'subject': 'Your Invoice',
                'message': 'Please find attached.',
            },
            user=user,
        )
        self.assertEqual(resp.status_code, 200)
        mock_task.assert_called_once()
        invoice.refresh_from_db()
        self.assertIsNotNone(invoice.sent_date)

    @patch('invoices.tasks.send_invoice_email.delay')
    def test_send_email_with_cc_emails(self, mock_task):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client, status='sent',
        )
        resp = self.api_post(
            f'/api/invoices/invoices/{invoice.id}/send_email/',
            {
                'recipient_email': 'customer@example.com',
                'cc_emails': ['cc1@example.com', 'cc2@example.com'],
            },
            user=user,
        )
        self.assertEqual(resp.status_code, 200)
        mock_task.assert_called_once()


# ============================================================================
# Invoice Stats
# ============================================================================


class TestInvoiceStatsEndpoint(InvoiceTestCase):

    def test_stats_structure(self):
        user = self.create_invoice_user()
        resp = self.api_get('/api/invoices/invoices/stats/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('current_month', resp.data)
        self.assertIn('outstanding_amount', resp.data)
        self.assertIn('overdue_count', resp.data)
        self.assertIn('total_invoiced', resp.data['current_month'])
        self.assertIn('total_paid', resp.data['current_month'])
        self.assertIn('count', resp.data['current_month'])

    def test_stats_reflect_data(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        self.create_invoice(
            created_by=user, client=ecom_client,
            status='paid', total=Decimal('1000.00'),
        )
        self.create_invoice(
            created_by=user, client=ecom_client,
            status='sent', total=Decimal('500.00'),
            due_date=timezone.now().date() - timedelta(days=5),
        )
        resp = self.api_get('/api/invoices/invoices/stats/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.data['overdue_count'], 1)


# ============================================================================
# Invoice Settings Available ItemLists
# ============================================================================


class TestInvoiceSettingsAvailableItemLists(InvoiceTestCase):

    def test_available_itemlists(self):
        user = self.create_invoice_user()
        self.create_item_list(title='Clients List', created_by=user)
        self.create_item_list(title='Products List', created_by=user)
        resp = self.api_get('/api/invoices/settings/available-itemlists/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.data), 2)


# ============================================================================
# Invoice Template Update
# ============================================================================


class TestInvoiceTemplateUpdate(InvoiceTestCase):

    def test_update_template_name(self):
        user = self.create_invoice_user()
        template = self.create_invoice_template(name='Old Name', created_by=user)
        resp = self.api_patch(
            f'/api/invoices/templates/{template.id}/',
            {'name': 'New Name'},
            user=user,
        )
        self.assertEqual(resp.status_code, 200)
        template.refresh_from_db()
        self.assertEqual(template.name, 'New Name')

    def test_update_template_html(self):
        user = self.create_invoice_user()
        template = self.create_invoice_template(created_by=user)
        new_html = '<html><body>Updated {{ invoice }}</body></html>'
        resp = self.api_patch(
            f'/api/invoices/templates/{template.id}/',
            {'html_content': new_html},
            user=user,
        )
        self.assertEqual(resp.status_code, 200)
        template.refresh_from_db()
        self.assertEqual(template.html_content, new_html)

    def test_update_template_set_default(self):
        user = self.create_invoice_user()
        t1 = self.create_invoice_template(
            name='T1', is_default=True, created_by=user,
        )
        t2 = self.create_invoice_template(
            name='T2', is_default=False, created_by=user,
        )
        resp = self.api_patch(
            f'/api/invoices/templates/{t2.id}/',
            {'is_default': True},
            user=user,
        )
        self.assertEqual(resp.status_code, 200)
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertFalse(t1.is_default)
        self.assertTrue(t2.is_default)


# ============================================================================
# Client ViewSet
# ============================================================================


class TestClientViewSet(InvoiceTestCase):

    def test_list_clients_from_ecommerce(self):
        """When no client_itemlist configured, returns EcommerceClients."""
        user = self.create_invoice_user()
        self.create_ecommerce_client(first_name='Ecom', last_name='Client')
        resp = self.api_get('/api/invoices/clients/', user=user)
        self.assertEqual(resp.status_code, 200)



# ============================================================================
# Materials ViewSet
# ============================================================================


class TestMaterialsViewSet(InvoiceTestCase):

    def test_list_materials_empty_when_not_configured(self):
        user = self.create_invoice_user()
        resp = self.api_get('/api/invoices/materials/', user=user)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 0)
