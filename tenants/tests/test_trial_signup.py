"""
Pricing decision D1 (2026-11-11): POST /api/register/ is the real 30-day
free trial with no card. A card is added in-app before the trial ends; the
existing trial machinery charges it at trial end.

Covers:
  1. register_tenant  -> trial TenantSubscription + background provisioning
  2. add_new_card + BOG webhook 'add_card' -> subscription.parent_order_id
  3. process_trial_expirations (charge / deactivate) + the 'trial_conversion'
     webhook branch; check_subscription_status trial reminders
  4. GET /api/subscription/me/ trial fields
  5. SubscriptionMiddleware: an expired trial without a card is blocked the
     same way as an expired (suspended) paid subscription
"""
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.hashers import check_password
from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIRequestFactory

from users.tests.conftest import EchoDeskTenantTestCase
from tenants.feature_models import Feature
from tenants.models import (
    Invoice, PaymentOrder, PendingRegistration, SavedCard, SubscriptionEvent,
    Tenant, TenantSubscription,
)
from tenants.pricing_rules import TRIAL_DAYS
from tenants.subscription_middleware import SubscriptionMiddleware
from tenants.views import register_tenant

WEBHOOK_URL = '/api/payments/webhook/'
ADD_CARD_URL = '/api/payments/saved-card/add/'
SUBSCRIPTION_ME_URL = '/api/subscription/me/'

BOG_CONFIRMED = {'status': 'paid', 'bog_status': 'completed', 'response_code': '100'}


def _completed_webhook(external_order_id, bog_order_id, saved_card_type='subscription'):
    return {
        'event': 'order_payment',
        'body': {
            'order_id': bog_order_id,
            'external_order_id': external_order_id,
            'order_status': {'key': 'completed'},
            'payment_detail': {
                'code': '100',
                'transaction_id': f'tx-{uuid.uuid4().hex[:8]}',
                'saved_card_type': saved_card_type,
                'card_type': 'visa',
                'payer_identifier': '411111***1111',
                'card_expiry_date': '12/28',
                'transfer_amount': '0.00',
            },
        },
    }


class TrialMixin:

    def _make_feature(self, key, price='9.00'):
        return Feature.objects.create(
            key=key, name=key.title(), price_per_user_gel=Decimal(price),
            price_unlimited_gel=Decimal('0'), category='integration', is_active=True,
        )

    def _trial_subscription(self, days_left=20, **kwargs):
        ends = timezone.now() + timedelta(days=days_left)
        defaults = {
            'tenant': self.tenant,
            'is_active': True,
            'starts_at': ends - timedelta(days=TRIAL_DAYS),
            'expires_at': ends,
            'next_billing_date': ends,
            'agent_count': 10,
            'is_trial': True,
            'trial_ends_at': ends,
            'trial_converted': False,
            'subscription_type': 'trial',
            'parent_order_id': None,
        }
        defaults.update(kwargs)
        return TenantSubscription.objects.create(**defaults)


# ============================================================================
# 1. POST /api/register/
# ============================================================================

class TestRegisterTrial(TrialMixin, EchoDeskTenantTestCase):
    """The view runs on the public schema and provisions in the background."""

    def setUp(self):
        super().setUp()
        self.feature = self._make_feature('tickets_trial')
        self.feature2 = self._make_feature('bookings_trial', price='15.00')
        self.factory = APIRequestFactory()

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def _payload(self, **overrides):
        data = {
            'company_name': 'Trial Co',
            'domain': 'trial-co',
            'feature_ids': [self.feature.id, self.feature2.id],
            'pricing_model': 'agent',
            'agent_count': 10,
            'admin_email': 'owner@trial.example',
            'admin_password': 'Strong1Password',
            'admin_first_name': 'Nino',
            'admin_last_name': 'B',
            'preferred_language': 'ka',
        }
        data.update(overrides)
        return data

    def _register(self, **overrides):
        connection.set_schema_to_public()
        request = self.factory.post('/api/register/', self._payload(**overrides), format='json')
        with mock.patch('tenants.views.trigger_tenant_processing') as trigger:
            response = register_tenant(request)
        return response, trigger

    def test_creates_no_card_30_day_trial(self):
        before = timezone.now()
        response, trigger = self._register()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        tenant = Tenant.objects.get(schema_name='trial_co')
        sub = TenantSubscription.objects.get(tenant=tenant)

        self.assertTrue(sub.is_trial)
        self.assertFalse(sub.trial_converted)
        self.assertEqual(sub.subscription_type, 'trial')
        self.assertIsNone(sub.parent_order_id)
        self.assertIsNone(sub.last_billed_at)
        self.assertEqual(sub.agent_count, 10)

        expected_end = sub.starts_at + timedelta(days=TRIAL_DAYS)
        self.assertEqual(sub.trial_ends_at, expected_end)
        self.assertEqual(sub.expires_at, expected_end)
        self.assertEqual(sub.next_billing_date, expected_end)
        self.assertGreaterEqual(sub.starts_at, before)
        self.assertEqual((sub.trial_ends_at - before).days, TRIAL_DAYS)

        self.assertEqual(set(sub.selected_features.values_list('id', flat=True)),
                         {self.feature.id, self.feature2.id})
        self.assertEqual(float(sub.monthly_cost), 240.0)  # (9 + 15) x 10

    def test_limits_match_the_paid_path(self):
        response, _ = self._register(agent_count=15)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        tenant = Tenant.objects.get(schema_name='trial_co')
        self.assertEqual(tenant.max_users, 15)          # not the old 1000 placeholder
        self.assertEqual(tenant.max_storage, 100 * 1024)
        self.assertTrue(tenant.is_active)
        self.assertEqual(response.data['subscription']['limits']['max_users'], 15)

    def test_provisions_in_the_background_like_the_paid_path(self):
        response, trigger = self._register()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        tenant = Tenant.objects.get(schema_name='trial_co')

        # Schema + admin user are created by process_pending_tenants, which
        # reads the processed PendingRegistration (hashed password).
        self.assertEqual(tenant.deployment_status, 'deploying')
        trigger.assert_called_once_with('trial_co')
        pending = PendingRegistration.objects.get(schema_name='trial_co')
        self.assertTrue(pending.is_processed)
        self.assertEqual(pending.admin_email, 'owner@trial.example')
        self.assertEqual(pending.agent_count, 10)
        self.assertTrue(pending.order_id.startswith('TRIAL-'))
        self.assertTrue(check_password('Strong1Password', pending.admin_password))
        self.assertNotEqual(pending.admin_password, 'Strong1Password')
        self.assertEqual(pending.selected_features.count(), 2)
        self.assertEqual(tenant.frontend_url, 'https://trial_co.echodesk.ge')

    def test_response_shape_and_trial_ends_at(self):
        response, _ = self._register()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        data = response.data
        for key in ('message', 'tenant', 'subscription', 'trial_ends_at', 'domain_url',
                    'admin_email', 'preferred_language', 'login_url', 'api_url',
                    'frontend_url', 'deployment_status', 'credentials'):
            self.assertIn(key, data, key)
        sub = TenantSubscription.objects.get(tenant__schema_name='trial_co')
        self.assertEqual(data['trial_ends_at'], sub.trial_ends_at)
        self.assertTrue(data['subscription']['is_trial'])
        self.assertEqual(data['subscription']['trial_ends_at'], sub.trial_ends_at)
        self.assertEqual(data['subscription']['trial_expires'], sub.trial_ends_at)
        self.assertEqual(data['subscription']['agent_count'], 10)
        self.assertEqual(data['subscription']['monthly_cost'], 240.0)
        self.assertEqual(data['tenant']['schema'], 'trial_co')
        self.assertEqual(data['tenant']['deployment_status'], 'deploying')
        self.assertEqual(data['frontend_url'], 'https://trial_co.echodesk.ge')
        self.assertEqual(data['deployment_status'], 'deploying')

    def test_duplicate_domain_rejected(self):
        first, _ = self._register()
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
        second, trigger = self._register(admin_email='other@trial.example')
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('already exists', second.data['error'])
        trigger.assert_not_called()
        self.assertEqual(Tenant.objects.filter(schema_name='trial_co').count(), 1)

    def test_agent_count_rule_still_applies(self):
        response, trigger = self._register(agent_count=7)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('agent_count', response.data)
        trigger.assert_not_called()
        self.assertFalse(Tenant.objects.filter(schema_name='trial_co').exists())


# ============================================================================
# 2. Card added in-app later
# ============================================================================

class TestAddCardDuringTrial(TrialMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.tenant.payment_provider = 'bog'
        self.tenant.save()
        self.admin = self.create_admin(email='trial-admin@test.com')
        self.sub = self._trial_subscription()

    def test_add_new_card_enables_subscription_card_saving(self):
        with mock.patch('tenants.payment_views.bog_service') as bog:
            bog.is_configured.return_value = True
            bog.create_payment.return_value = {
                'order_id': 'bog-card-1', 'payment_id': 'bog-card-1',
                'payment_url': 'https://pay.example/bog-card-1', 'metadata': {},
            }
            bog.enable_subscription_card_saving.return_value = True
            resp = self.api_post(ADD_CARD_URL, {'make_default': True}, user=self.admin)

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['amount'], 0.0)
        self.assertEqual(resp.data['payment_url'], 'https://pay.example/bog-card-1')
        _, kwargs = bog.create_payment.call_args
        self.assertEqual(kwargs['amount'], 0.0)
        self.assertEqual(kwargs['metadata']['type'], 'add_card')
        # The saved-card flag BOG needs so the webhook carries
        # saved_card_type=subscription and the order can be charged later.
        bog.enable_subscription_card_saving.assert_called_once_with('bog-card-1')

        order = PaymentOrder.objects.get(tenant=self.tenant, bog_order_id='bog-card-1')
        self.assertEqual(order.metadata['type'], 'add_card')
        self.assertTrue(order.metadata['make_default'])
        self.assertTrue(order.metadata['save_card'])
        self.assertTrue(order.metadata['card_saving_enabled'])
        self.assertEqual(order.amount, 0)
        self.assertEqual(order.status, 'pending')

    def _card_order(self, make_default=False):
        return PaymentOrder.objects.create(
            order_id=f'CARD-{uuid.uuid4().hex[:12].upper()}',
            bog_order_id=f'bog-{uuid.uuid4().hex[:8]}',
            tenant=self.tenant, amount=Decimal('0'), currency='GEL', agent_count=0,
            status='pending', metadata={'type': 'add_card', 'make_default': make_default},
        )

    def _post_webhook(self, body):
        with mock.patch('tenants.payment_views.bog_service') as bog:
            bog.check_payment_status.return_value = BOG_CONFIRMED
            return self.api_post(WEBHOOK_URL, body)

    def test_saved_card_webhook_sets_subscription_parent_order_id(self):
        order = self._card_order()
        self.assertIsNone(self.sub.parent_order_id)

        resp = self._post_webhook(_completed_webhook(order.order_id, 'bog-parent-77'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['action'], 'card_added')

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.parent_order_id, 'bog-parent-77')
        self.assertTrue(self.sub.is_trial)             # still a trial until trial_ends_at
        self.assertFalse(self.sub.trial_converted)
        self.assertEqual(self.sub.payment_status, 'current')

        card = SavedCard.objects.get(tenant=self.tenant, parent_order_id='bog-parent-77')
        self.assertTrue(card.is_active)
        self.assertTrue(card.is_default)               # first card is the billing card
        self.assertEqual(card.card_save_type, 'subscription')
        self.assertEqual(card.masked_card_number, '411111***1111')
        self.assertEqual(card.card_expiry, '12/28')
        order.refresh_from_db()
        self.assertEqual(order.status, 'paid')
        self.assertTrue(order.card_saved)

    def test_second_card_not_default_keeps_billing_card(self):
        self.sub.parent_order_id = 'bog-first'
        self.sub.save()
        SavedCard.objects.create(tenant=self.tenant, parent_order_id='bog-first', is_default=True)
        order = self._card_order(make_default=False)
        resp = self._post_webhook(_completed_webhook(order.order_id, 'bog-second'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.parent_order_id, 'bog-first')

    def test_second_card_as_default_replaces_billing_card(self):
        self.sub.parent_order_id = 'bog-first'
        self.sub.save()
        SavedCard.objects.create(tenant=self.tenant, parent_order_id='bog-first', is_default=True)
        order = self._card_order(make_default=True)
        resp = self._post_webhook(_completed_webhook(order.order_id, 'bog-second'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.parent_order_id, 'bog-second')
        self.assertEqual(SavedCard.objects.get(tenant=self.tenant, is_default=True).parent_order_id, 'bog-second')

    def test_webhook_without_bog_confirmation_changes_nothing(self):
        order = self._card_order()
        with mock.patch('tenants.payment_views.bog_service') as bog:
            bog.check_payment_status.return_value = {
                'status': 'pending', 'bog_status': 'created', 'response_code': '',
            }
            resp = self.api_post(WEBHOOK_URL, _completed_webhook(order.order_id, 'bog-forged'))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'ignored')
        self.sub.refresh_from_db()
        self.assertIsNone(self.sub.parent_order_id)
        self.assertFalse(SavedCard.objects.filter(tenant=self.tenant).exists())


# ============================================================================
# 3. Trial end: charge the card or deactivate; reminders
# ============================================================================

class TestProcessTrialExpirations(TrialMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.tenant.payment_provider = 'bog'
        self.tenant.save()
        self.feature = self._make_feature('tickets_trial_exp', price='9.00')

    def _run(self, **charge_result):
        out = StringIO()
        with mock.patch('tenants.management.commands.process_trial_expirations.bog_service') as bog:
            bog.charge_saved_card.return_value = charge_result or {'order_id': 'bog-conv-1'}
            call_command('process_trial_expirations', stdout=out)
        return out.getvalue(), bog

    def test_with_card_charges_and_converts(self):
        sub = self._trial_subscription(days_left=0, parent_order_id='bog-parent-1')
        sub.trial_ends_at = timezone.now() - timedelta(hours=1)
        sub.save()
        sub.selected_features.add(self.feature)

        before = timezone.now()
        output, bog = self._run()

        bog.charge_saved_card.assert_called_once()
        _, kwargs = bog.charge_saved_card.call_args
        self.assertEqual(kwargs['parent_order_id'], 'bog-parent-1')
        self.assertEqual(kwargs['amount'], 90.0)  # 9 GEL x 10 agents
        self.assertTrue(kwargs['external_order_id'].startswith('TRIAL-CONV-'))

        sub.refresh_from_db()
        self.assertFalse(sub.is_trial)
        self.assertTrue(sub.trial_converted)
        self.assertEqual(sub.subscription_type, 'paid')
        self.assertTrue(sub.is_active)
        self.assertIsNotNone(sub.last_billed_at)
        self.assertEqual((sub.next_billing_date - before).days, 30)
        self.assertEqual(sub.expires_at, sub.next_billing_date)
        self.assertEqual(sub.agent_count, 10)

        order = PaymentOrder.objects.get(tenant=self.tenant, order_id__startswith='TRIAL-CONV-')
        self.assertEqual(order.metadata['type'], 'trial_conversion')
        self.assertEqual(order.bog_order_id, 'bog-conv-1')
        self.assertEqual(float(order.amount), 90.0)
        self.assertEqual(order.agent_count, 10)
        self.assertTrue(SubscriptionEvent.objects.filter(subscription=sub, event_type='trial_converted').exists())
        self.assertIn('Trial converted', output)

    def test_without_card_deactivates(self):
        sub = self._trial_subscription(days_left=0)
        sub.trial_ends_at = timezone.now() - timedelta(hours=1)
        sub.save()

        output, bog = self._run()

        bog.charge_saved_card.assert_not_called()
        sub.refresh_from_db()
        self.assertFalse(sub.is_active)
        self.assertTrue(sub.is_trial)
        self.assertFalse(sub.trial_converted)
        self.assertEqual(sub.payment_status, 'no_card')
        self.assertTrue(SubscriptionEvent.objects.filter(subscription=sub, event_type='trial_expired').exists())
        self.assertIn('No saved card', output)

    def test_trial_still_running_untouched(self):
        sub = self._trial_subscription(days_left=5, parent_order_id='bog-parent-1')
        output, bog = self._run()
        bog.charge_saved_card.assert_not_called()
        sub.refresh_from_db()
        self.assertTrue(sub.is_trial)
        self.assertTrue(sub.is_active)
        self.assertIn('No trials to process', output)

    def test_charge_failure_deactivates(self):
        sub = self._trial_subscription(days_left=0, parent_order_id='bog-parent-1')
        sub.trial_ends_at = timezone.now() - timedelta(hours=1)
        sub.save()
        out = StringIO()
        with mock.patch('tenants.management.commands.process_trial_expirations.bog_service') as bog:
            bog.charge_saved_card.side_effect = ValueError('BOG down')
            call_command('process_trial_expirations', stdout=out)
        sub.refresh_from_db()
        self.assertFalse(sub.is_active)
        self.assertTrue(sub.is_trial)
        self.assertEqual(sub.payment_status, 'failed')


class TestTrialConversionWebhook(TrialMixin, EchoDeskTenantTestCase):
    """The charge fired by process_trial_expirations is settled by the webhook."""

    def setUp(self):
        super().setUp()
        self.tenant.payment_provider = 'bog'
        self.tenant.save()
        # State right after the command ran: already paid, dates advanced.
        self.sub = self._trial_subscription(
            days_left=30, is_trial=False, trial_converted=True, subscription_type='paid',
            parent_order_id='bog-parent-1', agent_count=10, last_billed_at=timezone.now(),
        )
        self.order = PaymentOrder.objects.create(
            order_id=f'TRIAL-CONV-{uuid.uuid4().hex[:12].upper()}',
            bog_order_id='bog-conv-1', tenant=self.tenant,
            amount=Decimal('90.00'), currency='GEL', agent_count=10, status='processing',
            metadata={'type': 'trial_conversion', 'parent_order_id': 'bog-parent-1'},
        )

    def test_settles_payment_without_touching_the_plan(self):
        next_billing = self.sub.next_billing_date
        with mock.patch('tenants.payment_views.bog_service') as bog, \
                mock.patch('tenants.tasks.generate_tenant_invoice_pdf'):
            bog.check_payment_status.return_value = BOG_CONFIRMED
            body = _completed_webhook(self.order.order_id, 'bog-conv-1', saved_card_type=None)
            body['body']['payment_detail']['transfer_amount'] = '90.00'
            resp = self.api_post(WEBHOOK_URL, body)

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['action'], 'trial_converted')

        self.sub.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'paid')
        self.assertEqual(self.sub.agent_count, 10)            # legacy renewal would reset to 1
        self.assertEqual(self.sub.next_billing_date, next_billing)
        self.assertEqual(self.sub.parent_order_id, 'bog-parent-1')
        self.assertFalse(self.sub.is_trial)
        self.assertTrue(self.sub.trial_converted)
        self.assertEqual(self.sub.payment_status, 'current')
        self.assertTrue(self.sub.is_active)
        invoice = Invoice.objects.get(payment_order=self.order)
        self.assertEqual(invoice.amount, Decimal('90.00'))
        self.assertEqual(invoice.agent_count, 10)
        self.assertTrue(SubscriptionEvent.objects.filter(
            subscription=self.sub, event_type='trial_converted', payment_order=self.order,
        ).exists())


class TestTrialReminders(TrialMixin, EchoDeskTenantTestCase):
    """check_subscription_status: 7/3-day reminders fire for trials and say to add a card."""

    def _at_noon_in(self, days):
        target = timezone.localdate() + timedelta(days=days)
        return timezone.make_aware(datetime.combine(target, time(12, 0)))

    def _run(self):
        out = StringIO()
        call_command('check_subscription_status', stdout=out)
        return out.getvalue()

    def test_seven_day_reminder_without_card_says_add_a_card(self):
        ends = self._at_noon_in(7)
        sub = self._trial_subscription(expires_at=ends, trial_ends_at=ends, next_billing_date=ends)
        sub.selected_features.add(self._make_feature('tickets_rem', price='9.00'))
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertIn('free trial ends in 7 days', msg.subject)
        self.assertIn('add a card', msg.subject)
        self.assertIn('No card is on file', msg.body)
        self.assertIn('add a card before', msg.body)
        self.assertIn('90.00 GEL', msg.body)
        self.assertIn('nothing is charged before the trial ends', msg.body)
        self.assertIn('/settings/subscription', msg.body)
        self.assertNotIn('subscription has expired', msg.body)
        self.assertEqual(msg.to, [self.tenant.admin_email])

    def test_three_day_reminder_with_card_says_charged_automatically(self):
        ends = self._at_noon_in(3)
        self._trial_subscription(expires_at=ends, trial_ends_at=ends, next_billing_date=ends,
                                 parent_order_id='bog-parent-1')
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertIn('free trial ends in 3 days', msg.subject)
        self.assertNotIn('add a card', msg.subject)
        self.assertIn('charged', msg.body)
        self.assertIn('automatically', msg.body)
        self.assertNotIn('No card is on file', msg.body)

    def test_paid_subscription_copy_unchanged(self):
        ends = self._at_noon_in(7)
        TenantSubscription.objects.create(
            tenant=self.tenant, is_active=True, starts_at=timezone.now() - timedelta(days=60),
            expires_at=ends, agent_count=5, payment_status='current',
        )
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('subscription expires in 7 days', mail.outbox[0].subject)
        self.assertNotIn('trial', mail.outbox[0].body.lower())


# ============================================================================
# 4. GET /api/subscription/me/
# ============================================================================

class TestSubscriptionMeTrialFields(TrialMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='subme-trial@test.com')

    def _me(self):
        resp = self.api_get(SUBSCRIPTION_ME_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data['has_subscription'])
        return resp.data['subscription']

    def test_trial_without_card(self):
        ends = timezone.now() + timedelta(days=10, hours=12)
        self._trial_subscription(expires_at=ends, trial_ends_at=ends, next_billing_date=ends)
        sub = self._me()
        self.assertTrue(sub['is_trial'])
        self.assertEqual(sub['trial_ends_at'], ends)
        self.assertEqual(sub['trial_days_remaining'], 11)   # a started day counts
        self.assertIsInstance(sub['trial_days_remaining'], int)
        self.assertFalse(sub['has_saved_card'])

    def test_trial_with_card(self):
        self._trial_subscription(days_left=20, parent_order_id='bog-parent-1')
        sub = self._me()
        self.assertTrue(sub['is_trial'])
        self.assertTrue(sub['has_saved_card'])
        self.assertEqual(sub['trial_days_remaining'], 20)

    def test_ended_trial_never_negative(self):
        ends = timezone.now() - timedelta(days=3)
        self._trial_subscription(expires_at=ends, trial_ends_at=ends, next_billing_date=ends)
        sub = self._me()
        self.assertEqual(sub['trial_days_remaining'], 0)

    def test_paid_subscription(self):
        TenantSubscription.objects.create(
            tenant=self.tenant, is_active=True, starts_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=30), agent_count=5,
            parent_order_id='bog-parent-1', payment_status='current',
        )
        sub = self._me()
        self.assertFalse(sub['is_trial'])
        self.assertIsNone(sub['trial_ends_at'])
        self.assertEqual(sub['trial_days_remaining'], 0)
        self.assertTrue(sub['has_saved_card'])


# ============================================================================
# 5. Subscription middleware
# ============================================================================

class TestMiddlewareExpiredTrial(TrialMixin, EchoDeskTenantTestCase):
    """
    The middleware attaches the active subscription (and its features) or
    nothing. Expiry itself is enforced by the nightly jobs that deactivate:
    check_subscription_status for a paid subscription (after its grace
    period), process_trial_expirations for a trial with no card (on the
    day). Both end in the same block: no subscription, no features.
    """

    def setUp(self):
        super().setUp()
        self.feature = self._make_feature('tickets_mw', price='9.00')
        self.middleware = SubscriptionMiddleware(lambda request: request)

    def _attach(self):
        request = RequestFactory().get('/api/tickets/')
        request.tenant = self.tenant
        self.middleware._attach_subscription_info(request)
        return request

    def test_running_trial_has_its_features(self):
        sub = self._trial_subscription(days_left=20)
        sub.selected_features.add(self.feature)
        request = self._attach()
        self.assertEqual(request.subscription, sub)
        self.assertTrue(request.has_feature('tickets_mw'))
        self.assertTrue(request.tenant_has_feature('tickets_mw'))

    def test_expired_trial_without_card_is_blocked_like_an_expired_subscription(self):
        sub = self._trial_subscription(days_left=0)
        sub.trial_ends_at = timezone.now() - timedelta(hours=1)
        sub.expires_at = sub.trial_ends_at
        sub.save()
        sub.selected_features.add(self.feature)

        with mock.patch('tenants.management.commands.process_trial_expirations.bog_service'):
            call_command('process_trial_expirations', stdout=StringIO())

        request = self._attach()
        self.assertIsNone(request.subscription)
        self.assertEqual(request.subscription_features, {})
        self.assertFalse(request.has_feature('tickets_mw'))
        self.assertFalse(request.tenant_has_feature('tickets_mw'))

    def test_expired_paid_subscription_gets_the_same_block(self):
        sub = TenantSubscription.objects.create(
            tenant=self.tenant, is_active=True, starts_at=timezone.now() - timedelta(days=90),
            expires_at=timezone.now() - timedelta(days=30), agent_count=5, payment_status='current',
        )
        sub.selected_features.add(self.feature)
        call_command('check_subscription_status', stdout=StringIO())

        request = self._attach()
        self.assertIsNone(request.subscription)
        self.assertEqual(request.subscription_features, {})
        self.assertFalse(request.has_feature('tickets_mw'))

    def test_expired_trial_with_card_keeps_access(self):
        sub = self._trial_subscription(days_left=0, parent_order_id='bog-parent-1')
        sub.trial_ends_at = timezone.now() - timedelta(hours=1)
        sub.expires_at = sub.trial_ends_at
        sub.save()
        sub.selected_features.add(self.feature)
        with mock.patch('tenants.management.commands.process_trial_expirations.bog_service') as bog:
            bog.charge_saved_card.return_value = {'order_id': 'bog-conv-1'}
            call_command('process_trial_expirations', stdout=StringIO())

        request = self._attach()
        self.assertIsNotNone(request.subscription)
        self.assertFalse(request.subscription.is_trial)
        self.assertTrue(request.has_feature('tickets_mw'))
