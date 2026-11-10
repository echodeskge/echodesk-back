"""Management command: prove every stored PBX secret still decrypts.

Run right after a deploy that ships ``crm/migrations/0018_calls_integration``
(or after a FERNET_KEY rotation)::

    python manage.py verify_pbx_secrets            # every tenant schema
    python manage.py verify_pbx_secrets demo acme  # only these schemas

For each schema and each encrypted column it counts the non-empty raw values
that ``EncryptedCharField`` would read back as ``''`` (``InvalidToken``). Such
rows are secrets the application has effectively lost: the user sees an empty
password and the softphone cannot register. Exit status is non-zero when any
are found so the deploy pipeline can gate on it. Values are never printed.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from tenant_schemas.utils import get_public_schema_name, schema_context

from crm.secret_migration import SECRET_COLUMNS, count_undecryptable


class Command(BaseCommand):
    help = "Count PBX secrets that no longer decrypt (per tenant, per table)."

    def add_arguments(self, parser):
        parser.add_argument(
            'schemas', nargs='*',
            help='Tenant schema names to check (default: every tenant).',
        )

    def handle(self, *args, **options):
        from tenants.models import Tenant

        public = get_public_schema_name()
        schemas = options['schemas'] or list(
            Tenant.objects.exclude(schema_name=public)
            .order_by('schema_name')
            .values_list('schema_name', flat=True)
        )

        grand_total = 0
        grand_bad = 0
        for schema in schemas:
            with schema_context(schema):
                for table, columns in SECRET_COLUMNS.items():
                    for column in columns:
                        stats = count_undecryptable(connection, table, column)
                        grand_total += stats['total']
                        grand_bad += stats['undecryptable']
                        line = (
                            f"{schema:<24} {table + '.' + column:<44} "
                            f"non_empty={stats['total']:<5} undecryptable={stats['undecryptable']}"
                        )
                        if stats['undecryptable']:
                            self.stdout.write(self.style.ERROR(line))
                        else:
                            self.stdout.write(line)

        summary = (
            f"{len(schemas)} schema(s), {grand_total} non-empty secret value(s), "
            f"{grand_bad} undecryptable"
        )
        if grand_bad:
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))
