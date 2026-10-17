"""
Parity package P4 (e-commerce orders + catalog) backend tests:

- permissions: ecommerce_crm feature on every admin endpoint, manager level
  for money moves / deletes / customer edits, store-admin level for config
- order status rules: cancel via Order.cancel() (restock + promo release)
  from update_status / bulk / PATCH / courier webhook, terminal statuses,
  atomic refund, mark-paid guards, bulk dedupe + skipped + web alias
- initiate_payment charges the tenant's BOG merchant
- order list filters
- products: all-status default, status / status__in, multilingual name
  search, in_stock=false incl. oversold, bulk-update validation + fields,
  adjust-stock (product + variant), image create, 409 on protected delete
"""
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone
from rest_framework import status

from ecommerce_crm.models import (
    EcommerceSettings,
    Order,
    OrderItem,
    Product,
    ProductImage,
    ProductVariant,
    PromoCode,
)
from ecommerce_crm.tests.base import EcommerceTenantTestCase, remove_ecommerce_feature
from ecommerce_crm.tests.test_order_views import OrderTestMixin

ORDER_URL = '/api/ecommerce/admin/orders/'
PROD_URL = '/api/ecommerce/admin/products/'


def _results(resp):
    if isinstance(resp.data, dict) and 'results' in resp.data:
        return resp.data['results']
    return resp.data


class P4Base(OrderTestMixin, EcommerceTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='p4-admin@test.com')

    def _agent(self, email, with_feature=True, **kw):
        user = self.create_user(email=email, role='agent', **kw)
        if with_feature:
            self.grant_feature_group(user, name=f'grp-{email}')
        return user

    def _promo(self, code='P4SAVE', times_used=1):
        now = timezone.now()
        return PromoCode.objects.create(
            code=code, discount_type='fixed', discount_value=Decimal('5.00'),
            valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=1),
            times_used=times_used,
        )


# ============================================================================
# Permissions
# ============================================================================

class TestEcommercePermissions(P4Base):

    def test_staff_denied_when_tenant_has_no_feature(self):
        remove_ecommerce_feature(self.tenant)
        resp = self.api_get(ORDER_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_agent_without_feature_denied_everywhere(self):
        agent = self._agent('p4-nofeat@test.com', with_feature=False)
        for url in (ORDER_URL, PROD_URL, '/api/ecommerce/admin/clients/',
                    '/api/ecommerce/admin/reviews/', '/api/ecommerce/admin/settings/',
                    '/api/ecommerce/admin/orders/analytics/'):
            resp = self.api_get(url, user=agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, url)

    def test_staff_without_group_allowed_when_tenant_has_feature(self):
        resp = self.api_get(ORDER_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_agent_with_feature_can_read_and_fulfil(self):
        agent = self._agent('p4-agent@test.com')
        order = self._make_order()
        self.assertEqual(self.api_get(ORDER_URL, user=agent).status_code, 200)
        self.assertEqual(self.api_get(PROD_URL, user=agent).status_code, 200)
        with patch('ecommerce_crm.tasks.send_order_email.delay'):
            resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'confirmed'}, user=agent)
        self.assertEqual(resp.status_code, 200)

    def test_agent_with_feature_cannot_move_money_or_delete(self):
        agent = self._agent('p4-agent2@test.com')
        order = self._make_order_with_items()
        product = self._make_product()
        client = order.client
        checks = [
            self.api_post(f'{ORDER_URL}{order.id}/refund/', user=agent),
            self.api_post(f'{ORDER_URL}{order.id}/mark-paid/', user=agent),
            self.api_post(f'{ORDER_URL}{order.id}/initiate_payment/', user=agent),
            self.api_patch(f'{ORDER_URL}{order.id}/', {'admin_notes': 'x'}, user=agent),
            self.api_delete(f'{ORDER_URL}{order.id}/', user=agent),
            self.api_patch(f'/api/ecommerce/admin/clients/{client.id}/', {'is_active': False}, user=agent),
            self.api_delete(f'/api/ecommerce/admin/clients/{client.id}/', user=agent),
            self.api_delete(f'{PROD_URL}{product.id}/', user=agent),
            self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'refunded'}, user=agent),
        ]
        for resp in checks:
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending')
        self.assertEqual(order.payment_status, 'pending')

    def test_agent_with_feature_cannot_change_store_config(self):
        agent = self._agent('p4-agent3@test.com')
        checks = [
            self.api_post('/api/ecommerce/admin/languages/', {'code': 'de', 'name': {'en': 'German'}}, user=agent),
            self.api_post('/api/ecommerce/admin/settings/', {'store_name': 'x'}, user=agent),
            self.api_post('/api/ecommerce/admin/homepage-sections/generate-from-variant/', {'variant': 'x'}, user=agent),
            self.api_post('/api/ecommerce/admin/quickshipper/test-connection/', user=agent),
        ]
        for resp in checks:
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)
        # Reads stay open to the feature.
        self.assertEqual(self.api_get('/api/ecommerce/admin/languages/', user=agent).status_code, 200)
        self.assertEqual(self.api_get('/api/ecommerce/admin/settings/', user=agent).status_code, 200)

    def test_manager_role_can_refund(self):
        manager = self.create_manager(email='p4-mgr@test.com')
        self.grant_feature_group(manager, name='mgr-grp')
        order = self._make_order_with_items()
        resp = self.api_post(f'{ORDER_URL}{order.id}/refund/', user=manager)
        self.assertEqual(resp.status_code, 200)

    def test_access_orders_permission_can_mark_paid(self):
        agent = self._agent('p4-orders@test.com', can_access_orders=True)
        order = self._make_order()
        with patch('ecommerce_crm.tasks.book_quickshipper_courier.delay'):
            resp = self.api_post(f'{ORDER_URL}{order.id}/mark-paid/', user=agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['payment_status'], 'paid')


# ============================================================================
# Order status rules
# ============================================================================

class TestOrderStatusRules(P4Base):

    def test_update_status_cancel_restocks_and_releases_promo(self):
        product = self._make_product(quantity=10)
        promo = self._promo(times_used=1)
        order = self._make_order_with_items(product=product, promo_code=promo)
        resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'cancelled'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'cancelled')
        product.refresh_from_db()
        promo.refresh_from_db()
        self.assertEqual(product.quantity, 12)
        self.assertEqual(promo.times_used, 0)
        order.refresh_from_db()
        self.assertIsNotNone(order.cancelled_at)
        self.assertIn('[cancelled]', order.admin_notes)

    def test_cancelled_order_cannot_be_reopened(self):
        product = self._make_product(quantity=10)
        order = self._make_order_with_items(product=product)
        self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'cancelled'}, user=self.admin)
        resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'pending'}, user=self.admin)
        self.assertEqual(resp.status_code, 400)
        # Cancelling again is a no-op, not a second restock.
        resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'cancelled'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.quantity, 12)

    def test_update_status_refunded_runs_refund_flow(self):
        product = self._make_product(quantity=5)
        order = self._make_order_with_items(product=product)
        resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {'status': 'refunded'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'refunded')
        self.assertEqual(resp.data['payment_status'], 'refunded')
        product.refresh_from_db()
        self.assertEqual(product.quantity, 7)

    def test_refund_releases_promo_and_is_idempotent(self):
        product = self._make_product(quantity=1)
        promo = self._promo(code='P4REF', times_used=2)
        order = self._make_order_with_items(product=product, promo_code=promo)
        self.assertEqual(self.api_post(f'{ORDER_URL}{order.id}/refund/', user=self.admin).status_code, 200)
        self.assertEqual(self.api_post(f'{ORDER_URL}{order.id}/refund/', user=self.admin).status_code, 400)
        product.refresh_from_db()
        promo.refresh_from_db()
        self.assertEqual(product.quantity, 3)
        self.assertEqual(promo.times_used, 1)

    def test_shipped_sets_tracking_and_sends_email(self):
        order = self._make_order()
        with patch('ecommerce_crm.tasks.send_order_email.delay') as mock_email:
            resp = self.api_post(f'{ORDER_URL}{order.id}/update_status/', {
                'status': 'shipped', 'tracking_number': 'TRK-1', 'courier_provider': 'Wolt',
            }, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['tracking_number'], 'TRK-1')
        mock_email.assert_called_once()
        self.assertEqual(mock_email.call_args[0][2], 'shipped')

    def test_patch_status_cancelled_goes_through_cancel(self):
        product = self._make_product(quantity=0)
        order = self._make_order_with_items(product=product)
        resp = self.api_patch(f'{ORDER_URL}{order.id}/', {'status': 'cancelled', 'admin_notes': 'dup'}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        product.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(product.quantity, 2)
        resp = self.api_patch(f'{ORDER_URL}{order.id}/', {'status': 'pending'}, user=self.admin)
        self.assertEqual(resp.status_code, 400)

    def test_mark_paid_refused_for_refunded_or_cancelled(self):
        refunded = self._make_order(status='refunded', payment_status='refunded')
        cancelled = self._make_order(status='cancelled')
        for order in (refunded, cancelled):
            resp = self.api_post(f'{ORDER_URL}{order.id}/mark-paid/', user=self.admin)
            self.assertEqual(resp.status_code, 400)
            order.refresh_from_db()
            self.assertNotEqual(order.payment_status, 'paid')

    def test_mark_paid_appends_notes(self):
        order = self._make_order()
        with patch('ecommerce_crm.tasks.book_quickshipper_courier.delay'):
            resp = self.api_post(f'{ORDER_URL}{order.id}/mark-paid/', {'notes': 'cash at door'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'confirmed')
        self.assertIn('[Payment] cash at door', resp.data['admin_notes'])

    def test_bulk_cancel_restocks_skips_terminal_and_dedupes(self):
        product = self._make_product(quantity=0)
        o1 = self._make_order_with_items(product=product)
        o2 = self._make_order(status='refunded', payment_status='refunded')
        resp = self.api_post(f'{ORDER_URL}bulk-update-status/', {
            'order_ids': [o1.id, o1.id, o2.id], 'status': 'cancelled',
        }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['updated'], 1)
        self.assertEqual(resp.data['order_ids'], [o1.id])
        self.assertEqual([s['id'] for s in resp.data['skipped']], [o2.id])
        product.refresh_from_db()
        self.assertEqual(product.quantity, 2)

    def test_bulk_refunded_rejected(self):
        order = self._make_order()
        resp = self.api_post(f'{ORDER_URL}bulk-update-status/', {
            'order_ids': [order.id], 'status': 'refunded',
        }, user=self.admin)
        self.assertEqual(resp.status_code, 400)

    def test_web_bulk_update_alias(self):
        o1 = self._make_order()
        o2 = self._make_order()
        with patch('ecommerce_crm.tasks.send_order_email.delay'):
            resp = self.api_post(f'{ORDER_URL}bulk-update/', {
                'order_ids': [o1.id, o2.id], 'status': 'shipped',
            }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['updated'], 2)

    def test_quickshipper_cancel_restocks(self):
        EcommerceSettings.objects.create(tenant=self.tenant)
        product = self._make_product(quantity=0)
        order = self._make_order_with_items(product=product)
        resp = self.client.post(
            '/api/ecommerce/shipping-webhook/quickshipper/',
            {'integrationOrderId': order.order_number, 'status': 'Cancelled', 'orderId': 77},
            format='json', HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        order.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(product.quantity, 2)

    def test_quickshipper_delivered_sends_email(self):
        EcommerceSettings.objects.create(tenant=self.tenant)
        order = self._make_order(status='shipped')
        with patch('ecommerce_crm.tasks.send_order_email.delay') as mock_email:
            resp = self.client.post(
                '/api/ecommerce/shipping-webhook/quickshipper/',
                {'integrationOrderId': order.order_number, 'status': 'Delivered'},
                format='json', HTTP_HOST='tenant.test.com',
            )
        self.assertEqual(resp.status_code, 200)
        mock_email.assert_called_once()
        self.assertEqual(mock_email.call_args[0][2], 'delivered')

    def test_delete_order_is_manager_only_and_works_for_staff(self):
        order = self._make_order_with_items()
        resp = self.api_delete(f'{ORDER_URL}{order.id}/', user=self.admin)
        self.assertEqual(resp.status_code, 204)


# ============================================================================
# initiate_payment
# ============================================================================

class TestInitiatePayment(P4Base):

    def _bog_settings(self):
        return EcommerceSettings.objects.create(
            tenant=self.tenant, bog_client_id='tenant-client', bog_client_secret_encrypted=b'enc',
        )

    def test_uses_tenant_bog_service(self):
        self._bog_settings()
        order = self._make_order()
        tenant_service = MagicMock()
        tenant_service.create_payment.return_value = {
            'order_id': 'bog-1', 'payment_url': 'https://pay.test/1', 'amount': 60.0, 'currency': 'GEL',
        }
        with patch('ecommerce_crm.payment_utils.get_tenant_bog_service', return_value=tenant_service) as mock_get, \
                patch('tenants.bog_payment.bog_service') as platform_service:
            resp = self.api_post(f'{ORDER_URL}{order.id}/initiate_payment/', {}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        mock_get.assert_called_once()
        tenant_service.create_payment.assert_called_once()
        platform_service.create_payment.assert_not_called()
        self.assertEqual(resp.data['payment_url'], 'https://pay.test/1')
        order.refresh_from_db()
        self.assertEqual(order.bog_order_id, 'bog-1')
        self.assertEqual(order.payment_provider, 'bog')

    def test_credential_error_returns_503(self):
        from ecommerce_crm.payment_utils import BogCredentialError
        self._bog_settings()
        order = self._make_order()
        with patch('ecommerce_crm.payment_utils.get_tenant_bog_service', side_effect=BogCredentialError('x')):
            resp = self.api_post(f'{ORDER_URL}{order.id}/initiate_payment/', {}, user=self.admin)
        self.assertEqual(resp.status_code, 503)

    def test_cancelled_order_refused(self):
        order = self._make_order(status='cancelled')
        resp = self.api_post(f'{ORDER_URL}{order.id}/initiate_payment/', {}, user=self.admin)
        self.assertEqual(resp.status_code, 400)


# ============================================================================
# Order list filters
# ============================================================================

class TestOrderListFilters(P4Base):

    def test_payment_status_status_in_and_tracking(self):
        paid = self._make_order(payment_status='paid', status='confirmed', tracking_number='T1')
        cod = self._make_order(payment_status='pending', payment_method='cash_on_delivery')
        shipped = self._make_order(status='shipped')

        ids = lambda r: {o['id'] for o in _results(r)}
        self.assertEqual(ids(self.api_get(f'{ORDER_URL}?payment_status=paid', user=self.admin)), {paid.id})
        self.assertEqual(ids(self.api_get(f'{ORDER_URL}?status__in=confirmed,shipped', user=self.admin)), {paid.id, shipped.id})
        self.assertEqual(ids(self.api_get(f'{ORDER_URL}?has_tracking=true', user=self.admin)), {paid.id})
        self.assertEqual(ids(self.api_get(f'{ORDER_URL}?payment_method=cash_on_delivery', user=self.admin)), {cod.id})

    def test_created_range(self):
        old = self._make_order()
        Order.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))
        new = self._make_order()
        today = timezone.localdate().isoformat()
        week_ago = (timezone.localdate() - timedelta(days=5)).isoformat()
        r = self.api_get(f'{ORDER_URL}?created_after={week_ago}', user=self.admin)
        self.assertEqual({o['id'] for o in _results(r)}, {new.id})
        r = self.api_get(f'{ORDER_URL}?created_before={week_ago}', user=self.admin)
        self.assertEqual({o['id'] for o in _results(r)}, {old.id})
        r = self.api_get(f'{ORDER_URL}?created_before={today}', user=self.admin)
        self.assertEqual({o['id'] for o in _results(r)}, {old.id, new.id})
        r = self.api_get(f'{ORDER_URL}?created_after=not-a-date', user=self.admin)
        self.assertEqual(r.status_code, 400)

    def test_list_total_items_is_integer(self):
        self._make_order_with_items()
        r = self.api_get(ORDER_URL, user=self.admin)
        self.assertIsInstance(_results(r)[0]['total_items'], int)


# ============================================================================
# Products
# ============================================================================

class TestProductCatalogParity(P4Base):

    def test_status_all_csv_and_invalid(self):
        a = self._make_product(status='active')
        d = self._make_product(status='draft')
        i = self._make_product(status='inactive')
        ids = lambda r: {p['id'] for p in _results(r)}
        self.assertEqual(ids(self.api_get(PROD_URL, user=self.admin)), {a.id, d.id, i.id})
        self.assertEqual(ids(self.api_get(f'{PROD_URL}?status=all', user=self.admin)), {a.id, d.id, i.id})
        self.assertEqual(ids(self.api_get(f'{PROD_URL}?status=draft', user=self.admin)), {d.id})
        self.assertEqual(ids(self.api_get(f'{PROD_URL}?status__in=draft,inactive', user=self.admin)), {d.id, i.id})
        self.assertEqual(ids(self.api_get(f'{PROD_URL}?status=active,inactive', user=self.admin)), {a.id, i.id})
        self.assertEqual(self.api_get(f'{PROD_URL}?status=bogus', user=self.admin).status_code, 400)

    def test_search_matches_name_in_any_language_and_variant_sku(self):
        p = self._make_product(sku='ZZ-1', name={'en': 'Red Mug', 'ka': 'წითელი ფინჯანი'})
        other = self._make_product(sku='ZZ-2', name={'en': 'Blue Plate'})
        ProductVariant.objects.create(product=other, sku='VAR-XYZ', name={'en': 'Large'})
        ids = lambda r: {x['id'] for x in _results(r)}
        self.assertEqual(ids(self.api_get(f'{PROD_URL}?search=mug', user=self.admin)), {p.id})
        self.assertEqual(ids(self.api_get(f'{PROD_URL}?search=ფინჯანი', user=self.admin)), {p.id})
        self.assertEqual(ids(self.api_get(f'{PROD_URL}?search=var-xyz', user=self.admin)), {other.id})
        self.assertEqual(ids(self.api_get(f'{PROD_URL}?search=ZZ-2', user=self.admin)), {other.id})

    def test_in_stock_false_includes_oversold(self):
        zero = self._make_product(quantity=0)
        neg = self._make_product(quantity=-3)
        self._make_product(quantity=4)
        r = self.api_get(f'{PROD_URL}?in_stock=false', user=self.admin)
        self.assertEqual({x['id'] for x in _results(r)}, {zero.id, neg.id})

    def test_low_stock_honours_search(self):
        hit = self._make_product(sku='LOW-A', quantity=2, name={'en': 'Candle'})
        self._make_product(sku='LOW-B', quantity=2, name={'en': 'Soap'})
        r = self.api_get(f'{PROD_URL}low_stock/?search=candle', user=self.admin)
        self.assertEqual(r.status_code, 200)
        self.assertEqual({x['id'] for x in _results(r)}, {hit.id})

    def test_bulk_update_validates_status_and_sets_fields(self):
        p1 = self._make_product(status='draft')
        p2 = self._make_product(status='draft')
        r = self.api_post(f'{PROD_URL}bulk-update/', {'product_ids': [p1.id], 'status': 'nope'}, user=self.admin)
        self.assertEqual(r.status_code, 400)
        r = self.api_post(f'{PROD_URL}bulk-update/', {'product_ids': [p1.id]}, user=self.admin)
        self.assertEqual(r.status_code, 400)
        r = self.api_post(f'{PROD_URL}bulk-update/', {
            'product_ids': [p1.id, p2.id], 'status': 'active', 'price': '12.50',
            'quantity': 7, 'is_featured': True,
        }, user=self.admin)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['updated'], 2)
        p1.refresh_from_db()
        self.assertEqual((p1.status, p1.price, p1.quantity, p1.is_featured), ('active', Decimal('12.50'), 7, True))
        # The web's status-only payload still works.
        r = self.api_post(f'{PROD_URL}bulk-update/', {'product_ids': [p2.id], 'status': 'inactive'}, user=self.admin)
        self.assertEqual(r.status_code, 200)

    def test_adjust_stock_product_and_variant(self):
        p = self._make_product(quantity=5, low_stock_threshold=10)
        r = self.api_post(f'{PROD_URL}{p.id}/adjust-stock/', {'delta': -2, 'reason': 'damaged'}, user=self.admin)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['quantity'], 3)
        self.assertTrue(r.data['is_low_stock'])
        r = self.api_post(f'{PROD_URL}{p.id}/adjust-stock/', {'delta': 10}, user=self.admin)
        self.assertEqual(r.data['quantity'], 13)
        for bad in (0, 'abc', 1.5, None):
            r = self.api_post(f'{PROD_URL}{p.id}/adjust-stock/', {'delta': bad}, user=self.admin)
            self.assertEqual(r.status_code, 400, bad)
        v = ProductVariant.objects.create(product=p, sku='ADJ-V', name={'en': 'S'}, quantity=1)
        r = self.api_post(f'/api/ecommerce/admin/variants/{v.id}/adjust-stock/', {'delta': 4}, user=self.admin)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['quantity'], 5)

    def test_adjust_stock_needs_feature(self):
        p = self._make_product(quantity=5)
        agent = self._agent('p4-stock@test.com', with_feature=False)
        r = self.api_post(f'{PROD_URL}{p.id}/adjust-stock/', {'delta': 1}, user=agent)
        self.assertEqual(r.status_code, 403)

    def test_image_create_with_product(self):
        p = self._make_product()
        r = self.api_post('/api/ecommerce/admin/images/', {'image': 'https://cdn.test/a.jpg'}, user=self.admin)
        self.assertEqual(r.status_code, 400)
        self.assertIn('product', r.data)
        r = self.api_post('/api/ecommerce/admin/images/', {
            'product': p.id, 'image': 'https://cdn.test/a.jpg', 'sort_order': 1,
        }, user=self.admin)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(ProductImage.objects.filter(product=p, image='https://cdn.test/a.jpg').exists())

    def test_delete_product_with_orders_is_409(self):
        product = self._make_product()
        self._make_order_with_items(product=product)
        r = self.api_delete(f'{PROD_URL}{product.id}/', user=self.admin)
        self.assertEqual(r.status_code, 409)
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

    def test_delete_client_with_orders_is_409(self):
        order = self._make_order()
        r = self.api_delete(f'/api/ecommerce/admin/clients/{order.client_id}/', user=self.admin)
        self.assertEqual(r.status_code, 409)

    def test_promo_code_validation(self):
        now = timezone.now()
        base = {
            'code': 'BAD1', 'discount_type': 'percentage', 'discount_value': '150',
            'valid_from': now.isoformat(), 'valid_until': (now + timedelta(days=1)).isoformat(),
        }
        r = self.api_post('/api/ecommerce/admin/promo-codes/', base, user=self.admin)
        self.assertEqual(r.status_code, 400)
        self.assertIn('discount_value', r.data)
        r = self.api_post('/api/ecommerce/admin/promo-codes/', {
            **base, 'discount_value': '10', 'valid_until': (now - timedelta(days=1)).isoformat(),
        }, user=self.admin)
        self.assertEqual(r.status_code, 400)
        self.assertIn('valid_until', r.data)
        r = self.api_post('/api/ecommerce/admin/promo-codes/', {**base, 'discount_value': '10'}, user=self.admin)
        self.assertEqual(r.status_code, 201, r.data)

    def test_review_lists_product_name(self):
        from ecommerce_crm.models import ProductReview
        product = self._make_product(name={'en': 'Reviewed'})
        client = self._make_client(email='rev-p4@test.com')
        ProductReview.objects.create(product=product, client=client, rating=5, title='t', content='c')
        r = self.api_get('/api/ecommerce/admin/reviews/', user=self.admin)
        row = _results(r)[0]
        self.assertEqual(row['product_name'], {'en': 'Reviewed'})
        self.assertEqual(row['product_sku'], product.sku)
        self.assertIn('product_image', row)
