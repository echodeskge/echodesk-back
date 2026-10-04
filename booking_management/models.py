from django.db import models
from django.contrib.auth.hashers import make_password, check_password
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator
from cryptography.fernet import Fernet
from django.conf import settings
import uuid
import secrets
from decimal import Decimal
from amanati_crm.file_utils import sanitized_upload_to


def generate_booking_number():
    """Generate unique booking number"""
    timestamp = timezone.now().strftime('%Y%m%d%H%M%S')
    random_part = secrets.token_hex(3).upper()
    return f"BK{timestamp}{random_part}"


class BookingClient(models.Model):
    """
    DEPRECATED: This model is deprecated. Use social_integrations.Client instead.

    Booking functionality has been merged into the unified Client model.
    This model is kept for backwards compatibility during migration.
    """
    email = models.EmailField(unique=True, db_index=True)
    phone_number = models.CharField(max_length=20)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    password_hash = models.CharField(max_length=255)

    # Email verification
    is_verified = models.BooleanField(default=False)
    verification_token = models.CharField(max_length=100, blank=True, null=True)
    verification_sent_at = models.DateTimeField(blank=True, null=True)

    # Password reset
    reset_token = models.CharField(max_length=100, blank=True, null=True)
    reset_token_expires = models.DateTimeField(blank=True, null=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_login = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'booking_clients'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['phone_number']),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def set_password(self, raw_password):
        """Hash and set password"""
        self.password_hash = make_password(raw_password)

    def check_password(self, raw_password):
        """Check password against hash"""
        return check_password(raw_password, self.password_hash)

    def generate_verification_token(self):
        """Generate email verification token"""
        self.verification_token = secrets.token_urlsafe(32)
        self.verification_sent_at = timezone.now()
        return self.verification_token

    def generate_reset_token(self):
        """Generate password reset token (valid for 1 hour)"""
        self.reset_token = secrets.token_urlsafe(32)
        self.reset_token_expires = timezone.now() + timezone.timedelta(hours=1)
        return self.reset_token

    def verify_reset_token(self, token):
        """Check if reset token is valid"""
        if not self.reset_token or not self.reset_token_expires:
            return False
        if self.reset_token != token:
            return False
        if timezone.now() > self.reset_token_expires:
            return False
        return True


class ServiceCategory(models.Model):
    """
    Categories for grouping services (e.g., Hair, Nails, Massage, Beauty)
    """
    name = models.JSONField(help_text="Multilingual name: {'en': 'Hair Services', 'ka': '...'}")
    description = models.JSONField(blank=True, null=True, help_text="Multilingual description")
    icon = models.CharField(max_length=50, blank=True, help_text="Icon name or emoji")
    display_order = models.IntegerField(default=0, help_text="Sort order")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'booking_service_categories'
        ordering = ['display_order', 'id']
        verbose_name_plural = 'Service Categories'

    def __str__(self):
        name = self.name if isinstance(self.name, str) else self.name.get('en', 'N/A')
        return name


class BookingStaff(models.Model):
    """
    Staff members who provide services
    Links to existing User model
    """
    user = models.OneToOneField('users.User', on_delete=models.CASCADE, related_name='booking_staff')
    bio = models.TextField(blank=True, help_text="Staff bio/description")
    profile_image = models.ImageField(upload_to=sanitized_upload_to('booking/staff', date_based=False), blank=True, null=True)

    # Rating system
    average_rating = models.DecimalField(max_digits=3, decimal_places=2, default=0.00, validators=[MinValueValidator(0), MaxValueValidator(5)])
    total_ratings = models.IntegerField(default=0)

    # Availability
    is_active_for_bookings = models.BooleanField(default=True, help_text="Can accept new bookings")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'booking_staff'
        ordering = ['user__first_name', 'user__last_name']
        verbose_name = 'Booking Staff'
        verbose_name_plural = 'Booking Staff'

    def __str__(self):
        return self.user.get_full_name() or self.user.email

    def update_rating(self, new_rating):
        """Update average rating with new rating"""
        total = (self.average_rating * self.total_ratings) + Decimal(str(new_rating))
        self.total_ratings += 1
        self.average_rating = total / self.total_ratings
        self.save(update_fields=['average_rating', 'total_ratings'])


class Service(models.Model):
    """
    Bookable services (e.g., Haircut, Manicure, Massage, Braiding)
    """
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('coming_soon', 'Coming Soon'),
    ]

    BOOKING_TYPE_CHOICES = [
        ('fixed_slots', 'Fixed Time Slots'),
        ('duration_based', 'Duration Based'),
    ]

    name = models.JSONField(help_text="Multilingual name: {'en': 'Haircut', 'ka': '...'}")
    description = models.JSONField(blank=True, null=True, help_text="Multilingual description")
    category = models.ForeignKey(ServiceCategory, on_delete=models.SET_NULL, null=True, related_name='services')

    # Pricing
    base_price = models.DecimalField(max_digits=10, decimal_places=2, help_text="Base price for the service")
    deposit_percentage = models.IntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)], help_text="Percentage required as deposit (0-100)")

    # Duration
    duration_minutes = models.IntegerField(help_text="Service duration in minutes")
    buffer_time_minutes = models.IntegerField(default=0, help_text="Buffer time after service (cleanup, etc.)")

    # Booking configuration
    booking_type = models.CharField(max_length=20, choices=BOOKING_TYPE_CHOICES, default='duration_based')
    available_time_slots = models.JSONField(blank=True, null=True, help_text="For fixed_slots: ['09:00', '10:00', '11:00']")

    # Staff assignment
    staff_members = models.ManyToManyField(BookingStaff, related_name='services', blank=True, help_text="Staff who can provide this service")

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    image = models.ImageField(upload_to=sanitized_upload_to('booking/services', date_based=False), blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'booking_services'
        ordering = ['category', 'id']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['booking_type']),
        ]

    def __str__(self):
        name = self.name if isinstance(self.name, str) else self.name.get('en', 'N/A')
        return name

    @property
    def total_duration_minutes(self):
        """Total time slot needed (service + buffer)"""
        return self.duration_minutes + self.buffer_time_minutes

    def calculate_deposit_amount(self):
        """Calculate deposit amount based on percentage"""
        return (self.base_price * Decimal(str(self.deposit_percentage))) / Decimal('100')

    def calculate_remaining_amount(self):
        """Calculate remaining amount after deposit"""
        return self.base_price - self.calculate_deposit_amount()


class StaffAvailability(models.Model):
    """
    Weekly availability schedule for staff members
    """
    DAY_CHOICES = [
        (0, 'Monday'),
        (1, 'Tuesday'),
        (2, 'Wednesday'),
        (3, 'Thursday'),
        (4, 'Friday'),
        (5, 'Saturday'),
        (6, 'Sunday'),
    ]

    staff = models.ForeignKey(BookingStaff, on_delete=models.CASCADE, related_name='availability')
    day_of_week = models.IntegerField(choices=DAY_CHOICES, help_text="0=Monday, 6=Sunday")
    start_time = models.TimeField(help_text="Start of working hours")
    end_time = models.TimeField(help_text="End of working hours")
    is_available = models.BooleanField(default=True, help_text="Available on this day")

    # Optional break time
    break_start = models.TimeField(blank=True, null=True, help_text="Break start time")
    break_end = models.TimeField(blank=True, null=True, help_text="Break end time")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'booking_staff_availability'
        ordering = ['staff', 'day_of_week', 'start_time']
        unique_together = ['staff', 'day_of_week']
        verbose_name_plural = 'Staff Availability'

    def __str__(self):
        day_name = dict(self.DAY_CHOICES)[self.day_of_week]
        return f"{self.staff} - {day_name} {self.start_time}-{self.end_time}"


class StaffException(models.Model):
    """
    Exceptions to regular availability (vacation, sick days, special hours)
    """
    staff = models.ForeignKey(BookingStaff, on_delete=models.CASCADE, related_name='exceptions')
    date = models.DateField(help_text="Date of exception")
    start_time = models.TimeField(blank=True, null=True, help_text="If available, custom start time")
    end_time = models.TimeField(blank=True, null=True, help_text="If available, custom end time")
    is_available = models.BooleanField(default=False, help_text="Available on this date (overrides regular schedule)")
    reason = models.CharField(max_length=255, blank=True, help_text="Reason for exception (vacation, sick, etc.)")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'booking_staff_exceptions'
        ordering = ['staff', '-date']
        indexes = [
            models.Index(fields=['staff', 'date']),
        ]

    def __str__(self):
        status = "Available" if self.is_available else "Unavailable"
        return f"{self.staff} - {self.date} ({status})"


class Booking(models.Model):
    """
    Main booking model for client appointments
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    PAYMENT_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('deposit_paid', 'Deposit Paid'),
        ('fully_paid', 'Fully Paid'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]

    # Unique identifier
    booking_number = models.CharField(max_length=50, unique=True, default=generate_booking_number, db_index=True)

    # Relationships - client points to unified Client model in social_integrations
    client = models.ForeignKey(
        'social_integrations.Client',
        on_delete=models.PROTECT,
        related_name='bookings',
        help_text="Client who made the booking"
    )
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name='bookings')
    staff = models.ForeignKey(BookingStaff, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings', help_text="Assigned staff member")

    # Booking details
    date = models.DateField(db_index=True)
    start_time = models.TimeField()
    end_time = models.TimeField()

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='pending', db_index=True)

    # Pricing
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, help_text="Total service price")
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Deposit amount (if applicable)")
    paid_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Amount paid so far")

    # Payment integration (BOG)
    bog_order_id = models.CharField(max_length=255, blank=True, null=True, help_text="BOG payment order ID")
    payment_url = models.URLField(max_length=500, blank=True, null=True, help_text="BOG payment URL")
    payment_method = models.CharField(
        max_length=10, blank=True, default='',
        choices=[('cash', 'Pay at the venue'), ('card', 'Card (online)')],
        help_text="How the customer chose to pay (empty for bookings made before this field existed)"
    )
    payment_metadata = models.JSONField(blank=True, null=True, help_text="Full payment response from BOG")

    # Notes
    client_notes = models.TextField(blank=True, help_text="Notes from client")
    staff_notes = models.TextField(blank=True, help_text="Internal notes from staff")

    # Rating/Feedback
    rating = models.IntegerField(blank=True, null=True, validators=[MinValueValidator(1), MaxValueValidator(5)], help_text="Client rating (1-5)")
    review = models.TextField(blank=True, help_text="Client review text")

    # Reminders
    reminder_sent = models.BooleanField(default=False, help_text="Whether reminder was sent")

    # Cancellation
    cancelled_at = models.DateTimeField(blank=True, null=True)
    cancelled_by = models.CharField(max_length=50, blank=True, choices=[('client', 'Client'), ('staff', 'Staff'), ('admin', 'Admin')])
    cancellation_reason = models.TextField(blank=True)

    # Where this booking's notices go, and in which language. Kept on the
    # booking because a guest may be attached to an existing contact whose
    # stored email is different (or missing) — the notice must reach the
    # person who actually made the booking.
    contact_email = models.EmailField(blank=True, default='')
    contact_language = models.CharField(max_length=5, blank=True, default='')

    # Secret link token so a guest (no account) can view/cancel their booking
    manage_token = models.CharField(max_length=64, blank=True, null=True, unique=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'bookings'
        ordering = ['-date', '-start_time']
        indexes = [
            models.Index(fields=['booking_number']),
            models.Index(fields=['client', 'status']),
            models.Index(fields=['staff', 'date']),
            models.Index(fields=['date', 'start_time']),
            models.Index(fields=['status', 'payment_status']),
        ]

    def __str__(self):
        return f"{self.booking_number} - {self.client.full_name} - {self.service}"

    def save(self, *args, **kwargs):
        # Auto-generate booking number if not set
        if not self.booking_number:
            self.booking_number = generate_booking_number()
        super().save(*args, **kwargs)

    def confirm(self, notify=True):
        """Confirm booking"""
        self.status = 'confirmed'
        self.confirmed_at = timezone.now()
        self.save(update_fields=['status', 'confirmed_at', 'updated_at'])
        if notify:
            self._notify_client('confirmed')

    def complete(self):
        """Mark booking as completed"""
        self.status = 'completed'
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'completed_at', 'updated_at'])

    def cancel(self, cancelled_by, reason='', notify=True):
        """Cancel booking"""
        self.status = 'cancelled'
        self.cancelled_at = timezone.now()
        self.cancelled_by = cancelled_by
        self.cancellation_reason = reason
        self.save(update_fields=['status', 'cancelled_at', 'cancelled_by', 'cancellation_reason', 'updated_at'])
        if notify:
            self._notify_client('cancelled')

    def _notify_client(self, kind):
        """Email the client about a status change, once the change is committed."""
        from django.db import connection, transaction

        schema_name = connection.schema_name
        booking_id = self.id

        def _send():
            try:
                from .tasks import send_booking_email_task
                send_booking_email_task.delay(schema_name, booking_id, kind, None)  # language: booking.contact_language
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Could not queue %s email for booking %s', kind, booking_id)

        transaction.on_commit(_send)

    @property
    def is_paid(self):
        """Check if booking is fully paid"""
        return self.payment_status == 'fully_paid'

    @property
    def remaining_amount(self):
        """Calculate remaining unpaid amount"""
        return self.total_amount - self.paid_amount


class RecurringBooking(models.Model):
    """
    Recurring booking schedule for clients
    Automatically creates bookings based on schedule
    """
    FREQUENCY_CHOICES = [
        ('weekly', 'Weekly'),
        ('biweekly', 'Biweekly (Every 2 weeks)'),
        ('monthly', 'Monthly'),
    ]

    STATUS_CHOICES = [
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed'),
    ]

    # Relationships - client points to unified Client model in social_integrations
    client = models.ForeignKey(
        'social_integrations.Client',
        on_delete=models.CASCADE,
        related_name='recurring_bookings',
        help_text="Client with recurring booking"
    )
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name='recurring_bookings')
    staff = models.ForeignKey(BookingStaff, on_delete=models.SET_NULL, null=True, blank=True, related_name='recurring_bookings', help_text="Preferred staff (optional)")

    # Schedule
    frequency = models.CharField(max_length=20, choices=FREQUENCY_CHOICES)
    preferred_day_of_week = models.IntegerField(choices=StaffAvailability.DAY_CHOICES, help_text="Preferred day of week")
    preferred_time = models.TimeField(help_text="Preferred start time")

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')

    # Auto-creation tracking
    next_booking_date = models.DateField(help_text="Date for next booking creation")
    last_created_booking = models.ForeignKey(Booking, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', help_text="Last auto-created booking")

    # End conditions
    end_date = models.DateField(blank=True, null=True, help_text="Stop creating bookings after this date")
    max_occurrences = models.IntegerField(blank=True, null=True, help_text="Max number of bookings to create")
    current_occurrences = models.IntegerField(default=0, help_text="Number of bookings created so far")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'booking_recurring'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'next_booking_date']),
        ]

    def __str__(self):
        return f"{self.client.full_name} - {self.service} ({self.frequency})"

    def should_create_booking(self):
        """Check if it's time to create next booking"""
        if self.status != 'active':
            return False
        if self.end_date and timezone.now().date() > self.end_date:
            return False
        if self.max_occurrences and self.current_occurrences >= self.max_occurrences:
            return False
        if timezone.now().date() >= self.next_booking_date:
            return True
        return False

    def calculate_next_date(self):
        """Calculate next booking date based on frequency"""
        if self.frequency == 'weekly':
            return self.next_booking_date + timezone.timedelta(days=7)
        elif self.frequency == 'biweekly':
            return self.next_booking_date + timezone.timedelta(days=14)
        elif self.frequency == 'monthly':
            # Add one month, handling month-end edge cases (e.g., Jan 31 → Feb 28)
            import calendar
            next_month = self.next_booking_date.month + 1
            next_year = self.next_booking_date.year
            if next_month > 12:
                next_month = 1
                next_year += 1
            max_day = calendar.monthrange(next_year, next_month)[1]
            next_day = min(self.next_booking_date.day, max_day)
            return self.next_booking_date.replace(year=next_year, month=next_month, day=next_day)
        return self.next_booking_date


def _credentials_fernet():
    """Fernet for the stored BOG credentials, keyed from SECRET_KEY (same
    scheme as EcommerceSettings). settings.ENCRYPTION_KEY was never defined, so
    the previous code could not store credentials at all."""
    import base64
    key = settings.SECRET_KEY[:32].encode().ljust(32, b'0')
    return Fernet(base64.urlsafe_b64encode(key))


class BookingSettings(models.Model):
    """
    Tenant-specific booking settings
    """
    REFUND_POLICY_CHOICES = [
        ('full', 'Full Refund'),
        ('partial_50', '50% Refund'),
        ('partial_25', '25% Refund'),
        ('no_refund', 'No Refund'),
    ]

    PAYMENT_METHOD_CHOICES = [
        ('manual_transfer', 'Manual Bank Transfer'),
        ('bog_gateway', 'BOG Payment Gateway'),
    ]

    tenant = models.OneToOneField('tenants.Tenant', on_delete=models.CASCADE, related_name='booking_settings')

    # Payment method choice
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default='manual_transfer')

    # Bank transfer details (for manual method)
    bank_name = models.CharField(max_length=100, blank=True)
    bank_iban = models.CharField(max_length=50, blank=True)
    bank_account_holder = models.CharField(max_length=255, blank=True)

    # Payment settings
    require_deposit = models.BooleanField(default=False, help_text="Require deposit for all bookings")
    allow_cash_payment = models.BooleanField(default=True, help_text="Allow cash payment on arrival")
    allow_card_payment = models.BooleanField(default=True, help_text="Allow card payment via BOG")

    # BOG Payment Gateway credentials (encrypted)
    _bog_client_id_encrypted = models.BinaryField(blank=True, null=True)
    _bog_client_secret_encrypted = models.BinaryField(blank=True, null=True)
    bog_use_production = models.BooleanField(default=False, help_text="Use production BOG API (vs test)")

    # Cancellation policy
    cancellation_hours_before = models.IntegerField(default=24, help_text="Minimum hours before booking to cancel")
    refund_policy = models.CharField(max_length=20, choices=REFUND_POLICY_CHOICES, default='full')

    # Auto-confirmation
    auto_confirm_on_deposit = models.BooleanField(default=True, help_text="Auto-confirm when deposit is paid")
    auto_confirm_on_full_payment = models.BooleanField(default=True, help_text="Auto-confirm when fully paid")

    # Booking window
    min_hours_before_booking = models.IntegerField(default=2, help_text="Minimum hours in advance to book")
    max_days_advance_booking = models.IntegerField(default=60, help_text="Maximum days in advance to book")

    # Business clock. Booking dates/times are stored in this zone's wall time.
    timezone = models.CharField(max_length=64, default='Asia/Tbilisi', help_text="IANA timezone of the business")

    # Public booking page (book.echodesk.ge/<schema>)
    public_page_enabled = models.BooleanField(default=True, help_text="Let customers book online")
    public_description = models.JSONField(default=dict, blank=True, help_text='Localized description: {"en": "...", "ka": "..."}')
    public_address = models.CharField(max_length=255, blank=True)
    public_phone = models.CharField(max_length=50, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'booking_settings'
        verbose_name_plural = 'Booking Settings'

    def __str__(self):
        return f"Booking Settings for {self.tenant.schema_name}"

    @property
    def bog_client_id(self):
        """Decrypt and return BOG client ID"""
        if not self._bog_client_id_encrypted:
            return ''
        try:
            fernet = _credentials_fernet()
            return fernet.decrypt(bytes(self._bog_client_id_encrypted)).decode()
        except Exception:
            return ''

    @bog_client_id.setter
    def bog_client_id(self, value):
        """Encrypt and store BOG client ID"""
        if value:
            fernet = _credentials_fernet()
            self._bog_client_id_encrypted = fernet.encrypt(value.encode())
        else:
            self._bog_client_id_encrypted = None

    @property
    def bog_client_secret(self):
        """Decrypt and return BOG client secret"""
        if not self._bog_client_secret_encrypted:
            return ''
        try:
            fernet = _credentials_fernet()
            return fernet.decrypt(bytes(self._bog_client_secret_encrypted)).decode()
        except Exception:
            return ''

    @bog_client_secret.setter
    def bog_client_secret(self, value):
        """Encrypt and store BOG client secret"""
        if value:
            fernet = _credentials_fernet()
            self._bog_client_secret_encrypted = fernet.encrypt(value.encode())
        else:
            self._bog_client_secret_encrypted = None

    def save(self, *args, **kwargs):
        # Ensure encrypted values are set if assigned via deferred attributes
        if hasattr(self, '_client_secret_to_encrypt'):
            self.bog_client_secret = self._client_secret_to_encrypt
            delattr(self, '_client_secret_to_encrypt')
        if hasattr(self, '_client_id_to_encrypt'):
            self.bog_client_id = self._client_id_to_encrypt
            delattr(self, '_client_id_to_encrypt')
        super().save(*args, **kwargs)
