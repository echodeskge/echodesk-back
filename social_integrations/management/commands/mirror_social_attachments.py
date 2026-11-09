"""
Backfill: copy Facebook / Instagram message attachments off Meta's expiring
CDN into our storage and rewrite the stored URLs (same service the ingest
task and the refresh endpoint use — social_integrations/services/media_mirror.py).

Per message it first downloads the stored CDN link (free); when that has
expired it asks the Graph API for a fresh link (spends app rate budget —
disable with --no-graph). The run stops if the Graph API starts throttling.

Usage:
    python manage.py mirror_social_attachments --schema amanati --dry-run
    python manage.py mirror_social_attachments --schema amanati --since 2026-06-01 --limit 500
    python manage.py mirror_social_attachments --all
"""
import time
from collections import Counter
from datetime import datetime, time as dtime, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from tenant_schemas.utils import schema_context

from social_integrations.services import media_mirror


class GraphThrottledAbort(Exception):
    pass


def candidate_queryset(platform, since=None):
    """Rows that may still reference a Meta CDN link (text pre-filter; the
    exact check is media_mirror.needs_mirroring)."""
    Model = media_mirror.model_for(platform)
    q = Q()
    for host in media_mirror.META_HOST_SUFFIXES:
        q |= Q(attachment_url__icontains=host) | Q(attachments__icontains=host)
    qs = Model.objects.filter(q, is_deleted=False)
    if since is not None:
        qs = qs.filter(timestamp__gte=since)
    related = 'page_connection' if platform == 'facebook' else 'account_connection__facebook_page'
    return qs.select_related(related).order_by('-timestamp', '-id')


class Command(BaseCommand):
    help = "Mirror Facebook/Instagram attachments from Meta's CDN into our storage"

    def add_arguments(self, parser):
        parser.add_argument('--schema', help='Tenant schema to process')
        parser.add_argument('--all', action='store_true', help='Process every tenant schema')
        parser.add_argument('--since', help='Only messages on/after this date (YYYY-MM-DD)')
        parser.add_argument('--limit', type=int, default=None,
                            help='Stop after processing N messages (across all tenants)')
        parser.add_argument('--platform', choices=list(media_mirror.PLATFORMS),
                            help='Only this platform (default: both)')
        parser.add_argument('--no-graph', action='store_true',
                            help='Never call the Graph API for expired links')
        parser.add_argument('--delay', type=float, default=0.2,
                            help='Seconds to sleep after a message that needed the Graph API (default 0.2)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Only count candidate messages/attachments; no network calls or writes')

    def handle(self, *args, **options):
        if bool(options['all']) == bool(options['schema']):
            raise CommandError('Pass exactly one of --schema <name> or --all')
        since = None
        if options['since']:
            try:
                day = datetime.strptime(options['since'], '%Y-%m-%d').date()
            except ValueError:
                raise CommandError('--since must be YYYY-MM-DD')
            since = datetime.combine(day, dtime.min, tzinfo=dt_timezone.utc)

        if options['all']:
            from tenants.models import Tenant
            schemas = list(Tenant.objects.exclude(schema_name='public')
                           .order_by('schema_name').values_list('schema_name', flat=True))
        else:
            schemas = [options['schema']]

        platforms = [options['platform']] if options['platform'] else list(media_mirror.PLATFORMS)
        self.dry_run = options['dry_run']
        self.use_graph = not options['no_graph']
        self.delay = options['delay']
        self.remaining = options['limit']
        self.totals = Counter()

        try:
            for schema in schemas:
                self.stdout.write(f'\n=== Tenant: {schema} ===')
                with schema_context(schema):
                    for platform in platforms:
                        if self.remaining is not None and self.remaining <= 0:
                            break
                        self.process(platform, since)
        except GraphThrottledAbort:
            self.stdout.write(self.style.ERROR(
                '\nGraph API is rate-limiting this app — aborting. Re-run later.'))

        t = self.totals
        if self.dry_run:
            types = ', '.join(f'{k}={v}' for k, v in sorted(t.items()) if k.startswith('type:')) or 'none'
            self.stdout.write(self.style.SUCCESS(
                f"\nDry run: {t['messages']} message(s), {t['attachments']} attachment(s) to mirror "
                f"({types}). Nothing downloaded or written."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\nDone. messages={t['messages']} mirrored={t['mirrored']} "
                f"failed={t['failed']} graph_used={t['graph_used']}"))
            reasons = ', '.join(f'{k[7:]}={v}' for k, v in sorted(t.items()) if k.startswith('reason:'))
            if reasons:
                self.stdout.write(f'Failure reasons: {reasons}')

    def process(self, platform, since):
        qs = candidate_queryset(platform, since)
        counts = Counter()
        for msg in qs.iterator(chunk_size=200):
            if self.remaining is not None and self.remaining <= 0:
                break
            if not media_mirror.needs_mirroring(msg):
                continue
            if self.remaining is not None:
                self.remaining -= 1
            counts['messages'] += 1
            self.totals['messages'] += 1
            if self.dry_run:
                atts = media_mirror._working_attachments(msg)
                for att in atts:
                    if media_mirror._is_candidate(att):
                        counts['attachments'] += 1
                        self.totals['attachments'] += 1
                        self.totals[f"type:{att.get('type') or 'unknown'}"] += 1
                continue
            self.mirror_one(msg, platform, counts)
        if self.dry_run:
            self.stdout.write(f"  {platform}: {counts['messages']} message(s), "
                              f"{counts['attachments']} attachment(s) would be mirrored")
        else:
            self.stdout.write(f"  {platform}: {counts['messages']} message(s), "
                              f"{counts['mirrored']} attachment(s) mirrored, {counts['failed']} failed")

    def mirror_one(self, msg, platform, counts):
        result = media_mirror.mirror_message(msg, platform)
        if result.failures and self.use_graph:
            result = media_mirror.refresh_message(msg, platform, initial=result)
            self.totals['graph_used'] += 1
            if any(f['reason'] == 'graph_throttled' for f in result.failures):
                raise GraphThrottledAbort()
            if self.delay:
                time.sleep(self.delay)
        counts['mirrored'] += result.mirrored
        counts['failed'] += len(result.failures)
        self.totals['mirrored'] += result.mirrored
        self.totals['failed'] += len(result.failures)
        for f in result.failures:
            self.totals[f"reason:{f['reason']}"] += 1
