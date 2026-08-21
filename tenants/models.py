from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.conf import settings
from datetime import timedelta
from tenant_schemas.models import TenantMixin
from amanati_crm.file_utils import sanitized_upload_to

# Import feature models
from .feature_models import (
    Feature, FeaturePermission,
    TenantFeature, TenantPermission,
    FeatureCategory
)
from django.contrib.auth.models import Permission




PAYMENT_PROVIDER_CHOICES = [
    ('bog', 'Bank of Georgia'),
    ('paddle', 'Paddle'),
]


class Tenant(TenantMixin):
    """
    Tenant model for multi-tenancy support.
    Each tenant represents a separate organization/company.
    """
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    created_on = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    # Owner/admin contact information
    admin_email = models.EmailField()
    admin_name = models.CharField(max_length=100)

    # Payment provider
    payment_provider = models.CharField(
        max_length=20,
        choices=PAYMENT_PROVIDER_CHOICES,
        default='bog',
        help_text='Payment provider used for subscription billing'
    )
    paddle_customer_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text='Paddle customer ID for this tenant'
    )
    
    # Legacy plan field (kept for backward compatibility)
    plan = models.CharField(
        max_length=50, 
        default='basic',
        choices=[
            ('basic', 'Basic'),
            ('premium', 'Premium'),
            ('enterprise', 'Enterprise'),
        ]
    )
    
    # Legacy limits (kept for backward compatibility)
    max_users = models.IntegerField(default=10)
    max_storage = models.BigIntegerField(default=1073741824)  # 1GB in bytes
    
    # Language preference for tenant dashboard
    preferred_language = models.CharField(
        max_length=10,
        default='en',
        choices=[
            ('en', 'English'),
            ('ru', 'Russian'),
            ('ka', 'Georgian'),
        ],
        help_text='Preferred language for the frontend dashboard'
    )

    # Branding
    logo = models.ImageField(upload_to=sanitized_upload_to('tenant_logos', date_based=False), blank=True, null=True, help_text="Company logo")

    # Ticket settings
    min_users_per_ticket = models.IntegerField(
        default=0,
        help_text="Minimum number of users required on a ticket after initial assignment. 0 = no minimum. Only superadmins can reduce below this number."
    )
    only_superadmin_can_delete_tickets = models.BooleanField(
        default=False,
        help_text="If True, only superadmins can delete tickets. If False, both ticket owners and superadmins can delete tickets."
    )

    # IP Whitelist settings
    ip_whitelist_enabled = models.BooleanField(
        default=False,
        help_text="If True, only whitelisted IPs can access this tenant"
    )
    superadmin_bypass_whitelist = models.BooleanField(
        default=True,
        help_text="If True, superadmins can bypass IP whitelist restrictions"
    )

    # Frontend deployment fields
    frontend_url = models.URLField(blank=True, null=True, help_text="URL of the deployed frontend")
    vercel_project_id = models.CharField(max_length=100, blank=True, null=True, help_text="Vercel project ID for frontend deployment")
    deployment_status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('deploying', 'Deploying'),
            ('deployed', 'Deployed'),
            ('failed', 'Failed'),
        ],
        default='pending',
        help_text="Status of the frontend deployment"
    )
    
    # Auto-created schema name is available as self.schema_name
    # domain_url inherited from TenantMixin

    auto_create_schema = False  # Handle schema creation manually to avoid webhook timeouts
    auto_drop_schema = False
    
    class Meta:
        db_table = 'tenants_tenant'
    
    def __str__(self):
        return f"{self.name} ({self.schema_name})"
    
    @property
    def is_public_schema(self):
        """Check if this is the public schema"""
        return self.schema_name == 'public'
    
    @property
    def current_package(self):
        """Get current package from subscription"""
        try:
            return self.subscription.package
        except:
            return None
    
    @property
    def current_subscription(self):
        """Get current subscription"""
        try:
            return self.subscription
        except:
            return None


class TenantSubscription(models.Model):
    """
    Tracks tenant's current subscription and usage
    """
    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name='subscription')

    # Feature-based subscription
    selected_features = models.ManyToManyField(
        Feature,
        related_name='subscriptions',
        blank=True,
        help_text="Features selected for this subscription"
    )
    agent_count = models.IntegerField(
        default=10,
        help_text="Number of agents (in increments of 10: 10, 20, 30... 200)"
    )

    # Subscription status
    is_active = models.BooleanField(default=True)
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True, blank=True)

    # Usage tracking
    current_users = models.IntegerField(default=0)
    whatsapp_messages_used = models.IntegerField(default=0)
    storage_used_gb = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Billing
    last_billed_at = models.DateTimeField(null=True, blank=True)
    next_billing_date = models.DateTimeField(null=True, blank=True)

    # Trial information
    is_trial = models.BooleanField(default=False, help_text='Whether this is a trial subscription')
    trial_ends_at = models.DateTimeField(null=True, blank=True, help_text='When the trial period ends')
    trial_converted = models.BooleanField(default=False, help_text='Whether trial was converted to paid subscription')

    # Saved card for recurring payments
    parent_order_id = models.CharField(max_length=100, blank=True, null=True, help_text='BOG order ID with saved card')

    # Provider subscription ID (Paddle subscription_id, etc.)
    provider_subscription_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text='External provider subscription ID (e.g., Paddle sub_xxx)'
    )

    # Payment health tracking
    payment_status = models.CharField(
        max_length=20,
        choices=[
            ('current', 'Current'),  # All payments successful
            ('overdue', 'Overdue'),  # Payment due but not paid
            ('retrying', 'Retrying'),  # Failed payment, retry in progress
            ('failed', 'Failed'),  # All retries exhausted
            ('no_card', 'No Card'),  # No saved card on file
        ],
        default='current',
        help_text='Current payment health status'
    )
    last_payment_failure = models.DateTimeField(null=True, blank=True, help_text='When last payment failed')
    failed_payment_count = models.IntegerField(default=0, help_text='Number of consecutive failed payments')

    subscription_type = models.CharField(
        max_length=20,
        choices=[
            ('trial', 'Trial'),
            ('paid', 'Paid'),
        ],
        default='paid',
        help_text='Current subscription type'
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'tenants_tenant_subscription'
    
    def __str__(self):
        feature_count = self.selected_features.count()
        return f"{self.tenant.name} - {self.agent_count} agents, {feature_count} features"
    
    @property
    def monthly_cost(self):
        """Calculate monthly cost based on selected features and agent count"""
        # Feature-based pricing model
        if self.selected_features.exists():
            total_cost_per_agent = sum(
                feature.price_per_user_gel
                for feature in self.selected_features.all()
            )
            return total_cost_per_agent * self.agent_count

        # No features = free
        return 0
    
    @property
    def is_over_user_limit(self):
        """Check if tenant is over user limit"""
        # Check against agent_count
        return self.current_users > self.agent_count
    
    @property
    def is_over_whatsapp_limit(self):
        """Check if tenant is over WhatsApp message limit"""
        # Default 10k limit
        return self.whatsapp_messages_used > 10000

    @property
    def is_over_storage_limit(self):
        """Check if tenant is over storage limit"""
        # Default 100GB limit
        return self.storage_used_gb > 100
    
    def can_add_user(self):
        """Check if tenant can add another user"""
        # Check against agent_count
        return self.current_users < self.agent_count
    
    def can_send_whatsapp_message(self):
        """Check if tenant can send WhatsApp message (deprecated - always returns True)"""
        return True  # WhatsApp limits removed

    @property
    def payment_health_status(self):
        """Get user-friendly payment health status for admin display"""
        status_map = {
            'current': '🟢 Current',
            'overdue': '🟡 Overdue',
            'retrying': '🟠 Retrying',
            'failed': '🔴 Failed',
            'no_card': '⚪ No Card',
        }
        return status_map.get(self.payment_status, self.payment_status)

    @property
    def has_payment_issues(self):
        """Check if subscription has any payment issues"""
        return self.payment_status in ['overdue', 'retrying', 'failed']

    @property
    def days_until_next_billing(self):
        """Calculate days until next billing"""
        if not self.next_billing_date:
            return None
        delta = self.next_billing_date - timezone.now()
        return delta.days

    def mark_payment_failed(self):
        """Mark a payment as failed and update counters"""
        self.failed_payment_count += 1
        self.last_payment_failure = timezone.now()
        self.payment_status = 'retrying'
        self.save(update_fields=['failed_payment_count', 'last_payment_failure', 'payment_status'])

    def mark_payment_succeeded(self):
        """Mark a payment as succeeded and reset failure counter"""
        self.failed_payment_count = 0
        self.last_payment_failure = None
        self.payment_status = 'current'
        self.save(update_fields=['failed_payment_count', 'last_payment_failure', 'payment_status'])


class UsageLog(models.Model):
    """
    Track usage events for billing and analytics
    """
    subscription = models.ForeignKey(TenantSubscription, on_delete=models.CASCADE, related_name='usage_logs')

    event_type = models.CharField(
        max_length=50,
        choices=[
            ('user_added', 'User Added'),
            ('user_removed', 'User Removed'),
            ('whatsapp_message', 'WhatsApp Message'),
            ('storage_usage', 'Storage Usage'),
            ('feature_used', 'Feature Used'),
        ]
    )

    quantity = models.IntegerField(default=1)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tenants_usage_log'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.subscription.tenant.name} - {self.event_type} ({self.quantity})"


class SavedCard(models.Model):
    """
    Store saved payment card details from Bank of Georgia for recurring payments
    """
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='saved_cards',
        help_text='Tenant that owns this saved card'
    )

    # BOG order ID for recurring charges
    parent_order_id = models.CharField(
        max_length=100,
        unique=True,
        help_text='BOG parent order ID used for recurring payments'
    )

    # Card save type - determines which BOG endpoint was used
    card_save_type = models.CharField(
        max_length=20,
        choices=[
            ('subscription', 'Subscription (fixed amount)'),
            ('ecommerce', 'Ecommerce (variable amount)'),
        ],
        default='subscription',
        help_text='Type of card save: subscription (BOG /subscriptions endpoint - fixed recurring amount) or ecommerce (BOG /cards endpoint - variable amounts)'
    )

    # Card details (masked/safe to store)
    card_type = models.CharField(
        max_length=20,
        blank=True,
        help_text='Card type (e.g., mc, visa)'
    )
    masked_card_number = models.CharField(
        max_length=20,
        blank=True,
        help_text='Masked card number (e.g., 531125***1450)'
    )
    card_expiry = models.CharField(
        max_length=7,
        blank=True,
        help_text='Card expiry date (MM/YY format)'
    )

    # Payment metadata
    transaction_id = models.CharField(
        max_length=100,
        blank=True,
        help_text='Initial transaction ID'
    )
    saved_at = models.DateTimeField(
        auto_now_add=True,
        help_text='When the card was saved'
    )

    # Status
    is_active = models.BooleanField(
        default=True,
        help_text='Whether this card is active for recurring payments'
    )
    is_default = models.BooleanField(
        default=False,
        help_text='Whether this is the default card for automatic payments'
    )

    class Meta:
        db_table = 'tenants_saved_card'
        verbose_name = 'Saved Card'
        verbose_name_plural = 'Saved Cards'
        ordering = ['-is_default', '-saved_at']

    def __str__(self):
        card_display = f"{self.card_type.upper()} {self.masked_card_number}" if self.masked_card_number else "Card"
        default_indicator = " (Default)" if self.is_default else ""
        return f"{self.tenant.name} - {card_display}{default_indicator}"

    def save(self, *args, **kwargs):
        # If this card is being set as default, unset other default cards for this tenant
        if self.is_default:
            SavedCard.objects.filter(tenant=self.tenant, is_default=True).exclude(id=self.id).update(is_default=False)

        # If this is the first active card for the tenant, make it default
        if self.is_active and not SavedCard.objects.filter(tenant=self.tenant, is_active=True).exclude(id=self.id).exists():
            self.is_default = True

        super().save(*args, **kwargs)


class PaymentOrder(models.Model):
    """
    Track payment orders and metadata for subscription payments
    """
    order_id = models.CharField(max_length=100, unique=True, db_index=True)
    bog_order_id = models.CharField(max_length=100, blank=True, null=True, help_text='BOG internal order ID for saved card charging')
    provider_order_id = models.CharField(
        max_length=100, blank=True, null=True,
        help_text='Generic provider order/transaction ID (Paddle txn_xxx, etc.)'
    )
    payment_provider = models.CharField(
        max_length=20,
        choices=PAYMENT_PROVIDER_CHOICES,
        default='bog',
        help_text='Payment provider used for this order'
    )
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, null=True, blank=True)

    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='GEL')
    agent_count = models.IntegerField(default=1, help_text='DEPRECATED: Agent count for old pricing model')

    status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('paid', 'Paid'),
            ('failed', 'Failed'),
            ('cancelled', 'Cancelled'),
        ],
        default='pending'
    )

    payment_url = models.URLField(max_length=500, blank=True)
    card_saved = models.BooleanField(default=False, help_text='Whether card was saved for recurring payments')
    is_trial_payment = models.BooleanField(default=False, help_text='Whether this is a 0 GEL trial payment')

    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'tenants_payment_order'
        ordering = ['-created_at']

    def __str__(self):
        tenant_name = self.tenant.name if self.tenant else "Registration"
        return f"{self.order_id} - {tenant_name} - {self.status}"


class Invoice(models.Model):
    """
    Invoice generated for successful payments
    """
    invoice_number = models.CharField(max_length=100, unique=True, db_index=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    payment_order = models.OneToOneField(PaymentOrder, on_delete=models.CASCADE, related_name='invoice')

    # Invoice details
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='GEL')

    # What was invoiced for
    description = models.TextField()
    agent_count = models.IntegerField(default=1)

    # Dates
    invoice_date = models.DateTimeField(auto_now_add=True)
    due_date = models.DateField(null=True, blank=True)
    paid_date = models.DateTimeField(null=True, blank=True)

    # PDF generation
    pdf_generated = models.BooleanField(default=False)
    pdf_url = models.URLField(max_length=500, blank=True)

    # Additional data
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'tenants_invoice'
        ordering = ['-invoice_date']

    def __str__(self):
        return f"Invoice {self.invoice_number} - {self.tenant.name} - {self.amount} {self.currency}"

    def generate_invoice_number(self):
        """Generate a unique invoice number"""
        date_str = timezone.now().strftime('%Y%m')
        # Find the last invoice for this month
        last_invoice = Invoice.objects.filter(
            invoice_number__startswith=f'INV-{date_str}'
        ).order_by('-invoice_number').first()

        if last_invoice:
            # Extract the sequence number and increment
            try:
                last_seq = int(last_invoice.invoice_number.split('-')[-1])
                new_seq = last_seq + 1
            except (ValueError, IndexError):
                new_seq = 1
        else:
            new_seq = 1

        return f'INV-{date_str}-{new_seq:04d}'

    def save(self, *args, **kwargs):
        # If a number is already set, save normally.
        if self.invoice_number:
            return super().save(*args, **kwargs)

        # Auto-generate a unique invoice number. There is no settings singleton
        # to lock on for this public-schema invoice, and locking the matching
        # rows can't serialize the first-of-month insert (empty set). So instead
        # retry on the unique-constraint collision: each attempt regenerates from
        # the current max, so a loser simply picks the next free number. Each
        # attempt is its own savepoint so a collision doesn't poison an
        # enclosing transaction (e.g. the payment webhook's atomic block).
        from django.db import IntegrityError, transaction
        last_error = None
        for _ in range(5):
            self.invoice_number = self.generate_invoice_number()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError as exc:
                last_error = exc
                self.invoice_number = None
                continue
        raise last_error


class PendingRegistration(models.Model):
    """
    Stores tenant registration data temporarily until payment is completed
    """
    schema_name = models.CharField(max_length=63, unique=True, db_index=True)
    name = models.CharField(max_length=100)
    admin_email = models.EmailField()
    admin_password = models.CharField(max_length=255)  # Will be hashed
    admin_first_name = models.CharField(max_length=100)
    admin_last_name = models.CharField(max_length=100)
    preferred_language = models.CharField(max_length=5, default='en', help_text='User preferred language (en, ka, ru)')

    # Feature-based pricing
    selected_features = models.ManyToManyField(Feature, blank=True, help_text='Selected features for feature-based pricing')
    agent_count = models.IntegerField(default=10, help_text='Number of agents (10-200 in increments of 10)')
    order_id = models.CharField(max_length=100, unique=True, db_index=True, blank=True, default='')

    # Payment provider
    payment_provider = models.CharField(
        max_length=20,
        choices=PAYMENT_PROVIDER_CHOICES,
        default='bog',
        help_text='Payment provider chosen during registration'
    )

    # Status tracking
    is_processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = 'tenants_pending_registration'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.schema_name} - {self.name} - {'Processed' if self.is_processed else 'Pending'}"

    def save(self, *args, **kwargs):
        # Set expiration to 1 hour from now if not set
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(hours=1)
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        """Check if registration has expired"""
        return timezone.now() > self.expires_at

    def can_process(self):
        """Check if this registration can be processed"""
        return not self.is_processed and not self.is_expired


class PaymentAttempt(models.Model):
    """
    Tracks every payment attempt made through BOG
    Links to PaymentOrder and tracks success/failure with detailed BOG response
    """
    payment_order = models.ForeignKey(
        'PaymentOrder',
        on_delete=models.CASCADE,
        related_name='attempts'
    )
    subscription = models.ForeignKey(
        'TenantSubscription',
        on_delete=models.CASCADE,
        related_name='payment_attempts',
        null=True,
        blank=True
    )
    tenant = models.ForeignKey(
        'Tenant',
        on_delete=models.CASCADE,
        related_name='payment_attempts'
    )

    # Attempt tracking
    attempt_number = models.IntegerField(
        default=1,
        help_text='Attempt number (1 for initial, 2+ for retries)'
    )
    status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('success', 'Success'),
            ('failed', 'Failed'),
            ('cancelled', 'Cancelled'),
        ],
        default='pending'
    )

    # BOG data
    bog_order_id = models.CharField(max_length=255, db_index=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    # Timing
    attempted_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Failure details
    failed_reason = models.TextField(blank=True)
    bog_error_code = models.CharField(max_length=50, blank=True, default='')
    bog_response = models.JSONField(default=dict, blank=True)

    # Retry tracking
    is_retry = models.BooleanField(default=False)
    parent_attempt = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='retry_attempts'
    )

    class Meta:
        db_table = 'tenants_payment_attempt'
        ordering = ['-attempted_at']
        indexes = [
            models.Index(fields=['tenant', 'status']),
            models.Index(fields=['subscription', 'attempted_at']),
            models.Index(fields=['bog_order_id']),
        ]

    def __str__(self):
        return f"Attempt #{self.attempt_number} - {self.tenant.name} - {self.status} - {self.amount} GEL"

    @property
    def duration(self):
        """Calculate attempt duration"""
        if self.completed_at:
            return (self.completed_at - self.attempted_at).total_seconds()
        return None


class SubscriptionEvent(models.Model):
    """
    Logs all subscription-related events for audit trail and timeline
    """
    EVENT_TYPES = [
        ('created', 'Created'),
        ('activated', 'Activated'),
        ('payment_success', 'Payment Succeeded'),
        ('payment_failed', 'Payment Failed'),
        ('retry_scheduled', 'Retry Scheduled'),
        ('retry_success', 'Retry Succeeded'),
        ('retry_failed', 'Retry Failed'),
        ('suspended', 'Suspended'),
        ('reactivated', 'Reactivated'),
        ('cancelled', 'Cancelled'),
        ('upgraded', 'Upgraded'),
        ('downgraded', 'Downgraded'),
        ('card_updated', 'Card Updated'),
        ('card_added', 'Card Added'),
        ('card_removed', 'Card Removed'),
        ('trial_started', 'Trial Started'),
        ('trial_converted', 'Trial Converted'),
        ('trial_expired', 'Trial Expired'),
        ('feature_added', 'Feature Added'),
        ('feature_removed', 'Feature Removed'),
        ('billing_date_changed', 'Billing Date Changed'),
    ]

    subscription = models.ForeignKey(
        'TenantSubscription',
        on_delete=models.CASCADE,
        related_name='events'
    )
    tenant = models.ForeignKey(
        'Tenant',
        on_delete=models.CASCADE,
        related_name='subscription_events'
    )
    event_type = models.CharField(max_length=30, choices=EVENT_TYPES, db_index=True)

    # Related objects
    payment_order = models.ForeignKey(
        'PaymentOrder',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='events'
    )
    payment_attempt = models.ForeignKey(
        'PaymentAttempt',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='events'
    )

    # Event data
    old_value = models.JSONField(null=True, blank=True, help_text='Previous state for upgrades/downgrades')
    new_value = models.JSONField(null=True, blank=True, help_text='New state for upgrades/downgrades')
    metadata = models.JSONField(default=dict, blank=True)
    description = models.TextField()

    # Tracking
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text='Superadmin who triggered this event (if manual)'
    )

    class Meta:
        db_table = 'tenants_subscription_event'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['subscription', '-created_at']),
            models.Index(fields=['tenant', 'event_type']),
            models.Index(fields=['event_type', '-created_at']),
        ]

    def __str__(self):
        return f"{self.tenant.name} - {self.get_event_type_display()} - {self.created_at.strftime('%Y-%m-%d %H:%M')}"


class PaymentRetrySchedule(models.Model):
    """
    Manages automatic retry scheduling for failed payments
    Implements smart retry logic: +4hrs, +3days, +7days
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('executing', 'Executing'),
        ('succeeded', 'Succeeded'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
        ('cancelled', 'Cancelled'),
    ]

    payment_order = models.ForeignKey(
        'PaymentOrder',
        on_delete=models.CASCADE,
        related_name='retry_schedules'
    )
    subscription = models.ForeignKey(
        'TenantSubscription',
        on_delete=models.CASCADE,
        related_name='retry_schedules'
    )
    tenant = models.ForeignKey(
        'Tenant',
        on_delete=models.CASCADE,
        related_name='payment_retries'
    )
    original_attempt = models.ForeignKey(
        'PaymentAttempt',
        on_delete=models.CASCADE,
        related_name='retry_schedules'
    )

    # Retry config
    retry_number = models.IntegerField(
        help_text='Retry attempt number (1, 2, or 3)'
    )
    scheduled_for = models.DateTimeField(db_index=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Result
    retry_attempt = models.ForeignKey(
        'PaymentAttempt',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='retry_schedule'
    )
    skip_reason = models.TextField(blank=True, help_text='Reason if skipped or cancelled')

    class Meta:
        db_table = 'tenants_payment_retry_schedule'
        ordering = ['scheduled_for']
        indexes = [
            models.Index(fields=['status', 'scheduled_for']),
            models.Index(fields=['tenant', 'status']),
            models.Index(fields=['subscription', 'retry_number']),
        ]

    def __str__(self):
        return f"Retry #{self.retry_number} - {self.tenant.name} - {self.status} - {self.scheduled_for.strftime('%Y-%m-%d %H:%M')}"

    @property
    def is_overdue(self):
        """Check if retry is overdue"""
        if self.status == 'pending':
            return timezone.now() > self.scheduled_for
        return False


class PlatformMetrics(models.Model):
    """
    Daily platform-wide subscription and revenue metrics
    Calculated by cron job for analytics dashboard
    """
    date = models.DateField(unique=True, db_index=True)

    # Subscription counts
    total_subscriptions = models.IntegerField(default=0)
    active_subscriptions = models.IntegerField(default=0)
    trial_subscriptions = models.IntegerField(default=0)
    suspended_subscriptions = models.IntegerField(default=0)
    cancelled_subscriptions = models.IntegerField(default=0)
    new_subscriptions_today = models.IntegerField(default=0)
    cancelled_today = models.IntegerField(default=0)

    # Revenue metrics
    mrr = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text='Monthly Recurring Revenue'
    )
    arr = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text='Annual Recurring Revenue'
    )
    total_revenue_today = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Payment metrics
    successful_payments = models.IntegerField(default=0)
    failed_payments = models.IntegerField(default=0)
    retry_success_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text='Percentage of successful retries'
    )

    # Churn metrics
    churn_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text='Monthly churn rate percentage'
    )
    retention_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text='Monthly retention rate percentage'
    )

    # Metadata
    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tenants_platform_metrics'
        ordering = ['-date']
        verbose_name = 'Platform Metrics'
        verbose_name_plural = 'Platform Metrics'

    def __str__(self):
        return f"Metrics {self.date} - MRR: {self.mrr} GEL - Active: {self.active_subscriptions}"

    @property
    def payment_success_rate(self):
        """Calculate payment success rate"""
        total = self.successful_payments + self.failed_payments
        if total == 0:
            return 0
        return round((self.successful_payments / total) * 100, 2)


class PaddlePriceMapping(models.Model):
    """
    Maps EchoDesk features to Paddle product/price IDs.
    Used to build subscription line items when creating Paddle transactions.
    """
    feature = models.OneToOneField(
        Feature,
        on_delete=models.CASCADE,
        related_name='paddle_price_mapping',
        help_text='EchoDesk feature this mapping belongs to'
    )
    paddle_product_id = models.CharField(
        max_length=100,
        help_text='Paddle product ID (pro_xxx)'
    )
    paddle_price_id = models.CharField(
        max_length=100,
        help_text='Paddle recurring price ID (pri_xxx)'
    )

    class Meta:
        db_table = 'tenants_paddle_price_mapping'
        verbose_name = 'Paddle Price Mapping'
        verbose_name_plural = 'Paddle Price Mappings'

    def __str__(self):
        return f"{self.feature.name} → {self.paddle_price_id}"


class TenantDomain(models.Model):
    """
    Stores custom domains for tenant ecommerce stores.
    Used by the multi-tenant ecommerce frontend to resolve tenant from hostname.

    Subdomains (e.g., store1.ecommerce.echodesk.ge) are resolved automatically
    from the hostname pattern. This model stores custom domains like mystore.com.
    """
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='ecommerce_domains',
        help_text='Tenant that owns this domain'
    )
    domain = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text='Custom domain name (e.g., shop.example.com or mystore.com)'
    )
    is_verified = models.BooleanField(
        default=False,
        help_text='Whether DNS has been verified for this domain'
    )
    is_primary = models.BooleanField(
        default=False,
        help_text='Whether this is the primary custom domain for the tenant'
    )

    # Verification metadata
    verification_record = models.JSONField(
        default=dict,
        blank=True,
        help_text='Vercel DNS verification record data'
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'tenants_tenant_domain'
        verbose_name = 'Tenant Domain'
        verbose_name_plural = 'Tenant Domains'
        ordering = ['-is_primary', '-created_at']

    def __str__(self):
        status = "✓" if self.is_verified else "⏳"
        primary = " (Primary)" if self.is_primary else ""
        return f"{self.domain} - {self.tenant.name} {status}{primary}"

    def save(self, *args, **kwargs):
        # Normalize domain to lowercase
        self.domain = self.domain.lower().strip()

        # If this is being set as primary, unset other primary domains for this tenant
        if self.is_primary:
            TenantDomain.objects.filter(
                tenant=self.tenant,
                is_primary=True
            ).exclude(id=self.id).update(is_primary=False)

        super().save(*args, **kwargs)


class DashboardAppearanceSettings(models.Model):
    """
    Dashboard appearance settings for tenant customization.
    Stores theme colors, border radius, and sidebar ordering that superadmins can configure.
    All users in the tenant see the same customizations.
    """
    tenant = models.OneToOneField(
        Tenant,
        on_delete=models.CASCADE,
        related_name='dashboard_appearance',
        help_text="Tenant these appearance settings belong to"
    )

    # Colors (HSL format without hsl() wrapper, e.g., "240 5.9% 10%")
    primary_color = models.CharField(
        max_length=50,
        default="240 5.9% 10%",
        help_text="Primary color in HSL format for light mode"
    )
    primary_color_dark = models.CharField(
        max_length=50,
        default="0 0% 98%",
        help_text="Primary color in HSL format for dark mode"
    )
    secondary_color = models.CharField(
        max_length=50,
        default="239 49% 32%",
        help_text="Secondary color in HSL format"
    )
    accent_color = models.CharField(
        max_length=50,
        default="240 4.8% 95.9%",
        help_text="Accent color in HSL format"
    )
    sidebar_background = models.CharField(
        max_length=50,
        default="0 0% 100%",
        help_text="Sidebar background color in HSL format"
    )
    sidebar_primary = models.CharField(
        max_length=50,
        default="240 5.9% 10%",
        help_text="Sidebar primary/active color in HSL format"
    )

    # Border radius
    border_radius = models.CharField(
        max_length=20,
        default="0.5rem",
        help_text="Border radius value (e.g., '0', '0.3rem', '0.5rem', '0.75rem', '1rem')"
    )

    # Sidebar ordering (JSON array of menu item IDs)
    sidebar_order = models.JSONField(
        default=list,
        blank=True,
        help_text="Order of sidebar menu items as JSON array of item IDs"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tenants_dashboard_appearance_settings'
        verbose_name = 'Dashboard Appearance Settings'
        verbose_name_plural = 'Dashboard Appearance Settings'

    def __str__(self):
        return f"Dashboard Appearance for {self.tenant.name}"


class SecurityLog(models.Model):
    """
    Logs security events like login, logout, and token expiry.
    This model is stored in the public schema since it references Tenant.
    """
    EVENT_TYPES = [
        ('login_success', 'Successful Login'),
        ('login_failed', 'Failed Login'),
        ('logout', 'Logout'),
        ('token_expired', 'Token Expired'),
    ]

    DEVICE_TYPES = [
        ('desktop', 'Desktop'),
        ('mobile', 'Mobile'),
        ('tablet', 'Tablet'),
        ('unknown', 'Unknown'),
    ]

    # Tenant reference for filtering logs by tenant
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='security_logs',
        null=True,
        blank=True,
        help_text='Tenant this log belongs to'
    )

    # Note: Using IntegerField instead of ForeignKey to avoid cross-schema FK issues
    # in multi-tenant setup. The user may exist in a different schema context.
    user_id = models.IntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text='User ID associated with this event (null for failed logins with unknown user)'
    )
    attempted_email = models.EmailField(
        blank=True,
        help_text='Email attempted during login (useful for failed logins)'
    )
    event_type = models.CharField(
        max_length=30,
        choices=EVENT_TYPES,
        db_index=True
    )

    # Request metadata
    ip_address = models.GenericIPAddressField(db_index=True)
    user_agent = models.TextField(blank=True)
    device_type = models.CharField(
        max_length=20,
        choices=DEVICE_TYPES,
        default='unknown'
    )
    browser = models.CharField(max_length=100, blank=True)
    operating_system = models.CharField(max_length=100, blank=True)

    # Geolocation
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    country_code = models.CharField(max_length=10, blank=True)

    # Failure details
    failure_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text='Reason for failed login (wrong password, user not found, IP blocked, etc.)'
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'tenants_security_log'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', '-created_at']),
            models.Index(fields=['tenant', 'user_id', '-created_at']),
            models.Index(fields=['tenant', 'event_type', '-created_at']),
            models.Index(fields=['ip_address', '-created_at']),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} - {self.attempted_email or 'Unknown'} - {self.ip_address}"


class TenantIPWhitelist(models.Model):
    """
    Stores whitelisted IP addresses for a tenant.
    This model is stored in the public schema since it references Tenant.
    """
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='ip_whitelists',
        help_text='Tenant this IP whitelist entry belongs to'
    )
    ip_address = models.GenericIPAddressField(
        help_text='Single IP address to whitelist'
    )
    cidr_notation = models.CharField(
        max_length=20,
        blank=True,
        help_text='CIDR notation for IP range (e.g., 192.168.1.0/24). If provided, ip_address is the network address.'
    )
    description = models.CharField(
        max_length=255,
        blank=True,
        help_text='Description of this IP entry (e.g., Office, VPN, etc.)'
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Whether this whitelist entry is active'
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_ip_whitelists',
        help_text='User who created this whitelist entry'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tenants_ip_whitelist'
        verbose_name = 'Tenant IP Whitelist'
        verbose_name_plural = 'Tenant IP Whitelists'
        ordering = ['-created_at']
        unique_together = [['tenant', 'ip_address', 'cidr_notation']]

    def __str__(self):
        ip_display = f"{self.ip_address}/{self.cidr_notation}" if self.cidr_notation else self.ip_address
        status = "Active" if self.is_active else "Inactive"
        return f"{self.tenant.name} - {ip_display} ({status})"


class SocialPlatformRoute(models.Model):
    """Public-schema lookup table mapping a social platform's external account
    id (Facebook page id, Instagram account id, WhatsApp phone-number id,
    TikTok shop id, …) to the tenant schema that owns it.

    Inbound webhooks arrive with no tenant context. Historically each webhook
    resolved its tenant by iterating *every* tenant schema and querying the
    per-platform connection model until a match was found — an O(N-tenants)
    scan that runs on every inbound message and becomes a scaling/DB-load
    landmine as the customer base grows. This table replaces that scan with a
    single O(1) lookup in the public schema.

    Kept eventually-consistent with the per-tenant connection models by signals
    in ``social_integrations.platform_routing`` (and a one-shot
    ``backfill_platform_routes`` command). The webhook resolver still falls
    back to the legacy scan + self-heals this table if a row is missing, so a
    stale/empty table never drops a message.

    Mirrors the established ``widget_registry.WidgetConnection`` pattern: a
    plain ``tenant_schema`` string rather than a cross-schema FK.
    """

    PLATFORM_FACEBOOK = 'facebook'
    PLATFORM_INSTAGRAM = 'instagram'
    PLATFORM_WHATSAPP = 'whatsapp'
    PLATFORM_TIKTOK = 'tiktok'
    PLATFORM_WECHAT = 'wechat'
    PLATFORM_TELEGRAM = 'telegram'
    PLATFORM_CHOICES = [
        (PLATFORM_FACEBOOK, 'Facebook'),
        (PLATFORM_INSTAGRAM, 'Instagram'),
        (PLATFORM_WHATSAPP, 'WhatsApp'),
        (PLATFORM_TIKTOK, 'TikTok'),
        (PLATFORM_WECHAT, 'WeChat'),
        (PLATFORM_TELEGRAM, 'Telegram'),
    ]

    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES, db_index=True)
    external_id = models.CharField(
        max_length=255,
        db_index=True,
        help_text='Platform account id used in webhook payloads (page_id, '
                  'instagram_account_id, phone_number_id, shop_id, …)',
    )
    tenant_schema = models.CharField(max_length=63, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tenants_social_platform_route'
        verbose_name = 'Social Platform Route'
        verbose_name_plural = 'Social Platform Routes'
        unique_together = [['platform', 'external_id']]
        indexes = [
            models.Index(fields=['platform', 'external_id', 'is_active']),
        ]

    def __str__(self):
        return f"{self.platform}:{self.external_id} → {self.tenant_schema}"
