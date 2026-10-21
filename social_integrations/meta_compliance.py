"""
Meta platform-compliance callbacks: signed_request verification, the
"Data Deletion Request Callback" and the "Deauthorize Callback".

What the Facebook ``user_id`` in a signed_request can (and cannot) be mapped to
-------------------------------------------------------------------------------
Meta sends the **app-scoped user id (ASID)** of the person who used Facebook
Login with the EchoDesk app. In EchoDesk that person is a business admin who
connected their Page / Instagram account; we store that id on
``FacebookPageConnection.connected_by_fb_user_id`` (filled since migration
0058 — older connections have it blank and cannot be matched).

Customers who message a connected Page/Instagram account never log in to our
app. Their messages are keyed by **page-scoped ids (PSID / IGSID)**, which are a
different id space from ASIDs, and Meta provides no ASID->PSID mapping to us
here. So a deletion callback can reliably:

* deactivate the Page connections that ASID granted, wipe their Page access
  tokens (and the tokens of Instagram accounts linked to those Pages), and
  forget the stored ASID;
* delete any message / social-account / cached-picture rows whose stored
  platform id equals the ASID (best effort — normally zero, because messages
  carry PSIDs/IGSIDs, not ASIDs).

It can NOT find a customer's Messenger/Instagram conversations by ASID. Those
are removed on request via support@echodesk.ge (the business that owns the
inbox is the controller of that data) — see the privacy policy.
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class SignedRequestError(ValueError):
    """The signed_request is malformed, unsigned or signed with another secret."""


class SignedRequestUnverifiable(RuntimeError):
    """No app secret is configured, so the signed_request cannot be verified."""


def _b64url_decode(value):
    value = value.strip()
    padding = '=' * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _app_secrets():
    from social_integrations.views import meta_app_secrets
    return meta_app_secrets()


def parse_signed_request(signed_request, app_secrets=None):
    """Verify and decode a Meta ``signed_request``.

    Format: ``<base64url(HMAC-SHA256(payload_b64, app_secret))>.<payload_b64>``
    where the payload is JSON with ``algorithm: "HMAC-SHA256"``.

    Returns the decoded payload dict. Raises SignedRequestError when invalid
    and SignedRequestUnverifiable when no app secret is configured.
    """
    if app_secrets is None:
        app_secrets = _app_secrets()
    if not app_secrets:
        raise SignedRequestUnverifiable('No Meta app secret configured')
    if not signed_request or not isinstance(signed_request, str) or '.' not in signed_request:
        raise SignedRequestError('Malformed signed_request')

    encoded_sig, payload_b64 = signed_request.split('.', 1)
    try:
        signature = _b64url_decode(encoded_sig)
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, TypeError) as exc:  # binascii.Error is a ValueError
        raise SignedRequestError('Undecodable signed_request') from exc
    if not isinstance(payload, dict):
        raise SignedRequestError('signed_request payload is not an object')
    if str(payload.get('algorithm', '')).upper() != 'HMAC-SHA256':
        raise SignedRequestError('Unsupported signed_request algorithm')

    for secret in app_secrets:
        expected = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
        if hmac.compare_digest(signature, expected):
            return payload
    raise SignedRequestError('signed_request signature mismatch')


def _tenant_schemas():
    from tenant_schemas.utils import get_tenant_model
    return list(
        get_tenant_model().objects.exclude(schema_name='public').values_list('schema_name', flat=True)
    )


def _revoke_connections_in_current_schema(user_id, forget_user_id):
    """Deactivate + wipe tokens for Page/IG connections granted by ``user_id``."""
    from social_integrations.models import FacebookPageConnection, InstagramAccountConnection

    pages = list(FacebookPageConnection.objects.filter(connected_by_fb_user_id=user_id))
    page_ids = []
    ig_count = 0
    now = timezone.now()
    for page in pages:
        for ig in InstagramAccountConnection.objects.filter(facebook_page=page):
            ig.is_active = False
            ig.access_token = ''
            ig.save()  # save() so platform-routing signals mark routes inactive
            ig_count += 1
        page.is_active = False
        page.page_access_token = ''
        page.deactivated_at = now
        page.deactivation_reason = 'permission_revoked'
        if forget_user_id:
            page.connected_by_fb_user_id = ''
        page.save()
        page_ids.append(page.page_id)
    return {'pages_deactivated': page_ids, 'instagram_accounts_deactivated': ig_count}


def deauthorize_user(user_id):
    """Deauthorize callback: the user removed the app. Deactivate the Page /
    Instagram connections they granted and wipe the tokens, in every tenant."""
    from tenant_schemas.utils import schema_context

    summary = {}
    for schema in _tenant_schemas():
        try:
            with schema_context(schema):
                result = _revoke_connections_in_current_schema(user_id, forget_user_id=False)
        except Exception as exc:  # one broken schema must not stop the rest
            logger.error('Deauthorize failed in schema %s: %s', schema, exc)
            summary[schema] = {'error': str(exc)}
            continue
        if result['pages_deactivated'] or result['instagram_accounts_deactivated']:
            summary[schema] = result
    return summary


def delete_user_data(user_id):
    """Data deletion callback work for one ASID across all tenant schemas.

    See the module docstring for exactly what an ASID can be mapped to.
    Returns ``(ok, details)``.
    """
    from tenant_schemas.utils import schema_context
    from social_integrations.models import (
        CachedProfilePicture, FacebookMessage, InstagramMessage, SocialAccount,
    )

    details = {'schemas': {}, 'errors': {}}
    for schema in _tenant_schemas():
        try:
            with schema_context(schema):
                result = _revoke_connections_in_current_schema(user_id, forget_user_id=True)
                result['facebook_messages_deleted'] = FacebookMessage.objects.filter(sender_id=user_id).delete()[0]
                result['instagram_messages_deleted'] = InstagramMessage.objects.filter(sender_id=user_id).delete()[0]
                result['social_accounts_deleted'] = SocialAccount.objects.filter(
                    platform__in=['facebook', 'instagram'], platform_id=user_id,
                ).delete()[0]
                result['profile_pictures_deleted'] = CachedProfilePicture.objects.filter(
                    platform__in=['facebook', 'instagram'], platform_id=user_id,
                ).delete()[0]
        except Exception as exc:
            logger.error('Data deletion failed in schema %s: %s', schema, exc)
            details['errors'][schema] = str(exc)
            continue
        if any(result[k] for k in result):
            details['schemas'][schema] = result
    return not details['errors'], details


def new_confirmation_code():
    return secrets.token_hex(10)


def deletion_status_url(confirmation_code):
    domain = getattr(settings, 'API_DOMAIN', 'api.echodesk.ge')
    return f"https://{domain}/api/social/facebook/data-deletion/status/{confirmation_code}/"
