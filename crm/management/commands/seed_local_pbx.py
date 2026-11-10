"""Point a tenant at the local Asterisk harness (DEBUG only).

The calls E2E harness (``calls-e2e/asterisk/``, outside the repo) runs one
Asterisk in Docker with three static WebRTC extensions 101/102/103, a
``support`` ringall queue, AMI on 127.0.0.1:5038 and recordings served at
``http://localhost:8088/static/recordings/``. This command writes the
matching product rows into a tenant schema so the softphones, the call
views and the PBX webhooks all resolve to that container:

* ``SipConfiguration`` "Local Asterisk": ``ws://localhost:8088/ws``, no
  STUN, the tenant's default;
* ``UserPhoneAssignment`` 101/102/103 for admin@demo.ge, agent@demo.ge and
  staff1@demo.ge (primary, ``can_make_calls`` switched on);
* ``PbxServer`` "Local Asterisk": active, AMI echodesk/echodesk, with the
  ``api_token`` the harness must send as ``X-PBX-Token``.

The extension password must match ``conf/pjsip.conf`` in the harness:
``--ext-password``, else ``LOCAL_PBX_EXT_PASSWORD``, else ``echodesk-local``.

Idempotent: re-running updates the same rows and keeps the ``api_token``.
Refuses to run unless ``settings.DEBUG`` is on, so it can never touch a
production tenant. Human-readable progress goes to stderr; **stdout is the
``api_token`` only**, so the operator can redirect it straight into the
harness ``.env``::

    python manage.py seed_local_pbx --schema demo
"""

import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings
from tenant_schemas.utils import get_tenant_model, schema_context

from crm.models import PbxServer, SipConfiguration, UserPhoneAssignment

NAME = 'Local Asterisk'
DEFAULT_EXT_PASSWORD = 'echodesk-local'
EXT_PASSWORD_ENV = 'LOCAL_PBX_EXT_PASSWORD'

# (extension, user email, caller-id display name) - same as conf/pjsip.conf.
EXTENSIONS = (
    ('101', 'admin@demo.ge', 'Admin'),
    ('102', 'agent@demo.ge', 'Agent'),
    ('103', 'staff1@demo.ge', 'Staff 1'),
)

SIP_CONFIG = {
    'sip_server': 'localhost',
    'sip_port': 8088,
    'username': '',
    'password': '',
    'realm': 'localhost',
    'proxy': '',
    'phone_number': '',
    'stun_server': '',
    'websocket_path': '/ws',
    'turn_server': '',
    'turn_username': '',
    'turn_password': '',
    'turn_auth_secret': '',
    'is_active': True,
    'is_default': True,
}

PBX_SERVER = {
    'fqdn': 'localhost',
    'public_ip': None,
    # No realtime DB in the harness (pjsip/queues are static files); these
    # point at the local Postgres so the row is complete and the dynamic DB
    # alias resolves if the sync layer is ever switched on.
    'realtime_db_host': '127.0.0.1',
    'realtime_db_port': 55432,
    'realtime_db_user': 'postgres',
    'realtime_db_password': '',
    'realtime_db_sslmode': 'disable',
    'ami_host': '127.0.0.1',
    'ami_port': 5038,
    'ami_username': 'echodesk',
    'ami_password': 'echodesk',
    'wss_url': 'ws://localhost:8088/ws',
    'recording_base_url': 'http://localhost:8088/static/recordings/',
    'status': PbxServer.STATUS_ACTIVE,
    'use_tenant_prefix': False,
    'notes': 'Local calls E2E harness (calls-e2e/asterisk). Seeded by seed_local_pbx.',
}


class Command(BaseCommand):
    help = 'Seed a tenant with the local Asterisk harness (DEBUG only); prints the PbxServer api_token'

    def add_arguments(self, parser):
        parser.add_argument('--schema', default='demo', help='Tenant schema (default: demo)')
        parser.add_argument(
            '--ext-password', default=None,
            help=f'Extension password for 101-103 (default: ${EXT_PASSWORD_ENV} or "{DEFAULT_EXT_PASSWORD}"); '
                 'must match the harness conf/pjsip.conf',
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError('seed_local_pbx only runs with DEBUG=True (local development only)')

        schema = options['schema']
        ext_password = (
            options['ext_password']
            or os.environ.get(EXT_PASSWORD_ENV)
            or DEFAULT_EXT_PASSWORD
        )

        Tenant = get_tenant_model()
        if not Tenant.objects.filter(schema_name=schema).exists():
            raise CommandError(f"Tenant schema '{schema}' does not exist")

        # The harness Asterisk is static-config: nothing to write to a
        # realtime DB, so keep the sync signals quiet while seeding.
        with override_settings(ASTERISK_SYNC_ENABLED=False), schema_context(schema), transaction.atomic():
            api_token = self._seed(schema, ext_password)

        self.stdout.write(api_token)

    def _seed(self, schema, ext_password):
        User = get_user_model()
        users = {}
        for extension, email, _display in EXTENSIONS:
            try:
                users[extension] = User.objects.get(email=email)
            except User.DoesNotExist:
                raise CommandError(
                    f"User {email} not found in schema '{schema}' (run seed_demo_tenant first)"
                )

        admin = users[EXTENSIONS[0][0]]
        sip, created = SipConfiguration.objects.update_or_create(
            name=NAME, defaults={**SIP_CONFIG, 'created_by': admin},
        )
        self._note('SipConfiguration', f'{NAME} ({sip.sip_server}:{sip.sip_port}{sip.websocket_path})', created)

        for extension, email, display_name in EXTENSIONS:
            user = users[extension]
            assignment, created = UserPhoneAssignment.objects.update_or_create(
                sip_configuration=sip, extension=extension,
                defaults={
                    'user': user,
                    'extension_password': ext_password,
                    'phone_number': extension,
                    'display_name': display_name,
                    'is_primary': True,
                    'is_active': True,
                },
            )
            self._note('UserPhoneAssignment', f'{extension} -> {email}', created)
            if not user.can_make_calls:
                user.can_make_calls = True
                user.save(update_fields=['can_make_calls'])
                self.stderr.write(f'    can_make_calls=True for {email}')

        pbx, created = PbxServer.objects.update_or_create(name=NAME, defaults=PBX_SERVER)
        self._note('PbxServer', f'{NAME} (AMI {pbx.ami_host}:{pbx.ami_port}, {pbx.wss_url})', created)
        self.stderr.write('  api_token on stdout (put it in the harness .env as ECHODESK_PBX_TOKEN)')
        return pbx.api_token

    def _note(self, kind, ident, created):
        self.stderr.write(f'  {kind:<20} {ident:<48} [{"created" if created else "updated"}]')
