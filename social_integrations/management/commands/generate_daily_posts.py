import logging
from datetime import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone
from tenant_schemas.utils import schema_context
from tenants.models import Tenant
import pytz

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Generate daily AI posts for tenants with auto-posting enabled'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant',
            type=str,
            help='Run only for a specific tenant schema name',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be generated without creating posts',
        )

    def handle(self, *args, **options):
        target_tenant = options.get('tenant')
        dry_run = options.get('dry_run', False)

        tenants = Tenant.objects.exclude(schema_name='public')
        if target_tenant:
            tenants = tenants.filter(schema_name=target_tenant)

        total_generated = 0

        for tenant in tenants:
            try:
                with schema_context(tenant.schema_name):
                    generated = self._process_tenant(tenant, dry_run)
                    total_generated += generated
            except Exception as e:
                logger.error(f"Error processing tenant {tenant.schema_name}: {e}")
                self.stderr.write(f"Error for {tenant.schema_name}: {e}")

        self.stdout.write(self.style.SUCCESS(
            f"Generated {total_generated} posts (dry_run={dry_run})"
        ))

    def _process_tenant(self, tenant, dry_run):
        from social_integrations.models import AutoPostSettings, AutoPostContent

        try:
            auto_settings = AutoPostSettings.objects.get(pk=1)
        except AutoPostSettings.DoesNotExist:
            return 0

        if not auto_settings.is_enabled:
            return 0

        # Check if posting time has arrived in tenant's timezone
        try:
            tz = pytz.timezone(auto_settings.timezone)
        except pytz.exceptions.UnknownTimeZoneError:
            tz = pytz.UTC

        now_in_tz = timezone.now().astimezone(tz)
        posting_time = auto_settings.posting_time

        # Only generate if current hour matches posting hour
        if now_in_tz.hour != posting_time.hour:
            return 0

        # Check if we already generated today
        today_start = now_in_tz.replace(hour=0, minute=0, second=0, microsecond=0)
        today_count = AutoPostContent.objects.filter(
            created_at__gte=today_start,
        ).count()

        if today_count >= auto_settings.max_posts_per_day:
            logger.info(f"Tenant {tenant.schema_name}: already reached {today_count} posts today")
            return 0

        if dry_run:
            self.stdout.write(f"[DRY RUN] Would generate post for {tenant.schema_name}")
            return 1

        # Generate the post
        try:
            from social_integrations.services.ai_content_service import AIContentService
            service = AIContentService()
            post = service.generate_post_for_tenant(auto_settings)

            from social_integrations.notifications import notify_auto_post

            # If auto-approve, immediately mark for publishing
            if not auto_settings.require_approval and post.status == 'approved':
                self._auto_publish(post)
                if post.status == 'failed':
                    notify_auto_post(post, 'failed')
            elif post.status == 'draft':
                # Needs a human: tell the social-settings admins (push + bell).
                notify_auto_post(post, 'draft')

            logger.info(f"Generated post for {tenant.schema_name}: {post.id}")
            self.stdout.write(f"Generated post for {tenant.schema_name}: {post.id}")
            return 1

        except Exception as e:
            logger.error(f"Failed to generate post for {tenant.schema_name}: {e}")
            self.stderr.write(f"Failed for {tenant.schema_name}: {e}")
            return 0

    def _auto_publish(self, post):
        """Publish the post immediately if auto-approve is enabled."""
        from social_integrations.services.facebook_publisher import FacebookPublisher
        from social_integrations.services.instagram_publisher import InstagramPublisher

        errors = []

        if post.target_facebook:
            try:
                fb = FacebookPublisher()
                post.facebook_post_id = fb.publish(post)
            except Exception as e:
                errors.append(f"Facebook: {str(e)}")

        if post.target_instagram:
            try:
                ig = InstagramPublisher()
                post.instagram_media_id = ig.publish(post)
            except Exception as e:
                errors.append(f"Instagram: {str(e)}")

        if errors:
            post.status = 'failed'
            post.error_message = '; '.join(errors)
        else:
            post.status = 'published'
            post.published_at = timezone.now()

        post.save()
