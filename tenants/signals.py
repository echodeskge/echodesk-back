"""
Django signals for automatic feature and permission synchronization

Automatically syncs tenant features and permissions when:
- A new subscription is created
- A subscription package is changed
- A subscription is activated/deactivated
"""

from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import TenantSubscription
from .subscription_service import SubscriptionService
import logging

logger = logging.getLogger(__name__)


@receiver(post_save, sender=TenantSubscription)
def sync_subscription_features(sender, instance, created, **kwargs):
    """
    Automatically sync tenant features and permissions when subscription changes

    Triggered on:
    - New subscription creation
    - Subscription package change
    - Subscription activation/deactivation
    """
    try:
        # Only sync if subscription is active
        if instance.is_active:
            logger.info(f"Syncing features for subscription: {instance}")
            result = SubscriptionService.sync_tenant_features(instance)
            logger.info(
                f"Feature sync completed for {instance.tenant.schema_name}: "
                f"{result['enabled_features'].__len__()} enabled, "
                f"{result['disabled_features'].__len__()} disabled, "
                f"{result['permissions_granted']} permissions granted"
            )
        else:
            logger.info(f"Subscription {instance} is inactive, skipping feature sync")

    except Exception as e:
        # Handle case where tenant schema migrations haven't completed yet
        error_msg = str(e).lower()
        if 'does not exist' in error_msg or 'relation' in error_msg:
            logger.warning(
                f"Tenant schema tables not ready yet for {instance.tenant.schema_name}, "
                f"skipping feature sync. This is normal during initial tenant creation."
            )
        else:
            logger.error(f"Error syncing features for subscription {instance}: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Keep TenantSubscription.current_users in sync with the tenant's users.
# Read paths recount live as well (tenants.permissions), so these signals are
# about keeping the stored value (admin, raw serializers) fresh.
# ---------------------------------------------------------------------------
from django.conf import settings as _settings  # noqa: E402
from django.db.models.signals import post_delete  # noqa: E402


@receiver(post_save, sender=_settings.AUTH_USER_MODEL)
def sync_user_count_on_user_save(sender, instance, created, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    # Logins save(update_fields=['last_login']); only recount when the
    # billable state could have changed.
    if not created and update_fields is not None and 'is_active' not in update_fields:
        return
    from .user_count import sync_current_schema_user_count
    sync_current_schema_user_count()


@receiver(post_delete, sender=_settings.AUTH_USER_MODEL)
def sync_user_count_on_user_delete(sender, instance, **kwargs):
    from .user_count import sync_current_schema_user_count
    sync_current_schema_user_count()
