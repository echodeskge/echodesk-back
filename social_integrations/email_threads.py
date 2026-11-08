"""Per-thread work state for the email inbox: assignee and linked ticket.

Assignments are ordinary ``ChatAssignment`` rows (platform ``email``,
``conversation_id`` = the thread id, ``account_id`` = the connection id as a
string), so the unified inbox and the email screen see the same state.
Tickets are ``EmailThreadTicket`` rows.
"""
from django.db.models import CharField, Exists, OuterRef
from django.db.models.functions import Cast

from .models import ChatAssignment, EmailThreadTicket

ACTIVE_STATUSES = ('active', 'in_session')
THREAD_VIEWS = ('assigned_to_me', 'unassigned', 'ticketed')


def user_brief(user):
    if user is None:
        return None
    name = (user.get_full_name() or '').strip() or user.email
    parts = [p for p in name.replace('@', ' ').split() if p]
    initials = ''.join(p[0] for p in parts[:2]).upper() or '?'
    return {'id': user.id, 'name': name, 'initials': initials, 'email': user.email}


def ticket_brief(ticket):
    if ticket is None:
        return None
    return {'id': ticket.id, 'title': ticket.title}


def thread_meta(pairs):
    """``{(connection_id, thread_id): {'assignee': …, 'ticket': …}}`` in two queries."""
    pairs = {(c, t) for c, t in pairs if c and t}
    if not pairs:
        return {}
    conn_ids = {c for c, _ in pairs}
    thread_ids = {t for _, t in pairs}
    out = {p: {'assignee': None, 'ticket': None} for p in pairs}

    assignments = ChatAssignment.objects.filter(
        platform='email',
        status__in=ACTIVE_STATUSES,
        assigned_user__isnull=False,
        account_id__in=[str(c) for c in conn_ids],
        conversation_id__in=thread_ids,
    ).select_related('assigned_user').order_by('updated_at')
    for a in assignments:  # newest last, so it wins
        key = (int(a.account_id), a.conversation_id)
        if key in out:
            out[key]['assignee'] = user_brief(a.assigned_user)

    links = EmailThreadTicket.objects.filter(
        connection_id__in=conn_ids, thread_id__in=thread_ids,
    ).select_related('ticket')
    for link in links:
        key = (link.connection_id, link.thread_id)
        if key in out:
            out[key]['ticket'] = ticket_brief(link.ticket)
    return out


def find_thread_assignment(connection_id, thread_id):
    return ChatAssignment.objects.filter(
        platform='email', account_id=str(connection_id), conversation_id=thread_id,
    ).select_related('assigned_user').order_by('-updated_at').first()


def _assignment_exists(**filters):
    return Exists(ChatAssignment.objects.filter(
        platform='email',
        account_id=OuterRef('_thread_acct'),
        conversation_id=OuterRef('thread_id'),
        status__in=ACTIVE_STATUSES,
        assigned_user__isnull=False,
        **filters,
    ))


def apply_thread_view(queryset, view, user):
    """Filter an EmailMessage queryset by the inbox "Views" (by thread)."""
    if view not in THREAD_VIEWS:
        return queryset
    if view == 'ticketed':
        return queryset.filter(Exists(EmailThreadTicket.objects.filter(
            connection_id=OuterRef('connection_id'), thread_id=OuterRef('thread_id'),
        )))
    queryset = queryset.annotate(_thread_acct=Cast('connection_id', output_field=CharField()))
    if view == 'assigned_to_me':
        return queryset.filter(_assignment_exists(assigned_user=user))
    return queryset.filter(~_assignment_exists())
