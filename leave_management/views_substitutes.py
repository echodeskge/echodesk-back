from django.db import transaction
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import SubstituteAssignment, LeaveRequest
from .permissions import HasLeaveManagementFeature, LeaveFeatureEnabled
from .serializers import (
    SubstituteAssignmentSerializer, SubstituteReplaceSerializer,
    CoveringRequestSerializer, LeaveRequestDetailSerializer,
    SubstituteRespondSerializer,
)
from .substitutes import clean_rows, respond, SubstituteResponseError
from .audit import log_event

TAG = ['Leave Management - Substitutes']
SubstitutesOn = LeaveFeatureEnabled('substitutes_enabled')


@extend_schema_view(
    list=extend_schema(tags=TAG, summary='My substitutes'),
    retrieve=extend_schema(tags=TAG, summary='My substitute'),
    create=extend_schema(tags=TAG, summary='Add a substitute'),
    partial_update=extend_schema(tags=TAG, summary='Change a substitute'),
    destroy=extend_schema(tags=TAG, summary='Remove a substitute'),
)
class MySubstitutesViewSet(viewsets.ModelViewSet):
    """The caller's own substitute list. Nobody else can edit it."""
    serializer_class = SubstituteAssignmentSerializer
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature, SubstitutesOn]
    http_method_names = ['get', 'post', 'patch', 'put', 'delete']
    pagination_class = None

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return SubstituteAssignment.objects.none()
        return SubstituteAssignment.objects.filter(
            tenant=self.request.tenant, owner=self.request.user
        ).select_related('substitute')

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.tenant, owner=self.request.user)

    def update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=TAG, summary='Replace my whole substitute list',
        request=SubstituteReplaceSerializer,
        responses={200: SubstituteAssignmentSerializer(many=True)},
    )
    @action(detail=False, methods=['put'])
    def replace(self, request):
        serializer = SubstituteReplaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rows = clean_rows(request.user, serializer.validated_data['items'])
        with transaction.atomic():
            qs = self.get_queryset()
            existing = {a.substitute_id: a for a in qs}
            keep = set()
            for index, (user, task) in enumerate(rows):
                assignment = existing.get(user.pk)
                if assignment is None:
                    SubstituteAssignment.objects.create(
                        tenant=request.tenant, owner=request.user, substitute=user,
                        task=task, sort_order=index,
                    )
                else:
                    assignment.task = task
                    assignment.sort_order = index
                    assignment.save(update_fields=['task', 'sort_order', 'updated_at'])
                keep.add(user.pk)
            qs.exclude(substitute_id__in=keep).delete()
        return Response(self.get_serializer(self.get_queryset(), many=True).data)


STATE_PARAM = OpenApiParameter(
    'state', str, OpenApiParameter.QUERY, enum=['awaiting', 'all'],
    description='awaiting (default): requests waiting for my answer; all: every request I cover'
)


@extend_schema_view(
    list=extend_schema(tags=TAG, summary='Requests I cover', parameters=[STATE_PARAM]),
    retrieve=extend_schema(tags=TAG, summary='A request I cover'),
)
class CoveringViewSet(viewsets.ReadOnlyModelViewSet):
    """Leave requests where the caller is a substitute."""
    permission_classes = [IsAuthenticated, HasLeaveManagementFeature]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return LeaveRequest.objects.none()
        qs = LeaveRequest.objects.filter(
            tenant=self.request.tenant, substitutes__substitute=self.request.user
        ).select_related('employee', 'leave_type').prefetch_related('substitutes__substitute').distinct()
        if self.action == 'list' and self.request.query_params.get('state', 'awaiting') != 'all':
            qs = qs.filter(
                status='awaiting_substitutes',
                substitutes__substitute=self.request.user, substitutes__status='pending',
            )
        return qs.order_by('start_date', 'id')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return LeaveRequestDetailSerializer
        return CoveringRequestSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['language'] = self.request.query_params.get('lang', 'en')
        return context

    @extend_schema(
        tags=TAG, summary='Accept or decline covering a request',
        description='Every substitute has to accept before the request goes to its approvers; '
                    'one decline rejects it. A reason is required when declining.',
        request=SubstituteRespondSerializer, responses={200: LeaveRequestDetailSerializer},
    )
    @action(detail=True, methods=['post'])
    def respond(self, request, pk=None):
        leave_request = self.get_object()
        serializer = SubstituteRespondSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            updated = respond(
                leave_request, request.user,
                serializer.validated_data['action'], serializer.validated_data.get('comment', ''),
            )
        except SubstituteResponseError as exc:
            return Response({'error': exc.message}, status=exc.status_code)
        return Response(LeaveRequestDetailSerializer(updated, context=self.get_serializer_context()).data)
