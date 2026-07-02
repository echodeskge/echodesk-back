"""
Tests for ecommerce admin Order API endpoints:
- Paginated listing
- Create order with line items
- Update order status (with timestamp tracking)
- Order detail with line items
- Filter by status
- Filter by date range
- Order refund with stock restoration
- Bulk status update
- Mark as paid
"""
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.utils import timezone
from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase
from ecommerce_crm.models import (
    Product,
    EcommerceClient,
    ClientAddress,
    Order,
    OrderItem,
    Cart,
    CartItem,
)

User = get_user_model()

ORDER_URL = '/api/ecommerce/admin/orders/'


def _results(resp):
    """Extract results from paginated or plain response."""
    if isinstance(resp.data, dict) and 'results' in resp.data:
        return resp.data['results']
    return resp.data


class OrderTestMixin:
    """Shared helpers for order view tests."""

    def _make_client(self, email='order-client@test.com', **kw):
        defaults = {
            'first_name': 'Order',
            'last_name': 'Client',
            'email': email,
            'phone_number': f'+99555{EcommerceClient.objects.count():06d}',
            'password': make_password('clientpass123'),
            'is_active': True,
            'is_verified': True,
        }
        defaults.update(kw)
        return EcommerceClient.objects.create(**defaults)

    def _make_product(self, sku=None, **kw):
        if sku is None:
            sku = f'ORD-PROD-{Product.objects.count() + 1:04d}'
        defaults = {
            'sku': sku,
            'name': {'en': 'Order Test Product'},
            'price': Decimal('30.00'),
            'status': 'active',
            'quantity': 100,
            'track_inventory': True,
            'low_stock_threshold': 10,
            'created_by': self.admin,
        }
        defaults.update(kw)
        return Product.objects.create(**defaults)

    def _make_address(self, client, **kw):
        defaults = {
            'client': client,
            'label': 'Home',
            'address': '123 Test St',
            'city': 'Tbilisi',
        }
        defaults.update(kw)
        return ClientAddress.objects.create(**defaults)

    def _make_order(self, client=None, address=None, **kw):
        if client is None:
            client = self._make_client(
                email=f'oc-{Order.objects.count()}@test.com'
            )
        if address is None:
            address = self._make_address(client)
        defaults = {
            'order_number': Order.generate_order_number(),
            'client': client,
            'delivery_address': address,
            'total_amount': Decimal('60.00'),
            'status': 'pending',
        }
        defaults.update(kw)
        return Order.objects.create(**defaults)

    def _make_order_with_items(self, client=None, address=None, product=None, **kw):
        order = self._make_order(client=client, address=address, **kw)
        if product is None:
            product = self._make_product()
        OrderItem.objects.create(
            order=order, product=product,
            product_name={'en': 'Product A'}, quantity=2, price=Decimal('30.00'),
        )
        return order


# ============================================================================
# List orders
# ============================================================================

class TestListOrders(OrderTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='list-order-admin@test.com')

    def test_list_orders_returns_200(self):
        self._make_order()
        resp = self.api_get(ORDER_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_orders_paginated(self):
        for _ in range(3):
            self._make_order()
        resp = self.api_get(ORDER_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        self.assertGreaterEqual(len(results), 3)

    def test_unauthenticated_denied(self):
        resp = self.api_get(ORDER_URL)
        self.assertIn(resp.status_code, [401, 403])


# ============================================================================
# Create order
# ============================================================================

class TestCreateOrder(OrderTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='create-order-admin@test.com')
        self.eclient = self._make_client(email='co-buyer@test.com')
        self.address = self._make_address(self.eclient)
        self.product = self._make_product('CO-PROD-001')
        # Create a cart with items to convert into an order
        self.cart = Cart.objects.create(client=self.eclient, status='active')
        CartItem.objects.create(
            cart=self.cart, product=self.product,
            quantity=2, price_at_add=self.product.price,
        )



# ============================================================================
# Order detail
# ============================================================================

class TestOrderDetail(OrderTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='detail-order-admin@test.com')

    def test_order_detail_returns_order(self):
        product = self._make_product('DET-PROD-001')
        order = self._make_order_with_items(product=product)
        resp = self.api_get(f'{ORDER_URL}{order.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['order_number'], order.order_number)

    def test_order_detail_includes_items(self):
        product = self._make_product('DET-ITEM-001')
        order = self._make_order_with_items(product=product)
        resp = self.api_get(f'{ORDER_URL}{order.pk}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('items', resp.data)
        self.assertGreaterEqual(len(resp.data['items']), 1)

    def test_nonexistent_order_returns_404(self):
        resp = self.api_get(f'{ORDER_URL}99999/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ============================================================================
# Update order status
# ============================================================================

class TestUpdateOrderStatus(OrderTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='status-order-admin@test.com')

    @patch('ecommerce_crm.views.send_order_email', create=True)
    def test_update_status_to_confirmed(self, mock_email=None):
        order = self._make_order(status='pending')
        resp = self.api_post(
            f'{ORDER_URL}{order.pk}/update_status/',
            {'status': 'confirmed'},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')
        self.assertIsNotNone(order.confirmed_at)

    @patch('ecommerce_crm.views.send_order_email', create=True)
    def test_update_status_to_shipped_with_tracking(self, mock_email=None):
        order = self._make_order(status='confirmed')
        resp = self.api_post(
            f'{ORDER_URL}{order.pk}/update_status/',
            {
                'status': 'shipped',
                'tracking_number': 'TRK-12345',
                'courier_provider': 'DHL',
            },
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, 'shipped')
        self.assertIsNotNone(order.shipped_at)
        self.assertEqual(order.tracking_number, 'TRK-12345')
        self.assertEqual(order.courier_provider, 'DHL')

    @patch('ecommerce_crm.views.send_order_email', create=True)
    def test_update_status_to_delivered(self, mock_email=None):
        order = self._make_order(status='shipped')
        resp = self.api_post(
            f'{ORDER_URL}{order.pk}/update_status/',
            {'status': 'delivered'},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        self.assertIsNotNone(order.delivered_at)

    @patch('ecommerce_crm.views.send_order_email', create=True)
    def test_update_status_to_cancelled(self, mock_email=None):
        order = self._make_order(status='pending')
        resp = self.api_post(
            f'{ORDER_URL}{order.pk}/update_status/',
            {'status': 'cancelled'},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertIsNotNone(order.cancelled_at)

    def test_update_status_missing_status_returns_400(self):
        order = self._make_order()
        resp = self.api_post(
            f'{ORDER_URL}{order.pk}/update_status/',
            {},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_status_invalid_status_returns_400(self):
        order = self._make_order()
        resp = self.api_post(
            f'{ORDER_URL}{order.pk}/update_status/',
            {'status': 'nonexistent'},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Filter by status
# ============================================================================

class TestFilterOrdersByStatus(OrderTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='filt-order-admin@test.com')

    def test_filter_pending_orders(self):
        self._make_order(status='pending')
        self._make_order(status='confirmed')
        resp = self.api_get(f'{ORDER_URL}?status=pending', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        for r in results:
            self.assertEqual(r['status'], 'pending')

    def test_filter_confirmed_orders(self):
        self._make_order(status='confirmed')
        resp = self.api_get(f'{ORDER_URL}?status=confirmed', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        for r in results:
            self.assertEqual(r['status'], 'confirmed')

    def test_filter_shipped_orders(self):
        self._make_order(status='shipped')
        resp = self.api_get(f'{ORDER_URL}?status=shipped', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        for r in results:
            self.assertEqual(r['status'], 'shipped')

    def test_filter_delivered_orders(self):
        self._make_order(status='delivered')
        resp = self.api_get(f'{ORDER_URL}?status=delivered', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = _results(resp)
        for r in results:
            self.assertEqual(r['status'], 'delivered')


# ============================================================================
# Order refund
# ============================================================================

class TestOrderRefund(OrderTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='refund-order-admin@test.com')

    def test_refund_restores_stock(self):
        product = self._make_product('REFUND-PROD-001', quantity=100, track_inventory=True)
        order = self._make_order_with_items(product=product, status='confirmed')
        # Simulate that stock was decremented on order
        product.quantity -= 2  # 2 items were ordered
        product.save(update_fields=['quantity'])
        self.assertEqual(product.quantity, 98)

        resp = self.api_post(f'{ORDER_URL}{order.pk}/refund/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'refunded')

        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')
        self.assertEqual(order.payment_status, 'refunded')

        product.refresh_from_db()
        self.assertEqual(product.quantity, 100)  # stock restored

    def test_refund_already_refunded_returns_400(self):
        order = self._make_order(status='refunded')
        resp = self.api_post(f'{ORDER_URL}{order.pk}/refund/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_refund_cancelled_order_returns_400(self):
        order = self._make_order(status='cancelled')
        resp = self.api_post(f'{ORDER_URL}{order.pk}/refund/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Bulk status update
# ============================================================================

class TestBulkOrderStatusUpdate(OrderTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='bulk-order-admin@test.com')

    @patch('ecommerce_crm.views.send_order_email', create=True)
    def test_bulk_update_status_to_confirmed(self, mock_email=None):
        o1 = self._make_order(status='pending')
        o2 = self._make_order(status='pending')
        resp = self.api_post(f'{ORDER_URL}bulk-update-status/', {
            'order_ids': [o1.pk, o2.pk],
            'status': 'confirmed',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['updated'], 2)
        o1.refresh_from_db()
        o2.refresh_from_db()
        self.assertEqual(o1.status, 'confirmed')
        self.assertEqual(o2.status, 'confirmed')
        self.assertIsNotNone(o1.confirmed_at)
        self.assertIsNotNone(o2.confirmed_at)

    def test_bulk_update_missing_order_ids_returns_400(self):
        resp = self.api_post(f'{ORDER_URL}bulk-update-status/', {
            'status': 'confirmed',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_update_missing_status_returns_400(self):
        o = self._make_order()
        resp = self.api_post(f'{ORDER_URL}bulk-update-status/', {
            'order_ids': [o.pk],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_update_invalid_status_returns_400(self):
        o = self._make_order()
        resp = self.api_post(f'{ORDER_URL}bulk-update-status/', {
            'order_ids': [o.pk],
            'status': 'invalid_status',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_update_nonexistent_order_returns_400(self):
        resp = self.api_post(f'{ORDER_URL}bulk-update-status/', {
            'order_ids': [99999],
            'status': 'confirmed',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Mark order as paid
# ============================================================================

class TestMarkOrderPaid(OrderTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='paid-order-admin@test.com')

    def test_mark_pending_as_paid(self):
        order = self._make_order(status='pending', payment_status='pending')
        resp = self.api_post(f'{ORDER_URL}{order.pk}/mark-paid/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
        self.assertIsNotNone(order.paid_at)
        # Pending order should be auto-confirmed
        self.assertEqual(order.status, 'confirmed')

    def test_mark_already_paid_returns_400(self):
        order = self._make_order(payment_status='paid')
        resp = self.api_post(f'{ORDER_URL}{order.pk}/mark-paid/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_mark_paid_with_notes(self):
        order = self._make_order(status='confirmed', payment_status='pending')
        resp = self.api_post(f'{ORDER_URL}{order.pk}/mark-paid/', {
            'notes': 'Cash received at counter',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertIn('Cash received at counter', order.admin_notes)


# ============================================================================
# Order model properties
# ============================================================================

class TestOrderModelProperties(OrderTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='orderprop-admin@test.com')

    def test_order_number_format(self):
        order_num = Order.generate_order_number()
        self.assertTrue(order_num.startswith('ORD-'))
        # Format: ORD-YYYYMMDD-XXXXXX
        parts = order_num.split('-')
        self.assertEqual(len(parts), 3)

    def test_order_total_items(self):
        product = self._make_product('TOTAL-PROD-001')
        order = self._make_order()
        OrderItem.objects.create(
            order=order, product=product,
            product_name={'en': 'P1'}, quantity=3, price=Decimal('10.00'),
        )
        OrderItem.objects.create(
            order=order, product=product,
            product_name={'en': 'P2'}, quantity=2, price=Decimal('20.00'),
        )
        self.assertEqual(order.total_items, 5)

    def test_order_item_subtotal(self):
        product = self._make_product('SUB-PROD-001')
        order = self._make_order()
        item = OrderItem.objects.create(
            order=order, product=product,
            product_name={'en': 'P1'}, quantity=4, price=Decimal('15.00'),
        )
        self.assertEqual(item.subtotal, Decimal('60.00'))

    def test_order_status_choices(self):
        valid_statuses = [choice[0] for choice in Order.STATUS_CHOICES]
        expected = ['pending', 'confirmed', 'processing', 'shipped',
                    'delivered', 'cancelled', 'refunded']
        self.assertEqual(valid_statuses, expected)

    def test_order_payment_status_choices(self):
        valid = [choice[0] for choice in Order.PAYMENT_STATUS_CHOICES]
        expected = ['pending', 'paid', 'failed', 'refunded', 'partially_refunded']
        self.assertEqual(valid, expected)


# ============================================================================
# Order.cancel() — inventory restore + idempotency
# ============================================================================

class TestOrderCancelMethod(OrderTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='cancel-order-admin@test.com')

    def test_cancel_restores_stock_and_sets_status(self):
        product = self._make_product('CANCEL-PROD-001', quantity=100, track_inventory=True)
        order = self._make_order_with_items(product=product, status='pending')
        # Simulate the checkout stock decrement (2 units ordered).
        product.quantity -= 2
        product.save(update_fields=['quantity'])
        self.assertEqual(product.quantity, 98)

        self.assertTrue(order.cancel(reason='test'))

        order.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertIsNotNone(order.cancelled_at)
        self.assertEqual(product.quantity, 100)  # stock restored

    def test_cancel_is_idempotent(self):
        """A second cancel must not restock again (double-refund of inventory)."""
        product = self._make_product('CANCEL-PROD-002', quantity=100, track_inventory=True)
        order = self._make_order_with_items(product=product, status='pending')
        product.quantity -= 2
        product.save(update_fields=['quantity'])

        self.assertTrue(order.cancel())
        product.refresh_from_db()
        self.assertEqual(product.quantity, 100)

        # Second call is a no-op and must NOT push stock to 102.
        self.assertFalse(order.cancel())
        product.refresh_from_db()
        self.assertEqual(product.quantity, 100)
