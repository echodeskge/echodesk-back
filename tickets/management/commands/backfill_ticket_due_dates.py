"""
Copy the legacy ``payment_due_date`` into the new task ``due_date``.

Before the tickets redesign the detail page's "Due date" row wrote
``payment_due_date`` for every ticket. ``due_date`` (migration 0041) is now the
task due date and ``payment_due_date`` keeps its payment meaning, so for
tickets that are *not* orders the old value is copied over once:

    python manage.py backfill_ticket_due_dates --schema amanati --dry-run
    python manage.py backfill_ticket_due_dates --all --dry-run
    python manage.py backfill_ticket_due_dates --all

Only rows with ``is_order=False``, a ``payment_due_date`` and no ``due_date``
are touched; ``payment_due_date`` itself is left as it is. It runs as a single
queryset ``UPDATE`` per schema, so no model signal (history, broadcasts,
notifications) fires. Re-running it is a no-op.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from tenant_schemas.utils import get_public_schema_name, schema_context

from tenants.models import Tenant


def backfill_queryset():
    from tickets.models import Ticket
    return Ticket.objects.filter(
        is_order=False,
        due_date__isnull=True,
        payment_due_date__isnull=False,
    )


class Command(BaseCommand):
    help = "Copy payment_due_date into due_date for non-order tickets whose due_date is empty"

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument('--schema', help='Schema name of a single tenant')
        target.add_argument('--all', action='store_true', help='Every tenant schema')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Only count the tickets that would be updated; write nothing',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if options['schema']:
            tenants = tenants.filter(schema_name=options['schema'])
            if not tenants.exists():
                raise CommandError(f"Tenant with schema '{options['schema']}' not found")

        total = changed_schemas = failed = 0
        for schema in tenants.order_by('schema_name').values_list('schema_name', flat=True):
            try:
                with schema_context(schema), transaction.atomic():
                    qs = backfill_queryset()
                    if dry_run:
                        count = qs.count()
                    else:
                        count = qs.update(due_date=F('payment_due_date'))
            except Exception as exc:  # noqa: BLE001 - reported per schema, the rest still run
                failed += 1
                self.stderr.write(f'{schema}: ERROR {type(exc).__name__}: {exc}')
                continue
            total += count
            if count:
                changed_schemas += 1
            verb = 'would update' if dry_run else 'updated'
            self.stdout.write(f'{schema}: {count} ticket(s) {verb}')

        prefix = '[DRY RUN] ' if dry_run else ''
        verb = 'would be updated' if dry_run else 'updated'
        summary = f'{prefix}{total} ticket(s) {verb} in {changed_schemas} schema(s); {failed} schema(s) failed'
        if failed:
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))
