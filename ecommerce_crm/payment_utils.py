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
