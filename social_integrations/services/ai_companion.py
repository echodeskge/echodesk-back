"""
AI Companion orchestration: auto-answer decisions, human handoff, and
on-demand conversation summaries.

Configuration lives in AICompanionSettings (tenant singleton) +
AICompanionChannel rows; per-conversation mode in AIConversationState
(no row = AI active). All provider calls go through
``ai_providers.generate_structured`` and are audited in AICompanionRun.
"""
import logging

from django.conf import settings as django_settings
from django.utils import timezone

from social_integrations.services import ai_providers
from social_integrations.services.ai_providers import AICompanionError
from social_integrations.services.conversation_transcript import (
    build_transcript,
    get_customer_display_name,
)

logger = logging.getLogger(__name__)

# Platforms the companion can answer on. Email is transcript/summary-only.
REPLYABLE_PLATFORMS = ('facebook', 'instagram', 'whatsapp', 'telegram', 'widget')

DECISION_TRANSCRIPT_LIMIT = 30
DECISION_TRANSCRIPT_MAX_CHARS = 8000
SUMMARY_TRANSCRIPT_LIMIT = 200
SUMMARY_TRANSCRIPT_MAX_CHARS = 15000

COMPANION_DECISION_TOOL = 'companion_decision'
COMPANION_DECISION_SCHEMA = {
    'type': 'object',
    'required': ['action', 'reason'],
    'properties': {
        'action': {
            'type': 'string',
            'enum': ['reply', 'handoff', 'ignore'],
            'description': (
                'reply = send reply_text to the customer. '
                'handoff = a human must take over (stop replying). '
                'ignore = no response needed (e.g. a plain "thanks").'
            ),
        },
        'reply_text': {
            'type': 'string',
            'description': 'The message to send when action=reply.',
        },
        'reason': {
            'type': 'string',
            'description': 'One short sentence explaining the decision.',
        },
    },
}

CONVERSATION_SUMMARY_TOOL = 'conversation_summary'
CONVERSATION_SUMMARY_SCHEMA = {
    'type': 'object',
    'required': ['summary'],
    'properties': {
        'summary': {
            'type': 'string',
            'description': 'Concise narrative summary of the conversation.',
        },
        'customer_intent': {
            'type': 'string',
            'description': "What the customer wants, in one or two sentences.",
        },
        'open_items': {
            'type': 'string',
            'description': 'Unresolved questions or promised follow-ups.',
        },
    },
}


# ── Settings / channel resolution ───────────────────────────────────────────

def get_settings():
    from social_integrations.models import AICompanionSettings
    return AICompanionSettings.objects.first()


def resolve_channel(platform, account_id):
    """Exact (platform, account_id) row, else the (platform, '') wildcard."""
    from social_integrations.models import AICompanionChannel
    channels = {
        c.account_id: c
        for c in AICompanionChannel.objects.filter(
            platform=platform, account_id__in=[str(account_id), '']
        )
    }
    return channels.get(str(account_id)) or channels.get('')


def resolve_api_key(companion_settings):
    if companion_settings and companion_settings.api_key:
        return companion_settings.api_key
    return ''  # generate_structured falls back to the platform env key


def tenant_has_ai_companion():
    """
    Tenant-subscription check usable from webhook/worker threads where no
    user object exists (mirrors the subscription half of
    User.get_feature_keys, users/models.py).
    """
    from django.db import connection
    from tenants.models import Tenant

    schema = getattr(connection, 'schema_name', None)
    if not schema or schema == 'public':
        return False
    try:
        tenant = Tenant.objects.get(schema_name=schema)
        subscription = tenant.current_subscription
        if not subscription or not subscription.is_active:
            return False
        return subscription.selected_features.filter(
            key='ai_companion', is_active=True
        ).exists()
    except Exception:
        logger.exception('ai_companion: tenant feature check failed')
        return False


def get_conversation_state(platform, conversation_id, account_id):
    from social_integrations.models import AIConversationState
    return AIConversationState.objects.filter(
        platform=platform,
        conversation_id=str(conversation_id),
        account_id=str(account_id),
    ).first()


def is_ai_active(platform, account_id, conversation_id):
    """
    Cheap gate used by process_auto_reply and the webhook schedule hook.
    Deliberately avoids the tenants-schema feature lookup (deferred to the
    Celery task) to keep webhook threads fast.
    """
    if not getattr(django_settings, 'AI_COMPANION_ENABLED', True):
        return False
    if platform not in REPLYABLE_PLATFORMS:
        return False
    companion_settings = get_settings()
    if not companion_settings or not companion_settings.is_enabled:
        return False
    channel = resolve_channel(platform, account_id)
    if not channel or not channel.enabled:
        return False
    state = get_conversation_state(platform, conversation_id, account_id)
    return state is None or state.mode == 'ai'


def schedule_ai_reply(platform, account_id, conversation_id):
    """
    Enqueue a debounced AI reply for a just-persisted inbound message.
    Safe to call from webhook/worker threads — never raises.
    """
    try:
        if not is_ai_active(platform, account_id, conversation_id):
            return False
        from django.db import connection
        from social_integrations.services.conversation_transcript import (
            get_latest_inbound_pk,
        )
        from social_integrations.tasks import ai_companion_reply_task

        trigger_pk = get_latest_inbound_pk(platform, account_id, conversation_id)
        if trigger_pk is None:
            return False
        ai_companion_reply_task.apply_async(
            args=[
                connection.schema_name, platform, str(account_id),
                str(conversation_id), trigger_pk,
            ],
            countdown=8,
        )
        return True
    except Exception:
        logger.exception('ai_companion: failed to schedule reply')
        return False


# ── Prompt assembly ─────────────────────────────────────────────────────────

def _language_name(code):
    try:
        # ai_content_service imports the openai SDK at module level, which
        # may be absent in minimal environments — fall back gracefully.
        from social_integrations.services.ai_content_service import LANGUAGE_NAMES
    except Exception:
        LANGUAGE_NAMES = {'ka': 'Georgian', 'en': 'English', 'ru': 'Russian'}
    return LANGUAGE_NAMES.get(code, code or 'Georgian')


def _transcript_messages(transcript):
    """Collapse transcript entries into alternating chat messages."""
    messages = []
    for entry in transcript:
        role = 'assistant' if entry['role'] == 'business' else 'user'
        prefix = ''
        if role == 'user' and entry['sender_name']:
            prefix = f"{entry['sender_name']}: "
        text = f"{prefix}{entry['text']}"
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'] += f"\n{text}"
        else:
            messages.append({'role': role, 'content': text})
    # Providers require the first message to be from the user.
    while messages and messages[0]['role'] != 'user':
        messages.pop(0)
    return messages


def build_decision_system_prompt(companion_settings, channel, platform):
    guidance = (channel.guidance_prompt or companion_settings.guidance_prompt).strip()
    escalation = companion_settings.escalation_instructions.strip()
    language = _language_name(companion_settings.language)

    parts = [
        "You are an AI customer-support assistant answering on behalf of a "
        f"business in a {platform} chat. Decide how to handle the customer's "
        f"latest message by calling the {COMPANION_DECISION_TOOL} tool.",
        "Hard rules:",
        "- Never invent facts, prices, or promises not supported by the "
        "business instructions below. If you are not sure, choose handoff.",
        "- If the customer asks to talk to a human, an operator, or a "
        "specific person, choose handoff.",
        "- If the customer asks for a phone, audio, or video call with the "
        "business owner or any person, choose handoff and state the call "
        "request in the reason.",
        "- If the latest message needs no response (a bare thanks, an "
        "emoji, a goodbye), choose ignore.",
        f"- Reply in {language} unless the customer clearly writes in "
        "another language — then match their language.",
        "- Keep replies short, warm, and concrete, like a good human "
        "support agent in a chat.",
    ]
    if guidance:
        parts.append(f"Business instructions:\n{guidance}")
    if escalation:
        parts.append(f"Additional handoff rules from the business:\n{escalation}")
    return '\n\n'.join(parts)


def build_summary_system_prompt(language_code):
    language = _language_name(language_code)
    return (
        "You summarize a customer-support chat between a business and a "
        f"customer so a human agent can take over or a manager can prepare "
        f"for a call. Call the {CONVERSATION_SUMMARY_TOOL} tool. Write in "
        f"{language}. Be factual and specific: who the customer is, what "
        "they want, what was already promised or answered, and anything "
        "unresolved. Do not pad."
    )


# ── Audit helper ────────────────────────────────────────────────────────────

def _record_run(kind, platform='', account_id='', conversation_id='',
                provider='', model=''):
    from social_integrations.models import AICompanionRun
    return AICompanionRun.objects.create(
        kind=kind,
        platform=platform,
        account_id=str(account_id or ''),
        conversation_id=str(conversation_id or ''),
        provider=provider,
        model=model,
    )


def _finish_run(run, success, usage=None, action='', error='', raw=None):
    run.completed_at = timezone.now()
    run.success = success
    run.action = action
    run.error_message = (error or '')[:2000]
    if usage:
        run.prompt_tokens = usage.get('prompt_tokens')
        run.completion_tokens = usage.get('completion_tokens')
    if raw is not None:
        run.raw_response = raw
    run.save()


# ── Summary ─────────────────────────────────────────────────────────────────

def generate_summary(platform, account_id, conversation_id, requested_by=None):
    """
    Build the transcript, run the summary call, persist and return a
    ConversationSummary. Raises AICompanionError on provider failure and
    ValueError when the conversation has no messages.
    """
    from social_integrations.models import ConversationSummary

    transcript = build_transcript(
        platform, account_id, conversation_id,
        limit=SUMMARY_TRANSCRIPT_LIMIT, max_chars=SUMMARY_TRANSCRIPT_MAX_CHARS,
    )
    if not transcript:
        raise ValueError('Conversation has no messages to summarize.')

    companion_settings = get_settings()
    provider = companion_settings.provider if companion_settings else 'anthropic'
    model = (companion_settings.model if companion_settings else '') or \
        ai_providers.default_model(provider)
    language = companion_settings.language if companion_settings else 'ka'

    lines = []
    for entry in transcript:
        who = entry['sender_name'] or (
            'Customer' if entry['role'] == 'customer' else 'Business'
        )
        role_tag = 'CUSTOMER' if entry['role'] == 'customer' else 'BUSINESS'
        stamp = entry['timestamp'].strftime('%Y-%m-%d %H:%M')
        lines.append(f"[{stamp}] {role_tag} ({who}): {entry['text']}")
    transcript_block = '\n'.join(lines)

    run = _record_run(
        'summary', platform, account_id, conversation_id, provider, model
    )
    try:
        payload, usage = ai_providers.generate_structured(
            provider=provider,
            model=model,
            api_key=resolve_api_key(companion_settings),
            system=build_summary_system_prompt(language),
            messages=[{
                'role': 'user',
                'content': f"Summarize this conversation:\n\n{transcript_block}",
            }],
            tool_name=CONVERSATION_SUMMARY_TOOL,
            tool_schema=CONVERSATION_SUMMARY_SCHEMA,
            max_tokens=1500,
        )
    except AICompanionError as exc:
        _finish_run(run, success=False, error=str(exc))
        raise

    sections = [payload.get('summary', '').strip()]
    if payload.get('customer_intent', '').strip():
        sections.append(f"• {payload['customer_intent'].strip()}")
    if payload.get('open_items', '').strip():
        sections.append(f"• {payload['open_items'].strip()}")
    summary_text = '\n\n'.join(s for s in sections if s)

    summary = ConversationSummary.objects.create(
        platform=platform,
        account_id=str(account_id),
        conversation_id=str(conversation_id),
        summary_text=summary_text,
        provider=provider,
        model=model,
        prompt_tokens=usage.get('prompt_tokens'),
        completion_tokens=usage.get('completion_tokens'),
        requested_by=requested_by,
    )
    _finish_run(run, success=True, usage=usage)
    return summary


# ── Decision / reply / handoff ──────────────────────────────────────────────

def run_decision(companion_settings, channel, platform, account_id, conversation_id):
    """One structured decision call. Returns the tool payload; raises on failure."""
    transcript = build_transcript(
        platform, account_id, conversation_id,
        limit=DECISION_TRANSCRIPT_LIMIT, max_chars=DECISION_TRANSCRIPT_MAX_CHARS,
    )
    messages = _transcript_messages(transcript)
    if not messages:
        raise ValueError('No inbound messages to answer.')

    provider = companion_settings.provider
    model = companion_settings.model or ai_providers.default_model(provider)
    run = _record_run(
        'reply', platform, account_id, conversation_id, provider, model
    )
    try:
        payload, usage = ai_providers.generate_structured(
            provider=provider,
            model=model,
            api_key=resolve_api_key(companion_settings),
            system=build_decision_system_prompt(companion_settings, channel, platform),
            messages=messages,
            tool_name=COMPANION_DECISION_TOOL,
            tool_schema=COMPANION_DECISION_SCHEMA,
            max_tokens=1000,
        )
    except AICompanionError as exc:
        _finish_run(run, success=False, error=str(exc))
        raise
    _finish_run(
        run, success=True, usage=usage,
        action=payload.get('action', ''), raw=payload,
    )
    return payload


def _get_platform_connection(platform, account_id):
    from social_integrations.models import (
        FacebookPageConnection,
        InstagramAccountConnection,
        TelegramAccount,
        WhatsAppBusinessAccount,
    )
    try:
        if platform == 'facebook':
            return FacebookPageConnection.objects.get(page_id=account_id, is_active=True)
        if platform == 'instagram':
            return InstagramAccountConnection.objects.get(
                instagram_account_id=account_id, is_active=True
            )
        if platform == 'whatsapp':
            return WhatsAppBusinessAccount.objects.get(waba_id=account_id, is_active=True)
        if platform == 'telegram':
            return TelegramAccount.objects.get(
                telegram_user_id=int(account_id), is_active=True
            )
    except Exception:
        logger.warning(
            'ai_companion: no active %s connection for account %s',
            platform, account_id,
        )
    return None


def send_ai_reply(platform, account_id, conversation_id, reply_text):
    """
    Deliver an AI reply through the existing per-platform senders,
    synchronously (we are already inside a Celery task).
    Returns True when the platform accepted the send.
    """
    if platform == 'widget':
        return _send_widget_ai_reply(account_id, conversation_id, reply_text)

    from social_integrations import views
    sender_map = {
        'facebook': views.send_facebook_auto_reply,
        'instagram': views.send_instagram_auto_reply,
        'whatsapp': views.send_whatsapp_auto_reply,
        'telegram': views.send_telegram_auto_reply,
    }
    sender = sender_map.get(platform)
    if sender is None:
        return False
    conn = _get_platform_connection(platform, account_id)
    if conn is None:
        return False
    return bool(sender(conversation_id, reply_text, conn))


def _send_widget_ai_reply(account_id, session_id, reply_text):
    """Create the outbound widget row + broadcast, mirroring the agent send
    path in widget_views."""
    import uuid

    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from django.db import connection as db_connection

    from social_integrations.models import WidgetMessage, WidgetSession

    try:
        session = WidgetSession.objects.get(
            session_id=session_id, connection_id=int(account_id)
        )
    except (WidgetSession.DoesNotExist, TypeError, ValueError):
        logger.warning('ai_companion: widget session %s not found', session_id)
        return False

    now = timezone.now()
    msg = WidgetMessage.objects.create(
        session=session,
        message_id=uuid.uuid4().hex,
        message_text=reply_text,
        is_from_visitor=False,
        sent_by=None,
        is_delivered=True,
        delivered_at=now,
        timestamp=now,
    )
    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            schema = db_connection.schema_name
            conversation_id = f"widget_{account_id}_{session_id}"
            async_to_sync(channel_layer.group_send)(f'messages_{schema}', {
                'type': 'new_message',
                'message': {
                    'message_id': msg.message_id,
                    'message_text': msg.message_text,
                    'attachments': [],
                    'is_from_visitor': False,
                    'timestamp': msg.timestamp.isoformat(),
                    'session_id': session_id,
                    'connection_id': int(account_id),
                    'platform': 'widget',
                },
                'conversation_id': conversation_id,
                'timestamp': msg.timestamp.isoformat(),
            })
    except Exception:
        logger.exception('ai_companion: widget reply broadcast failed')
    return True


def set_conversation_mode(platform, conversation_id, account_id, mode,
                          reason='', broadcast=True):
    """Create/update the state row and broadcast the change."""
    from social_integrations.models import AIConversationState

    state, _ = AIConversationState.objects.get_or_create(
        platform=platform,
        conversation_id=str(conversation_id),
        account_id=str(account_id),
    )
    state.mode = mode
    state.reason = reason or ''
    if mode == 'needs_human':
        state.escalated_at = timezone.now()
    state.save()

    if broadcast:
        _broadcast_state(state)
    return state


def apply_handoff(platform, account_id, conversation_id, reason):
    """AI decided a human must take over: flip state + notify + broadcast."""
    state = set_conversation_mode(
        platform, conversation_id, account_id, 'needs_human', reason=reason
    )
    try:
        from users.notification_utils import create_ai_handoff_notification
        create_ai_handoff_notification(
            platform=platform,
            conversation_id=str(conversation_id),
            account_id=str(account_id),
            sender_name=get_customer_display_name(
                platform, account_id, conversation_id
            ),
            reason=reason,
        )
    except Exception:
        logger.exception('ai_companion: handoff notification failed')
    return state


def _broadcast_state(state):
    try:
        from asgiref.sync import async_to_sync
        from django.db import connection as db_connection

        from social_integrations.consumers import send_ai_state_update

        async_to_sync(send_ai_state_update)(
            db_connection.schema_name,
            platform=state.platform,
            conversation_id=state.conversation_id,
            account_id=state.account_id,
            mode=state.mode,
            reason=state.reason,
        )
    except Exception:
        logger.exception('ai_companion: state broadcast failed')
