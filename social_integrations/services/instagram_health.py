"""
Instagram messaging health for connected accounts.

An Instagram account can be "connected" (valid Page token, webhook
subscription in place) and still never deliver a DM: when the account owner
turns off Instagram -> Settings -> Messages and story replies -> Message
controls -> Connected tools -> "Allow access to messages", Meta stops sending
messaging webhooks and refuses every messaging call with error #200 /
subcode 2534041. Nothing on our side fails loudly, so tenants just see
Instagram go quiet.

This module detects that condition — actively via a cheap read of the Page's
Instagram conversations, and passively from send failures — and stores it on
``InstagramAccountConnection.messaging_status`` so the connections page can
show the owner how to turn access back on.
"""
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Graph error #200 subcode 2534041: "the owner of this Instagram account has
# not allowed access to messages" (Connected tools toggle is off).
MESSAGING_DISABLED_SUBCODE = 2534041

STATUS_UNKNOWN = 'unknown'
STATUS_OK = 'ok'
STATUS_MESSAGING_DISABLED = 'messaging_disabled'
STATUS_ERROR = 'error'

# How long a check result is trusted before the status endpoint re-checks.
CHECK_STALE_AFTER = timedelta(hours=6)
CHECK_TIMEOUT_SECONDS = 10


def _graph_error(error_data):
    err = (error_data or {}).get('error') if isinstance(error_data, dict) else None
    return err if isinstance(err, dict) else {}


def is_messaging_disabled_error(error_data):
    """True for the 'Allow access to messages is off' Graph error."""
    err = _graph_error(error_data)
    try:
        subcode = int(err.get('error_subcode') or 0)
    except (TypeError, ValueError):
        return False
    return subcode == MESSAGING_DISABLED_SUBCODE


def _error_code(err):
    code = err.get('code')
    subcode = err.get('error_subcode')
    if code is None and subcode is None:
        return ''
    return f"{code or ''}/{subcode or ''}"


def _save(account, status, message='', code=''):
    account.messaging_status = status
    account.messaging_error = (message or '')[:1000]
    account.messaging_error_code = (code or '')[:32]
    account.messaging_checked_at = timezone.now()
    account.save(update_fields=[
        'messaging_status', 'messaging_error', 'messaging_error_code',
        'messaging_checked_at', 'updated_at',
    ])


def mark_messaging_ok(account):
    """Record a successful messaging call (send / incoming DM)."""
    if account.messaging_status == STATUS_OK and not account.messaging_error:
        return
    _save(account, STATUS_OK)


def record_messaging_error(account, error_data):
    """Record a messaging failure if it means access is disabled.

    Other send errors (closed window, unknown recipient, …) are about one
    conversation, not the account, so they are ignored here. Returns True when
    the account was flagged.
    """
    if not is_messaging_disabled_error(error_data):
        return False
    err = _graph_error(error_data)
    _save(account, STATUS_MESSAGING_DISABLED, err.get('message', ''), _error_code(err))
    logger.warning(
        "Instagram @%s: messaging access disabled by the account owner (%s)",
        account.username, _error_code(err),
    )
    return True


def needs_check(account, now=None):
    if not account.is_active:
        return False
    if account.messaging_status == STATUS_UNKNOWN or not account.messaging_checked_at:
        return True
    return (now or timezone.now()) - account.messaging_checked_at > CHECK_STALE_AFTER


def check_instagram_messaging(account):
    """Probe messaging access for one account and store the result.

    Reads one Instagram conversation through the linked Page — the same
    permission path inbound DMs and replies use. Returns the new status, or
    the previous one if Meta could not be reached (a network blip should not
    flip a healthy account to an error).
    """
    page = account.facebook_page
    token = page.page_access_token if page else ''
    if not page or not token:
        _save(account, STATUS_ERROR, 'Instagram account is not linked to a connected Facebook Page')
        return account.messaging_status

    api_version = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_API_VERSION', 'v23.0')
    try:
        response = requests.get(
            f"https://graph.facebook.com/{api_version}/{page.page_id}/conversations",
            params={'platform': 'instagram', 'limit': 1, 'fields': 'id', 'access_token': token},
            timeout=CHECK_TIMEOUT_SECONDS,
        )
        data = response.json() if response.content else {}
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Instagram messaging check for @%s failed to reach Meta: %s", account.username, exc)
        return account.messaging_status

    err = _graph_error(data)
    if response.status_code == 200 and not err:
        _save(account, STATUS_OK)
    elif is_messaging_disabled_error(data):
        record_messaging_error(account, data)
    else:
        _save(account, STATUS_ERROR, err.get('message') or f'HTTP {response.status_code}', _error_code(err))
    return account.messaging_status
