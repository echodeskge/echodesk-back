"""
Inviting a user: the one code path behind ``POST /api/users/`` and the
onboarding workspace step.

``invite_user`` gives an already-created ``User`` a temporary password,
forces a password change on first login, records who invited them and
queues the invitation e-mail (Celery, with retries). Callers create the
user row first (serializer or ``User.objects.create``) and then call this.
"""
import logging
import secrets
import string

from django.conf import settings
from django.utils import timezone

from .tasks import send_user_invitation_email_task

logger = logging.getLogger(__name__)

TEMPORARY_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*"


def generate_temporary_password(length=12):
    """12 characters: letters, digits, special chars (same as the users API)."""
    return ''.join(secrets.choice(TEMPORARY_PASSWORD_ALPHABET) for _ in range(length))


def invite_user(user, *, invited_by, tenant=None, send_email=True):
    """Set a temporary password on ``user``, flag the forced change, record
    the inviter and queue the invitation e-mail. Returns the temporary password.

    ``tenant`` supplies the workspace name, frontend URL and language of the
    e-mail (``invited_by`` is the acting admin). Queuing failures are logged,
    never raised: the user row stays valid and an admin can resend.
    """
    temporary_password = generate_temporary_password()

    user.invited_by = invited_by
    user.invitation_sent_at = timezone.now()
    user.temporary_password = temporary_password
    user.password_change_required = True
    user.set_password(temporary_password)
    user.save()

    if not send_email:
        return temporary_password

    # Off the request cycle (SendGrid can take seconds / time out) — queued
    # to Celery with retries.
    try:
        tenant_name = tenant.name if tenant else 'EchoDesk'
        frontend_url = tenant.frontend_url if tenant else settings.MAIN_DOMAIN
        language = tenant.preferred_language if tenant else 'en'

        send_user_invitation_email_task.delay(
            user_email=user.email,
            user_name=user.get_full_name(),
            tenant_name=tenant_name,
            temporary_password=temporary_password,
            frontend_url=frontend_url,
            invited_by=invited_by.get_full_name() if invited_by else tenant_name,
            language=language,
        )
        logger.info(f'Queued invitation email to {user.email}')
    except Exception as e:  # noqa: BLE001
        logger.error(f'Error queuing invitation email to {user.email}: {str(e)}')

    return temporary_password
