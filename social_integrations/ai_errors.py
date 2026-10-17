"""
Stable "AI not configured" error contract shared by every AI endpoint.

When no provider API key is available (no tenant override in AI companion
settings and no platform env key), AI endpoints respond with:

    HTTP 503
    {"code": "ai_not_configured", "detail": "<human message>"}

Clients (web + mobile) switch on ``code`` to show a dedicated "AI isn't set
up" state instead of a generic error. Real validation errors keep their
existing 4xx responses.
"""
from drf_spectacular.utils import OpenApiResponse
from rest_framework import serializers, status
from rest_framework.response import Response

AI_NOT_CONFIGURED_CODE = 'ai_not_configured'
AI_NOT_CONFIGURED_DETAIL = (
    "AI isn't set up for this workspace yet. "
    "An admin can enable it in web settings."
)


class AINotConfiguredErrorSerializer(serializers.Serializer):
    code = serializers.ChoiceField(
        choices=[AI_NOT_CONFIGURED_CODE],
        help_text="Always 'ai_not_configured'",
    )
    detail = serializers.CharField(help_text='Human-readable explanation')
    error = serializers.CharField(help_text='Same text as detail (the web reads `error`)')


AI_NOT_CONFIGURED_RESPONSE = OpenApiResponse(
    response=AINotConfiguredErrorSerializer,
    description=(
        "No AI provider API key is configured for this workspace "
        "(code 'ai_not_configured')."
    ),
)


def ai_not_configured_response():
    return Response(
        {
            'code': AI_NOT_CONFIGURED_CODE,
            'detail': AI_NOT_CONFIGURED_DETAIL,
            # The web summary dialog toasts `error`; keep it until it reads `detail`.
            'error': AI_NOT_CONFIGURED_DETAIL,
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )
