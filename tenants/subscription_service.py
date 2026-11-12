"""
Subscription service for managing tenant features and permissions

Handles the automatic provisioning of features and permissions when
a tenant subscribes to a package or changes packages.
"""

from django.db import transaction
from django.utils import timezone
from .models import (
    TenantSubscription, TenantFeature, TenantPermission
)
from .feature_models import FeaturePermission
import logging

logger = logging.getLogger(__name__)


class SubscriptionService:
    """Service for managing subscription features and permissions"""

    @staticmethod
    def sync_tenant_features(subscription):
        """
        Sync tenant features based on their selected features

        This method:
        1. Gets all features from the subscription
        2. Creates/activates TenantFeature records
        3. Deactivates features not in the subscription
        4. Syncs permissions for each feature

        Args:
            subscription: TenantSubscription instance

        Returns:
            dict: Summary of changes made
        """
        with transaction.atomic():
            tenant = subscription.tenant

            # Get all selected features
            selected_features = subscription.selected_features.filter(is_active=True)

            enabled_features = []
            disabled_features = []
            enabled_keys = []   # feature keys newly enabled (created or reactivated)
            disabled_keys = []  # feature keys disabled this sync
            permissions_granted = 0

            # Enable features from subscription
            for feature in selected_features:
                # Get or create tenant feature
                tenant_feature, created = TenantFeature.objects.get_or_create(
                    tenant=tenant,
                    feature=feature,
                    defaults={
                        'is_active': True,
                        'custom_value': None
                    }
                )

                # Reactivate if it was disabled
                if not tenant_feature.is_active:
                    tenant_feature.is_active = True
                    tenant_feature.disabled_at = None
                    tenant_feature.save()
                    enabled_features.append(feature.name)
                    enabled_keys.append(feature.key)

                if created:
                    enabled_features.append(feature.name)
                    enabled_keys.append(feature.key)

                # Grant permissions for this feature
                permissions_granted += SubscriptionService._sync_feature_permissions(
                    tenant, feature
                )

            # Disable features not in subscription
            feature_ids = list(selected_features.values_list('id', flat=True))
            disabled_tenant_features = TenantFeature.objects.filter(
                tenant=tenant,
                is_active=True
            ).exclude(feature_id__in=feature_ids)

            for tf in disabled_tenant_features:
                tf.is_active = False
                tf.disabled_at = timezone.now()
                tf.save()
                disabled_features.append(tf.feature.name)
                disabled_keys.append(tf.feature.key)

                # Revoke permissions for disabled features
                SubscriptionService._revoke_feature_permissions(tenant, tf.feature)

            logger.info(
                f"Synced features for tenant {tenant.schema_name}: "
                f"enabled={len(enabled_features)}, disabled={len(disabled_features)}, "
                f"permissions={permissions_granted}"
            )

            summary = {
                'enabled_features': enabled_features,
                'disabled_features': disabled_features,
                'enabled_feature_keys': enabled_keys,
                'disabled_feature_keys': disabled_keys,
                'permissions_granted': permissions_granted
            }

        # Outside the atomic block: onboarding keeps its own savepoint and
        # never raises, so a missing tenant schema can't poison the feature sync.
        SubscriptionService._notify_onboarding(tenant, enabled_keys, disabled_keys)
        return summary

    @staticmethod
    def _notify_onboarding(tenant, enabled_keys, disabled_keys):
        """Mark newly enabled modules' onboarding steps pending and drop the
        entries of disabled ones (no-op without the onboarding app)."""
        if not enabled_keys and not disabled_keys:
            return
        from django.apps import apps
        if not apps.is_installed('onboarding'):
            return
        try:
            from onboarding.services import on_features_changed
            on_features_changed(tenant, enabled_keys, disabled_keys)
        except Exception as e:  # noqa: BLE001 - never break feature sync
            logger.warning(f'Onboarding feature-change hook failed for {tenant.schema_name}: {e}')

    @staticmethod
    def _sync_feature_permissions(tenant, feature):
        """
        Grant all permissions associated with a feature to a tenant

        Args:
            tenant: Tenant instance
            feature: Feature instance

        Returns:
            int: Number of permissions granted
        """
        # Get all permissions for this feature
        feature_permissions = FeaturePermission.objects.filter(
            feature=feature
        ).select_related('permission')

        granted_count = 0

        for fp in feature_permissions:
            permission = fp.permission

            # Get or create tenant permission
            tenant_perm, created = TenantPermission.objects.get_or_create(
                tenant=tenant,
                permission=permission,
                defaults={
                    'is_active': True,
                    'granted_by_feature': feature
                }
            )

            # Reactivate if it was revoked
            if not tenant_perm.is_active:
                tenant_perm.is_active = True
                tenant_perm.revoked_at = None
                tenant_perm.granted_by_feature = feature
                tenant_perm.save()
                granted_count += 1

            if created:
                granted_count += 1

        return granted_count

    @staticmethod
    def _revoke_feature_permissions(tenant, feature):
        """
        Revoke permissions that were granted by this feature

        Only revokes permissions that:
        1. Were granted by this specific feature
        2. Are not granted by any other active feature

        Args:
            tenant: Tenant instance
            feature: Feature instance
        """
        # Get all permissions granted by this feature
        feature_permission_ids = FeaturePermission.objects.filter(
            feature=feature
        ).values_list('permission_id', flat=True)

        # Get active features for tenant (excluding this one)
        other_active_features = TenantFeature.objects.filter(
            tenant=tenant,
            is_active=True
        ).exclude(feature=feature).values_list('feature_id', flat=True)

        # Get permissions granted by other active features
        protected_permission_ids = FeaturePermission.objects.filter(
            feature_id__in=other_active_features
        ).values_list('permission_id', flat=True)

        # Revoke only permissions not granted by other features
        revokable_permission_ids = set(feature_permission_ids) - set(protected_permission_ids)

        TenantPermission.objects.filter(
            tenant=tenant,
            permission_id__in=revokable_permission_ids,
            granted_by_feature=feature
        ).update(
            is_active=False,
            revoked_at=timezone.now()
        )

    @staticmethod
    def check_tenant_has_permission_available(tenant, permission_key):
        """
        Check if a tenant has a permission available to grant to users

        This checks if the tenant's package includes features that grant this permission.
        The tenant admin can then grant this permission to users via the User model fields.

        Args:
            tenant: Tenant instance
            permission_key: Permission key (e.g., 'view_all_tickets')

        Returns:
            bool: True if permission is available to the tenant
        """
        from .models import TenantPermission, Permission

        try:
            # Get the permission object
            permission = Permission.objects.get(key=permission_key, is_active=True)

            # Check if tenant has this permission available
            has_tenant_permission = TenantPermission.objects.filter(
                tenant=tenant,
                permission=permission,
                is_active=True
            ).exists()

            return has_tenant_permission

        except Permission.DoesNotExist:
            logger.warning(f"Permission '{permission_key}' not found")
            return False
        except Exception as e:
            logger.error(f"Error checking permission '{permission_key}' for tenant {tenant.schema_name}: {e}")
            return False

    @staticmethod
    def check_tenant_feature(tenant, feature_key):
        """
        Check if a tenant has a specific feature enabled

        Args:
            tenant: Tenant instance
            feature_key: Feature key (e.g., 'whatsapp_integration')

        Returns:
            bool: True if feature is enabled
        """
        return TenantSubscription.objects.filter(
            tenant=tenant,
            is_active=True,
            selected_features__key=feature_key,
            selected_features__is_active=True
        ).exists()
