"""Backfill the public-schema SocialPlatformRoute table from existing
per-tenant social connections.

Run once after deploying the routing change (and safe to re-run — it's
idempotent). Going forward, signals in ``social_integrations.platform_routing``
keep the table in sync, and the webhook resolver self-heals any missing rows.

    python manage.py backfill_platform_routes
    python manage.py backfill_platform_routes --dry-run
"""
from django.core.management.base import BaseCommand
from tenant_schemas.utils import schema_context

from social_integrations.platform_routing import PLATFORM_CONFIG, route_upsert


class Command(BaseCommand):
    help = "Backfill SocialPlatformRoute rows from existing tenant connections."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be written without touching the table.",
        )

    def handle(self, *args, **options):
        from social_integrations import models as social_models
        from tenants.models import Tenant

        dry_run = options["dry_run"]
        total = 0

        for tenant in Tenant.objects.exclude(schema_name="public"):
            for platform, (model_name, field) in PLATFORM_CONFIG.items():
                Model = getattr(social_models, model_name, None)
                if Model is None:
                    continue
                try:
                    with schema_context(tenant.schema_name):
                        rows = list(
                            Model.objects.values_list(field, "is_active")
                        )
                except Exception as exc:  # noqa: BLE001 — skip tenants w/o tables
                    self.stderr.write(
                        f"  ! {tenant.schema_name}/{platform}: skipped ({exc})"
                    )
                    continue

                for external_id, is_active in rows:
                    if not external_id:
                        continue
                    total += 1
                    if dry_run:
                        self.stdout.write(
                            f"  would map {platform}:{external_id} → "
                            f"{tenant.schema_name} (active={is_active})"
                        )
                    else:
                        route_upsert(
                            platform,
                            external_id,
                            tenant.schema_name,
                            is_active=bool(is_active),
                        )

        verb = "Would backfill" if dry_run else "Backfilled"
        self.stdout.write(self.style.SUCCESS(f"{verb} {total} platform route(s)."))
