"""Tickets redesign: additive fields.

Every operation is old-code safe (R1): new columns are nullable or have a
default. Django drops the DB-level DEFAULT after AddField(default=...), so the
three NOT NULL columns that older code would omit on INSERT get a permanent
DB default via RunSQL (same pattern as social_integrations/0035).
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('social_integrations', '0065_conversation_ticket_client_tags'),
        ('tickets', '0040_add_performance_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='board',
            name='color',
            field=models.CharField(default='#3b82f6', max_length=7),
        ),
        migrations.AddField(
            model_name='board',
            name='position',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='ticket',
            name='due_date',
            field=models.DateField(blank=True, db_index=True, help_text='Task due date', null=True),
        ),
        migrations.AddField(
            model_name='ticket',
            name='customer',
            field=models.ForeignKey(blank=True, help_text='Customer (client) this ticket is about', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tickets', to='social_integrations.client'),
        ),
        migrations.AddField(
            model_name='ticketcomment',
            name='is_internal',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='checklistitem',
            name='checked_by',
            field=models.ForeignKey(blank=True, help_text='User who last checked this item', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='checklistitem',
            name='checked_at',
            field=models.DateTimeField(blank=True, help_text='When this item was last checked', null=True),
        ),
        migrations.RunSQL(
            sql="ALTER TABLE tickets_ticketcomment ALTER COLUMN is_internal SET DEFAULT true;",
            reverse_sql="ALTER TABLE tickets_ticketcomment ALTER COLUMN is_internal DROP DEFAULT;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE tickets_board ALTER COLUMN color SET DEFAULT '#3b82f6';",
            reverse_sql="ALTER TABLE tickets_board ALTER COLUMN color DROP DEFAULT;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE tickets_board ALTER COLUMN position SET DEFAULT 0;",
            reverse_sql="ALTER TABLE tickets_board ALTER COLUMN position DROP DEFAULT;",
        ),
    ]
