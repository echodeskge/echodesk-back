import base64
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(soft_time_limit=120, time_limit=180)
def generate_invoice_pdf(schema_name, invoice_id):
    from tenant_schemas.utils import schema_context
    from invoices.models import Invoice

    with schema_context(schema_name):
        invoice = Invoice.objects.get(id=invoice_id)
        invoice.generate_pdf()
        logger.info(f'generate_invoice_pdf({schema_name}, {invoice_id}) completed')
        return str(invoice.pdf_file.url) if invoice.pdf_file else None


@shared_task(
    bind=True,
    soft_time_limit=120,
    time_limit=180,
    autoretry_for=(Exception,),
    retry_kwargs={'max_retries': 3},
    default_retry_delay=60,
)
def send_invoice_email(self, schema_name, invoice_id, recipient_email,
                       cc_emails=None, subject=None, message=None,
                       attach_pdf=True):
    """
    Send invoice email via SendGrid.

    Args:
        schema_name: Tenant schema name for multi-tenancy
        invoice_id: Invoice ID to send
        recipient_email: Primary recipient email
        cc_emails: List of CC email addresses
        subject: Email subject (supports template variables)
        message: Email body (supports template variables)
        attach_pdf: Whether to attach the PDF
    """
    from tenant_schemas.utils import schema_context
    from invoices.models import Invoice, InvoiceSettings
    from django.conf import settings as django_settings
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import (
        Mail, Email, To, Content, Attachment, FileContent,
        FileName, FileType, Disposition, Cc,
    )

    with schema_context(schema_name):
        invoice = Invoice.objects.select_related(
            'client', 'client_itemlist_item'
        ).get(id=invoice_id)

        try:
            inv_settings = InvoiceSettings.objects.first()
        except InvoiceSettings.DoesNotExist:
            inv_settings = None

        # Resolve client name
        client_name = invoice.client_name
        if not client_name:
            if invoice.client_itemlist_item:
                client_name = invoice.client_itemlist_item.label
            elif invoice.client:
                client_name = (
                    f"{invoice.client.first_name} {invoice.client.last_name}".strip()
                    or invoice.client.email
                )
            else:
                client_name = 'Client'

        company_name = inv_settings.company_name if inv_settings else 'Company'

        # Template variable substitution
        template_vars = {
            '{invoice_number}': invoice.invoice_number,
            '{company_name}': company_name,
            '{client_name}': client_name,
            '{total}': f"{invoice.currency} {invoice.total}",
            '{due_date}': str(invoice.due_date),
        }

        def apply_vars(text):
            if not text:
                return text
            for key, val in template_vars.items():
                text = text.replace(key, val)
            return text

        # Resolve subject
        if not subject and inv_settings:
            subject = inv_settings.email_subject_template
        if not subject:
            subject = f"Invoice {invoice.invoice_number} from {company_name}"
        subject = apply_vars(subject)

        # Resolve message body
        if not message and inv_settings and inv_settings.email_message_template:
            message = inv_settings.email_message_template
        if not message:
            message = (
                f"Dear {client_name},\n\n"
                f"Please find attached invoice {invoice.invoice_number} "
                f"for {invoice.currency} {invoice.total}.\n\n"
                f"Due date: {invoice.due_date}\n\n"
                f"Best regards,\n{company_name}"
            )
        message = apply_vars(message)

        # Determine sender
        from_email_addr = django_settings.SENDGRID_FROM_EMAIL
        from_name = django_settings.SENDGRID_FROM_NAME
        if inv_settings:
            if inv_settings.email_from:
                from_email_addr = inv_settings.email_from
            if inv_settings.email_from_name:
                from_name = inv_settings.email_from_name

        # Build email
        mail = Mail(
            from_email=Email(from_email_addr, from_name),
            to_emails=To(recipient_email),
            subject=subject,
            html_content=Content(
                "text/html",
                f"<div style='white-space: pre-wrap;'>{message}</div>",
            ),
        )

        # Add CC recipients
        all_cc = []
        if cc_emails:
            all_cc.extend(cc_emails)
        if inv_settings and inv_settings.email_cc:
            settings_cc = [
                e.strip() for e in inv_settings.email_cc.replace(',', '\n').split('\n')
                if e.strip()
            ]
            all_cc.extend(settings_cc)
        for cc_addr in set(all_cc):
            mail.add_cc(Cc(cc_addr))

        # Attach PDF. Right after finalize the async generate_invoice_pdf task
        # may not have finished yet; render it here rather than silently
        # sending the email without the attachment.
        if attach_pdf and not invoice.pdf_file:
            try:
                invoice.generate_pdf()
            except Exception as e:
                logger.warning(
                    f"Could not generate PDF for invoice {invoice_id} before emailing: {e}"
                )

        if attach_pdf and invoice.pdf_file:
            try:
                pdf_data = invoice.pdf_file.read()
                encoded = base64.b64encode(pdf_data).decode('ascii')
                attachment = Attachment(
                    FileContent(encoded),
                    FileName(f"{invoice.invoice_number}.pdf"),
                    FileType("application/pdf"),
                    Disposition("attachment"),
                )
                mail.attachment = attachment
            except Exception as e:
                logger.warning(f"Could not attach PDF for invoice {invoice_id}: {e}")

        # Send via SendGrid
        api_key = django_settings.SENDGRID_API_KEY
        if not api_key:
            logger.error("SendGrid API key not configured. Email not sent.")
            return False

        sg = SendGridAPIClient(api_key)
        response = sg.send(mail)

        if response.status_code in (200, 201, 202):
            logger.info(
                f"Invoice email sent for {invoice.invoice_number} to {recipient_email}"
            )
            return True
        else:
            logger.error(
                f"SendGrid returned {response.status_code} for invoice {invoice.invoice_number}"
            )
            raise Exception(f"SendGrid error: status {response.status_code}")
