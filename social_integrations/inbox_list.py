"""Unified social inbox list (``GET /api/social/conversations/``).

Each platform contributes "conversation heads" (the newest message of every
conversation). The heads are computed and filtered (archive, assignment,
hide-assigned, search) in SQL, ordered newest first and cut to
``page * page_size`` rows per platform, then merged. Only the rows on the
requested page are enriched (names, avatars, unread counts, assignment,
archive and AI state), so the number of queries does not depend on how many
conversations the tenant has.
"""
from dataclasses import dataclass

from django.db.models import (
    Case, CharField, Count, Exists, F, Func, OuterRef, Q, Subquery, Value, When,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Cast
from tenant_schemas.utils import schema_context

from .models import (
    ChatAssignment, ConversationArchive, EmailConnection, EmailMessage,
    FacebookMessage, InstagramMessage, SocialAccount, TelegramMessage,
    WhatsAppMessage, WidgetMessage, WidgetSession,
)

ACTIVE_ASSIGNMENT_STATUSES = ('active', 'in_session')
ALL_PLATFORMS = ('facebook', 'instagram', 'whatsapp', 'email', 'widget', 'telegram')


@dataclass
class ListContext:
    user: object
    is_admin: bool
    hide_assigned: bool
    assigned_only: bool
    archived_only: bool
    search: str
    need: int


class LTrim(Func):
    function = 'LTRIM'
    output_field = CharField()


def _apply_visibility(qs, platform, ctx, assignment_keys=('_conv',)):
    """Archive / assigned-tab / hide-assigned filters, expressed in SQL.

    ``qs`` must annotate ``_acct`` and ``_conv`` (and every name in
    ``assignment_keys``) as strings matching the ConversationArchive /
    ChatAssignment key columns.
    """
    archived = Exists(ConversationArchive.objects.filter(
        platform=platform,
        account_id=OuterRef('_acct'),
        conversation_id=OuterRef('_conv'),
    ))
    qs = qs.filter(archived) if ctx.archived_only else qs.filter(~archived)

    key_q = Q()
    for key in assignment_keys:
        key_q |= Q(conversation_id=OuterRef(key))

    if ctx.assigned_only:
        return qs.filter(Exists(ChatAssignment.objects.filter(
            key_q,
            platform=platform,
            account_id=OuterRef('_acct'),
            assigned_user=ctx.user,
            status__in=ACTIVE_ASSIGNMENT_STATUSES,
        )))
    if ctx.hide_assigned and not ctx.is_admin:
        return qs.filter(~Exists(ChatAssignment.objects.filter(
            key_q,
            platform=platform,
            account_id=OuterRef('_acct'),
            status__in=ACTIVE_ASSIGNMENT_STATUSES,
            assigned_user__isnull=False,
        ).exclude(assigned_user=ctx.user)))
    return qs


def _take(qs, ctx, order=('-timestamp', '-id')):
    return qs.count(), list(qs.order_by(*order)[:ctx.need])


# ── Per-platform head queries ───────────────────────────────────────────────

def _facebook_heads(ctx):
    heads = (
        FacebookMessage.objects.filter(is_deleted=False)
        .order_by('page_connection_id', 'sender_id', '-timestamp', '-id')
        .distinct('page_connection_id', 'sender_id')
        .values('id')
    )
    qs = FacebookMessage.objects.filter(id__in=Subquery(heads)).annotate(
        _acct=F('page_connection__page_id'), _conv=F('sender_id'),
    ).select_related('page_connection')
    if ctx.search:
        qs = qs.filter(Exists(FacebookMessage.objects.filter(
            page_connection_id=OuterRef('page_connection_id'),
            sender_id=OuterRef('sender_id'),
            is_deleted=False,
        ).filter(Q(sender_name__icontains=ctx.search) | Q(message_text__icontains=ctx.search))))
    return _take(_apply_visibility(qs, 'facebook', ctx), ctx)


def _instagram_heads(ctx):
    heads = (
        InstagramMessage.objects.filter(is_deleted=False)
        .order_by('account_connection_id', 'sender_id', '-timestamp', '-id')
        .distinct('account_connection_id', 'sender_id')
        .values('id')
    )
    qs = InstagramMessage.objects.filter(id__in=Subquery(heads)).annotate(
        _acct=F('account_connection__instagram_account_id'), _conv=F('sender_id'),
    ).select_related('account_connection')
    if ctx.search:
        qs = qs.filter(Exists(InstagramMessage.objects.filter(
            account_connection_id=OuterRef('account_connection_id'),
            sender_id=OuterRef('sender_id'),
            is_deleted=False,
        ).filter(
            Q(sender_name__icontains=ctx.search)
            | Q(sender_username__icontains=ctx.search)
            | Q(message_text__icontains=ctx.search)
        )))
    return _take(_apply_visibility(qs, 'instagram', ctx), ctx)


def _whatsapp_heads(ctx):
    # Customer-sent messages group by from_number, business-sent by
    # to_number; both inner DISTINCT ONs are index-backed. The outer
    # DISTINCT ON keeps the newest per (account, customer number).
    table = WhatsAppMessage._meta.db_table
    heads_sql = (
        'SELECT h.id FROM ('
        ' SELECT DISTINCT ON (u.business_account_id, u.cnum) u.id'
        ' FROM ('
        '  (SELECT DISTINCT ON (business_account_id, from_number)'
        '     id, business_account_id, from_number AS cnum, "timestamp"'
        f'   FROM "{table}" WHERE is_deleted = false AND is_from_business = false'
        '   ORDER BY business_account_id, from_number, "timestamp" DESC, id DESC)'
        '  UNION ALL'
        '  (SELECT DISTINCT ON (business_account_id, to_number)'
        '     id, business_account_id, to_number AS cnum, "timestamp"'
        f'   FROM "{table}" WHERE is_deleted = false AND is_from_business = true'
        '   ORDER BY business_account_id, to_number, "timestamp" DESC, id DESC)'
        ' ) u'
        ' ORDER BY u.business_account_id, u.cnum, u."timestamp" DESC, u.id DESC'
        ') h'
    )
    qs = WhatsAppMessage.objects.filter(id__in=RawSQL(heads_sql, [])).annotate(
        cnum=Case(
            When(is_from_business=True, then=F('to_number')),
            default=F('from_number'),
            output_field=CharField(),
        ),
    ).annotate(
        _acct=F('business_account__waba_id'),
        _conv=LTrim(F('cnum'), Value('+')),
    ).select_related('business_account')
    if ctx.search:
        qs = qs.filter(
            Q(cnum__icontains=ctx.search)
            | Exists(WhatsAppMessage.objects.filter(
                business_account_id=OuterRef('business_account_id'),
                is_deleted=False,
            ).filter(
                Q(from_number=OuterRef('cnum')) | Q(to_number=OuterRef('cnum'))
            ).filter(
                Q(contact_name__icontains=ctx.search) | Q(message_text__icontains=ctx.search)
            ))
        )
    return _take(_apply_visibility(qs, 'whatsapp', ctx), ctx)


def _email_heads(ctx, connection_ids, folder):
    base = EmailMessage.objects.filter(connection_id__in=connection_ids)
    all_folders = folder.lower() in ('all', 'all folders') if folder else True
    if folder and not all_folders:
        base = base.filter(folder=folder)
    heads = (
        base.order_by('connection_id', 'thread_id', '-timestamp', '-id')
        .distinct('connection_id', 'thread_id')
        .values('id')
    )
    qs = EmailMessage.objects.filter(id__in=Subquery(heads)).annotate(
        _acct=Cast('connection_id', output_field=CharField()), _conv=F('thread_id'),
    ).select_related('connection')
    if ctx.search:
        search_qs = base.filter(
            connection_id=OuterRef('connection_id'), thread_id=OuterRef('thread_id'),
        ).filter(
            Q(from_name__icontains=ctx.search)
            | Q(from_email__icontains=ctx.search)
            | Q(subject__icontains=ctx.search)
            | Q(body_text__icontains=ctx.search)
        )
        qs = qs.filter(Exists(search_qs))
    # Archive / assignment rules only apply to the inbox (and "all folders").
    if (folder and folder.upper() == 'INBOX') or all_folders:
        qs = _apply_visibility(qs, 'email', ctx)
    return _take(qs, ctx)


def _widget_heads(ctx, connection_ids):
    latest = WidgetMessage.objects.filter(
        session_id=OuterRef('pk'), is_deleted=False,
    ).order_by('-timestamp', '-id')
    qs = WidgetSession.objects.filter(connection_id__in=connection_ids).annotate(
        _last_id=Subquery(latest.values('id')[:1]),
        _last_ts=Subquery(latest.values('timestamp')[:1]),
    ).filter(_last_id__isnull=False).annotate(
        _acct=Cast('connection_id', output_field=CharField()),
        _conv=F('session_id'),
    )
    if ctx.search:
        qs = qs.filter(
            Q(visitor_name__icontains=ctx.search)
            | Exists(WidgetMessage.objects.filter(
                id=OuterRef('_last_id'), message_text__icontains=ctx.search,
            ))
        )
    # Assignments for widget chats are keyed by visitor id (what the
    # dashboard sends); legacy rows may use the session id.
    qs = _apply_visibility(qs, 'widget', ctx, assignment_keys=('visitor_id', 'session_id'))
    return _take(qs, ctx, order=('-_last_ts', '-pk'))


def _telegram_heads(ctx):
    heads = (
        TelegramMessage.objects.filter(is_deleted=False, account__is_active=True)
        .order_by('account_id', 'peer_id', '-timestamp', '-id')
        .distinct('account_id', 'peer_id')
        .values('id')
    )
    qs = TelegramMessage.objects.filter(id__in=Subquery(heads)).annotate(
        _acct=Cast('account__telegram_user_id', output_field=CharField()),
        _conv=Cast('peer_id', output_field=CharField()),
    ).select_related('account')
    if ctx.search:
        qs = qs.filter(Exists(TelegramMessage.objects.filter(
            account_id=OuterRef('account_id'), peer_id=OuterRef('peer_id'), is_deleted=False,
        ).filter(
            Q(peer_name__icontains=ctx.search)
            | Q(peer_username__icontains=ctx.search)
            | Q(message_text__icontains=ctx.search)
        )))
    return _take(_apply_visibility(qs, 'telegram', ctx), ctx)


# ── Page-row builders (only for rows on the requested page) ────────────────

def _last_message(obj, *, text, is_from_business, attachment_type):
    return {
        'id': str(obj.id),
        'text': text,
        'timestamp': obj.timestamp,
        'is_from_business': is_from_business,
        'attachment_type': attachment_type,
        'platform_message_id': obj.message_id,
    }


def _build_facebook(rows):
    pairs = {(m.page_connection_id, m.sender_id) for m in rows}
    conn_ids = {p[0] for p in pairs}
    sender_ids = {p[1] for p in pairs}
    unread = {
        (r['page_connection_id'], r['sender_id']): r['n']
        for r in FacebookMessage.objects.filter(
            page_connection_id__in=conn_ids, sender_id__in=sender_ids, is_deleted=False,
            is_from_page=False, is_read_by_staff=False,
        ).values('page_connection_id', 'sender_id').annotate(n=Count('id'))
    }
    customer = {
        (m.page_connection_id, m.sender_id): m
        for m in FacebookMessage.objects.filter(
            page_connection_id__in=conn_ids, sender_id__in=sender_ids, is_deleted=False,
            is_from_page=False,
        ).order_by('page_connection_id', 'sender_id', '-timestamp').distinct('page_connection_id', 'sender_id')
    }
    page_ids = {m.page_connection.page_id for m in rows}
    accounts = {
        (sa.account_connection_id, sa.platform_id): sa
        for sa in SocialAccount.objects.filter(
            platform='facebook', platform_id__in=sender_ids, account_connection_id__in=page_ids,
        )
    }
    out = {}
    for msg in rows:
        page = msg.page_connection
        sender_id = msg.sender_id
        cust = customer.get((msg.page_connection_id, sender_id))
        sa = accounts.get((page.page_id, sender_id))
        if cust and cust.sender_name:
            name, pic = cust.sender_name, cust.profile_pic_url
        elif sa and sa.display_name:
            name, pic = sa.display_name, sa.profile_pic_url
        else:
            name, pic = f"Facebook User {sender_id}", None
        out[('facebook', msg.pk)] = {
            'conversation_id': f"fb_{page.page_id}_{sender_id}",
            'platform': 'facebook',
            'sender_id': sender_id,
            'sender_name': name,
            'profile_pic_url': pic,
            'last_message': _last_message(
                msg, text=msg.message_text, is_from_business=msg.is_from_page,
                attachment_type=msg.attachment_type,
            ),
            'message_count': 0,
            'unread_count': unread.get((msg.page_connection_id, sender_id), 0),
            'account_name': page.page_name,
            'account_id': page.page_id,
        }
    return out


def _build_instagram(rows):
    pairs = {(m.account_connection_id, m.sender_id) for m in rows}
    conn_ids = {p[0] for p in pairs}
    sender_ids = {p[1] for p in pairs}
    unread = {
        (r['account_connection_id'], r['sender_id']): r['n']
        for r in InstagramMessage.objects.filter(
            account_connection_id__in=conn_ids, sender_id__in=sender_ids, is_deleted=False,
            is_from_business=False, is_read_by_staff=False,
        ).values('account_connection_id', 'sender_id').annotate(n=Count('id'))
    }
    customer = {
        (m.account_connection_id, m.sender_id): m
        for m in InstagramMessage.objects.filter(
            account_connection_id__in=conn_ids, sender_id__in=sender_ids, is_deleted=False,
            is_from_business=False,
        ).order_by('account_connection_id', 'sender_id', '-timestamp').distinct('account_connection_id', 'sender_id')
    }
    account_ids = {m.account_connection.instagram_account_id for m in rows}
    accounts = {
        (sa.account_connection_id, sa.platform_id): sa
        for sa in SocialAccount.objects.filter(
            platform='instagram', platform_id__in=sender_ids, account_connection_id__in=account_ids,
        )
    }
    out = {}
    for msg in rows:
        account = msg.account_connection
        sender_id = msg.sender_id
        cust = customer.get((msg.account_connection_id, sender_id))
        sa = accounts.get((account.instagram_account_id, sender_id))
        if cust and (cust.sender_name or cust.sender_username):
            name, pic = cust.sender_name or cust.sender_username, cust.sender_profile_pic
        elif sa and sa.display_name:
            name, pic = sa.display_name, sa.profile_pic_url
        else:
            name, pic = f"Instagram User {sender_id}", None
        out[('instagram', msg.pk)] = {
            'conversation_id': f"ig_{account.instagram_account_id}_{sender_id}",
            'platform': 'instagram',
            'sender_id': sender_id,
            'sender_name': name,
            'profile_pic_url': pic,
            'last_message': _last_message(
                msg, text=msg.message_text, is_from_business=msg.is_from_business,
                attachment_type=msg.attachment_type,
            ),
            'message_count': 0,
            'unread_count': unread.get((msg.account_connection_id, sender_id), 0),
            'account_name': f"@{account.username}",
            'account_id': account.instagram_account_id,
        }
    return out


def _build_whatsapp(rows):
    account_ids = {m.business_account_id for m in rows}
    numbers = {m.cnum for m in rows}
    unread = {
        (r['business_account_id'], r['from_number']): r['n']
        for r in WhatsAppMessage.objects.filter(
            business_account_id__in=account_ids, from_number__in=numbers, is_deleted=False,
            is_from_business=False, is_read_by_staff=False,
        ).values('business_account_id', 'from_number').annotate(n=Count('id'))
    }
    customer = {
        (m.business_account_id, m.from_number): m
        for m in WhatsAppMessage.objects.filter(
            business_account_id__in=account_ids, from_number__in=numbers, is_deleted=False,
            is_from_business=False,
        ).order_by('business_account_id', 'from_number', '-timestamp').distinct('business_account_id', 'from_number')
    }
    out = {}
    for msg in rows:
        account = msg.business_account
        number = msg.cnum
        normalized = number.lstrip('+')
        cust = customer.get((msg.business_account_id, number))
        msg_type = msg.message_type
        out[('whatsapp', msg.pk)] = {
            'conversation_id': f"wa_{account.waba_id}_{normalized}",
            'platform': 'whatsapp',
            'sender_id': number,
            'sender_name': cust.contact_name if cust and cust.contact_name else number,
            'profile_pic_url': None,
            'last_message': _last_message(
                msg, text=msg.message_text, is_from_business=msg.is_from_business,
                attachment_type=msg_type if msg_type and msg_type != 'text' else None,
            ),
            'message_count': 0,
            'unread_count': unread.get((msg.business_account_id, number), 0),
            'account_name': account.business_name or account.display_phone_number,
            'account_id': account.waba_id,
        }
    return out


def _build_email(rows, folder):
    conn_ids = {m.connection_id for m in rows}
    thread_ids = {m.thread_id for m in rows}
    base = EmailMessage.objects.filter(connection_id__in=conn_ids, thread_id__in=thread_ids)
    if folder and folder.lower() not in ('all', 'all folders'):
        base = base.filter(folder=folder)
    unread = {
        (r['connection_id'], r['thread_id']): r['n']
        for r in base.filter(is_from_business=False, is_read=False)
        .values('connection_id', 'thread_id').annotate(n=Count('id'))
    }
    customer = {
        (m.connection_id, m.thread_id): m
        for m in base.filter(is_from_business=False)
        .order_by('connection_id', 'thread_id', '-timestamp').distinct('connection_id', 'thread_id')
    }
    out = {}
    for msg in rows:
        conn = msg.connection
        cust = customer.get((msg.connection_id, msg.thread_id))
        if cust:
            email = cust.from_email
            name = cust.from_name or cust.from_email
        elif not msg.is_from_business:
            email = msg.from_email
            name = msg.from_name or msg.from_email
        else:
            email, name = '', ''
        body_text = msg.body_text or ''
        out[('email', msg.pk)] = {
            'conversation_id': f"email_{conn.id}_{msg.thread_id}",
            'platform': 'email',
            'sender_id': email,
            'sender_name': name or email,
            'profile_pic_url': None,
            'last_message': _last_message(
                msg, text=body_text[:200] if body_text else '', is_from_business=msg.is_from_business,
                attachment_type='attachment' if msg.attachments else None,
            ),
            'message_count': 0,
            'unread_count': unread.get((msg.connection_id, msg.thread_id), 0),
            'account_name': conn.email_address,
            'account_id': str(conn.id),
            'subject': msg.subject,
        }
    return out


def _build_widget(rows, connection_labels):
    last_messages = {m.id: m for m in WidgetMessage.objects.filter(id__in=[s._last_id for s in rows])}
    unread = {
        r['session_id']: r['n']
        for r in WidgetMessage.objects.filter(
            session_id__in=[s.pk for s in rows], is_from_visitor=True,
            is_read_by_staff=False, is_deleted=False,
        ).values('session_id').annotate(n=Count('id'))
    }
    out = {}
    for session in rows:
        last = last_messages.get(session._last_id)
        if last is None:
            continue
        out[('widget', session.pk)] = {
            'conversation_id': f"widget_{session.connection_id}_{session.session_id}",
            'platform': 'widget',
            'sender_id': session.visitor_id,
            'sender_name': session.visitor_name or f"Website visitor {session.visitor_id[:6]}",
            'profile_pic_url': None,
            'last_message': _last_message(
                last, text=last.message_text, is_from_business=not last.is_from_visitor,
                attachment_type='attachment' if last.attachments else None,
            ),
            'message_count': 0,
            'unread_count': unread.get(session.pk, 0),
            'account_name': connection_labels.get(session.connection_id, ''),
            'account_id': str(session.connection_id),
            # Widget-only: the chat's own close state (visitor or agent ended it).
            '_widget_session_id': session.session_id,
            '_widget_ended_at': session.ended_at,
            'session_ended_by': session.ended_by,
        }
    return out


def _build_telegram(rows):
    account_pks = {m.account_id for m in rows}
    peers = {m.peer_id for m in rows}
    unread = {
        (r['account_id'], r['peer_id']): r['n']
        for r in TelegramMessage.objects.filter(
            account_id__in=account_pks, peer_id__in=peers, is_deleted=False,
            is_from_business=False, is_read_by_staff=False,
        ).values('account_id', 'peer_id').annotate(n=Count('id'))
    }
    info = {
        (m.account_id, m.peer_id): m
        for m in TelegramMessage.objects.filter(
            account_id__in=account_pks, peer_id__in=peers, is_deleted=False, is_from_business=False,
        ).order_by('account_id', 'peer_id', '-timestamp').distinct('account_id', 'peer_id')
    }
    out = {}
    for last in rows:
        account = last.account
        account_id = str(account.telegram_user_id)
        cust = info.get((last.account_id, last.peer_id))
        name = (
            (cust.peer_name if cust else '')
            or (cust.peer_username if cust else '')
            or last.peer_name or last.peer_username
            or f"Telegram User {last.peer_id}"
        )
        out[('telegram', last.pk)] = {
            'conversation_id': f"tg_{account_id}_{last.peer_id}",
            'platform': 'telegram',
            'sender_id': str(last.peer_id),
            'sender_name': name,
            'profile_pic_url': (cust.profile_pic_url if cust else None) or last.profile_pic_url,
            'last_message': _last_message(
                last, text=last.message_text, is_from_business=last.is_from_business,
                attachment_type=last.message_type if last.message_type != 'text' else None,
            ),
            'message_count': 0,
            'unread_count': unread.get((last.account_id, last.peer_id), 0),
            'account_name': account.username or account.first_name or account.phone_number,
            'account_id': account_id,
        }
    return out


def _enrich(conversations):
    """Attach assignment, archive and AI state to the page's rows (3 queries)."""
    from .models import AIConversationState

    if not conversations:
        return

    def assignment_keys(c):
        keys = [c['sender_id']]
        if c['platform'] == 'widget':
            keys.append(c['_widget_session_id'])
        elif c['platform'] == 'whatsapp':
            keys.append(c['sender_id'].lstrip('+'))
        return keys

    def state_key(c):
        # Archive + AI state rows use the session id for widget chats.
        if c['platform'] == 'widget':
            return c['_widget_session_id']
        if c['platform'] == 'whatsapp':
            return c['sender_id'].lstrip('+')
        return c['sender_id']

    assign_q = Q()
    for c in conversations:
        assign_q |= Q(platform=c['platform'], account_id=c['account_id'],
                      conversation_id__in=assignment_keys(c))
    assignments = {}
    for a in ChatAssignment.objects.filter(assign_q).select_related('assigned_user'):
        assignments[(a.platform, a.account_id, a.conversation_id)] = a

    state_q = Q()
    for c in conversations:
        keys = {state_key(c), c['sender_id']} if c['platform'] != 'widget' else {state_key(c)}
        state_q |= Q(platform=c['platform'], account_id=c['account_id'], conversation_id__in=keys)
    archives = {
        (ar.platform, ar.account_id, ar.conversation_id): ar
        for ar in ConversationArchive.objects.filter(state_q)
    }
    ai_states = {
        (st.platform, st.account_id, st.conversation_id): st.mode
        for st in AIConversationState.objects.filter(state_q).exclude(mode='ai')
    }

    for c in conversations:
        plat, acct = c['platform'], c['account_id']
        assignment = None
        for key in assignment_keys(c):
            assignment = assignments.get((plat, acct, key))
            if assignment:
                break
        if assignment and assignment.assigned_user:
            c['assigned_user_id'] = assignment.assigned_user_id
            c['assigned_user_name'] = (
                assignment.assigned_user.get_full_name() or assignment.assigned_user.email
            )
            c['assignment_status'] = assignment.status
            c['session_started_at'] = assignment.session_started_at
            c['session_ended_at'] = assignment.session_ended_at
        else:
            c['assigned_user_id'] = None
            c['assigned_user_name'] = None
            c['assignment_status'] = None
            c['session_started_at'] = None
            c['session_ended_at'] = None

        if plat == 'widget':
            # The widget chat's own end (visitor/agent closed it) wins over
            # the assignment's session end, which may be missing.
            ended_at = c.pop('_widget_ended_at', None)
            if ended_at:
                c['session_ended_at'] = ended_at
            session_key = c.pop('_widget_session_id')
        else:
            session_key = None

        skey = session_key or state_key(c)
        archive = archives.get((plat, acct, skey)) or (
            archives.get((plat, acct, c['sender_id'])) if plat != 'widget' else None
        )
        c['is_archived'] = bool(archive)
        c['archived_at'] = archive.archived_at if archive else None
        c['ai_state'] = ai_states.get((plat, acct, skey)) or (
            ai_states.get((plat, acct, c['sender_id'])) if plat != 'widget' else None
        )


def build_unified_conversations(request, *, enabled_platforms, search, page, page_size,
                                email_folder, assigned_only, archived_only,
                                email_connection_id, settings_obj, is_admin,
                                email_access_check):
    """Return ``(total_count, page_rows)`` for the unified inbox list."""
    import logging
    logger = logging.getLogger(__name__)

    ctx = ListContext(
        user=request.user,
        is_admin=is_admin,
        hide_assigned=bool(settings_obj and settings_obj.hide_assigned_chats),
        assigned_only=assigned_only,
        archived_only=archived_only,
        search=search,
        need=page * page_size,
    )

    total = 0
    heads = []  # (timestamp, platform, obj)
    email_folder = email_folder or 'INBOX'
    widget_labels = {}

    def collect(platform, fn, *args):
        nonlocal total
        try:
            count, rows = fn(ctx, *args)
        except Exception as e:  # noqa: BLE001 — one broken platform must not blank the inbox
            logger.error(f"Error loading {platform} conversations: {e}")
            return
        total += count
        for row in rows:
            ts = row._last_ts if platform == 'widget' else row.timestamp
            heads.append((ts, platform, row))

    if 'facebook' in enabled_platforms:
        collect('facebook', _facebook_heads)
    if 'instagram' in enabled_platforms:
        collect('instagram', _instagram_heads)
    if 'whatsapp' in enabled_platforms:
        collect('whatsapp', _whatsapp_heads)
    if 'email' in enabled_platforms:
        try:
            connections = EmailConnection.objects.filter(is_active=True).prefetch_related('user_assignments')
            if email_connection_id:
                connections = connections.filter(id=email_connection_id)
            allowed = [conn.id for conn in connections if email_access_check(conn)]
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error loading Email connections: {e}")
            allowed = []
        if allowed:
            collect('email', _email_heads, allowed, email_folder)
    if 'widget' in enabled_platforms:
        try:
            from widget_registry.models import WidgetConnection
            with schema_context('public'):
                widget_labels = dict(
                    WidgetConnection.objects.filter(
                        tenant_schema=request.tenant.schema_name,
                    ).values_list('id', 'label')
                )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error loading Widget connections: {e}")
        if widget_labels:
            collect('widget', _widget_heads, list(widget_labels.keys()))
    if 'telegram' in enabled_platforms:
        collect('telegram', _telegram_heads)

    heads.sort(key=lambda h: h[0], reverse=True)
    start = (page - 1) * page_size
    page_heads = heads[start:start + page_size]

    by_platform = {}
    for _ts, platform, row in page_heads:
        by_platform.setdefault(platform, []).append(row)

    built = {}
    builders = {
        'facebook': _build_facebook,
        'instagram': _build_instagram,
        'whatsapp': _build_whatsapp,
        'telegram': _build_telegram,
    }
    for platform, rows in by_platform.items():
        try:
            if platform == 'email':
                built.update(_build_email(rows, email_folder))
            elif platform == 'widget':
                built.update(_build_widget(rows, widget_labels))
            else:
                built.update(builders[platform](rows))
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error building {platform} conversations: {e}")

    conversations = [
        built[(platform, row.pk)] for _ts, platform, row in page_heads
        if (platform, row.pk) in built
    ]
    _enrich(conversations)
    for c in conversations:
        c.pop('_widget_session_id', None)
        c.pop('_widget_ended_at', None)
    return total, conversations
