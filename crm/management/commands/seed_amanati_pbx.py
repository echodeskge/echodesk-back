"""Seed the amanati tenant with the PBX state currently live on pbx2.echodesk.cloud.

Creates Trunk, Queue, and InboundRoute rows that mirror the hand-written
configs in /etc/asterisk/pjsip.conf, queues.conf, and extensions.conf so the
new PBX management UI shows what's really running on Asterisk today.

Idempotent — safe to re-run. Matches by Trunk.name / Queue.slug / InboundRoute.did.

Usage:
    AMANATI_TRUNK_PASSWORD=... python manage.py seed_amanati_pbx [--dry-run] [--schema amanati]

The trunk's SIP password is never stored in this file: it comes from the
``AMANATI_TRUNK_PASSWORD`` environment variable (the command refuses to
run without it) and is only ever written to the encrypted Trunk.password
column.
"""

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from tenant_schemas.utils import schema_context

from crm.models import (
    InboundRoute,
    Queue,
    SipConfiguration,
    Trunk,
)


GEO_PROVIDER_TRUNK = {
    "name": "Geo Provider (Magti SIP)",
    "provider": "Magti",
    "sip_server": "89.150.1.11",
    "sip_port": 5060,
    "username": "1048444e3",
    # "password" is injected from AMANATI_TRUNK_PASSWORD at run time.
    "realm": "",
    "proxy": "",
    "register": True,
    "codecs": ["g722", "amrwb", "alaw", "ulaw"],
    "caller_id_number": "1048444e3",
    "phone_numbers": ["+995322421219"],
    "is_active": True,
}

SUPPORT_QUEUE = {
    "name": "Support",
    "slug": "support",
    "strategy": "rrmemory",
    "timeout_seconds": 30,
    "max_wait_seconds": 300,
    "max_len": 10,
    "wrapup_time": 10,
    "music_on_hold": "queue-hold",
    "announce_position": True,
    "announce_holdtime": False,
    "joinempty": "yes",
    "leavewhenempty": "no",
    "is_active": True,
    "is_default": True,
}

# Name of the tenant group that backs the Support queue. Must already exist
# in the amanati schema. "ოპერატორები" (Operators) contains the agents
# currently assigned extensions 100 and 101.
SUPPORT_GROUP_NAME = "ოპერატორები"

INBOUND_ROUTES = [
    {
        "did": "+995322421219",
        "destination_type": "queue",
        "destination_queue_slug": "support",
        "priority": 100,
        "is_active": True,
    },
]


class Command(BaseCommand):
    help = "Seed a tenant with the PBX state live on pbx2.echodesk.cloud"

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema", default="amanati", help="Tenant schema (default: amanati)"
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        schema = opts["schema"]
        dry = opts["dry_run"]
        trunk_password = os.environ.get("AMANATI_TRUNK_PASSWORD", "")
        if not trunk_password:
            raise CommandError(
                "AMANATI_TRUNK_PASSWORD is not set: export the Magti trunk's SIP "
                "password in the environment (it is not kept in the source tree)."
            )
        trunk_defaults = {**GEO_PROVIDER_TRUNK, "password": trunk_password}

        with schema_context(schema):
            # Lazy import so the TenantGroup model is resolved in the right schema
            from users.models import TenantGroup

            self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== Seeding schema '{schema}' ==="))

            with transaction.atomic():
                # --- Trunk ---
                trunk, created = Trunk.objects.get_or_create(
                    name=GEO_PROVIDER_TRUNK["name"],
                    defaults=trunk_defaults,
                )
                self._report("Trunk", trunk.name, created, dry)
                if not created and not dry:
                    # Update phone_numbers / codecs if they drift from source of truth
                    dirty = False
                    if list(trunk.phone_numbers or []) != GEO_PROVIDER_TRUNK["phone_numbers"]:
                        trunk.phone_numbers = GEO_PROVIDER_TRUNK["phone_numbers"]
                        dirty = True
                    if list(trunk.codecs or []) != GEO_PROVIDER_TRUNK["codecs"]:
                        trunk.codecs = GEO_PROVIDER_TRUNK["codecs"]
                        dirty = True
                    if dirty:
                        trunk.save()
                        self.stdout.write("    updated codecs / phone_numbers")

                # --- Queue ---
                try:
                    group = TenantGroup.objects.get(name=SUPPORT_GROUP_NAME)
                except TenantGroup.DoesNotExist:
                    raise CommandError(
                        f"TenantGroup '{SUPPORT_GROUP_NAME}' not found in schema '{schema}'. "
                        f"Create the group first or pass a different backing group."
                    )

                queue, created = Queue.objects.get_or_create(
                    slug=SUPPORT_QUEUE["slug"],
                    defaults={**SUPPORT_QUEUE, "group": group},
                )
                self._report("Queue", queue.slug, created, dry)
                if not created and not dry and queue.group_id != group.id:
                    queue.group = group
                    queue.save()
                    self.stdout.write("    updated group FK")

                # --- InboundRoute ---
                for route_spec in INBOUND_ROUTES:
                    target_queue = Queue.objects.filter(
                        slug=route_spec["destination_queue_slug"]
                    ).first()
                    if not target_queue:
                        self.stdout.write(
                            self.style.WARNING(
                                f"    skipped route {route_spec['did']} — "
                                f"queue '{route_spec['destination_queue_slug']}' missing"
                            )
                        )
                        continue

                    route, created = InboundRoute.objects.get_or_create(
                        did=route_spec["did"],
                        defaults={
                            "trunk": trunk,
                            "destination_type": route_spec["destination_type"],
                            "destination_queue": target_queue,
                            "priority": route_spec["priority"],
                            "is_active": route_spec["is_active"],
                        },
                    )
                    self._report("InboundRoute", route.did, created, dry)
                    if not created and not dry:
                        # Make sure the route currently points at our trunk/queue
                        if route.trunk_id != trunk.id or route.destination_queue_id != target_queue.id:
                            route.trunk = trunk
                            route.destination_queue = target_queue
                            route.save()
                            self.stdout.write("    updated trunk / destination")

                # --- Summary ---
                self.stdout.write("")
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Trunks={Trunk.objects.count()} Queues={Queue.objects.count()} "
                        f"InboundRoutes={InboundRoute.objects.count()} "
                        f"SipConfigurations={SipConfiguration.objects.count()}"
                    )
                )

                if dry:
                    self.stdout.write(self.style.WARNING("--dry-run: rolling back."))
                    transaction.set_rollback(True)

    def _report(self, kind: str, ident: str, created: bool, dry: bool):
        action = "created" if created else "exists"
        if dry and created:
            action = "would create"
        self.stdout.write(f"  {kind:<14} {ident:<40} [{action}]")
