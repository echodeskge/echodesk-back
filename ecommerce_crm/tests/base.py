"""
Shared base for ecommerce admin API tests.

The admin endpoints require the ``ecommerce_crm`` feature
(ecommerce_crm.permissions). ``EcommerceTenantTestCase`` gives the test
tenant an active subscription that includes it, so staff users pass the
feature check; ``grant_feature_group`` puts a non-staff user in a group
that has the feature (how agents get it in production).
"""
from decimal import Decimal

from django.utils import timezone

from users.tests.conftest import EchoDeskTenantTestCase


def ensure_ecommerce_feature(tenant, active=True):
    from tenants.models import Feature, TenantSubscription

    feature, _ = Feature.objects.get_or_create(
        key='ecommerce_crm',
        defaults={
            'name': 'E-commerce',
            'price_per_user_gel': Decimal('0'),
            'price_unlimited_gel': Decimal('0'),
            'category': 'support',
            'is_active': True,
        },
    )
    sub, _ = TenantSubscription.objects.get_or_create(
        tenant=tenant,
        defaults={
            'is_active': active,
            'starts_at': timezone.now(),
            'agent_count': 10,
        },
    )
    if sub.is_active != active:
        sub.is_active = active
        sub.save(update_fields=['is_active'])
    sub.selected_features.add(feature)
    return feature


def remove_ecommerce_feature(tenant):
    from tenants.models import TenantSubscription

    for sub in TenantSubscription.objects.filter(tenant=tenant):
        sub.selected_features.remove(*sub.selected_features.filter(key='ecommerce_crm'))


class EcommerceTenantTestCase(EchoDeskTenantTestCase):
    """EchoDeskTenantTestCase whose tenant plan includes ecommerce_crm."""

    def setUp(self):
        super().setUp()
        self.ecommerce_feature = ensure_ecommerce_feature(self.tenant)

    def grant_feature_group(self, user, name='Shop team'):
        group = self.create_tenant_group(name=name)
        group.features.add(self.ecommerce_feature)
        user.tenant_groups.add(group)
        return group
