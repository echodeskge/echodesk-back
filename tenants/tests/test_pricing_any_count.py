"""
Pricing decision 2026-11-13 (supersedes D2): the number of employees
(``agent_count``) is any whole number from 1, with no upper cap. It is
chosen at signup (default 1) and can be changed in Settings at any time;
during the free trial the change is free (no proration, no BOG order).

Covers every place that accepts an agent_count:
  - TenantRegistrationSerializer (POST /api/register/ and
    /api/register-with-payment/ for both BOG and Paddle)
  - PUT /api/subscription/agent-count/ (in-app change, trial and paid)
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase
from tenants.models import PaymentOrder
from tenants.pricing_rules import (
    AGENT_COUNT_ERROR, AGENT_COUNT_STEP, MIN_AGENT_COUNT, TRIAL_DAYS, is_valid_agent_count,
)
from tenants.serializers import TenantRegistrationSerializer
from tenants.tests.test_subscription_views import SubscriptionTestMixin, AGENT_COUNT_URL


class TestPricingRules(EchoDeskTenantTestCase):
    """Pure helper: the single source of truth for the rule."""

    def test_constants(self):
        self.assertEqual(MIN_AGENT_COUNT, 1)
        self.assertEqual(AGENT_COUNT_STEP, 1)

    def test_accepted_values(self):
        for value in (1, 2, 3, 7, 999, 100000):
            self.assertTrue(is_valid_agent_count(value), value)

    def test_rejected_values(self):
        for value in (0, -1, 2.5, 5.0, '5', True, False, None):
            self.assertFalse(is_valid_agent_count(value), repr(value))

    def test_error_message_names_the_rule(self):
        self.assertEqual(AGENT_COUNT_ERROR, 'Employee count must be a whole number of at least 1')
        self.assertNotIn('multiple', AGENT_COUNT_ERROR)


class TestRegistrationAgentCount(SubscriptionTestMixin, EchoDeskTenantTestCase):
    """TenantRegistrationSerializer — shared by the free and the paid signup."""

    def setUp(self):
        super().setUp()
        self.feature = self._make_feature('tickets_any', name='Tickets', price=Decimal('9.00'))

    def _payload(self, **overrides):
        data = {
            'company_name': 'Any Ltd',
            'domain': 'any-ltd',
            'feature_ids': [self.feature.id],
            'pricing_model': 'agent',
            'agent_count': 1,
            'admin_email': 'owner@any.example',
            'admin_password': 'Strong1Password',
            'admin_first_name': 'Nino',
            'admin_last_name': 'B',
            'preferred_language': 'ka',
        }
        data.update(overrides)
        return data

    def test_any_whole_number_from_one_accepted(self):
        for value in (1, 2, 3, 7, 999):
            s = TenantRegistrationSerializer(data=self._payload(agent_count=value))
            self.assertTrue(s.is_valid(), (value, s.errors))
            self.assertEqual(s.validated_data['agent_count'], value)

    def test_zero_and_negative_rejected(self):
        for value in (0, -1):
            s = TenantRegistrationSerializer(data=self._payload(agent_count=value))
            self.assertFalse(s.is_valid(), value)
            self.assertIn('agent_count', s.errors)

    def test_non_integers_rejected(self):
        for value in (2.5, 'seven', True):
            s = TenantRegistrationSerializer(data=self._payload(agent_count=value))
            self.assertFalse(s.is_valid(), repr(value))
            self.assertIn('agent_count', s.errors)

    def test_default_is_one(self):
        payload = self._payload()
        del payload['agent_count']
        s = TenantRegistrationSerializer(data=payload)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data['agent_count'], 1)

    def test_crm_pricing_normalises_to_one(self):
        s = TenantRegistrationSerializer(data=self._payload(pricing_model='crm', agent_count=20))
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data['agent_count'], MIN_AGENT_COUNT)


class TestUpdateAgentCountPaid(SubscriptionTestMixin, EchoDeskTenantTestCase):
    """PUT /api/subscription/agent-count/ on a paid subscription (unchanged proration)."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='agany@test.com')
        self.sub = self._make_subscription(agent_count=10)
        self.feat = self._make_feature('base_any', name='Base', price=Decimal('5.00'))
        self.sub.selected_features.add(self.feat)

    def test_seven_accepted(self):
        resp = self.api_put(AGENT_COUNT_URL, {'agent_count': 7}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['agent_count'], 7)
        self.assertEqual(resp.data['new_agent_count'], 7)
        self.assertEqual(resp.data['monthly_cost'], 35.0)
        self.assertEqual(resp.data['new_monthly_cost'], 35.0)
        self.assertTrue(resp.data['charged'])
        self.assertFalse(resp.data['is_trial'])
        self.assertIn('prorated_difference', resp.data)
        self.assertIn('days_remaining', resp.data)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.agent_count, 7)

    def test_one_accepted(self):
        resp = self.api_put(AGENT_COUNT_URL, {'agent_count': 1}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['monthly_cost'], 5.0)

    def test_large_count_accepted_no_cap(self):
        resp = self.api_put(AGENT_COUNT_URL, {'agent_count': 999}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['agent_count'], 999)

    def test_zero_rejected(self):
        resp = self.api_put(AGENT_COUNT_URL, {'agent_count': 0}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['error'], AGENT_COUNT_ERROR)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.agent_count, 10)

    def test_string_and_float_rejected(self):
        for value in ('10', 2.5, True):
            resp = self.api_put(AGENT_COUNT_URL, {'agent_count': value}, user=self.admin)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, repr(value))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.agent_count, 10)


class TestUpdateAgentCountTrial(SubscriptionTestMixin, EchoDeskTenantTestCase):
    """PUT /api/subscription/agent-count/ during the free trial is free."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='agtrial@test.com')
        ends = timezone.now() + timedelta(days=20)
        self.sub = self._make_subscription(
            agent_count=3,
            is_trial=True,
            subscription_type='trial',
            trial_ends_at=ends,
            expires_at=ends,
            next_billing_date=ends,
            starts_at=ends - timedelta(days=TRIAL_DAYS),
        )
        self.feat = self._make_feature('base_trial_any', name='Base', price=Decimal('5.00'))
        self.sub.selected_features.add(self.feat)

    def test_trial_change_is_free_and_updates_monthly_cost(self):
        orders_before = PaymentOrder.objects.count()
        resp = self.api_put(AGENT_COUNT_URL, {'agent_count': 4}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data['success'])
        self.assertEqual(resp.data['agent_count'], 4)
        self.assertEqual(resp.data['monthly_cost'], 20.0)
        self.assertFalse(resp.data['charged'])
        self.assertTrue(resp.data['is_trial'])
        self.assertIn('free during the trial', resp.data['message'])
        self.assertNotIn('prorated_difference', resp.data)
        self.assertNotIn('days_remaining', resp.data)
        self.assertNotIn('payment_url', resp.data)
        self.assertEqual(PaymentOrder.objects.count(), orders_before)

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.agent_count, 4)
        self.assertEqual(self.sub.monthly_cost, Decimal('20.00'))
        # Trial state untouched
        self.assertTrue(self.sub.is_trial)
        self.assertFalse(self.sub.trial_converted)
        self.assertIsNone(self.sub.parent_order_id)

    def test_trial_decrease_to_one(self):
        resp = self.api_put(AGENT_COUNT_URL, {'agent_count': 1}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['monthly_cost'], 5.0)
        self.assertFalse(resp.data['charged'])

    def test_trial_zero_rejected(self):
        resp = self.api_put(AGENT_COUNT_URL, {'agent_count': 0}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.agent_count, 3)
