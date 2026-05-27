from django.db import migrations


def deactivate_messages_beta(apps, schema_editor):
    """Retire the standalone `messages_beta` feature.

    The socket-based inbox is now the default Social Messages experience, gated
    by the existing `social_integrations` feature — there's no longer a separate
    beta feature. Deactivating (rather than deleting) drops it from
    get_feature_keys() (which filters is_active=True) so it stops gating and
    disappears from active feature/billing lists, while preserving the row and
    any group/subscription assignments. Idempotent + safe where the row is
    absent (the feature was admin-created, not seeded by a migration).
    """
    Feature = apps.get_model("tenants", "Feature")
    Feature.objects.filter(key="messages_beta").update(is_active=False)


def reactivate_messages_beta(apps, schema_editor):
    Feature = apps.get_model("tenants", "Feature")
    Feature.objects.filter(key="messages_beta").update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0051_socialplatformroute"),
    ]

    operations = [
        migrations.RunPython(deactivate_messages_beta, reactivate_messages_beta),
    ]
