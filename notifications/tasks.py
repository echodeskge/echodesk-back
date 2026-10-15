import logging

import requests
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(requests.RequestException,),
    retry_kwargs={'max_retries': 3},
    retry_backoff=True,
    soft_time_limit=60,
    time_limit=90,
)
def deliver_push_notification_task(self, tenant_schema, user_id, title, body,
                                   data=None, url=None, tag=None, badge=None):
    """Deliver one in-app notification to every push channel of a user.

    Runs off the request thread so a slow push service never blocks the
    signal/view that created the `Notification` row. Two channels:

    - Web Push (`notifications.utils.send_notification_to_user`) — never
      raises; dead subscriptions are deactivated inside the helper.
    - Expo push (`notifications.expo.send_expo_push`) — raises on transport
      failure, which triggers the autoretry (3 attempts, exponential backoff).

    Web Push runs first so a flaky Expo request does not re-send browser
    notifications on retry: on a retry the web channel is skipped by
    checking `self.request.retries`.
    """
    from tenant_schemas.utils import schema_context

    with schema_context(tenant_schema):
        from django.contrib.auth import get_user_model
        from notifications.expo import send_expo_push
        from notifications.utils import send_notification_to_user as send_web_push

        User = get_user_model()
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            logger.warning(
                f"deliver_push_notification_task: user {user_id} gone in {tenant_schema}"
            )
            return {'web': 0, 'expo': 0}

        data = data or {}
        web_sent = 0
        if self.request.retries == 0:
            web_sent = send_web_push(
                user=user,
                title=title,
                body=body,
                data=dict(data),
                icon='/favicon.ico',
                url=url,
                tag=tag,
            )

        expo_sent = send_expo_push(
            user=user,
            title=title,
            body=body,
            data=data,
            badge=badge,
        )

        return {'web': web_sent, 'expo': expo_sent}
