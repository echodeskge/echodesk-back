"""
"Clean order list": reversible soft delete of orders.

- POST /admin/orders/clean/ (all / by status / created_before / dry_run),
  store admins only
- hidden orders vanish from the admin list, export, analytics, bulk status
  updates, the storefront history, the public-token lookup and the
  open-orders delete guard; retrieve still shows them (with is_deleted)
  and every mutation 404s
- webhooks still find hidden orders through the unfiltered manager
- ?cleaned=true lists them with deleted_by_name
- restore by ids / all / single
- shop clients cannot PUT/PATCH/DELETE their orders (security fix)
"""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from ecommerce_crm.models import EcommerceSettings, Order, OrderItem, Product
from ecommerce_crm.tests.base import EcommerceTenantTestCase
from ecommerce_crm.tests.test_order_views import OrderTestMixin, _results
from ecommerce_crm.tokens import issue_shop_tokens

ORDER_URL = '/api/ecommerce/admin/orders/'
CLEAN_URL = f'{ORDER_URL}clean/'
RESTORE_URL = f'{ORDER_URL}restore/'
PROD_URL = '/api/ecommerce/admin/products/'
CLIENT_ORDERS_URL = '/api/ecommerce/client/orders/'
BY_TOKEN_URL = f'{CLIENT_ORDERS_URL}by-token/'
QS_WEBHOOK = '/api/ecommerce/shipping-webhook/quickshipper/'


class CleanBase(OrderTestMixin, EcommerceTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_user(
            email='clean-admin@test.com', role='admin', is_staff=True,
            first_name='Ann', last_name='Admin',
        )
        self.manager = self.create_manager(email='clean-manager@test.com')
        self.grant_feature_group(self.manager, name='clean-managers')

    def _clean(self, data=None, user=None):
        return self.api_post(CLEAN_URL, data if data is not None else {}, user=user or self.admin)

    def _hide(self, *orders):
        """Hide orders directly (what a successful clean does)."""
        Order.objects.filter(pk__in=[o.pk for o in orders]).update(
            is_deleted=True, deleted_at=timezone.now(), deleted_by=self.admin,
        )
        for o in orders:
            o.refresh_from_db()

    @staticmethod
    def _set_created(order, dt):
        Order.objects.filter(pk=order.pk).update(created_at=dt)
        order.refresh_from_db()

    @staticmethod
    def _shopper_api(client):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {issue_shop_tokens(client).access_token}')
        return api

    def _ids(self, resp):
        return {o['id'] for o in _results(resp)}


# ============================================================================
# POST /clean/
# ============================================================================

class TestCleanAction(CleanBase):

    def test_store_admin_only(self):
        order = self._make_order()
        agent = self.create_user(email='clean-agent@test.com', role='agent')
        self.grant_feature_group(agent, name='clean-agents')
        for user in (self.manager, agent):
            for url, body in ((CLEAN_URL, {}), (RESTORE_URL, {'all': True}),
                              (f'{ORDER_URL}{order.id}/restore/', {})):
                resp = self.api_post(url, body, user=user)
                self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, (user.email, url, resp.data))
        order.refresh_from_db()
        self.assertFalse(order.is_deleted)
        # unauthenticated
        self.assertIn(self.api_post(CLEAN_URL, {}).status_code, (401, 403))

    def test_clean_everything(self):
        orders = [self._make_order(status=s) for s in ('pending', 'delivered', 'cancelled')]
        before = timezone.now()
        resp = self._clean()
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data, {'cleaned': 3})
        for o in orders:
            o.refresh_from_db()
            self.assertTrue(o.is_deleted)
            self.assertIsNotNone(o.deleted_at)
            self.assertGreaterEqual(o.deleted_at, before)
            self.assertEqual(o.deleted_by_id, self.admin.id)
        # Nothing was hard-deleted and the items are still there.
        self.assertEqual(Order.objects.count(), 3)
        # A second clean has nothing left to hide.
        self.assertEqual(self._clean().data, {'cleaned': 0})

    def test_clean_by_status(self):
        pending = self._make_order(status='pending')
        delivered = self._make_order(status='delivered')
        cancelled = self._make_order(status='cancelled')
        resp = self._clean({'status': ['delivered', 'cancelled']})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['cleaned'], 2)
        for o in (pending, delivered, cancelled):
            o.refresh_from_db()
        self.assertFalse(pending.is_deleted)
        self.assertTrue(delivered.is_deleted)
        self.assertTrue(cancelled.is_deleted)
        # An empty list means "any status".
        self.assertEqual(self._clean({'status': []}).data['cleaned'], 1)

    def test_created_before_is_inclusive_of_that_day(self):
        on_day = self._make_order()
        self._set_created(on_day, timezone.make_aware(datetime(2026, 1, 15, 23, 30)))
        day_after = self._make_order()
        self._set_created(day_after, timezone.make_aware(datetime(2026, 1, 16, 0, 0)))
        day_before = self._make_order()
        self._set_created(day_before, timezone.make_aware(datetime(2026, 1, 14, 9, 0)))

        resp = self._clean({'created_before': '2026-01-15'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['cleaned'], 2)
        for o in (on_day, day_after, day_before):
            o.refresh_from_db()
        self.assertTrue(on_day.is_deleted)
        self.assertTrue(day_before.is_deleted)
        self.assertFalse(day_after.is_deleted)

    def test_filters_combine(self):
        old_cancelled = self._make_order(status='cancelled')
        self._set_created(old_cancelled, timezone.now() - timedelta(days=40))
        old_pending = self._make_order(status='pending')
        self._set_created(old_pending, timezone.now() - timedelta(days=40))
        new_cancelled = self._make_order(status='cancelled')
        cutoff = (timezone.now() - timedelta(days=30)).date().isoformat()
        resp = self._clean({'status': ['cancelled'], 'created_before': cutoff})
        self.assertEqual(resp.data, {'cleaned': 1})
        old_cancelled.refresh_from_db()
        self.assertTrue(old_cancelled.is_deleted)
        for o in (old_pending, new_cancelled):
            o.refresh_from_db()
            self.assertFalse(o.is_deleted)

    def test_dry_run_counts_without_changing_anything(self):
        self._make_order(status='pending')
        self._make_order(status='delivered')
        resp = self._clean({'dry_run': True, 'status': ['delivered']})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data, {'count': 1})
        self.assertEqual(Order.objects.filter(is_deleted=True).count(), 0)
        self.assertEqual(self._clean({'dry_run': True}).data, {'count': 2})
        # The dry-run count is what the (visible) list shows.
        self.assertEqual(len(_results(self.api_get(ORDER_URL, user=self.admin))), 2)

    def test_invalid_status_or_date_is_400(self):
        self._make_order()
        for body in ({'status': ['bogus']}, {'status': 'pending'}, {'created_before': '15/01/2026'},
                     {'created_before': 'yesterday'}, {'dry_run': 'maybe'}):
            resp = self._clean(body)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, body)
        self.assertEqual(Order.objects.filter(is_deleted=True).count(), 0)


# ============================================================================
# Hidden orders drop out of every staff/customer read
# ============================================================================

class TestHiddenEverywhere(CleanBase):

    def test_list_hides_cleaned_and_cleaned_flag_lists_them(self):
        visible = self._make_order()
        hidden = self._make_order()
        self._hide(hidden)

        resp = self.api_get(ORDER_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(resp), {visible.id})
        row = _results(resp)[0]
        self.assertFalse(row['is_deleted'])
        self.assertIsNone(row['deleted_at'])
        self.assertEqual(row['deleted_by_name'], '')

        resp = self.api_get(ORDER_URL + '?cleaned=true', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(self._ids(resp), {hidden.id})
        row = _results(resp)[0]
        self.assertTrue(row['is_deleted'])
        self.assertIsNotNone(row['deleted_at'])
        self.assertEqual(row['deleted_by_name'], 'Ann Admin')

        # cleaned=false and other spellings of true behave the same.
        self.assertEqual(self._ids(self.api_get(ORDER_URL + '?cleaned=false', user=self.admin)), {visible.id})
        self.assertEqual(self._ids(self.api_get(ORDER_URL + '?cleaned=1', user=self.admin)), {hidden.id})
        # Other filters still apply on the cleaned side.
        resp = self.api_get(ORDER_URL + '?cleaned=true&status=delivered', user=self.admin)
        self.assertEqual(self._ids(resp), set())

    def test_retrieve_shows_the_cleaned_order_but_mutations_404(self):
        hidden = self._make_order_with_items(status='pending')
        self._hide(hidden)

        resp = self.api_get(f'{ORDER_URL}{hidden.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data['is_deleted'])
        self.assertEqual(resp.data['deleted_by_name'], 'Ann Admin')

        with patch('ecommerce_crm.tasks.send_order_email.delay'):
            checks = [
                self.api_post(f'{ORDER_URL}{hidden.id}/update_status/', {'status': 'confirmed'}, user=self.admin),
                self.api_post(f'{ORDER_URL}{hidden.id}/mark-paid/', user=self.admin),
                self.api_post(f'{ORDER_URL}{hidden.id}/refund/', user=self.admin),
                self.api_post(f'{ORDER_URL}{hidden.id}/initiate_payment/', user=self.admin),
                self.api_patch(f'{ORDER_URL}{hidden.id}/', {'admin_notes': 'x'}, user=self.admin),
                self.api_delete(f'{ORDER_URL}{hidden.id}/', user=self.admin),
            ]
        for resp in checks:
            self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        hidden.refresh_from_db()
        self.assertEqual(hidden.status, 'pending')
        self.assertEqual(hidden.payment_status, 'pending')
        self.assertEqual(hidden.admin_notes, '')

    def test_bulk_update_status_rejects_cleaned_orders(self):
        visible = self._make_order()
        hidden = self._make_order()
        self._hide(hidden)
        with patch('ecommerce_crm.tasks.send_order_email.delay'):
            resp = self.api_post(f'{ORDER_URL}bulk-update-status/',
                                 {'order_ids': [visible.id, hidden.id], 'status': 'confirmed'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)
        self.assertIn(str(hidden.id), resp.data['error'])
        for o in (visible, hidden):
            o.refresh_from_db()
            self.assertEqual(o.status, 'pending')

    def test_export_csv_omits_cleaned_orders(self):
        visible = self._make_order()
        hidden = self._make_order()
        self._hide(hidden)
        resp = self.api_get(f'{ORDER_URL}export/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.content.decode()
        self.assertIn(visible.order_number, body)
        self.assertNotIn(hidden.order_number, body)
        self.assertEqual(len([line for line in body.splitlines() if line.strip()]), 2)  # header + 1

    def test_analytics_ignore_cleaned_orders_and_top_products_ignore_cancelled(self):
        product = self._make_product(name={'en': 'Counted'})
        other = self._make_product(name={'en': 'Hidden only'})
        cancelled_only = self._make_product(name={'en': 'Cancelled only'})
        client = self._make_client(email='returning@test.com')
        addr = self._make_address(client)

        kept = self._make_order_with_items(client=client, address=addr, product=product,
                                           status='delivered', payment_status='paid',
                                           total_amount=Decimal('60.00'))
        kept2 = self._make_order_with_items(client=client, address=addr, product=product,
                                            status='confirmed', payment_status='paid',
                                            total_amount=Decimal('60.00'))
        hidden = self._make_order_with_items(client=client, address=addr, product=other,
                                             status='delivered', payment_status='paid',
                                             total_amount=Decimal('1000.00'))
        cancelled = self._make_order_with_items(product=cancelled_only, status='cancelled',
                                                payment_status='paid', total_amount=Decimal('500.00'))
        self._hide(hidden)

        resp = self.api_get(f'{ORDER_URL}analytics/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        data = resp.data
        self.assertEqual(data['orders']['total'], 2)
        self.assertEqual(data['revenue']['total'], 120.0)
        self.assertEqual(data['order_status_breakdown'], {'delivered': 1, 'confirmed': 1})
        self.assertEqual([p['product__name'] for p in data['top_products']], [{'en': 'Counted'}])
        self.assertEqual(data['top_products'][0]['sold'], 4)
        self.assertEqual({o['id'] for o in data['recent_orders']}, {kept.id, kept2.id})
        self.assertNotIn(cancelled.id, {o['id'] for o in data['recent_orders']})
        # Still a returning customer thanks to the two visible orders...
        self.assertEqual(data['customer_insights']['returning'], 1)
        # ...but not once one of them is cleaned (the hidden one never counted).
        self._hide(kept2)
        data = self.api_get(f'{ORDER_URL}analytics/', user=self.admin).data
        self.assertEqual(data['customer_insights']['returning'], 0)
        self.assertEqual(data['orders']['total'], 1)

    def test_storefront_history_and_detail_hide_cleaned_orders(self):
        client = self._make_client(email='shopper-clean@test.com')
        addr = self._make_address(client)
        visible = self._make_order(client=client, address=addr)
        hidden = self._make_order(client=client, address=addr)
        self._hide(hidden)
        api = self._shopper_api(client)

        resp = api.get(CLIENT_ORDERS_URL, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(self._ids(resp), {visible.id})
        self.assertEqual(api.get(f'{CLIENT_ORDERS_URL}{hidden.id}/', HTTP_HOST='tenant.test.com').status_code, 404)
        self.assertEqual(api.get(f'{CLIENT_ORDERS_URL}{visible.id}/', HTTP_HOST='tenant.test.com').status_code, 200)
        resp = api.post(f'{CLIENT_ORDERS_URL}{hidden.id}/cancel/', {}, format='json', HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        hidden.refresh_from_db()
        self.assertEqual(hidden.status, 'pending')

    def test_public_token_lookup_404s_for_cleaned_orders(self):
        order = self._make_order()
        self.assertEqual(self.api_get(f'{BY_TOKEN_URL}?token={order.public_token}').status_code, 200)
        self._hide(order)
        resp = self.api_get(f'{BY_TOKEN_URL}?token={order.public_token}')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_cleaned_open_orders_no_longer_block_product_delete(self):
        product = self._make_product()
        open_order = self._make_order_with_items(product=product, status='confirmed')
        resp = self.api_delete(f'{PROD_URL}{product.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data['orders'], [open_order.order_number])

        self._hide(open_order)
        resp = self.api_delete(f'{PROD_URL}{product.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT, resp.content)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())
        # The hidden order keeps its line with the snapshot.
        line = OrderItem.objects.get(order=open_order)
        self.assertIsNone(line.product_id)
        self.assertEqual(line.product_name, {'en': 'Product A'})

    def test_courier_webhook_still_updates_a_cleaned_order(self):
        EcommerceSettings.objects.create(tenant=self.tenant)
        order = self._make_order(status='shipped')
        self._hide(order)
        with patch('ecommerce_crm.tasks.send_order_email.delay') as mock_email:
            resp = self.client.post(
                QS_WEBHOOK, {'integrationOrderId': order.order_number, 'status': 'Delivered'},
                format='json', HTTP_HOST='tenant.test.com',
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        mock_email.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        self.assertTrue(order.is_deleted)  # the webhook doesn't un-hide it

    def test_unpaid_sweeper_still_restocks_cleaned_orders(self):
        from ecommerce_crm.tasks import _cancel_unpaid_orders_for_tenant
        product = self._make_product(quantity=0)
        order = self._make_order_with_items(product=product, status='pending', payment_method='card',
                                            payment_provider='bog')
        self._set_created(order, timezone.now() - timedelta(days=3))
        self._hide(order)
        _cancel_unpaid_orders_for_tenant()
        order.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(product.quantity, 2)


# ============================================================================
# Restore
# ============================================================================

class TestRestore(CleanBase):

    def test_restore_by_ids(self):
        a, b, c = (self._make_order() for _ in range(3))
        visible = self._make_order()
        self._hide(a, b, c)
        resp = self.api_post(RESTORE_URL, {'ids': [a.id, b.id, visible.id, 999999]}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data, {'restored': 2})
        for o in (a, b):
            o.refresh_from_db()
            self.assertFalse(o.is_deleted)
            self.assertIsNone(o.deleted_at)
            self.assertIsNone(o.deleted_by)
        c.refresh_from_db()
        self.assertTrue(c.is_deleted)
        self.assertEqual(self._ids(self.api_get(ORDER_URL, user=self.admin)), {a.id, b.id, visible.id})
        self.assertEqual(self._ids(self.api_get(ORDER_URL + '?cleaned=true', user=self.admin)), {c.id})

    def test_restore_all(self):
        orders = [self._make_order() for _ in range(3)]
        self._hide(*orders)
        resp = self.api_post(RESTORE_URL, {'all': True}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data, {'restored': 3})
        self.assertEqual(Order.objects.filter(is_deleted=True).count(), 0)
        self.assertEqual(self.api_post(RESTORE_URL, {'all': True}, user=self.admin).data, {'restored': 0})

    def test_restore_requires_ids_or_all(self):
        for body in ({}, {'ids': []}, {'all': False}, {'ids': ['x']}, {'ids': 5}):
            resp = self.api_post(RESTORE_URL, body, user=self.admin)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, body)

    def test_restore_single(self):
        order = self._make_order_with_items()
        self._hide(order)
        resp = self.api_post(f'{ORDER_URL}{order.id}/restore/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['id'], order.id)
        self.assertFalse(resp.data['is_deleted'])
        self.assertIsNone(resp.data['deleted_at'])
        self.assertEqual(resp.data['deleted_by_name'], '')
        self.assertEqual(len(resp.data['items']), 1)
        order.refresh_from_db()
        self.assertFalse(order.is_deleted)
        # Idempotent on a visible order; unknown ids 404.
        self.assertEqual(self.api_post(f'{ORDER_URL}{order.id}/restore/', user=self.admin).status_code, 200)
        self.assertEqual(self.api_post(f'{ORDER_URL}999999/restore/', user=self.admin).status_code, 404)

    def test_clean_then_restore_round_trip_matches_baseline(self):
        for s in ('delivered', 'confirmed', 'cancelled'):
            self._make_order_with_items(status=s, payment_status='paid')
        baseline = self.api_get(f'{ORDER_URL}analytics/', user=self.admin).data
        self.assertEqual(self._clean({'status': ['delivered']}).data, {'cleaned': 1})
        self.assertEqual(self.api_get(f'{ORDER_URL}analytics/', user=self.admin).data['orders']['total'],
                         baseline['orders']['total'] - 1)
        self.assertEqual(self.api_post(RESTORE_URL, {'all': True}, user=self.admin).data, {'restored': 1})
        self.assertEqual(self.api_get(f'{ORDER_URL}analytics/', user=self.admin).data, baseline)


# ============================================================================
# Security: shop clients can only read, create and cancel
# ============================================================================

class TestClientOrderMethods(CleanBase):

    def test_shop_client_cannot_edit_or_delete_orders(self):
        client = self._make_client(email='shopper-methods@test.com')
        addr = self._make_address(client)
        order = self._make_order(client=client, address=addr, total_amount=Decimal('60.00'))
        api = self._shopper_api(client)
        host = {'HTTP_HOST': 'tenant.test.com'}

        checks = [
            api.patch(f'{CLIENT_ORDERS_URL}{order.id}/', {'status': 'delivered'}, format='json', **host),
            api.patch(f'{CLIENT_ORDERS_URL}{order.id}/', {'total_amount': '0.01'}, format='json', **host),
            api.patch(f'{CLIENT_ORDERS_URL}{order.id}/', {'admin_notes': 'pwned'}, format='json', **host),
            api.put(f'{CLIENT_ORDERS_URL}{order.id}/', {'status': 'delivered', 'total_amount': '0.01'},
                    format='json', **host),
            api.delete(f'{CLIENT_ORDERS_URL}{order.id}/', **host),
        ]
        for resp in checks:
            self.assertEqual(resp.status_code, status.HTTP_405_METHOD_NOT_ALLOWED, getattr(resp, 'data', None))
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending')
        self.assertEqual(order.total_amount, Decimal('60.00'))
        self.assertEqual(order.admin_notes, '')
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())

        # Reads and cancel keep working.
        self.assertEqual(api.get(CLIENT_ORDERS_URL, **host).status_code, 200)
        self.assertEqual(api.get(f'{CLIENT_ORDERS_URL}{order.id}/', **host).status_code, 200)
        resp = api.post(f'{CLIENT_ORDERS_URL}{order.id}/cancel/', {'reason': 'changed my mind'}, format='json', **host)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'cancelled')
