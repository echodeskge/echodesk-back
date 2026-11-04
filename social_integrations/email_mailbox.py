"""
Server-side mailbox operations for IMAP email connections (Outlook semantics).

- Folder roles / canonical DB folder names shared by sync and the folder list.
- UID-based move, flag and permanent-delete helpers. They never run a bare
  EXPUNGE: permanent removal is ``UID EXPUNGE <uids>`` (UIDPLUS) of our own
  UIDs only.
- View-facing helpers (``trash_messages``, ``restore_messages``, ...) that
  update the DB immediately and queue the IMAP write as a Celery task
  (best-effort, logged, never fails the HTTP request).
"""
import email
import hashlib
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.db.models import F
from django.utils import timezone

from . import email_utils

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Folder roles
# --------------------------------------------------------------------------

FOLDER_ROLES = ('inbox', 'sent', 'drafts', 'trash', 'junk', 'archive', 'custom')

# Canonical DB folder name per role (C4). Drafts is listed for completeness;
# it is not synced.
CANONICAL_FOLDERS = {
    'inbox': 'INBOX',
    'sent': 'Sent',
    'drafts': 'Drafts',
    'trash': 'Trash',
    'junk': 'Junk',
    'archive': 'Archive',
}

_SPECIAL_USE_ROLES = {
    '\\sent': 'sent',
    '\\trash': 'trash',
    '\\junk': 'junk',
    '\\drafts': 'drafts',
    '\\archive': 'archive',
}

# Lower-cased decoded names recognised when a server has no special-use flags.
_ROLE_NAMES = {
    'sent': ['sent', 'sent items', 'sent messages', 'sent mail', '[gmail]/sent mail',
             'inbox.sent', 'inbox/sent', 'inbox.sent items', 'inbox.sent messages'],
    'trash': ['trash', 'deleted items', 'deleted messages', 'deleted', '[gmail]/trash',
              '[gmail]/bin', 'bin', 'inbox.trash', 'inbox/trash', 'inbox.deleted items',
              'inbox.deleted messages'],
    'junk': ['junk', 'junk e-mail', 'junk email', 'spam', '[gmail]/spam', 'bulk mail',
             'inbox.junk', 'inbox/junk', 'inbox.spam', 'inbox/spam'],
    'drafts': ['drafts', '[gmail]/drafts', 'inbox.drafts', 'inbox/drafts'],
    'archive': ['archive', 'archives', 'inbox.archive', 'inbox/archive',
                'inbox.archives', 'inbox/archives'],
}

# Folders sync never imports: drafts (EmailDraft owns those) and the Gmail
# "All Mail" view, which duplicates every other folder.
SYNC_SKIP_FLAGS = {'\\drafts', '\\all'}
SYNC_SKIP_NAMES = {'drafts', '[gmail]/drafts', '[gmail]/all mail', 'inbox.drafts', 'inbox/drafts'}


def folder_role(name: str, flags: Iterable[str] = ()) -> str:
    """Role of a folder from its LIST flags, then its (decoded) name."""
    flags = {f.lower() for f in flags or ()}
    if (name or '').upper() == 'INBOX':
        return 'inbox'
    for flag, role in _SPECIAL_USE_ROLES.items():
        if flag in flags:
            return role
    lowered = (name or '').strip().lower()
    for role, names in _ROLE_NAMES.items():
        if lowered in names:
            return role
    return 'custom'


def role_for_db_folder(db_folder: str) -> str:
    """Role of a folder name as stored on EmailMessage.folder."""
    for role, canonical in CANONICAL_FOLDERS.items():
        if db_folder == canonical:
            return role
    return folder_role(db_folder)


def canonical_db_folder(name: str) -> str:
    """DB folder name for a folder name given by a client ("Deleted Items" -> "Trash")."""
    role = folder_role(name)
    return CANONICAL_FOLDERS[role] if role != 'custom' else name


def folder_counts(connection_ids) -> Dict[Tuple[Any, str], Dict[str, int]]:
    """{(connection_id, folder): {total_count, unread_count}} over non-deleted rows."""
    from django.db.models import Count, Q
    from .models import EmailMessage
    rows = (
        EmailMessage.objects.filter(connection_id__in=list(connection_ids), is_deleted=False)
        .values('connection_id', 'folder')
        .annotate(total=Count('id'), unread=Count('id', filter=Q(is_read=False)))
    )
    return {
        (row['connection_id'], row['folder']): {'total_count': row['total'], 'unread_count': row['unread']}
        for row in rows
    }


def folder_entries_with_counts(connection_id, details, counts) -> List[Dict[str, Any]]:
    """Attach role + DB counts to folder detail dicts (keyed by DB name ``name``)."""
    out = []
    for detail in details or []:
        entry = dict(detail)
        entry.setdefault('role', role_for_db_folder(entry['name']))
        entry.update(counts.get((connection_id, entry['name']), {'total_count': 0, 'unread_count': 0}))
        out.append(entry)
    return out


def list_mailboxes(imap) -> Optional[List[Dict[str, Any]]]:
    """Selectable folders from a live IMAP LIST, with roles and DB names.

    Each entry: ``{raw_name, name (decoded), flags, role, db_name}``. Exactly
    one folder per special role gets the canonical ``db_name`` (the
    special-use flagged one first, then the first by name), so e.g. a server
    with both "Trash" and "Deleted Items" maps only one of them to "Trash";
    the other keeps its decoded name as a custom folder.
    Returns None when LIST fails.
    """
    result, data = imap.list()
    if result != 'OK':
        return None
    return mailboxes_from_list_data(data)


def mailboxes_from_list_data(data) -> List[Dict[str, Any]]:
    """``list_mailboxes`` for an already-fetched LIST response."""
    entries = []
    for folder_data in data or []:
        raw_name = email_utils.parse_imap_list_entry(folder_data)
        if not raw_name:
            continue
        flags = email_utils.imap_list_flags(folder_data)
        if '\\noselect' in flags or '\\nonexistent' in flags:
            continue
        decoded = email_utils.decode_imap_utf7(raw_name) or raw_name
        entries.append({
            'raw_name': raw_name,
            'name': decoded,
            'flags': flags,
            'role': folder_role(decoded, flags),
            'db_name': decoded,
            'special_use': any(f in flags for f in _SPECIAL_USE_ROLES),
        })

    owner = {}
    # Special-use flagged folders win over name matches.
    for entry in sorted(entries, key=lambda e: 0 if e['special_use'] or e['role'] == 'inbox' else 1):
        role = entry['role']
        if role == 'custom':
            continue
        if role in owner:
            # A second folder with the same role keeps its own name.
            entry['role'] = 'custom'
            continue
        owner[role] = entry
        entry['db_name'] = CANONICAL_FOLDERS[role]
    canonical_in_use = {e['db_name'] for e in owner.values()}
    result = []
    for entry in entries:
        entry.pop('special_use', None)
        if entry['role'] == 'custom' and entry['db_name'] in canonical_in_use:
            # e.g. a plain "Trash" next to the \Trash-flagged "Deleted Items":
            # its rows would mix with the real Trash, so it is left out.
            continue
        result.append(entry)
    return result


def quote_mailbox(name: str) -> str:
    """Quote a raw (modified UTF-7) mailbox name for an IMAP command."""
    return '"' + (name or '').replace('\\', '\\\\').replace('"', '\\"') + '"'


def resolve_raw_folder(imap, db_folder: str, mailboxes=None, create: bool = False) -> Optional[str]:
    """Raw IMAP name for a DB folder name (canonical or custom).

    Canonical names resolve through the folder role (special-use flag, then
    name); custom names through the decoded name. With ``create`` a missing
    folder is CREATEd under the DB name. Returns None when it can't be found.
    """
    if not db_folder or db_folder.upper() == 'INBOX':
        return 'INBOX'
    if mailboxes is None:
        try:
            mailboxes = list_mailboxes(imap) or []
        except Exception as e:
            logger.warning(f"[EMAIL_MAILBOX] LIST failed while resolving {db_folder!r}: {e}")
            mailboxes = []
    for entry in mailboxes:
        if entry['db_name'] == db_folder:
            return entry['raw_name']
    for entry in mailboxes:
        if entry['name'] == db_folder:
            return entry['raw_name']
    for entry in mailboxes:
        if entry['name'].lower() == db_folder.lower():
            return entry['raw_name']
    role = role_for_db_folder(db_folder)
    if role != 'custom':
        for entry in mailboxes:
            if folder_role(entry['name'], entry['flags']) == role:
                return entry['raw_name']
    if create:
        raw = email_utils.encode_imap_utf7(db_folder)
        try:
            typ, _ = imap.create(quote_mailbox(raw))
            if typ == 'OK':
                return raw
        except Exception as e:
            logger.warning(f"[EMAIL_MAILBOX] CREATE {db_folder!r} failed: {e}")
    return None


# --------------------------------------------------------------------------
# Low-level IMAP helpers
# --------------------------------------------------------------------------

def imap_capabilities(imap) -> set:
    """Upper-cased CAPABILITY atoms of an authenticated session."""
    cached = getattr(imap, '_echodesk_caps', None)
    if cached is not None:
        return cached
    caps = set()
    try:
        typ, data = imap.capability()
        if typ == 'OK' and data and data[0]:
            raw = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
            caps = {c.upper() for c in raw.split()}
    except Exception:
        pass
    if not caps:
        caps = {str(c).upper() for c in (getattr(imap, 'capabilities', None) or ())}
    try:
        imap._echodesk_caps = caps
    except Exception:
        pass
    return caps


def _response_value(imap, code: str) -> Optional[bytes]:
    """Pop the last untagged ``code`` response (e.g. UIDVALIDITY, COPYUID)."""
    try:
        typ, data = imap.response(code)
    except Exception:
        return None
    if not data:
        return None
    value = data[-1]
    if value is None:
        return None
    return value if isinstance(value, bytes) else str(value).encode()


def read_uidvalidity(imap) -> str:
    value = _response_value(imap, 'UIDVALIDITY')
    if not value:
        return ''
    match = re.search(rb'\d+', value)
    return match.group(0).decode() if match else ''


def select_folder(imap, raw_folder: str, readonly: bool = False) -> Tuple[bool, str, int]:
    """SELECT a folder; returns (ok, uidvalidity, exists)."""
    _response_value(imap, 'UIDVALIDITY')  # drop stale value
    typ, data = imap.select(quote_mailbox(raw_folder), readonly=readonly)
    if typ != 'OK':
        return False, '', 0
    try:
        exists = int(data[0]) if data and data[0] else 0
    except (TypeError, ValueError):
        exists = 0
    return True, read_uidvalidity(imap), exists


def expand_uid_set(value: str) -> List[str]:
    """'3,5:7' -> ['3', '5', '6', '7']."""
    out = []
    for part in (value or '').split(','):
        part = part.strip()
        if not part:
            continue
        if ':' in part:
            lo, hi = part.split(':', 1)
            try:
                lo_i, hi_i = int(lo), int(hi)
            except ValueError:
                continue
            step = 1 if hi_i >= lo_i else -1
            out.extend(str(i) for i in range(lo_i, hi_i + step, step))
        else:
            out.append(part)
    return out


def parse_copyuid(value: Optional[bytes]) -> Tuple[str, Dict[str, str]]:
    """``b'<uidvalidity> <src-set> <dst-set>'`` -> (uidvalidity, {src: dst})."""
    if not value:
        return '', {}
    text = value.decode(errors='replace')
    match = re.search(r'(\d+)\s+([\d:,]+)\s+([\d:,]+)', text)
    if not match:
        return '', {}
    src = expand_uid_set(match.group(2))
    dst = expand_uid_set(match.group(3))
    if len(src) != len(dst):
        return match.group(1), {}
    return match.group(1), dict(zip(src, dst))


_FETCH_START_RE = re.compile(rb'^\d+\s+\(')
_UID_RE = re.compile(rb'UID\s+(\d+)', re.IGNORECASE)
_FLAGS_RE = re.compile(rb'FLAGS\s+\(([^)]*)\)', re.IGNORECASE)


def parse_fetch_response(data) -> List[Dict[str, Any]]:
    """Parse imaplib FETCH data into ``[{uid, flags (lower-case str set), body}]``.

    Handles literals (tuples) and FLAGS/UID that some servers send after the
    literal (a trailing bytes item).
    """
    entries = []
    current = None
    for item in data or []:
        if isinstance(item, tuple):
            current = {'meta': item[0] or b'', 'body': item[1] if len(item) > 1 else None}
            entries.append(current)
        elif isinstance(item, bytes):
            if _FETCH_START_RE.match(item):
                current = {'meta': item, 'body': None}
                entries.append(current)
            elif current is not None:
                current['meta'] += b' ' + item
    parsed = []
    for entry in entries:
        meta = entry['meta'] if isinstance(entry['meta'], bytes) else str(entry['meta']).encode()
        uid_match = _UID_RE.search(meta)
        if not uid_match:
            continue
        flags_match = _FLAGS_RE.search(meta)
        flags = set()
        if flags_match:
            flags = {f.decode(errors='replace').lower() for f in flags_match.group(1).split()}
        parsed.append({
            'uid': uid_match.group(1).decode(),
            'flags': flags,
            'body': entry['body'],
        })
    return parsed


def normalize_message_id(value) -> str:
    """Strip whitespace / folding from a Message-ID / In-Reply-To value."""
    if not value:
        return ''
    return ' '.join(str(value).split())


# Header fields sync fetches per message (cheap, no body). DATE/FROM/TO/
# SUBJECT identify a message that has no Message-ID across folders.
SYNC_HEADER_FIELDS = 'MESSAGE-ID DATE FROM TO SUBJECT'


def _header_fingerprint(header_bytes: Optional[bytes]) -> str:
    """Date/From/To/Subject of a header block, whitespace-normalised ('' if none)."""
    if not header_bytes:
        return ''
    try:
        parsed = email.message_from_bytes(header_bytes)
    except Exception:
        return ''
    parts = [normalize_message_id(parsed.get(name, '')) for name in ('Date', 'From', 'To', 'Subject')]
    return '|'.join(parts) if any(parts) else ''


def synthetic_message_id(connection_id, raw_folder: str, uidvalidity: str, uid: str,
                         header_bytes: Optional[bytes] = None) -> str:
    """Stable Message-ID for a server message that has none.

    Built from the message's own Date/From/To/Subject when it has any, so
    the id survives a move to another folder and a UIDVALIDITY change (a
    folder/UID based id would re-key the message and duplicate the row).
    Falls back to connection/folder/UIDVALIDITY/UID for header-less mail.
    """
    fingerprint = _header_fingerprint(header_bytes)
    if fingerprint:
        key = f'{connection_id}|{fingerprint}'
    else:
        key = f'{connection_id}|{raw_folder}|{uidvalidity}|{uid}'
    digest = hashlib.sha1(key.encode('utf-8', errors='replace')).hexdigest()
    return f'<echodesk-{digest}@sync.local>'


def _raw_header_message_id(body: Optional[bytes]) -> str:
    """Message-ID exactly as ``email.message_from_bytes`` returns it.

    The pre-UID sync stored this un-normalised value (it may carry folding
    whitespace), so sync looks rows up by it as well.
    """
    if not body:
        return ''
    try:
        return str(email.message_from_bytes(body).get('Message-ID', '') or '')
    except Exception:
        return ''


def _header_message_id(body: Optional[bytes]) -> str:
    return normalize_message_id(_raw_header_message_id(body))


def _chunks(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _search_uid_by_message_id(imap, message_id: str) -> str:
    if not message_id:
        return ''
    try:
        typ, data = imap.uid('SEARCH', None, 'HEADER', 'Message-ID', quote_mailbox(message_id))
    except Exception as e:
        logger.warning(f"[EMAIL_MAILBOX] UID SEARCH by Message-ID failed: {e}")
        return ''
    uids = data[0].split() if typ == 'OK' and data and data[0] else []
    return uids[-1].decode() if uids else ''


def resolve_item_uids(imap, items: List[Dict[str, Any]], uidvalidity: str) -> List[Tuple[Dict[str, Any], str]]:
    """Map each item to its UID in the currently selected folder.

    A stored uid is trusted only after its Message-ID header is confirmed
    with one batched FETCH (synthetic sync ids are trusted when the stored
    UIDVALIDITY matches). Anything else falls back to UID SEARCH HEADER
    Message-ID. Items that cannot be found are dropped.
    """
    resolved = []
    to_verify = {}
    pending = []
    for item in items:
        uid = (item.get('uid') or '').strip()
        mid = normalize_message_id(item.get('message_id'))
        stale = bool(item.get('uid_validity')) and bool(uidvalidity) and item['uid_validity'] != uidvalidity
        if uid.isdigit() and not stale:
            if mid.startswith('<echodesk-') and mid.endswith('@sync.local>'):
                if item.get('uid_validity') and item['uid_validity'] == uidvalidity:
                    resolved.append((item, uid))
                continue  # can't verify a synthetic id any other way
            to_verify[uid] = item
        else:
            pending.append(item)

    if to_verify:
        server_ids = {}
        try:
            for chunk in _chunks(sorted(to_verify, key=int), 200):
                typ, data = imap.uid('FETCH', ','.join(chunk), '(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
                if typ == 'OK':
                    for entry in parse_fetch_response(data):
                        server_ids[entry['uid']] = _header_message_id(entry['body'])
        except Exception as e:
            logger.warning(f"[EMAIL_MAILBOX] Message-ID verification FETCH failed: {e}")
        for uid, item in to_verify.items():
            if server_ids.get(uid) and server_ids[uid] == normalize_message_id(item.get('message_id')):
                resolved.append((item, uid))
            else:
                pending.append(item)

    for item in pending:
        uid = _search_uid_by_message_id(imap, normalize_message_id(item.get('message_id')))
        if uid:
            resolved.append((item, uid))
    return resolved


def _uid_expunge(imap, uids: List[str]) -> bool:
    """UID EXPUNGE of exactly ``uids`` (never a bare EXPUNGE)."""
    if not uids:
        return True
    if 'UIDPLUS' not in imap_capabilities(imap):
        logger.warning(
            "[EMAIL_MAILBOX] Server lacks UIDPLUS; leaving %d message(s) flagged \\Deleted "
            "instead of running a folder-wide EXPUNGE", len(uids),
        )
        return False
    typ, _ = imap.uid('EXPUNGE', ','.join(uids))
    return typ == 'OK'


def uid_move(imap, uids: List[str], target_raw: str) -> Tuple[bool, str, Dict[str, str]]:
    """Move ``uids`` from the selected folder to ``target_raw``.

    UID MOVE when the server supports it, else UID COPY + UID STORE \\Deleted
    + UID EXPUNGE <uids>. Returns (ok, target uidvalidity, {old_uid: new_uid})
    using COPYUID when the server reports it.
    """
    if not uids:
        return True, '', {}
    uid_set = ','.join(uids)
    _response_value(imap, 'COPYUID')  # drop stale value
    if 'MOVE' in imap_capabilities(imap):
        typ, _ = imap.uid('MOVE', uid_set, quote_mailbox(target_raw))
        if typ != 'OK':
            return False, '', {}
        uidvalidity, mapping = parse_copyuid(_response_value(imap, 'COPYUID'))
        return True, uidvalidity, mapping

    typ, _ = imap.uid('COPY', uid_set, quote_mailbox(target_raw))
    if typ != 'OK':
        return False, '', {}
    uidvalidity, mapping = parse_copyuid(_response_value(imap, 'COPYUID'))
    typ, _ = imap.uid('STORE', uid_set, '+FLAGS.SILENT', '(\\Deleted)')
    if typ == 'OK':
        _uid_expunge(imap, uids)
    return True, uidvalidity, mapping


def uid_store_flag(imap, uids: List[str], flag: str, value: bool) -> bool:
    if not uids:
        return True
    typ, _ = imap.uid('STORE', ','.join(uids), '+FLAGS.SILENT' if value else '-FLAGS.SILENT', f'({flag})')
    return typ == 'OK'


def uid_delete_permanently(imap, uids: List[str]) -> bool:
    if not uids:
        return True
    typ, _ = imap.uid('STORE', ','.join(uids), '+FLAGS.SILENT', '(\\Deleted)')
    if typ != 'OK':
        return False
    return _uid_expunge(imap, uids)


# --------------------------------------------------------------------------
# Operations (run inside the Celery task)
# --------------------------------------------------------------------------

FLAG_ACTIONS = {
    'mark_read': ('\\Seen', True),
    'mark_unread': ('\\Seen', False),
    'star': ('\\Flagged', True),
    'unstar': ('\\Flagged', False),
}


# A flag change made in EchoDesk is "held" until its \\Seen / \\Flagged STORE
# reached the server, so a sync that runs first doesn't copy the old server
# flags back over it.
FLAG_HOLD_SECONDS = 15 * 60


def _flag_hold_key(message_pk) -> str:
    from django.db import connection as db_connection
    schema = getattr(db_connection, 'schema_name', '') or 'public'
    return f'email-flag-hold:{schema}:{message_pk}'


def hold_flags(message_pks) -> None:
    from django.core.cache import cache
    try:
        cache.set_many({_flag_hold_key(pk): 1 for pk in message_pks}, FLAG_HOLD_SECONDS)
    except Exception as e:
        logger.warning(f"[EMAIL_MAILBOX] Could not record flag holds: {e}")


def release_flag_holds(message_pks) -> None:
    from django.core.cache import cache
    try:
        cache.delete_many([_flag_hold_key(pk) for pk in message_pks])
    except Exception:
        pass


def held_flag_ids(message_pks) -> set:
    """Pks whose flags were changed in EchoDesk and not yet written to the server."""
    from django.core.cache import cache
    pks = list(message_pks)
    if not pks:
        return set()
    try:
        found = cache.get_many([_flag_hold_key(pk) for pk in pks]) or {}
    except Exception:
        return set()
    return {pk for pk in pks if _flag_hold_key(pk) in found}


def _still_wanted(connection, operation: str, items: List[Dict[str, Any]], target_folder=None,
                  flag=None, value=True) -> List[Dict[str, Any]]:
    """Drop items whose DB row no longer asks for this operation.

    Tasks for the same message are not ordered (a delete then a quick
    restore can run restore-first). The DB holds the latest intent, so a
    stale task skips the message instead of undoing the newer change.
    """
    from .models import EmailMessage
    ids = [item['id'] for item in items if item.get('id')]
    if not ids:
        return items
    rows = {
        row['id']: row for row in EmailMessage.objects.filter(pk__in=ids, connection=connection).values(
            'id', 'folder', 'is_deleted', 'is_read', 'is_starred',
        )
    }
    wanted = []
    for item in items:
        if not item.get('id'):
            wanted.append(item)
            continue
        row = rows.get(item['id'])
        if row is None:
            continue
        if operation == 'move' and row['folder'] != target_folder:
            continue
        if operation == 'delete' and not row['is_deleted']:
            continue
        if operation == 'flag':
            if flag == '\\Seen' and row['is_read'] != value:
                continue
            if flag == '\\Flagged' and row['is_starred'] != value:
                continue
        wanted.append(item)
    if len(wanted) != len(items):
        logger.info(
            "[EMAIL_MAILBOX] %s: skipped %d message(s) changed again since the task was queued",
            operation, len(items) - len(wanted),
        )
    return wanted


def message_item(message) -> Dict[str, Any]:
    """Snapshot of what the IMAP task needs, taken BEFORE the DB update."""
    return {
        'id': message.pk,
        'folder': message.folder,
        'uid': message.uid or '',
        'uid_validity': getattr(message, 'uid_validity', '') or '',
        'message_id': message.message_id or '',
    }


def perform_mailbox_operation(connection, operation: str, items: List[Dict[str, Any]],
                              target_folder: Optional[str] = None, flag: Optional[str] = None,
                              value: bool = True, imap=None) -> Dict[str, Any]:
    """Apply one operation to ``items`` (see ``message_item``) on the server.

    operation: ``move`` (to DB folder ``target_folder``; Trash is created when
    missing), ``delete`` (permanent: STORE \\Deleted + UID EXPUNGE of those
    uids) or ``flag`` (``flag`` set/cleared per ``value``).
    Rows moved are re-keyed (uid/uid_validity) from COPYUID when the DB row
    is still in the target folder.
    """
    from .models import EmailMessage

    summary = {'done': 0, 'missing': 0, 'failed_folders': []}
    items = _still_wanted(connection, operation, items or [], target_folder, flag, value)
    if not items:
        return summary
    own_imap = imap is None
    if own_imap:
        imap = email_utils._connect_imap(connection)
    try:
        mailboxes = list_mailboxes(imap) or []
        target_raw = None
        if operation == 'move':
            target_raw = resolve_raw_folder(
                imap, target_folder, mailboxes,
                create=role_for_db_folder(target_folder) in ('trash', 'archive', 'junk'),
            )
            if not target_raw:
                raise ValueError(f"Target folder {target_folder!r} not found on server")

        by_folder = {}
        for item in items:
            by_folder.setdefault(item.get('folder') or 'INBOX', []).append(item)

        for db_folder, folder_items in by_folder.items():
            raw = resolve_raw_folder(imap, db_folder, mailboxes)
            if not raw:
                summary['failed_folders'].append(db_folder)
                continue
            if operation == 'move' and raw == target_raw:
                continue
            ok, uidvalidity, _ = select_folder(imap, raw, readonly=False)
            if not ok:
                summary['failed_folders'].append(db_folder)
                continue
            resolved = resolve_item_uids(imap, folder_items, uidvalidity)
            summary['missing'] += len(folder_items) - len(resolved)
            uids = [uid for _, uid in resolved]
            if not uids:
                continue
            if operation == 'move':
                ok, dst_validity, mapping = uid_move(imap, uids, target_raw)
                if ok:
                    for item, old_uid in resolved:
                        EmailMessage.objects.filter(
                            pk=item['id'], connection=connection, folder=target_folder,
                        ).update(uid=mapping.get(old_uid, ''), uid_validity=dst_validity if mapping.get(old_uid) else '')
            elif operation == 'delete':
                ok = uid_delete_permanently(imap, uids)
            elif operation == 'flag':
                ok = uid_store_flag(imap, uids, flag, value)
                if ok:
                    release_flag_holds([item['id'] for item, _ in resolved if item.get('id')])
            else:
                raise ValueError(f"Unknown mailbox operation {operation!r}")
            if ok:
                summary['done'] += len(uids)
            else:
                summary['failed_folders'].append(db_folder)
        return summary
    finally:
        if own_imap:
            try:
                imap.logout()
            except Exception:
                pass


def schedule_mailbox_operation(connection, operation: str, items: List[Dict[str, Any]], **kwargs) -> None:
    """Queue ``perform_mailbox_operation`` once the DB transaction commits.

    Falls back to running inline when the broker is unavailable. Never raises.
    """
    if not items or connection is None or not getattr(connection, 'is_active', True):
        return
    from django.db import connection as db_connection, transaction

    schema_name = getattr(db_connection, 'schema_name', None)
    connection_id = connection.pk

    def _dispatch():
        try:
            from .tasks import email_mailbox_operation_task
            email_mailbox_operation_task.delay(schema_name, connection_id, operation, items, kwargs)
            return
        except Exception as e:
            logger.warning(f"[EMAIL_MAILBOX] Could not queue {operation} task, running inline: {e}")
        try:
            perform_mailbox_operation(connection, operation, items, **kwargs)
        except Exception as e:
            logger.error(f"[EMAIL_MAILBOX] {operation} failed for {connection.email_address}: {e}")

    try:
        transaction.on_commit(_dispatch)
    except Exception as e:
        logger.error(f"[EMAIL_MAILBOX] Failed to schedule {operation}: {e}")


def _group_by_connection(messages) -> Dict[Any, List[Any]]:
    grouped = {}
    for message in messages:
        grouped.setdefault(message.connection_id, []).append(message)
    return grouped


def _connections_for(messages) -> Dict[Any, Any]:
    return {m.connection_id: m.connection for m in messages}


# --------------------------------------------------------------------------
# View-facing helpers: immediate DB change + queued server write
# --------------------------------------------------------------------------

TRASH = CANONICAL_FOLDERS['trash']


def set_flag(queryset, action: str) -> int:
    """mark_read / mark_unread / star / unstar: DB now, \\Seen / \\Flagged later."""
    flag, value = FLAG_ACTIONS[action]
    messages = list(queryset.select_related('connection'))
    if not messages:
        return 0
    ids = [m.pk for m in messages]
    from .models import EmailMessage
    rows = EmailMessage.objects.filter(pk__in=ids)
    if action == 'mark_read':
        rows.update(is_read=True, is_read_by_staff=True, read_by_staff_at=timezone.now())
    elif action == 'mark_unread':
        rows.update(is_read=False, is_read_by_staff=False, read_by_staff_at=None)
    elif action == 'star':
        rows.update(is_starred=True)
    else:
        rows.update(is_starred=False)
    hold_flags(ids)
    connections = _connections_for(messages)
    for connection_id, group in _group_by_connection(messages).items():
        schedule_mailbox_operation(
            connections[connection_id], 'flag', [message_item(m) for m in group], flag=flag, value=value,
        )
    return len(messages)


def move_messages(queryset, target_folder: str) -> int:
    """Move to ``target_folder`` (DB folder name) in the DB and on the server.

    Moving into Trash records ``previous_folder``; moving out of Trash clears it.
    """
    from .models import EmailMessage
    messages = [m for m in queryset.select_related('connection') if m.folder != target_folder]
    if not messages:
        return 0
    connections = _connections_for(messages)
    for connection_id, group in _group_by_connection(messages).items():
        items = [message_item(m) for m in group]
        ids = [m.pk for m in group]
        rows = EmailMessage.objects.filter(pk__in=ids)
        if target_folder == TRASH:
            rows.update(previous_folder=F('folder'), folder=TRASH, uid='', uid_validity='')
        else:
            rows.update(folder=target_folder, previous_folder='', uid='', uid_validity='')
        schedule_mailbox_operation(connections[connection_id], 'move', items, target_folder=target_folder)
    return len(messages)


def trash_messages(queryset) -> int:
    """Delete outside Trash: move to the server's Trash folder (C3)."""
    return move_messages(queryset.exclude(folder=TRASH), TRASH)


def delete_permanently(queryset, user=None) -> int:
    """Delete inside Trash: \\Deleted + UID EXPUNGE of those uids; DB is_deleted."""
    from .models import EmailMessage
    messages = list(queryset.filter(folder=TRASH, is_deleted=False).select_related('connection'))
    if not messages:
        return 0
    connections = _connections_for(messages)
    now = timezone.now()
    for connection_id, group in _group_by_connection(messages).items():
        items = [message_item(m) for m in group]
        EmailMessage.objects.filter(pk__in=[m.pk for m in group]).update(
            is_deleted=True, deleted_at=now, deleted_by=user,
        )
        schedule_mailbox_operation(connections[connection_id], 'delete', items)
    return len(messages)


def delete_messages(queryset, user=None) -> int:
    """Outlook delete: rows outside Trash go to Trash, rows in Trash are purged."""
    in_trash = list(queryset.filter(folder=TRASH).values_list('pk', flat=True))
    others = list(queryset.exclude(folder=TRASH).values_list('pk', flat=True))
    model = queryset.model
    count = trash_messages(model.objects.filter(pk__in=others)) if others else 0
    if in_trash:
        count += delete_permanently(model.objects.filter(pk__in=in_trash), user=user)
    return count


def restore_messages(queryset) -> int:
    """Restore: Trash -> previous_folder (default INBOX) on server and DB.

    Rows soft-deleted outside Trash (the old DB-only delete) are simply
    un-deleted, as before.
    """
    from .models import EmailMessage
    legacy = queryset.filter(is_deleted=True).exclude(folder=TRASH)
    count = legacy.update(is_deleted=False, deleted_at=None, deleted_by=None)
    in_trash = list(queryset.filter(folder=TRASH, is_deleted=False).select_related('connection'))
    connections = _connections_for(in_trash)
    by_target = {}
    for message in in_trash:
        target = message.previous_folder or 'INBOX'
        if target == TRASH:
            target = 'INBOX'
        by_target.setdefault((message.connection_id, target), []).append(message)
    for (connection_id, target), group in by_target.items():
        items = [message_item(m) for m in group]
        EmailMessage.objects.filter(pk__in=[m.pk for m in group]).update(
            folder=target, previous_folder='', uid='', uid_validity='',
        )
        schedule_mailbox_operation(connections[connection_id], 'move', items, target_folder=target)
        count += len(group)
    return count
