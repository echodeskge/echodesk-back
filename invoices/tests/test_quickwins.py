"""
Tests for the "quick wins" invoice package (mobile readiness):

- list filters: ``status=overdue`` == ``overdue=true`` (computed), search on
  invoice_number / client_name / ItemList client label + email,
  ``client_itemlist_item`` filter
- PUT/PATCH return InvoiceDetail and can change the client (FK assignment bug)
- mark_paid rejected on cancelled invoices
- payment amount <= balance validation, no payments on cancelled invoices,
  deleting a payment recomputes paid_amount / status
- send_invoice_email task renders the PDF when it is missing
- OpenAPI schema declares the real shapes of the ad-hoc endpoints
"""
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone

from invoices.models import InvoiceSettings, Invoice, InvoicePayment
from invoices.serializers import InvoicePaymentSerializer
from invoices.tests.conftest import InvoiceTestCase


# ============================================================================
# List filters & search
# ============================================================================


class TestOverdueStatusFilter(InvoiceTestCase):

    def _seed(self, user):
        ecom_client = self.create_ecommerce_client()
        today = timezone.now().date()
        overdue_sent = self.create_invoice(
            created_by=user, client=ecom_client, status='sent',
            due_date=today - timedelta(days=5),
        )
        overdue_partial = self.create_invoice(
            created_by=user, client=ecom_client, status='partially_paid',
            due_date=today - timedelta(days=1),
        )
        # Stored 'overdue' status (written by update_payment_status) counts too
        overdue_stored = self.create_invoice(
            created_by=user, client=ecom_client, status='overdue',
            due_date=today - timedelta(days=3),
        )
        # Not overdue: future due date, paid, draft, cancelled
        self.create_invoice(
            created_by=user, client=ecom_client, status='sent',
            due_date=today + timedelta(days=5),
        )
        self.create_invoice(
            created_by=user, client=ecom_client, status='paid',
            due_date=today - timedelta(days=5),
        )
        self.create_invoice(
            created_by=user, client=ecom_client, status='draft',
            due_date=today - timedelta(days=5),
        )
        self.create_invoice(
            created_by=user, client=ecom_client, status='cancelled',
            due_date=today - timedelta(days=5),
        )
        return {overdue_sent.id, overdue_partial.id, overdue_stored.id}

    def test_status_overdue_matches_overdue_true(self):
        user = self.create_invoice_user()
        expected = self._seed(user)

        by_status = self.api_get('/api/invoices/invoices/?status=overdue', user=user)
        by_flag = self.api_get('/api/invoices/invoices/?overdue=true', user=user)
        self.assertEqual(by_status.status_code, 200)
        self.assertEqual(by_flag.status_code, 200)

        ids_status = {r['id'] for r in self.get_results(by_status)}
        ids_flag = {r['id'] for r in self.get_results(by_flag)}
        self.assertEqual(ids_status, expected)
        self.assertEqual(ids_flag, expected)
        for row in self.get_results(by_flag):
            self.assertTrue(row['is_overdue'])

    def test_overdue_false_is_ignored(self):
        user = self.create_invoice_user()
        self._seed(user)
        resp = self.api_get('/api/invoices/invoices/?overdue=false', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 7)

    def test_stored_status_filter_still_exact(self):
        """Other statuses keep filtering on the stored column."""
        user = self.create_invoice_user()
        self._seed(user)
        resp = self.api_get('/api/invoices/invoices/?status=sent', user=user)
        self.assertEqual(resp.status_code, 200)
        statuses = {r['status'] for r in self.get_results(resp)}
        self.assertEqual(statuses, {'sent'})
        self.assertEqual(resp.data['count'], 2)

    def test_stats_overdue_count_uses_same_rule(self):
        user = self.create_invoice_user()
        expected = self._seed(user)
        resp = self.api_get('/api/invoices/invoices/stats/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['overdue_count'], len(expected))

    def test_invalid_status_is_400(self):
        user = self.create_invoice_user()
        resp = self.api_get('/api/invoices/invoices/?status=bogus', user=user)
        self.assertEqual(resp.status_code, 400)

    def test_date_range_and_currency_filters_still_work(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        today = timezone.now().date()
        self.create_invoice(created_by=user, client=ecom_client, issue_date=today, currency='USD')
        self.create_invoice(created_by=user, client=ecom_client, issue_date=today - timedelta(days=40), currency='GEL')
        resp = self.api_get(
            f'/api/invoices/invoices/?date_from={today - timedelta(days=10)}&date_to={today}',
            user=user,
        )
        self.assertEqual(resp.data['count'], 1)
        resp = self.api_get('/api/invoices/invoices/?currency=GEL', user=user)
        self.assertEqual(resp.data['count'], 1)
        self.assertEqual(self.get_results(resp)[0]['currency'], 'GEL')


class TestInvoiceSearchAndItemListClient(InvoiceTestCase):

    def _itemlist_client(self, user, label, email):
        item_list = self.create_item_list(title='Clients', created_by=user)
        list_item = self.create_list_item(
            item_list, label=label, created_by=user,
            custom_data={'email': email, 'phone': '+995555000000'},
        )
        settings = InvoiceSettings.objects.get_or_create()[0]
        settings.client_itemlist = item_list
        settings.save()
        return item_list, list_item

    def test_search_by_itemlist_client_label(self):
        user = self.create_invoice_user()
        _, list_item = self._itemlist_client(user, 'Acme Widgets Ltd', 'billing@acme.example')
        target = self.create_invoice(
            created_by=user, status='sent', client_itemlist_item=list_item, client_name='Acme Widgets Ltd',
        )
        self.create_invoice(created_by=user, status='sent', client_name='Someone Else')

        resp = self.api_get('/api/invoices/invoices/?search=widgets', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r['id'] for r in self.get_results(resp)], [target.id])

    def test_search_by_itemlist_client_email(self):
        user = self.create_invoice_user()
        _, list_item = self._itemlist_client(user, 'Acme Widgets Ltd', 'billing@acme.example')
        target = self.create_invoice(
            created_by=user, status='sent', client_itemlist_item=list_item, client_name='Acme Widgets Ltd',
        )
        self.create_invoice(created_by=user, status='sent', client_name='Other Co')

        resp = self.api_get('/api/invoices/invoices/?search=billing@acme', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r['id'] for r in self.get_results(resp)], [target.id])

    def test_search_by_cached_client_name(self):
        """Invoices whose client was deleted/unlinked are still found by client_name."""
        user = self.create_invoice_user()
        target = self.create_invoice(created_by=user, status='sent', client_name='Zebra Holdings')
        self.create_invoice(created_by=user, status='sent', client_name='Other Co')
        resp = self.api_get('/api/invoices/invoices/?search=zebra', user=user)
        self.assertEqual([r['id'] for r in self.get_results(resp)], [target.id])

    def test_search_by_ecommerce_client_email_still_works(self):
        user = self.create_invoice_user()
        client_a = self.create_ecommerce_client(first_name='Ann', last_name='A', email='ann@findme.example')
        client_b = self.create_ecommerce_client(first_name='Bob', last_name='B', email='bob@other.example')
        target = self.create_invoice(created_by=user, client=client_a)
        self.create_invoice(created_by=user, client=client_b)
        resp = self.api_get('/api/invoices/invoices/?search=findme', user=user)
        self.assertEqual([r['id'] for r in self.get_results(resp)], [target.id])

    def test_filter_by_client_itemlist_item(self):
        user = self.create_invoice_user()
        item_list, list_item = self._itemlist_client(user, 'Acme', 'a@acme.example')
        other_item = self.create_list_item(item_list, label='Beta', created_by=user)
        target = self.create_invoice(created_by=user, client_itemlist_item=list_item, client_name='Acme')
        self.create_invoice(created_by=user, client_itemlist_item=other_item, client_name='Beta')

        resp = self.api_get(
            f'/api/invoices/invoices/?client_itemlist_item={list_item.id}', user=user,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r['id'] for r in self.get_results(resp)], [target.id])


# ============================================================================
# Update returns InvoiceDetail and can reassign the client
# ============================================================================


class TestInvoiceUpdateReturnsDetail(InvoiceTestCase):

    def test_patch_returns_detail_shape(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(created_by=user, client=ecom_client, status='draft')
        self.create_line_item(invoice, unit_price=Decimal('100.00'), tax_rate=Decimal('0.00'))

        resp = self.api_patch(
            f'/api/invoices/invoices/{invoice.id}/', {'notes': 'edited'}, user=user,
        )
        self.assertEqual(resp.status_code, 200)
        for field in ('id', 'invoice_number', 'status', 'total', 'balance', 'line_items',
                      'payments', 'client_details', 'is_overdue', 'pdf_url', 'created_by_name'):
            self.assertIn(field, resp.data, field)
        self.assertEqual(resp.data['id'], invoice.id)
        self.assertEqual(resp.data['notes'], 'edited')
        self.assertEqual(Decimal(resp.data['total']), Decimal('100.00'))
        self.assertEqual(len(resp.data['line_items']), 1)
        self.assertEqual(resp.data['status'], 'draft')

    def test_patch_can_change_ecommerce_client(self):
        """Regression: setattr(instance, 'client', <int>) used to raise."""
        user = self.create_invoice_user()
        client_a = self.create_ecommerce_client(first_name='Ann', last_name='A')
        client_b = self.create_ecommerce_client(first_name='Bob', last_name='B')
        invoice = self.create_invoice(created_by=user, client=client_a, status='draft')

        resp = self.api_patch(
            f'/api/invoices/invoices/{invoice.id}/', {'client': client_b.id}, user=user,
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        invoice.refresh_from_db()
        self.assertEqual(invoice.client_id, client_b.id)
        self.assertEqual(invoice.client_name, 'Bob B')
        self.assertEqual(resp.data['client'], client_b.id)
        self.assertEqual(resp.data['client_details']['full_name'], 'Bob B')

    def test_put_with_itemlist_client(self):
        user = self.create_invoice_user()
        item_list = self.create_item_list(title='Clients', created_by=user)
        item_a = self.create_list_item(item_list, label='Acme', created_by=user)
        item_b = self.create_list_item(item_list, label='Beta', created_by=user)
        settings = InvoiceSettings.objects.get_or_create()[0]
        settings.client_itemlist = item_list
        settings.save()
        invoice = self.create_invoice(
            created_by=user, status='draft', client_itemlist_item=item_a, client_name='Acme',
        )
        today = timezone.now().date()
        resp = self.api_put(
            f'/api/invoices/invoices/{invoice.id}/',
            {
                'client': item_b.id,
                'issue_date': str(today),
                'due_date': str(today + timedelta(days=10)),
                'line_items': [
                    {'description': 'Work', 'quantity': '2.00', 'unit_price': '50.00', 'tax_rate': '0.00'},
                ],
            },
            user=user,
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        invoice.refresh_from_db()
        self.assertEqual(invoice.client_itemlist_item_id, item_b.id)
        self.assertIsNone(invoice.client_id)
        self.assertEqual(invoice.client_name, 'Beta')
        self.assertEqual(Decimal(resp.data['total']), Decimal('100.00'))
        self.assertEqual(resp.data['client_itemlist_item'], item_b.id)

    def test_patch_non_draft_still_rejected(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(created_by=user, client=ecom_client, status='sent')
        resp = self.api_patch(
            f'/api/invoices/invoices/{invoice.id}/', {'notes': 'nope'}, user=user,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['error'], 'Only draft invoices can be edited')


# ============================================================================
# mark_paid on cancelled
# ============================================================================


class TestMarkPaidCancelled(InvoiceTestCase):

    def test_mark_paid_cancelled_rejected(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client, status='cancelled', total=Decimal('100.00'),
        )
        resp = self.api_post(
            f'/api/invoices/invoices/{invoice.id}/mark_paid/', {}, user=user,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.data)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'cancelled')
        self.assertEqual(invoice.paid_amount, Decimal('0.00'))
        self.assertFalse(InvoicePayment.objects.filter(invoice=invoice).exists())

    def test_mark_paid_without_body_still_works(self):
        user = self.create_invoice_user()
        ecom_client = self.create_ecommerce_client()
        invoice = self.create_invoice(
            created_by=user, client=ecom_client, status='sent', total=Decimal('100.00'),
        )
        resp = self.api_post(f'/api/invoices/invoices/{invoice.id}/mark_paid/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'paid')
        payment = InvoicePayment.objects.get(invoice=invoice)
        self.assertEqual(payment.payment_method, 'bank_transfer')


# ============================================================================
# Payment validation + delete recompute
# ============================================================================


class TestPaymentAmountValidation(InvoiceTestCase):

    def test_amount_above_balance_rejected(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user, total=Decimal('100.00'), status='sent')
        resp = self.api_post(
            '/api/invoices/payments/',
            {'invoice': invoice.id, 'amount': '100.01', 'payment_method': 'cash'},
            user=user,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('amount', resp.data)
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, Decimal('0.00'))

    def test_amount_above_remaining_balance_rejected(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user, total=Decimal('100.00'), status='sent')
        self.create_payment(invoice, amount=Decimal('70.00'), recorded_by=user)
        resp = self.api_post(
            '/api/invoices/payments/',
            {'invoice': invoice.id, 'amount': '40.00'},
            user=user,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('amount', resp.data)

    def test_amount_equal_to_balance_accepted(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user, total=Decimal('100.00'), status='sent')
        self.create_payment(invoice, amount=Decimal('70.00'), recorded_by=user)
        resp = self.api_post(
            '/api/invoices/payments/',
            {'invoice': invoice.id, 'amount': '30.00'},
            user=user,
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'paid')
        self.assertEqual(invoice.paid_amount, Decimal('100.00'))

    def test_payment_on_cancelled_invoice_rejected(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user, total=Decimal('100.00'), status='cancelled')
        resp = self.api_post(
            '/api/invoices/payments/',
            {'invoice': invoice.id, 'amount': '10.00'},
            user=user,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('invoice', resp.data)

    def test_update_payment_amount_gives_back_own_amount(self):
        """Editing a payment compares against balance + its previous amount."""
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user, total=Decimal('100.00'), status='sent')
        payment = self.create_payment(invoice, amount=Decimal('100.00'), recorded_by=user)
        # Lowering to 60 is fine (balance is 0 + its own 100)
        resp = self.api_patch(
            f'/api/invoices/payments/{payment.id}/', {'amount': '60.00'}, user=user,
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, Decimal('60.00'))
        self.assertEqual(invoice.status, 'partially_paid')
        # Raising above the invoice total is not
        resp = self.api_patch(
            f'/api/invoices/payments/{payment.id}/', {'amount': '100.01'}, user=user,
        )
        self.assertEqual(resp.status_code, 400)

    def test_serializer_validation_direct(self):
        invoice = self.create_invoice(total=Decimal('50.00'), status='sent')
        serializer = InvoicePaymentSerializer(data={'invoice': invoice.id, 'amount': '60.00'})
        self.assertFalse(serializer.is_valid())
        self.assertIn('amount', serializer.errors)


class TestPaymentDeleteRecompute(InvoiceTestCase):

    def test_delete_only_payment_reverts_to_sent(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user, total=Decimal('100.00'), status='sent')
        payment = self.create_payment(invoice, amount=Decimal('100.00'), recorded_by=user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'paid')
        self.assertIsNotNone(invoice.paid_date)

        resp = self.api_delete(f'/api/invoices/payments/{payment.id}/', user=user)
        self.assertEqual(resp.status_code, 204)
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, Decimal('0.00'))
        self.assertEqual(invoice.status, 'sent')
        self.assertIsNone(invoice.paid_date)

    def test_delete_one_of_two_payments_goes_partially_paid(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user, total=Decimal('100.00'), status='sent')
        self.create_payment(invoice, amount=Decimal('40.00'), recorded_by=user)
        second = self.create_payment(invoice, amount=Decimal('60.00'), recorded_by=user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'paid')

        second.delete()
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, Decimal('40.00'))
        self.assertEqual(invoice.status, 'partially_paid')
        self.assertIsNone(invoice.paid_date)

    def test_delete_payment_on_past_due_invoice_becomes_overdue(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(
            created_by=user, total=Decimal('100.00'), status='sent',
            due_date=timezone.now().date() - timedelta(days=2),
        )
        payment = self.create_payment(invoice, amount=Decimal('100.00'), recorded_by=user)
        payment.delete()
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, Decimal('0.00'))
        self.assertEqual(invoice.status, 'overdue')

    def test_queryset_delete_recomputes(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user, total=Decimal('100.00'), status='sent')
        self.create_payment(invoice, amount=Decimal('100.00'), recorded_by=user)
        InvoicePayment.objects.filter(invoice=invoice).delete()
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, Decimal('0.00'))
        self.assertEqual(invoice.status, 'sent')

    def test_deleting_invoice_cascades_without_error(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user, total=Decimal('100.00'), status='draft')
        self.create_payment(invoice, amount=Decimal('50.00'), recorded_by=user)
        invoice_id = invoice.id
        invoice.delete()
        self.assertFalse(Invoice.objects.filter(id=invoice_id).exists())
        self.assertFalse(InvoicePayment.objects.filter(invoice_id=invoice_id).exists())


# ============================================================================
# send_invoice_email task generates the PDF when missing
# ============================================================================


class TestSendInvoiceEmailTaskGeneratesPdf(InvoiceTestCase):

    def _run_task(self, invoice, attach_pdf=True):
        from invoices.tasks import send_invoice_email
        fake_response = MagicMock(status_code=202)
        with patch('sendgrid.SendGridAPIClient') as sg_cls, \
                patch('django.conf.settings.SENDGRID_API_KEY', 'test-key', create=True), \
                patch('django.conf.settings.SENDGRID_FROM_EMAIL', 'from@test.example', create=True), \
                patch('django.conf.settings.SENDGRID_FROM_NAME', 'Test', create=True):
            sg_cls.return_value.send.return_value = fake_response
            result = send_invoice_email.apply(
                args=(self.tenant.schema_name, invoice.id, 'to@test.example'),
                kwargs={'attach_pdf': attach_pdf},
            ).get()
            sent_mail = sg_cls.return_value.send.call_args[0][0]
        return result, sent_mail

    def test_generates_pdf_when_missing(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user, status='sent', total=Decimal('10.00'))
        self.assertFalse(invoice.pdf_file)

        def fake_generate(self_inv):
            # Point the FileField at a name without touching the (S3) storage;
            # the read below is mocked too.
            self_inv.pdf_file = 'invoices/pdfs/fake.pdf'
            self_inv.pdf_generated_at = timezone.now()
            self_inv.save()
            return self_inv.pdf_file

        from django.db.models.fields.files import FieldFile
        with patch.object(Invoice, 'generate_pdf', autospec=True, side_effect=fake_generate) as gen, \
                patch.object(FieldFile, 'read', create=True, return_value=b'%PDF-1.4 fake'):
            result, mail = self._run_task(invoice)

        self.assertTrue(result)
        gen.assert_called_once()
        self.assertIsNotNone(mail.attachments)
        self.assertEqual(len(mail.attachments), 1)

    def test_does_not_generate_when_attach_pdf_false(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user, status='sent', total=Decimal('10.00'))
        with patch.object(Invoice, 'generate_pdf', autospec=True) as gen:
            result, mail = self._run_task(invoice, attach_pdf=False)
        self.assertTrue(result)
        gen.assert_not_called()
        self.assertFalse(mail.attachments)

    def test_pdf_failure_still_sends_email(self):
        user = self.create_invoice_user()
        invoice = self.create_invoice(created_by=user, status='sent', total=Decimal('10.00'))
        with patch.object(Invoice, 'generate_pdf', autospec=True, side_effect=RuntimeError('weasy')) as gen:
            result, mail = self._run_task(invoice)
        self.assertTrue(result)
        gen.assert_called_once()
        self.assertFalse(mail.attachments)


# ============================================================================
# OpenAPI schema
# ============================================================================


class TestInvoiceOpenApiSchema(InvoiceTestCase):
    """The generated schema must describe what the endpoints really return."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from drf_spectacular.generators import SchemaGenerator
        from django.urls import get_resolver
        generator = SchemaGenerator(urlconf=get_resolver().urlconf_name)
        schema = generator.get_schema(request=None, public=True)
        cls.paths = schema['paths']
        cls.components = schema['components']['schemas']

    def _op(self, path, method):
        return self.paths[path][method]

    def _response_schema(self, op, code='200', content_type='application/json'):
        return op['responses'][str(code)]['content'][content_type]['schema']

    def test_list_declares_filters(self):
        names = {p['name'] for p in self._op('/api/invoices/invoices/', 'get')['parameters']}
        for expected in ('status', 'overdue', 'date_from', 'date_to', 'client',
                         'client_itemlist_item', 'currency', 'search', 'ordering', 'page', 'page_size'):
            self.assertIn(expected, names)
        status_param = next(p for p in self._op('/api/invoices/invoices/', 'get')['parameters'] if p['name'] == 'status')
        self.assertIn('overdue', status_param['schema']['enum'])

    def test_list_and_detail_field_types(self):
        inv_list = self.components['InvoiceList']['properties']
        self.assertEqual(inv_list['is_overdue']['type'], 'boolean')
        self.assertEqual(inv_list['line_items_count']['type'], 'integer')

        detail = self.components['InvoiceDetail']['properties']
        self.assertEqual(detail['is_overdue']['type'], 'boolean')
        # Nested read-only serializer fields are emitted as allOf-wrapped refs
        client_details_ref = detail['client_details'].get('$ref') or detail['client_details']['allOf'][0]['$ref']
        self.assertIn('InvoiceClientDetails', client_details_ref)
        self.assertNotIn('nullable', detail['client_details'])
        self.assertTrue(detail['pdf_url']['nullable'])
        self.assertEqual(detail['pdf_url']['format'], 'uri')
        self.assertTrue(detail['created_by']['nullable'])
        self.assertTrue(detail['created_by_name']['nullable'])
        self.assertTrue(detail['template_name']['nullable'])

        client_details = self.components['InvoiceClientDetails']['properties']
        self.assertTrue(client_details['id']['nullable'])
        for f in ('first_name', 'last_name', 'email', 'phone', 'full_name'):
            self.assertEqual(client_details[f]['type'], 'string')

    def test_create_and_update_return_detail(self):
        for method, code in (('post', '201'),):
            schema = self._response_schema(self._op('/api/invoices/invoices/', method), code)
            self.assertEqual(schema['$ref'], '#/components/schemas/InvoiceDetail')
        for method in ('put', 'patch'):
            schema = self._response_schema(self._op('/api/invoices/invoices/{id}/', method))
            self.assertEqual(schema['$ref'], '#/components/schemas/InvoiceDetail')

    def test_actions_request_bodies(self):
        for name in ('finalize', 'duplicate'):
            op = self._op(f'/api/invoices/invoices/{{id}}/{name}/', 'post')
            self.assertNotIn('requestBody', op, name)
        mark_paid = self._op('/api/invoices/invoices/{id}/mark_paid/', 'post')
        self.assertIn('InvoiceMarkPaidRequest', mark_paid['requestBody']['content']['application/json']['schema']['$ref'])
        self.assertFalse(mark_paid['requestBody'].get('required', False))
        send = self._op('/api/invoices/invoices/{id}/send_email/', 'post')
        self.assertIn('InvoiceSendEmailRequest', send['requestBody']['content']['application/json']['schema']['$ref'])
        self.assertEqual(self.components['InvoiceSendEmailRequest']['required'], ['recipient_email'])
        self.assertIn('MessageResponse', self._response_schema(send)['$ref'])
        self.assertIn('ErrorResponse', self._response_schema(send, '400')['$ref'])

    def test_binary_downloads(self):
        pdf = self._op('/api/invoices/invoices/{id}/pdf/', 'get')
        self.assertEqual(self._response_schema(pdf, '200', 'application/pdf')['format'], 'binary')
        self.assertIn('regenerate', {p['name'] for p in pdf['parameters']})
        excel = self._op('/api/invoices/invoices/{id}/excel/', 'get')
        xlsx = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        self.assertEqual(self._response_schema(excel, '200', xlsx)['format'], 'binary')

    def test_stats_shape(self):
        schema = self._response_schema(self._op('/api/invoices/invoices/stats/', 'get'))
        self.assertEqual(schema['$ref'], '#/components/schemas/InvoiceStats')
        stats = self.components['InvoiceStats']['properties']
        self.assertEqual(stats['outstanding_amount']['type'], 'number')
        self.assertEqual(stats['overdue_count']['type'], 'integer')
        month = self.components['InvoiceStatsCurrentMonth']['properties']
        self.assertEqual(month['total_invoiced']['type'], 'number')
        self.assertEqual(month['count']['type'], 'integer')

    def test_settings_list_is_single_object(self):
        schema = self._response_schema(self._op('/api/invoices/settings/', 'get'))
        self.assertEqual(schema['$ref'], '#/components/schemas/InvoiceSettings')
        self.assertNotIn('PaginatedInvoiceSettingsList', self.components)
        settings = self.components['InvoiceSettings']['properties']
        self.assertTrue(settings['client_itemlist_details']['nullable'])
        for f in ('logo', 'badge', 'signature'):
            self.assertTrue(settings[f]['nullable'])
            self.assertEqual(settings[f]['format'], 'uri')
        # POST on the singleton responds 200
        self.assertIn('200', self._op('/api/invoices/settings/', 'post')['responses'])

    def test_materials_and_itemlists_are_bare_lists(self):
        schema = self._response_schema(self._op('/api/invoices/materials/', 'get'))
        self.assertEqual(schema['type'], 'array')
        self.assertEqual(schema['items']['$ref'], '#/components/schemas/ListItemMaterial')
        self.assertNotIn('PaginatedListItemMaterialList', self.components)
        names = {p['name'] for p in self._op('/api/invoices/materials/', 'get')['parameters']}
        self.assertNotIn('page', names)
        self.assertIn('search', names)

        schema = self._response_schema(self._op('/api/invoices/settings/available-itemlists/', 'get'))
        self.assertEqual(schema['type'], 'array')
        self.assertEqual(schema['items']['$ref'], '#/components/schemas/InvoiceAvailableItemList')

    def test_clients_component_and_payments_param(self):
        schema = self._response_schema(self._op('/api/invoices/clients/', 'get'))
        self.assertEqual(schema['$ref'], '#/components/schemas/PaginatedInvoiceClientList')
        client = self.components['InvoiceClient']['properties']
        self.assertEqual(set(client), {'id', 'name', 'email', 'phone'})
        id_param = next(p for p in self._op('/api/invoices/clients/{id}/', 'get')['parameters'] if p['name'] == 'id')
        self.assertEqual(id_param['schema']['type'], 'integer')

        for path in ('/api/invoices/payments/', '/api/invoices/line-items/'):
            names = {p['name'] for p in self._op(path, 'get')['parameters']}
            self.assertIn('invoice_id', names)

    def test_operation_ids_unchanged(self):
        """The web dashboard imports these generated function names."""
        expected = {
            ('/api/invoices/invoices/', 'get'): 'invoices_invoices_list',
            ('/api/invoices/invoices/{id}/', 'patch'): 'invoices_invoices_partial_update',
            ('/api/invoices/invoices/stats/', 'get'): 'invoices_invoices_stats_retrieve',
            ('/api/invoices/settings/', 'get'): 'invoices_settings_list',
            ('/api/invoices/settings/', 'post'): 'invoices_settings_create',
            ('/api/invoices/materials/', 'get'): 'invoices_materials_list',
            ('/api/invoices/clients/', 'get'): 'invoices_clients_list',
            ('/api/invoices/invoices/{id}/mark_paid/', 'post'): 'invoices_invoices_mark_paid_create',
            ('/api/invoices/invoices/{id}/send_email/', 'post'): 'invoices_invoices_send_email_create',
            ('/api/invoices/settings/available-itemlists/', 'get'): 'invoices_settings_available_itemlists_retrieve',
        }
        for (path, method), op_id in expected.items():
            self.assertEqual(self._op(path, method)['operationId'], op_id)
