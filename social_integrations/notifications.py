"""Social-side notification fan-out helpers (inbox messages, auto-posting,
WhatsApp template reviews). All helpers are best-effort: a notification
failure must never break the webhook or task that triggered it."""
import logging

logger = logging.getLogger(__name__)


def widget_assigned_user_id(session):
    """Active assignee of a widget chat (assignments are keyed by visitor id)."""
    from .views import get_assignment_for_conversation
    return get_assignment_for_conversation(
        'widget', session.session_id, str(session.connection_id),
    )


def notify_widget_visitor_message(session, message, assigned_user_id=None):
    """'message_received' for a website-chat visitor message.

    Links to ``/messages/widget_<connection>_<session>``; metadata
    conversation_id is the session id (what mark-read clears by).
    """
    try:
        from users.notification_utils import create_social_message_notification

        text = message.message_text or ('Sent an attachment' if message.attachments else '')
        create_social_message_notification(
            platform='Widget',
            sender_name=session.visitor_name or f"Website visitor {session.visitor_id[:6]}",
            message_text=text,
            conversation_id=session.session_id,
            sender_id=session.visitor_id,
            account_id=str(session.connection_id),
            assigned_user_id=assigned_user_id,
            chat_conversation_key=session.session_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception('widget message notification failed')


AUTO_POST_LINK = '/settings/social/auto-posting?post={post_id}'


def notify_auto_post(post, kind):
    """Tell social-settings admins about an auto-post.

    kind 'draft'  → notification_type 'auto_post_draft' (needs approval)
    kind 'failed' → notification_type 'auto_post_failed' (publish failed)
    Link: /settings/social/auto-posting?post=<id>; metadata.auto_post_id.
    """
    try:
        from users.notification_utils import create_notification, social_admin_recipients

        if kind == 'draft':
            notification_type = 'auto_post_draft'
            title = 'New AI post waiting for approval'
            preview = (post.facebook_text or post.instagram_text or '')[:120]
            message = preview or 'An auto-generated post needs your review.'
        else:
            notification_type = 'auto_post_failed'
            title = 'Auto-post failed to publish'
            message = (post.error_message or 'Publishing failed.')[:200]
        metadata = {
            'auto_post_id': post.id,
            'status': post.status,
            'scheduled_for': post.scheduled_for.isoformat() if post.scheduled_for else None,
        }
        for user in social_admin_recipients('manage_social_settings'):
            create_notification(
                user=user,
                notification_type=notification_type,
                title=title,
                message=message,
                metadata=dict(metadata),
                link_url=AUTO_POST_LINK.format(post_id=post.id),
            )
    except Exception:  # noqa: BLE001
        logger.exception('auto-post notification failed')


def notify_whatsapp_template_status(template, previous_status, reason=''):
    """'whatsapp_template_status' when Meta approves/rejects/pauses a template.

    Recipients: the template's creator plus social-connection admins.
    Link: /social/templates?id=<pk>; metadata {template_id, name, language,
    status, previous_status, reason, waba_id}.
    """
    try:
        from users.notification_utils import create_notification, social_admin_recipients

        recipients = {u.id: u for u in social_admin_recipients('manage_social_connections')}
        creator = template.created_by
        if creator is not None and creator.is_active:
            recipients.setdefault(creator.id, creator)
        status_label = (template.status or '').lower().replace('_', ' ')
        message = f'Template "{template.name}" ({template.language}) is now {status_label}.'
        if reason and reason.upper() != 'NONE':
            message += f' Reason: {reason}'
        metadata = {
            'template_id': template.id,
            'name': template.name,
            'language': template.language,
            'status': template.status,
            'previous_status': previous_status,
            'reason': reason or '',
            'waba_id': template.business_account.waba_id,
        }
        for user in recipients.values():
            create_notification(
                user=user,
                notification_type='whatsapp_template_status',
                title='WhatsApp template reviewed',
                message=message[:500],
                metadata=dict(metadata),
                link_url=f'/social/templates?id={template.id}',
            )
    except Exception:  # noqa: BLE001
        logger.exception('whatsapp template notification failed')
