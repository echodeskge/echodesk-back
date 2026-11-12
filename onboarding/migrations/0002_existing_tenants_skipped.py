"""Tenants that existed before onboarding shipped must not be sent through the
wizard: pre-fill their row with every step ``skipped`` so the gate and the
"Finish setup" chip stay quiet. A freshly provisioned schema has no users yet
when this migration runs (the admin is created after ``migrate_schemas``), so
it is left untouched and ``process_pending_tenants`` marks its steps pending.
"""
from django.db import migrations
from django.utils import timezone

STEP_KEYS = [
    'workspace', 'tickets', 'social_integrations', 'booking_management',
    'ecommerce_crm', 'invoice_management', 'leave_management', 'ip_calling',
]


def skip_existing(apps, schema_editor):
    User = apps.get_model('users', 'User')
    TenantOnboarding = apps.get_model('onboarding', 'TenantOnboarding')
    if not User.objects.exists():
        return  # brand-new schema: provisioning will mark steps pending
    if TenantOnboarding.objects.exists():
        return
    now = timezone.now()
    TenantOnboarding.objects.create(
        state={key: {'status': 'skipped', 'answers': {}, 'completed_at': now.isoformat()} for key in STEP_KEYS},
        started_at=now,
        finished_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [
        ('onboarding', '0001_initial'),
        ('users', '0001_initial'),
    ]
    operations = [migrations.RunPython(skip_existing, migrations.RunPython.noop)]
