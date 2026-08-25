import base64
import logging

from celery import shared_task
from django.core.management import call_command
from io import StringIO

logger = logging.getLogger(__name__)


@shared_task
def process_recurring_payments():
    output = StringIO()
    call_command('process_recurring_payments', stdout=output)
    result = output.getvalue()
    logger.info(f'process_recurring_payments completed: {result}')
    return result


@shared_task
def check_subscription_status():
    output = StringIO()
    call_command('check_subscription_status', stdout=output)
    result = output.getvalue()
    logger.info(f'check_subscription_status completed: {result}')
    return result


@shared_task
def process_trial_expirations():
    output = StringIO()
    call_command('process_trial_expirations', stdout=output)
    result = output.getvalue()
    logger.info(f'process_trial_expirations completed: {result}')
    return result


@shared_task
def process_payment_retries():
    output = StringIO()
    call_command('process_payment_retries', stdout=output)
    result = output.getvalue()
    logger.info(f'process_payment_retries completed: {result}')
    return result


@shared_task
def reconcile_pending_payments():
    output = StringIO()
    call_command('reconcile_pending_payments', stdout=output)
    result = output.getvalue()
    logger.info(f'reconcile_pending_payments completed: {result}')
    return result


@shared_task
def calculate_platform_metrics():
    output = StringIO()
    call_command('calculate_platform_metrics', stdout=output)
    result = output.getvalue()
    logger.info(f'calculate_platform_metrics completed: {result}')
    return result


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def generate_tenant_invoice_pdf(self, invoice_id):
    """Generate a PDF for a tenant subscription invoice and upload to DO Spaces."""
    from django.template.loader import render_to_string
    from django.core.files.storage import default_storage
    from django.core.files.base import ContentFile
    from weasyprint import HTML
    from tenants.models import Invoice

    try:
        invoice = Invoice.objects.select_related('tenant', 'payment_order').get(id=invoice_id)

        if invoice.pdf_generated and invoice.pdf_url:
            logger.info(f'PDF already generated for invoice {invoice.invoice_number}')
            send_tenant_invoice_email.delay(invoice_id)
            return f'PDF already exists: {invoice.pdf_url}'

        tenant = invoice.tenant

        # Determine provider display name
        provider = invoice.metadata.get('provider', '')
        if not provider:
            provider = 'bog' if invoice.metadata.get('bog_order_id') else 'unknown'
        provider_display = {'paddle': 'Paddle', 'bog': 'Bank of Georgia'}.get(provider, provider.title())

        # Get selected features if subscription exists
        features = ''
        subscription = getattr(tenant, 'subscription', None)
        if subscription:
            feature_names = list(subscription.selected_features.values_list('name', flat=True))
            if feature_names:
                features = ', '.join(feature_names)

        context = {
            'invoice': invoice,
            'tenant': tenant,
            'provider_display': provider_display,
            'features': features,
        }

        html_string = render_to_string('tenants/subscription_invoice_pdf.html', context)
        pdf_bytes = HTML(string=html_string).write_pdf()

        # Upload to DO Spaces
        file_path = f'tenant_invoices/pdfs/{invoice.invoice_number}.pdf'
        saved_path = default_storage.save(file_path, ContentFile(pdf_bytes))

        # Build public URL
        from django.conf import settings
        pdf_url = f'https://{settings.AWS_S3_CUSTOM_DOMAIN}/{settings.AWS_LOCATION}/{saved_path}'

        invoice.pdf_url = pdf_url
        invoice.pdf_generated = True
        invoice.save(update_fields=['pdf_url', 'pdf_generated'])

        logger.info(f'PDF generated for invoice {invoice.invoice_number}: {pdf_url}')

        # Chain to email delivery
        send_tenant_invoice_email.delay(invoice_id)

        return f'PDF generated: {pdf_url}'

    except Invoice.DoesNotExist:
        logger.error(f'Invoice {invoice_id} not found for PDF generation')
        return f'Invoice {invoice_id} not found'
    except Exception as exc:
        logger.error(f'Failed to generate PDF for invoice {invoice_id}: {exc}')
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_tenant_invoice_email(self, invoice_id):
    """Send invoice email with PDF attachment to tenant admin."""
    from django.conf import settings
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import (
        Mail, Email, To, Content, Attachment, FileContent,
        FileName, FileType, Disposition,
    )
    from tenants.models import Invoice

    try:
        invoice = Invoice.objects.select_related('tenant').get(id=invoice_id)
        tenant = invoice.tenant

        if not tenant.admin_email:
            logger.warning(f'No admin email for tenant {tenant.schema_name}, skipping invoice email')
            return 'No admin email'

        api_key = settings.SENDGRID_API_KEY
        if not api_key:
            logger.error('SendGrid API key not configured, cannot send invoice email')
            return 'No SendGrid API key'

        subject = f'Your EchoDesk Invoice {invoice.invoice_number}'

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background-color: #4F46E5; color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0;">
                    <h1 style="margin: 0;">Your EchoDesk Invoice</h1>
                </div>
                <div style="background-color: #f9fafb; padding: 30px; border-radius: 0 0 8px 8px;">
                    <p>Hello{' ' + tenant.admin_name if tenant.admin_name else ''},</p>
                    <p>Thank you for your payment. Please find your invoice details below:</p>
                    <div style="background-color: white; padding: 20px; border-radius: 6px; margin: 20px 0; border-left: 4px solid #4F46E5;">
                        <p><strong>Invoice #:</strong> {invoice.invoice_number}</p>
                        <p><strong>Amount:</strong> {invoice.amount} {invoice.currency}</p>
                        <p><strong>Description:</strong> {invoice.description}</p>
                        <p><strong>Date:</strong> {invoice.invoice_date.strftime('%b %d, %Y') if invoice.invoice_date else 'N/A'}</p>
                    </div>
                    {'<p>The PDF invoice is attached to this email.</p>' if invoice.pdf_generated else ''}
                    <p>If you have any questions about this invoice, please contact us at support@echodesk.ge.</p>
                    <p>Best regards,<br>The EchoDesk Team</p>
                </div>
                <div style="text-align: center; padding: 20px; color: #6b7280; font-size: 14px;">
                    <p>&copy; 2025 EchoDesk. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        mail = Mail(
            from_email=Email(settings.SENDGRID_FROM_EMAIL, settings.SENDGRID_FROM_NAME),
            to_emails=To(tenant.admin_email),
            subject=subject,
            html_content=Content("text/html", html_content),
        )

        # Attach PDF if available
        if invoice.pdf_generated and invoice.pdf_url:
            try:
                import urllib.request
                req = urllib.request.Request(invoice.pdf_url)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    pdf_data = resp.read()
                encoded = base64.b64encode(pdf_data).decode('ascii')
                attachment = Attachment(
                    FileContent(encoded),
                    FileName(f"{invoice.invoice_number}.pdf"),
                    FileType("application/pdf"),
                    Disposition("attachment"),
                )
                mail.attachment = attachment
            except Exception as e:
                logger.warning(f'Could not attach PDF for invoice {invoice.invoice_number}: {e}')

        sg = SendGridAPIClient(api_key)
        response = sg.send(mail)

        if response.status_code in [200, 201, 202]:
            logger.info(f'Invoice email sent to {tenant.admin_email} for {invoice.invoice_number}')
            return f'Email sent to {tenant.admin_email}'
        else:
            logger.error(f'Failed to send invoice email: status {response.status_code}')
            return f'Email failed: status {response.status_code}'

    except Invoice.DoesNotExist:
        logger.error(f'Invoice {invoice_id} not found for email')
        return f'Invoice {invoice_id} not found'
    except Exception as exc:
        logger.error(f'Failed to send invoice email for {invoice_id}: {exc}')
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def process_pending_tenant(self, schema_name):
    try:
        output = StringIO()
        call_command('process_pending_tenants', '--schema-name', schema_name, stdout=output)
        result = output.getvalue()
        logger.info(f'process_pending_tenant({schema_name}) completed: {result}')
        return result
    except Exception as exc:
        logger.error(f'process_pending_tenant({schema_name}) failed: {exc}')
        raise self.retry(exc=exc)
