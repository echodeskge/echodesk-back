"""
Create (or top up) a fully-featured demo tenant for local development.

    python manage.py seed_demo_tenant
    python manage.py seed_demo_tenant --reset          # drop + recreate the schema
    python manage.py seed_demo_tenant --schema demo2 --domain demo2.api.lvh.me

Logins (password for every user: Demo12345!):
    admin@demo.ge                     superuser + staff, role admin; every feature
    manager@demo.ge                   role manager; leave manager of agent + staff1-4; "Support line" queue
    agent@demo.ge                     role agent; booking staff; "Support line" queue
    staff1@demo.ge .. staff4@demo.ge  role agent; every feature
    staff5@demo.ge                    role manager; every feature
    staff6@demo.ge                    role viewer; ONLY tickets + leave (restricted group)

Local development only: it creates a superuser with a published password, so
it refuses to run unless settings.DEBUG is True or --allow-production is
passed explicitly.

The command is idempotent: re-running it never duplicates data (each module
is skipped once its rows exist; a module that failed is rolled back and
retried on the next run). It never calls an external service; social, e-mail
and PBX connections are DB rows with fake tokens, and Asterisk realtime sync
is switched off while seeding.

The store settings (EcommerceSettings: store info, cash on delivery + pickup,
18 % VAT, storefront look) are their own module, so an older demo tenant is
topped up on the next run. No payment-gateway credential is ever written.
"""
import time as _time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone
from tenant_schemas.postgresql_backend.base import _is_valid_schema_name
from tenant_schemas.utils import get_public_schema_name, schema_context, schema_exists

from tenants.demo_seed import DEMO_LOGINS, DEMO_PASSWORD, DemoSeeder, DemoSeedError, demo_counts, demo_email

DEMO_ADMIN_EMAIL = demo_email('admin')
# Feature keys checked by the backend permission classes and the web/mobile
# navigation (tenants/permissions.py, */permissions.py, navigationConfig.ts).
BASELINE_FEATURES = [
    ('ticket_management', 'Ticket Management', 'core'),
    ('user_management', 'User Management', 'core'),
    ('settings', 'Settings', 'core'),
    ('advanced_analytics', 'Advanced Analytics', 'analytics'),
    ('ecommerce_crm', 'E-commerce CRM', 'core'),
    ('order_management', 'Order Management', 'core'),
    ('booking_management', 'Booking Management', 'core'),
    ('leave_management', 'Leave Management', 'core'),
    ('invoice_management', 'Invoice Management', 'core'),
    ('social_integrations', 'Social Media Integrations', 'integration'),
    ('ip_calling', 'IP Calling', 'communication'),
    ('sip_calling', 'SIP Calling', 'communication'),
    ('ai_companion', 'AI Companion', 'integration'),
]
DEMO_DESCRIPTION = 'EchoDesk demo tenant (created by seed_demo_tenant). Safe to reset.'
DEFAULT_NAME = 'EchoDesk Demo'
# Schemas that must never be (re)created, seeded or dropped by this command,
# on top of tenant_schemas' own rule (identifier, <= 63 chars, no pg_ prefix).
RESERVED_SCHEMAS = {'public', 'information_schema'}


def validate_schema_name(raw):
    """Return the normalised schema name or raise CommandError."""
    schema = (raw or '').strip().lower()
    if (not _is_valid_schema_name(schema) or schema in RESERVED_SCHEMAS
            or schema == get_public_schema_name().lower()):
        raise CommandError(
            f'Invalid schema name "{schema}": use letters, digits and _ (max 63 chars, '
            'not starting with pg_), and not public / information_schema')
    return schema


def _logins_text():
    return '; '.join(f'{login} ({desc})' for login, desc in DEMO_LOGINS)


def migrate_tenant_schema(tenant):
    """Create the schema if needed and apply every pending tenant migration.

    Uses Django's MigrationExecutor inside the tenant schema (same approach as
    the test-suite tenant fixture) instead of tenant_schemas' migrate_schemas,
    so it works the same locally, in CI and in tests.
    """
    from django.db.backends.postgresql.introspection import DatabaseIntrospection as _PGIntrospection
    from django.core.management.sql import emit_post_migrate_signal
    from django.db.migrations.executor import MigrationExecutor
    from tenant_schemas.postgresql_backend.introspection import DatabaseSchemaIntrospection

    # tenant_schemas' introspection lacks get_sequences(), which the
    # migration framework needs; borrow the stock PostgreSQL implementation.
    if 'get_sequences' not in DatabaseSchemaIntrospection.__dict__:
        DatabaseSchemaIntrospection.get_sequences = _PGIntrospection.get_sequences

    if not schema_exists(tenant.schema_name):
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{tenant.schema_name}"')
    with schema_context(tenant.schema_name):
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        if executor.migration_plan(targets):
            executor.migrate(targets)
        # Like ``migrate``: create the schema's content types + permissions
        # (feature permission sync and group permissions need them).
        emit_post_migrate_signal(verbosity=0, interactive=False, db=connection.alias)


class Command(BaseCommand):
    help = (
        'LOCAL DEVELOPMENT ONLY (refuses unless DEBUG=True or --allow-production). '
        'Create or top up the demo tenant (schema "demo", domain demo.api.lvh.me) with every feature '
        'enabled and realistic data in every module, including PBX trunk/queue/inbound routes. '
        f'Logins, password for all {DEMO_PASSWORD}: {_logins_text()}. '
        'Idempotent; --reset drops and recreates the schema.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--schema', default='demo', help='Tenant schema name (default: demo)')
        parser.add_argument('--domain', default=None,
                            help='Tenant domain_url (default: <schema>.api.lvh.me)')
        parser.add_argument('--name', default=None, help=f'Tenant display name (default: {DEFAULT_NAME})')
        parser.add_argument('--reset', action='store_true',
                            help='Drop the demo schema (only a tenant created by this command) and reseed it')
        parser.add_argument('--allow-production', action='store_true',
                            help='Run even though settings.DEBUG is False. DANGEROUS: creates a superuser '
                                 f'admin@demo.ge with the published password {DEMO_PASSWORD}.')

    def handle(self, *args, **options):
        started = _time.monotonic()
        self._check_environment(options['allow_production'])
        schema = validate_schema_name(options['schema'])
        domain = options['domain'] or f'{schema}.api.lvh.me'

        with schema_context(get_public_schema_name()):
            tenant = self._get_or_create_tenant(schema, domain, options['domain'], options['name'])
            if options['reset']:
                self._reset(tenant)
            migrate_tenant_schema(tenant)
            self._ensure_subscription(tenant)

        self.stdout.write(f'Seeding tenant "{schema}" ...')
        with schema_context(schema):
            try:
                DemoSeeder(tenant, stdout=self.stdout).run()
            except DemoSeedError as exc:
                raise CommandError(str(exc)) from exc
            counts = demo_counts(tenant)

        self.stdout.write('')
        for key, value in counts.items():
            self.stdout.write(f'  {key:<26}{value}')
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Demo tenant ready in {_time.monotonic() - started:.1f}s: schema "{schema}", domain {domain}'))
        self.stdout.write(f'  Logins (password for all: {DEMO_PASSWORD})')
        for login, desc in DEMO_LOGINS:
            self.stdout.write(f'    {login:<34} {desc}')

    # ------------------------------------------------------------ environment
    def _check_environment(self, allow_production):
        if settings.DEBUG:
            return
        if not allow_production:
            raise CommandError(
                'seed_demo_tenant is for local development: settings.DEBUG is False. It creates a '
                f'superuser {DEMO_ADMIN_EMAIL} with the published password {DEMO_PASSWORD}. '
                'Pass --allow-production only if you really mean to run it here.')
        banner = '!' * 78
        for line in (
            banner,
            '!! WARNING: seed_demo_tenant is running with DEBUG=False (--allow-production).',
            f'!! It creates the superuser {DEMO_ADMIN_EMAIL} with the PUBLISHED password {DEMO_PASSWORD},',
            '!! adds missing Feature rows to the shared public schema and may add help-center content.',
            '!! Delete the demo tenant (or change its passwords) when you are done.',
            banner,
        ):
            self.stderr.write(self.style.ERROR(line))

    # ----------------------------------------------------------------- tenant
    def _get_or_create_tenant(self, schema, domain, domain_option, name_option):
        from tenants.models import Tenant

        tenant = Tenant.objects.filter(schema_name=schema).first()
        if tenant is not None:
            if tenant.admin_email != DEMO_ADMIN_EMAIL:
                raise CommandError(
                    f'Tenant "{schema}" exists and was not created by seed_demo_tenant '
                    f'(admin_email={tenant.admin_email}); refusing to seed demo data into it.')
            # The tenant row is never rewritten; say so instead of silently ignoring flags.
            if domain_option and domain_option != tenant.domain_url:
                self.stderr.write(self.style.WARNING(
                    f'--domain {domain_option} ignored: tenant "{schema}" already exists with domain '
                    f'{tenant.domain_url} (change it in the admin, or delete the tenant to recreate it).'))
            if name_option and name_option != tenant.name:
                self.stderr.write(self.style.WARNING(
                    f'--name "{name_option}" ignored: tenant "{schema}" already exists with name '
                    f'"{tenant.name}".'))
            return tenant
        if schema_exists(schema):
            # A schema without a tenant row is someone else's data (or a
            # leftover); never adopt it, since --reset would drop it.
            raise CommandError(
                f'Schema "{schema}" already exists but has no tenant row; refusing to seed or reset it.')
        if Tenant.objects.filter(domain_url=domain).exists():
            raise CommandError(f'Domain "{domain}" is already used by another tenant')
        name = name_option or DEFAULT_NAME
        tenant = Tenant(
            schema_name=schema, domain_url=domain, name=name, description=DEMO_DESCRIPTION,
            admin_email=DEMO_ADMIN_EMAIL, admin_name='Nino Beridze', plan='enterprise',
            preferred_language='en', max_users=50, deployment_status='deployed',
            frontend_url=f'http://{schema}.lvh.me:3000',
        )
        tenant.save(verbosity=0)
        self.stdout.write(f'Created tenant "{schema}" ({domain})')
        return tenant

    def _ensure_subscription(self, tenant):
        from tenants.feature_models import Feature
        from tenants.models import TenantSubscription
        from tenants.subscription_service import SubscriptionService

        # A fresh local DB only has the Feature rows that data migrations
        # create; add any missing key the API / dashboard gates on so every
        # module is reachable. Existing rows are never modified.
        for order, (key, name, category) in enumerate(BASELINE_FEATURES):
            Feature.objects.get_or_create(key=key, defaults={
                'name': name, 'category': category, 'sort_order': 100 + order, 'is_active': True})

        far = timezone.now() + timedelta(days=3650)
        sub = TenantSubscription.objects.filter(tenant=tenant).first()
        if sub is None:
            sub = TenantSubscription.objects.create(
                tenant=tenant, is_active=True, starts_at=timezone.now(), expires_at=far,
                next_billing_date=far, agent_count=50, payment_status='current', subscription_type='paid')
        elif not sub.is_active:
            sub.is_active = True
            sub.save(update_fields=['is_active'])
        sub.selected_features.set(Feature.objects.filter(is_active=True))
        SubscriptionService.sync_tenant_features(sub)

    def _reset(self, tenant):
        from tenants.models import SocialPlatformRoute
        from widget_registry.models import WidgetConnection

        if tenant.admin_email != DEMO_ADMIN_EMAIL:
            raise CommandError('Refusing to reset a tenant that was not created by seed_demo_tenant')
        self.stdout.write(self.style.WARNING(f'Dropping schema "{tenant.schema_name}" ...'))
        with connection.cursor() as cursor:
            if connection.in_atomic_block:
                # Inside an outer transaction (tests): flush deferred FK checks
                # first, Postgres refuses to drop tables with pending triggers.
                cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
            cursor.execute(f'DROP SCHEMA IF EXISTS "{tenant.schema_name}" CASCADE')
        SocialPlatformRoute.objects.filter(tenant_schema=tenant.schema_name).delete()
        WidgetConnection.objects.filter(tenant_schema=tenant.schema_name).delete()
