from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from tenants.models import PendingRegistration, Tenant


class Command(BaseCommand):
    help = 'Delete a specific tenant by schema name'

    def add_arguments(self, parser):
        parser.add_argument(
            'schema_name',
            type=str,
            help='Schema name of the tenant to delete'
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Confirm that you want to delete this tenant',
        )

    def handle(self, *args, **options):
        schema_name = options['schema_name']

        # Prevent deletion of protected schemas
        protected_schemas = ['public', 'amanati']
        if schema_name in protected_schemas:
            self.stdout.write(
                self.style.ERROR(
                    f'Cannot delete protected schema: {schema_name}\n'
                    f'Protected schemas: {", ".join(protected_schemas)}'
                )
            )
            return

        # Check if tenant exists
        try:
            tenant = Tenant.objects.get(schema_name=schema_name)
        except Tenant.DoesNotExist:
            self.stdout.write(
                self.style.ERROR(
                    f'Tenant with schema "{schema_name}" not found.\n'
                    f'Use "python manage.py list_tenants" to see available tenants.'
                )
            )
            return

        # Show tenant details
        self.stdout.write(self.style.WARNING('Tenant details:'))
        self.stdout.write(f'  Schema name: {tenant.schema_name}')
        self.stdout.write(f'  Name: {tenant.name}')
        self.stdout.write(f'  Domain: {tenant.domain_url}')
        self.stdout.write(f'  Admin email: {tenant.admin_email}')
        self.stdout.write(f'  Created: {tenant.created_on}')
        self.stdout.write(f'  Active: {tenant.is_active}')

        # Check for subscription
        if hasattr(tenant, 'subscription'):
            subscription = tenant.subscription
            self.stdout.write(f'  Has subscription: Yes')
            self.stdout.write(
                f'  Plan: {subscription.agent_count} users, '
                f'{subscription.selected_features.count()} features, '
                f'{subscription.monthly_cost} GEL/month'
                f'{" (trial)" if subscription.is_trial else ""}'
            )

        if not options['confirm']:
            self.stdout.write(
                self.style.WARNING(
                    f'\nThis will permanently delete tenant "{schema_name}" and ALL its data.\n'
                    f'This action cannot be undone!\n\n'
                    f'Run with --confirm to proceed: python manage.py delete_specific_tenant {schema_name} --confirm'
                )
            )
            return

        # Perform deletion
        self.stdout.write(
            self.style.WARNING(f'\nDeleting tenant: {tenant.schema_name}...')
        )

        try:
            # Drop the schema with CASCADE (removes all tenant data)
            with connection.cursor() as cursor:
                self.stdout.write(f'  Dropping schema "{tenant.schema_name}" with CASCADE...')
                cursor.execute(f'DROP SCHEMA IF EXISTS "{tenant.schema_name}" CASCADE')
                self.stdout.write(self.style.SUCCESS(f'  Schema dropped successfully'))

            # Delete the public-schema rows that point at the tenant
            # (subscription, orders, invoices, saved cards, events, domains,
            # feature/permission rows, ...) through the ORM, model by model,
            # then the tenant row itself with raw SQL. tenant.delete() cannot
            # be used: models in TENANT_APPS (bookings, leave, e-commerce
            # settings) also have FKs to Tenant and their tables live in the
            # schema just dropped, and the FK constraints have no ON DELETE
            # CASCADE, so a bare DELETE on tenants_tenant is rejected.
            self.stdout.write(f'  Deleting public-schema records of the tenant...')
            shared_app_names = set(settings.SHARED_APPS)
            with transaction.atomic():
                for rel in Tenant._meta.related_objects:
                    model = rel.related_model
                    if apps.get_app_config(model._meta.app_label).name not in shared_app_names:
                        continue
                    deleted, _ = model._default_manager.filter(**{rel.field.name: tenant}).delete()
                    if deleted:
                        self.stdout.write(f'    {model._meta.label}: {deleted} row(s)')
                # Registration bookkeeping is keyed by schema name, not FK;
                # register_tenant refuses a domain with a pending row.
                pending, _ = PendingRegistration.objects.filter(schema_name=tenant.schema_name).delete()
                if pending:
                    self.stdout.write(f'    tenants.PendingRegistration: {pending} row(s)')
                with connection.cursor() as cursor:
                    cursor.execute(
                        'DELETE FROM tenants_tenant WHERE id = %s',
                        [tenant.id]
                    )
            self.stdout.write(self.style.SUCCESS(f'  Tenant record deleted'))

            self.stdout.write(
                self.style.SUCCESS(
                    f'\nSuccessfully deleted tenant: {schema_name}\n'
                    f'All data associated with this tenant has been permanently removed.'
                )
            )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(
                    f'\nError deleting tenant: {str(e)}\n'
                    f'The tenant may be partially deleted. Please check the database manually.'
                )
            )
            raise
