import json
import logging
from typing import Dict, List, Optional
from pywebpush import webpush, WebPushException
from django.conf import settings
from .models import PushSubscription, NotificationLog

logger = logging.getLogger(__name__)


def get_vapid_keys():
    """Get VAPID keys from settings.

    Returns None if VAPID keys are not configured, so callers can skip
    push notifications gracefully rather than generating ephemeral keys
    that won't match existing browser subscriptions.
    """
    private_key = getattr(settings, 'VAPID_PRIVATE_KEY', None)
    public_key = getattr(settings, 'VAPID_PUBLIC_KEY', None)

    if not private_key or not public_key:
        logger.warning(
            "VAPID_PRIVATE_KEY and/or VAPID_PUBLIC_KEY are not set in settings. "
            "Push notifications will be skipped until these are configured. "
            "Generate keys with: python -c \"from py_vapid import Vapid; v = Vapid(); v.generate_keys(); "
            "print('VAPID_PRIVATE_KEY =', repr(v.private_pem().decode())); "
            "print('VAPID_PUBLIC_KEY =', repr(v.public_key))\""
        )
        return None

    return {
        'private_key': private_key,
        'public_key': public_key,
        'admin_email': getattr(settings, 'VAPID_ADMIN_EMAIL', 'mailto:admin@echodesk.ge')
    }


def send_push_notification(
    subscription: PushSubscription,
    title: str,
    body: str,
    data: Optional[Dict] = None,
    icon: Optional[str] = None,
    url: Optional[str] = None,
    tag: Optional[str] = None
) -> bool:
    """
    Send a push notification to a single subscription.

    Args:
        subscription: PushSubscription model instance
        title: Notification title
        body: Notification body text
        data: Additional data to send
        icon: URL to notification icon
        url: URL to open when notification is clicked
        tag: Notification tag for grouping

    Returns:
        bool: True if sent successfully, False otherwise
    """
    if not subscription.is_active:
        logger.warning(f"Subscription {subscription.id} is inactive")
        return False

    # Get VAPID keys
    vapid_keys = get_vapid_keys()
    if vapid_keys is None:
        logger.warning("VAPID keys not configured — skipping push notification")
        return False

    # Prepare subscription info
    subscription_info = {
        "endpoint": subscription.endpoint,
        "keys": {
            "p256dh": subscription.p256dh,
            "auth": subscription.auth
        }
    }

    # Prepare notification data
    notification_data = {
        "title": title,
        "body": body,
        "icon": icon or "/logo-svg.svg",
        "data": data or {},
    }

    if url:
        notification_data["data"]["url"] = url
    if tag:
        notification_data["tag"] = tag

    # Create log entry
    log = NotificationLog.objects.create(
        user=subscription.user,
        subscription=subscription,
        title=title,
        body=body,
        data=notification_data.get("data", {})
    )

    try:
        # Send push notification
        response = webpush(
            subscription_info=subscription_info,
            data=json.dumps(notification_data),
            vapid_private_key=vapid_keys['private_key'],
            vapid_claims={
                "sub": vapid_keys['admin_email']
            }
        )

        # Update log
        log.status = 'sent'
        log.save()

        logger.info(f"Push notification sent to user {subscription.user.email}: {title}")
        return True

    except WebPushException as e:
        # 4xx from the push service is per-subscription (expired / revoked /
        # VAPID-key mismatch), NOT a code bug — log it at warning level so
        # Sentry doesn't get flooded by every dead subscription. 5xx is the
        # push service misbehaving, so keep that at error.
        status_code = getattr(e.response, 'status_code', None) if e.response else None
        if status_code and 400 <= status_code < 500:
            logger.warning(
                f"WebPush {status_code} for subscription {subscription.id}: {str(e)}"
            )
        else:
            logger.error(
                f"WebPush error for subscription {subscription.id}: {str(e)}"
            )

        # Update log
        log.status = 'failed'
        log.error_message = str(e)
        log.save()

        # 404 = subscription not found, 410 = unsubscribed/expired, 403 =
        # endpoint refuses our VAPID identity (key rotated or wrong project).
        # All three are permanent for THIS subscription — deactivate so it
        # stops being retried on every event (was generating 9k+ Sentry
        # events/14d before this guard).
        if status_code in (403, 404, 410):
            subscription.is_active = False
            subscription.save()
            logger.info(f"Marked subscription {subscription.id} as inactive ({status_code})")

        return False

    except Exception as e:
        logger.error(f"Error sending push notification: {str(e)}")

        # Update log
        log.status = 'failed'
        log.error_message = str(e)
        log.save()

        return False


def send_notification_to_user(
    user,
    title: str,
    body: str,
    data: Optional[Dict] = None,
    icon: Optional[str] = None,
    url: Optional[str] = None,
    tag: Optional[str] = None
) -> int:
    """
    Send push notification to all active subscriptions of a user.

    Returns:
        int: Number of notifications sent successfully
    """
    subscriptions = PushSubscription.objects.filter(
        user=user,
        is_active=True
    )

    sent_count = 0
    for subscription in subscriptions:
        if send_push_notification(subscription, title, body, data, icon, url, tag):
            sent_count += 1

    return sent_count


def send_notification_to_users(
    users: List,
    title: str,
    body: str,
    data: Optional[Dict] = None,
    icon: Optional[str] = None,
    url: Optional[str] = None,
    tag: Optional[str] = None
) -> int:
    """
    Send push notification to multiple users.

    Returns:
        int: Total number of notifications sent successfully
    """
    sent_count = 0
    for user in users:
        sent_count += send_notification_to_user(user, title, body, data, icon, url, tag)

    return sent_count
