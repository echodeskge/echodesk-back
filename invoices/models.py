"""
Invoice Management Models

This module contains all models for the Invoice Management feature:
- InvoiceSettings: Company and invoice configuration per tenant
- Invoice: Main invoice model
- InvoiceLineItem: Individual line items in an invoice
- InvoicePayment: Payment tracking for invoices
- InvoiceTemplate: Customizable invoice templates
"""

from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from decimal import Decimal
import uuid


class InvoiceSettings(models.Model):
    """
    Tenant-specific invoice settings and company information.
    One instance per tenant.
    """
    # Company Information
    company_name = models.CharField(max_length=255, blank=True, verbose_name=_("Company Name"))
    tax_id = models.CharField(max_length=50, blank=True, verbose_name=_("Tax ID"))
    registration_number = models.CharField(max_length=50, blank=True, verbose_name=_("Registration Number"))
    address = models.TextField(blank=True, verbose_name=_("Address"))
    phone = models.CharField(max_length=50, blank=True, verbose_name=_("Phone"))
    email = models.EmailField(blank=True, verbose_name=_("Email"))
    website = models.URLField(blank=True, verbose_name=_("Website"))

    # Branding
    logo = models.FileField(upload_to='invoices/logos/', blank=True, null=True, verbose_name=_("Logo"))
    badge = models.FileField(upload_to='invoices/badges/', blank=True, null=True, verbose_name=_("Badge/Seal"))
    signature = models.FileField(upload_to='invoices/signatures/', blank=True, null=True, verbose_name=_("Signature"))

    # Invoice Configuration
    invoice_prefix = models.CharField(
        max_length=10,
        default='INV',
        verbose_name=_("Invoice Prefix"),
        help_text=_("Prefix for invoice numbers (e.g., INV, FAC)")
    )
    starting_number = models.PositiveIntegerField(
        default=1,
        verbose_name=_("Starting Number"),
        help_text=_("Starting number for invoice sequence")
    )
    default_currency = models.CharField(
        max_length=3,
        default='GEL',
        verbose_name=_("Default Currency")
    )
    default_tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.00,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name=_("Default Tax Rate (%)"),
        help_text=_("Default tax rate percentage for new invoices")
    )
    default_due_days = models.PositiveIntegerField(
        default=30,
        verbose_name=_("Default Due Days"),
        help_text=_("Default number of days until invoice is due")
    )

    # Bank Accounts (JSON field to store multiple accounts)
    bank_accounts = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Bank Accounts"),
        help_text=_("List of bank accounts for payment")
    )
    # Example structure:
    # [
    #     {
    #         "bank_name": "Bank of Georgia",
    #         "account_number": "GE00BG0000000000000000",
    #         "iban": "GE00BG0000000000000000",
    #         "swift": "BAGAGE22",
    #         "is_default": true
    #     }
    # ]

    # Client List Configuration
    client_itemlist = models.ForeignKey(
        'tickets.ItemList',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoice_settings_client',
        verbose_name=_("Client Item List"),
        help_text=_("The item list to use for invoice clients (if using ItemList instead of ecommerce clients)")
    )

    # Materials/Products List Configuration
    materials_itemlist = models.ForeignKey(
        'tickets.ItemList',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoice_settings_materials',
        verbose_name=_("Materials Item List"),
        help_text=_("The item list to use for invoice line items / materials")
    )

    # Email Settings
    email_from = models.EmailField(blank=True, verbose_name=_("From Email"))
    email_from_name = models.CharField(max_length=100, blank=True, verbose_name=_("From Name"))
    email_cc = models.TextField(blank=True, verbose_name=_("CC Emails"), help_text=_("One email per line"))
    email_subject_template = models.CharField(
        max_length=255,
        default="Invoice {invoice_number} from {company_name}",
        verbose_name=_("Email Subject Template")
    )
    email_message_template = models.TextField(
        blank=True,
        verbose_name=_("Email Message Template"),
        help_text=_("Email body template. Available variables: {client_name}, {invoice_number}, {total}, {due_date}")
    )

    # Footer & Terms
    footer_text = models.TextField(blank=True, verbose_name=_("Footer Text"))
    default_terms = models.TextField(
        blank=True,
        verbose_name=_("Default Terms and Conditions"),
        help_text=_("Default terms and conditions for new invoices")
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Invoice Settings")
        verbose_name_plural = _("Invoice Settings")

    def __str__(self):
        return f"Invoice Settings - {self.company_name or 'Not Configured'}"

    def get_next_invoice_number(self):
        """Generate the next invoice number based on current sequence"""
        # Get the latest invoice number for this year
        current_year = timezone.now().year
        latest_invoice = Invoice.objects.filter(
            invoice_number__startswith=f"{self.invoice_prefix}-{current_year}"
        ).order_by('-created_at').first()

        if latest_invoice:
            # Extract the sequence number from the last invoice
            try:
                parts = latest_invoice.invoice_number.split('-')
                last_sequence = int(parts[-1])
                next_sequence = last_sequence + 1
            except (IndexError, ValueError):
                next_sequence = self.starting_number
        else:
            next_sequence = self.starting_number

        # Format: INV-2025-0001
        return f"{self.invoice_prefix}-{current_year}-{str(next_sequence).zfill(4)}"


class Invoice(models.Model):
    """
    Main invoice model
    """
    STATUS_CHOICES = [
        ('draft', _('Draft')),
        ('sent', _('Sent')),
        ('viewed', _('Viewed')),
        ('partially_paid', _('Partially Paid')),
        ('paid', _('Paid')),
        ('overdue', _('Overdue')),
        ('cancelled', _('Cancelled')),
    ]

    # Statuses that can become overdue once the due date has passed. Drafts,
    # paid and cancelled invoices are never overdue. The stored 'overdue'
    # status (only ever written by update_payment_status) is included so a
    # row that was explicitly flagged still shows up in the overdue filters.
    OVERDUE_ELIGIBLE_STATUSES = ('sent', 'viewed', 'partially_paid', 'overdue')

    # Basic Information
    invoice_number = models.CharField(
        max_length=50,
        unique=True,
        verbose_name=_("Invoice Number")
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='draft',
        verbose_name=_("Status")
    )

    # Client - Link to E-commerce Client (legacy)
    client = models.ForeignKey(
        'ecommerce_crm.EcommerceClient',
        on_delete=models.PROTECT,
        related_name='invoices',
        verbose_name=_("Client"),
        null=True,
        blank=True
    )

    # Client from ItemList (new flexible approach)
    client_itemlist_item = models.ForeignKey(
        'tickets.ListItem',
        on_delete=models.PROTECT,
        related_name='invoices',
        verbose_name=_("Client (ItemList)"),
        null=True,
        blank=True,
        help_text=_("Client from configured ItemList")
    )

    # Store client name for display (denormalized for performance and data preservation)
    client_name = models.CharField(
        max_length=255,
        default='',
        blank=True,
        verbose_name=_("Client Name"),
        help_text=_("Cached client name for display")
    )

    # Dates
    issue_date = models.DateField(default=timezone.now, verbose_name=_("Issue Date"))
    due_date = models.DateField(verbose_name=_("Due Date"))
    sent_date = models.DateTimeField(null=True, blank=True, verbose_name=_("Sent Date"))
    paid_date = models.DateTimeField(null=True, blank=True, verbose_name=_("Paid Date"))

    # Financial Fields
    currency = models.CharField(max_length=3, default='GEL', verbose_name=_("Currency"))
    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Subtotal")
    )
    tax_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Tax Amount")
    )
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Discount Amount")
    )
    total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Total")
    )
    paid_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name=_("Paid Amount")
    )

    # Additional Information
    notes = models.TextField(blank=True, verbose_name=_("Notes"), help_text=_("Internal notes"))
    terms_and_conditions = models.TextField(blank=True, verbose_name=_("Terms and Conditions"))

    # PDF Storage
    pdf_file = models.FileField(
        upload_to='invoices/pdfs/',
        blank=True,
        null=True,
        verbose_name=_("PDF File")
    )
    pdf_generated_at = models.DateTimeField(null=True, blank=True, verbose_name=_("PDF Generated At"))

    # Template
    template = models.ForeignKey(
        'InvoiceTemplate',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoices',
        verbose_name=_("Template")
    )

    # Metadata
    created_by = models.ForeignKey(
        'users.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_invoices',
        verbose_name=_("Created By")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated At"))

    # Unique identifier for external references
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    class Meta:
        verbose_name = _("Invoice")
        verbose_name_plural = _("Invoices")
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['invoice_number']),
            models.Index(fields=['status']),
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['issue_date']),
            models.Index(fields=['due_date']),
        ]

    def __str__(self):
        client_display = self.client_name or str(self.client) if self.client else str(self.client_itemlist_item) if self.client_itemlist_item else 'No Client'
        return f"{self.invoice_number} - {client_display} - {self.get_status_display()}"

    def calculate_totals(self):
        """Calculate subtotal, tax, and total from line items"""
        line_items = self.line_items.all()

        subtotal = sum((item.line_total for item in line_items), Decimal('0.00'))
        tax_amount = sum((item.tax_amount for item in line_items), Decimal('0.00'))

        self.subtotal = subtotal
        self.tax_amount = tax_amount
        self.total = subtotal + tax_amount - (self.discount_amount or Decimal('0.00'))

        return self.total

    def get_balance(self):
        """Get remaining balance to be paid"""
        return self.total - self.paid_amount

    def is_overdue(self):
        """Check if invoice is overdue"""
        if self.status in ['paid', 'cancelled', 'draft']:
            return False
        return timezone.now().date() > self.due_date

    @classmethod
    def overdue_q(cls, today=None):
        """
        Queryset-level equivalent of ``is_overdue()``: open invoices whose due
        date is in the past. Shared by the list filters (``?overdue=true`` /
        ``?status=overdue``) and the stats endpoint so they always agree.
        """
        today = today or timezone.now().date()
        return models.Q(
            due_date__lt=today,
            status__in=cls.OVERDUE_ELIGIBLE_STATUSES,
        )

    def mark_as_paid(self):
        """Mark invoice as paid"""
        self.status = 'paid'
        self.paid_amount = self.total
        self.paid_date = timezone.now()
        self.save()

    def update_payment_status(self):
        """Update status based on paid amount"""
        if self.paid_amount >= self.total:
            self.status = 'paid'
            self.paid_date = timezone.now()
        elif self.paid_amount > 0:
            self.status = 'partially_paid'
        elif self.is_overdue():
            self.status = 'overdue'
        self.save()

    def recompute_paid_amount(self):
        """
        Re-derive ``paid_amount`` (and the payment-driven status) from the
        payment rows. Used after a payment is saved or deleted so the invoice
        never drifts from its ledger.
        """
        total_paid = self.payments.aggregate(
            total=models.Sum('amount')
        )['total'] or Decimal('0.00')

        self.paid_amount = total_paid
        # paid_date is re-set by update_payment_status() when still fully paid.
        self.paid_date = None

        if total_paid <= 0:
            # Every payment is gone: fall back to the pre-payment state. The
            # overdue case is handled by update_payment_status() below.
            if self.status in ('paid', 'partially_paid'):
                self.status = 'sent'
            if self.status in ('draft', 'cancelled'):
                self.save()
                return

        self.update_payment_status()

    def generate_pdf(self):
        """Generate PDF for this invoice"""
        from django.template.loader import render_to_string
        from weasyprint import HTML, CSS
        from django.core.files.base import ContentFile
        import tempfile

        # Get invoice settings for company info
        try:
            settings = InvoiceSettings.objects.first()
        except InvoiceSettings.DoesNotExist:
            settings = None

        # Prepare context for template
        context = {
            'invoice': self,
            'settings': settings,
            'line_items': self.line_items.all().order_by('position'),
            'payments': self.payments.all().order_by('-payment_date'),
        }

        # Render HTML template
        html_string = render_to_string('invoices/invoice_pdf.html', context)

        # Generate PDF using WeasyPrint
        pdf_file = HTML(string=html_string).write_pdf()

        # Save PDF to model
        filename = f"invoice_{self.invoice_number}.pdf"
        self.pdf_file.save(filename, ContentFile(pdf_file), save=False)
        self.pdf_generated_at = timezone.now()
        self.save()

        return self.pdf_file


class InvoiceLineItem(models.Model):
    """
    Individual line items in an invoice
    """
    ITEM_SOURCE_CHOICES = [
        ('product', _('E-commerce Product')),
        ('list_item', _('ItemList Material')),
        ('manual', _('Manual Entry')),
    ]

    invoice = models.ForeignKey(
        'Invoice',
        on_delete=models.CASCADE,
        related_name='line_items',
        verbose_name=_("Invoice")
    )

    # Item Source (can be from Product, ListItem, or manual entry)
    item_source = models.CharField(
        max_length=20,
        choices=ITEM_SOURCE_CHOICES,
        default='manual',
        verbose_name=_("Item Source")
    )
    product = models.ForeignKey(
        'ecommerce_crm.Product',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Product")
    )
    list_item = models.ForeignKey(
        'tickets.ListItem',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("List Item (Material)")
    )

    # Line Item Details
    description = models.CharField(max_length=500, verbose_name=_("Description"))
    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0.01)],
        verbose_name=_("Quantity")
    )
    unit = models.CharField(max_length=50, default='unit', verbose_name=_("Unit"))
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        verbose_name=_("Unit Price")
    )

    # Tax and Discount
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.00,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name=_("Tax Rate (%)")
    )
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.00,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name=_("Discount (%)")
    )

    # Ordering
    position = models.PositiveIntegerField(default=0, verbose_name=_("Position"))

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Invoice Line Item")
        verbose_name_plural = _("Invoice Line Items")
        ordering = ['position', 'id']

    def __str__(self):
        return f"{self.description} x {self.quantity}"

    @property
    def line_subtotal(self):
        """Calculate line subtotal before tax and discount"""
        return self.quantity * self.unit_price

    @property
    def discount_amount(self):
        """Calculate discount amount"""
        return (self.line_subtotal * self.discount_percent) / Decimal('100')

    @property
    def taxable_amount(self):
        """Calculate amount subject to tax (after discount)"""
        return self.line_subtotal - self.discount_amount

    @property
    def tax_amount(self):
        """Calculate tax amount"""
        return (self.taxable_amount * self.tax_rate) / Decimal('100')

    @property
    def line_total(self):
        """Calculate total for this line item (after discount, before tax)"""
        return self.taxable_amount

    def save(self, *args, **kwargs):
        # Auto-populate description from product or list_item if not provided
        if not self.description:
            if self.product:
                self.description = self.product.name
            elif self.list_item:
                self.description = self.list_item.label

        # Auto-populate unit_price from product or list_item if not set
        if not self.unit_price:
            if self.product:
                self.unit_price = self.product.price
            elif self.list_item and self.list_item.custom_data:
                self.unit_price = self.list_item.custom_data.get('price', 0)

        super().save(*args, **kwargs)

        # Update invoice totals
        if self.invoice:
            self.invoice.calculate_totals()
            self.invoice.save()


class InvoicePayment(models.Model):
    """
    Payment records for invoices
    """
    PAYMENT_METHOD_CHOICES = [
        ('card', _('Credit/Debit Card')),
        ('cash', _('Cash')),
        ('bank_transfer', _('Bank Transfer')),
        ('check', _('Check')),
        ('other', _('Other')),
    ]

    invoice = models.ForeignKey(
        'Invoice',
        on_delete=models.CASCADE,
        related_name='payments',
        verbose_name=_("Invoice")
    )

    payment_date = models.DateTimeField(default=timezone.now, verbose_name=_("Payment Date"))
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(0.01)],
        verbose_name=_("Amount")
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        default='bank_transfer',
        verbose_name=_("Payment Method")
    )
    reference_number = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Reference Number"),
        help_text=_("Transaction ID, check number, etc.")
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    # Metadata
    recorded_by = models.ForeignKey(
        'users.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='recorded_payments',
        verbose_name=_("Recorded By")
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Invoice Payment")
        verbose_name_plural = _("Invoice Payments")
        ordering = ['-payment_date']

    def __str__(self):
        return f"Payment {self.amount} for {self.invoice.invoice_number}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        # Update invoice paid amount and status
        if self.invoice:
            self.invoice.recompute_paid_amount()


@receiver(post_delete, sender=InvoicePayment, dispatch_uid='invoices.payment_deleted_recompute')
def recompute_invoice_on_payment_delete(sender, instance, origin=None, **kwargs):
    """
    Deleting (or bulk-deleting) a payment must pull the invoice's paid_amount
    and status back in line with the remaining payments.

    Skipped when the deletion is the cascade of the invoice itself: the invoice
    row is going away, so there is nothing to recompute (and saving it mid-
    delete would fire the invoice notification signals for nothing).
    """
    if isinstance(origin, Invoice):
        return
    if isinstance(origin, models.QuerySet) and origin.model is Invoice:
        return

    invoice = Invoice.objects.filter(pk=instance.invoice_id).first()
    if invoice is None:
        return
    invoice.recompute_paid_amount()


class InvoiceTemplate(models.Model):
    """
    Customizable invoice templates
    """
    name = models.CharField(max_length=100, verbose_name=_("Template Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))

    # Template content (HTML)
    html_content = models.TextField(
        verbose_name=_("HTML Content"),
        help_text=_("HTML template with Django template syntax")
    )

    # CSS styles
    css_styles = models.TextField(
        blank=True,
        verbose_name=_("CSS Styles"),
        help_text=_("CSS styles for the template")
    )

    # Settings
    is_default = models.BooleanField(
        default=False,
        verbose_name=_("Is Default"),
        help_text=_("Use this template as default for new invoices")
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Is Active"))

    # Supported languages
    supported_languages = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Supported Languages"),
        help_text=_("List of language codes (e.g., ['en', 'ka', 'ru'])")
    )

    # Metadata
    created_by = models.ForeignKey(
        'users.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_templates',
        verbose_name=_("Created By")
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Invoice Template")
        verbose_name_plural = _("Invoice Templates")
        ordering = ['-is_default', 'name']

    def __str__(self):
        default_marker = " (Default)" if self.is_default else ""
        return f"{self.name}{default_marker}"

    def save(self, *args, **kwargs):
        # Ensure only one default template
        if self.is_default:
            InvoiceTemplate.objects.filter(is_default=True).update(is_default=False)
        super().save(*args, **kwargs)
