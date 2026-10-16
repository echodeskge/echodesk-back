"""
Invoice Management Serializers
"""

from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field, extend_schema_serializer
from drf_spectacular.types import OpenApiTypes
from .models import (
    InvoiceSettings,
    Invoice,
    InvoiceLineItem,
    InvoicePayment,
    InvoiceTemplate
)
from ecommerce_crm.models import EcommerceClient
from tickets.models import ListItem
from django.db import transaction
from django.utils import timezone
from datetime import timedelta


# ---------------------------------------------------------------------------
# Schema-only serializers
#
# These are never used to parse or render data at runtime; they exist so the
# OpenAPI schema (and the TypeScript clients generated from it) describe the
# ad-hoc dict payloads the views already return / accept.
# ---------------------------------------------------------------------------


class MessageResponseSerializer(serializers.Serializer):
    """``{"message": "..."}`` success payload."""
    message = serializers.CharField()


class ErrorResponseSerializer(serializers.Serializer):
    """``{"error": "..."}`` failure payload used by the invoice actions."""
    error = serializers.CharField()


class ClientItemListDetailsSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    title = serializers.CharField()
    description = serializers.CharField(allow_null=True, allow_blank=True)


class InvoiceClientDetailsSerializer(serializers.Serializer):
    """Shape of ``InvoiceDetail.client_details``."""
    id = serializers.IntegerField(allow_null=True)
    first_name = serializers.CharField(allow_blank=True)
    last_name = serializers.CharField(allow_blank=True)
    email = serializers.CharField(allow_blank=True)
    phone = serializers.CharField(allow_blank=True)
    full_name = serializers.CharField(allow_blank=True)


class InvoiceStatsCurrentMonthSerializer(serializers.Serializer):
    total_invoiced = serializers.FloatField(help_text="Sum of `total` for invoices created this month (all statuses).")
    total_paid = serializers.FloatField(help_text="Sum of `total` for invoices created this month with status paid.")
    count = serializers.IntegerField(help_text="Invoices created this month.")


class InvoiceStatsSerializer(serializers.Serializer):
    """Response of ``GET /api/invoices/invoices/stats/``. Amounts are JSON numbers, not strings."""
    current_month = InvoiceStatsCurrentMonthSerializer()
    outstanding_amount = serializers.FloatField(help_text="Sum of `total` for invoices not paid / cancelled (drafts included).")
    overdue_count = serializers.IntegerField(help_text="Open invoices past their due date (same rule as `?overdue=true`).")


# component_name without the "Request" suffix: COMPONENT_SPLIT_REQUEST adds it.
@extend_schema_serializer(component_name='InvoiceSendEmail')
class InvoiceSendEmailRequestSerializer(serializers.Serializer):
    """Body of ``POST /api/invoices/invoices/{id}/send_email/`` (schema component ``InvoiceSendEmailRequest``)."""
    recipient_email = serializers.EmailField()
    cc_emails = serializers.ListField(
        child=serializers.EmailField(), required=False,
        help_text="List of CC addresses. A comma-separated string is accepted too.",
    )
    subject = serializers.CharField(
        required=False, allow_blank=True,
        help_text="Falls back to settings.email_subject_template. Supports {invoice_number} {company_name} {client_name} {total} {due_date}.",
    )
    message = serializers.CharField(
        required=False, allow_blank=True,
        help_text="Plain-text body; falls back to settings.email_message_template. Same placeholders as subject.",
    )
    attach_pdf = serializers.BooleanField(
        required=False, default=True,
        help_text="Attach the invoice PDF (generated on the fly if it does not exist yet).",
    )


@extend_schema_serializer(component_name='InvoiceMarkPaid')
class InvoiceMarkPaidRequestSerializer(serializers.Serializer):
    """Optional body of ``POST /api/invoices/invoices/{id}/mark_paid/`` (schema component ``InvoiceMarkPaidRequest``)."""
    payment_method = serializers.ChoiceField(
        choices=InvoicePayment.PAYMENT_METHOD_CHOICES, required=False, default='bank_transfer',
    )
    notes = serializers.CharField(required=False, allow_blank=True)


class InvoiceLineItemReorderItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    position = serializers.IntegerField()


@extend_schema_serializer(component_name='InvoiceLineItemReorder')
class InvoiceLineItemReorderRequestSerializer(serializers.Serializer):
    """Body of ``POST /api/invoices/line-items/reorder/`` (schema component ``InvoiceLineItemReorderRequest``)."""
    items = InvoiceLineItemReorderItemSerializer(many=True)


class InvoiceAvailableItemListSerializer(serializers.Serializer):
    """
    Row of ``GET /api/invoices/settings/available-itemlists/`` (the runtime
    output comes from ``tickets.serializers.ItemListMinimalSerializer``; this
    mirrors it so the schema does not collide with the e-commerce serializer
    of the same name).
    """
    id = serializers.IntegerField()
    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    is_active = serializers.BooleanField()
    is_public = serializers.BooleanField()
    parent_list = serializers.IntegerField(allow_null=True)
    custom_fields_schema = serializers.JSONField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    created_by = serializers.DictField(allow_null=True, help_text='Minimal user object (id, email, first_name, last_name, ...).')
    items_count = serializers.IntegerField()


class InvoiceLogoUploadSerializer(serializers.Serializer):
    logo = serializers.FileField()


class InvoiceBadgeUploadSerializer(serializers.Serializer):
    badge = serializers.FileField()


class InvoiceSignatureUploadSerializer(serializers.Serializer):
    signature = serializers.FileField()


# ---------------------------------------------------------------------------
# Runtime serializers
# ---------------------------------------------------------------------------


# ``many=False``: the settings viewset's ``list`` action returns the singleton
# object, not a paginated array, so the schema must not wrap it.
@extend_schema_serializer(many=False)
class InvoiceSettingsSerializer(serializers.ModelSerializer):
    """
    Serializer for InvoiceSettings model
    """
    client_itemlist_details = serializers.SerializerMethodField()

    class Meta:
        model = InvoiceSettings
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')

    @extend_schema_field(ClientItemListDetailsSerializer(allow_null=True))
    def get_client_itemlist_details(self, obj):
        """Return detailed information about the client item list"""
        if obj.client_itemlist:
            return {
                'id': obj.client_itemlist.id,
                'title': obj.client_itemlist.title,
                'description': obj.client_itemlist.description
            }
        return None

    def validate_bank_accounts(self, value):
        """Validate bank accounts JSON structure"""
        if not isinstance(value, list):
            raise serializers.ValidationError("Bank accounts must be a list")

        for account in value:
            required_fields = ['bank_name', 'account_number']
            for field in required_fields:
                if field not in account:
                    raise serializers.ValidationError(
                        f"Each bank account must have '{field}' field"
                    )

        return value


class InvoiceLineItemSerializer(serializers.ModelSerializer):
    """
    Serializer for InvoiceLineItem model
    """
    line_subtotal = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    taxable_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    tax_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    line_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    # Include related object names
    product_name = serializers.CharField(source='product.name', read_only=True, default=None, allow_null=True)
    list_item_label = serializers.CharField(source='list_item.label', read_only=True, default=None, allow_null=True)

    class Meta:
        model = InvoiceLineItem
        exclude = ['invoice']  # Exclude invoice when used as nested serializer
        read_only_fields = ('created_at', 'updated_at')

    def validate(self, data):
        """Validate that at least one source is provided or manual entry is complete"""
        item_source = data.get('item_source', 'manual')

        if item_source == 'product' and not data.get('product'):
            raise serializers.ValidationError(
                "Product is required when item_source is 'product'"
            )

        if item_source == 'list_item' and not data.get('list_item'):
            raise serializers.ValidationError(
                "List item is required when item_source is 'list_item'"
            )

        if item_source == 'manual' and not data.get('description'):
            raise serializers.ValidationError(
                "Description is required for manual entries"
            )

        return data


class InvoicePaymentSerializer(serializers.ModelSerializer):
    """
    Serializer for InvoicePayment model
    """
    recorded_by_name = serializers.CharField(
        source='recorded_by.get_full_name', read_only=True, allow_null=True, default=None,
    )

    class Meta:
        model = InvoicePayment
        fields = '__all__'
        read_only_fields = ('recorded_by', 'created_at', 'updated_at')

    def validate(self, data):
        """
        A payment can never exceed what is still owed on the invoice, and
        cancelled invoices cannot take payments at all. On update the
        payment's own previous amount is given back before comparing.
        """
        invoice = data.get('invoice') or (self.instance.invoice if self.instance else None)
        amount = data.get('amount', self.instance.amount if self.instance else None)
        if invoice is None or amount is None:
            return data

        if invoice.status == 'cancelled':
            raise serializers.ValidationError(
                {'invoice': 'Cannot record a payment on a cancelled invoice'}
            )

        balance = invoice.get_balance()
        if self.instance and self.instance.invoice_id == invoice.id:
            balance += self.instance.amount

        if amount > balance:
            raise serializers.ValidationError(
                {'amount': f'Amount exceeds the remaining balance of {balance:.2f}'}
            )

        return data


class InvoiceListSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for invoice lists
    """
    client_name = serializers.SerializerMethodField()
    balance = serializers.DecimalField(max_digits=12, decimal_places=2, source='get_balance', read_only=True)
    is_overdue = serializers.SerializerMethodField()
    line_items_count = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'uuid', 'invoice_number', 'status', 'client', 'client_name',
            'issue_date', 'due_date', 'currency', 'total', 'paid_amount',
            'balance', 'is_overdue', 'line_items_count', 'created_at'
        ]

    @extend_schema_field(OpenApiTypes.STR)
    def get_client_name(self, obj):
        """Get full client name - handles both EcommerceClient and ItemList clients"""
        if obj.client_itemlist_item:
            return obj.client_itemlist_item.label
        elif obj.client:
            return f"{obj.client.first_name} {obj.client.last_name}".strip() or obj.client.email
        else:
            return obj.client_name or 'Unknown Client'

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_is_overdue(self, obj):
        """Check if invoice is overdue"""
        return obj.is_overdue()

    @extend_schema_field(OpenApiTypes.INT)
    def get_line_items_count(self, obj):
        """Get number of line items (uses annotated count)"""
        return getattr(obj, 'line_items_count', obj.line_items.count())


class InvoiceDetailSerializer(serializers.ModelSerializer):
    """
    Detailed serializer for invoice with all related data
    """
    client_details = serializers.SerializerMethodField()
    line_items = InvoiceLineItemSerializer(many=True, read_only=True)
    payments = InvoicePaymentSerializer(many=True, read_only=True)
    balance = serializers.DecimalField(max_digits=12, decimal_places=2, source='get_balance', read_only=True)
    is_overdue = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(
        source='created_by.get_full_name', read_only=True, allow_null=True, default=None,
    )
    template_name = serializers.CharField(
        source='template.name', read_only=True, allow_null=True, default=None,
    )
    pdf_url = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = '__all__'
        read_only_fields = (
            'invoice_number', 'subtotal', 'tax_amount', 'total',
            'paid_amount', 'uuid', 'created_by', 'created_at', 'updated_at',
            'pdf_generated_at'
        )

    @extend_schema_field(InvoiceClientDetailsSerializer)
    def get_client_details(self, obj):
        """Get full client details - handles both EcommerceClient and ItemList clients"""
        if obj.client_itemlist_item:
            # Client from ItemList
            item = obj.client_itemlist_item
            custom_data = item.custom_data or {}
            return {
                'id': item.id,
                'first_name': '',
                'last_name': '',
                'email': custom_data.get('email', ''),
                'phone': custom_data.get('phone', ''),
                'full_name': item.label
            }
        elif obj.client:
            # Client from EcommerceClient
            client = obj.client
            return {
                'id': client.id,
                'first_name': client.first_name,
                'last_name': client.last_name,
                'email': client.email,
                'phone': getattr(client, 'phone_number', '') or '',
                'full_name': f"{client.first_name} {client.last_name}".strip() or client.email
            }
        else:
            # Fallback - use cached client_name
            return {
                'id': None,
                'first_name': '',
                'last_name': '',
                'email': '',
                'phone': '',
                'full_name': obj.client_name
            }

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_is_overdue(self, obj):
        """Check if invoice is overdue"""
        return obj.is_overdue()

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_pdf_url(self, obj):
        """Get PDF file URL if exists"""
        if obj.pdf_file:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.pdf_file.url)
        return None


class InvoiceCreateUpdateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating/updating invoices
    """
    line_items = InvoiceLineItemSerializer(many=True, required=False)
    # Accept client as an integer - will be interpreted based on InvoiceSettings
    client = serializers.IntegerField(write_only=True)

    class Meta:
        model = Invoice
        fields = [
            'client', 'issue_date', 'due_date', 'currency', 'discount_amount',
            'notes', 'terms_and_conditions', 'template', 'line_items'
        ]

    def validate_client(self, value):
        """
        Validate client ID and determine if it's from EcommerceClient or ListItem
        based on InvoiceSettings configuration
        """
        from tickets.models import ListItem
        from ecommerce_crm.models import EcommerceClient

        # Get invoice settings to determine client source
        settings, _ = InvoiceSettings.objects.get_or_create()

        if settings.client_itemlist:
            # Client should be from ItemList
            try:
                client = ListItem.objects.get(id=value, item_list=settings.client_itemlist)
                # Store for use in create()
                self.context['client_type'] = 'itemlist'
                self.context['client_obj'] = client
                return value
            except ListItem.DoesNotExist:
                raise serializers.ValidationError(f"Client with ID {value} not found in configured ItemList")
        else:
            # Client should be from EcommerceClient
            try:
                client = EcommerceClient.objects.get(id=value)
                self.context['client_type'] = 'ecommerce'
                self.context['client_obj'] = client
                return value
            except EcommerceClient.DoesNotExist:
                raise serializers.ValidationError(f"Client with ID {value} not found")

    def validate(self, data):
        """Validate invoice data"""
        # Validate due date is after issue date
        issue_date = data.get('issue_date')
        due_date = data.get('due_date')

        if issue_date and due_date and due_date < issue_date:
            raise serializers.ValidationError(
                {'due_date': 'Due date must be after issue date'}
            )

        return data

    def create(self, validated_data):
        """Create invoice with line items"""
        line_items_data = validated_data.pop('line_items', [])
        validated_data.pop('client', None)  # client is resolved from context below

        # Get client info from context (set in validate_client)
        client_type = self.context.get('client_type')
        client_obj = self.context.get('client_obj')

        # The whole create runs in one transaction so a failure part-way (bad
        # line item, totals error) can't leave a half-built invoice behind, and
        # so number generation + insert are atomic.
        with transaction.atomic():
            # Lock the settings singleton so concurrent invoice creations
            # serialize on number generation. invoice_number is unique=True;
            # without this lock two racing creates produce the same number and
            # one of them 500s on the unique constraint.
            settings = InvoiceSettings.objects.select_for_update().first()
            if not settings:
                settings = InvoiceSettings.objects.create()

            # Generate invoice number (under the settings lock)
            invoice_number = settings.get_next_invoice_number()

            # Set due date if not provided
            if 'due_date' not in validated_data:
                validated_data['due_date'] = validated_data.get('issue_date', timezone.now().date()) + timedelta(days=settings.default_due_days)

            # Set currency if not provided
            if 'currency' not in validated_data:
                validated_data['currency'] = settings.default_currency

            # Set terms and conditions if not provided
            if not validated_data.get('terms_and_conditions'):
                validated_data['terms_and_conditions'] = settings.default_terms

            # Set client fields based on type
            if client_type == 'itemlist':
                validated_data['client_itemlist_item'] = client_obj
                validated_data['client'] = None
                validated_data['client_name'] = client_obj.label
            else:
                validated_data['client'] = client_obj
                validated_data['client_itemlist_item'] = None
                validated_data['client_name'] = f"{client_obj.first_name} {client_obj.last_name}".strip() or client_obj.email

            # Create invoice
            invoice = Invoice.objects.create(
                invoice_number=invoice_number,
                **validated_data
            )

            # Create line items
            for item_data in line_items_data:
                InvoiceLineItem.objects.create(invoice=invoice, **item_data)

            # Calculate totals
            invoice.calculate_totals()
            invoice.save()

        return invoice

    def update(self, instance, validated_data):
        """Update invoice and line items"""
        line_items_data = validated_data.pop('line_items', None)

        # ``client`` is a bare integer resolved by validate_client(); it must
        # not be setattr'd onto the FK (Django rejects "client = 5"). Assign
        # whichever client object the settings say the id refers to.
        validated_data.pop('client', None)
        client_type = self.context.get('client_type')
        client_obj = self.context.get('client_obj')
        if client_obj is not None:
            if client_type == 'itemlist':
                instance.client_itemlist_item = client_obj
                instance.client = None
                instance.client_name = client_obj.label
            else:
                instance.client = client_obj
                instance.client_itemlist_item = None
                instance.client_name = (
                    f"{client_obj.first_name} {client_obj.last_name}".strip() or client_obj.email
                )

        # Update invoice fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        # Update line items if provided
        if line_items_data is not None:
            # Delete existing line items
            instance.line_items.all().delete()

            # Create new line items
            for item_data in line_items_data:
                InvoiceLineItem.objects.create(invoice=instance, **item_data)

        # Recalculate totals
        instance.calculate_totals()
        instance.save()

        return instance


class InvoiceTemplateSerializer(serializers.ModelSerializer):
    """
    Serializer for InvoiceTemplate model
    """
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)

    class Meta:
        model = InvoiceTemplate
        fields = '__all__'
        read_only_fields = ('created_by', 'created_at', 'updated_at')

    def validate_supported_languages(self, value):
        """Validate supported languages list"""
        if not isinstance(value, list):
            raise serializers.ValidationError("Supported languages must be a list")

        valid_languages = ['en', 'ka', 'ru']
        for lang in value:
            if lang not in valid_languages:
                raise serializers.ValidationError(
                    f"Invalid language code '{lang}'. Valid codes are: {', '.join(valid_languages)}"
                )

        return value


# Named ``InvoiceClient`` in the schema: ``crm.serializers.ClientSerializer``
# already owns the ``Client`` component and the two shapes differ.
@extend_schema_serializer(component_name='InvoiceClient')
class ClientSerializer(serializers.Serializer):
    """
    Unified serializer for client selection - works with both EcommerceClient and ListItem
    """
    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    email = serializers.SerializerMethodField()
    phone = serializers.SerializerMethodField()

    @extend_schema_field(OpenApiTypes.STR)
    def get_name(self, obj):
        """Get client name from either EcommerceClient or ListItem"""
        if hasattr(obj, 'label'):
            # ListItem - use label
            return obj.label
        else:
            # EcommerceClient - use full name
            return f"{obj.first_name} {obj.last_name}".strip() or obj.email

    @extend_schema_field(OpenApiTypes.STR)
    def get_email(self, obj):
        """Get email if available"""
        if hasattr(obj, 'custom_data') and obj.custom_data:
            # ListItem - check custom_data
            return obj.custom_data.get('email', '')
        elif hasattr(obj, 'email'):
            # EcommerceClient
            return obj.email
        return ''

    @extend_schema_field(OpenApiTypes.STR)
    def get_phone(self, obj):
        """Get phone if available"""
        if hasattr(obj, 'custom_data') and obj.custom_data:
            # ListItem - check custom_data
            return obj.custom_data.get('phone', '')
        elif hasattr(obj, 'phone'):
            # EcommerceClient
            return obj.phone
        return ''


class ListItemMaterialSerializer(serializers.ModelSerializer):
    """
    Serializer for list items (materials) for invoice selection
    """
    price = serializers.SerializerMethodField()
    unit = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()

    class Meta:
        model = ListItem
        fields = ['id', 'label', 'custom_data', 'price', 'unit', 'description']

    @extend_schema_field({
        'oneOf': [{'type': 'number'}, {'type': 'string'}],
        'description': 'custom_data.price as stored in the ItemList (number or numeric string); 0 when missing.',
    })
    def get_price(self, obj):
        """Extract price from custom_data"""
        return obj.custom_data.get('price', 0) if obj.custom_data else 0

    @extend_schema_field(OpenApiTypes.STR)
    def get_unit(self, obj):
        """Extract unit from custom_data"""
        return obj.custom_data.get('unit', 'unit') if obj.custom_data else 'unit'

    @extend_schema_field(OpenApiTypes.STR)
    def get_description(self, obj):
        """Extract description from custom_data"""
        return obj.custom_data.get('description', '') if obj.custom_data else ''
