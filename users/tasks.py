import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    soft_time_limit=60,
    time_limit=90,
    autoretry_for=(Exception,),
    retry_kwargs={'max_retries': 3},
    default_retry_delay=60,
)
def send_user_invitation_email_task(self, user_email, user_name, tenant_name,
                                    temporary_password, frontend_url,
                                    invited_by, language='en'):
    """Send a user-invitation email off the request cycle.

    All content is passed in as arguments, so no tenant schema context is
    needed — the email service just formats and sends via SendGrid.
    """
    from tenants.email_service import email_service

    sent = email_service.send_user_invitation_email(
        user_email=user_email,
        user_name=user_name,
        tenant_name=tenant_name,
        temporary_password=temporary_password,
        frontend_url=frontend_url,
        invited_by=invited_by,
        language=language,
    )
    if not sent:
        # Raise so autoretry kicks in on a soft SendGrid failure.
        raise RuntimeError(f'send_user_invitation_email returned falsy for {user_email}')
    logger.info(f'Invitation email sent to {user_email}')
    return True


@shared_task(
    bind=True,
    soft_time_limit=60,
    time_limit=90,
    autoretry_for=(Exception,),
    retry_kwargs={'max_retries': 3},
    default_retry_delay=60,
)
def send_new_password_email_task(self, user_email, user_name, tenant_name,
                                 new_password, frontend_url, language='en'):
    """Send a new-password email off the request cycle."""
    from tenants.email_service import email_service

    sent = email_service.send_new_password_email(
        user_email=user_email,
        user_name=user_name,
        tenant_name=tenant_name,
        new_password=new_password,
        frontend_url=frontend_url,
        language=language,
    )
    if not sent:
        raise RuntimeError(f'send_new_password_email returned falsy for {user_email}')
    logger.info(f'New password email sent to {user_email}')
    return True
