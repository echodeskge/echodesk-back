"""
In-app + push notifications for the e-commerce module.

Types (users.Notification.NOTIFICATION_TYPES):

* ``ecommerce_order_created``: a cash-on-delivery order was placed (the
  order is final the moment it is placed). Card orders are announced when
  they are paid instead, so abandoned card checkouts never notify.
* ``ecommerce_order_paid``: an order's payment_status became ``paid``
  (BOG / TBC / Flitt webhooks, admin mark-paid).
* ``ecommerce_low_stock``: daily low-stock digest (tasks.check_low_stock_products).

Deep links: ``/ecommerce/orders/<id>``, ``/ecommerce/products/<id>`` (one
product) or ``/ecommerce/products?low_stock=true`` (several).

Recipients (``ecommerce_notification_recipients``): active users who can
use the e-commerce module (superuser, staff of a tenant whose plan has the
feature, or a member of an active group with the feature) AND are staff,
superuser, admin/manager role or have ``can_access_orders``. Capped at 50.
"""
import logging

from django.db import connection
from django.db.models import Q

logger = logging.getLogger(__name__)

FEATURE_KEY = 'ecommerce_crm'
RECIPIENT_CAP = 50
CURRENCY = 'GEL'

ORDER_CREATED = 'ecommerce_order_created'
ORDER_PAID = 'ecommerce_order_paid'
LOW_STOCK = 'ecommerce_low_stock'


def _current_tenant():
    from tenants.models import Tenant
    tenant = getattr(connection, 'tenant', None)
    if tenant is not None and getattr(tenant, 'pk', None):
        return tenant
    schema = getattr(connection, 'schema_name', None)
    if not schema or schema == 'public':
        return None
    return Tenant.objects.filter(schema_name=schema).first()


def ecommerce_notification_recipients(limit=RECIPIENT_CAP):
    from django.contrib.auth import get_user_model
    from tenants.subscription_service import SubscriptionService
    from .permissions import MANAGER_ROLES

    tenant = _current_tenant()
    if tenant is None or not SubscriptionService.check_tenant_feature(tenant, FEATURE_KEY):
        return []

    User = get_user_model()
    can_use_module = (
        Q(is_superuser=True)
        | Q(is_staff=True)
        | Q(
            tenant_groups__is_active=True,
            tenant_groups__features__key=FEATURE_KEY,
            tenant_groups__features__is_active=True,
        )
    )
    may_handle_orders = (
        Q(is_superuser=True)
        | Q(is_staff=True)
        | Q(role__in=MANAGER_ROLES)
        | Q(can_access_orders=True)
    )
    ids = (
        User.objects.filter(is_active=True)
        .filter(can_use_module)
        .filter(may_handle_orders)
        .values_list('id', flat=True)
        .distinct()
        .order_by('id')[:limit]
    )
    return list(User.objects.filter(id__in=list(ids)).order_by('id'))


def _already_sent(notification_type, key, value):
    from users.models import Notification
    return Notification.objects.filter(
        notification_type=notification_type, **{f'metadata__{key}': value}
    ).exists()


def _fan_out(notification_type, title, message, metadata, link_url):
    from users.notification_utils import create_notification

    sent = 0
    for user in ecommerce_notification_recipients():
        try:
            create_notification(
                user=user,
                notification_type=notification_type,
                title=title,
                message=message,
                metadata=dict(metadata),
                link_url=link_url,
            )
            sent += 1
        except Exception:
            logger.exception('Failed to create %s notification for user %s', notification_type, user.pk)
    return sent


def _order_metadata(order):
    return {
        'order_id': order.id,
        'order_number': order.order_number,
        'total_amount': str(order.total_amount),
        'payment_method': order.payment_method or '',
        'customer_name': order.contact_name or '',
    }


def order_link(order_id):
    return f'/ecommerce/orders/{order_id}'


def product_link(product_id):
    return f'/ecommerce/products/{product_id}'


LOW_STOCK_LIST_LINK = '/ecommerce/products?low_stock=true'


def notify_order_created(order):
    """New cash-on-delivery order. Sent once per order."""
    if _already_sent(ORDER_CREATED, 'order_id', order.id):
        return 0
    name = order.contact_name or 'A customer'
    return _fan_out(
        ORDER_CREATED,
        title=f'New order {order.order_number}',
        message=f'{name} ordered {order.total_amount} {CURRENCY} (cash on delivery)',
        metadata=_order_metadata(order),
        link_url=order_link(order.id),
    )


def notify_order_paid(order):
    """Order payment cleared. Sent once per order."""
    if _already_sent(ORDER_PAID, 'order_id', order.id):
        return 0
    name = order.contact_name or 'A customer'
    return _fan_out(
        ORDER_PAID,
        title=f'Order {order.order_number} paid',
        message=f'{name} paid {order.total_amount} {CURRENCY}',
        metadata=_order_metadata(order),
        link_url=order_link(order.id),
    )


def notify_low_stock(products, total_count=None):
    """One digest notification for the products that are low on stock."""
    products = list(products)
    if not products:
        return 0
    total = total_count if total_count is not None else len(products)

    def _name(p):
        if isinstance(p.name, dict):
            return p.name.get('en') or next(iter(p.name.values()), p.sku) or p.sku
        return str(p.name or p.sku)

    if total == 1:
        product = products[0]
        title = f'Low stock: {_name(product)}'
        message = f'{product.sku} has {product.quantity} left (alert at {product.low_stock_threshold})'
        link = product_link(product.id)
    else:
        names = ', '.join(_name(p) for p in products[:3])
        more = f' and {total - 3} more' if total > 3 else ''
        title = f'{total} products are low on stock'
        message = f'{names}{more}'
        link = LOW_STOCK_LIST_LINK
    metadata = {
        'product_ids': [p.id for p in products[:50]],
        'product_id': products[0].id if total == 1 else None,
        'count': total,
    }
    return _fan_out(LOW_STOCK, title=title, message=message, metadata=metadata, link_url=link)
