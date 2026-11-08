"""Tickets redesign: personal timer entries (TicketTimeEntry)."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('tickets', '0041_redesign_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='TicketTimeEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('ended_at', models.DateTimeField(null=True)),
                ('duration_seconds', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('ticket', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='time_entries', to='tickets.ticket')),
                ('user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ticket_time_entries', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-started_at'],
                'indexes': [
                    models.Index(fields=['ticket', '-started_at'], name='ticket_time_entry_tkt_idx'),
                    models.Index(fields=['user', 'ended_at'], name='ticket_time_entry_usr_idx'),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name='tickettimeentry',
            constraint=models.UniqueConstraint(condition=models.Q(('ended_at__isnull', True)), fields=('user',), name='tickets_one_running_timer_per_user'),
        ),
    ]
