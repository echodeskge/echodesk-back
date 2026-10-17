"""
Review fixes on the P4 e-commerce parity work:

1. Double restock: Order.cancel() treats cancelled AND refunded as final
   under its row lock; _apply_order_status, PATCH, refund, mark-paid and the
   Quickshipper webhook re-read the status under select_for_update. Each
   race is reproduced with a refund/cancel that commits between the view's
   lookup and its write: stock and the promo use are given back exactly once.
2. BOG / TBC / Flitt callbacks never mark a refunded order paid (or failed).
3. Cancelling a PAID order needs e-commerce manager rights (update_status
   and bulk); fulfilment-level users get 403.
4. products bulk-update validates product_ids and quantity.
5. EcommerceDeleteConflict is one schema component.
6. stock=low|out|low_or_out filter; the low-stock digest link opens exactly
   the digest's products; low_stock=true keeps its meaning.
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qsl, urlsplit

from django.db.models.query import QuerySet
from rest_framework import status

from ecommerce_crm.models import EcommerceSettings, Order
from ecommerce_crm.notifications import LOW_STOCK_LIST_LINK
from ecommerce_crm.tests.test_p4_admin_parity import P4Base, PROD_URL, ORDER_URL, _results
from ecommerce_crm.tests.test_p4_notifications import NotificationBase
from ecommerce_crm.views import (
    OrderStatusError,
    OrderViewSet,
    _apply_order_status,
    _refund_order,
)
from users.models import Notification

QS_WEBHOOK = '/api/ecommerce/shipping-webhook/quickshipper/'
BOG_WEBHOOK = '/api/ecommerce/payment-webhook/'
TBC_WEBHOOK = '/api/ecommerce/payment-webhook/tbc/'
FLITT_WEBHOOK = '/api/ecommerce/payment-webhook/flitt/'


def _racing_get_object(action):
    """Patch OrderViewSet.get_object so ``action(order_pk)`` commits right
    after the view loaded its (now stale) copy of the order."""
    original = OrderViewSet.get_object

    def racing(view):
        obj = original(view)
        action(obj.pk)
        return obj

    return patch.object(OrderViewSet, 'get_object', racing)


def _refund_pk(pk):
    _refund_order(Order.objects.get(pk=pk))


def _cancel_pk(pk):
    Order.objects.get(pk=pk).cancel(reason='other tab')


class _RaceBase(P4Base):

    def _setup_order(self, **kw):
        self.product = self._make_product(quantity=10)
        self.promo = self._promo(code=f'RACE{Order.objects.count()}', times_used=3)
        return self._make_order_with_items(product=self.product, promo_code=self.promo, **kw)

    def assertRestockedOnce(self):
        self.product.refresh_from_db()
        self.promo.refresh_from_db()
        self.assertEqual(self.product.quantity, 12)  # order had 2 items
        self.assertEqual(self.promo.times_used, 2)


# ============================================================================
# 1. Double restock race
# ============================================================================

class TestRefundCancelRace(_RaceBase):

    def test_stale_cancel_after_refund_is_noop(self):
        order = self._setup_order()
        stale = Order.objects.get(pk=order.pk)
        _refund_pk(order.pk)
        self.assertFalse(stale.cancel(reason='late'))
        self.assertEqual(stale.status, 'refunded')
        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')
        self.assertIsNone(order.cancelled_at)
        self.assertRestockedOnce()

    def test_cancel_on_refunded_instance_is_noop(self):
        order = self._setup_order()
        _refund_pk(order.pk)
        order.refresh_from_db()
        self.assertFalse(order.cancel())
        self.assertRestockedOnce()

    def test_refund_after_cancel_is_400(self):
        order = self._setup_order()
        resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'cancelled'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        resp = self.api_post(f'{ORDER_URL}{order.id}/refund/', user=self.admin)
        self.assertEqual(resp.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertRestockedOnce()

    def test_cancel_after_refund_is_400(self):
        order = self._setup_order()
        self.assertEqual(self.api_post(f'{ORDER_URL}{order.id}/refund/', user=self.admin).status_code, 200)
        resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'cancelled'}, user=self.admin)
        self.assertEqual(resp.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')
        self.assertRestockedOnce()

    def test_update_status_cancel_racing_a_refund(self):
        # Reproduction: the refund commits after update_status loaded the
        # order as pending. Before the fix this restocked twice and turned
        # refunded into cancelled.
        order = self._setup_order()
        with _racing_get_object(_refund_pk):
            resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'cancelled'}, user=self.admin)
        self.assertEqual(resp.status_code, 400, resp.data)
        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')
        self.assertRestockedOnce()

    def test_refund_racing_a_cancel(self):
        order = self._setup_order()
        with _racing_get_object(_cancel_pk):
            resp = self.api_post(f'{ORDER_URL}{order.id}/refund/', user=self.admin)
        self.assertEqual(resp.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertRestockedOnce()

    def test_update_status_shipped_racing_a_refund(self):
        order = self._setup_order()
        with _racing_get_object(_refund_pk), patch('ecommerce_crm.tasks.send_order_email.delay') as email:
            resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'shipped'}, user=self.admin)
        self.assertEqual(resp.status_code, 400)
        email.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')

    def test_stale_bulk_order_is_skipped(self):
        # bulk loads every order first and applies them one by one.
        order = self._setup_order()
        stale = Order.objects.get(pk=order.pk)
        _refund_pk(order.pk)
        with self.assertRaises(OrderStatusError):
            _apply_order_status(stale, 'cancelled', actor=self.admin)
        self.assertEqual(stale.status, 'refunded')
        self.assertRestockedOnce()

    def test_bulk_cancel_racing_a_refund(self):
        order = self._setup_order()
        original_filter = QuerySet.filter
        state = {'done': False}

        def racing_filter(qs, *args, **kwargs):
            result = original_filter(qs, *args, **kwargs)
            if qs.model is Order and 'id__in' in kwargs and not state['done']:
                state['done'] = True
                list(result)  # the view's snapshot (still pending)
                _refund_pk(order.pk)
            return result

        with patch.object(QuerySet, 'filter', racing_filter):
            resp = self.api_post(f'{ORDER_URL}bulk-update-status/', {
                'order_ids': [order.id], 'status': 'cancelled',
            }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['updated'], 0)
        self.assertEqual([s['id'] for s in resp.data['skipped']], [order.id])
        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')
        self.assertRestockedOnce()

    def test_patch_racing_a_refund_keeps_refunded(self):
        order = self._setup_order()
        with _racing_get_object(_refund_pk):
            resp = self.api_patch(f'{ORDER_URL}{order.id}/', {'admin_notes': 'note'}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')
        self.assertEqual(order.payment_status, 'refunded')
        self.assertEqual(order.admin_notes, 'note')

    def test_patch_cancel_racing_a_refund(self):
        order = self._setup_order()
        with _racing_get_object(_refund_pk):
            resp = self.api_patch(f'{ORDER_URL}{order.id}/', {'status': 'cancelled'}, user=self.admin)
        self.assertEqual(resp.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')
        self.assertRestockedOnce()

    def test_mark_paid_racing_a_refund(self):
        order = self._setup_order()
        with _racing_get_object(_refund_pk):
            resp = self.api_post(f'{ORDER_URL}{order.id}/mark-paid/', user=self.admin)
        self.assertEqual(resp.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'refunded')
        self.assertEqual(order.status, 'refunded')

    def _quickshipper(self, order, courier_status):
        return self.client.post(
            QS_WEBHOOK,
            {'integrationOrderId': order.order_number, 'status': courier_status, 'orderId': 99},
            format='json', HTTP_HOST='tenant.test.com',
        )

    def test_quickshipper_cancel_on_refunded_order_is_noop(self):
        EcommerceSettings.objects.create(tenant=self.tenant)
        order = self._setup_order()
        _refund_pk(order.pk)
        resp = self._quickshipper(order, 'Cancelled')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['order_status'], 'refunded')
        self.assertRestockedOnce()

    def test_quickshipper_cancel_racing_a_refund(self):
        # The refund commits after the webhook looked the order up.
        EcommerceSettings.objects.create(tenant=self.tenant)
        order = self._setup_order()
        original_first = QuerySet.first
        state = {'done': False}

        def racing_first(qs):
            obj = original_first(qs)
            if isinstance(obj, Order) and not state['done']:
                state['done'] = True
                _refund_pk(obj.pk)
            return obj

        with patch.object(QuerySet, 'first', racing_first):
            resp = self._quickshipper(order, 'Cancelled')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(state['done'])
        self.assertEqual(resp.data['order_status'], 'refunded')
        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')
        self.assertIsNone(order.cancelled_at)
        self.assertRestockedOnce()

    def test_quickshipper_shipped_racing_a_refund(self):
        EcommerceSettings.objects.create(tenant=self.tenant)
        order = self._setup_order()
        original_first = QuerySet.first
        state = {'done': False}

        def racing_first(qs):
            obj = original_first(qs)
            if isinstance(obj, Order) and not state['done']:
                state['done'] = True
                _refund_pk(obj.pk)
            return obj

        with patch.object(QuerySet, 'first', racing_first), \
                patch('ecommerce_crm.tasks.send_order_email.delay') as email:
            resp = self._quickshipper(order, 'PickedUp')
        self.assertEqual(resp.status_code, 200)
        email.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')


# ============================================================================
# 2. Payment callbacks never touch refunded orders
# ============================================================================

class TestPaymentWebhooksIgnoreRefunded(P4Base):

    def _refunded_order(self, **kw):
        order = self._make_order_with_items(payment_method='card', **kw)
        _refund_pk(order.pk)
        order.refresh_from_db()
        return order

    def _assert_still_refunded(self, order):
        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')
        self.assertEqual(order.payment_status, 'refunded')
        self.assertIsNone(order.paid_at)

    def test_bog_completed_ignored_for_refunded(self):
        order = self._refunded_order(payment_provider='bog')
        bog = MagicMock()
        bog.check_payment_status.return_value = {
            'status': 'completed', 'bog_status': 'completed', 'response_code': '100',
        }
        body = {
            'event': 'order_payment',
            'body': {
                'order_id': 'bog-1', 'external_order_id': order.order_number,
                'order_status': {'key': 'completed'}, 'payment_detail': {'code': '100'},
            },
        }
        with patch('ecommerce_crm.payment_utils.get_tenant_bog_service', return_value=bog), \
                patch('ecommerce_crm.tasks.send_order_email.delay') as email, \
                self.assertLogs('ecommerce_crm.views', level='ERROR') as logs, \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api_post(BOG_WEBHOOK, body)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'conflict')
        self.assertIn('refunded', resp.data['message'])
        self.assertTrue(any('REFUNDED' in line for line in logs.output))
        email.assert_not_called()
        self._assert_still_refunded(order)

    def test_bog_rejected_does_not_mark_refunded_failed(self):
        order = self._refunded_order(payment_provider='bog')
        body = {
            'event': 'order_payment',
            'body': {
                'order_id': 'bog-1', 'external_order_id': order.order_number,
                'order_status': {'key': 'rejected'}, 'payment_detail': {'code': '101'},
            },
        }
        resp = self.api_post(BOG_WEBHOOK, body)
        self.assertEqual(resp.status_code, 200)
        self._assert_still_refunded(order)

    def test_tbc_succeeded_ignored_for_refunded(self):
        order = self._refunded_order(payment_provider='tbc')
        with patch('ecommerce_crm.tasks.send_order_email.delay') as email, \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api_post(TBC_WEBHOOK, {
                'payId': 'tbc-1', 'status': 'Succeeded', 'merchantPaymentId': order.order_number,
            })
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'conflict')
        email.assert_not_called()
        self._assert_still_refunded(order)

    def test_tbc_failed_does_not_mark_refunded_failed(self):
        order = self._refunded_order(payment_provider='tbc')
        resp = self.api_post(TBC_WEBHOOK, {
            'payId': 'tbc-1', 'status': 'Failed', 'merchantPaymentId': order.order_number,
        })
        self.assertEqual(resp.status_code, 200)
        self._assert_still_refunded(order)

    def test_tbc_partially_refunded_payment_not_repaid(self):
        order = self._make_order(payment_method='card', payment_status='partially_refunded', status='delivered')
        resp = self.api_post(TBC_WEBHOOK, {
            'payId': 'tbc-2', 'status': 'Succeeded', 'merchantPaymentId': order.order_number,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'conflict')
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'partially_refunded')
        self.assertEqual(order.status, 'delivered')

    def _flitt_post(self, order, order_status):
        from tenants.payment_providers.flitt import _compute_signature
        EcommerceSettings.objects.get_or_create(tenant=self.tenant)
        body = {'order_id': order.order_number, 'order_status': order_status, 'payment_id': '999'}
        body['signature'] = _compute_signature('flitt-secret', body)
        with patch('tenants.payment_providers.flitt.FlittPaymentProvider._get_credentials',
                   return_value={'merchant_id': '1549901', 'password': 'flitt-secret'}), \
                patch('ecommerce_crm.tasks.send_order_email.delay') as email, \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api_post(FLITT_WEBHOOK, body)
        return resp, email

    def test_flitt_approved_ignored_for_refunded(self):
        order = self._refunded_order(payment_provider='flitt')
        resp, email = self._flitt_post(order, 'approved')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'conflict')
        email.assert_not_called()
        self._assert_still_refunded(order)

    def test_flitt_declined_does_not_mark_refunded_failed(self):
        order = self._refunded_order(payment_provider='flitt')
        resp, _ = self._flitt_post(order, 'declined')
        self.assertEqual(resp.status_code, 200)
        self._assert_still_refunded(order)

    def test_cancelled_order_still_reported_as_cancelled(self):
        order = self._make_order(payment_method='card', status='cancelled')
        resp = self.api_post(TBC_WEBHOOK, {
            'payId': 'tbc-3', 'status': 'Succeeded', 'merchantPaymentId': order.order_number,
        })
        self.assertEqual(resp.data['message'], 'Order was cancelled; manual reconciliation required')
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'pending')

    def test_unpaid_order_still_marked_paid(self):
        order = self._make_order(payment_method='card')
        with patch('ecommerce_crm.tasks.send_order_email.delay'), \
                patch('ecommerce_crm.tasks.book_quickshipper_courier.delay'):
            resp = self.api_post(TBC_WEBHOOK, {
                'payId': 'tbc-4', 'status': 'Succeeded', 'merchantPaymentId': order.order_number,
            })
        self.assertEqual(resp.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
        self.assertEqual(order.status, 'confirmed')


# ============================================================================
# 3. Cancelling a paid order is manager-only
# ============================================================================

class TestPaidCancelNeedsManager(_RaceBase):

    def test_agent_cannot_cancel_paid_order(self):
        agent = self._agent('fx-agent@test.com')
        order = self._setup_order(payment_status='paid', status='confirmed')
        resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'cancelled'}, user=agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('error', resp.data)
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 10)

    def test_agent_can_still_cancel_unpaid_order(self):
        agent = self._agent('fx-agent2@test.com')
        order = self._setup_order()
        resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'cancelled'}, user=agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertRestockedOnce()

    def test_agent_can_still_ship_paid_order(self):
        agent = self._agent('fx-agent3@test.com')
        order = self._setup_order(payment_status='paid', status='confirmed')
        with patch('ecommerce_crm.tasks.send_order_email.delay'):
            resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'shipped'}, user=agent)
        self.assertEqual(resp.status_code, 200)

    def test_manager_can_cancel_paid_order(self):
        manager = self.create_manager(email='fx-mgr@test.com')
        self.grant_feature_group(manager, name='fx-mgr')
        order = self._setup_order(payment_status='paid', status='confirmed')
        resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'cancelled'}, user=manager)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertRestockedOnce()

    def test_agent_paid_check_uses_fresh_row(self):
        # The order is paid between the view's lookup and the cancel.
        agent = self._agent('fx-agent4@test.com')
        order = self._setup_order()

        def pay(pk):
            Order.objects.filter(pk=pk).update(payment_status='paid')

        with _racing_get_object(pay):
            resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'cancelled'}, user=agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending')

    def test_agent_bulk_cancel_with_a_paid_order_is_403(self):
        agent = self._agent('fx-agent5@test.com')
        unpaid = self._setup_order()
        paid = self._make_order_with_items(product=self.product, payment_status='paid', status='confirmed')
        resp = self.api_post(f'{ORDER_URL}bulk-update-status/', {
            'order_ids': [unpaid.id, paid.id], 'status': 'cancelled',
        }, user=agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        unpaid.refresh_from_db()
        paid.refresh_from_db()
        self.assertEqual(unpaid.status, 'pending')
        self.assertEqual(paid.status, 'confirmed')
        # The web alias follows the same rule.
        resp = self.api_post(f'{ORDER_URL}bulk-update/', {
            'order_ids': [paid.id], 'status': 'cancelled',
        }, user=agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_agent_bulk_cancel_unpaid_ok(self):
        agent = self._agent('fx-agent6@test.com')
        unpaid = self._setup_order()
        resp = self.api_post(f'{ORDER_URL}bulk-update-status/', {
            'order_ids': [unpaid.id], 'status': 'cancelled',
        }, user=agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['updated'], 1)

    def test_admin_bulk_cancel_paid_ok(self):
        paid = self._setup_order(payment_status='paid', status='confirmed')
        resp = self.api_post(f'{ORDER_URL}bulk-update-status/', {
            'order_ids': [paid.id], 'status': 'cancelled',
        }, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['updated'], 1)
        self.assertRestockedOnce()

    def test_recancel_of_cancelled_paid_order_is_noop_not_403(self):
        agent = self._agent('fx-agent7@test.com')
        order = self._make_order(payment_status='paid', status='cancelled')
        resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'cancelled'}, user=agent)
        self.assertEqual(resp.status_code, 200)


# ============================================================================
# 4. products bulk-update validation
# ============================================================================

class TestProductBulkUpdateValidation(P4Base):

    def _bulk(self, payload):
        return self.api_post(f'{PROD_URL}bulk-update/', payload, user=self.admin)

    def test_non_integer_ids_are_400(self):
        p = self._make_product(quantity=5)
        for ids in (['abc'], [p.id, 'x'], [True], [1.5], [None], [{'id': p.id}], [0], [-3]):
            resp = self._bulk({'product_ids': ids, 'status': 'draft'})
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, ids)
        p.refresh_from_db()
        self.assertEqual(p.status, 'active')

    def test_numeric_string_ids_accepted(self):
        p = self._make_product(quantity=5)
        resp = self._bulk({'product_ids': [str(p.id)], 'status': 'draft'})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['updated'], 1)

    def test_quantity_must_be_non_bool_int_ge_0(self):
        p = self._make_product(quantity=5)
        for qty in (True, False, -1, '-2', 1.5, 'abc', [3]):
            resp = self._bulk({'product_ids': [p.id], 'quantity': qty})
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, qty)
        p.refresh_from_db()
        self.assertEqual(p.quantity, 5)

    def test_quantity_zero_and_int_accepted(self):
        p = self._make_product(quantity=5)
        resp = self._bulk({'product_ids': [p.id], 'quantity': 0})
        self.assertEqual(resp.status_code, 200, resp.data)
        p.refresh_from_db()
        self.assertEqual(p.quantity, 0)
        resp = self._bulk({'product_ids': [p.id], 'quantity': '7'})
        self.assertEqual(resp.status_code, 200, resp.data)
        p.refresh_from_db()
        self.assertEqual(p.quantity, 7)


# ============================================================================
# 5. Schema: one EcommerceDeleteConflict component
# ============================================================================

class TestDeleteConflictSchema(P4Base):

    def test_single_component_without_duplicate_warning(self):
        from drf_spectacular.drainage import GENERATOR_STATS
        from drf_spectacular.generators import SchemaGenerator

        GENERATOR_STATS.reset()
        schema = SchemaGenerator().get_schema(request=None, public=True)
        self.assertIn('EcommerceDeleteConflict', schema['components']['schemas'])
        dupes = [w for w in GENERATOR_STATS._warn_cache if 'EcommerceDeleteConflict' in str(w)]
        self.assertEqual(dupes, [])


# ============================================================================
# 6. Low-stock links agree with the digest
# ============================================================================

class TestStockFilterAndDigestLink(NotificationBase):

    def setUp(self):
        super().setUp()
        self.low = self._make_product(sku='ST-LOW', quantity=2, low_stock_threshold=5)
        self.zero = self._make_product(sku='ST-ZERO', quantity=0, low_stock_threshold=5)
        self.oversold = self._make_product(sku='ST-NEG', quantity=-1, low_stock_threshold=5)
        self.at_threshold = self._make_product(sku='ST-EQ', quantity=5, low_stock_threshold=5)
        self.healthy = self._make_product(sku='ST-OK', quantity=50, low_stock_threshold=5)
        self.untracked = self._make_product(sku='ST-UNTR', quantity=0, track_inventory=False)
        self.inactive_low = self._make_product(sku='ST-INACT', quantity=1, low_stock_threshold=5, status='inactive')

    def _ids(self, query):
        resp = self.api_get(f'{PROD_URL}?{query}&page_size=100', user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        return {p['id'] for p in _results(resp)}

    def test_stock_values(self):
        self.assertEqual(self._ids('stock=low'), {self.low.id, self.at_threshold.id, self.inactive_low.id})
        self.assertEqual(self._ids('stock=out'), {self.zero.id, self.oversold.id})
        self.assertEqual(
            self._ids('stock=low_or_out'),
            {self.low.id, self.zero.id, self.oversold.id, self.at_threshold.id, self.inactive_low.id},
        )

    def test_low_stock_true_meaning_unchanged(self):
        self.assertEqual(self._ids('low_stock=true'), {self.low.id, self.at_threshold.id, self.inactive_low.id})

    def test_invalid_stock_is_400(self):
        resp = self.api_get(f'{PROD_URL}?stock=lots', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_digest_link_lists_exactly_the_digest(self):
        from ecommerce_crm.tasks import check_low_stock_products
        with patch('ecommerce_crm.email_utils.send_email', return_value=True):
            check_low_stock_products()
        n = Notification.objects.get(notification_type='ecommerce_low_stock', user=self.admin)
        self.assertEqual(n.link_url, LOW_STOCK_LIST_LINK)
        digest_ids = set(n.metadata['product_ids'])
        self.assertEqual(digest_ids, {self.low.id, self.zero.id, self.oversold.id, self.at_threshold.id})
        query = urlsplit(n.link_url).query
        self.assertEqual(dict(parse_qsl(query)), {'stock': 'low_or_out', 'status': 'active'})
        self.assertEqual(self._ids(query), digest_ids)

    def test_sold_out_only_digest_is_not_an_empty_list(self):
        for p in (self.low, self.at_threshold):
            p.quantity = 40
            p.save()
        from ecommerce_crm.tasks import check_low_stock_products
        with patch('ecommerce_crm.email_utils.send_email', return_value=True):
            check_low_stock_products()
        n = Notification.objects.get(notification_type='ecommerce_low_stock', user=self.admin)
        self.assertEqual(self._ids(urlsplit(n.link_url).query), {self.zero.id, self.oversold.id})
