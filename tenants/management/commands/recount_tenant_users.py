"""
Recalculate TenantSubscription.current_users from each tenant's active users.

    python manage.py recount_tenant_users --schema amanati
    python manage.py recount_tenant_users --all [--dry-run]
"""
from django.core.management.base import BaseCommand, CommandError
from tenant_schemas.utils import get_public_schema_name

from tenants.models import Tenant
from tenants.user_count import sync_subscription_user_count


class Command(BaseCommand):
    help = "Recount billable (active) users into each tenant subscription's current_users"

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument('--schema', help='Schema name of a single tenant')
        target.add_argument('--all', action='store_true', help='Recount every tenant')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would change without writing anything',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if options['schema']:
            tenants = tenants.filter(schema_name=options['schema'])
            if not tenants.exists():
                raise CommandError(f"Tenant with schema '{options['schema']}' not found")

        changed = unchanged = skipped = failed = 0
        for tenant in tenants.order_by('schema_name'):
            try:
                result = sync_subscription_user_count(tenant, dry_run=dry_run)
            except Exception as exc:
                failed += 1
                self.stderr.write(f'{tenant.schema_name}: ERROR {exc}')
                continue
            if result is None:
                skipped += 1
                self.stdout.write(f'{tenant.schema_name}: no subscription, skipped')
                continue
            old, new = result
            if old == new:
                unchanged += 1
                self.stdout.write(f'{tenant.schema_name}: {new} (unchanged)')
            else:
                changed += 1
                verb = 'would update' if dry_run else 'updated'
                self.stdout.write(f'{tenant.schema_name}: {old} -> {new} ({verb})')

        prefix = '[DRY RUN] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}{changed} changed, {unchanged} unchanged, '
            f'{skipped} without subscription, {failed} failed'
        ))
