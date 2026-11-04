"""
A small in-memory IMAP server double with imaplib.IMAP4's call shapes.

Only what the mailbox/sync code uses: LIST, CAPABILITY, SELECT (+ the
UIDVALIDITY untagged response), CREATE, UID SEARCH/FETCH/STORE/COPY/MOVE/
EXPUNGE and the COPYUID response code. ``bare_expunges`` counts any
non-UID EXPUNGE so tests can assert it never happens.
"""
import re
from collections import OrderedDict

from social_integrations.email_utils import decode_imap_utf7


def raw_message(message_id='<m1@example.com>', subject='Hello', sender='Client <client@example.com>',
                to='info@example.com', date='Mon, 02 Nov 2026 10:00:00 +0000', body='Body text',
                reply_to=None, in_reply_to=None, references=None):
    lines = []
    if message_id is not None:
        lines.append(f'Message-ID: {message_id}')
    lines += [f'From: {sender}', f'To: {to}', f'Subject: {subject}', f'Date: {date}']
    if reply_to:
        lines.append(f'Reply-To: {reply_to}')
    if in_reply_to:
        lines.append(f'In-Reply-To: {in_reply_to}')
    if references:
        lines.append(f'References: {references}')
    lines += ['MIME-Version: 1.0', 'Content-Type: text/plain; charset=utf-8', '', body, '']
    return '\r\n'.join(lines).encode('utf-8')


def _unquote(name):
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        return re.sub(r'\\(.)', r'\1', name[1:-1])
    return name


class FakeFolder:
    def __init__(self, raw_name, flags=(), uidvalidity='100'):
        self.raw_name = raw_name
        self.flags = list(flags)
        self.uidvalidity = uidvalidity
        self.messages = OrderedDict()  # uid(int) -> {'raw': bytes, 'flags': set}
        self.next_uid = 1

    def add(self, raw, flags=(), uid=None):
        uid = uid or self.next_uid
        self.next_uid = max(self.next_uid, uid + 1)
        self.messages[uid] = {'raw': raw, 'flags': set(flags)}
        return uid


class FakeIMAP:
    def __init__(self, folders, capabilities=('IMAP4REV1', 'UIDPLUS', 'MOVE')):
        self.folders = OrderedDict((f.raw_name, f) for f in folders)
        self.caps = list(capabilities)
        self.selected = None
        self.readonly = None
        self._responses = {}
        self.commands = []
        self.bare_expunges = 0
        self.logged_out = False

    # -- helpers ----------------------------------------------------------
    def folder(self, raw_name):
        return self.folders[raw_name]

    def _uids(self, uid_set):
        folder = self.folders[self.selected]
        out = []
        for part in str(uid_set).split(','):
            if ':' in part:
                lo, hi = part.split(':')
                hi = max(folder.messages) if hi == '*' and folder.messages else int(hi) if hi != '*' else 0
                out.extend(u for u in folder.messages if int(lo) <= u <= hi)
            elif part.isdigit() and int(part) in folder.messages:
                out.append(int(part))
        return out

    # -- imaplib API ------------------------------------------------------
    def list(self, *args):
        lines = []
        for f in self.folders.values():
            flags = ' '.join(['\\HasNoChildren'] + f.flags)
            lines.append(f'({flags}) "/" "{f.raw_name}"'.encode())
        return 'OK', lines

    def capability(self):
        return 'OK', [' '.join(self.caps).encode()]

    def select(self, mailbox='INBOX', readonly=False):
        name = _unquote(mailbox)
        self.commands.append(('SELECT', name, readonly))
        if name not in self.folders:
            return 'NO', [b'Mailbox does not exist']
        self.selected = name
        self.readonly = readonly
        folder = self.folders[name]
        self._responses['UIDVALIDITY'] = [folder.uidvalidity.encode()]
        return 'OK', [str(len(folder.messages)).encode()]

    def response(self, code):
        value = self._responses.pop(code, None)
        return code, value if value is not None else [None]

    def create(self, mailbox):
        name = _unquote(mailbox)
        self.commands.append(('CREATE', name))
        self.folders[name] = FakeFolder(name, uidvalidity='900')
        return 'OK', [b'Created']

    def expunge(self):
        self.bare_expunges += 1
        return 'OK', [None]

    def logout(self):
        self.logged_out = True
        return 'BYE', [b'']

    def uid(self, command, *args):
        command = command.upper()
        self.commands.append(('UID', command) + tuple(args))
        folder = self.folders[self.selected]
        if command == 'SEARCH':
            criteria = [a for a in args if a is not None]
            if criteria and criteria[0] == 'HEADER':
                wanted = _unquote(criteria[2])
                hits = [u for u, m in folder.messages.items() if wanted.encode() in m['raw']]
                return 'OK', [' '.join(str(u) for u in hits).encode()]
            return 'OK', [' '.join(str(u) for u in folder.messages).encode()]
        if command == 'FETCH':
            uid_set, spec = args
            data = []
            for seq, uid in enumerate(self._uids(uid_set), start=1):
                msg = folder.messages[uid]
                flags = ' '.join(sorted(msg['flags']))
                if 'HEADER.FIELDS' in spec:
                    names = re.search(r'HEADER\.FIELDS \(([^)]*)\)', spec).group(1).split()
                    head = msg['raw'].split(b'\r\n\r\n', 1)[0] + b'\r\n'
                    literal = b''
                    for name in names:
                        # A header line plus any folded continuation lines.
                        match = re.search(
                            rb'^' + re.escape(name.encode()) + rb':[^\r\n]*\r\n(?:[ \t][^\r\n]*\r\n)*',
                            head, re.IGNORECASE | re.MULTILINE,
                        )
                        literal += match.group(0) if match else b''
                    literal += b'\r\n'
                    section = f'BODY[HEADER.FIELDS ({" ".join(names)})]'
                else:
                    literal = msg['raw']
                    section = 'BODY[]'
                if 'FLAGS' in spec:
                    meta = f'{seq} (UID {uid} FLAGS ({flags}) {section} {{{len(literal)}}}'.encode()
                else:
                    meta = f'{seq} (UID {uid} {section} {{{len(literal)}}}'.encode()
                data.append((meta, literal))
                data.append(b')')
            return 'OK', data or [None]
        if command == 'STORE':
            uid_set, op, flag_list = args
            flags = set(flag_list.strip('()').split())
            for uid in self._uids(uid_set):
                if op.startswith('+'):
                    folder.messages[uid]['flags'] |= flags
                else:
                    folder.messages[uid]['flags'] -= flags
            return 'OK', [None]
        if command in ('COPY', 'MOVE'):
            uid_set, target = args
            target_folder = self.folders.get(_unquote(target))
            if target_folder is None:
                return 'NO', [b'[TRYCREATE] no such mailbox']
            src, dst = [], []
            for uid in self._uids(uid_set):
                msg = folder.messages[uid]
                new_uid = target_folder.add(msg['raw'], msg['flags'] - {'\\Deleted'})
                src.append(str(uid))
                dst.append(str(new_uid))
                if command == 'MOVE':
                    del folder.messages[uid]
            if 'UIDPLUS' in self.caps and src:
                self._responses['COPYUID'] = [
                    f'{target_folder.uidvalidity} {",".join(src)} {",".join(dst)}'.encode()
                ]
            return 'OK', [None]
        if command == 'EXPUNGE':
            (uid_set,) = args
            for uid in self._uids(uid_set):
                if '\\Deleted' in folder.messages[uid]['flags']:
                    del folder.messages[uid]
            return 'OK', [None]
        raise AssertionError(f'unexpected UID command {command}')

    # convenience for assertions
    def decoded_names(self):
        return [decode_imap_utf7(n) for n in self.folders]
