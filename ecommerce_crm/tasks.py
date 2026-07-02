"""
Celery tasks for ecommerce_crm app.
Handles asynchronous email sending for order lifecycle events.
"""
import logging
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_order_email(self, schema_name, order_id, email_type):
    """
    Send order-related emails: confirmation, shipped, delivered.

    Args:
        schema_name: Tenant schema name for multi-tenant context
        order_id: Order ID to fetch
        email_type: One of 'confirmation', 'shipped', 'delivered'
    """
    from tenant_schemas.utils import schema_context

    try:
        with schema_context(schema_name):
            from .models import Order
            from .email_utils import send_email

            order = Order.objects.select_related(
                'client', 'delivery_address', 'shipping_method'
            ).prefetch_related('items__product').get(id=order_id)

            client = order.client
            items = order.items.all()

            # Resolve the storefront URL for the public-track link. The
            # tenant's `frontend_url` is the canonical place to put the
            # storefront URL for this tenant; if missing, fall back to
            # the tenant's first verified custom domain or its
            # subdomain on `.ecommerce.echodesk.ge`.
            storefront_url = ''
            try:
                from tenants.models import Tenant, EcommerceCustomDomain
                tenant = Tenant.objects.get(schema_name=schema_name)
                storefront_url = (tenant.frontend_url or '').rstrip('/')
                if not storefront_url:
                    custom = EcommerceCustomDomain.objects.filter(
                        tenant=tenant,
                        is_verified=True,
                    ).first()
                    if custom:
                        storefront_url = f"https://{custom.domain}"
                    else:
                        storefront_url = f"https://{schema_name}.ecommerce.echodesk.ge"
            except Exception:
                storefront_url = f"https://{schema_name}.ecommerce.echodesk.ge"

            public_order_url = (
                f"{storefront_url}/order-confirmation"
                f"?order_id={order.id}&token={order.public_token}"
            ) if order.public_token else f"{storefront_url}/order-confirmation?order_id={order.id}"

            # Build common context
            context = {
                'order': order,
                'order_number': order.order_number,
                'frontend_url': storefront_url,
                'public_order_url': public_order_url,
                'public_token': order.public_token,
                'client_name': client.first_name or 'Valued Customer',
                'client_email': client.email,
                'items': [
                    {
                        'product_name': _get_product_name(item.product_name),
                        'quantity': item.quantity,
                        'price': item.price,
                        'subtotal': item.subtotal,
                    }
                    for item in items
                ],
                'subtotal': order.subtotal or order.total_amount,
                'shipping_cost': order.shipping_cost,
                'tax_amount': order.tax_amount,
                'discount_amount': order.discount_amount,
                'total_amount': order.total_amount,
            }

            if order.delivery_address:
                context['delivery_address'] = {
                    'label': order.delivery_address.label,
                    'address': order.delivery_address.address,
                    'city': order.delivery_address.city,
                }

            # Add store branding context
            try:
                from .models import EcommerceSettings
                store_settings = EcommerceSettings.objects.first()
                context['store_name'] = store_settings.store_name if store_settings and store_settings.store_name else 'Our Store'
                context['store_email'] = store_settings.store_email if store_settings and store_settings.store_email else ''
                context['store_phone'] = store_settings.store_phone if store_settings and store_settings.store_phone else ''
                context['store_logo'] = ''
                # Theme colors for email branding
                if store_settings:
                    context['theme_primary_color'] = store_settings.theme_primary_color or '221 83% 53%'
            except Exception:
                context['store_name'] = 'Our Store'
                context['store_email'] = ''
                context['store_phone'] = ''
                context['store_logo'] = ''
                context['theme_primary_color'] = '221 83% 53%'

            if email_type == 'confirmation':
                subject = f'Order Confirmed - {order.order_number}'
                template_name = 'order_confirmation'

            elif email_type == 'shipped':
                subject = f'Order Shipped - {order.order_number}'
                template_name = 'order_shipped'
                context['tracking_number'] = order.tracking_number
                context['courier_provider'] = order.courier_provider
                context['estimated_delivery_date'] = order.estimated_delivery_date

            elif email_type == 'delivered':
                subject = f'Order Delivered - {order.order_number}'
                template_name = 'order_delivered'

            else:
                logger.error(f'Unknown email type: {email_type}')
                return False

            success = send_email(
                subject=subject,
                recipient_email=client.email,
                template_name=template_name,
                context=context,
            )

            if success:
                logger.info(
                    f'Order {email_type} email sent for order {order.order_number} '
                    f'to {client.email}'
                )
            else:
                logger.warning(
                    f'Failed to send {email_type} email for order {order.order_number}'
                )

            # On a fresh order also notify the merchant. The customer
            # confirmation above goes to the buyer; this second email
            # goes to the tenant's `EcommerceSettings.store_email` so
            # the shop owner sees the sale come in. Quietly swallow
            # failures here — the customer-facing email is the one the
            # task's success status should reflect.
            if email_type == 'confirmation':
                try:
                    from .models import EcommerceSettings
                    s = EcommerceSettings.objects.first()
                    merchant_email = (s.store_email or '').strip() if s else ''
                    if merchant_email and merchant_email.lower() != (client.email or '').lower():
                        send_email(
                            subject=f'New Order #{order.order_number} — {order.total_amount} GEL',
                            recipient_email=merchant_email,
                            template_name='new_order_admin',
                            context=context,
                        )
                        logger.info(
                            f'Merchant new-order alert sent for order {order.order_number} '
                            f'to {merchant_email}'
                        )
                except Exception as merchant_exc:
                    logger.warning(
                        f'Failed to send merchant new-order alert for order '
                        f'{order.order_number}: {merchant_exc}'
                    )

            return success

    except Exception as exc:
        logger.error(
            f'Error sending order {email_type} email for order_id={order_id}: {exc}'
        )
        raise self.retry(exc=exc)


def _get_product_name(product_name_field):
    """Extract a display name from a product_name JSONField."""
    if isinstance(product_name_field, dict):
        return product_name_field.get('en', next(iter(product_name_field.values()), 'Product'))
    return str(product_name_field)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def check_low_stock_products(self):
    """Daily check for low stock products - sends email to store admin."""
    from tenant_schemas.utils import schema_context
    from tenants.models import Tenant
    from django.db.models import F

    try:
        tenants = Tenant.objects.exclude(schema_name='public')

        for tenant in tenants:
            try:
                with schema_context(tenant.schema_name):
                    from .models import Product, EcommerceSettings
                    from .email_utils import send_email

                    low_stock = Product.objects.filter(
                        track_inventory=True,
                        quantity__lte=F('low_stock_threshold'),
                        status='active',
                    )

                    if not low_stock.exists():
                        continue

                    settings_obj = EcommerceSettings.objects.first()
                    if not settings_obj or not settings_obj.store_email:
                        logger.info(
                            f'Low stock products found for tenant {tenant.schema_name} '
                            f'but no store_email configured. Skipping.'
                        )
                        continue

                    products_list = []
                    for product in low_stock[:50]:  # Limit to 50 products per email
                        products_list.append({
                            'name': _get_product_name(product.name),
                            'sku': product.sku,
                            'quantity': product.quantity,
                            'threshold': product.low_stock_threshold,
                        })

                    context = {
                        'products': products_list,
                        'total_low_stock': low_stock.count(),
                        'store_name': settings_obj.store_name or 'Your Store',
                    }

                    success = send_email(
                        subject=f'Low Stock Alert - {len(products_list)} products need attention',
                        recipient_email=settings_obj.store_email,
                        template_name='low_stock_alert',
                        context=context,
                    )

                    if success:
                        logger.info(
                            f'Low stock alert sent for tenant {tenant.schema_name}: '
                            f'{low_stock.count()} products'
                        )
                    else:
                        logger.warning(
                            f'Failed to send low stock alert for tenant {tenant.schema_name}'
                        )

            except Exception as e:
                logger.error(
                    f'Error checking low stock for tenant {tenant.schema_name}: {e}'
                )
                continue

    except Exception as exc:
        logger.error(f'Error in check_low_stock_products task: {exc}')
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def book_quickshipper_courier(self, schema_name, order_id):
    """Create a Quickshipper shipment for a paid order, then store the
    returned tracking ID on the local Order. Invoked from the BOG / TBC /
    Flitt webhook handlers (and the cash-on-delivery confirm path) the
    moment ``payment_status`` flips to ``paid``.

    No-op if the tenant doesn't have Quickshipper configured — keeps the
    BOG/TBC payment hooks free to call this unconditionally.
    """
    from tenant_schemas.utils import schema_context

    try:
        with schema_context(schema_name):
            from .models import Order, EcommerceSettings
            from .services.quickshipper import (
                client_from_settings,
                QuickshipperError,
            )

            order = (
                Order.objects
                .select_related('delivery_address__client')
                .prefetch_related('items__product')
                .filter(id=order_id)
                .first()
            )
            if not order:
                logger.warning('book_quickshipper_courier: order %s not found', order_id)
                return

            # Pickup orders never go through a courier — the customer
            # is collecting at the store. The delivery_method is
            # stamped on payment_metadata at order creation.
            if (order.payment_metadata or {}).get('delivery_method') == 'pickup':
                logger.info(
                    'book_quickshipper_courier: skipped — pickup order %s',
                    order_id,
                )
                return

            settings_obj = EcommerceSettings.objects.first()
            if not settings_obj or not settings_obj.quickshipper_enabled:
                logger.debug(
                    'book_quickshipper_courier: skipped — Quickshipper disabled for %s',
                    schema_name,
                )
                return

            client = client_from_settings(settings_obj)
            if client is None:
                logger.info(
                    'book_quickshipper_courier: no Quickshipper credentials for %s',
                    schema_name,
                )
                return

            if order.tracking_number:
                logger.info(
                    'book_quickshipper_courier: order %s already has tracking %s — skipping',
                    order.id, order.tracking_number,
                )
                return

            address = order.delivery_address
            if not address or address.latitude is None or address.longitude is None:
                logger.warning(
                    'book_quickshipper_courier: order %s missing delivery lat/lng',
                    order.id,
                )
                return

            # Pull cached quote details off the order's payment_metadata if the
            # checkout step persisted them; otherwise re-quote here so we have
            # a providerId / providerFeeId / parcelDimensionsId to attach.
            quote_meta = (order.payment_metadata or {}).get('quickshipper_quote') or {}
            provider_id = quote_meta.get('provider_id')
            provider_fee_id = quote_meta.get('provider_fee_id')
            parcel_dimensions_id = quote_meta.get('parcel_dimensions_id')

            if provider_id is None:
                # Re-quote on-the-fly. This is a fallback — checkout is meant
                # to persist the quote so the actual booking matches what the
                # customer paid for.
                cart_amount = float(order.total_amount or 0)
                cart_weight = sum(
                    (float(getattr(item.product, 'weight', None) or 0.5) * item.quantity)
                    for item in order.items.all()
                )
                try:
                    envelope = client.get_quote(
                        from_lat=settings_obj.quickshipper_pickup_latitude,
                        from_lng=settings_obj.quickshipper_pickup_longitude,
                        from_street=settings_obj.quickshipper_pickup_address,
                        from_city=settings_obj.quickshipper_pickup_city,
                        to_lat=address.latitude,
                        to_lng=address.longitude,
                        to_street=address.address,
                        to_city=address.city,
                        cart_amount=cart_amount,
                        cart_weight=cart_weight,
                    )
                except QuickshipperError as exc:
                    logger.warning(
                        'book_quickshipper_courier: re-quote failed for order %s: %s',
                        order.id, exc,
                    )
                    raise self.retry(exc=exc)
                fees = envelope.get('fees') or []
                cheapest = None
                for fee in fees:
                    if fee.get('isActive') is False:
                        continue
                    for price in (fee.get('prices') or []):
                        # Live response uses `amount`; fall back to
                        # `userPrice` for forward-compat with the published
                        # OpenAPI spec field name.
                        up = price.get('amount')
                        if up is None:
                            up = price.get('userPrice')
                        if up is None:
                            continue
                        if cheapest is None or up < cheapest['user_price']:
                            cheapest = {
                                'provider_id': fee.get('providerId'),
                                'provider_fee_id': price.get('id') or price.get('providerFeeId'),
                                'parcel_dimensions_id': price.get('parcelDimensionsId'),
                                'user_price': float(up),
                            }
                if cheapest is None:
                    logger.warning(
                        'book_quickshipper_courier: no quote available for order %s',
                        order.id,
                    )
                    return
                provider_id = cheapest['provider_id']
                provider_fee_id = cheapest['provider_fee_id']
                parcel_dimensions_id = cheapest['parcel_dimensions_id']

            payload = {
                'integrationOrderId': order.order_number,
                'cartAmount': float(order.total_amount or 0),
                'comment': order.notes or '',
                'pickUpInfo': {
                    'address': settings_obj.quickshipper_pickup_address or '',
                    'addressComment': settings_obj.quickshipper_pickup_extra_instructions or '',
                    'city': settings_obj.quickshipper_pickup_city or '',
                    'country': '',
                    'latitude': float(settings_obj.quickshipper_pickup_latitude) if settings_obj.quickshipper_pickup_latitude else 0,
                    'longitude': float(settings_obj.quickshipper_pickup_longitude) if settings_obj.quickshipper_pickup_longitude else 0,
                    'name': settings_obj.quickshipper_pickup_contact_name or '',
                    'phone': settings_obj.quickshipper_pickup_phone or '',
                    'phonePrefix': '',
                },
                'dropOffInfo': {
                    'address': address.address or '',
                    'addressComment': address.extra_instructions or '',
                    'city': address.city or '',
                    'country': address.country or '',
                    'latitude': float(address.latitude) if address.latitude is not None else 0,
                    'longitude': float(address.longitude) if address.longitude is not None else 0,
                    'name': address.client.full_name if address.client_id else '',
                    'phone': (address.client.phone or '') if address.client_id else '',
                    'phonePrefix': '',
                },
                'provider': {
                    'providerId': provider_id,
                    'providerFeeId': provider_fee_id,
                },
                'parcelDimensionId': parcel_dimensions_id,
                'autoAssign': True,
                'cashOnDelivery': (
                    {
                        'parcelPrice': float(order.total_amount or 0),
                    }
                    if order.payment_status != 'paid'
                    else None
                ),
            }
            # Strip None values so we don't trip Quickshipper's strict
            # additionalProperties=false on nested models.
            payload = {k: v for k, v in payload.items() if v is not None}

            try:
                resp = client.create_order(payload)
            except QuickshipperError as exc:
                logger.warning(
                    'book_quickshipper_courier: create_order failed for %s: %s',
                    order.id, exc,
                )
                raise self.retry(exc=exc)

            qs_order_id = resp.get('orderId')
            # Persist the tracking number IMMEDIATELY, before assembling metadata.
            # The Quickshipper order already exists at this point; if anything
            # below throws and the task retries, the tracking_number guard above
            # must already see it so we don't double-book the courier.
            if qs_order_id is not None:
                order.tracking_number = str(qs_order_id)
                order.courier_provider = 'quickshipper'
                order.save(update_fields=['tracking_number', 'courier_provider', 'updated_at'])

            tracking_url = resp.get('trackingUrl')
            metadata = order.payment_metadata or {}
            metadata['quickshipper'] = {
                'order_id': qs_order_id,
                'tracking_url': tracking_url,
                'provider_name': resp.get('providerName'),
                'delivery_fee': resp.get('deliveryFee'),
                'create_date': resp.get('createDate'),
            }
            order.payment_metadata = metadata
            order.save(update_fields=['payment_metadata', 'updated_at'])
            logger.info(
                'book_quickshipper_courier: order %s booked, tracking=%s',
                order.id, order.tracking_number,
            )
    except Exception as exc:
        logger.error('book_quickshipper_courier failed: %s', exc)
        raise self.retry(exc=exc)


# Orders unpaid after this many hours are treated as abandoned and cancelled,
# restoring their reserved stock. Mirrors booking_management's grace window.
UNPAID_ORDER_GRACE_HOURS = 24


@shared_task
def cancel_unpaid_orders():
    """Cancel card orders that were never paid, restoring reserved inventory.

    Stock is decremented at checkout, so an abandoned card order (customer closed
    the BOG page) would otherwise reserve that stock forever. This runs across all
    tenants and cancels+restocks orders still awaiting payment past the grace
    window. Restock-on-failure is done here (grace-based) rather than immediately
    on a 'rejected' webhook so a customer retrying payment can't be oversold.
    """
    from tenant_schemas.utils import schema_context
    from tenants.models import Tenant

    total_cancelled = 0
    for tenant in Tenant.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                total_cancelled += _cancel_unpaid_orders_for_tenant()
        except Exception:
            logger.exception('cancel_unpaid_orders failed for tenant %s', tenant.schema_name)
    logger.info('cancel_unpaid_orders completed: %d orders cancelled', total_cancelled)
    return total_cancelled


def _cancel_unpaid_orders_for_tenant():
    from datetime import timedelta
    from django.utils import timezone
    from .models import Order

    cutoff = timezone.now() - timedelta(hours=UNPAID_ORDER_GRACE_HOURS)
    # Only cash-up-front card orders that never got paid. Cash-on-delivery orders
    # are excluded (payment_method), and paid/cancelled orders are skipped.
    candidates = Order.objects.filter(
        status='pending',
        payment_status__in=['pending', 'failed'],
        created_at__lt=cutoff,
    ).exclude(payment_method='cash_on_delivery')

    cancelled = 0
    for order in candidates:
        try:
            if order.cancel(reason='Auto-cancelled: unpaid after grace period'):
                cancelled += 1
        except Exception:
            logger.exception('Failed to auto-cancel unpaid order %s', order.id)
    return cancelled
