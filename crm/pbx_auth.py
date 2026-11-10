"""Authentication of requests that come from a tenant's PBX (Asterisk).

The AGI routing script and the dialplan hangup handlers call the backend
with the PBX's long-lived ``PbxServer.api_token`` in the ``X-PBX-Token``
header (``?pbx_token=`` is accepted as a fallback for tools that cannot
set headers). PbxServer rows live in tenant schemas, so a token is mapped
to its tenant by scanning the tenant registry once and caching the result
under ``sha256(token)`` (never the raw token).

Accepted credentials, in order:

1. ``X-PBX-Token`` matching ``PbxServer.api_token`` of an ``active`` or
   ``provisioning`` server (the installer bakes the token into the systemd
   drop-in before the ping flips the status to active);
2. ``X-PBX-Token`` matching ``PbxServer.enrollment_token`` of an ``active``
   server: legacy identifier for drop-ins written before ``api_token``
   existed (pbx2). Logged as deprecated; the runbook rotates it;
3. ``Authorization: Bearer <PBX_SHARED_SECRET>``, only while the setting is
   non-empty, compared in constant time. The tenant then comes from the
   request host (the tenant middleware already switched the schema); a
   request to the public schema has no tenant and is refused.

Anything else is 401. The tenant never comes from the host name alone.

:func:`pbx_authenticated` wraps a DRF function view: it sets
``request.pbx`` (the PbxServer, ``None`` on the shared-secret path) and
``request.pbx_schema`` and runs the view inside that tenant's schema.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass
from functools import wraps
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from rest_framework import status
from rest_framework.response import Response

logger = logging.getLogger(__name__)

CACHE_PREFIX = 'pbx_auth:'
CACHE_TTL = 60 * 10  # 10 minutes

VIA_API_TOKEN = 'api_token'
VIA_ENROLLMENT_TOKEN = 'enrollment_token'
VIA_SHARED_SECRET = 'shared_secret'

# Which PbxServer statuses may authenticate with which token field.
_ACCEPTED_STATUSES = {
    VIA_API_TOKEN: ('active', 'provisioning'),
    VIA_ENROLLMENT_TOKEN: ('active',),
}


@dataclass(frozen=True)
class PbxAuth:
    schema: str
    pbx: object  # crm.models.PbxServer, or None on the shared-secret path
    via: str


def _cache_key(token: str) -> str:
    return CACHE_PREFIX + hashlib.sha256(token.encode('utf-8')).hexdigest()


def _token_from_request(request) -> str:
    meta = getattr(request, 'META', {}) or {}
    token = meta.get('HTTP_X_PBX_TOKEN', '')
    if not token:
        params = getattr(request, 'GET', None)
        if params is not None:
            token = params.get('pbx_token', '')
    return (token or '').strip()


def _load_pbx(schema: str, pbx_id: int):
    from tenant_schemas.utils import schema_context

    from crm.models import PbxServer

    with schema_context(schema):
        return PbxServer.objects.filter(pk=pbx_id).first()


def _matched_field(pbx, token: str) -> Optional[str]:
    """Which token field of ``pbx`` equals ``token`` (constant time), or None."""
    for field in (VIA_API_TOKEN, VIA_ENROLLMENT_TOKEN):
        value = getattr(pbx, field, '') or ''
        if value and hmac.compare_digest(value.encode('utf-8'), token.encode('utf-8')):
            return field
    return None


def _scan_tenants(token: str):
    """``(schema, PbxServer, field)`` for the one PbxServer whose api_token or
    enrollment_token is ``token``, scanning every tenant schema; None when
    no server carries it."""
    from django.db.models import Q
    from tenant_schemas.utils import schema_context
    from tenants.models import Tenant

    try:
        tenants = list(Tenant.objects.exclude(schema_name='public').values_list('schema_name', flat=True))
    except Exception:  # noqa: BLE001 - registry unavailable: treat as unknown token
        logger.exception('pbx_auth: tenant registry lookup failed')
        return None

    for schema in tenants:
        try:
            with schema_context(schema):
                from crm.models import PbxServer

                pbx = PbxServer.objects.filter(
                    Q(api_token=token) | Q(enrollment_token=token)
                ).first()
                if pbx is None:
                    continue
                field = _matched_field(pbx, token)
                if field is None:
                    continue
                return schema, pbx, field
        except Exception:  # noqa: BLE001 - a broken tenant must not hide the others
            logger.debug('pbx_auth: lookup failed in schema %s', schema, exc_info=True)
            continue
    return None


def lookup_pbx_token(token: str):
    """Resolve a PBX token to ``(schema, PbxServer, field)`` regardless of
    the server's status, or None. ``field`` is ``'api_token'`` or
    ``'enrollment_token'``. The tenant mapping is cached by the token's
    SHA-256 for 10 minutes; on a hit the row is re-read and the token
    re-compared, so a rotated or revoked token stops working at once."""
    if not token:
        return None
    key = _cache_key(token)
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001 - cache outage: fall back to the scan
        cached = None
    if cached:
        schema, pbx_id = cached
        try:
            pbx = _load_pbx(schema, pbx_id)
        except Exception:  # noqa: BLE001
            pbx = None
        field = _matched_field(pbx, token) if pbx is not None else None
        if field is not None:
            return schema, pbx, field
        try:
            cache.delete(key)
        except Exception:  # noqa: BLE001
            pass

    found = _scan_tenants(token)
    if found is None:
        return None
    schema, pbx, field = found
    try:
        cache.set(key, (schema, pbx.pk), CACHE_TTL)
    except Exception:  # noqa: BLE001
        pass
    return schema, pbx, field


def find_pbx_by_enrollment_token(token: str):
    """``(schema, pbx_id)`` of the PbxServer whose *enrollment* token this is
    (any status: the installer serves pending servers), else ``(None, None)``.
    The long-lived api_token is deliberately not accepted here."""
    found = lookup_pbx_token((token or '').strip())
    if found is None:
        return None, None
    schema, pbx, field = found
    if field != VIA_ENROLLMENT_TOKEN:
        return None, None
    return schema, pbx.pk


def shared_secret_ok(request) -> bool:
    """True when the request carries ``Authorization: Bearer <PBX_SHARED_SECRET>``
    and that setting is non-empty (constant-time compare). The host is not
    looked at here; :func:`resolve_pbx` adds the tenant-host requirement."""
    secret = getattr(settings, 'PBX_SHARED_SECRET', '') or ''
    if not secret:
        return False
    header = (getattr(request, 'META', {}) or {}).get('HTTP_AUTHORIZATION', '') or ''
    scheme, _, presented = header.strip().partition(' ')
    if scheme.lower() != 'bearer':
        return False
    presented = presented.strip()
    return bool(presented) and hmac.compare_digest(
        presented.encode('utf-8'), secret.encode('utf-8'),
    )


def resolve_pbx(request) -> Optional[PbxAuth]:
    """The PBX behind this request, or None when it must be refused (401)."""
    token = _token_from_request(request)
    if token:
        found = lookup_pbx_token(token)
        if found is None:
            logger.warning('pbx_auth: unknown X-PBX-Token')
            return None
        schema, pbx, field = found
        if pbx.status not in _ACCEPTED_STATUSES[field]:
            logger.warning(
                'pbx_auth: PbxServer %s in tenant %s is %s; token refused',
                pbx.pk, schema, pbx.status,
            )
            return None
        if field == VIA_ENROLLMENT_TOKEN:
            logger.warning(
                'pbx_auth: tenant %s PbxServer %s still authenticates with the '
                'enrollment token (deprecated); rotate it to api_token',
                schema, pbx.pk,
            )
        return PbxAuth(schema=schema, pbx=pbx, via=field)

    if shared_secret_ok(request):
        schema = getattr(connection, 'schema_name', None)
        if not schema or schema == 'public':
            logger.warning('pbx_auth: shared secret presented without a tenant host')
            return None
        return PbxAuth(schema=schema, pbx=None, via=VIA_SHARED_SECRET)

    return None


def pbx_authenticated(view):
    """Decorator for DRF ``@api_view`` functions called by a PBX.

    Place it *below* ``@api_view`` / ``@permission_classes`` so it sees the
    DRF request. 401 unless :func:`resolve_pbx` identifies the PBX; then
    ``request.pbx`` / ``request.pbx_schema`` are set and the view runs
    inside that tenant's schema.
    """
    from tenant_schemas.utils import schema_context

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        auth = resolve_pbx(request)
        if auth is None:
            return Response({'error': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        request.pbx = auth.pbx
        request.pbx_schema = auth.schema
        request.pbx_auth_via = auth.via
        with schema_context(auth.schema):
            return view(request, *args, **kwargs)

    return wrapper
