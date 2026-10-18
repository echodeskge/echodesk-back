"""
Short-lived response cache for the public storefront endpoints.

Tenant storefronts (Next.js, server side) hit the anonymous client
endpoints -- product list/detail, reviews, shipping methods, theme -- in
bursts of hundreds of requests a minute. Every one of them used to open a
Postgres connection. This module caches the rendered JSON for a few
seconds so a burst costs one DB round-trip per distinct URL.

Design
------
* Only anonymous GET/HEAD requests are cached. Any ``Authorization``
  header, session cookie or authenticated user bypasses the cache
  entirely (read and write).
* Only 200 responses rendered as JSON are stored.
* The cache key covers: tenant schema, a per-tenant "version" token,
  host, path, the query string (keys sorted, value order kept because DRF
  reads the *last* value of a repeated key), ``Accept-Language`` and the
  ``X-Language`` / ``X-Currency`` headers.
* Invalidation is eager: signals in ``ecommerce_crm.signals`` call
  :func:`schedule_version_bump` whenever catalogue data changes, which
  rotates the tenant's version token after the transaction commits. Old
  entries are never read again and simply expire.
* Cache failures (Redis down, serialisation errors) are logged and the
  request falls through to the normal, uncached view.

Shoppers may therefore see prices/stock that are at most
``STOREFRONT_CACHE_TTL`` seconds old *if* a change slipped past the
signals (e.g. a raw ``QuerySet.update``). Checkout always re-reads and
locks the product rows, so this never affects what is charged.
"""
import functools
import hashlib
import logging
import time
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse
from django.utils.cache import patch_response_headers

logger = logging.getLogger(__name__)

STOREFRONT_CACHE_TTL = getattr(settings, 'STOREFRONT_CACHE_TTL', 30)

_VERSION_KEY = 'storefront:v1:ver:{schema}'
_RESPONSE_KEY = 'storefront:v1:resp:{schema}:{version}:{digest}'

# Request headers that can change the rendered body. ``language`` as a
# query param is already part of the query string.
_VARY_HEADERS = ('HTTP_ACCEPT_LANGUAGE', 'HTTP_X_LANGUAGE', 'HTTP_X_CURRENCY')

# Response headers worth replaying on a hit (everything else is added by
# middleware / DRF on every request anyway).
_REPLAY_HEADERS = ('Content-Language',)


def _current_schema():
    return getattr(connection, 'schema_name', None) or 'public'


def _version_key(schema):
    return _VERSION_KEY.format(schema=schema)


def _new_token():
    return str(time.time_ns())


def get_version(schema):
    """Return the tenant's current cache version token.

    A missing token is initialised with a fresh, never-before-used value
    (``add`` keeps concurrent initialisers consistent), so an evicted
    version key can never resurrect entries written under an older one.
    """
    key = _version_key(schema)
    version = cache.get(key)
    if version is None:
        cache.add(key, _new_token(), timeout=None)
        version = cache.get(key)
        if version is None:  # pragma: no cover - cache silently not storing
            raise RuntimeError('storefront cache version could not be stored')
    return version


def bump_version(schema=None):
    """Rotate the tenant's version token so every cached response for that
    tenant becomes unreachable. Never raises."""
    schema = schema or _current_schema()
    try:
        cache.set(_version_key(schema), _new_token(), timeout=None)
    except Exception:  # noqa: BLE001 - cache outage must not break writes
        logger.warning('storefront cache: version bump failed for %s', schema, exc_info=True)


def schedule_version_bump(schema=None):
    """Invalidate the tenant's cached storefront responses.

    Outside a transaction this bumps once, immediately. Inside one it bumps
    now *and* again after commit: a concurrent reader may re-cache the
    still-committed old rows under the first new version before our commit
    lands, and the post-commit bump makes that entry unreachable. (The
    immediate bump also keeps behaviour intuitive under TestCase, whose
    on_commit hooks never fire.)"""
    schema = schema or _current_schema()
    bump_version(schema)
    try:
        if transaction.get_connection().in_atomic_block:
            transaction.on_commit(functools.partial(bump_version, schema))
    except Exception:  # noqa: BLE001
        logger.warning('storefront cache: could not schedule bump for %s', schema, exc_info=True)


def _django_request(request):
    """DRF wraps the HttpRequest; return the underlying Django request."""
    return getattr(request, '_request', request)


def is_cacheable_request(request):
    if request.method not in ('GET', 'HEAD'):
        return False
    raw = _django_request(request)
    meta = raw.META
    if meta.get('HTTP_AUTHORIZATION'):
        return False
    if settings.SESSION_COOKIE_NAME in raw.COOKIES:
        return False
    # The DRF request's user is already resolved by ``initial()`` (these
    # views use no authenticators, so it is AnonymousUser and costs no
    # query). Deliberately do NOT touch the Django-level lazy user: that
    # would hit the session table.
    user = getattr(request, '_user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return False
    return True


def build_cache_key(request, schema=None, version=None):
    raw = _django_request(request)
    schema = schema or _current_schema()
    if version is None:
        version = get_version(schema)
    query_items = []
    for key in sorted(raw.GET.keys()):
        for value in raw.GET.getlist(key):
            query_items.append((key, value))
    parts = [
        raw.method if raw.method != 'HEAD' else 'GET',
        raw.get_host().lower(),
        raw.path,
        urlencode(query_items),
    ]
    parts.extend(raw.META.get(h, '') for h in _VARY_HEADERS)
    digest = hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()
    return _RESPONSE_KEY.format(schema=schema, version=version, digest=digest)


def _store(key, response):
    try:
        if response.status_code != 200 or response.streaming:
            return
        renderer = getattr(response, 'accepted_renderer', None)
        if renderer is not None and getattr(renderer, 'format', None) != 'json':
            return  # never cache the browsable API's HTML
        if response.cookies:
            return
        payload = {
            'content': response.content,
            'content_type': response.get('Content-Type', 'application/json'),
            'headers': {h: response[h] for h in _REPLAY_HEADERS if response.has_header(h)},
        }
        cache.set(key, payload, timeout=STOREFRONT_CACHE_TTL)
    except Exception:  # noqa: BLE001
        logger.warning('storefront cache: store failed', exc_info=True)


def cache_public_response(view_func):
    """Decorator for DRF viewset actions (``def list(self, request, ...)``)
    and ``@api_view`` functions (``def view(request, ...)``; apply it
    *below* ``@api_view`` so it receives the DRF request)."""

    @functools.wraps(view_func)
    def wrapper(*args, **kwargs):
        if args and isinstance(_django_request(args[0]), HttpRequest):
            request = args[0]
        else:
            request = args[1]

        if not is_cacheable_request(request):
            return view_func(*args, **kwargs)

        try:
            key = build_cache_key(request)
            cached = cache.get(key)
        except Exception:  # noqa: BLE001 - Redis down: serve uncached
            logger.warning('storefront cache: lookup failed', exc_info=True)
            return view_func(*args, **kwargs)

        if cached is not None:
            try:
                response = HttpResponse(
                    cached['content'], content_type=cached['content_type'], status=200,
                )
                for header, value in cached.get('headers', {}).items():
                    response[header] = value
                response['X-Storefront-Cache'] = 'HIT'
                patch_response_headers(response, STOREFRONT_CACHE_TTL)
                return response
            except Exception:  # noqa: BLE001 - corrupt entry: serve uncached
                logger.warning('storefront cache: bad entry', exc_info=True)

        response = view_func(*args, **kwargs)
        if getattr(response, 'status_code', None) == 200:
            response['X-Storefront-Cache'] = 'MISS'
            patch_response_headers(response, STOREFRONT_CACHE_TTL)
            if hasattr(response, 'add_post_render_callback'):
                # DRF Responses are rendered after the view returns.
                response.add_post_render_callback(lambda r: _store(key, r))
            else:
                _store(key, response)
        return response

    return wrapper
