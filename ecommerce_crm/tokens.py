"""
JWTs for shop (ecommerce) customers.

Every token names the tenant that issued it and that it identifies a shop
customer. All tenants' tokens are signed with the same key and client ids are
per-tenant row numbers, so without these claims a token from another shop —
or from a booking site — would be accepted as whichever customer here has
the same id.
"""
from datetime import datetime, timezone as dt_timezone

from django.db import connection
from rest_framework_simplejwt.tokens import RefreshToken

SHOP_TOKEN_KIND = 'shop'

# Tokens issued before the claims existed carry neither `kind` nor `tenant`.
# They are honoured only if issued before this moment, so current shoppers
# stay logged in; refreshing replaces them with claimed tokens, and the last
# unclaimed refresh token expires 7 days after the cutoff (REFRESH_TOKEN_LIFETIME).
LEGACY_TOKEN_CUTOFF = datetime(2026, 10, 4, 22, 0, tzinfo=dt_timezone.utc).timestamp()


def issue_shop_tokens(client):
    """Refresh token (with `.access_token`) for a shop customer."""
    refresh = RefreshToken()
    refresh['client_id'] = client.id
    refresh['email'] = client.email
    refresh['kind'] = SHOP_TOKEN_KIND
    refresh['tenant'] = connection.schema_name
    return refresh


def is_shop_token_for_current_tenant(token):
    kind = token.get('kind')
    tenant = token.get('tenant')
    if kind is None and tenant is None:
        return (token.get('iat') or 0) < LEGACY_TOKEN_CUTOFF
    return kind == SHOP_TOKEN_KIND and tenant == connection.schema_name
