import logging

import requests
from celery import shared_task
from django.core.management import call_command
from io import StringIO

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(requests.RequestException,),
    retry_kwargs={"max_retries": 3},
    retry_backoff=True,
    soft_time_limit=60,
    time_limit=90,
)
def send_auto_reply_task(self, schema_name, platform, recipient_id, message_text, connection_id):
    """Send a platform auto-reply off the webhook request thread.

    Auto-replies used to be sent synchronously inside the inbound webhook,
    blocking the request on the outbound Graph/WhatsApp API call. This task
    moves that work to a worker and retries transient *connection* failures
    (``requests.RequestException``). The underlying senders return ``False`` on
    a non-200 response without raising, so a genuine API rejection is logged
    but not retried (avoids spamming a recipient).
    """
    from tenant_schemas.utils import schema_context

    with schema_context(schema_name):
        from social_integrations.models import (
            FacebookPageConnection,
            InstagramAccountConnection,
            TelegramAccount,
            WhatsAppBusinessAccount,
        )
        from social_integrations import views

        model_map = {
            "facebook": FacebookPageConnection,
            "instagram": InstagramAccountConnection,
            "whatsapp": WhatsAppBusinessAccount,
            "telegram": TelegramAccount,
        }
        sender_map = {
            "facebook": views.send_facebook_auto_reply,
            "instagram": views.send_instagram_auto_reply,
            "whatsapp": views.send_whatsapp_auto_reply,
            "telegram": views.send_telegram_auto_reply,
        }
        Model = model_map.get(platform)
        sender = sender_map.get(platform)
        if Model is None or sender is None:
            logger.warning("send_auto_reply_task: unsupported platform %s", platform)
            return False

        try:
            conn = Model.objects.get(pk=connection_id)
        except Model.DoesNotExist:
            logger.warning(
                "send_auto_reply_task: %s connection %s gone in %s",
                platform, connection_id, schema_name,
            )
            return False

        return sender(recipient_id, message_text, conn)


# One mailbox can take minutes (first sync of a big folder, slow attachment
# uploads). The beat fires every 5 minutes, so without a lock the next run
# synced the same mailbox in parallel and the hard limit killed the worker.
EMAIL_SYNC_LOCK_SECONDS = 15 * 60


def _sync_connection_locked(schema_name, connection):
    """Sync one mailbox unless another worker is already on it. Returns the
    number of new messages (0 when skipped)."""
    from django.core.cache import cache
    from social_integrations.email_utils import sync_imap_messages

    lock_key = f'email-sync-lock:{schema_name}:{connection.id}'
    try:
        acquired = cache.add(lock_key, '1', EMAIL_SYNC_LOCK_SECONDS)
    except Exception:  # cache down: sync anyway rather than never
        acquired = True
    if not acquired:
        logger.info(f"Email sync {schema_name}/{connection.email_address}: already running, skipped")
        return 0
    try:
        return sync_imap_messages(connection)
    finally:
        try:
            cache.delete(lock_key)
        except Exception:
            pass


@shared_task(soft_time_limit=540, time_limit=600)
def sync_all_tenant_emails():
    from celery.exceptions import SoftTimeLimitExceeded
    from tenant_schemas.utils import schema_context
    from tenants.models import Tenant
    from social_integrations.models import EmailConnection

    tenants = Tenant.objects.exclude(schema_name='public')
    total_synced = 0

    try:
        for tenant in tenants:
            try:
                with schema_context(tenant.schema_name):
                    connections = EmailConnection.objects.filter(is_active=True)
                    for connection in connections:
                        try:
                            count = _sync_connection_locked(tenant.schema_name, connection)
                            total_synced += count
                            logger.info(f"Email sync {tenant.schema_name}/{connection.email_address}: {count} new")
                        except SoftTimeLimitExceeded:
                            raise
                        except Exception as e:
                            logger.error(f"Email sync failed {tenant.schema_name}/{connection.email_address}: {e}")
            except SoftTimeLimitExceeded:
                raise
            except Exception as e:
                logger.error(f"Email sync failed for tenant {tenant.schema_name}: {e}")
    except SoftTimeLimitExceeded:
        # Stop cleanly; the next beat run (5 min) continues where IMAP
        # left off because already-stored messages are skipped.
        logger.warning(f'sync_all_tenant_emails hit its time limit after {total_synced} messages; continuing next run')

    logger.info(f'sync_all_tenant_emails completed: {total_synced} total messages')
    return total_synced


@shared_task
def generate_daily_posts():
    output = StringIO()
    call_command('generate_daily_posts', stdout=output)
    result = output.getvalue()
    logger.info(f'generate_daily_posts completed: {result}')
    return result


@shared_task
def publish_approved_posts():
    output = StringIO()
    call_command('publish_approved_posts', stdout=output)
    result = output.getvalue()
    logger.info(f'publish_approved_posts completed: {result}')
    return result


@shared_task(soft_time_limit=120, time_limit=180)
def generate_ai_post_for_tenant(schema_name):
    output = StringIO()
    call_command('generate_daily_posts', '--schema-name', schema_name, stdout=output)
    result = output.getvalue()
    logger.info(f'generate_ai_post_for_tenant({schema_name}) completed: {result}')
    return result


@shared_task
def sync_tenant_emails(schema_name):
    from tenant_schemas.utils import schema_context
    from social_integrations.models import EmailConnection

    total = 0
    with schema_context(schema_name):
        connections = EmailConnection.objects.filter(is_active=True)
        for connection in connections:
            try:
                count = _sync_connection_locked(schema_name, connection)
                total += count
                logger.info(f"Email sync {schema_name}/{connection.email_address}: {count} new")
            except Exception as e:
                logger.error(f"Email sync failed {schema_name}/{connection.email_address}: {e}")

    return total




@shared_task(bind=True, max_retries=3, soft_time_limit=120, time_limit=180)
def email_post_send_task(self, schema_name, connection_id, sent_message_id, eml_b64, flag_targets=None):
    """After an SMTP send: APPEND the sent MIME to the server's Sent folder
    and set \\Answered / $Forwarded on the originals.

    Best-effort: the mail is already delivered. An APPEND failure is retried
    with backoff, then logged and dropped.
    """
    import base64
    from tenant_schemas.utils import schema_context

    with schema_context(schema_name):
        from social_integrations.models import EmailConnection
        from social_integrations.email_utils import perform_post_send_imap

        connection = EmailConnection.objects.filter(pk=connection_id).first()
        if connection is None:
            return None
        message_bytes = base64.b64decode(eml_b64) if eml_b64 else None
        try:
            return perform_post_send_imap(connection, message_bytes, sent_message_id, flag_targets or [])
        except Exception as exc:
            logger.warning(
                "Post-send IMAP for %s/%s failed (attempt %s): %s",
                schema_name, connection.email_address, self.request.retries + 1, exc,
            )
            if self.request.retries >= self.max_retries:
                logger.error("Giving up on Sent APPEND for %s/%s: %s", schema_name, connection.email_address, exc)
                return None
            raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@shared_task(bind=True, soft_time_limit=90, time_limit=120)
def ai_companion_reply_task(self, schema_name, platform, account_id,
                            conversation_id, trigger_message_pk):
    """Debounced AI companion reply for one inbound message.

    Deliberately has NO autoretry: a duplicate AI reply is worse than a
    missed one (the customer's next message re-triggers the loop anyway).
    Every early exit returns a short reason string for logs/tests.
    """
    from django.conf import settings as django_settings
    from django.utils import timezone as dj_timezone
    from tenant_schemas.utils import schema_context

    with schema_context(schema_name):
        from social_integrations.services import ai_companion
        from social_integrations.services.ai_providers import AICompanionError
        from social_integrations.services.conversation_transcript import (
            get_last_inbound_at,
            get_latest_inbound_pk,
            get_platform_config,
        )

        # ── Safety rails (all re-checked here, not just at schedule time) ──
        if not getattr(django_settings, 'AI_COMPANION_ENABLED', True):
            return 'skip:kill_switch'

        companion_settings = ai_companion.get_settings()
        if not companion_settings or not companion_settings.is_enabled:
            return 'skip:disabled'

        channel = ai_companion.resolve_channel(platform, account_id)
        if not channel or not channel.enabled:
            return 'skip:channel_disabled'

        if not ai_companion.tenant_has_ai_companion():
            return 'skip:no_feature'

        state = ai_companion.get_conversation_state(
            platform, conversation_id, account_id
        )
        if state is not None and state.mode != 'ai':
            return f'skip:mode_{state.mode}'

        from social_integrations.views import get_assignment_for_conversation
        if get_assignment_for_conversation(platform, conversation_id, account_id):
            return 'skip:assigned'

        # Debounce: a newer inbound message supersedes this task.
        latest_pk = get_latest_inbound_pk(platform, account_id, conversation_id)
        if latest_pk is None:
            return 'skip:no_inbound'
        if latest_pk != trigger_message_pk:
            return 'skip:superseded'

        # Meta messaging window: never auto-reply past 24h (the HUMAN_AGENT
        # tag stays reserved for humans and allowlisted tenants).
        window_hours = get_platform_config(platform).messaging_window_hours
        if window_hours is not None:
            last_inbound_at = get_last_inbound_at(
                platform, account_id, conversation_id
            )
            if last_inbound_at is None:
                return 'skip:no_inbound'
            age = dj_timezone.now() - last_inbound_at
            if age.total_seconds() > window_hours * 3600:
                return 'skip:window_expired'

        # Daily caps.
        today = dj_timezone.now().date()
        if state is not None and state.daily_count_date == today and \
                state.daily_reply_count >= companion_settings.max_replies_per_conversation_per_day:
            return 'skip:conversation_cap'
        from social_integrations.models import AICompanionRun
        tenant_replies_today = AICompanionRun.objects.filter(
            kind='reply', success=True, started_at__date=today,
        ).count()
        if tenant_replies_today >= companion_settings.max_replies_per_day:
            return 'skip:tenant_cap'

        # ── Decision ──
        try:
            payload = ai_companion.run_decision(
                companion_settings, channel, platform, account_id, conversation_id
            )
        except (AICompanionError, ValueError) as exc:
            logger.warning(
                'ai_companion decision failed %s/%s/%s: %s',
                platform, account_id, conversation_id, exc,
            )
            return 'error:decision'

        action = payload.get('action')
        if action == 'ignore':
            return 'done:ignore'

        if action == 'handoff':
            ai_companion.apply_handoff(
                platform, account_id, conversation_id,
                reason=payload.get('reason', ''),
            )
            return 'done:handoff'

        if action == 'reply':
            reply_text = (payload.get('reply_text') or '').strip()
            if not reply_text:
                return 'error:empty_reply'
            # Re-check the debounce right before sending — a message may
            # have arrived while the LLM was thinking.
            if get_latest_inbound_pk(platform, account_id, conversation_id) != trigger_message_pk:
                return 'skip:superseded_late'
            sent = ai_companion.send_ai_reply(
                platform, account_id, conversation_id, reply_text
            )
            if not sent:
                return 'error:send_failed'

            from social_integrations.models import AIConversationState
            state, _ = AIConversationState.objects.get_or_create(
                platform=platform,
                conversation_id=str(conversation_id),
                account_id=str(account_id),
            )
            if state.daily_count_date != today:
                state.daily_count_date = today
                state.daily_reply_count = 0
            state.daily_reply_count += 1
            state.total_ai_replies += 1
            state.last_ai_reply_at = dj_timezone.now()
            state.save()
            return 'done:reply'

        return f'error:unknown_action_{action}'


@shared_task(bind=True, max_retries=2, soft_time_limit=120, time_limit=180)
def email_mailbox_operation_task(self, schema_name, connection_id, operation, items, options=None):
    """Server half of an email action (move to Trash / restore / move /
    permanent delete / \\Seen / \\Flagged). The DB was already updated by the
    view; this is best-effort: connection errors are retried a couple of
    times, everything else is logged and dropped.
    """
    from tenant_schemas.utils import schema_context

    with schema_context(schema_name):
        from social_integrations.models import EmailConnection
        from social_integrations.email_mailbox import perform_mailbox_operation

        connection = EmailConnection.objects.filter(pk=connection_id, is_active=True).first()
        if connection is None:
            return None
        try:
            return perform_mailbox_operation(connection, operation, items, **(options or {}))
        except (OSError, ConnectionError) as exc:
            if self.request.retries < self.max_retries:
                raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))
            logger.error("Email %s for %s/%s gave up: %s", operation, schema_name, connection.email_address, exc)
        except Exception as exc:
            logger.error("Email %s for %s/%s failed: %s", operation, schema_name, connection.email_address, exc)
        return None
