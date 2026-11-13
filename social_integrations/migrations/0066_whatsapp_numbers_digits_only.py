"""WhatsApp numbers are stored digits-only.

Our own senders (rating requests, auto-replies, the Business App echo sync)
stored the customer as ``+995577603228`` and the business line as
``+995 579 37 03 74`` while Meta's webhook stores ``995577603228``. The inbox
groups a chat by that column, so one customer showed up as two rows, and an
archive / assignment keyed by the '+' form never matched the list.
``WhatsAppMessage.save`` now normalises; this rewrites what is already there.
"""
import re

from django.db import migrations


def _digits(raw):
    return re.sub(r'\D', '', str(raw or ''))


def forwards(apps, schema_editor):
    WhatsAppMessage = apps.get_model('social_integrations', 'WhatsAppMessage')
    table = WhatsAppMessage._meta.db_table
    with schema_editor.connection.cursor() as cur:
        for col in ('from_number', 'to_number'):
            # Only rows with a non-digit, and never blank a value that has no
            # digits at all (nothing to group by would be worse than a '+').
            cur.execute(
                f'UPDATE "{table}" SET "{col}" = regexp_replace("{col}", \'\\D\', \'\', \'g\') '
                f'WHERE "{col}" ~ \'\\D\' AND regexp_replace("{col}", \'\\D\', \'\', \'g\') <> \'\''
            )

    for model_name in ('ConversationArchive', 'ChatAssignment'):
        Model = apps.get_model('social_integrations', model_name)
        for row in Model.objects.filter(platform='whatsapp').exclude(conversation_id__regex=r'^[0-9]+$'):
            key = _digits(row.conversation_id)
            if not key:
                continue
            clash = Model.objects.filter(
                platform='whatsapp', account_id=row.account_id, conversation_id=key,
            ).exclude(pk=row.pk).exists()
            if clash:
                # The digits-only row is the one the list reads; the '+' twin
                # was unreachable, so dropping it loses nothing visible.
                row.delete()
            else:
                row.conversation_id = key
                row.save(update_fields=['conversation_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('social_integrations', '0065_conversation_ticket_client_tags'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
