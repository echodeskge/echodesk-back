"""
Merge duplicate 1:1 team-chat conversations.

Before ``users.team_chat.get_or_create_pair_conversation`` serialised first
contact with an advisory lock, two concurrent requests could each create a
conversation for the same pair of users. Every lookup now picks the pair's
oldest conversation (``order_by('id')``), so the messages of the newer
duplicates are moved onto it and the duplicates are deleted.

A user keeps the merged conversation hidden only if they had hidden every
conversation of the pair. Data-only; the reverse is a no-op.
"""
from collections import defaultdict

from django.db import migrations
from django.db.models import Max


def merge_duplicate_pairs(apps, schema_editor):
    Conversation = apps.get_model('users', 'TeamChatConversation')
    Message = apps.get_model('users', 'TeamChatMessage')
    Hidden = apps.get_model('users', 'HiddenTeamChatConversation')
    Through = Conversation.participants.through

    participants = defaultdict(set)
    for conv_id, user_id in Through.objects.values_list('teamchatconversation_id', 'user_id'):
        participants[conv_id].add(user_id)

    pairs = defaultdict(list)
    for conv_id in sorted(participants):
        users = participants[conv_id]
        if len(users) == 2:
            pairs[tuple(sorted(users))].append(conv_id)

    for pair, conv_ids in pairs.items():
        if len(conv_ids) < 2:
            continue
        keep, duplicates = conv_ids[0], conv_ids[1:]

        latest = Conversation.objects.filter(id__in=conv_ids).aggregate(latest=Max('updated_at'))['latest']

        # Hidden only if the user had hidden every conversation of the pair.
        hidden_counts = defaultdict(int)
        for user_id in Hidden.objects.filter(conversation_id__in=conv_ids).values_list('user_id', flat=True):
            hidden_counts[user_id] += 1
        for user_id in pair:
            if hidden_counts.get(user_id, 0) < len(conv_ids):
                Hidden.objects.filter(conversation_id=keep, user_id=user_id).delete()
            elif not Hidden.objects.filter(conversation_id=keep, user_id=user_id).exists():
                Hidden.objects.create(conversation_id=keep, user_id=user_id)

        Message.objects.filter(conversation_id__in=duplicates).update(conversation_id=keep)
        # Cascades the duplicates' participant and hidden rows.
        Conversation.objects.filter(id__in=duplicates).delete()
        if latest is not None:
            # .update() bypasses auto_now, so the merged thread keeps its
            # latest activity time.
            Conversation.objects.filter(id=keep).update(updated_at=latest)


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0025_user_preferred_language'),
    ]

    operations = [
        migrations.RunPython(merge_duplicate_pairs, migrations.RunPython.noop),
    ]
