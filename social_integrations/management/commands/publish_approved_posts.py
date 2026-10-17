import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from tenant_schemas.utils import schema_context
from tenants.models import Tenant

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Publish approved auto-posts that are scheduled for now or in the past'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant',
            type=str,
            help='Run only for a specific tenant schema name',
        )

    def handle(self, *args, **options):
        target_tenant = options.get('tenant')

        tenants = Tenant.objects.exclude(schema_name='public')
        if target_tenant:
            tenants = tenants.filter(schema_name=target_tenant)

        total_published = 0

        for tenant in tenants:
            try:
                with schema_context(tenant.schema_name):
                    published = self._process_tenant(tenant)
                    total_published += published
            except Exception as e:
                logger.error(f"Error processing tenant {tenant.schema_name}: {e}")
                self.stderr.write(f"Error for {tenant.schema_name}: {e}")

        self.stdout.write(self.style.SUCCESS(
            f"Published {total_published} posts"
        ))

    def _process_tenant(self, tenant):
        from social_integrations.models import AutoPostContent
        from social_integrations.services.facebook_publisher import FacebookPublisher
        from social_integrations.services.instagram_publisher import InstagramPublisher

        now = timezone.now()
        posts = AutoPostContent.objects.filter(
            status='approved',
            scheduled_for__lte=now,
        )

        published_count = 0

        for post in posts:
            errors = []

            if post.target_facebook:
                try:
                    fb = FacebookPublisher()
                    post.facebook_post_id = fb.publish(post)
                except Exception as e:
                    errors.append(f"Facebook: {str(e)}")
                    logger.error(f"Facebook publish error for post {post.id}: {e}")

            if post.target_instagram:
                try:
                    ig = InstagramPublisher()
                    post.instagram_media_id = ig.publish(post)
                except Exception as e:
                    errors.append(f"Instagram: {str(e)}")
                    logger.error(f"Instagram publish error for post {post.id}: {e}")

            if errors:
                post.status = 'failed'
                post.error_message = '; '.join(errors)
            else:
                post.status = 'published'
                post.published_at = now
                published_count += 1

            post.save()
            logger.info(f"Post {post.id} for tenant {tenant.schema_name}: {post.status}")
            if post.status == 'failed':
                from social_integrations.notifications import notify_auto_post
                notify_auto_post(post, 'failed')

        return published_count
