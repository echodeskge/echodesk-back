"""Personal ticket timer endpoints (A14, E32).

- ``tickets/{id}/timer/`` (GET), ``.../start/``, ``.../stop/`` (POST):
  ``TicketTimerViewSet``; the ticket must be visible to the caller (404).
- ``time-entries/`` (GET, paginated, visibility-scoped), ``time-entries/current/``
  (GET) and ``time-entries/current/stop/`` (POST, no visibility check):
  ``TimeEntryViewSet``.
"""
import django_filters
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view,
)
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import timer
from .models import TicketTimeEntry
from .serializers_timer import (
    CurrentTimerSerializer, TicketTimeEntrySerializer, TicketTimerStateSerializer,
    TimerConflictSerializer, TimerStartSerializer,
)

_ID_PARAM = OpenApiParameter('id', OpenApiTypes.STR, OpenApiParameter.PATH)
_CONFLICT = OpenApiResponse(TimerConflictSerializer, description='Timer conflict')


def _conflict(exc):
    return Response(exc.payload, status=status.HTTP_409_CONFLICT)


@extend_schema(tags=['Time Tracking'])
class TicketTimerViewSet(viewsets.ViewSet):
    """The caller's personal timer on one ticket."""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = TicketTimeEntrySerializer  # schema hint only

    def _ticket(self, request, id):
        qs = timer.visible_tickets(request.user).select_related('column')
        return get_object_or_404(qs, pk=id)

    @extend_schema(
        operation_id='tickets_timer_retrieve',
        parameters=[_ID_PARAM],
        responses={200: TicketTimerStateSerializer},
        summary='Timer state and logged-time totals for a ticket',
    )
    def retrieve(self, request, id=None):
        ticket = self._ticket(request, id)
        data = timer.totals(ticket, request.user)
        return Response(TicketTimerStateSerializer(data).data)

    @extend_schema(
        operation_id='tickets_timer_start',
        parameters=[_ID_PARAM],
        request=TimerStartSerializer,
        responses={
            200: OpenApiResponse(TicketTimeEntrySerializer, description='Already running on this ticket'),
            201: TicketTimeEntrySerializer,
            409: _CONFLICT,
        },
        summary='Start (or switch to) the personal timer on a ticket',
    )
    @action(detail=True, methods=['post'])
    def start(self, request, id=None):
        ticket = self._ticket(request, id)
        body = TimerStartSerializer(data=request.data or {})
        body.is_valid(raise_exception=True)
        try:
            entry, created = timer.start(request.user, ticket, switch=body.validated_data['switch'])
        except timer.TimerConflict as exc:
            return _conflict(exc)
        return Response(
            TicketTimeEntrySerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        operation_id='tickets_timer_stop',
        parameters=[_ID_PARAM],
        request=None,
        responses={200: TicketTimeEntrySerializer, 409: _CONFLICT},
        summary="Stop the caller's timer on this ticket",
    )
    @action(detail=True, methods=['post'])
    def stop(self, request, id=None):
        ticket = self._ticket(request, id)
        try:
            entry = timer.stop_for_ticket(request.user, ticket)
        except timer.TimerConflict as exc:
            return _conflict(exc)
        return Response(TicketTimeEntrySerializer(entry).data)


class TimeEntryFilter(django_filters.FilterSet):
    ticket = django_filters.NumberFilter(field_name='ticket_id')
    user = django_filters.NumberFilter(field_name='user_id')
    started_after = django_filters.IsoDateTimeFilter(field_name='started_at', lookup_expr='gte')
    started_before = django_filters.IsoDateTimeFilter(field_name='started_at', lookup_expr='lt')

    class Meta:
        model = TicketTimeEntry
        fields = ['ticket', 'user', 'started_after', 'started_before']


@extend_schema_view(
    list=extend_schema(summary='Personal timer entries (visibility-scoped)'),
    retrieve=extend_schema(parameters=[_ID_PARAM]),
)
@extend_schema(tags=['Time Tracking'])
class TimeEntryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TicketTimeEntry.objects.none()  # schema hint; real scope in get_queryset
    serializer_class = TicketTimeEntrySerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = TimeEntryFilter
    lookup_value_regex = r'\d+'

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return TicketTimeEntry.objects.none()
        return (
            timer.visible_entries(self.request.user)
            .select_related('user')
            .order_by('-started_at', '-id')
        )

    @extend_schema(
        operation_id='time_entries_current_retrieve',
        responses={200: CurrentTimerSerializer},
        summary="The caller's running timer on any ticket",
    )
    @action(detail=False, methods=['get'], url_path='current', filter_backends=[], pagination_class=None)
    def current(self, request):
        entry = timer.current(request.user)
        return Response(CurrentTimerSerializer({'running': entry, 'server_now': timezone.now()}).data)

    @extend_schema(
        operation_id='time_entries_current_stop',
        request=None,
        responses={200: TicketTimeEntrySerializer, 409: _CONFLICT},
        summary="Stop the caller's running timer, whatever ticket it is on",
    )
    @action(detail=False, methods=['post'], url_path='current/stop', filter_backends=[], pagination_class=None)
    def stop_current(self, request):
        try:
            entry = timer.stop_current(request.user)
        except timer.TimerConflict as exc:
            return _conflict(exc)
        return Response(TicketTimeEntrySerializer(entry).data)
