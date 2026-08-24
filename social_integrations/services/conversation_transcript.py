"""
Cross-platform conversation transcript access.

Every message platform stores its transcript in its own table with its own
field names and outbound-flag polarity. This module is the single registry
that normalises them, so the AI companion (summaries, auto-answers) never
grows another per-platform ``if platform == ...`` ladder.

Conversations are addressed by the same (platform, account_id,
conversation_id) triple used by ChatAssignment / ConversationArchive /
ConversationAutoReply.
"""
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from django.db.models import Q

logger = logging.getLogger(__name__)


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PlatformTranscriptConfig:
    get_model: Callable[[], type]
    account_q: Callable[[str], Q]
    conversation_q: Callable[[str], Q]
    # Field whose truthiness marks a business-sent (outbound) message.
    # ``outbound_when`` is the value of that field for outbound rows —
    # widget uses is_from_visitor, where outbound means False.
    outbound_field: str
    outbound_when: bool
    text_field: str
    sender_name: Callable[[object], str]
    has_is_deleted: bool
    # Platform messaging window for business-initiated replies, in hours.
    # None = no window (telegram, widget).
    messaging_window_hours: Optional[int]
    select_related: tuple = ()


def _facebook_model():
    from social_integrations.models import FacebookMessage
    return FacebookMessage


def _instagram_model():
    from social_integrations.models import InstagramMessage
    return InstagramMessage


def _whatsapp_model():
    from social_integrations.models import WhatsAppMessage
    return WhatsAppMessage


def _telegram_model():
    from social_integrations.models import TelegramMessage
    return TelegramMessage


def _widget_model():
    from social_integrations.models import WidgetMessage
    return WidgetMessage


def _email_model():
    from social_integrations.models import EmailMessage
    return EmailMessage


PLATFORM_MESSAGE_CONFIG = {
    'facebook': PlatformTranscriptConfig(
        get_model=_facebook_model,
        account_q=lambda account_id: Q(page_connection__page_id=account_id),
        # Outbound rows deliberately store sender_id = recipient so the
        # whole conversation shares one sender_id (see views.py send paths).
        conversation_q=lambda conversation_id: Q(sender_id=conversation_id),
        outbound_field='is_from_page',
        outbound_when=True,
        text_field='message_text',
        sender_name=lambda m: m.sender_name or '',
        has_is_deleted=True,
        messaging_window_hours=24,
    ),
    'instagram': PlatformTranscriptConfig(
        get_model=_instagram_model,
        account_q=lambda account_id: Q(
            account_connection__instagram_account_id=account_id
        ),
        conversation_q=lambda conversation_id: Q(sender_id=conversation_id),
        outbound_field='is_from_business',
        outbound_when=True,
        text_field='message_text',
        sender_name=lambda m: m.sender_name or m.sender_username or '',
        has_is_deleted=True,
        messaging_window_hours=24,
    ),
    'whatsapp': PlatformTranscriptConfig(
        get_model=_whatsapp_model,
        account_q=lambda account_id: Q(business_account__waba_id=account_id),
        # Inbound rows carry the customer in from_number, outbound in
        # to_number — a conversation is the union of both directions.
        conversation_q=lambda conversation_id: (
            Q(from_number=conversation_id, is_from_business=False)
            | Q(to_number=conversation_id, is_from_business=True)
        ),
        outbound_field='is_from_business',
        outbound_when=True,
        text_field='message_text',
        sender_name=lambda m: m.contact_name or '',
        has_is_deleted=False,
        messaging_window_hours=24,
    ),
    'telegram': PlatformTranscriptConfig(
        get_model=_telegram_model,
        account_q=lambda account_id: Q(
            account__telegram_user_id=_int_or_none(account_id)
        ),
        conversation_q=lambda conversation_id: Q(
            peer_id=_int_or_none(conversation_id)
        ),
        outbound_field='is_from_business',
        outbound_when=True,
        text_field='message_text',
        sender_name=lambda m: m.peer_name or m.peer_username or '',
        has_is_deleted=True,
        messaging_window_hours=None,
    ),
    'widget': PlatformTranscriptConfig(
        get_model=_widget_model,
        account_q=lambda account_id: Q(
            session__connection_id=_int_or_none(account_id)
        ),
        conversation_q=lambda conversation_id: Q(
            session__session_id=conversation_id
        ),
        # Inverted polarity: is_from_visitor=False means the business sent it.
        outbound_field='is_from_visitor',
        outbound_when=False,
        text_field='message_text',
        sender_name=lambda m: m.session.visitor_name or '',
        has_is_deleted=True,
        messaging_window_hours=None,
        select_related=('session',),
    ),
    'email': PlatformTranscriptConfig(
        get_model=_email_model,
        account_q=lambda account_id: Q(connection_id=_int_or_none(account_id)),
        conversation_q=lambda conversation_id: Q(thread_id=conversation_id),
        outbound_field='is_from_business',
        outbound_when=True,
        text_field='body_text',
        sender_name=lambda m: m.from_name or m.from_email or '',
        has_is_deleted=True,
        messaging_window_hours=None,
    ),
}


def get_platform_config(platform):
    config = PLATFORM_MESSAGE_CONFIG.get(platform)
    if config is None:
        raise ValueError(f"Unsupported transcript platform: {platform}")
    return config


def _conversation_queryset(platform, account_id, conversation_id):
    config = get_platform_config(platform)
    model = config.get_model()
    qs = model.objects.filter(
        config.account_q(account_id) & config.conversation_q(conversation_id)
    )
    if config.has_is_deleted:
        qs = qs.filter(is_deleted=False)
    if config.select_related:
        qs = qs.select_related(*config.select_related)
    return qs, config


def _is_outbound(message, config):
    return getattr(message, config.outbound_field) == config.outbound_when


def build_transcript(platform, account_id, conversation_id, limit=30, max_chars=8000):
    """
    Return the newest ``limit`` messages of a conversation, oldest-first,
    as [{'role': 'customer'|'business', 'text', 'timestamp', 'sender_name'}].
    Entries are dropped from the OLD end until the total fits max_chars.
    """
    qs, config = _conversation_queryset(platform, account_id, conversation_id)
    messages = list(qs.order_by('-timestamp')[:limit])
    messages.reverse()

    entries = []
    for message in messages:
        text = (getattr(message, config.text_field) or '').strip()
        if not text:
            text = '[attachment]'
        entries.append({
            'role': 'business' if _is_outbound(message, config) else 'customer',
            'text': text,
            'timestamp': message.timestamp,
            'sender_name': config.sender_name(message) or '',
        })

    # Char-cap keeping the newest entries.
    total = 0
    kept = []
    for entry in reversed(entries):
        total += len(entry['text'])
        if kept and total > max_chars:
            break
        kept.append(entry)
    kept.reverse()
    return kept


def get_last_inbound_at(platform, account_id, conversation_id):
    """Timestamp of the customer's most recent message, or None."""
    qs, config = _conversation_queryset(platform, account_id, conversation_id)
    inbound = qs.filter(**{config.outbound_field: not config.outbound_when})
    return inbound.order_by('-timestamp').values_list('timestamp', flat=True).first()


def get_latest_inbound_pk(platform, account_id, conversation_id):
    """PK of the customer's most recent message — used for debounce checks."""
    qs, config = _conversation_queryset(platform, account_id, conversation_id)
    inbound = qs.filter(**{config.outbound_field: not config.outbound_when})
    return inbound.order_by('-timestamp').values_list('pk', flat=True).first()


def get_customer_display_name(platform, account_id, conversation_id):
    qs, config = _conversation_queryset(platform, account_id, conversation_id)
    inbound = qs.filter(**{config.outbound_field: not config.outbound_when})
    latest = inbound.order_by('-timestamp').first()
    if latest is None:
        return ''
    return config.sender_name(latest) or ''
