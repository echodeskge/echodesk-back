"""Signed OAuth ``state`` for the social connect flows (Facebook, WhatsApp
embedded signup, TikTok).

The public OAuth callbacks have no session, so the state is the only thing
that says which tenant the authorisation belongs to. It used to be a plain
``tenant=<schema>`` string, which let anyone craft a callback that attached
their pages / WABA to another tenant. It is now a ``django.core.signing``
token carrying the tenant schema, a random nonce, the initiating user and
(optionally) ``return_to=app`` for the mobile app. Each state is accepted
once and expires after ``STATE_MAX_AGE`` seconds.
"""
import logging
import secrets
from urllib.parse import quote_plus, urlencode

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.http import HttpResponseRedirect

logger = logging.getLogger(__name__)

STATE_SALT = 'social_integrations.oauth_state.v1'
STATE_MAX_AGE = 60 * 60  # 1 hour to finish the Meta / TikTok dialog
USED_STATE_CACHE_KEY = 'social_oauth_state_used:{nonce}'

APP_RETURN = 'app'
APP_CONNECTIONS_URL = 'echodesk://social/connections'


def make_oauth_state(tenant_schema, *, user_id=None, return_to=None, publishing=False):
    payload = {'t': tenant_schema, 'n': secrets.token_urlsafe(16)}
    if user_id:
        payload['u'] = user_id
    if return_to == APP_RETURN:
        payload['r'] = APP_RETURN
    if publishing:
        payload['p'] = 1
    return signing.dumps(payload, salt=STATE_SALT, compress=True)


def read_oauth_state(state, *, consume=True):
    """Return the verified state dict, or None if it is missing, forged,
    expired or already used. Keys: tenant, user_id, return_to, publishing."""
    if not state:
        return None
    try:
        payload = signing.loads(state, salt=STATE_SALT, max_age=STATE_MAX_AGE)
    except signing.BadSignature:  # includes SignatureExpired
        logger.warning('Rejected OAuth callback with an invalid or expired state')
        return None
    if not isinstance(payload, dict) or not payload.get('t') or not payload.get('n'):
        return None
    if consume:
        try:
            first_use = cache.add(
                USED_STATE_CACHE_KEY.format(nonce=payload['n']), 1, STATE_MAX_AGE,
            )
        except Exception:  # noqa: BLE001 — cache down: signature + expiry still hold
            first_use = True
        if not first_use:
            logger.warning('Rejected a replayed OAuth state for tenant %s', payload['t'])
            return None
    return {
        'tenant': payload['t'],
        'user_id': payload.get('u'),
        'return_to': payload.get('r'),
        'publishing': bool(payload.get('p')),
    }


def return_to_from_request(request):
    """``app`` when the caller asked to come back to the mobile app."""
    value = (request.query_params.get('return_to') if hasattr(request, 'query_params')
             else request.GET.get('return_to'))
    return APP_RETURN if (value or '').strip().lower() == APP_RETURN else None


class AppSchemeRedirect(HttpResponseRedirect):
    allowed_schemes = ['echodesk', 'https', 'http']


def oauth_result_redirect(*, platform, ok, message='', state=None, tenant_schema=None, extra=None):
    """Where a callback sends the browser when it is done.

    - return_to=app → ``echodesk://social/connections?platform=<p>&status=<ok|error>&message=…``
      so ``WebBrowser.openAuthSessionAsync`` closes and reports the result.
    - otherwise the tenant dashboard's ``/social/connections`` page with the
      legacy ``<platform>_status=connected|error`` query (unchanged).
    """
    if state and state.get('return_to') == APP_RETURN:
        params = {'platform': platform, 'status': 'ok' if ok else 'error'}
        if message:
            params['message'] = message
        for key, value in (extra or {}).items():
            params[key] = value
        return AppSchemeRedirect(f'{APP_CONNECTIONS_URL}?{urlencode(params)}')

    schema = tenant_schema or (state or {}).get('tenant')
    base = f'https://{schema}.{settings.MAIN_DOMAIN}' if schema else f'https://{settings.MAIN_DOMAIN}'
    query = f'{platform}_status={"connected" if ok else "error"}'
    for key, value in (extra or {}).items():
        query += f'&{key}={quote_plus(str(value))}'
    if message:
        query += f'&message={quote_plus(message)}'
    return HttpResponseRedirect(f'{base}/social/connections?{query}')
