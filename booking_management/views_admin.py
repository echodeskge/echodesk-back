from rest_framework import viewsets, status, permissions
from rest_framework.decorators import api_view, permission_classes, action
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Q, Count, Sum, Avg, Prefetch
from datetime import datetime, timedelta, date
from drf_spectacular.utils import extend_schema, extend_schema_view
from .models import (
    Service, ServiceCategory, BookingStaff,
    Booking, RecurringBooking, StaffAvailability, StaffException,
    BookingSettings
)
from social_integrations.models import Client
from .serializers import (
    BookingClientSerializer, ServiceListSerializer, ServiceDetailSerializer,
    ServiceCategorySerializer, BookingStaffSerializer, BookingStaffCreateSerializer,
    BookingListSerializer, BookingDetailSerializer,
    RecurringBookingSerializer, StaffAvailabilitySerializer,
    StaffExceptionSerializer, BookingSettingsSerializer
)
from .permissions import HasBookingManagementFeature
from .utils import generate_available_slots, can_cancel_booking
from .payment_service import get_booking_payment_service
import logging

logger = logging.getLogger(__name__)


def _optimized_booking_qs(qs=None):
    """Apply common select_related/prefetch_related for BookingListSerializer."""
    if qs is None:
        qs = Booking.objects.all()
    return qs.select_related(
        'client', 'service', 'service__category', 'staff', 'staff__user',
    ).prefetch_related(
        Prefetch(
            'service__staff_members',
            queryset=BookingStaff.objects.select_related('user').prefetch_related('services'),
        ),
        'staff__services',
    )


# ============================================================================
# DASHBOARD & ANALYTICS
# ============================================================================

@extend_schema(tags=['Booking Admin - Dashboard'])
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, HasBookingManagementFeature])
def dashboard_stats(request):
    """Get dashboard statistics"""
    today = timezone.now().date()

    # Today's bookings
    today_bookings = Booking.objects.filter(date=today).exclude(status='cancelled')

    # This week's stats
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)
    week_bookings = Booking.objects.filter(
        date__gte=week_start,
        date__lt=week_end
    ).exclude(status='cancelled')

    # Revenue stats (exclude refunded and cancelled bookings)
    total_revenue = Booking.objects.filter(
        payment_status__in=['fully_paid', 'deposit_paid']
    ).exclude(
        status='cancelled'
    ).exclude(
        payment_status__in=['refunded', 'partially_refunded']
    ).aggregate(total=Sum('paid_amount'))['total'] or 0

    week_revenue = week_bookings.filter(
        payment_status__in=['fully_paid', 'deposit_paid']
    ).exclude(
        payment_status__in=['refunded', 'partially_refunded']
    ).aggregate(total=Sum('paid_amount'))['total'] or 0

    # Status breakdown
    status_counts = today_bookings.values('status').annotate(count=Count('id'))

    # Popular services
    popular_services = Booking.objects.filter(
        date__gte=week_start
    ).values('service__name').annotate(
        count=Count('id')
    ).order_by('-count')[:5]

    return Response({
        'today': {
            'total_bookings': today_bookings.count(),
            'confirmed': today_bookings.filter(status='confirmed').count(),
            'pending': today_bookings.filter(status='pending').count(),
            'completed': today_bookings.filter(status='completed').count(),
            'status_breakdown': list(status_counts),
            'bookings': BookingListSerializer(
                _optimized_booking_qs(today_bookings).order_by('start_time'),
                many=True
            ).data
        },
        'week': {
            'total_bookings': week_bookings.count(),
            'revenue': float(week_revenue),
            'popular_services': list(popular_services)
        },
        'overall': {
            'total_clients': Client.objects.count(),
            'total_revenue': float(total_revenue),
            'active_staff': BookingStaff.objects.filter(is_active_for_bookings=True).count()
        }
    })


@extend_schema(tags=['Booking Admin - Dashboard'])
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, HasBookingManagementFeature])
def staff_schedule(request):
    """Get staff schedule for a specific date"""
    date_str = request.query_params.get('date')
    staff_id = request.query_params.get('staff_id')

    if not date_str:
        date_obj = timezone.now().date()
    else:
        try:
            date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )

    # Filter by staff if specified
    queryset = Booking.objects.filter(date=date_obj).exclude(status='cancelled')
    if staff_id:
        queryset = queryset.filter(staff_id=staff_id)

    # Get staff availability
    staff_list = BookingStaff.objects.select_related('user').prefetch_related('services').filter(is_active_for_bookings=True)
    if staff_id:
        staff_list = staff_list.filter(id=staff_id)

    # Pre-optimize the booking queryset
    optimized_qs = _optimized_booking_qs(queryset)

    schedule = []
    for staff_member in staff_list:
        staff_bookings = optimized_qs.filter(staff=staff_member).order_by('start_time')

        # Get availability
        from .utils import get_staff_availability
        availability = get_staff_availability(staff_member, date_obj)

        schedule.append({
            'staff': BookingStaffSerializer(staff_member).data,
            'availability': availability,
            'bookings': BookingListSerializer(staff_bookings, many=True).data,
            'booking_count': staff_bookings.count()
        })

    return Response({
        'date': date_str or str(date_obj),
        'schedule': schedule
    })


# ============================================================================
# SERVICE MANAGEMENT
# ============================================================================

@extend_schema_view(
    list=extend_schema(tags=['Booking Admin - Services']),
    retrieve=extend_schema(tags=['Booking Admin - Services']),
    create=extend_schema(tags=['Booking Admin - Services']),
    update=extend_schema(tags=['Booking Admin - Services']),
    partial_update=extend_schema(tags=['Booking Admin - Services']),
    destroy=extend_schema(tags=['Booking Admin - Services'])
)
class AdminServiceCategoryViewSet(viewsets.ModelViewSet):
    """Admin service category management"""
    queryset = ServiceCategory.objects.all()
    serializer_class = ServiceCategorySerializer
    permission_classes = [permissions.IsAuthenticated, HasBookingManagementFeature]
    feature_required = 'booking_management'

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context


@extend_schema_view(
    list=extend_schema(tags=['Booking Admin - Services']),
    retrieve=extend_schema(tags=['Booking Admin - Services']),
    create=extend_schema(tags=['Booking Admin - Services']),
    update=extend_schema(tags=['Booking Admin - Services']),
    partial_update=extend_schema(tags=['Booking Admin - Services']),
    destroy=extend_schema(tags=['Booking Admin - Services']),
    activate=extend_schema(tags=['Booking Admin - Services']),
    deactivate=extend_schema(tags=['Booking Admin - Services'])
)
class AdminServiceViewSet(viewsets.ModelViewSet):
    """Admin service management"""
    queryset = Service.objects.select_related('category').prefetch_related(
        Prefetch('staff_members', queryset=BookingStaff.objects.select_related('user').prefetch_related('services')),
    ).all()
    permission_classes = [permissions.IsAuthenticated, HasBookingManagementFeature]
    feature_required = 'booking_management'
    filterset_fields = ['category', 'status', 'booking_type']
    search_fields = ['name']

    def get_serializer_class(self):
        if self.action in ['retrieve', 'create', 'update', 'partial_update']:
            return ServiceDetailSerializer
        return ServiceListSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """Activate service"""
        service = self.get_object()
        service.status = 'active'
        service.save(update_fields=['status'])
        return Response(ServiceDetailSerializer(service).data)

    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        """Deactivate service"""
        service = self.get_object()
        service.status = 'inactive'
        service.save(update_fields=['status'])
        return Response(ServiceDetailSerializer(service).data)


# ============================================================================
# STAFF MANAGEMENT
# ============================================================================

@extend_schema_view(
    list=extend_schema(tags=['Booking Admin - Staff']),
    retrieve=extend_schema(tags=['Booking Admin - Staff']),
    create=extend_schema(tags=['Booking Admin - Staff']),
    update=extend_schema(tags=['Booking Admin - Staff']),
    partial_update=extend_schema(tags=['Booking Admin - Staff']),
    destroy=extend_schema(tags=['Booking Admin - Staff']),
    availability=extend_schema(tags=['Booking Admin - Staff']),
    exceptions=extend_schema(tags=['Booking Admin - Staff']),
    bookings=extend_schema(tags=['Booking Admin - Staff']),
    toggle_active=extend_schema(tags=['Booking Admin - Staff']),
    available_users=extend_schema(tags=['Booking Admin - Staff'])
)
class AdminBookingStaffViewSet(viewsets.ModelViewSet):
    """Admin staff management"""
    queryset = BookingStaff.objects.select_related('user').prefetch_related('services').all()
    serializer_class = BookingStaffSerializer
    permission_classes = [permissions.IsAuthenticated, HasBookingManagementFeature]
    feature_required = 'booking_management'
    filterset_fields = ['is_active_for_bookings']
    search_fields = ['user__first_name', 'user__last_name']

    def get_serializer_class(self):
        """Use different serializer for create/update operations"""
        if self.action in ['create', 'update', 'partial_update']:
            return BookingStaffCreateSerializer
        return BookingStaffSerializer

    @action(detail=False, methods=['get'])
    def available_users(self, request):
        """Get users who are not already booking staff"""
        from users.models import User

        # Get IDs of users who are already staff
        staff_user_ids = BookingStaff.objects.values_list('user_id', flat=True)

        # Get active users who are not staff
        available_users = User.objects.filter(
            is_active=True
        ).exclude(
            id__in=staff_user_ids
        ).order_by('first_name', 'last_name')

        # Search filter
        search = request.query_params.get('search')
        if search:
            available_users = available_users.filter(
                Q(first_name__icontains=search) |
                Q(last_name__icontains=search) |
                Q(email__icontains=search)
            )

        # Simple serialization
        data = [
            {
                'id': user.id,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'full_name': user.get_full_name() or user.email,
            }
            for user in available_users[:50]  # Limit to 50 results
        ]

        return Response(data)

    @action(detail=True, methods=['get'])
    def availability(self, request, pk=None):
        """Get staff availability schedule"""
        staff = self.get_object()
        availability = StaffAvailability.objects.filter(staff=staff).order_by('day_of_week')
        return Response(StaffAvailabilitySerializer(availability, many=True).data)

    @action(detail=True, methods=['get'])
    def exceptions(self, request, pk=None):
        """Get staff exceptions (vacations, special hours)"""
        staff = self.get_object()
        # Get future exceptions
        exceptions = StaffException.objects.filter(
            staff=staff,
            date__gte=timezone.now().date()
        ).order_by('date')
        return Response(StaffExceptionSerializer(exceptions, many=True).data)

    @action(detail=True, methods=['get'])
    def bookings(self, request, pk=None):
        """Get staff bookings"""
        staff = self.get_object()
        date_str = request.query_params.get('date')

        queryset = _optimized_booking_qs(
            Booking.objects.filter(staff=staff).exclude(status='cancelled')
        )

        if date_str:
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
                queryset = queryset.filter(date=date_obj)
            except ValueError:
                pass
        else:
            # Default to upcoming bookings
            queryset = queryset.filter(date__gte=timezone.now().date())

        queryset = queryset.order_by('date', 'start_time')
        return Response(BookingListSerializer(queryset, many=True).data)

    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Toggle staff active status"""
        staff = self.get_object()
        staff.is_active_for_bookings = not staff.is_active_for_bookings
        staff.save(update_fields=['is_active_for_bookings'])
        return Response(BookingStaffSerializer(staff).data)


@extend_schema_view(
    list=extend_schema(tags=['Booking Admin - Staff']),
    retrieve=extend_schema(tags=['Booking Admin - Staff']),
    create=extend_schema(tags=['Booking Admin - Staff']),
    update=extend_schema(tags=['Booking Admin - Staff']),
    partial_update=extend_schema(tags=['Booking Admin - Staff']),
    destroy=extend_schema(tags=['Booking Admin - Staff'])
)
class AdminStaffAvailabilityViewSet(viewsets.ModelViewSet):
    """Admin staff availability management"""
    queryset = StaffAvailability.objects.all()
    serializer_class = StaffAvailabilitySerializer
    permission_classes = [permissions.IsAuthenticated, HasBookingManagementFeature]
    feature_required = 'booking_management'
    filterset_fields = ['staff', 'day_of_week', 'is_available']


@extend_schema_view(
    list=extend_schema(tags=['Booking Admin - Staff']),
    retrieve=extend_schema(tags=['Booking Admin - Staff']),
    create=extend_schema(tags=['Booking Admin - Staff']),
    update=extend_schema(tags=['Booking Admin - Staff']),
    partial_update=extend_schema(tags=['Booking Admin - Staff']),
    destroy=extend_schema(tags=['Booking Admin - Staff'])
)
class AdminStaffExceptionViewSet(viewsets.ModelViewSet):
    """Admin staff exception management"""
    queryset = StaffException.objects.all()
    serializer_class = StaffExceptionSerializer
    permission_classes = [permissions.IsAuthenticated, HasBookingManagementFeature]
    feature_required = 'booking_management'
    filterset_fields = ['staff', 'is_available']

    def get_queryset(self):
        """Filter future exceptions by default"""
        queryset = super().get_queryset()
        show_past = self.request.query_params.get('show_past', 'false').lower() == 'true'

        if not show_past:
            queryset = queryset.filter(date__gte=timezone.now().date())

        return queryset.order_by('date')


# ============================================================================
# BOOKING MANAGEMENT
# ============================================================================

@extend_schema_view(
    list=extend_schema(tags=['Booking Admin - Bookings']),
    retrieve=extend_schema(tags=['Booking Admin - Bookings']),
    create=extend_schema(tags=['Booking Admin - Bookings']),
    update=extend_schema(tags=['Booking Admin - Bookings']),
    partial_update=extend_schema(tags=['Booking Admin - Bookings']),
    destroy=extend_schema(tags=['Booking Admin - Bookings']),
    confirm=extend_schema(tags=['Booking Admin - Bookings']),
    complete=extend_schema(tags=['Booking Admin - Bookings']),
    cancel=extend_schema(tags=['Booking Admin - Bookings']),
    assign_staff=extend_schema(tags=['Booking Admin - Bookings']),
    reschedule=extend_schema(tags=['Booking Admin - Bookings']),
    check_payment_status=extend_schema(tags=['Booking Admin - Bookings'])
)
class AdminBookingViewSet(viewsets.ModelViewSet):
    """Admin booking management - view and manage all bookings"""
    serializer_class = BookingListSerializer
    permission_classes = [permissions.IsAuthenticated, HasBookingManagementFeature]
    feature_required = 'booking_management'
    filterset_fields = ['status', 'payment_status', 'service', 'staff', 'client']
    search_fields = ['booking_number', 'client__email', 'client__first_name', 'client__last_name']

    def get_queryset(self):
        """Get all bookings with filters"""
        queryset = _optimized_booking_qs().order_by('-date', '-start_time')

        # Date range filter
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')

        if date_from:
            try:
                queryset = queryset.filter(date__gte=datetime.strptime(date_from, '%Y-%m-%d').date())
            except ValueError:
                pass

        if date_to:
            try:
                queryset = queryset.filter(date__lte=datetime.strptime(date_to, '%Y-%m-%d').date())
            except ValueError:
                pass

        return queryset

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return BookingDetailSerializer
        return BookingListSerializer

    @action(detail=True, methods=['post'])
    def confirm(self, request, pk=None):
        """Confirm booking — requires deposit or full payment unless force=true"""
        booking = self.get_object()

        if booking.status != 'pending':
            return Response(
                {'error': f'Cannot confirm booking with status: {booking.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check payment unless admin forces confirmation
        force = request.data.get('force', False)
        if not force and booking.payment_status not in ['deposit_paid', 'fully_paid']:
            return Response(
                {'error': 'Cannot confirm booking without payment. Use force=true to override.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        booking.confirm()
        return Response(BookingDetailSerializer(booking).data)

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Mark booking as completed"""
        booking = self.get_object()

        if booking.status not in ['confirmed', 'in_progress']:
            return Response(
                {'error': f'Cannot complete booking with status: {booking.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        booking.complete()
        return Response(BookingDetailSerializer(booking).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel booking (admin can cancel anytime)"""
        booking = self.get_object()

        if booking.status in ['completed', 'cancelled']:
            return Response(
                {'error': f'Cannot cancel booking with status: {booking.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        cancellation_reason = request.data.get('reason', 'Cancelled by admin')
        booking.cancel(cancelled_by='admin', reason=cancellation_reason)

        # Initiate refund if applicable
        if booking.paid_amount > 0:
            refund_requested = request.data.get('refund', True)
            if refund_requested:
                try:
                    payment_service = get_booking_payment_service()
                    refund_result = payment_service.initiate_refund(booking)
                except Exception as e:
                    logger.error(f"Failed to initiate refund for {booking.booking_number}: {str(e)}")

        return Response(BookingDetailSerializer(booking).data)

    @action(detail=True, methods=['post'])
    def assign_staff(self, request, pk=None):
        """Assign or reassign staff to booking"""
        booking = self.get_object()
        staff_id = request.data.get('staff_id')

        if not staff_id:
            return Response(
                {'error': 'staff_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            staff = BookingStaff.objects.get(id=staff_id)
        except BookingStaff.DoesNotExist:
            return Response(
                {'error': 'Staff not found'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if staff is available for this service
        if not staff.services.filter(id=booking.service.id).exists():
            return Response(
                {'error': 'Staff is not assigned to this service'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check availability
        from .utils import is_slot_booked
        if is_slot_booked(staff, booking.date, booking.start_time, booking.service.total_duration_minutes):
            return Response(
                {'error': 'Staff is not available at this time'},
                status=status.HTTP_400_BAD_REQUEST
            )

        booking.staff = staff
        booking.save(update_fields=['staff'])

        return Response(BookingDetailSerializer(booking).data)

    @action(detail=True, methods=['post'])
    def reschedule(self, request, pk=None):
        """Reschedule booking to new date/time"""
        booking = self.get_object()

        new_date_str = request.data.get('date')
        new_time_str = request.data.get('start_time')

        if not new_date_str or not new_time_str:
            return Response(
                {'error': 'date and start_time are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            new_date = datetime.strptime(new_date_str, '%Y-%m-%d').date()
            new_time = datetime.strptime(new_time_str, '%H:%M').time()
        except ValueError:
            return Response(
                {'error': 'Invalid date or time format'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate availability
        from .utils import validate_booking_availability
        is_available, error_msg = validate_booking_availability(
            booking.service,
            booking.staff,
            new_date,
            new_time,
            exclude_booking_id=booking.pk,
        )

        if not is_available:
            return Response({'error': error_msg}, status=status.HTTP_400_BAD_REQUEST)

        # Update booking
        booking.date = new_date
        booking.start_time = new_time
        end_dt = datetime.combine(new_date, new_time) + timedelta(minutes=booking.service.total_duration_minutes)
        booking.end_time = end_dt.time()
        booking.save(update_fields=['date', 'start_time', 'end_time'])

        # TODO: Send notification to client about reschedule

        return Response(BookingDetailSerializer(booking).data)

    @action(detail=True, methods=['get'])
    def check_payment_status(self, request, pk=None):
        """Check payment status from BOG"""
        booking = self.get_object()

        try:
            payment_service = get_booking_payment_service()
            payment_status = payment_service.check_payment_status(booking)
            return Response(payment_status)
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


@extend_schema_view(
    list=extend_schema(tags=['Booking Admin - Bookings']),
    retrieve=extend_schema(tags=['Booking Admin - Bookings']),
    create=extend_schema(tags=['Booking Admin - Bookings']),
    update=extend_schema(tags=['Booking Admin - Bookings']),
    partial_update=extend_schema(tags=['Booking Admin - Bookings']),
    destroy=extend_schema(tags=['Booking Admin - Bookings']),
    pause=extend_schema(tags=['Booking Admin - Bookings']),
    resume=extend_schema(tags=['Booking Admin - Bookings']),
    cancel=extend_schema(tags=['Booking Admin - Bookings'])
)
class AdminRecurringBookingViewSet(viewsets.ModelViewSet):
    """Admin recurring booking management"""
    queryset = RecurringBooking.objects.select_related(
        'client', 'service', 'service__category', 'staff', 'staff__user',
    ).prefetch_related(
        Prefetch(
            'service__staff_members',
            queryset=BookingStaff.objects.select_related('user').prefetch_related('services'),
        ),
        'staff__services',
    ).all()
    serializer_class = RecurringBookingSerializer
    permission_classes = [permissions.IsAuthenticated, HasBookingManagementFeature]
    feature_required = 'booking_management'
    filterset_fields = ['status', 'frequency', 'client', 'service']

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

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel recurring booking"""
        recurring = self.get_object()
        recurring.status = 'cancelled'
        recurring.end_date = timezone.now().date()
        recurring.save(update_fields=['status', 'end_date'])
        return Response(RecurringBookingSerializer(recurring).data)


# ============================================================================
# CLIENT MANAGEMENT
# ============================================================================

@extend_schema_view(
    list=extend_schema(tags=['Booking Admin - Clients']),
    retrieve=extend_schema(tags=['Booking Admin - Clients']),
    bookings=extend_schema(tags=['Booking Admin - Clients']),
    stats=extend_schema(tags=['Booking Admin - Clients'])
)
class AdminBookingClientViewSet(viewsets.ReadOnlyModelViewSet):
    """Admin client viewing - shows clients with booking enabled from unified Client model"""
    queryset = Client.objects.filter(is_booking_enabled=True)
    serializer_class = BookingClientSerializer
    permission_classes = [permissions.IsAuthenticated, HasBookingManagementFeature]
    feature_required = 'booking_management'
    search_fields = ['email', 'first_name', 'last_name', 'phone', 'name']
    filterset_fields = ['is_verified']

    @action(detail=True, methods=['get'])
    def bookings(self, request, pk=None):
        """Get client's booking history"""
        client = self.get_object()
        bookings = _optimized_booking_qs(
            Booking.objects.filter(client=client)
        ).order_by('-date', '-start_time')
        return Response(BookingListSerializer(bookings, many=True).data)

    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        """Get client statistics"""
        client = self.get_object()

        stats = Booking.objects.filter(client=client).aggregate(
            total_bookings=Count('id'),
            completed_bookings=Count('id', filter=Q(status='completed')),
            cancelled_bookings=Count('id', filter=Q(status='cancelled')),
            total_spent=Sum('paid_amount', filter=Q(payment_status__in=['fully_paid', 'deposit_paid'])),
        )
        total_bookings = stats['total_bookings']

        last_booking = None
        if total_bookings > 0:
            last = _optimized_booking_qs(
                Booking.objects.filter(client=client)
            ).order_by('-date', '-start_time').first()
            if last:
                last_booking = BookingListSerializer(last).data

        return Response({
            'total_bookings': total_bookings,
            'completed_bookings': stats['completed_bookings'],
            'cancelled_bookings': stats['cancelled_bookings'],
            'total_spent': float(stats['total_spent'] or 0),
            'last_booking': last_booking,
        })


# ============================================================================
# SETTINGS MANAGEMENT
# ============================================================================

@extend_schema(tags=['Booking Admin - Settings'])
@api_view(['GET', 'PUT', 'PATCH'])
@permission_classes([permissions.IsAuthenticated, HasBookingManagementFeature])
def booking_settings(request):
    """Get or update booking settings"""
    from .models import BookingSettings
    from django.db import connection
    from tenants.models import Tenant

    # Get current tenant
    try:
        tenant = Tenant.objects.get(schema_name=connection.schema_name)
    except Tenant.DoesNotExist:
        return Response(
            {'error': 'Tenant not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    # Get or create settings
    settings, created = BookingSettings.objects.get_or_create(tenant=tenant)

    if request.method == 'GET':
        serializer = BookingSettingsSerializer(settings)
        return Response(serializer.data)

    elif request.method in ['PUT', 'PATCH']:
        partial = request.method == 'PATCH'
        serializer = BookingSettingsSerializer(settings, data=request.data, partial=partial)

        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
