"""
P4 order / low-stock notifications:

- ecommerce_order_created: cash-on-delivery order placed (create or card->COD)
- ecommerce_order_paid: payment_status becomes paid (webhook-style save, mark-paid)
- never for unpaid card orders; once per order; per-order batching
- recipients: feature + (staff / superuser / admin|manager role / can_access_orders), cap
- ecommerce_low_stock digest from the daily task, with deep links
"""
from decimal import Decimal
from unittest.mock import patch

from ecommerce_crm.models import Order
from ecommerce_crm.notifications import (
    LOW_STOCK_LIST_LINK,
    ecommerce_notification_recipients,
)
from ecommerce_crm.tests.base import EcommerceTenantTestCase, remove_ecommerce_feature
from ecommerce_crm.tests.test_order_views import OrderTestMixin
from users.models import Notification

ORDER_URL = '/api/ecommerce/admin/orders/'

_ws_patch = patch('asgiref.sync.async_to_sync', return_value=lambda f: (lambda **kw: None))


class NotificationBase(OrderTestMixin, EcommerceTenantTestCase):

    def setUp(self):
        super().setUp()
        self._ws = _ws_patch.start()
        self._push = patch('users.notification_utils._dispatch_push').start()
        self.addCleanup(patch.stopall)
        # admin: staff, no group (tenant has the feature) -> recipient
        self.admin = self.create_admin(email='n-admin@test.com')
        # manager role in a feature group -> recipient
        self.manager = self.create_manager(email='n-manager@test.com')
        self.grant_feature_group(self.manager, name='n-mgr')
        # agent with can_access_orders in a feature group -> recipient
        self.orders_agent = self.create_user(email='n-orders@test.com', can_access_orders=True)
        self.grant_feature_group(self.orders_agent, name='n-orders')
        # plain agent with the feature -> not a recipient
        self.plain_agent = self.create_user(email='n-plain@test.com')
        self.grant_feature_group(self.plain_agent, name='n-plain')
        # manager without the feature -> not a recipient
        self.manager_no_feature = self.create_manager(email='n-mgr-nofeat@test.com')
        # inactive staff -> not a recipient
        self.inactive = self.create_user(email='n-inactive@test.com', is_staff=True, is_active=False)

    @property
    def expected(self):
        return {self.admin.id, self.manager.id, self.orders_agent.id}

    def _recipients_of(self, notification_type, order_id=None):
        qs = Notification.objects.filter(notification_type=notification_type)
        if order_id is not None:
            qs = qs.filter(metadata__order_id=order_id)
        return set(qs.values_list('user_id', flat=True))


class TestOrderNotifications(NotificationBase):

    def test_recipient_rule(self):
        ids = {u.id for u in ecommerce_notification_recipients()}
        self.assertEqual(ids, self.expected)
        self.assertEqual(len(ecommerce_notification_recipients(limit=2)), 2)

    def test_no_recipients_without_tenant_feature(self):
        remove_ecommerce_feature(self.tenant)
        self.assertEqual(ecommerce_notification_recipients(), [])
        with self.captureOnCommitCallbacks(execute=True):
            self._make_order(payment_method='cash_on_delivery')
        self.assertFalse(Notification.objects.filter(notification_type='ecommerce_order_created').exists())

    def test_cod_order_created_notifies_with_deep_link(self):
        with self.captureOnCommitCallbacks(execute=True):
            order = self._make_order(
                payment_method='cash_on_delivery', customer_first_name='Nino', customer_last_name='B',
                total_amount=Decimal('42.00'),
            )
        self.assertEqual(self._recipients_of('ecommerce_order_created', order.id), self.expected)
        n = Notification.objects.filter(notification_type='ecommerce_order_created', user=self.manager).get()
        self.assertEqual(n.link_url, f'/ecommerce/orders/{order.id}')
        self.assertEqual(n.metadata['order_id'], order.id)
        self.assertEqual(n.metadata['order_number'], order.order_number)
        self.assertEqual(n.metadata['total_amount'], '42.00')
        self.assertEqual(n.metadata['payment_method'], 'cash_on_delivery')
        self.assertEqual(n.metadata['customer_name'], 'Nino B')
        self.assertIn(order.order_number, n.title)
        # Push fan-out: one push per recipient, deep-linking to the order.
        self.assertEqual(self._push.call_count, 3)
        for call in self._push.call_args_list:
            self.assertEqual(call.kwargs['url'], f'/ecommerce/orders/{order.id}')
            self.assertEqual(call.kwargs['data']['link_url'], f'/ecommerce/orders/{order.id}')

    def test_card_to_cod_switch_notifies_once(self):
        with self.captureOnCommitCallbacks(execute=True):
            order = self._make_order()  # card, unpaid -> nothing
        self.assertFalse(Notification.objects.filter(notification_type='ecommerce_order_created').exists())
        with self.captureOnCommitCallbacks(execute=True):
            order.payment_method = 'cash_on_delivery'
            order.save(update_fields=['payment_method'])
        self.assertEqual(self._recipients_of('ecommerce_order_created', order.id), self.expected)
        with self.captureOnCommitCallbacks(execute=True):
            order.notes = 'ring twice'
            order.save()
        self.assertEqual(
            Notification.objects.filter(notification_type='ecommerce_order_created').count(), 3
        )

    def test_unpaid_card_order_never_notifies(self):
        with self.captureOnCommitCallbacks(execute=True):
            order = self._make_order(payment_method='card')
            order.payment_status = 'failed'
            order.save()
        self.assertFalse(Notification.objects.filter(
            notification_type__in=['ecommerce_order_created', 'ecommerce_order_paid']).exists())

    def test_webhook_style_paid_transition_notifies_once(self):
        order = self._make_order(payment_method='card')
        with self.captureOnCommitCallbacks(execute=True):
            order.payment_status = 'paid'
            order.status = 'confirmed'
            order.save()
        self.assertEqual(self._recipients_of('ecommerce_order_paid', order.id), self.expected)
        with self.captureOnCommitCallbacks(execute=True):
            order.admin_notes = 'checked'
            order.save()
        self.assertEqual(Notification.objects.filter(notification_type='ecommerce_order_paid').count(), 3)
        n = Notification.objects.filter(notification_type='ecommerce_order_paid', user=self.admin).get()
        self.assertEqual(n.link_url, f'/ecommerce/orders/{order.id}')

    def test_mark_paid_notifies(self):
        order = self._make_order(payment_method='cash_on_delivery')
        with patch('ecommerce_crm.tasks.book_quickshipper_courier.delay'), \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api_post(f'{ORDER_URL}{order.id}/mark-paid/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._recipients_of('ecommerce_order_paid', order.id), self.expected)

    def test_two_orders_inside_batch_window_stay_separate(self):
        with self.captureOnCommitCallbacks(execute=True):
            o1 = self._make_order(payment_method='cash_on_delivery')
        with self.captureOnCommitCallbacks(execute=True):
            o2 = self._make_order(payment_method='cash_on_delivery')
        rows = Notification.objects.filter(notification_type='ecommerce_order_created', user=self.admin)
        self.assertEqual(rows.count(), 2)
        self.assertEqual({r.link_url for r in rows}, {f'/ecommerce/orders/{o1.id}', f'/ecommerce/orders/{o2.id}'})

    def test_notification_failure_never_breaks_the_save(self):
        with patch('ecommerce_crm.notifications.notify_order_created', side_effect=RuntimeError('boom')), \
                self.captureOnCommitCallbacks(execute=True):
            order = self._make_order(payment_method='cash_on_delivery')
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())


class TestLowStockNotifications(NotificationBase):

    def _run_task(self):
        from ecommerce_crm.tasks import check_low_stock_products
        with patch('ecommerce_crm.email_utils.send_email', return_value=True):
            check_low_stock_products()

    def test_single_product_links_to_product(self):
        p = self._make_product(sku='LS-1', quantity=2, low_stock_threshold=5)
        self._make_product(sku='LS-OK', quantity=50, low_stock_threshold=5)
        self._run_task()
        rows = Notification.objects.filter(notification_type='ecommerce_low_stock')
        self.assertEqual(set(rows.values_list('user_id', flat=True)), self.expected)
        n = rows.get(user=self.admin)
        self.assertEqual(n.link_url, f'/ecommerce/products/{p.id}')
        self.assertEqual(n.metadata['product_id'], p.id)
        self.assertEqual(n.metadata['count'], 1)

    def test_several_products_link_to_filtered_list(self):
        a = self._make_product(sku='LS-A', quantity=1, low_stock_threshold=5)
        b = self._make_product(sku='LS-B', quantity=0, low_stock_threshold=5)
        self._run_task()
        n = Notification.objects.get(notification_type='ecommerce_low_stock', user=self.manager)
        self.assertEqual(n.link_url, LOW_STOCK_LIST_LINK)
        self.assertEqual(set(n.metadata['product_ids']), {a.id, b.id})
        self.assertEqual(n.metadata['count'], 2)
        self.assertIn('2 products', n.title)

    def test_no_low_stock_no_notification(self):
        self._make_product(sku='LS-FULL', quantity=50, low_stock_threshold=5)
        self._run_task()
        self.assertFalse(Notification.objects.filter(notification_type='ecommerce_low_stock').exists())
