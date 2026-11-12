"""
/api/onboarding/            GET   state + ordered step list
/api/onboarding/<key>/apply/  POST  {"answers": {...}} → apply, mark done
/api/onboarding/<key>/skip/   POST  mark skipped
/api/onboarding/<key>/reset/  POST  mark pending again (answers kept)

All endpoints are tenant-admin only (``IsTenantAdmin``). A step key that is
unknown, or whose feature is not enabled for the tenant, is a 404.
"""
import json
import logging

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import registry
from .base import normalize_result
from .models import STATUS_DONE, STATUS_PENDING, STATUS_SKIPPED, TenantOnboarding
from .permissions import IsTenantAdmin
from .serializers import (
    ApplyRequestSerializer,
    ApplyResponseSerializer,
    ErrorsSerializer,
    OnboardingStateSerializer,
    StepResponseSerializer,
)
from .services import build_state_payload, build_step_payload, enabled_feature_keys

logger = logging.getLogger(__name__)


def _current_tenant(request):
    tenant = getattr(request, 'tenant', None)
    if tenant is not None:
        return tenant
    from tenants.models import Tenant
    return Tenant.objects.filter(schema_name=connection.schema_name).first()


def _json_safe(data):
    """``validated_data`` may hold OrderedDicts / dates; store plain JSON."""
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


class OnboardingBaseView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsTenantAdmin]


@extend_schema(tags=['Onboarding'])
class OnboardingStateView(OnboardingBaseView):
    """Current onboarding state with the ordered list of steps for this tenant."""

    @extend_schema(
        summary='Onboarding state',
        description=(
            'The workspace step plus one step per enabled subscription feature that '
            'has a registered onboarding step, ordered by `order`, each with its status, '
            'last answers, prefill and read context.'
        ),
        responses={200: OnboardingStateSerializer, 403: OpenApiResponse(description='Not a tenant admin')},
    )
    def get(self, request):
        tenant = _current_tenant(request)
        state = TenantOnboarding.get()
        return Response(build_state_payload(state, tenant, request))


@extend_schema(tags=['Onboarding'])
class OnboardingStepActionView(OnboardingBaseView):
    """``apply`` / ``skip`` / ``reset`` one step (chosen with ``as_view(action=...)``)."""

    action = None

    def _step_or_404(self, request, key, tenant):
        step = registry.get(key)
        if step is None:
            return None
        if step.feature_key is not None and step.feature_key not in enabled_feature_keys(tenant):
            return None
        return step

    @extend_schema(
        summary='Apply / skip / reset an onboarding step',
        request=ApplyRequestSerializer,
        responses={
            200: ApplyResponseSerializer,
            400: ErrorsSerializer,
            403: OpenApiResponse(description='Not a tenant admin'),
            404: OpenApiResponse(description='Unknown step or feature not enabled'),
        },
    )
    def post(self, request, key):
        tenant = _current_tenant(request)
        step = self._step_or_404(request, key, tenant)
        if step is None:
            return Response({'detail': 'Unknown onboarding step.'}, status=status.HTTP_404_NOT_FOUND)
        handler = getattr(self, f'_{self.action}', None)
        if handler is None:
            return Response({'detail': 'Unknown action.'}, status=status.HTTP_404_NOT_FOUND)
        return handler(request, step, tenant)

    # ── actions ──

    def _apply(self, request, step, tenant):
        body = request.data if isinstance(request.data, dict) else {}
        answers = body.get('answers')
        if not isinstance(answers, dict):
            return Response(
                {'errors': {'answers': ['Expected an object with the step answers.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        state = TenantOnboarding.get()
        serializer = step.serializer_class(
            data=answers, context={'request': request, 'tenant': tenant, 'state': state},
        )
        if not serializer.is_valid():
            return Response({'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            result = normalize_result(step.apply(tenant, serializer.validated_data, request.user))
            # Re-read: the step may have written to the row itself (industry).
            state = TenantOnboarding.get()
            state.set_status(step.key, STATUS_DONE, answers=_json_safe(serializer.validated_data), save=False)
            state.refresh_finished(enabled_feature_keys(tenant), save=False)
            state.save()
        logger.info('Onboarding step %r applied on %s by %s', step.key, tenant.schema_name, request.user.email)
        return Response({'step': build_step_payload(step, state, tenant, request), 'result': result})

    @extend_schema(summary='Skip an onboarding step', request=None, responses={200: StepResponseSerializer})
    def _skip(self, request, step, tenant):
        with transaction.atomic():
            state = TenantOnboarding.get()
            state.set_status(step.key, STATUS_SKIPPED, save=False)
            state.refresh_finished(enabled_feature_keys(tenant), save=False)
            state.save()
        return Response({'step': build_step_payload(step, state, tenant, request)})

    @extend_schema(summary='Reset an onboarding step', request=None, responses={200: StepResponseSerializer})
    def _reset(self, request, step, tenant):
        with transaction.atomic():
            state = TenantOnboarding.get()
            state.set_status(step.key, STATUS_PENDING, save=False)
            state.finished_at = None
            state.save()
        return Response({'step': build_step_payload(step, state, tenant, request)})
