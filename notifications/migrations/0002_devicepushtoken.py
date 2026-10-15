# Additive migration: new DevicePushToken table + nullable NotificationLog FK.
# Safe to auto-apply on every tenant schema.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('notifications', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='DevicePushToken',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.CharField(max_length=255, unique=True)),
                ('platform', models.CharField(choices=[('ios', 'iOS'), ('android', 'Android')], max_length=10)),
                ('device_id', models.CharField(blank=True, db_index=True, max_length=255)),
                ('device_name', models.CharField(blank=True, max_length=255)),
                ('app_version', models.CharField(blank=True, max_length=50)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_seen_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='device_push_tokens', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Device Push Token',
                'verbose_name_plural': 'Device Push Tokens',
                'ordering': ['-last_seen_at'],
            },
        ),
        migrations.AddField(
            model_name='notificationlog',
            name='device_token',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='notifications.devicepushtoken'),
        ),
        migrations.AddIndex(
            model_name='devicepushtoken',
            index=models.Index(fields=['user', 'is_active'], name='notificatio_user_id_f95ba1_idx'),
        ),
    ]
