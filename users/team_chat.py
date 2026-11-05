"""
Shared team-chat helpers used by both the websocket consumer
(``users.consumers.TeamChatConsumer``) and the REST views.

- ``get_or_create_pair_conversation``: the one race-free way to find or create
  the 1:1 conversation of two users (BE-2).
- ``unhide_conversation``: a new message brings a hidden thread back (BE-7).
- ``message_event``: the channel-layer event for a new message, carrying the
  routing keys clients need to place it in the right window (BE-1).
- ``read_receipt_event``: the channel-layer event for a read receipt (BE-6).
"""
from django.db import connection, transaction


def personal_group(tenant_schema, user_id):
    """Channel-layer group every team-chat socket of ``user_id`` joins."""
    return f'team_chat_{tenant_schema}_{user_id}'


def find_pair_conversation(user_a, user_b):
    """The 1:1 conversation of two users, oldest first (deterministic)."""
    from users.models import TeamChatConversation

    return (
        TeamChatConversation.objects
        .filter(participants=user_a)
        .filter(participants=user_b)
        .order_by('id')
        .first()
    )


def get_or_create_pair_conversation(user_a, user_b):
    """
    Return ``(conversation, created)`` for the 1:1 conversation of two users.

    A transaction-level Postgres advisory lock keyed on the tenant schema and
    the sorted user pair serialises concurrent first contacts, so two
    colleagues opening the chat (or sending the first message) at the same
    time end up in one conversation instead of two. ``select_for_update`` is
    not enough here: it locks nothing while the row does not exist yet.
    """
    from users.models import TeamChatConversation

    lo, hi = sorted((user_a.id, user_b.id))
    schema = getattr(connection, 'schema_name', '') or ''
    with transaction.atomic():
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                    [f'team_chat_pair:{schema}:{lo}:{hi}'],
                )
        conversation = find_pair_conversation(user_a, user_b)
        created = conversation is None
        if created:
            conversation = TeamChatConversation.objects.create()
            conversation.participants.add(user_a, user_b)
    return conversation, created


def unhide_conversation(conversation):
    """Bring a hidden conversation back for every participant (new activity)."""
    from users.models import HiddenTeamChatConversation

    HiddenTeamChatConversation.objects.filter(conversation=conversation).delete()


def message_event(message_data, conversation_id, sender_id, recipient_id, origin_channel=None):
    """
    Channel-layer ``chat_message`` event for a new team-chat message.

    ``sender_id`` / ``recipient_id`` let every receiving socket work out which
    chat window the message belongs to even when that window has no (or a
    stale) conversation id; the consumer adds ``other_user_id`` relative to the
    socket's own user. ``origin_channel`` marks the socket that sent it (that
    socket gets its own ``message_sent`` ack, so it skips the fan-out copy).
    """
    event = {
        'type': 'chat_message',
        'message': message_data,
        'conversation': conversation_id,
        'conversation_id': conversation_id,
        'sender_id': sender_id,
        'recipient_id': recipient_id,
        'participant_ids': [sender_id, recipient_id],
    }
    if origin_channel:
        event['origin_channel'] = origin_channel
    return event


def read_receipt_event(conversation_id, reader_id, message_ids):
    """Channel-layer ``read_receipt`` event (``is_read`` flipped for ``message_ids``)."""
    return {
        'type': 'read_receipt',
        'message_ids': list(message_ids),
        'read_by': reader_id,
        'conversation_id': conversation_id,
    }


def broadcast_read_receipt(conversation_id, reader_id, recipient_ids, message_ids, tenant_schema=None):
    """
    Sync helper for REST views: tell ``recipient_ids`` (the senders of the
    messages) that ``reader_id`` read ``message_ids``. Never raises.
    """
    import logging

    logger = logging.getLogger(__name__)
    message_ids = list(message_ids)
    if not message_ids or not recipient_ids:
        return
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        if tenant_schema is None:
            tenant_schema = connection.schema_name
        event = read_receipt_event(conversation_id, reader_id, message_ids)
        for uid in set(recipient_ids):
            async_to_sync(channel_layer.group_send)(personal_group(tenant_schema, uid), event)
    except (ConnectionError, OSError) as e:
        logger.warning(f"Team chat read receipt could not be delivered: {e}")
    except Exception as e:
        logger.error(f"Failed to broadcast team chat read receipt: {e}")
