"""
Expo push delivery for the native mobile app.

Mirrors `notifications/utils.py` (Web Push) for Expo tokens: every active
`DevicePushToken` of the user gets a message via the Expo Push API, and a
`NotificationLog` row is written per token so delivery can be audited from
the admin.

Network failures *raise* (so the Celery task can retry); per-token errors
reported by Expo (`DeviceNotRegistered`, …) are logged and, where permanent,
deactivate the token so it stops being retried on every event.
"""
import logging
import re
from typing import Dict, List, Optional

import requests
from django.conf import settings

from .models import DevicePushToken, NotificationLog

logger = logging.getLogger(__name__)

# Expo tokens look like `ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]`
# (older SDKs emitted `ExpoPushToken[...]`).
EXPO_TOKEN_RE = re.compile(r'^Expo(nent)?PushToken\[[^\]]+\]$')

# Expo accepts at most 100 messages per request.
EXPO_BATCH_SIZE = 100

# Expo error codes that mean the token will never work again.
PERMANENT_ERRORS = {'DeviceNotRegistered'}


def is_valid_expo_token(token: str) -> bool:
    """True if `token` has the Expo push token shape."""
    return bool(token) and bool(EXPO_TOKEN_RE.match(token))


def _chunks(items: List, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _post_batch(messages: List[Dict]) -> List[Dict]:
    """POST one batch to Expo and return the per-message tickets.

    Raises `requests.RequestException` on connection / HTTP-level failure so
    the caller (Celery task) can retry. A malformed body is treated the same
    way as a transport failure.
    """
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    access_token = getattr(settings, 'EXPO_ACCESS_TOKEN', '')
    if access_token:
        headers['Authorization'] = f'Bearer {access_token}'

    response = requests.post(
        getattr(settings, 'EXPO_PUSH_URL', 'https://exp.host/--/api/v2/push/send'),
        json=messages,
        headers=headers,
        timeout=getattr(settings, 'EXPO_PUSH_TIMEOUT', 10),
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise requests.RequestException(f'Expo returned non-JSON body: {exc}') from exc

    tickets = payload.get('data')
    if not isinstance(tickets, list) or len(tickets) != len(messages):
        # Expo reports request-level problems under `errors`; surface them.
        raise requests.RequestException(
            f"Expo push response malformed: {payload.get('errors') or payload}"
        )
    return tickets


def send_expo_push(
    user,
    title: str,
    body: str,
    data: Optional[Dict] = None,
    badge: Optional[int] = None,
    sound: Optional[str] = 'default',
    channel_id: Optional[str] = None,
) -> int:
    """
    Send an Expo push notification to all active device tokens of `user`.

    Returns the number of tokens Expo accepted (`status == 'ok'`).
    Raises `requests.RequestException` when a batch could not be delivered
    to Expo at all (the pending logs for that batch are marked failed first).
    """
    tokens = list(DevicePushToken.objects.filter(user=user, is_active=True))
    if not tokens:
        return 0

    data = data or {}
    sent_count = 0

    for batch in _chunks(tokens, EXPO_BATCH_SIZE):
        messages = []
        logs = []
        for device in batch:
            message = {
                'to': device.token,
                'title': title,
                'body': body,
                'data': data,
                'sound': sound,
            }
            if badge is not None:
                message['badge'] = badge
            if channel_id:
                message['channelId'] = channel_id
            messages.append(message)
            logs.append(NotificationLog.objects.create(
                user=user,
                device_token=device,
                title=title,
                body=body,
                data=data,
            ))

        try:
            tickets = _post_batch(messages)
        except requests.RequestException as exc:
            logger.error(f"Expo push request failed for user {user.id}: {exc}")
            for log in logs:
                log.status = 'failed'
                log.error_message = str(exc)
                log.save(update_fields=['status', 'error_message'])
            raise

        for device, log, ticket in zip(batch, logs, tickets):
            if ticket.get('status') == 'ok':
                log.status = 'sent'
                log.save(update_fields=['status'])
                sent_count += 1
                continue

            details = ticket.get('details') or {}
            error_code = details.get('error', '')
            message = ticket.get('message', '') or error_code or 'unknown error'
            log.status = 'failed'
            log.error_message = f'{error_code}: {message}' if error_code else message
            log.save(update_fields=['status', 'error_message'])

            if error_code in PERMANENT_ERRORS:
                # Device uninstalled the app / token rotated — stop sending.
                device.is_active = False
                device.save(update_fields=['is_active'])
                logger.info(
                    f"Deactivated Expo token {device.id} for user {user.id} ({error_code})"
                )
            else:
                logger.warning(
                    f"Expo push rejected for token {device.id}: {log.error_message}"
                )

    logger.info(f"Expo push sent to {sent_count}/{len(tokens)} device(s) of user {user.id}: {title}")
    return sent_count
