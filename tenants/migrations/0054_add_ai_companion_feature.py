from django.db import migrations


def create_ai_companion_feature(apps, schema_editor):
    """Create the AI Companion feature"""
    Feature = apps.get_model('tenants', 'Feature')

    Feature.objects.get_or_create(
        key='ai_companion',
        defaults={
            'name': 'AI Companion',
            'description': (
                'AI assistant for social chats: automatic customer replies '
                'guided by your instructions, human handoff, and on-demand '
                'conversation summaries'
            ),
            'category': 'integration',
            'price_per_user_gel': 15.00,
            'price_unlimited_gel': 150.00,
            'icon': '🤖',
            'sort_order': 35,
            'is_active': True,
        }
    )
    print("✅ AI Companion feature created successfully")


def remove_ai_companion_feature(apps, schema_editor):
    """Remove the AI Companion feature"""
    Feature = apps.get_model('tenants', 'Feature')
    Feature.objects.filter(key='ai_companion').delete()
    print("❌ AI Companion feature removed")


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0053_alter_socialplatformroute_platform'),
    ]

    operations = [
        migrations.RunPython(create_ai_companion_feature, remove_ai_companion_feature),
    ]
