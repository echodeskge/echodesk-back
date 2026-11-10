# Calls integration schema change — the ONE migration for the whole effort.
#
# Order matters:
#   1. AlterField x5 switch the secret columns to EncryptedCharField. From this
#      point the ORM reads the plaintext rows as '' (InvalidToken -> ''), so
#   2. RunPython(encrypt_existing) re-writes them as Fernet tokens *in the same
#      transaction*. Idempotent (already-encrypted values are skipped);
#      reverse decrypts so a rollback to 0017 restores plaintext.
#   3. api_token is added nullable, backfilled with secrets.token_urlsafe(32),
#      then tightened to unique/NOT NULL.
#   4. Users who own an active UserPhoneAssignment get can_make_calls=True so
#      enforcing the make_calls permission locks nobody out.
import secrets

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

import crm.fields
from crm.secret_migration import decrypt_existing, encrypt_existing


def backfill_api_token(apps, schema_editor):
    PbxServer = apps.get_model('crm', 'PbxServer')
    for pbx in PbxServer.objects.filter(api_token__isnull=True) | PbxServer.objects.filter(api_token=''):
        pbx.api_token = secrets.token_urlsafe(32)
        pbx.save(update_fields=['api_token'])


def grant_make_calls_to_assigned_users(apps, schema_editor):
    # Raw SQL with unqualified names: tenant-schemas sets search_path, so this
    # updates the current schema's users only.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('SELECT to_regclass(%s) IS NOT NULL', ['crm_userphoneassignment'])
        if not cursor.fetchone()[0]:
            return
        cursor.execute(
            'UPDATE users_user SET can_make_calls = TRUE '
            'WHERE can_make_calls = FALSE AND id IN ('
            '  SELECT user_id FROM crm_userphoneassignment WHERE is_active = TRUE'
            ')'
        )


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('crm', '0017_pbxserver'),
    ]

    operations = [
        # --- secrets at rest -------------------------------------------------
        migrations.AlterField(
            model_name='sipconfiguration',
            name='password',
            field=crm.fields.EncryptedCharField(help_text='SIP password (encrypted at rest)'),
        ),
        migrations.AlterField(
            model_name='sipconfiguration',
            name='turn_password',
            field=crm.fields.EncryptedCharField(blank=True, default='', help_text='Static TURN credential (encrypted at rest)'),
        ),
        migrations.AlterField(
            model_name='userphoneassignment',
            name='extension_password',
            field=crm.fields.EncryptedCharField(help_text='PBX extension password (encrypted at rest)'),
        ),
        migrations.AlterField(
            model_name='trunk',
            name='password',
            field=crm.fields.EncryptedCharField(help_text='SIP password (encrypted at rest)'),
        ),
        migrations.AlterField(
            model_name='pbxsettings',
            name='sms_api_key',
            field=crm.fields.EncryptedCharField(blank=True, default='', help_text='sender.ge API key (encrypted at rest)'),
        ),
        migrations.RunPython(encrypt_existing, decrypt_existing),

        # --- new fields --------------------------------------------------------
        migrations.AddField(
            model_name='sipconfiguration',
            name='turn_auth_secret',
            field=crm.fields.EncryptedCharField(blank=True, default='', help_text='coturn static-auth-secret; when set the softphone gets ephemeral TURN credentials instead of turn_password'),
        ),
        migrations.AlterField(
            model_name='sipconfiguration',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_sip_configs', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='calllog',
            name='caller_number',
            field=models.CharField(max_length=64),
        ),
        migrations.AlterField(
            model_name='calllog',
            name='recipient_number',
            field=models.CharField(max_length=64),
        ),
        migrations.AddField(
            model_name='calllog',
            name='pbx_call_id',
            field=models.CharField(blank=True, db_index=True, default='', help_text='Asterisk LINKEDID shared by every leg of one call (from the X-EchoDesk-Call header)', max_length=64),
        ),

        # --- PbxServer.api_token: add nullable -> backfill -> tighten ---------
        migrations.AddField(
            model_name='pbxserver',
            name='api_token',
            field=models.CharField(max_length=64, null=True, blank=True),
        ),
        migrations.RunPython(backfill_api_token, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='pbxserver',
            name='api_token',
            field=models.CharField(db_index=True, help_text='Long-lived token the PBX sends as X-PBX-Token on webhooks + AGI callbacks. Separate from the 24h enrollment_token.', max_length=64, unique=True),
        ),

        # --- make_calls backfill ------------------------------------------------
        migrations.RunPython(grant_make_calls_to_assigned_users, migrations.RunPython.noop),
    ]
