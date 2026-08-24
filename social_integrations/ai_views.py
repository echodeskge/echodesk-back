"""
AI Companion API: settings, on-demand summaries, per-conversation state.

All endpoints are gated by the `ai_companion` feature; settings writes
additionally require the admin role (see permissions module).
"""
import logging

from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import serializers as drf_serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import (
    AICompanionChannel,
    AICompanionSettings,
    AIConversationState,
    ConversationSummary,
)
from .permissions import CanManageAICompanion, CanUseAICompanion
from .services import ai_companion
from .services.ai_providers import AICompanionError
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

    def get_has_api_key(self, obj):
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

    def get_requested_by_name(self, obj):
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
    responses=AICompanionSettingsSerializer,
    description="Get or update the tenant's AI companion configuration",
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
    responses={201: ConversationSummarySerializer},
    description="Generate an AI summary of the whole conversation (synchronous)",
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
            conversation_id=data['conversation_id'],
            requested_by=request.user,
        )
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
    responses=ConversationSummarySerializer(many=True),
    description="List recent summaries for one conversation (newest first)",
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
        conversation_id=data['conversation_id'],
    )[:10]
    return Response({
        'results': ConversationSummarySerializer(summaries, many=True).data,
    })


@extend_schema(
    description=(
        "GET: read the conversation's AI mode (defaults to mode 'ai' when "
        "no state exists). POST: pause ('off') or resume ('ai') the AI for "
        "this conversation."
    ),
    methods=['GET', 'POST'],
    request=AIStateUpdateSerializer,
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
            conversation_id=data['conversation_id'],
            account_id=data['account_id'],
            mode=data['mode'],
            reason='' if data['mode'] == 'ai' else 'Paused by agent',
        )
    else:
        state = AIConversationState.objects.filter(
            platform=data['platform'],
            conversation_id=data['conversation_id'],
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
    request={
        'application/json': {
            'type': 'object',
            'required': ['message'],
            'properties': {
                'message': {'type': 'string'},
                'guidance_prompt': {'type': 'string'},
            },
        }
    },
    description="Dry-run one customer message against the companion prompt",
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
