"""Shared helpers for tenant storefront payments."""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class BogCredentialError(RuntimeError):
    """Raised when a tenant has BOG credentials configured but they can't be
    loaded. We must never silently fall back to the platform merchant account
    in that case — doing so routes the tenant's customer payments into
    EchoDesk's own BOG account."""


def get_tenant_bog_service(tenant=None):
    """Return a ``BOGPaymentService`` configured with the tenant's own BOG
    merchant credentials, or the platform credentials when the tenant hasn't
    configured any.

    ``tenant`` may be None when called from a request already bound to a tenant
    schema (e.g. the payment webhook): the single per-schema ``EcommerceSettings``
    row is used in that case.

    Raises ``BogCredentialError`` if the tenant HAS configured credentials but
    the secret can't be decrypted/loaded — callers must surface an error rather
    than charge into the platform account.
    """
    from tenants.bog_payment import BOGPaymentService
    from .models import EcommerceSettings

    client_id = settings.BOG_CLIENT_ID
    client_secret = settings.BOG_CLIENT_SECRET
    auth_url = settings.BOG_AUTH_URL
    api_base_url = settings.BOG_API_BASE_URL

    if tenant is not None:
        ecommerce_settings = EcommerceSettings.objects.filter(tenant=tenant).first()
    else:
        ecommerce_settings = EcommerceSettings.objects.first()

    if ecommerce_settings is not None and ecommerce_settings.has_bog_credentials:
        # The tenant configured their own merchant account.
        client_id = ecommerce_settings.bog_client_id
        try:
            client_secret = ecommerce_settings.get_bog_secret()
        except Exception as exc:  # decryption / key-rotation failure
            raise BogCredentialError(
                f'Tenant {getattr(tenant, "schema_name", tenant)!r} has BOG '
                f'credentials configured but the secret could not be loaded: {exc}'
            ) from exc
        if not client_secret:
            raise BogCredentialError(
                f'Tenant {getattr(tenant, "schema_name", tenant)!r} has BOG '
                f'credentials configured but the loaded secret is empty.'
            )

    service = BOGPaymentService()
    service.client_id = client_id
    service.client_secret = client_secret
    service.auth_url = auth_url
    service.base_url = api_base_url
    return service


# --- Per-order card provider selection ---

# Card gateways a storefront order can be charged through.
CARD_PROVIDERS = ('bog', 'tbc', 'flitt')

# Webhook path (under /api/ecommerce/) each provider calls back on.
WEBHOOK_PATHS = {
    'bog': 'payment-webhook/',
    'tbc': 'payment-webhook/tbc/',
    'flitt': 'payment-webhook/flitt/',
}


class PaymentProviderError(ValueError):
    """The requested card provider can't be used for this shop. The message
    is safe to return to the storefront as a 400."""


def resolve_card_provider(data, ecommerce_settings):
    """Pick the card provider for a storefront order.

    Reads ``payment_provider`` from the request; ``payment_method`` set to a
    provider key (e.g. ``"flitt"``) is accepted as an alias. Without either
    the order goes through BOG, as it always has. A provider the shop hasn't
    enabled or configured is rejected — never swapped for another bank.
    """
    provider = (data.get('payment_provider') or '').strip().lower()
    if not provider:
        method = (data.get('payment_method') or '').strip().lower()
        if method in CARD_PROVIDERS:
            provider = method
    if not provider:
        return 'bog'

    if provider not in CARD_PROVIDERS:
        raise PaymentProviderError(
            f"Unknown payment_provider '{provider}'. Expected one of: {', '.join(CARD_PROVIDERS)}."
        )

    active = (ecommerce_settings.active_payment_providers or []) if ecommerce_settings else []
    if active and provider not in active:
        raise PaymentProviderError(f"Payment provider '{provider}' is not enabled for this shop.")

    if provider == 'tbc' and not (ecommerce_settings and ecommerce_settings.has_tbc_credentials):
        raise PaymentProviderError("Payment provider 'tbc' is not configured for this shop.")
    if provider == 'flitt' and not (ecommerce_settings and ecommerce_settings.has_flitt_credentials):
        raise PaymentProviderError("Payment provider 'flitt' is not configured for this shop.")

    return provider


def create_card_payment(request, order, provider, *, customer_email='', customer_name='',
                        customer_phone='', return_url_success='', return_url_fail=''):
    """Open a hosted card-payment session for ``order`` with ``provider``.

    Returns ``{'payment_url', 'provider_order_id', 'raw'}`` where ``raw`` is the
    provider's response to keep in ``Order.payment_metadata``. BOG may raise
    ``BogCredentialError``; any provider may raise on a gateway failure.
    """
    callback_url = f"https://{request.get_host()}/api/ecommerce/{WEBHOOK_PATHS[provider]}"

    if provider == 'bog':
        result = get_tenant_bog_service(request.tenant).create_payment(
            amount=float(order.total_amount),
            currency='GEL',
            description=f"Order {order.order_number}",
            customer_email=customer_email,
            customer_name=customer_name,
            customer_phone=customer_phone,
            return_url_success=return_url_success,
            return_url_fail=return_url_fail,
            callback_url=callback_url,
            external_order_id=order.order_number,
            metadata={
                'order_id': order.id,
                'order_number': order.order_number,
                'tenant_id': request.tenant.id,
            },
        )
        return {
            'payment_url': result['payment_url'],
            'provider_order_id': result['order_id'],
            'raw': result,
        }

    from tenants.payment_providers.factory import _get_provider_instance
    result = _get_provider_instance(provider).create_payment(
        amount=order.total_amount,
        currency='GEL',
        external_order_id=order.order_number,
        description=f"Order {order.order_number}",
        customer_email=customer_email,
        customer_name=customer_name,
        return_url_success=return_url_success,
        return_url_fail=return_url_fail,
        callback_url=callback_url,
    )
    if not result.payment_url:
        raise RuntimeError(f'{provider} returned no payment URL for order {order.order_number}')
    return {
        'payment_url': result.payment_url,
        'provider_order_id': result.provider_order_id,
        'raw': {
            'provider': provider,
            'provider_order_id': result.provider_order_id,
            'payment_url': result.payment_url,
            'amount': str(result.amount),
            'currency': result.currency,
        },
    }
