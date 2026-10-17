"""Canonical conversation keys for the social inbox.

Most platforms identify a conversation by one customer-side id (FB/IG sender
id, WhatsApp number without '+', Telegram peer id). The website widget has two:

- ``visitor_id`` (the list row's ``sender_id``) — one per browser, stable
  across chats. ChatAssignment rows for widget chats are keyed by it, because
  that is what the dashboard sends to assign/unassign/transfer/start/end.
- ``session_id`` — one per chat. Messages, read state, ConversationArchive
  rows and AI companion state are keyed by it.

Clients may send either id (or the prefixed list id ``widget_<conn>_<session>``)
to any endpoint; the helpers below resolve it to the key the table expects so
that the same chat is found whatever form was used.
"""
import re

from django.db.models import Q

WIDGET_PREFIX_RE = re.compile(r'^widget_(\d+)_(.+)$')


def _strip_widget_prefix(raw):
    raw = str(raw or '').strip()
    match = WIDGET_PREFIX_RE.match(raw)
    if match:
        return match.group(2), match.group(1)
    return raw, None


def _widget_sessions(raw, account_id=None):
    from .models import WidgetSession

    key, prefixed_conn = _strip_widget_prefix(raw)
    if not key:
        return key, WidgetSession.objects.none()
    qs = WidgetSession.objects.filter(Q(session_id=key) | Q(visitor_id=key))
    conn = prefixed_conn or (str(account_id) if account_id not in (None, '') else None)
    if conn and str(conn).isdigit():
        qs = qs.filter(connection_id=int(conn))
    return key, qs


def widget_visitor_id(raw, account_id=None):
    """Visitor id for a widget chat given a visitor id, session id or list id."""
    key, qs = _widget_sessions(raw, account_id)
    if not key:
        return key
    session = qs.filter(session_id=key).values('visitor_id').first()
    if session:
        return session['visitor_id']
    return key


def widget_session_id(raw, account_id=None):
    """Current (newest) session id for a widget chat given any of its ids."""
    key, qs = _widget_sessions(raw, account_id)
    if not key:
        return key
    if qs.filter(session_id=key).exists():
        return key
    latest = qs.order_by('-last_seen_at', '-started_at').values('session_id').first()
    return latest['session_id'] if latest else key


def widget_all_keys(raw, account_id=None):
    """Every id the given widget chat may have been stored under.

    Used for lookups so legacy rows keyed by either form are still found.
    """
    key, qs = _widget_sessions(raw, account_id)
    if not key:
        return []
    keys = {key}
    for session_id, visitor_id in qs.values_list('session_id', 'visitor_id'):
        keys.add(session_id)
        keys.add(visitor_id)
    return sorted(keys)


def assignment_conversation_id(platform, conversation_id, account_id=None):
    """Key a ChatAssignment row is stored under for this chat."""
    if platform == 'widget':
        return widget_visitor_id(conversation_id, account_id)
    return str(conversation_id or '').strip()


def assignment_lookup_ids(platform, conversation_id, account_id=None):
    """All keys an existing ChatAssignment for this chat may use."""
    if platform == 'widget':
        return widget_all_keys(conversation_id, account_id) or [str(conversation_id or '')]
    raw = str(conversation_id or '').strip()
    keys = {raw}
    if platform == 'whatsapp' and raw:
        # The inbox list keys WhatsApp chats by the number without '+'.
        keys.add(raw.lstrip('+'))
    return sorted(k for k in keys if k)


def session_conversation_id(platform, conversation_id, account_id=None):
    """Key used for messages / archive / AI state rows of this chat."""
    if platform == 'widget':
        return widget_session_id(conversation_id, account_id)
    return str(conversation_id or '').strip()
