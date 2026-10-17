"""
AI Companion API: settings, on-demand summaries, per-conversation state.

All endpoints are gated by the `ai_companion` feature; settings writes
additionally require the admin role (see permissions module).
"""
import logging
from typing import Optional

from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_serializer
from rest_framework import serializers as drf_serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .ai_errors import AI_NOT_CONFIGURED_RESPONSE, ai_not_configured_response
from .models import (
    AICompanionChannel,
    AICompanionSettings,
    AIConversationState,
    ConversationSummary,
)
from .permissions import CanManageAICompanion, CanUseAICompanion
from .services import ai_companion
from .services.ai_providers import AICompanionError, AINotConfiguredError
from .services.conversation_transcript import PLATFORM_MESSAGE_CONFIG

logger = logging.getLogger(__name__)

AI_PLATFORMS = tuple(PLATFORM_MESSAGE_CONFIG.keys())


class AICompanionChannelSerializer(drf_serializers.Serializer):
    platform = drf_serializers.ChoiceField(choices=ai_companion.REPLYABLE_PLATFORMS)
    account_id = drf_serializers.CharField(allow_blank=True, default='', max_length=255)
    enabled = drf_serializers.BooleanField(default=False)
    guidance_prompt = drf_serializers.CharField(allow_blank=True, default='')


class AICompanionSettingsSerializer(drf_serializers.ModelSerializer):
    api_key = drf_serializers.CharField(
        write_only=True, required=False, allow_blank=True,
        help_text='Tenant API key override; write-only, empty string clears it',
    )
    has_api_key = drf_serializers.SerializerMethodField()
    channels = AICompanionChannelSerializer(many=True, required=False, write_only=True)

    class Meta:
        model = AICompanionSettings
        fields = [
            'is_enabled', 'provider', 'model', 'api_key', 'has_api_key',
            'guidance_prompt', 'escalation_instructions', 'language',
            'max_replies_per_conversation_per_day', 'max_replies_per_day',
            'channels', 'updated_at',
        ]
        read_only_fields = ['updated_at']

    def get_has_api_key(self, obj) -> bool:
        return bool(obj.api_key)

    def update(self, instance, validated_data):
        channels = validated_data.pop('channels', None)
        api_key = validated_data.pop('api_key', None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if api_key is not None:
            instance.api_key = api_key
        instance.save()

        if channels is not None:
            # Full-list sync: the payload is the complete channel set.
            kept_pks = []
            for c in channels:
                channel, _ = AICompanionChannel.objects.update_or_create(
                    platform=c['platform'],
                    account_id=c.get('account_id', ''),
                    defaults={
                        'enabled': c.get('enabled', False),
                        'guidance_prompt': c.get('guidance_prompt', ''),
                    },
                )
                kept_pks.append(channel.pk)
            AICompanionChannel.objects.exclude(pk__in=kept_pks).delete()
        return instance


class ConversationSummarySerializer(drf_serializers.ModelSerializer):
    requested_by_name = drf_serializers.SerializerMethodField()

    class Meta:
        model = ConversationSummary
        fields = [
            'id', 'platform', 'account_id', 'conversation_id', 'summary_text',
            'provider', 'model', 'created_at', 'requested_by_name',
        ]

    def get_requested_by_name(self, obj) -> Optional[str]:
        if obj.requested_by:
            full = f"{obj.requested_by.first_name} {obj.requested_by.last_name}".strip()
            return full or obj.requested_by.email
        return None


class ConversationTripleSerializer(drf_serializers.Serializer):
    platform = drf_serializers.ChoiceField(choices=AI_PLATFORMS)
    conversation_id = drf_serializers.CharField(max_length=255)
    account_id = drf_serializers.CharField(max_length=255)


class AIStateUpdateSerializer(ConversationTripleSerializer):
    mode = drf_serializers.ChoiceField(choices=['ai', 'off'])


# ── Response shapes (documentation for the generated clients) ──

@extend_schema_serializer(component_name='AICompanionSettings')
class AICompanionSettingsResponseSerializer(AICompanionSettingsSerializer):
    """GET/PATCH /ai/settings/ response: settings plus the full channel list."""
    channels = AICompanionChannelSerializer(many=True, read_only=True)


class AIStateResponseSerializer(drf_serializers.Serializer):
    mode = drf_serializers.ChoiceField(
        choices=['ai', 'needs_human', 'off'],
        help_text="'ai' (default when no state exists), 'needs_human' (escalated) or 'off' (paused)",
    )
    reason = drf_serializers.CharField(allow_blank=True)
    last_ai_reply_at = drf_serializers.DateTimeField(allow_null=True)
    updated_at = drf_serializers.DateTimeField(allow_null=True)


class AISummaryCreatedSerializer(drf_serializers.Serializer):
    summary = ConversationSummarySerializer()


class AISummaryListSerializer(drf_serializers.Serializer):
    results = ConversationSummarySerializer(many=True, help_text='At most 10, newest first')


class AITestSerializer(drf_serializers.Serializer):
    message = drf_serializers.CharField()
    guidance_prompt = drf_serializers.CharField(required=False, allow_blank=True)


class AITestResponseSerializer(drf_serializers.Serializer):
    action = drf_serializers.CharField(help_text="'reply' | 'handoff' | 'ignore'")
    reply_text = drf_serializers.CharField(allow_blank=True)
    reason = drf_serializers.CharField(allow_blank=True)


class AIErrorSerializer(drf_serializers.Serializer):
    error = drf_serializers.CharField()


def _state_conversation_id(platform, conversation_id, account_id):
    """AI state for widget chats is keyed by session id (the dashboard may
    send the visitor id or the prefixed list id)."""
    from .conversation_keys import session_conversation_id
    return session_conversation_id(platform, conversation_id, account_id)


def _get_or_create_settings():
    obj = AICompanionSettings.objects.first()
    if obj is None:
        obj = AICompanionSettings.objects.create()
    return obj


def _settings_payload(obj):
    data = AICompanionSettingsSerializer(obj).data
    data['channels'] = [
        {
            'platform': c.platform,
            'account_id': c.account_id,
            'enabled': c.enabled,
            'guidance_prompt': c.guidance_prompt,
        }
        for c in AICompanionChannel.objects.all().order_by('platform', 'account_id')
    ]
    return data


@extend_schema(
    responses=AICompanionSettingsResponseSerializer,
    description=(
        "Get or update the tenant's AI companion configuration. PATCH `channels` "
        "is a FULL-LIST sync: any channel not in the payload is deleted, so send "
        "the complete list or omit the key."
    ),
    methods=['GET', 'PATCH'],
    request=AICompanionSettingsSerializer,
)
@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated, CanManageAICompanion])
def ai_companion_settings(request):
    obj = _get_or_create_settings()
    if request.method == 'GET':
        return Response(_settings_payload(obj))

    serializer = AICompanionSettingsSerializer(obj, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(_settings_payload(obj))


@extend_schema(
    request=ConversationTripleSerializer,
    responses={
        201: AISummaryCreatedSerializer,
        400: AIErrorSerializer,
        422: AIErrorSerializer,
        503: AI_NOT_CONFIGURED_RESPONSE,
    },
    description=(
        "Generate an AI summary of the whole conversation (synchronous). "
        "Returns 503 `{code: 'ai_not_configured', detail}` when no AI provider "
        "key is configured; 422 for provider failures."
    ),
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanUseAICompanion])
def ai_summarize_conversation(request):
    serializer = ConversationTripleSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        summary = ai_companion.generate_summary(
            platform=data['platform'],
            account_id=data['account_id'],
            conversation_id=_state_conversation_id(data['platform'], data['conversation_id'], data['account_id']),
            requested_by=request.user,
        )
    except AINotConfiguredError as exc:
        logger.info('AI summary skipped, AI not configured: %s', exc)
        return ai_not_configured_response()
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except AICompanionError as exc:
        logger.warning('AI summary failed: %s', exc)
        return Response(
            {'error': f'AI summary failed: {exc}'},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return Response(
        {'summary': ConversationSummarySerializer(summary).data},
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    parameters=[
        OpenApiParameter('platform', str, required=True),
        OpenApiParameter('conversation_id', str, required=True),
        OpenApiParameter('account_id', str, required=True),
    ],
    responses=AISummaryListSerializer,
    operation_id='social_ai_summaries_list',
    description="List recent summaries for one conversation (newest first, at most 10)",
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, CanUseAICompanion])
def ai_conversation_summaries(request):
    serializer = ConversationTripleSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    summaries = ConversationSummary.objects.filter(
        platform=data['platform'],
        account_id=data['account_id'],
        conversation_id=_state_conversation_id(data['platform'], data['conversation_id'], data['account_id']),
    )[:10]
    return Response({
        'results': ConversationSummarySerializer(summaries, many=True).data,
    })


@extend_schema(
    description=(
        "Read the conversation's AI mode (mode 'ai' when no state exists). "
        "Widget chats accept the visitor id, session id or list id."
    ),
    methods=['GET'],
    parameters=[
        OpenApiParameter('platform', str, required=True, enum=list(AI_PLATFORMS)),
        OpenApiParameter('conversation_id', str, required=True),
        OpenApiParameter('account_id', str, required=True),
    ],
    responses={200: AIStateResponseSerializer},
)
@extend_schema(
    description="Pause ('off') or resume ('ai') the AI for this conversation.",
    methods=['POST'],
    request=AIStateUpdateSerializer,
    responses={200: AIStateResponseSerializer},
)
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, CanUseAICompanion])
def ai_conversation_state(request):
    if request.method == 'GET':
        serializer = ConversationTripleSerializer(data=request.query_params)
    else:
        serializer = AIStateUpdateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    if request.method == 'POST':
        state = ai_companion.set_conversation_mode(
            platform=data['platform'],
            conversation_id=_state_conversation_id(data['platform'], data['conversation_id'], data['account_id']),
            account_id=data['account_id'],
            mode=data['mode'],
            reason='' if data['mode'] == 'ai' else 'Paused by agent',
        )
    else:
        state = AIConversationState.objects.filter(
            platform=data['platform'],
            conversation_id=_state_conversation_id(data['platform'], data['conversation_id'], data['account_id']),
            account_id=data['account_id'],
        ).first()

    if state is None:
        return Response({
            'mode': 'ai', 'reason': '',
            'last_ai_reply_at': None, 'updated_at': None,
        })
    return Response({
        'mode': state.mode,
        'reason': state.reason,
        'last_ai_reply_at': state.last_ai_reply_at,
        'updated_at': state.updated_at,
    })


@extend_schema(
    request=AITestSerializer,
    responses={
        200: AITestResponseSerializer,
        400: AIErrorSerializer,
        422: AIErrorSerializer,
        503: AI_NOT_CONFIGURED_RESPONSE,
    },
    description=(
        "Dry-run one customer message against the companion prompt. Returns "
        "503 `{code: 'ai_not_configured', detail}` when no AI provider key is "
        "configured."
    ),
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageAICompanion])
def ai_companion_test(request):
    message = (request.data.get('message') or '').strip()
    if not message:
        return Response({'error': 'message is required'}, status=status.HTTP_400_BAD_REQUEST)

    obj = _get_or_create_settings()
    guidance_override = request.data.get('guidance_prompt')

    class _TempChannel:
        guidance_prompt = guidance_override or ''

    from .services import ai_providers
    from .models import AICompanionRun
    from django.utils import timezone as dj_timezone

    provider = obj.provider
    model = obj.model or ai_providers.default_model(provider)
    run = AICompanionRun.objects.create(kind='test', provider=provider, model=model)
    try:
        payload, usage = ai_providers.generate_structured(
            provider=provider,
            model=model,
            api_key=ai_companion.resolve_api_key(obj),
            system=ai_companion.build_decision_system_prompt(obj, _TempChannel(), 'chat'),
            messages=[{'role': 'user', 'content': message}],
            tool_name=ai_companion.COMPANION_DECISION_TOOL,
            tool_schema=ai_companion.COMPANION_DECISION_SCHEMA,
            max_tokens=1000,
        )
    except AICompanionError as exc:
        run.completed_at = dj_timezone.now()
        run.error_message = str(exc)[:2000]
        run.save()
        if isinstance(exc, AINotConfiguredError):
            return ai_not_configured_response()
        return Response({'error': str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    run.completed_at = dj_timezone.now()
    run.success = True
    run.action = payload.get('action', '')
    run.prompt_tokens = usage.get('prompt_tokens')
    run.completion_tokens = usage.get('completion_tokens')
    run.raw_response = payload
    run.save()
    return Response({
        'action': payload.get('action'),
        'reply_text': payload.get('reply_text', ''),
        'reason': payload.get('reason', ''),
    })
