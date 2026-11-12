"""Shared helpers: enable subscription features for the test tenant and a
fake step registered under a feature key no real module uses."""
from django.utils import timezone
from rest_framework import serializers

from onboarding import registry
from onboarding.base import OnboardingStep
from tenants.models import Feature, TenantSubscription
from tenants.subscription_service import SubscriptionService

FAKE_FEATURE = 'fake_feature'


def get_subscription(tenant):
    sub, _ = TenantSubscription.objects.get_or_create(
        tenant=tenant,
        defaults={'starts_at': timezone.now(), 'is_active': True, 'agent_count': 5},
    )
    return sub


def enable_features(tenant, *keys):
    """Put ``keys`` on the tenant's active subscription and sync (the same
    path Settings → Subscription takes)."""
    sub = get_subscription(tenant)
    for key in keys:
        feature, _ = Feature.objects.get_or_create(
            key=key, defaults={'name': key.replace('_', ' ').title(), 'is_active': True},
        )
        sub.selected_features.add(feature)
    SubscriptionService.sync_tenant_features(sub)
    return sub


def disable_features(tenant, *keys):
    sub = get_subscription(tenant)
    for feature in Feature.objects.filter(key__in=keys):
        sub.selected_features.remove(feature)
    SubscriptionService.sync_tenant_features(sub)
    return sub


class FakeAnswersSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=20)


class FakeStep(OnboardingStep):
    key = 'fake'
    feature_key = FAKE_FEATURE
    order = 99
    serializer_class = FakeAnswersSerializer

    applied = []   # answers passed to apply()
    seeded = []    # schema names seed_defaults() ran on

    def context(self, tenant, request):
        return {'flag': True}

    def prefill(self, tenant, state):
        return {'name': 'suggested'}

    def apply(self, tenant, answers, user):
        FakeStep.applied.append(dict(answers))
        return {'created': {'things': 1}, 'updated': ['name'], 'notes': ['hello']}

    def seed_defaults(self, tenant):
        FakeStep.seeded.append(tenant.schema_name)


class FakeStepMixin:
    """Register ``FakeStep`` for the test and clean up after it."""

    def setUp(self):
        super().setUp()
        registry.register(FakeStep)
        FakeStep.applied.clear()
        FakeStep.seeded.clear()

    def tearDown(self):
        registry.unregister(FakeStep.key)
        super().tearDown()
