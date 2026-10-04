from rest_framework import serializers
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken
from .models import (
    ServiceCategory, Service, BookingStaff,
    StaffAvailability, StaffException, Booking, RecurringBooking,
    BookingSettings
)
from social_integrations.models import Client
from users.models import User
from .utils_text import localized_text


# ============================================================================
# BOOKING CLIENT SERIALIZERS (using unified Client model)
# ============================================================================

class BookingClientSerializer(serializers.ModelSerializer):
    """Serializer for booking client (using unified Client model)"""
    full_name = serializers.ReadOnlyField()
    phone_number = serializers.CharField(source='phone', read_only=True)

    class Meta:
        model = Client
        fields = [
            'id', 'email', 'phone_number', 'first_name', 'last_name',
            'full_name', 'is_verified', 'is_booking_enabled', 'created_at'
        ]
        read_only_fields = ['id', 'is_verified', 'is_booking_enabled', 'created_at']


class BookingClientRegistrationSerializer(serializers.Serializer):
    """Serializer for booking client registration using unified Client model"""
    email = serializers.EmailField()
    phone_number = serializers.CharField(max_length=50)
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    password = serializers.CharField(write_only=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True)

    def validate_email(self, value):
        # A verified account already uses this email. (An unverified one may be
        # re-registered: whoever proves the address with the emailed code gets
        # the account, with the password from the latest registration — so
        # squatting on someone's email with your own password gains nothing.)
        existing = Client.objects.filter(
            email__iexact=value, is_booking_enabled=True, is_verified=True
        ).exclude(password_hash__isnull=True).exclude(password_hash='').first()
        if existing:
            raise serializers.ValidationError('A booking account with this email already exists', code='account_exists')
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError({"password": "Passwords do not match"})
        return attrs

    def create(self, validated_data):
        validated_data.pop('password_confirm')
        password = validated_data.pop('password')
        phone_number = validated_data.pop('phone_number')

        # A contact with this email may already exist (chat/social contact,
        # someone who booked as a guest, or an unverified earlier registration).
        # The account attaches to that record so their history stays together,
        # but nothing about the stored contact is changed or revealed here:
        # only the pending password is set, and the account stays unusable
        # until the emailed code proves the address.
        client = Client.objects.filter(email__iexact=validated_data['email']).order_by('id').first()

        if client is None:
            from .utils_text import normalize_phone
            client = Client(
                name=f"{validated_data['first_name']} {validated_data['last_name']}".strip(),
                email=validated_data['email'],
                first_name=validated_data['first_name'],
                last_name=validated_data['last_name'],
                phone=normalize_phone(phone_number) or phone_number,
            )

        client.is_booking_enabled = True
        client.is_verified = False
        client.set_password(password)
        client.save()
        return client


class BookingClientLoginSerializer(serializers.Serializer):
    """Serializer for booking client login using unified Client model"""
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')

        # Find client with booking enabled (email isn't unique on Client, so
        # never .get() it)
        client = Client.objects.filter(
            email__iexact=email, is_booking_enabled=True
        ).exclude(password_hash__isnull=True).exclude(password_hash='').order_by('id').first()
        if client is None:
            raise serializers.ValidationError({'code': 'invalid_credentials', 'detail': 'Invalid email or password'})

        if not client.check_password(password):
            raise serializers.ValidationError({'code': 'invalid_credentials', 'detail': 'Invalid email or password'})

        if not client.is_verified:
            raise serializers.ValidationError({'code': 'email_not_verified', 'detail': 'Email not verified. Please check your email.'})

        # Update last login
        client.last_login = timezone.now()
        client.save(update_fields=['last_login'])

        tokens = issue_client_tokens(client)
        attrs['client'] = client
        attrs['access'] = tokens['access']
        attrs['refresh'] = tokens['refresh']

        return attrs


BOOKING_TOKEN_KIND = 'booking'


def issue_client_tokens(client):
    """JWT pair for a booking client (claims carry client_id, no user_id).

    The token names the tenant it was issued for and what kind of client it
    identifies: client ids are per-tenant row numbers, so without these a
    token from another tenant's booking page (or a shop customer's token)
    would be accepted as whichever client here happens to share the id.
    """
    from django.db import connection
    refresh = RefreshToken()
    refresh['client_id'] = client.id
    refresh['kind'] = BOOKING_TOKEN_KIND
    refresh['tenant'] = connection.schema_name
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}


def is_booking_token_for_current_tenant(token):
    from django.db import connection
    return token.get('kind') == BOOKING_TOKEN_KIND and token.get('tenant') == connection.schema_name


# ============================================================================
# SERVICE CATEGORY SERIALIZERS
# ============================================================================

class ServiceCategorySerializer(serializers.ModelSerializer):
    """Serializer for ServiceCategory"""
    name_display = serializers.SerializerMethodField()
    description_display = serializers.SerializerMethodField()

    class Meta:
        model = ServiceCategory
        fields = ['id', 'name', 'description', 'icon', 'display_order', 'is_active', 'name_display', 'description_display']
        read_only_fields = ['id']

    def get_name_display(self, obj):
        """Get name in requested language"""
        language = self.context.get('language', 'en')
        if isinstance(obj.name, dict):
            return localized_text(obj.name, language)
        return obj.name

    def get_description_display(self, obj):
        """Get description in requested language"""
        language = self.context.get('language', 'en')
        if isinstance(obj.description, dict):
            return localized_text(obj.description, language)
        return obj.description if obj.description else ''


# ============================================================================
# STAFF SERIALIZERS
# ============================================================================

class UserMinimalSerializer(serializers.ModelSerializer):
    """Minimal User serializer for staff display"""
    # The User model exposes get_full_name(), not a `full_name` attribute, so a
    # bare ReadOnlyField() silently drops the field. Point it at the method.
    full_name = serializers.CharField(source='get_full_name', read_only=True)

    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'full_name']
        read_only_fields = fields
        # Distinct component name so drf-spectacular doesn't collide this with
        # tickets.serializers.UserMinimalSerializer (different field set).
        ref_name = 'BookingUserMinimal'


class ServiceMinimalSerializer(serializers.ModelSerializer):
    """Minimal Service serializer for staff display"""
    name_display = serializers.SerializerMethodField()

    class Meta:
        model = Service
        fields = ['id', 'name', 'name_display']
        read_only_fields = fields

    def get_name_display(self, obj):
        """Get localized name"""
        if isinstance(obj.name, dict):
            language = self.context.get('language', 'en')
            return localized_text(obj.name, language)
        return obj.name


class BookingStaffSerializer(serializers.ModelSerializer):
    """Serializer for BookingStaff (read)"""
    user = UserMinimalSerializer(read_only=True)
    services = ServiceMinimalSerializer(many=True, read_only=True)
    services_count = serializers.SerializerMethodField()

    class Meta:
        model = BookingStaff
        fields = ['id', 'user', 'bio', 'profile_image', 'average_rating', 'total_ratings', 'is_active_for_bookings', 'services', 'services_count']
        read_only_fields = ['id', 'average_rating', 'total_ratings']

    def get_services_count(self, obj):
        # Use prefetch cache if available to avoid extra COUNT query
        if hasattr(obj, '_prefetched_objects_cache') and 'services' in obj._prefetched_objects_cache:
            return len(obj._prefetched_objects_cache['services'])
        return obj.services.count()


class BookingStaffCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating BookingStaff"""
    user_id = serializers.IntegerField(write_only=True, required=False)
    service_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        help_text="List of service IDs this staff can provide"
    )

    class Meta:
        model = BookingStaff
        fields = ['user_id', 'bio', 'profile_image', 'is_active_for_bookings', 'service_ids']

    def validate_user_id(self, value):
        """Validate that user exists and is not already booking staff"""
        try:
            user = User.objects.get(id=value)
        except User.DoesNotExist:
            raise serializers.ValidationError("User not found")

        # Check if user is already a booking staff (only on create)
        if not self.instance and hasattr(user, 'booking_staff'):
            raise serializers.ValidationError("User is already assigned as booking staff")

        return value

    def validate_service_ids(self, value):
        """Validate that all services exist"""
        if value:
            existing_ids = set(Service.objects.filter(id__in=value).values_list('id', flat=True))
            invalid_ids = set(value) - existing_ids
            if invalid_ids:
                raise serializers.ValidationError(f"Services not found: {invalid_ids}")
        return value

    def create(self, validated_data):
        user_id = validated_data.pop('user_id')
        service_ids = validated_data.pop('service_ids', [])
        user = User.objects.get(id=user_id)
        staff = BookingStaff.objects.create(user=user, **validated_data)

        # Assign services to staff
        if service_ids:
            services = Service.objects.filter(id__in=service_ids)
            for service in services:
                service.staff_members.add(staff)

        return staff

    def update(self, instance, validated_data):
        # Remove user_id if present (can't change user on update)
        validated_data.pop('user_id', None)
        service_ids = validated_data.pop('service_ids', None)

        # Update basic fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # Update services if provided
        if service_ids is not None:
            # Remove from all services first
            instance.services.clear()
            # Add to new services
            if service_ids:
                services = Service.objects.filter(id__in=service_ids)
                for service in services:
                    service.staff_members.add(instance)

        return instance


class StaffAvailabilitySerializer(serializers.ModelSerializer):
    """Serializer for StaffAvailability"""
    day_name = serializers.SerializerMethodField()

    class Meta:
        model = StaffAvailability
        fields = ['id', 'staff', 'day_of_week', 'day_name', 'start_time', 'end_time', 'is_available', 'break_start', 'break_end']
        read_only_fields = ['id']

    def get_day_name(self, obj):
        return obj.get_day_of_week_display()


class StaffExceptionSerializer(serializers.ModelSerializer):
    """Serializer for StaffException"""
    class Meta:
        model = StaffException
        fields = ['id', 'staff', 'date', 'start_time', 'end_time', 'is_available', 'reason']
        read_only_fields = ['id']


# ============================================================================
# SERVICE SERIALIZERS
# ============================================================================

class ServiceListSerializer(serializers.ModelSerializer):
    """Serializer for Service list view"""
    category = ServiceCategorySerializer(read_only=True)
    staff_members = BookingStaffSerializer(many=True, read_only=True)
    name_display = serializers.SerializerMethodField()
    description_display = serializers.SerializerMethodField()
    deposit_amount = serializers.SerializerMethodField()

    class Meta:
        model = Service
        fields = [
            'id', 'name', 'description', 'category', 'base_price', 'deposit_percentage',
            'duration_minutes', 'buffer_time_minutes', 'booking_type', 'available_time_slots',
            'staff_members', 'status', 'image', 'name_display', 'description_display', 'deposit_amount'
        ]
        read_only_fields = ['id']

    def get_name_display(self, obj):
        language = self.context.get('language', 'en')
        if isinstance(obj.name, dict):
            return localized_text(obj.name, language)
        return obj.name

    def get_description_display(self, obj):
        language = self.context.get('language', 'en')
        if isinstance(obj.description, dict):
            return localized_text(obj.description, language)
        return obj.description if obj.description else ''

    def get_deposit_amount(self, obj):
        return float(obj.calculate_deposit_amount())


class ServiceDetailSerializer(ServiceListSerializer):
    """Detailed service serializer including staff members"""
    staff_members = BookingStaffSerializer(many=True, read_only=True)

    class Meta(ServiceListSerializer.Meta):
        fields = ServiceListSerializer.Meta.fields + ['staff_members']


class ServiceCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating Service"""
    category_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    staff_ids = serializers.ListField(child=serializers.IntegerField(), write_only=True, required=False)

    class Meta:
        model = Service
        fields = [
            'name', 'description', 'category_id', 'base_price', 'deposit_percentage',
            'duration_minutes', 'buffer_time_minutes', 'booking_type', 'available_time_slots',
            'staff_ids', 'status', 'image'
        ]

    def create(self, validated_data):
        category_id = validated_data.pop('category_id', None)
        staff_ids = validated_data.pop('staff_ids', [])

        if category_id:
            validated_data['category_id'] = category_id

        service = Service.objects.create(**validated_data)

        if staff_ids:
            staff_members = BookingStaff.objects.filter(id__in=staff_ids)
            service.staff_members.set(staff_members)

        return service

    def update(self, instance, validated_data):
        category_id = validated_data.pop('category_id', None)
        staff_ids = validated_data.pop('staff_ids', None)

        if category_id is not None:
            instance.category_id = category_id

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        if staff_ids is not None:
            staff_members = BookingStaff.objects.filter(id__in=staff_ids)
            instance.staff_members.set(staff_members)

        return instance


# ============================================================================
# BOOKING SERIALIZERS
# ============================================================================

class BookingListSerializer(serializers.ModelSerializer):
    """Serializer for Booking list view"""
    client = BookingClientSerializer(read_only=True)
    service = ServiceListSerializer(read_only=True)
    staff = BookingStaffSerializer(read_only=True)

    class Meta:
        model = Booking
        fields = [
            'id', 'booking_number', 'client', 'service', 'staff', 'date', 'start_time', 'end_time',
            'status', 'payment_status', 'payment_method', 'total_amount', 'deposit_amount', 'paid_amount',
            'client_notes', 'created_at'
        ]
        read_only_fields = ['id', 'booking_number']


class BookingDetailSerializer(serializers.ModelSerializer):
    """Serializer for Booking detail view"""
    client = BookingClientSerializer(read_only=True)
    service = ServiceListSerializer(read_only=True)
    staff = BookingStaffSerializer(read_only=True)
    remaining_amount = serializers.ReadOnlyField()

    class Meta:
        model = Booking
        fields = [
            'id', 'booking_number', 'client', 'service', 'staff', 'date', 'start_time', 'end_time',
            'status', 'payment_status', 'payment_method', 'total_amount', 'deposit_amount', 'paid_amount', 'remaining_amount',
            'bog_order_id', 'payment_url', 'client_notes', 'staff_notes', 'contact_email',
            'rating', 'review',
            'cancelled_at', 'cancelled_by', 'cancellation_reason',
            'created_at', 'updated_at', 'confirmed_at', 'completed_at'
        ]
        read_only_fields = ['id', 'booking_number', 'remaining_amount']


class BookingCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating Booking"""
    service_id = serializers.IntegerField(write_only=True)
    staff_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    # 'cash' = pay at the venue; 'deposit' / 'full' = pay online by card now.
    # Omitted: the first option the business allows.
    payment_type = serializers.ChoiceField(
        choices=['cash', 'deposit', 'full'], write_only=True, required=False, allow_null=True
    )

    class Meta:
        model = Booking
        fields = ['service_id', 'staff_id', 'date', 'start_time', 'client_notes', 'payment_type']

    def validate(self, attrs):
        """Validate booking availability"""
        from .utils import (
            available_payment_options, check_booking_window, find_available_staff,
            get_booking_settings, staff_can_perform, validate_booking_availability,
        )

        date = attrs.get('date')
        start_time = attrs.get('start_time')

        try:
            service = Service.objects.get(id=attrs.get('service_id'), status='active')
        except Service.DoesNotExist:
            raise serializers.ValidationError({"code": "service_unavailable", "error": "Service not found"})

        staff = None
        staff_id = attrs.get('staff_id')
        if staff_id:
            try:
                staff = BookingStaff.objects.get(id=staff_id)
            except BookingStaff.DoesNotExist:
                raise serializers.ValidationError({"code": "slot_unavailable", "error": "Staff not found"})
            if not staff_can_perform(service, staff):
                raise serializers.ValidationError(
                    {"code": "slot_unavailable", "error": "This staff member does not provide this service"}
                )

        booking_settings = get_booking_settings()

        # Lead time / how far ahead, judged on the business's own clock
        ok, error_message = check_booking_window(date, start_time, booking_settings)
        if not ok:
            raise serializers.ValidationError({"code": "outside_booking_window", "error": error_message})

        # Validate availability. Customers may only book times the slot list
        # offers (enforce_grid) — not arbitrary minutes that fragment the day.
        is_available, error_message = validate_booking_availability(
            service, staff, date, start_time, booking_settings=booking_settings, enforce_grid=True
        )
        if not is_available:
            raise serializers.ValidationError({"code": "slot_unavailable", "error": error_message})

        # "Any staff": assign someone who is actually free, so the booking
        # occupies a real person's time (the view re-checks under a lock).
        attrs['auto_assign_staff'] = staff is None
        if staff is None:
            staff = find_available_staff(service, date, start_time, enforce_grid=True)

        # Payment choice must be one the business offers
        options = available_payment_options(booking_settings, service)
        payment_type = attrs.get('payment_type') or options[0]
        if payment_type not in options:
            raise serializers.ValidationError({
                "code": "payment_option_unavailable",
                "payment_type": f"This payment option is not available. Choose one of: {', '.join(options)}.",
            })

        attrs['payment_type'] = payment_type
        attrs['service'] = service
        attrs['staff'] = staff
        return attrs

    def create(self, validated_data):
        payment_type = validated_data.pop('payment_type')
        service = validated_data.pop('service')
        staff = validated_data.pop('staff', None)
        validated_data.pop('service_id')
        validated_data.pop('staff_id', None)
        validated_data.pop('auto_assign_staff', None)

        # Calculate end time
        from datetime import datetime, timedelta
        start_datetime = datetime.combine(validated_data['date'], validated_data['start_time'])
        end_datetime = start_datetime + timedelta(minutes=service.total_duration_minutes)
        validated_data['end_time'] = end_datetime.time()

        # Set pricing. deposit_amount is what gets charged online now.
        validated_data['service'] = service
        validated_data['staff'] = staff
        validated_data['total_amount'] = service.base_price

        if payment_type == 'cash':
            validated_data['payment_method'] = 'cash'
            validated_data['deposit_amount'] = 0
        elif payment_type == 'deposit':
            validated_data['payment_method'] = 'card'
            validated_data['deposit_amount'] = service.calculate_deposit_amount()
        else:
            validated_data['payment_method'] = 'card'
            validated_data['deposit_amount'] = service.base_price

        # Client will be set from request.user in view
        booking = Booking.objects.create(**validated_data)

        return booking


class GuestBookingCreateSerializer(BookingCreateSerializer):
    """Booking by a visitor without an account: contact details + booking fields."""
    first_name = serializers.CharField(max_length=100, write_only=True)
    last_name = serializers.CharField(max_length=100, write_only=True, required=False, allow_blank=True, default='')
    phone_number = serializers.CharField(max_length=50, write_only=True)
    email = serializers.EmailField(write_only=True, required=False, allow_blank=True, allow_null=True)

    class Meta(BookingCreateSerializer.Meta):
        fields = BookingCreateSerializer.Meta.fields + ['first_name', 'last_name', 'phone_number', 'email']

    def validate_phone_number(self, value):
        digits = [c for c in value if c.isdigit()]
        if len(digits) < 6:
            raise serializers.ValidationError('Enter a valid phone number')
        return value.strip()

    def create(self, validated_data):
        for field in ('first_name', 'last_name', 'phone_number', 'email'):
            validated_data.pop(field, None)
        return super().create(validated_data)


# ----------------------------------------------------------------------------
# Public (customer-facing) variants: no staff email, no internal notes
# ----------------------------------------------------------------------------

class PublicStaffUserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    def get_full_name(self, obj) -> str:
        from .utils_text import public_user_name
        return public_user_name(obj)

    class Meta:
        model = User
        fields = ['id', 'first_name', 'last_name', 'full_name']
        read_only_fields = fields


class PublicBookingStaffSerializer(BookingStaffSerializer):
    user = PublicStaffUserSerializer(read_only=True)


class PublicServiceSerializer(ServiceListSerializer):
    staff_members = serializers.SerializerMethodField()

    def get_staff_members(self, obj):
        staff = [s for s in obj.staff_members.all() if s.is_active_for_bookings]
        return PublicBookingStaffSerializer(staff, many=True, context=self.context).data


class PublicBookingSerializer(serializers.ModelSerializer):
    """A customer's view of their own booking."""
    client = BookingClientSerializer(read_only=True)
    service = PublicServiceSerializer(read_only=True)
    staff = PublicBookingStaffSerializer(read_only=True)
    remaining_amount = serializers.ReadOnlyField()
    # Whether the customer may still cancel / move it online, per the
    # business's notice period — so every page can show or hide the buttons.
    can_cancel = serializers.SerializerMethodField()
    cancel_blocked_reason = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            'id', 'booking_number', 'client', 'service', 'staff', 'date', 'start_time', 'end_time',
            'status', 'payment_status', 'payment_method', 'total_amount', 'deposit_amount',
            'paid_amount', 'remaining_amount', 'payment_url', 'client_notes',
            'rating', 'review',
            'cancelled_at', 'cancelled_by', 'cancellation_reason',
            'created_at', 'confirmed_at', 'completed_at',
            'can_cancel', 'cancel_blocked_reason',
        ]
        read_only_fields = fields

    def _cancel_state(self, obj):
        from .utils import can_cancel_booking, get_or_create_booking_settings
        if '_booking_settings' not in self.context:
            self.context['_booking_settings'] = get_or_create_booking_settings()
        return can_cancel_booking(obj, self.context['_booking_settings'])

    def get_can_cancel(self, obj) -> bool:
        return self._cancel_state(obj)[0]

    def get_cancel_blocked_reason(self, obj) -> str:
        can_cancel, reason = self._cancel_state(obj)
        return '' if can_cancel else reason


class BookingUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating Booking (admin only)"""
    staff_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = Booking
        fields = ['staff_id', 'status', 'payment_status', 'staff_notes']

    def update(self, instance, validated_data):
        staff_id = validated_data.pop('staff_id', None)

        if staff_id:
            try:
                staff = BookingStaff.objects.get(id=staff_id)
                instance.staff = staff
            except BookingStaff.DoesNotExist:
                raise serializers.ValidationError({"staff_id": "Staff not found"})

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        # Auto-set timestamps based on status
        if instance.status == 'confirmed' and not instance.confirmed_at:
            instance.confirmed_at = timezone.now()
        elif instance.status == 'completed' and not instance.completed_at:
            instance.completed_at = timezone.now()

        instance.save()
        return instance


# ============================================================================
# RECURRING BOOKING SERIALIZERS
# ============================================================================

class RecurringBookingSerializer(serializers.ModelSerializer):
    """Serializer for RecurringBooking"""
    client = BookingClientSerializer(read_only=True)
    service = ServiceListSerializer(read_only=True)
    staff = BookingStaffSerializer(read_only=True)
    frequency_display = serializers.CharField(source='get_frequency_display', read_only=True)
    day_name = serializers.CharField(source='get_preferred_day_of_week_display', read_only=True)

    class Meta:
        model = RecurringBooking
        fields = [
            'id', 'client', 'service', 'staff', 'frequency', 'frequency_display',
            'preferred_day_of_week', 'day_name', 'preferred_time', 'status',
            'next_booking_date', 'end_date', 'max_occurrences', 'current_occurrences'
        ]
        read_only_fields = ['id', 'current_occurrences']


class RecurringBookingCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating RecurringBooking"""
    service_id = serializers.IntegerField(write_only=True)
    staff_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)

    class Meta:
        model = RecurringBooking
        fields = [
            'service_id', 'staff_id', 'frequency', 'preferred_day_of_week',
            'preferred_time', 'end_date', 'max_occurrences'
        ]

    def create(self, validated_data):
        service_id = validated_data.pop('service_id')
        staff_id = validated_data.pop('staff_id', None)

        try:
            service = Service.objects.get(id=service_id)
        except Service.DoesNotExist:
            raise serializers.ValidationError({"service_id": "Service not found"})

        validated_data['service'] = service

        if staff_id:
            try:
                staff = BookingStaff.objects.get(id=staff_id)
                validated_data['staff'] = staff
            except BookingStaff.DoesNotExist:
                raise serializers.ValidationError({"staff_id": "Staff not found"})

        # Calculate next_booking_date
        from datetime import timedelta
        today = timezone.now().date()
        preferred_day = validated_data['preferred_day_of_week']

        # Find next occurrence of preferred day
        days_ahead = preferred_day - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        validated_data['next_booking_date'] = today + timedelta(days=days_ahead)

        # Client will be set from request.user in view
        return RecurringBooking.objects.create(**validated_data)


# ============================================================================
# BOOKING SETTINGS SERIALIZERS
# ============================================================================

class BookingSettingsSerializer(serializers.ModelSerializer):
    """Serializer for BookingSettings"""
    bog_client_id = serializers.CharField(write_only=True, required=False, allow_blank=True)
    bog_client_secret = serializers.CharField(write_only=True, required=False, allow_blank=True)
    has_bog_client_id = serializers.SerializerMethodField()
    has_bog_client_secret = serializers.SerializerMethodField()

    class Meta:
        model = BookingSettings
        fields = [
            'id', 'payment_method', 'bank_name', 'bank_iban', 'bank_account_holder',
            'require_deposit', 'allow_cash_payment', 'allow_card_payment',
            'bog_client_id', 'bog_client_secret', 'has_bog_client_id', 'has_bog_client_secret',
            'bog_use_production',
            'cancellation_hours_before', 'refund_policy',
            'auto_confirm_on_deposit', 'auto_confirm_on_full_payment',
            'min_hours_before_booking', 'max_days_advance_booking',
            'timezone', 'public_page_enabled', 'public_description', 'public_address', 'public_phone',
        ]
        read_only_fields = ['id']

    def validate_timezone(self, value):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(value)
        except Exception:
            raise serializers.ValidationError('Unknown timezone')
        return value

    def get_has_bog_client_id(self, obj):
        return bool(obj.bog_client_id)

    def get_has_bog_client_secret(self, obj):
        return bool(obj.bog_client_secret)

    def update(self, instance, validated_data):
        bog_client_id = validated_data.pop('bog_client_id', None)
        bog_client_secret = validated_data.pop('bog_client_secret', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        # The dashboard never receives the stored credentials back, so it posts
        # these fields empty unless the user typed new ones. Empty = keep.
        if bog_client_id:
            instance.bog_client_id = bog_client_id

        if bog_client_secret:
            instance.bog_client_secret = bog_client_secret

        instance.save()
        return instance
