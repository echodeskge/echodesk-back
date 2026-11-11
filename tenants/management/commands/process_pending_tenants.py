"""
Management command to process pending tenant deployments

This command finds tenants with deployment_status='deploying' and completes their setup:
1. Creates the PostgreSQL schema
2. Runs migrations
3. Creates admin user
4. Sets up frontend access
5. Sends welcome email
6. Updates deployment_status to 'deployed'

Usage:
    python manage.py process_pending_tenants

Can be run as a cron job or triggered after webhook returns success
"""

from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.db import connection, transaction
from django.contrib.auth import get_user_model
from tenant_schemas.utils import schema_context
from tenants.models import Tenant, PendingRegistration
from tenants.services import SingleFrontendDeploymentService
from tenants.email_service import email_service
from tenants.subscription_service import SubscriptionService
import logging

User = get_user_model()
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Process pending tenant deployments (create schemas, run migrations, setup users)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--schema-name',
            type=str,
            help='Process only a specific tenant schema',
        )

    def handle(self, *args, **options):
        schema_name = options.get('schema_name')

        if schema_name:
            # Process specific tenant
            try:
                tenant = Tenant.objects.get(schema_name=schema_name, deployment_status__in=['deploying', 'failed'])
                self.process_tenant(tenant)
            except Tenant.DoesNotExist:
                self.stdout.write(self.style.ERROR(
                    f'Tenant {schema_name} not found or not in deploying status'
                ))
        else:
            # Process all pending tenants (including previously failed ones)
            pending_tenants = Tenant.objects.filter(deployment_status__in=['deploying', 'failed'], is_active=True)

            if not pending_tenants.exists():
                self.stdout.write(self.style.SUCCESS('No pending tenants to process'))
                return

            self.stdout.write(f'Found {pending_tenants.count()} pending tenant(s) to process')

            for tenant in pending_tenants:
                self.process_tenant(tenant)

    def process_tenant(self, tenant):
        """Process a single tenant: create schema, run migrations, setup user"""
        self.stdout.write(f'Processing tenant: {tenant.schema_name} (status: {tenant.deployment_status})')

        # Reset failed status to deploying for retry
        if tenant.deployment_status == 'failed':
            tenant.deployment_status = 'deploying'
            tenant.save(update_fields=['deployment_status'])

        try:
            # Step 1: Create schema
            self.stdout.write(f'  Creating schema...')
            self.create_schema(tenant)

            # Step 2: Run migrations
            self.stdout.write(f'  Running migrations...')
            self.run_migrations(tenant)

            # Step 3: Create admin user from PendingRegistration
            self.stdout.write(f'  Creating admin user...')
            self.create_admin_user(tenant)

            # Step 4: Sync features and permissions
            self.stdout.write(f'  Syncing features...')
            self.sync_features(tenant)

            # Step 5: Setup frontend
            self.stdout.write(f'  Setting up frontend...')
            self.setup_frontend(tenant)

            # Step 6: Send welcome email
            self.stdout.write(f'  Sending welcome email...')
            self.send_welcome_email(tenant)

            # Step 7: Update status
            tenant.deployment_status = 'deployed'
            tenant.save()

            self.stdout.write(self.style.SUCCESS(
                f'✓ Tenant {tenant.schema_name} processed successfully'
            ))

        except Exception as e:
            logger.error(f'Error processing tenant {tenant.schema_name}: {e}', exc_info=True)
            tenant.deployment_status = 'failed'
            tenant.save()
            self.stdout.write(self.style.ERROR(
                f'✗ Failed to process tenant {tenant.schema_name}: {str(e)}'
            ))

    def create_schema(self, tenant):
        """Create the PostgreSQL schema for the tenant"""
        with connection.cursor() as cursor:
            # Check if schema already exists
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s",
                [tenant.schema_name]
            )
            if cursor.fetchone():
                self.stdout.write(f'    Schema {tenant.schema_name} already exists, skipping creation')
                return

            # Create schema
            cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{tenant.schema_name}"')
            self.stdout.write(f'    Created schema: {tenant.schema_name}')

    def run_migrations(self, tenant):
        """Run Django migrations for the tenant schema"""
        # tenant_schemas' introspection lacks get_sequences(), which Django 4.2's
        # migration framework calls when a migration alters a primary-key column
        # type (e.g. token_blacklist 0008 AutoField -> BigAutoField). A brand new
        # schema runs every migration from scratch and hit NotImplementedError
        # there, leaving the tenant 'failed'. Borrow the stock PostgreSQL
        # implementation (same patch as the test suite / seed_demo_tenant).
        from django.db.backends.postgresql.introspection import DatabaseIntrospection as _PGIntrospection
        from tenant_schemas.postgresql_backend.introspection import DatabaseSchemaIntrospection
        if 'get_sequences' not in DatabaseSchemaIntrospection.__dict__:
            DatabaseSchemaIntrospection.get_sequences = _PGIntrospection.get_sequences

        call_command(
            'migrate_schemas',
            schema_name=tenant.schema_name,
            verbosity=1,
            interactive=False
        )
        self.stdout.write(f'    Migrations completed for {tenant.schema_name}')

    def create_admin_user(self, tenant):
        """Create admin user from PendingRegistration data"""
        try:
            # Find the pending registration for this tenant
            pending_reg = PendingRegistration.objects.filter(
                schema_name=tenant.schema_name,
                is_processed=True
            ).order_by('-created_at').first()

            if not pending_reg:
                self.stdout.write(self.style.WARNING(
                    f'    No PendingRegistration found for {tenant.schema_name}, skipping user creation'
                ))
                return

            # Create user in tenant schema
            with schema_context(tenant.schema_name):
                from django.db import IntegrityError
                try:
                    with transaction.atomic():
                        admin_user = User.objects.create(
                            email=pending_reg.admin_email,
                            first_name=pending_reg.admin_first_name,
                            last_name=pending_reg.admin_last_name,
                            is_staff=True,
                            is_superuser=True,
                            is_active=True
                        )
                        # Set the already-hashed password
                        admin_user.password = pending_reg.admin_password
                        admin_user.save()
                        self.stdout.write(f'    Created admin user: {admin_user.email}')
                except IntegrityError:
                    # User already exists
                    self.stdout.write(f'    Admin user {pending_reg.admin_email} already exists')

        except Exception as e:
            logger.error(f'Error creating admin user for {tenant.schema_name}: {e}')
            raise

    def sync_features(self, tenant):
        """Sync features and permissions for the tenant's subscription"""
        try:
            subscription = tenant.subscription
            result = SubscriptionService.sync_tenant_features(subscription)
            self.stdout.write(
                f'    Synced features: {len(result["enabled_features"])} enabled, '
                f'{len(result["disabled_features"])} disabled'
            )
        except Exception as e:
            logger.warning(f'Could not sync features for {tenant.schema_name}: {e}')
            # Don't fail deployment if feature sync fails

    def setup_frontend(self, tenant):
        """Setup frontend access for the tenant"""
        try:
            deployment_service = SingleFrontendDeploymentService()
            deployment_service.setup_tenant_frontend(tenant)
            self.stdout.write(f'    Frontend URL: {tenant.frontend_url}')
        except Exception as e:
            logger.error(f'Error setting up frontend for {tenant.schema_name}: {e}')
            raise

    def send_welcome_email(self, tenant):
        """Send welcome email to tenant admin"""
        try:
            # Get pending registration for email details
            pending_reg = PendingRegistration.objects.filter(
                schema_name=tenant.schema_name,
                is_processed=True
            ).order_by('-created_at').first()

            if not pending_reg:
                self.stdout.write(self.style.WARNING('    No registration data for welcome email'))
                return

            frontend_url = tenant.frontend_url or f"https://{tenant.schema_name}.echodesk.ge"
            email_sent = email_service.send_tenant_created_email(
                tenant_email=pending_reg.admin_email,
                tenant_name=tenant.name,
                admin_name=f"{pending_reg.admin_first_name} {pending_reg.admin_last_name}",
                frontend_url=frontend_url,
                schema_name=tenant.schema_name
            )

            if email_sent:
                self.stdout.write(f'    Welcome email sent to {pending_reg.admin_email}')
            else:
                self.stdout.write(self.style.WARNING('    Failed to send welcome email'))

        except Exception as e:
            logger.error(f'Error sending welcome email for {tenant.schema_name}: {e}')
            # Don't fail deployment if email fails
