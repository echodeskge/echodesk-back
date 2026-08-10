"""
Backfill customer profile pictures onto our own storage.

Meta's CDN URLs stored on old messages expire, which breaks avatars in
historical conversations. For every known sender this command copies the
picture to our storage and rewrites the stored URLs so old conversations
keep their avatars permanently.

Per sender it tries, in order:
  1. a fresh CachedProfilePicture entry        (no network at all)
  2. the CDN URL already stored on messages    (free — not a Graph API call)
  3. one Graph API lookup                      (spends app rate budget,
                                                bounded by --graph-budget)

Rows are only rewritten when a copy succeeds; failures leave everything
untouched, so a sender whose picture cannot be recovered keeps whatever
URL they had.

Usage:
    python manage.py cache_profile_pictures --all
    python manage.py cache_profile_pictures --schema amanati --days 180
    python manage.py cache_profile_pictures --all --dry-run
"""
import time
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from tenant_schemas.utils import schema_context

from social_integrations.models import (
    CachedProfilePicture,
    FacebookMessage,
    FacebookPageConnection,
    InstagramAccountConnection,
    InstagramMessage,
    SocialAccount,
    SocialClient,
)
from social_integrations.services.profile_pic_cache import (
    _is_fresh,
    _save_entry,
    get_facebook_profile_pic,
    get_instagram_profile,
    is_cached_url,
    recently_throttled,
    store_picture_copy,
)


class GraphThrottledAbort(Exception):
    """Raised to stop the whole run once the Graph API starts rate-limiting."""


class Command(BaseCommand):
    help = "Copy known senders' profile pictures to our storage and rewrite stored URLs"

    def add_arguments(self, parser):
        parser.add_argument('--schema', help='Tenant schema to process (omit with --all)')
        parser.add_argument('--all', action='store_true', help='Process every tenant schema')
        parser.add_argument('--days', type=int, default=365,
                            help='Only consider senders active in the last N days (default 365)')
        parser.add_argument('--graph-budget', type=int, default=150,
                            help='Max Graph API calls to spend per run across all tenants (default 150)')
        parser.add_argument('--delay', type=float, default=0.5,
                            help='Seconds to sleep between Graph API calls (default 0.5)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would happen without network calls or writes')

    def handle(self, *args, **options):
        if not options['all'] and not options['schema']:
            raise CommandError('Pass --schema <name> or --all')

        self.days = options['days']
        self.graph_budget = options['graph_budget']
        self.delay = options['delay']
        self.dry_run = options['dry_run']
        self.totals = {'reused': 0, 'rescued': 0, 'graph_fetched': 0, 'skipped': 0, 'failed': 0}
        self.throttle_strikes = 0

        if options['all']:
            from tenants.models import Tenant
            schemas = list(
                Tenant.objects.exclude(schema_name='public').values_list('schema_name', flat=True)
            )
        else:
            schemas = [options['schema']]

        try:
            for schema in schemas:
                self.stdout.write(f"\n=== Tenant: {schema} ===")
                with schema_context(schema):
                    self.process_facebook()
                    self.process_instagram()
        except GraphThrottledAbort:
            self.stdout.write(self.style.ERROR(
                "\nGraph API is rate-limiting this app — aborting the run. "
                "Re-run after the rolling one-hour window drains."
            ))

        t = self.totals
        self.stdout.write(self.style.SUCCESS(
            f"\nDone. reused={t['reused']} rescued={t['rescued']} "
            f"graph_fetched={t['graph_fetched']} skipped={t['skipped']} failed={t['failed']}"
            + (' (dry run — nothing written)' if self.dry_run else '')
        ))

    # ── Facebook ──────────────────────────────────────────────────────

    def process_facebook(self):
        since = timezone.now() - timedelta(days=self.days)
        for conn in FacebookPageConnection.objects.filter(is_active=True):
            sender_ids = (
                FacebookMessage.objects
                .filter(page_connection=conn, is_from_page=False, timestamp__gte=since)
                .exclude(sender_id=conn.page_id)
                .order_by('sender_id', '-timestamp')
                .distinct('sender_id')
                .values_list('sender_id', flat=True)
            )
            for sender_id in sender_ids:
                stored = (
                    FacebookMessage.objects
                    .filter(page_connection=conn, sender_id=sender_id, is_from_page=False)
                    .exclude(profile_pic_url__isnull=True)
                    .exclude(profile_pic_url='')
                    .order_by('-timestamp')
                    .values_list('profile_pic_url', flat=True)
                    .first()
                )
                url = self.resolve('facebook', sender_id, stored,
                                   lambda: get_facebook_profile_pic(sender_id, conn.page_access_token))
                if url and not self.dry_run:
                    updated = (
                        FacebookMessage.objects
                        .filter(page_connection=conn, sender_id=sender_id, is_from_page=False)
                        .exclude(profile_pic_url=url)
                        .update(profile_pic_url=url)
                    )
                    self.update_social_accounts('facebook', sender_id, conn.page_id, url)
                    self.stdout.write(f"  fb/{sender_id}: {updated} message rows updated")

    # ── Instagram ─────────────────────────────────────────────────────

    def process_instagram(self):
        since = timezone.now() - timedelta(days=self.days)
        for conn in InstagramAccountConnection.objects.filter(is_active=True):
            sender_ids = (
                InstagramMessage.objects
                .filter(account_connection=conn, is_from_business=False, timestamp__gte=since)
                .exclude(sender_id=conn.instagram_account_id)
                .order_by('sender_id', '-timestamp')
                .distinct('sender_id')
                .values_list('sender_id', flat=True)
            )
            for sender_id in sender_ids:
                stored = (
                    InstagramMessage.objects
                    .filter(account_connection=conn, sender_id=sender_id, is_from_business=False)
                    .exclude(sender_profile_pic__isnull=True)
                    .exclude(sender_profile_pic='')
                    .order_by('-timestamp')
                    .values_list('sender_profile_pic', flat=True)
                    .first()
                )
                url = self.resolve(
                    'instagram', sender_id, stored,
                    lambda: get_instagram_profile(sender_id, conn.access_token).get('profile_pic'),
                )
                if url and not self.dry_run:
                    updated = (
                        InstagramMessage.objects
                        .filter(account_connection=conn, sender_id=sender_id, is_from_business=False)
                        .exclude(sender_profile_pic=url)
                        .update(sender_profile_pic=url)
                    )
                    self.update_social_accounts('instagram', sender_id, conn.instagram_account_id, url)
                    self.stdout.write(f"  ig/{sender_id}: {updated} message rows updated")

    # ── Shared logic ──────────────────────────────────────────────────

    def resolve(self, platform, sender_id, stored_url, graph_fetch):
        """
        Return a permanent URL on our storage for the sender's picture, or
        None if it can't be recovered right now. Never blanks anything.
        """
        entry = CachedProfilePicture.objects.filter(
            platform=platform, platform_id=sender_id
        ).first()

        # 1. Fresh cache entry — reuse (an empty image_url is a negative
        #    cache entry: known to have no picture, nothing to rewrite).
        if entry and _is_fresh(entry):
            if entry.image_url:
                self.totals['reused'] += 1
                return entry.image_url
            self.totals['skipped'] += 1
            return None

        if stored_url and is_cached_url(stored_url):
            # Already points at our storage from a previous run.
            self.totals['skipped'] += 1
            return None

        if self.dry_run:
            action = 'rescue stored URL' if stored_url else 'Graph API fetch'
            self.stdout.write(f"  {platform}/{sender_id}: would try {action}")
            self.totals['skipped'] += 1
            return None

        if entry is None:
            entry = CachedProfilePicture(platform=platform, platform_id=sender_id)

        # 2. Free path: the stored CDN URL may still be alive — copying it
        #    costs no Graph API budget.
        if stored_url and store_picture_copy(entry, stored_url):
            entry.fetched_at = timezone.now()
            _save_entry(entry)
            self.totals['rescued'] += 1
            return entry.image_url

        # 3. Last resort: one Graph API call, if budget remains.
        if self.graph_budget <= 0:
            self.stdout.write(f"  {platform}/{sender_id}: graph budget exhausted, skipping")
            self.totals['skipped'] += 1
            return None
        self.graph_budget -= 1
        time.sleep(self.delay)
        url = graph_fetch()
        if recently_throttled():
            # The service does not negative-cache throttled responses, so
            # this sender stays retryable — but keeping going would just
            # deepen the rate-limit hole. Stop after a few strikes.
            self.throttle_strikes += 1
            self.totals['skipped'] += 1
            if self.throttle_strikes >= 3:
                raise GraphThrottledAbort()
            return None
        self.throttle_strikes = 0
        if url:
            self.totals['graph_fetched'] += 1
            return url
        self.totals['failed'] += 1
        return None

    def update_social_accounts(self, platform, sender_id, connection_id, url):
        """Point SocialAccount and linked client avatars at the permanent copy."""
        accounts = SocialAccount.objects.filter(
            platform=platform, platform_id=sender_id, account_connection_id=connection_id
        )
        accounts.exclude(profile_pic_url=url).update(profile_pic_url=url)
        for account in accounts.select_related('client'):
            client = account.client
            # Don't overwrite pictures that already live on our storage
            # (e.g. manually uploaded ones) — only replace expiring
            # platform URLs or fill empty values.
            if client and not is_cached_url(client.profile_picture or ''):
                client.profile_picture = url
                client.save(update_fields=['profile_picture'])
