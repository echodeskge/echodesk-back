"""
Pricing decision D2 (2026-11-11): the minimum number of employees
(``agent_count``) is 5, sold in increments of 5.

Covers every place that accepts an agent_count:
  - TenantRegistrationSerializer (POST /api/register/ and
    /api/register-with-payment/ for both BOG and Paddle)
  - PUT /api/subscription/agent-count/ (in-app change)
"""
from decimal import Decimal

from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase
from tenants.pricing_rules import (
    AGENT_COUNT_ERROR, AGENT_COUNT_STEP, MIN_AGENT_COUNT, is_valid_agent_count,
)
from tenants.serializers import TenantRegistrationSerializer
from tenants.tests.test_subscription_views import SubscriptionTestMixin, AGENT_COUNT_URL


class TestPricingRules(EchoDeskTenantTestCase):
    """Pure helper: the single source of truth for the rule."""

    def test_constants(self):
        self.assertEqual(MIN_AGENT_COUNT, 5)
        self.assertEqual(AGENT_COUNT_STEP, 5)

    def test_accepted_values(self):
        for value in (5, 10, 15, 50, 200):
            self.assertTrue(is_valid_agent_count(value), value)

    def test_rejected_values(self):
        for value in (0, 1, 4, 6, 7, 9, 12, -5, None, '5', 5.0, True):
            self.assertFalse(is_valid_agent_count(value), repr(value))

    def test_error_message_names_the_rule(self):
        self.assertIn('at least 5', AGENT_COUNT_ERROR)
        self.assertIn('multiple of 5', AGENT_COUNT_ERROR)


class TestRegistrationAgentCount(SubscriptionTestMixin, EchoDeskTenantTestCase):
    """TenantRegistrationSerializer — shared by the free and the paid signup."""

    def setUp(self):
        super().setUp()
        self.feature = self._make_feature('tickets_min5', name='Tickets', price=Decimal('9.00'))

    def _payload(self, **overrides):
        data = {
            'company_name': 'Min5 Ltd',
            'domain': 'min5-ltd',
            'feature_ids': [self.feature.id],
            'pricing_model': 'agent',
            'agent_count': 5,
            'admin_email': 'owner@min5.example',
            'admin_password': 'Strong1Password',
            'admin_first_name': 'Nino',
            'admin_last_name': 'B',
            'preferred_language': 'ka',
        }
        data.update(overrides)
        return data

    def test_five_agents_accepted(self):
        s = TenantRegistrationSerializer(data=self._payload(agent_count=5))
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data['agent_count'], 5)

    def test_ten_agents_still_accepted(self):
        s = TenantRegistrationSerializer(data=self._payload(agent_count=10))
        self.assertTrue(s.is_valid(), s.errors)

    def test_four_agents_rejected(self):
        s = TenantRegistrationSerializer(data=self._payload(agent_count=4))
        self.assertFalse(s.is_valid())
        self.assertIn('agent_count', s.errors)

    def test_seven_agents_rejected_not_multiple_of_five(self):
        s = TenantRegistrationSerializer(data=self._payload(agent_count=7))
        self.assertFalse(s.is_valid())
        self.assertIn('agent_count', s.errors)
        self.assertIn('multiple of 5', str(s.errors['agent_count'][0]))

    def test_default_is_five(self):
        payload = self._payload()
        del payload['agent_count']
        s = TenantRegistrationSerializer(data=payload)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data['agent_count'], MIN_AGENT_COUNT)

    def test_crm_pricing_normalises_to_minimum(self):
        s = TenantRegistrationSerializer(data=self._payload(pricing_model='crm', agent_count=20))
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data['agent_count'], MIN_AGENT_COUNT)


class TestUpdateAgentCountMinFive(SubscriptionTestMixin, EchoDeskTenantTestCase):
    """PUT /api/subscription/agent-count/"""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='agmin5@test.com')
        self.sub = self._make_subscription(agent_count=10)
        self.feat = self._make_feature('base_min5', name='Base', price=Decimal('5.00'))
        self.sub.selected_features.add(self.feat)

    def test_five_accepted(self):
        resp = self.api_put(AGENT_COUNT_URL, {'agent_count': 5}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['new_agent_count'], 5)
        self.assertEqual(resp.data['new_monthly_cost'], 25.0)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.agent_count, 5)

    def test_four_rejected(self):
        resp = self.api_put(AGENT_COUNT_URL, {'agent_count': 4}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['error'], AGENT_COUNT_ERROR)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.agent_count, 10)

    def test_seven_rejected_not_multiple_of_five(self):
        resp = self.api_put(AGENT_COUNT_URL, {'agent_count': 7}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('multiple of 5', resp.data['error'])
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.agent_count, 10)

    def test_string_rejected(self):
        resp = self.api_put(AGENT_COUNT_URL, {'agent_count': '10'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
