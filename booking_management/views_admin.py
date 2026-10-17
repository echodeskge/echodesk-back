from decimal import Decimal

from rest_framework import viewsets, status, permissions
from rest_framework.decorators import api_view, permission_classes, action
from rest_framework.response import Response
from django.db import transaction
from django.utils import timezone
from django.db.models import Q, Count, Sum, Avg, Prefetch
from datetime import datetime, timedelta, date
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from .models import (
    Service, ServiceCategory, BookingStaff,
    Booking, RecurringBooking, StaffAvailability, StaffException,
    BookingSettings
)
from social_integrations.models import Client
from .serializers import (
    AdminBookingCreateSerializer, BookingTestSmsSerializer, BookingTestSmsResultSerializer,
    BookingClientSerializer, ServiceListSerializer, ServiceDetailSerializer, ServiceCreateSerializer,
    ServiceCategorySerializer, BookingStaffSerializer, BookingStaffCreateSerializer,
    BookingListSerializer, BookingDetailSerializer, BookingRowSerializer, AdminBookingUpdateSerializer,
    RecurringBookingSerializer, AdminRecurringBookingCreateSerializer, StaffAvailabilitySerializer,
    StaffExceptionSerializer, BookingSettingsSerializer,
    BookingConfirmRequestSerializer, BookingCancelRequestSerializer, BookingAssignStaffRequestSerializer,
    BookingRescheduleRequestSerializer, BookingRecordPaymentRequestSerializer, BookingErrorSerializer,
    AdminSlotsResponseSerializer, BookingDashboardSerializer, StaffScheduleSerializer,
    AvailableUserSerializer, BookingClientStatsSerializer,
)
from .permissions import (
    HasBookingManagementFeature, IsBookingAdmin, IsBookingAdminForDestroy, IsBookingAdminForWrites,
)
from .staff_notifications import notify_assigned_staff
from .utils import generate_available_slots, can_cancel_booking, get_booking_settings, tenant_now
from .payment_service import get_booking_payment_service
import logging

logger = logging.getLogger(__name__)

BOOKING_ERROR = OpenApiResponse(BookingErrorSerializer, description='{"error": "..."}')

LITE_PARAM = OpenApiParameter(
    'lite', OpenApiTypes.BOOL, required=False,
    description='`1`/`true`: bookings use the slim `BookingRow` shape (no nested service roster).',
)
LANG_PARAM = OpenApiParameter(
    'lang', OpenApiTypes.STR, required=False, description='Language for *_display fields (en / ka; default en)',
)


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


def _row_booking_qs(qs=None):
    """What BookingRowSerializer reads, nothing more."""
    if qs is None:
        qs = Booking.objects.all()
    return qs.select_related('client', 'service', 'staff', 'staff__user')


def _wants_lite(request):
    return str(request.query_params.get('lite', '')).lower() in ('1', 'true', 'yes')


def _language(request):
    return request.query_params.get('lang', 'en')


def _serialize_bookings(request, queryset, many=True):
    """Full BookingList rows, or BookingRow rows with ?lite=1."""
    context = {'request': request, 'language': _language(request)}
    if _wants_lite(request):
        return BookingRowSerializer(_row_booking_qs(queryset), many=many, context=context).data
    return BookingListSerializer(_optimized_booking_qs(queryset), many=many, context=context).data


def _parse_date(value):
    return datetime.strptime(value, '%Y-%m-%d').date()


def _parse_time(value):
    """'HH:MM' or 'HH:MM:SS' (seconds dropped)."""
    return datetime.strptime(str(value)[:5], '%H:%M').time()


# ============================================================================
# DASHBOARD & ANALYTICS
# ============================================================================

@extend_schema(
    tags=['Booking Admin - Dashboard'],
    parameters=[LITE_PARAM, LANG_PARAM],
    responses={200: BookingDashboardSerializer},
)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, HasBookingManagementFeature])
def dashboard_stats(request):
    """Get dashboard statistics"""
    today = tenant_now(get_booking_settings()).date()

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
    ).aggregate(total=Sum('paid_amount'))['total'] or 0

    week_revenue = week_bookings.filter(
        payment_status__in=['fully_paid', 'deposit_paid']
    ).aggregate(total=Sum('paid_amount'))['total'] or 0

    # Status breakdown
    status_counts = today_bookings.values('status').annotate(count=Count('id'))

    # Popular services this week
    popular_services = week_bookings.values('service__name').annotate(
        count=Count('id')
    ).order_by('-count')[:5]

    return Response({
        'today': {
            'total_bookings': today_bookings.count(),
            'confirmed': today_bookings.filter(status='confirmed').count(),
            'pending': today_bookings.filter(status='pending').count(),
            'completed': today_bookings.filter(status='completed').count(),
            'status_breakdown': list(status_counts),
            'bookings': _serialize_bookings(request, today_bookings.order_by('start_time')),
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


@extend_schema(
    tags=['Booking Admin - Dashboard'],
    parameters=[
        OpenApiParameter('date', OpenApiTypes.DATE, description='Day to show (YYYY-MM-DD); default today'),
        OpenApiParameter('staff_id', OpenApiTypes.INT, description='Only this staff member'),
        LITE_PARAM,
        LANG_PARAM,
    ],
    responses={200: StaffScheduleSerializer, 400: BOOKING_ERROR},
)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, HasBookingManagementFeature])
def staff_schedule(request):
    """Get staff schedule for a specific date"""
    from .utils import get_staff_availability

    date_str = request.query_params.get('date')
    staff_id = request.query_params.get('staff_id')

    if not date_str:
        date_obj = tenant_now(get_booking_settings()).date()
    else:
        try:
            date_obj = _parse_date(date_str)
        except ValueError:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )
    if staff_id and not str(staff_id).isdigit():
        return Response({'error': 'staff_id must be a number'}, status=status.HTTP_400_BAD_REQUEST)

    # Filter by staff if specified
    queryset = Booking.objects.filter(date=date_obj).exclude(status='cancelled')
    if staff_id:
        queryset = queryset.filter(staff_id=staff_id)

    # Get staff availability
    staff_list = BookingStaff.objects.select_related('user').prefetch_related('services').filter(is_active_for_bookings=True)
    if staff_id:
        staff_list = staff_list.filter(id=staff_id)

    schedule = []
    for staff_member in staff_list:
        staff_bookings = queryset.filter(staff=staff_member).order_by('start_time')
        rows = _serialize_bookings(request, staff_bookings)
        schedule.append({
            'staff': BookingStaffSerializer(staff_member).data,
            'availability': get_staff_availability(staff_member, date_obj),
            'bookings': rows,
            'booking_count': len(rows),
        })

    return Response({
        'date': date_str or str(date_obj),
        'schedule': schedule
    })


@extend_schema(
    tags=['Booking Admin - Bookings'],
    summary='Free start times for staff booking',
    description=(
        'Start times when the service can be booked by staff, honouring weekly hours, day '
        'exceptions, breaks, existing bookings and the service duration + buffer. Unlike the '
        "customer slot list, the lead time / advance window don't apply. `service` and `date` "
        'are needed (400 otherwise). Times already passed today are left out unless '
        '`include_past=true`.'
    ),
    parameters=[
        OpenApiParameter('service', OpenApiTypes.INT, description='Service id (needed)'),
        OpenApiParameter('date', OpenApiTypes.DATE, description='YYYY-MM-DD (needed)'),
        OpenApiParameter('staff', OpenApiTypes.INT, description='Only this staff member (must provide the service)'),
        OpenApiParameter('exclude_booking', OpenApiTypes.INT,
                         description="Treat this booking's own time as free (when rescheduling it)"),
        OpenApiParameter('include_past', OpenApiTypes.BOOL, description='Also list times already passed today'),
        OpenApiParameter('step', OpenApiTypes.INT,
                         description='Grid in minutes for duration-based services (5-120; default as on the booking site)'),
    ],
    responses={200: AdminSlotsResponseSerializer, 400: BOOKING_ERROR},
)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, HasBookingManagementFeature])
def admin_slots(request):
    """Free start times for a service on a day, for staff (no lead-time rule)."""
    from .utils import generate_admin_slots, staff_can_perform

    params = request.query_params
    service_id = params.get('service')
    date_str = params.get('date')
    if not service_id or not date_str:
        return Response({'error': 'service and date are required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        date_obj = _parse_date(date_str)
    except ValueError:
        return Response({'error': 'Invalid date format. Use YYYY-MM-DD'}, status=status.HTTP_400_BAD_REQUEST)
    if not str(service_id).isdigit():
        return Response({'error': 'service must be a number'}, status=status.HTTP_400_BAD_REQUEST)
    service = Service.objects.filter(id=service_id).first()
    if service is None:
        return Response({'error': 'Service not found'}, status=status.HTTP_400_BAD_REQUEST)

    staff = None
    staff_id = params.get('staff')
    if staff_id:
        if not str(staff_id).isdigit():
            return Response({'error': 'staff must be a number'}, status=status.HTTP_400_BAD_REQUEST)
        staff = BookingStaff.objects.select_related('user').filter(id=staff_id).first()
        if staff is None:
            return Response({'error': 'Staff not found'}, status=status.HTTP_400_BAD_REQUEST)
        if not staff_can_perform(service, staff):
            return Response({'error': 'This staff member does not provide this service'}, status=status.HTTP_400_BAD_REQUEST)

    exclude_booking = params.get('exclude_booking')
    if exclude_booking and not str(exclude_booking).isdigit():
        return Response({'error': 'exclude_booking must be a number'}, status=status.HTTP_400_BAD_REQUEST)

    step = params.get('step')
    if step:
        try:
            step = int(step)
        except ValueError:
            step = 0
        if not 5 <= step <= 120:
            return Response({'error': 'step must be between 5 and 120 minutes'}, status=status.HTTP_400_BAD_REQUEST)

    include_past = str(params.get('include_past', '')).lower() in ('1', 'true', 'yes')
    not_before = None if include_past else tenant_now(get_booking_settings())

    slots = generate_admin_slots(
        service, date_obj, staff=staff,
        exclude_booking_id=int(exclude_booking) if exclude_booking else None,
        not_before=not_before, step_minutes=step or None,
    )
    return Response({
        'date': date_obj.isoformat(),
        'service_id': service.id,
        'staff_id': staff.id if staff else None,
        'slots': slots,
    })


# ============================================================================
# SERVICE MANAGEMENT
# ============================================================================

_CATALOG_PERMISSIONS = [permissions.IsAuthenticated, HasBookingManagementFeature, IsBookingAdminForWrites]


@extend_schema_view(
    list=extend_schema(tags=['Booking Admin - Services']),
    retrieve=extend_schema(tags=['Booking Admin - Services']),
    create=extend_schema(tags=['Booking Admin - Services'], description='Admins/managers only.'),
    update=extend_schema(tags=['Booking Admin - Services'], description='Admins/managers only.'),
    partial_update=extend_schema(tags=['Booking Admin - Services'], description='Admins/managers only.'),
    destroy=extend_schema(tags=['Booking Admin - Services'], description='Admins/managers only.'),
)
class AdminServiceCategoryViewSet(viewsets.ModelViewSet):
    """Admin service category management"""
    queryset = ServiceCategory.objects.all()
    serializer_class = ServiceCategorySerializer
    permission_classes = _CATALOG_PERMISSIONS
    feature_required = 'booking_management'

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context


@extend_schema_view(
    list=extend_schema(tags=['Booking Admin - Services']),
    retrieve=extend_schema(tags=['Booking Admin - Services']),
    create=extend_schema(
        tags=['Booking Admin - Services'], description='Admins/managers only.',
        request=ServiceCreateSerializer, responses={201: ServiceDetailSerializer},
    ),
    update=extend_schema(
        tags=['Booking Admin - Services'], description='Admins/managers only.',
        request=ServiceCreateSerializer, responses={200: ServiceDetailSerializer},
    ),
    partial_update=extend_schema(
        tags=['Booking Admin - Services'], description='Admins/managers only.',
        request=ServiceCreateSerializer, responses={200: ServiceDetailSerializer},
    ),
    destroy=extend_schema(tags=['Booking Admin - Services'], description='Admins/managers only.'),
)
class AdminServiceViewSet(viewsets.ModelViewSet):
    """Admin service management"""
    queryset = Service.objects.select_related('category').prefetch_related(
        Prefetch('staff_members', queryset=BookingStaff.objects.select_related('user').prefetch_related('services')),
    ).all()
    permission_classes = _CATALOG_PERMISSIONS
    feature_required = 'booking_management'
    filterset_fields = ['category', 'status', 'booking_type']
    search_fields = ['name']

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return ServiceCreateSerializer
        if self.action == 'retrieve':
            return ServiceDetailSerializer
        return ServiceListSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    def _detail(self, service):
        service = self.get_queryset().get(pk=service.pk)
        return ServiceDetailSerializer(service, context=self.get_serializer_context()).data

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        service = serializer.save()
        return Response(self._detail(service), status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        service = serializer.save()
        return Response(self._detail(service))

    @extend_schema(
        tags=['Booking Admin - Services'], description='Admins/managers only.',
        request=None, responses={200: ServiceDetailSerializer},
    )
    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """Activate service"""
        service = self.get_object()
        service.status = 'active'
        service.save(update_fields=['status'])
        return Response(self._detail(service))

    @extend_schema(
        tags=['Booking Admin - Services'], description='Admins/managers only.',
        request=None, responses={200: ServiceDetailSerializer},
    )
    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        """Deactivate service"""
        service = self.get_object()
        service.status = 'inactive'
        service.save(update_fields=['status'])
        return Response(self._detail(service))


# ============================================================================
# STAFF MANAGEMENT
# ============================================================================

@extend_schema_view(
    list=extend_schema(tags=['Booking Admin - Staff']),
    retrieve=extend_schema(tags=['Booking Admin - Staff']),
    create=extend_schema(
        tags=['Booking Admin - Staff'], description='Admins/managers only.',
        request=BookingStaffCreateSerializer, responses={201: BookingStaffSerializer},
    ),
    update=extend_schema(
        tags=['Booking Admin - Staff'], description='Admins/managers only.',
        request=BookingStaffCreateSerializer, responses={200: BookingStaffSerializer},
    ),
    partial_update=extend_schema(
        tags=['Booking Admin - Staff'], description='Admins/managers only.',
        request=BookingStaffCreateSerializer, responses={200: BookingStaffSerializer},
    ),
    destroy=extend_schema(tags=['Booking Admin - Staff'], description='Admins/managers only.'),
)
class AdminBookingStaffViewSet(viewsets.ModelViewSet):
    """Admin staff management"""
    queryset = BookingStaff.objects.select_related('user').prefetch_related('services').all()
    serializer_class = BookingStaffSerializer
    permission_classes = _CATALOG_PERMISSIONS
    feature_required = 'booking_management'
    filterset_fields = ['is_active_for_bookings']
    search_fields = ['user__first_name', 'user__last_name']

    def get_serializer_class(self):
        """Use different serializer for create/update operations"""
        if self.action in ['create', 'update', 'partial_update']:
            return BookingStaffCreateSerializer
        return BookingStaffSerializer

    def _detail(self, staff):
        staff = self.get_queryset().get(pk=staff.pk)
        return BookingStaffSerializer(staff, context=self.get_serializer_context()).data

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        staff = serializer.save()
        return Response(self._detail(staff), status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        staff = serializer.save()
        return Response(self._detail(staff))

    @extend_schema(
        tags=['Booking Admin - Staff'],
        summary="The signed-in user's booking staff profile",
        description='404 when the current user is not booking staff. Use its id for "my" filters (staff, staff_id).',
        responses={200: BookingStaffSerializer, 404: OpenApiResponse(description='Not booking staff')},
    )
    @action(detail=False, methods=['get'])
    def me(self, request):
        staff = self.get_queryset().filter(user=request.user).first()
        if staff is None:
            return Response({'detail': 'You are not booking staff.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(BookingStaffSerializer(staff, context=self.get_serializer_context()).data)

    @extend_schema(
        tags=['Booking Admin - Staff'],
        parameters=[OpenApiParameter('search', OpenApiTypes.STR, description='Name or email')],
        responses={200: AvailableUserSerializer(many=True)},
        operation_id='bookings_admin_staff_available_users_retrieve',
    )
    @action(detail=False, methods=['get'], pagination_class=None, filter_backends=[])
    def available_users(self, request):
        """Get users who are not already booking staff (at most 50)"""
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

    @extend_schema(
        tags=['Booking Admin - Staff'], responses={200: StaffAvailabilitySerializer(many=True)},
        operation_id='bookings_admin_staff_availability_retrieve',
    )
    @action(detail=True, methods=['get'], pagination_class=None, filter_backends=[])
    def availability(self, request, pk=None):
        """Get staff availability schedule"""
        staff = self.get_object()
        availability = StaffAvailability.objects.filter(staff=staff).order_by('day_of_week')
        return Response(StaffAvailabilitySerializer(availability, many=True).data)

    @extend_schema(
        tags=['Booking Admin - Staff'], responses={200: StaffExceptionSerializer(many=True)},
        operation_id='bookings_admin_staff_exceptions_retrieve',
    )
    @action(detail=True, methods=['get'], pagination_class=None, filter_backends=[])
    def exceptions(self, request, pk=None):
        """Get staff exceptions (vacations, special hours), from today on"""
        staff = self.get_object()
        # Get future exceptions
        exceptions = StaffException.objects.filter(
            staff=staff,
            date__gte=tenant_now(get_booking_settings()).date()
        ).order_by('date')
        return Response(StaffExceptionSerializer(exceptions, many=True).data)

    @extend_schema(
        tags=['Booking Admin - Staff'],
        parameters=[
            OpenApiParameter('date', OpenApiTypes.DATE, description='Only this day (YYYY-MM-DD); default: upcoming'),
            LITE_PARAM,
            LANG_PARAM,
        ],
        responses={200: BookingListSerializer(many=True), 400: BOOKING_ERROR},
        operation_id='bookings_admin_staff_bookings_retrieve',
    )
    @action(detail=True, methods=['get'], pagination_class=None, filter_backends=[])
    def bookings(self, request, pk=None):
        """Get staff bookings (not cancelled)"""
        staff = self.get_object()
        date_str = request.query_params.get('date')

        queryset = Booking.objects.filter(staff=staff).exclude(status='cancelled')

        if date_str:
            try:
                queryset = queryset.filter(date=_parse_date(date_str))
            except ValueError:
                return Response({'error': 'Invalid date format. Use YYYY-MM-DD'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            # Default to upcoming bookings
            queryset = queryset.filter(date__gte=tenant_now(get_booking_settings()).date())

        queryset = queryset.order_by('date', 'start_time')
        return Response(_serialize_bookings(request, queryset))

    @extend_schema(
        tags=['Booking Admin - Staff'], description='Admins/managers only.',
        request=None, responses={200: BookingStaffSerializer},
    )
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
    list=extend_schema(
        tags=['Booking Admin - Staff'],
        parameters=[OpenApiParameter('show_past', OpenApiTypes.BOOL,
                                     description='Include days before today (default false: from today on)')],
    ),
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
        show_past = self.request.query_params.get('show_past', 'false').lower() in ('1', 'true', 'yes')

        if not show_past:
            queryset = queryset.filter(date__gte=tenant_now(get_booking_settings()).date())

        return queryset.order_by('date')


# ============================================================================
# BOOKING MANAGEMENT
# ============================================================================

BOOKING_LIST_PARAMS = [
    OpenApiParameter('date_from', OpenApiTypes.DATE, description='On or after this day (YYYY-MM-DD)'),
    OpenApiParameter('date_to', OpenApiTypes.DATE, description='On or before this day (YYYY-MM-DD)'),
    OpenApiParameter('mine', OpenApiTypes.BOOL, description="Only bookings assigned to the signed-in user"),
    LANG_PARAM,
]


@extend_schema_view(
    list=extend_schema(
        tags=['Booking Admin - Bookings'],
        description='`lite=1` switches rows to the slim `BookingRow` shape (typed variant: `bookings/rows/`).',
        parameters=BOOKING_LIST_PARAMS + [LITE_PARAM],
    ),
    retrieve=extend_schema(tags=['Booking Admin - Bookings']),
    create=extend_schema(
        tags=['Booking Admin - Bookings'],
        request=AdminBookingCreateSerializer, responses={201: BookingDetailSerializer},
    ),
    update=extend_schema(
        tags=['Booking Admin - Bookings'],
        description='Notes and price only; everything else changes through the actions.',
        request=AdminBookingUpdateSerializer, responses={200: BookingDetailSerializer},
    ),
    partial_update=extend_schema(
        tags=['Booking Admin - Bookings'],
        description='Notes and price only (staff_notes, client_notes, total_amount).',
        request=AdminBookingUpdateSerializer, responses={200: BookingDetailSerializer},
    ),
    destroy=extend_schema(tags=['Booking Admin - Bookings'], description='Hard delete. Admins/managers only.'),
)
class AdminBookingViewSet(viewsets.ModelViewSet):
    """Admin booking management - view and manage all bookings"""
    serializer_class = BookingListSerializer
    permission_classes = [permissions.IsAuthenticated, HasBookingManagementFeature, IsBookingAdminForDestroy]
    feature_required = 'booking_management'
    filterset_fields = ['status', 'payment_status', 'service', 'staff', 'client']
    search_fields = ['booking_number', 'client__email', 'client__first_name', 'client__last_name']

    def _lite(self):
        return self.action in ('list', 'rows') and (self.action == 'rows' or _wants_lite(self.request))

    def get_queryset(self):
        """Get all bookings with filters"""
        base = _row_booking_qs() if self._lite() else _optimized_booking_qs()
        queryset = base.order_by('-date', '-start_time')

        # Date range filter
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')

        if date_from:
            try:
                queryset = queryset.filter(date__gte=_parse_date(date_from))
            except ValueError:
                pass

        if date_to:
            try:
                queryset = queryset.filter(date__lte=_parse_date(date_to))
            except ValueError:
                pass

        if str(self.request.query_params.get('mine', '')).lower() in ('1', 'true', 'yes'):
            queryset = queryset.filter(staff__user=self.request.user)

        return queryset

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return BookingDetailSerializer
        if self.action == 'create':
            return AdminBookingCreateSerializer
        if self.action in ('update', 'partial_update'):
            return AdminBookingUpdateSerializer
        if self._lite():
            return BookingRowSerializer
        return BookingListSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = _language(self.request)
        return context

    def _detail(self, booking):
        booking = _optimized_booking_qs().get(pk=booking.pk)
        return BookingDetailSerializer(booking, context=self.get_serializer_context()).data

    def create(self, request, *args, **kwargs):
        """Book for a customer by hand (phone call, walk-in, calendar click)."""
        from .utils import is_slot_booked

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            # Same lock as customer bookings so two front desks (or a customer
            # online) can't take the same minute of the same specialist.
            BookingStaff.objects.select_for_update().filter(pk=data['staff'].pk).exists()
            if is_slot_booked(data['staff'], data['date'], data['start_time'], data['service'].total_duration_minutes):
                return Response({'start_time': 'This time slot was just booked'}, status=status.HTTP_400_BAD_REQUEST)
            booking = serializer.save()
        if data.get('notify_client') and booking.status == 'confirmed':
            booking._notify_client('confirmed')
        # A colleague booked one of this staff member's clients
        notify_assigned_staff(booking, 'created', actor=request.user)
        return Response(self._detail(booking), status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        booking = self.get_object()
        serializer = self.get_serializer(booking, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(self._detail(booking))

    @extend_schema(
        tags=['Booking Admin - Bookings'],
        description='Same filters as the list, always in the slim `BookingRow` shape (paginated).',
        parameters=BOOKING_LIST_PARAMS,
        responses={200: BookingRowSerializer(many=True)},
    )
    @action(detail=False, methods=['get'])
    def rows(self, request):
        """Slim booking rows for phone lists."""
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(BookingRowSerializer(page, many=True, context=self.get_serializer_context()).data)
        return Response(BookingRowSerializer(queryset, many=True, context=self.get_serializer_context()).data)

    @extend_schema(
        tags=['Booking Admin - Bookings'],
        description='pending → confirmed. An unpaid online-card booking needs force=true.',
        request=BookingConfirmRequestSerializer, responses={200: BookingDetailSerializer, 400: BOOKING_ERROR},
    )
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
        # Only bookings the customer chose to pay online must be paid first;
        # pay-at-venue bookings (and ones staff created) are confirmed as-is.
        force = str(request.data.get('force', False)).lower() in ('1', 'true', 'yes')
        awaiting_online_payment = booking.payment_method == 'card'
        if not force and awaiting_online_payment and booking.payment_status not in ['deposit_paid', 'fully_paid']:
            return Response(
                {'error': 'Cannot confirm booking without payment. Use force=true to override.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        booking._notification_actor = request.user
        booking.confirm()
        return Response(self._detail(booking))

    @extend_schema(
        tags=['Booking Admin - Bookings'],
        description='The client has arrived: pending/confirmed → in_progress.',
        request=None, responses={200: BookingDetailSerializer, 400: BOOKING_ERROR},
    )
    @action(detail=True, methods=['post'])
    def check_in(self, request, pk=None):
        booking = self.get_object()
        if booking.status not in ['pending', 'confirmed']:
            return Response(
                {'error': f'Cannot check in booking with status: {booking.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        booking.check_in()
        return Response(self._detail(booking))

    @extend_schema(
        tags=['Booking Admin - Bookings'],
        description='confirmed/in_progress → completed.',
        request=None, responses={200: BookingDetailSerializer, 400: BOOKING_ERROR},
    )
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
        return Response(self._detail(booking))

    @extend_schema(
        tags=['Booking Admin - Bookings'],
        description="The client didn't come: pending/confirmed → no_show. Frees the staff member's time.",
        request=None, responses={200: BookingDetailSerializer, 400: BOOKING_ERROR},
    )
    @action(detail=True, methods=['post'])
    def no_show(self, request, pk=None):
        booking = self.get_object()
        if booking.status not in ['pending', 'confirmed']:
            return Response(
                {'error': f'Cannot mark booking with status {booking.status} as no-show'},
                status=status.HTTP_400_BAD_REQUEST
            )
        booking.mark_no_show()
        return Response(self._detail(booking))

    @extend_schema(
        tags=['Booking Admin - Bookings'],
        description=(
            'Cancel (any status except completed/cancelled/no_show). Refunds what was paid '
            'online unless refund=false.'
        ),
        request=BookingCancelRequestSerializer, responses={200: BookingDetailSerializer, 400: BOOKING_ERROR},
    )
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel booking (admin can cancel anytime)"""
        from .utils import FINAL_BOOKING_STATUSES

        booking = self.get_object()

        if booking.status in FINAL_BOOKING_STATUSES:
            return Response(
                {'error': f'Cannot cancel booking with status: {booking.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        cancellation_reason = (request.data.get('reason') or '').strip() or 'Cancelled by admin'
        with transaction.atomic():
            # Lock the row so two simultaneous cancels can't both refund.
            booking = Booking.objects.select_for_update().get(pk=booking.pk)
            if booking.status in FINAL_BOOKING_STATUSES:
                return Response(
                    {'error': f'Cannot cancel booking with status: {booking.status}'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            booking._notification_actor = request.user
            booking.cancel(cancelled_by='admin', reason=cancellation_reason)

        # Initiate refund if applicable
        if booking.paid_amount > 0:
            refund_requested = str(request.data.get('refund', True)).lower() not in ('0', 'false', 'no')
            if refund_requested:
                try:
                    payment_service = get_booking_payment_service()
                    payment_service.initiate_refund(booking)
                except Exception as e:
                    logger.error(f"Failed to initiate refund for {booking.booking_number}: {str(e)}")

        return Response(self._detail(booking))

    @extend_schema(
        tags=['Booking Admin - Bookings'],
        description=(
            'Hand a pending/confirmed booking to another staff member who provides the service and '
            'is working and free then. Both staff members are notified.'
        ),
        request=BookingAssignStaffRequestSerializer, responses={200: BookingDetailSerializer, 400: BOOKING_ERROR},
    )
    @action(detail=True, methods=['post'])
    def assign_staff(self, request, pk=None):
        """Assign or reassign staff to booking"""
        from .utils import check_staff_slot

        booking = self.get_object()
        staff_id = request.data.get('staff_id')

        if not staff_id:
            return Response(
                {'error': 'staff_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if booking.status not in ['pending', 'confirmed']:
            return Response(
                {'error': f'Cannot reassign booking with status: {booking.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            staff = BookingStaff.objects.select_related('user').get(id=staff_id)
        except (BookingStaff.DoesNotExist, ValueError, TypeError):
            return Response(
                {'error': 'Staff not found'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if staff.pk == booking.staff_id:
            return Response(self._detail(booking))

        # Check if staff is available for this service
        if not staff.services.filter(id=booking.service.id).exists():
            return Response(
                {'error': 'Staff is not assigned to this service'},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            BookingStaff.objects.select_for_update().filter(pk=staff.pk).exists()
            ok, error_message = check_staff_slot(
                booking.service, staff, booking.date, booking.start_time, exclude_booking_id=booking.pk,
            )
            if not ok:
                return Response(
                    {'error': f'Staff is not available at this time: {error_message}'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            previous = booking.staff
            booking.staff = staff
            booking.staff_reminder_sent = False
            booking.save(update_fields=['staff', 'staff_reminder_sent', 'updated_at'])

        notify_assigned_staff(booking, 'assigned', actor=request.user)
        if previous is not None:
            notify_assigned_staff(
                booking, 'unassigned', actor=request.user, staff=previous,
                other=staff.user.get_full_name() or staff.user.email,
            )
        return Response(self._detail(booking))

    @extend_schema(
        tags=['Booking Admin - Bookings'],
        description=(
            'Move a pending/confirmed booking. The new time must be in the future and free for the '
            'assigned staff member (no lead-time rule). The client is emailed, the staff member notified.'
        ),
        request=BookingRescheduleRequestSerializer, responses={200: BookingDetailSerializer, 400: BOOKING_ERROR},
    )
    @action(detail=True, methods=['post'])
    def reschedule(self, request, pk=None):
        """Reschedule booking to new date/time"""
        from .utils import validate_booking_availability

        booking = self.get_object()

        new_date_str = request.data.get('date')
        new_time_str = request.data.get('start_time')

        if not new_date_str or not new_time_str:
            return Response(
                {'error': 'date and start_time are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if booking.status not in ['pending', 'confirmed']:
            return Response(
                {'error': f'Cannot reschedule booking with status: {booking.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            new_date = _parse_date(str(new_date_str))
            new_time = _parse_time(new_time_str)
        except ValueError:
            return Response(
                {'error': 'Invalid date or time format'},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            if booking.staff_id:
                BookingStaff.objects.select_for_update().filter(pk=booking.staff_id).exists()
            # Validate availability
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
            booking.reminder_sent = False
            booking.second_reminder_sent = False
            booking.staff_reminder_sent = False
            booking.save(update_fields=[
                'date', 'start_time', 'end_time', 'reminder_sent', 'second_reminder_sent',
                'staff_reminder_sent', 'updated_at',
            ])
        booking._notify_client('rescheduled')
        notify_assigned_staff(booking, 'rescheduled', actor=request.user)

        return Response(self._detail(booking))

    @extend_schema(
        tags=['Booking Admin - Bookings'],
        description=(
            'Record a payment taken by staff (cash at the desk, card terminal, transfer). Adds to '
            'paid_amount (at most the remaining amount) and sets payment_status to fully_paid, or '
            'deposit_paid while something is still owed. Logged in manual_payments. Not for cancelled bookings.'
        ),
        request=BookingRecordPaymentRequestSerializer, responses={200: BookingDetailSerializer, 400: BOOKING_ERROR},
    )
    @action(detail=True, methods=['post'])
    def record_payment(self, request, pk=None):
        serializer = BookingRecordPaymentRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        booking = self.get_object()

        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(pk=booking.pk)
            if booking.status == 'cancelled':
                return Response({'error': 'Cannot take a payment for a cancelled booking'}, status=status.HTTP_400_BAD_REQUEST)
            amount = Decimal(data['amount'])
            remaining = (booking.total_amount or Decimal('0')) - (booking.paid_amount or Decimal('0'))
            if amount > remaining:
                return Response(
                    {'error': f'The amount is more than what is owed ({remaining})'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            booking.paid_amount = (booking.paid_amount or Decimal('0')) + amount
            booking.payment_status = 'fully_paid' if booking.paid_amount >= booking.total_amount else 'deposit_paid'
            payments = list(booking.manual_payments or [])
            payments.append({
                'amount': str(amount),
                'method': data['method'],
                'note': data.get('note', ''),
                'recorded_by': request.user.pk,
                'recorded_by_name': request.user.get_full_name() or request.user.email,
                'recorded_at': timezone.now().isoformat(),
            })
            booking.manual_payments = payments
            booking.save(update_fields=['paid_amount', 'payment_status', 'manual_payments', 'updated_at'])

        return Response(self._detail(booking))

    @extend_schema(
        tags=['Booking Admin - Bookings'],
        description='Ask BOG for the online payment state (card bookings).',
        responses={200: OpenApiTypes.OBJECT, 500: BOOKING_ERROR},
    )
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
    create=extend_schema(
        tags=['Booking Admin - Bookings'],
        request=AdminRecurringBookingCreateSerializer, responses={201: RecurringBookingSerializer},
    ),
    update=extend_schema(tags=['Booking Admin - Bookings']),
    partial_update=extend_schema(tags=['Booking Admin - Bookings']),
    destroy=extend_schema(tags=['Booking Admin - Bookings'], description='Hard delete. Admins/managers only.'),
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
    permission_classes = [permissions.IsAuthenticated, HasBookingManagementFeature, IsBookingAdminForDestroy]
    feature_required = 'booking_management'
    filterset_fields = ['status', 'frequency', 'client', 'service']

    def get_serializer_class(self):
        if self.action == 'create':
            return AdminRecurringBookingCreateSerializer
        return RecurringBookingSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recurring = serializer.save()
        recurring = self.get_queryset().get(pk=recurring.pk)
        return Response(RecurringBookingSerializer(recurring).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=['Booking Admin - Bookings'], request=None, responses={200: RecurringBookingSerializer})
    @action(detail=True, methods=['post'])
    def pause(self, request, pk=None):
        """Pause recurring booking"""
        recurring = self.get_object()
        recurring.status = 'paused'
        recurring.save(update_fields=['status'])
        return Response(RecurringBookingSerializer(recurring).data)

    @extend_schema(tags=['Booking Admin - Bookings'], request=None, responses={200: RecurringBookingSerializer})
    @action(detail=True, methods=['post'])
    def resume(self, request, pk=None):
        """Resume recurring booking"""
        recurring = self.get_object()
        recurring.status = 'active'
        recurring.save(update_fields=['status'])
        return Response(RecurringBookingSerializer(recurring).data)

    @extend_schema(tags=['Booking Admin - Bookings'], request=None, responses={200: RecurringBookingSerializer})
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel recurring booking"""
        recurring = self.get_object()
        recurring.status = 'cancelled'
        recurring.end_date = tenant_now(get_booking_settings()).date()
        recurring.save(update_fields=['status', 'end_date'])
        return Response(RecurringBookingSerializer(recurring).data)


# ============================================================================
# CLIENT MANAGEMENT
# ============================================================================

@extend_schema_view(
    list=extend_schema(tags=['Booking Admin - Clients']),
    retrieve=extend_schema(tags=['Booking Admin - Clients']),
)
class AdminBookingClientViewSet(viewsets.ReadOnlyModelViewSet):
    """Admin client viewing - shows clients with booking enabled from unified Client model"""
    queryset = Client.objects.filter(is_booking_enabled=True)
    serializer_class = BookingClientSerializer
    permission_classes = [permissions.IsAuthenticated, HasBookingManagementFeature]
    feature_required = 'booking_management'
    search_fields = ['email', 'first_name', 'last_name', 'phone', 'name']
    filterset_fields = ['is_verified']

    @extend_schema(
        tags=['Booking Admin - Clients'],
        parameters=[LITE_PARAM, LANG_PARAM],
        responses={200: BookingListSerializer(many=True)},
        operation_id='bookings_admin_clients_bookings_retrieve',
    )
    @action(detail=True, methods=['get'], pagination_class=None, filter_backends=[])
    def bookings(self, request, pk=None):
        """Get client's booking history"""
        client = self.get_object()
        bookings = Booking.objects.filter(client=client).order_by('-date', '-start_time')
        return Response(_serialize_bookings(request, bookings))

    @extend_schema(tags=['Booking Admin - Clients'], responses={200: BookingClientStatsSerializer})
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

@extend_schema(
    tags=['Booking Admin - Settings'],
    description='GET: everyone with the feature. PUT/PATCH: admins/managers only.',
    request=BookingSettingsSerializer,
    responses={200: BookingSettingsSerializer},
)
@api_view(['GET', 'PUT', 'PATCH'])
@permission_classes([permissions.IsAuthenticated, HasBookingManagementFeature, IsBookingAdminForWrites])
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


TEST_SMS_PER_HOUR = 10


@extend_schema(
    tags=['Booking Admin - Settings'],
    description='Admins/managers only.',
    request=BookingTestSmsSerializer,
    responses={200: BookingTestSmsResultSerializer, 400: OpenApiTypes.OBJECT, 429: BOOKING_ERROR},
)
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, HasBookingManagementFeature, IsBookingAdmin])
def booking_settings_test_sms(request):
    """Send one of the SMS texts, filled with sample data, to a phone number.

    Goes through the same account as real notices (own key, else the shared
    account and its monthly limit), so it shows what a customer would get.
    """
    from django.core.cache import cache
    from django.db import connection
    from .sms import render_template, sample_values, send_text, template_for
    from .utils import get_or_create_booking_settings

    serializer = BookingTestSmsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    counter_key = f'booking_test_sms:{connection.schema_name}'
    sent = cache.get(counter_key) or 0
    if sent >= TEST_SMS_PER_HOUR:
        return Response({'error': 'Too many test messages. Try again in an hour.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)

    settings = get_or_create_booking_settings()
    # Preview the text being edited, even before it is saved
    template = (data.get('text') or '').strip() or template_for(settings, data['kind'], data['language'])
    text = render_template(template, sample_values(data['language']))
    log = send_text(settings, data['phone'], text, f"test_{data['kind']}"[:20], data['language'])
    if log.status != 'sent':
        return Response({'error': log.error or 'The SMS could not be sent', 'text': text}, status=status.HTTP_400_BAD_REQUEST)

    cache.set(counter_key, sent + 1, 60 * 60)
    return Response({'status': 'sent', 'text': text, 'segments': log.segments, 'account': log.account})
