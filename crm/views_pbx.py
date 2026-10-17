"""ViewSets for the PBX management panel (Trunks, Queues, Inbound routes).

All endpoints are gated by the ``ip_calling`` subscription feature. Reads
are open to every user with the feature (the phone app shows a read-only
PBX glance); writes need a PBX admin (staff, superuser or
``manage_settings``, see ``crm.permissions``). PBX servers are PBX-admin
only for reads too, because the response carries the enrollment token.
"""

from django.db.models import Count, Q
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import filters, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .permissions import HasIpCallingFeature, IsPbxAdmin, IsPbxAdminOrReadOnly
from .models import InboundRoute, Queue, QueueMember, Trunk
from .serializers import (
    InboundRouteSerializer,
    PbxServerSerializer,
    QueueListSerializer,
    QueueMemberSerializer,
    QueueSerializer,
    TrunkListSerializer,
    TrunkSerializer,
)


_READ_OR_ADMIN = [permissions.IsAuthenticated, HasIpCallingFeature, IsPbxAdminOrReadOnly]


@extend_schema(tags=['PBX'])
class TrunkViewSet(viewsets.ModelViewSet):
    """CRUD for tenant-owned SIP trunks.

    Trunks represent the connection to a provider (Magti, Silknet, …) and
    own the set of inbound DIDs that route through them. Gated by the
    ``ip_calling`` subscription feature.
    """

    permission_classes = _READ_OR_ADMIN
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'is_default', 'register', 'provider']
    search_fields = ['name', 'provider', 'sip_server', 'username', 'realm']
    ordering_fields = ['name', 'provider', 'created_at', 'updated_at']
    ordering = ['-is_default', 'name']

    def get_queryset(self):
        # Tenant isolation is handled by the tenant-schemas middleware.
        return Trunk.objects.all()

    def get_serializer_class(self):
        if self.action == 'list':
            return TrunkListSerializer
        return TrunkSerializer


@extend_schema(tags=['PBX'])
class QueueViewSet(viewsets.ModelViewSet):
    """CRUD for call queues.

    A queue is backed by a ``TenantGroup`` — members with an active
    ``UserPhoneAssignment`` are materialised into ``QueueMember`` rows
    by the sync layer. Gated by the ``ip_calling`` subscription feature.
    """

    permission_classes = _READ_OR_ADMIN
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'is_default', 'strategy', 'group']
    search_fields = ['name', 'slug', 'group__name']
    ordering_fields = ['name', 'slug', 'created_at', 'updated_at']
    ordering = ['-is_default', 'name']

    def get_queryset(self):
        # One COUNT for the whole page instead of one per queue.
        return Queue.objects.select_related('group').annotate(
            active_member_count=Count('members', filter=Q(members__is_active=True)),
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return QueueListSerializer
        return QueueSerializer


@extend_schema(tags=['PBX'])
class QueueMemberViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only view over ``QueueMember`` rows.

    Rows are materialised by the sync layer — there is no create/update/delete.
    Filter by ``?queue=N`` to list an individual queue's agents.
    """

    serializer_class = QueueMemberSerializer
    permission_classes = [permissions.IsAuthenticated, HasIpCallingFeature]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['queue', 'is_active', 'paused']
    search_fields = [
        'queue__name', 'queue__slug',
        'user_phone_assignment__extension',
        'user_phone_assignment__phone_number',
        'user_phone_assignment__user__email',
    ]
    ordering_fields = ['queue', 'penalty', 'synced_at']
    ordering = ['queue', 'penalty']

    def get_queryset(self):
        return QueueMember.objects.select_related(
            'queue', 'user_phone_assignment', 'user_phone_assignment__user',
        ).all()


@extend_schema(tags=['PBX'])
class InboundRouteViewSet(viewsets.ModelViewSet):
    """CRUD for DID-to-destination routing rules."""

    serializer_class = InboundRouteSerializer
    permission_classes = _READ_OR_ADMIN
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['destination_type', 'is_active', 'trunk', 'destination_queue']
    search_fields = ['did', 'ivr_custom_context', 'trunk__name', 'destination_queue__slug']
    ordering_fields = ['priority', 'did', 'created_at', 'updated_at']
    ordering = ['priority', 'did']

    def get_queryset(self):
        return InboundRoute.objects.select_related(
            'trunk', 'destination_queue', 'destination_extension', 'working_hours_override',
        ).all()


@extend_schema(tags=['PBX'])
class PbxServerViewSet(viewsets.ModelViewSet):
    """CRUD for the tenant's BYO Asterisk server registration.

    On create, triggers provisioning (new realtime DB + role + migrate).
    Passwords are write-only in the response; admins can only read a
    masked placeholder once the server is registered. PBX admins only
    (reads included): the response carries the install enrollment token.
    """

    permission_classes = [permissions.IsAuthenticated, HasIpCallingFeature, IsPbxAdmin]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'fqdn', 'realtime_db_name']
    ordering_fields = ['name', 'fqdn', 'status', 'created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        from .models import PbxServer
        return PbxServer.objects.all()

    def get_serializer_class(self):
        from .serializers import PbxServerSerializer
        return PbxServerSerializer

    def perform_create(self, serializer):
        """Save then provision the per-tenant realtime DB."""
        from .pbx_provisioning import provision_and_bootstrap
        pbx = serializer.save()
        try:
            provision_and_bootstrap(pbx)
        except Exception as exc:  # noqa: BLE001
            pbx.status = pbx.STATUS_ERROR
            pbx.notes = f"Provisioning failed: {exc}"[:1000]
            pbx.save(update_fields=['status', 'notes', 'updated_at'])
            raise

    @extend_schema(responses={200: PbxServerSerializer})
    @action(detail=True, methods=['post'], url_path='regenerate-token')
    def regenerate_token(self, request, pk=None):
        pbx = self.get_object()
        pbx.regenerate_enrollment_token()
        return Response(self.get_serializer(pbx).data)
