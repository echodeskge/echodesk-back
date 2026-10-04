import logging
import secrets
from datetime import datetime, timedelta

from rest_framework import mixins, viewsets, status, permissions
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, throttle_classes, action,
)
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from django.db import connection, transaction
from django.db.models import Prefetch
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import extend_schema, extend_schema_view
from .models import (
    Service, ServiceCategory, BookingStaff,
    Booking, RecurringBooking
)
from social_integrations.models import Client
from .serializers import (
    BookingClientSerializer, BookingClientRegistrationSerializer,
    BookingClientLoginSerializer,
    ServiceCategorySerializer,
    BookingCreateSerializer, GuestBookingCreateSerializer,
    PublicBookingSerializer, PublicBookingStaffSerializer, PublicServiceSerializer,
    RecurringBookingSerializer, RecurringBookingCreateSerializer,
    issue_client_tokens,
)
from .authentication import BookingClientJWTAuthentication
from .emails import (
    CODE_TTL_MINUTES, booking_manage_url, send_password_reset_code, send_verification_code,
)
from .permissions import IsAuthenticatedBookingClient, PublicBookingEnabled
from .throttles import BookingAuthThrottle, BookingGuestThrottle
from .utils import (
    available_payment_options, can_cancel_booking, card_payment_enabled,
    check_booking_window, find_available_staff, generate_available_slots,
    get_booking_settings, get_or_create_booking_settings, is_slot_booked, staff_can_perform,
    validate_booking_availability,
)
from .payment_service import get_booking_payment_service

logger = logging.getLogger(__name__)


# ============================================================================
# HELPERS
# ============================================================================

def _language(request):
    lang = request.query_params.get('lang')
    if not lang and request.method != 'GET':
        try:
            lang = request.data.get('lang')
        except Exception:
            lang = None
    return lang if lang in ('en', 'ka') else 'en'


def _generate_code():
    return f"{secrets.randbelow(1000000):06d}"


def _find_account(email):
    """The registered (password-holding) booking account for an email, if any."""
    if not email:
        return None
    return Client.objects.filter(
        email__iexact=email.strip(), is_booking_enabled=True
    ).exclude(password_hash__isnull=True).exclude(password_hash='').order_by('id').first()


def _code_matches(stored, sent_at, code, ttl_minutes=CODE_TTL_MINUTES):
    if not stored or not code or not sent_at:
        return False
    if timezone.now() > sent_at + timedelta(minutes=ttl_minutes):
        return False
    return constant_time_compare(str(stored), str(code).strip())


def _queue_booking_email(booking, kind, language):
    """Send a booking notice in the background; never let mail break a request."""
    if not booking.client.email:
        return
    try:
        from .tasks import send_booking_email_task
        send_booking_email_task.delay(connection.schema_name, booking.id, kind, language)
    except Exception:
        logger.exception('Could not queue %s email for booking %s', kind, booking.booking_number)


def _booking_queryset():
    return Booking.objects.select_related(
        'client', 'service', 'service__category', 'staff', 'staff__user',
    ).prefetch_related(
        Prefetch(
            'service__staff_members',
            queryset=BookingStaff.objects.select_related('user').prefetch_related('services'),
        ),
        'staff__services',
    )


def _create_booking(request, serializer, client):
    """Create a validated booking for `client`, start card payment if chosen.

    Returns (booking, error_response). Exactly one of them is None.
    """
    data = serializer.validated_data
    service = data['service']
    date = data['date']
    start_time = data['start_time']
    staff = data.get('staff')
    auto_assign = data.get('auto_assign_staff', False)

    with transaction.atomic():
        # Serialize competing bookings for this service's staff, then re-check
        # the slot: two customers may have validated the same free slot.
        list(
            BookingStaff.objects.select_for_update()
            .filter(pk__in=service.staff_members.values('pk'))
            .order_by('pk')
        )
        if staff is None or is_slot_booked(staff, date, start_time, service.total_duration_minutes):
            staff = find_available_staff(service, date, start_time) if auto_assign else None
            if staff is None:
                raise ValidationError({'error': 'This time slot was just booked. Please select another time.'})
            data['staff'] = staff

        booking = serializer.save(client=client, manage_token=secrets.token_urlsafe(32))

    language = _language(request)

    if booking.payment_method == 'card':
        manage_url = booking_manage_url(connection.schema_name, booking.manage_token)
        try:
            get_booking_payment_service().create_booking_payment(
                booking,
                callback_url=f"https://{request.get_host()}/api/bookings/payment-webhook/",
                return_url_success=f"{manage_url}?paid=1",
                return_url_fail=f"{manage_url}?paid=0",
            )
        except Exception:
            # No way to pay → don't leave a booking holding the slot.
            logger.exception('Payment init failed for booking %s', booking.booking_number)
            booking.cancel(cancelled_by='admin', reason='Payment initialization failed', notify=False)
            return None, Response(
                {'error': 'Online payment is unavailable right now. Please try again or choose another payment option.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        booking.refresh_from_db()
    else:
        # Card bookings get their notice once the payment clears.
        _queue_booking_email(booking, 'created', language)

    return booking, None


def _booking_payload(booking, request, include_client=True):
    data = PublicBookingSerializer(
        booking, context={'request': request, 'language': _language(request)}
    ).data
    if not include_client:
        # A guest booking may be attached to an existing contact matched by
        # phone number; never echo that person's stored details back.
        data.pop('client', None)
    return data


def _cancel_booking(booking, reason):
    """Cancel on the customer's behalf and refund per policy."""
    booking.cancel(cancelled_by='client', reason=reason or '')
    if booking.paid_amount and booking.paid_amount > 0:
        try:
            get_booking_payment_service().initiate_refund(booking)
        except Exception:
            logger.exception('Refund failed for booking %s', booking.booking_number)
        booking.refresh_from_db()


# ============================================================================
# PUBLIC BUSINESS INFO
# ============================================================================

@extend_schema(tags=['Booking Client - Services'])
@api_view(['GET'])
@authentication_classes([])
@permission_classes([PublicBookingEnabled])
def public_info(request):
    """What the public booking page needs to render: who the business is and its booking rules."""
    tenant = request.tenant
    booking_settings = get_booking_settings()
    card_ok = card_payment_enabled(booking_settings)

    bank_transfer = None
    if booking_settings is not None and booking_settings.bank_iban:
        bank_transfer = {
            'bank_name': booking_settings.bank_name,
            'iban': booking_settings.bank_iban,
            'account_holder': booking_settings.bank_account_holder,
        }

    return Response({
        'schema_name': tenant.schema_name,
        'name': tenant.name,
        'logo': request.build_absolute_uri(tenant.logo.url) if getattr(tenant, 'logo', None) else None,
        'preferred_language': getattr(tenant, 'preferred_language', 'en'),
        'description': getattr(booking_settings, 'public_description', None) or {},
        'address': getattr(booking_settings, 'public_address', '') or '',
        'phone': getattr(booking_settings, 'public_phone', '') or '',
        'timezone': getattr(booking_settings, 'timezone', 'Asia/Tbilisi'),
        'min_hours_before_booking': getattr(booking_settings, 'min_hours_before_booking', 2),
        'max_days_advance_booking': getattr(booking_settings, 'max_days_advance_booking', 60),
        'cancellation_hours_before': getattr(booking_settings, 'cancellation_hours_before', 24),
        'payment_options': available_payment_options(booking_settings),
        'card_payment_enabled': card_ok,
        'bank_transfer': bank_transfer,
    })


# ============================================================================
# PUBLIC AUTHENTICATION ENDPOINTS
# ============================================================================

@extend_schema(tags=['Booking Client - Authentication'])
@api_view(['POST'])
@authentication_classes([])
@permission_classes([PublicBookingEnabled])
@throttle_classes([BookingAuthThrottle])
def client_register(request):
    """Register new booking client"""
    # Password strength validation
    password = request.data.get('password', '')
    if len(password) < 8:
        return Response(
            {'password': ['Password must be at least 8 characters long.']},
            status=status.HTTP_400_BAD_REQUEST
        )
    if not any(c.isdigit() for c in password):
        return Response(
            {'password': ['Password must contain at least one number.']},
            status=status.HTTP_400_BAD_REQUEST
        )

    serializer = BookingClientRegistrationSerializer(data=request.data)

    if serializer.is_valid():
        client = serializer.save()

        code = _generate_code()
        client.verification_token = code
        client.verification_sent_at = timezone.now()
        client.save(update_fields=['verification_token', 'verification_sent_at'])
        send_verification_code(client, code, _language(request))

        return Response({
            'message': 'Registration successful. Please check your email to verify your account.',
            'client': BookingClientSerializer(client).data
        }, status=status.HTTP_201_CREATED)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Booking Client - Authentication'])
@api_view(['POST'])
@authentication_classes([])
@permission_classes([PublicBookingEnabled])
@throttle_classes([BookingAuthThrottle])
def client_resend_verification(request):
    """Send a fresh verification code. Same answer whether or not the account exists."""
    client = _find_account(request.data.get('email'))
    if client is not None and not client.is_verified:
        code = _generate_code()
        client.verification_token = code
        client.verification_sent_at = timezone.now()
        client.save(update_fields=['verification_token', 'verification_sent_at'])
        send_verification_code(client, code, _language(request))
    return Response({'message': 'If the account exists and is not verified, a code was sent.'})


@extend_schema(tags=['Booking Client - Authentication'])
@api_view(['POST'])
@authentication_classes([])
@permission_classes([PublicBookingEnabled])
@throttle_classes([BookingAuthThrottle])
def client_login(request):
    """Login booking client"""
    serializer = BookingClientLoginSerializer(data=request.data)

    if serializer.is_valid():
        return Response({
            'access': serializer.validated_data['access'],
            'refresh': serializer.validated_data['refresh'],
            'client': BookingClientSerializer(serializer.validated_data['client']).data
        })

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Booking Client - Authentication'])
@api_view(['POST'])
@authentication_classes([])
@permission_classes([PublicBookingEnabled])
@throttle_classes([BookingAuthThrottle])
def client_token_refresh(request):
    """Exchange a refresh token for a new access token."""
    raw = request.data.get('refresh')
    if not raw:
        return Response({'error': 'Refresh token required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        refresh = RefreshToken(raw)
    except TokenError:
        return Response({'error': 'Invalid or expired refresh token'}, status=status.HTTP_401_UNAUTHORIZED)

    client_id = refresh.get('client_id') or refresh.get('booking_client_id')
    client = Client.objects.filter(id=client_id, is_booking_enabled=True, is_verified=True).first()
    if client is None:
        return Response({'error': 'Invalid or expired refresh token'}, status=status.HTTP_401_UNAUTHORIZED)

    return Response({'access': str(refresh.access_token), 'refresh': raw})


@extend_schema(tags=['Booking Client - Authentication'])
@api_view(['POST'])
@authentication_classes([])
@permission_classes([PublicBookingEnabled])
@throttle_classes([BookingAuthThrottle])
def client_verify_email(request):
    """Verify client email with the emailed code; logs the client in on success."""
    email = request.data.get('email')
    code = request.data.get('code')
    token = request.data.get('token')

    client = None
    if email and code:
        candidate = _find_account(email)
        if candidate is not None and candidate.is_verified:
            return Response({'message': 'Email already verified'})
        if candidate is not None and _code_matches(candidate.verification_token, candidate.verification_sent_at, code):
            client = candidate
    elif token and len(str(token)) >= 20:
        # Long link-style tokens issued before codes were introduced
        client = Client.objects.filter(verification_token=token, is_booking_enabled=True).first()
        if client is not None and client.is_verified:
            return Response({'message': 'Email already verified'})
    else:
        return Response({'error': 'Email and code required'}, status=status.HTTP_400_BAD_REQUEST)

    if client is None:
        return Response({'error': 'Invalid or expired verification code'}, status=status.HTTP_400_BAD_REQUEST)

    client.is_verified = True
    client.verification_token = None
    client.last_login = timezone.now()
    client.save(update_fields=['is_verified', 'verification_token', 'last_login'])

    tokens = issue_client_tokens(client)
    return Response({
        'message': 'Email verified successfully',
        'access': tokens['access'],
        'refresh': tokens['refresh'],
        'client': BookingClientSerializer(client).data,
    })


@extend_schema(tags=['Booking Client - Authentication'])
@api_view(['POST'])
@authentication_classes([])
@permission_classes([PublicBookingEnabled])
@throttle_classes([BookingAuthThrottle])
def client_password_reset_request(request):
    """Request password reset. Same answer whether or not the account exists."""
    email = request.data.get('email')

    if not email:
        return Response({'error': 'Email required'}, status=status.HTTP_400_BAD_REQUEST)

    client = _find_account(email)
    if client is not None:
        code = _generate_code()
        client.reset_token = code
        client.reset_token_expires = timezone.now() + timedelta(minutes=CODE_TTL_MINUTES)
        client.save(update_fields=['reset_token', 'reset_token_expires'])
        send_password_reset_code(client, code, _language(request))

    return Response({'message': 'If the account exists, a reset code was sent to its email.'})


@extend_schema(tags=['Booking Client - Authentication'])
@api_view(['POST'])
@authentication_classes([])
@permission_classes([PublicBookingEnabled])
@throttle_classes([BookingAuthThrottle])
def client_password_reset_confirm(request):
    """Confirm password reset with the emailed code"""
    email = request.data.get('email')
    code = request.data.get('code') or request.data.get('token')
    new_password = request.data.get('new_password') or ''

    if not email or not code or not new_password:
        return Response({'error': 'Email, code and new password required'}, status=status.HTTP_400_BAD_REQUEST)

    if len(new_password) < 8 or not any(c.isdigit() for c in new_password):
        return Response(
            {'new_password': ['Password must be at least 8 characters long and contain a number.']},
            status=status.HTTP_400_BAD_REQUEST
        )

    client = _find_account(email)
    if client is None or not client.verify_reset_token(str(code).strip()):
        return Response({'error': 'Invalid or expired code'}, status=status.HTTP_400_BAD_REQUEST)

    client.set_password(new_password)
    client.reset_token = None
    client.reset_token_expires = None
    # Receiving the code proves the address is theirs.
    client.is_verified = True
    client.save(update_fields=['password_hash', 'reset_token', 'reset_token_expires', 'is_verified'])

    return Response({'message': 'Password reset successful'})


# ============================================================================
# PAYMENT WEBHOOK
# ============================================================================

@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def payment_webhook(request):
    """BOG payment gateway webhook"""
    try:
        booking = get_booking_payment_service().process_webhook(request.data)
    except Exception:
        # Let BOG retry: we couldn't confirm the payment state right now.
        logger.exception('Booking payment webhook failed')
        return Response({'error': 'Could not process payment notification'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Paid but waiting for the business to confirm: tell the customer we have
    # it. (An auto-confirmed booking already triggered its own "confirmed" mail.)
    if booking is not None and booking.payment_status in ('deposit_paid', 'fully_paid') and booking.status == 'pending':
        marker = (booking.payment_metadata or {}).get('created_email_sent')
        if not marker:
            metadata = dict(booking.payment_metadata or {})
            metadata['created_email_sent'] = True
            booking.payment_metadata = metadata
            booking.save(update_fields=['payment_metadata'])
            _queue_booking_email(booking, 'created', getattr(request.tenant, 'preferred_language', 'en'))

    return Response({'status': 'ok'})


# ============================================================================
# CLIENT PROFILE
# ============================================================================

@extend_schema(tags=['Booking Client - Profile'])
@api_view(['GET', 'PATCH'])
@authentication_classes([BookingClientJWTAuthentication])
@permission_classes([PublicBookingEnabled, IsAuthenticatedBookingClient])
def client_profile(request):
    """Get or update client profile"""
    client = request.user

    if request.method == 'PATCH':
        updated = []
        for field, attr in (('first_name', 'first_name'), ('last_name', 'last_name'), ('phone_number', 'phone')):
            if field in request.data:
                value = str(request.data.get(field) or '').strip()
                if field == 'first_name' and not value:
                    return Response({'first_name': ['This field may not be blank.']}, status=status.HTTP_400_BAD_REQUEST)
                setattr(client, attr, value[:100] if attr != 'phone' else value[:50])
                updated.append(attr)
        if updated:
            client.name = client.full_name or client.name
            client.save(update_fields=updated + ['name'])

    return Response(BookingClientSerializer(client).data)


# ============================================================================
# GUEST BOOKING (no account)
# ============================================================================

def _guest_client(first_name, last_name, phone, email):
    """Contact record for a guest: reuse the one with this phone, else create.

    Returns (client, matched_existing). An existing record is never modified
    beyond being flagged as a booking client — a visitor typing someone's
    phone number must not be able to rename them or change their email.
    """
    phone = phone.strip()
    client = Client.objects.filter(phone=phone).order_by('id').first()
    if client is not None:
        if not client.is_booking_enabled:
            client.is_booking_enabled = True
            client.save(update_fields=['is_booking_enabled'])
        return client, True

    full_name = f"{first_name} {last_name}".strip()
    client = Client.objects.create(
        name=full_name,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        email=(email or None),
        is_booking_enabled=True,
    )
    return client, False


@extend_schema(tags=['Booking Client - Bookings'], request=GuestBookingCreateSerializer)
@api_view(['POST'])
@authentication_classes([])
@permission_classes([PublicBookingEnabled])
@throttle_classes([BookingGuestThrottle])
def guest_booking_create(request):
    """Book without an account. Returns the booking and its private manage token."""
    serializer = GuestBookingCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    first_name = data['first_name'].strip()
    last_name = (data.get('last_name') or '').strip()
    phone = data['phone_number']
    email = (data.get('email') or '').strip()

    client, matched = _guest_client(first_name, last_name, phone, email)

    booking, error = _create_booking(request, serializer, client)
    if error is not None:
        return error

    if matched:
        # Keep what the guest typed where staff can see it, since the stored
        # contact was left untouched.
        entered = ', '.join(p for p in (f"{first_name} {last_name}".strip(), phone, email) if p)
        booking.staff_notes = f"Booked online as: {entered}"
        booking.save(update_fields=['staff_notes'])

    payload = _booking_payload(booking, request, include_client=False)
    payload['manage_token'] = booking.manage_token
    return Response(payload, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Booking Client - Bookings'])
@api_view(['GET'])
@authentication_classes([])
@permission_classes([PublicBookingEnabled])
def manage_booking(request, token):
    """View a booking through its private link."""
    booking = _booking_queryset().filter(manage_token=token).first() if token else None
    if booking is None:
        return Response({'error': 'Booking not found'}, status=status.HTTP_404_NOT_FOUND)

    # Returning from the bank: don't wait for the callback to learn the result.
    if booking.payment_method == 'card' and booking.payment_status == 'pending' and booking.bog_order_id:
        try:
            get_booking_payment_service().process_webhook({'body': {'external_order_id': booking.booking_number}})
            booking = _booking_queryset().get(pk=booking.pk)
        except Exception:
            logger.exception('Payment status check failed for booking %s', booking.booking_number)

    payload = _booking_payload(booking, request, include_client=False)
    payload['manage_token'] = booking.manage_token
    return Response(payload)


@extend_schema(tags=['Booking Client - Bookings'])
@api_view(['POST'])
@authentication_classes([])
@permission_classes([PublicBookingEnabled])
@throttle_classes([BookingGuestThrottle])
def manage_booking_cancel(request, token):
    """Cancel a booking through its private link."""
    booking = _booking_queryset().filter(manage_token=token).first() if token else None
    if booking is None:
        return Response({'error': 'Booking not found'}, status=status.HTTP_404_NOT_FOUND)

    booking_settings = get_or_create_booking_settings()
    can_cancel, reason = can_cancel_booking(booking, booking_settings)
    if not can_cancel:
        return Response({'error': reason}, status=status.HTTP_400_BAD_REQUEST)

    _cancel_booking(booking, request.data.get('reason', ''))

    payload = _booking_payload(booking, request, include_client=False)
    payload['manage_token'] = booking.manage_token
    return Response(payload)


# ============================================================================
# CLIENT VIEWSETS
# ============================================================================

@extend_schema_view(
    list=extend_schema(tags=['Booking Client - Services']),
    retrieve=extend_schema(tags=['Booking Client - Services'])
)
class ClientServiceCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """View service categories (public read-only)"""
    queryset = ServiceCategory.objects.filter(is_active=True)
    serializer_class = ServiceCategorySerializer
    authentication_classes = []
    permission_classes = [PublicBookingEnabled]
    pagination_class = None
    feature_required = 'booking_management'

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # Get language from Accept-Language header or query param
        context['language'] = self.request.query_params.get('lang', 'en')
        return context


@extend_schema_view(
    list=extend_schema(tags=['Booking Client - Services']),
    retrieve=extend_schema(tags=['Booking Client - Services']),
    slots=extend_schema(tags=['Booking Client - Services'])
)
class ClientServiceViewSet(viewsets.ReadOnlyModelViewSet):
    """View services (public read-only)"""
    queryset = Service.objects.filter(status='active').select_related('category').prefetch_related(
        Prefetch('staff_members', queryset=BookingStaff.objects.select_related('user').prefetch_related('services')),
    )
    serializer_class = PublicServiceSerializer
    authentication_classes = []
    permission_classes = [PublicBookingEnabled]
    pagination_class = None
    filterset_fields = ['category', 'booking_type']
    search_fields = ['name']
    feature_required = 'booking_management'

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    @action(detail=True, methods=['get'])
    def slots(self, request, pk=None):
        """Get available time slots for a service"""
        service = self.get_object()
        date_str = request.query_params.get('date')
        staff_id = request.query_params.get('staff_id')
        language = request.query_params.get('lang', 'en')

        if not date_str:
            return Response({'error': 'Date parameter required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': 'Invalid date format. Use YYYY-MM-DD'}, status=status.HTTP_400_BAD_REQUEST)

        # Get staff if specified
        staff = None
        if staff_id:
            try:
                staff = BookingStaff.objects.get(id=staff_id)
            except (BookingStaff.DoesNotExist, ValueError):
                return Response({'error': 'Staff not found'}, status=status.HTTP_400_BAD_REQUEST)
            if not staff_can_perform(service, staff):
                return Response({'error': 'Staff not found'}, status=status.HTTP_400_BAD_REQUEST)

        # Generate available slots
        slots = generate_available_slots(service, date, staff, language)

        return Response({
            'date': date_str,
            'service': service.name if isinstance(service.name, str) else service.name.get(language, service.name.get('en', '')),
            'slots': slots
        })


@extend_schema_view(
    list=extend_schema(tags=['Booking Client - Staff']),
    retrieve=extend_schema(tags=['Booking Client - Staff'])
)
class ClientBookingStaffViewSet(viewsets.ReadOnlyModelViewSet):
    """View staff members (public read-only)"""
    queryset = BookingStaff.objects.filter(is_active_for_bookings=True).select_related('user').prefetch_related('services')
    serializer_class = PublicBookingStaffSerializer
    authentication_classes = []
    permission_classes = [PublicBookingEnabled]
    pagination_class = None
    feature_required = 'booking_management'


@extend_schema_view(
    list=extend_schema(tags=['Booking Client - Bookings']),
    retrieve=extend_schema(tags=['Booking Client - Bookings']),
    create=extend_schema(tags=['Booking Client - Bookings']),
    cancel=extend_schema(tags=['Booking Client - Bookings']),
    reschedule=extend_schema(tags=['Booking Client - Bookings']),
    rate=extend_schema(tags=['Booking Client - Bookings'])
)
class ClientBookingViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """A logged-in client's own bookings.

    Deliberately not a ModelViewSet: clients change a booking only through the
    cancel / reschedule / rate actions, never by writing its fields directly.
    """
    serializer_class = PublicBookingSerializer
    authentication_classes = [BookingClientJWTAuthentication]
    permission_classes = [PublicBookingEnabled, IsAuthenticatedBookingClient]
    feature_required = 'booking_management'

    def get_queryset(self):
        """Get only client's own bookings"""
        return _booking_queryset().filter(client=self.request.user).order_by('-date', '-start_time')

    def get_serializer_class(self):
        if self.action == 'create':
            return BookingCreateSerializer
        return PublicBookingSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    def create(self, request, *args, **kwargs):
        """Create a booking and, for card payment, return the payment URL"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        booking, error = _create_booking(request, serializer, request.user)
        if error is not None:
            return error

        payload = _booking_payload(booking, request)
        payload['manage_token'] = booking.manage_token
        return Response(payload, status=status.HTTP_201_CREATED)

    def retrieve(self, request, *args, **kwargs):
        return Response(_booking_payload(self.get_object(), request))

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel booking"""
        booking = self.get_object()

        booking_settings = get_or_create_booking_settings()

        # Check if can cancel
        can_cancel, reason = can_cancel_booking(booking, booking_settings)
        if not can_cancel:
            return Response({'error': reason}, status=status.HTTP_400_BAD_REQUEST)

        _cancel_booking(booking, request.data.get('reason', ''))

        return Response(_booking_payload(booking, request))

    @action(detail=True, methods=['post'])
    def reschedule(self, request, pk=None):
        """Client reschedules their own booking"""
        booking = self.get_object()

        if booking.status not in ['pending', 'confirmed']:
            return Response({'error': 'Cannot reschedule this booking'}, status=status.HTTP_400_BAD_REQUEST)

        new_date_str = request.data.get('date')
        new_time_str = request.data.get('start_time')

        if not new_date_str or not new_time_str:
            return Response(
                {'error': 'date and start_time are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            new_date = datetime.strptime(new_date_str, '%Y-%m-%d').date()
            new_time = datetime.strptime(new_time_str[:5], '%H:%M').time()
        except ValueError:
            return Response(
                {'error': 'Invalid date or time format. Use YYYY-MM-DD and HH:MM'},
                status=status.HTTP_400_BAD_REQUEST
            )

        booking_settings = get_or_create_booking_settings()

        # Moving a booking is bound by the same notice period as cancelling it,
        # and the new time by the same lead time as a new booking.
        can_change, reason = can_cancel_booking(booking, booking_settings)
        if not can_change:
            return Response({'error': reason.replace('cancelled', 'changed')}, status=status.HTTP_400_BAD_REQUEST)

        ok, error_msg = check_booking_window(new_date, new_time, booking_settings)
        if not ok:
            return Response({'error': error_msg}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            if booking.staff_id:
                BookingStaff.objects.select_for_update().filter(pk=booking.staff_id).first()

            # Validate availability (ignoring this booking's own current slot)
            is_available, error_msg = validate_booking_availability(
                booking.service,
                booking.staff,
                new_date,
                new_time,
                exclude_booking_id=booking.pk,
                booking_settings=booking_settings,
            )

            if not is_available:
                return Response({'error': error_msg}, status=status.HTTP_400_BAD_REQUEST)

            # Update booking
            if booking.staff is None:
                booking.staff = find_available_staff(booking.service, new_date, new_time, exclude_booking_id=booking.pk)
            booking.date = new_date
            booking.start_time = new_time
            end_dt = datetime.combine(new_date, new_time) + timedelta(minutes=booking.service.total_duration_minutes)
            booking.end_time = end_dt.time()
            booking.save(update_fields=['staff', 'date', 'start_time', 'end_time', 'updated_at'])

        return Response(_booking_payload(booking, request))

    @action(detail=True, methods=['post'])
    def rate(self, request, pk=None):
        """Client rates a completed booking"""
        booking = self.get_object()

        if booking.status != 'completed':
            return Response({'error': 'Can only rate completed bookings'}, status=status.HTTP_400_BAD_REQUEST)

        if booking.rating is not None:
            return Response({'error': 'This booking has already been rated'}, status=status.HTTP_400_BAD_REQUEST)

        rating = request.data.get('rating')
        review_text = request.data.get('review', '')

        if rating is None:
            return Response({'error': 'Rating is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            rating = int(rating)
        except (ValueError, TypeError):
            return Response({'error': 'Rating must be an integer'}, status=status.HTTP_400_BAD_REQUEST)

        if rating < 1 or rating > 5:
            return Response({'error': 'Rating must be between 1 and 5'}, status=status.HTTP_400_BAD_REQUEST)

        # Save rating to booking
        booking.rating = rating
        booking.review = review_text
        booking.save(update_fields=['rating', 'review'])

        # Update staff average rating
        if booking.staff:
            booking.staff.update_rating(rating)

        return Response(_booking_payload(booking, request))


@extend_schema_view(
    list=extend_schema(tags=['Booking Client - Recurring Bookings']),
    retrieve=extend_schema(tags=['Booking Client - Recurring Bookings']),
    create=extend_schema(tags=['Booking Client - Recurring Bookings']),
    update=extend_schema(tags=['Booking Client - Recurring Bookings']),
    partial_update=extend_schema(tags=['Booking Client - Recurring Bookings']),
    pause=extend_schema(tags=['Booking Client - Recurring Bookings']),
    resume=extend_schema(tags=['Booking Client - Recurring Bookings'])
)
class ClientRecurringBookingViewSet(viewsets.ModelViewSet):
    """Manage recurring bookings"""
    serializer_class = RecurringBookingSerializer
    authentication_classes = [BookingClientJWTAuthentication]
    permission_classes = [PublicBookingEnabled, IsAuthenticatedBookingClient]
    feature_required = 'booking_management'

    def get_queryset(self):
        """Get only client's own recurring bookings"""
        return RecurringBooking.objects.filter(client=self.request.user).select_related('client', 'service', 'staff')

    def get_serializer_class(self):
        if self.action == 'create':
            return RecurringBookingCreateSerializer
        return RecurringBookingSerializer

    def perform_create(self, serializer):
        """Create recurring booking"""
        serializer.save(client=self.request.user)

    @action(detail=True, methods=['post'])
    def pause(self, request, pk=None):
        """Pause recurring booking"""
        recurring = self.get_object()
        recurring.status = 'paused'
        recurring.save(update_fields=['status'])
        return Response(RecurringBookingSerializer(recurring).data)

    @action(detail=True, methods=['post'])
    def resume(self, request, pk=None):
        """Resume recurring booking"""
        recurring = self.get_object()
        recurring.status = 'active'
        recurring.save(update_fields=['status'])
        return Response(RecurringBookingSerializer(recurring).data)
