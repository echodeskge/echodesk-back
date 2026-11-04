"""
End-to-end email behaviour against a REAL IMAP/SMTP server (GreenMail).

Skipped unless GREENMAIL_HOST is set. Never point it at a real mailbox: the
test purges every folder of both test users before it runs.

    docker run -d --rm --name greenmail-it \\
      -e GREENMAIL_OPTS='-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 \\
        -Dgreenmail.users=agent:secret@example.test,customer:secret@example.test \\
        -Dgreenmail.auth.disabled=false' \\
      -p 3025:3025 -p 3143:3143 -p 3465:3465 -p 3993:3993 greenmail/standalone:2.1.2
    GREENMAIL_HOST=127.0.0.1 python3 -m pytest social_integrations/tests/test_email_greenmail_integration.py

GreenMail's plain ports have no STARTTLS, so the connection uses the SSL
ports (imaplib/smtplib don't verify certificates by default). GreenMail has
no special-use folders, so Sent / Trash are created by EchoDesk on demand.
Celery tasks run eagerly (``.delay`` -> ``.apply``) once the request commits.
"""
import email
import imaplib
import os
import smtplib
import time
import unittest
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from unittest import mock

from django.core.cache import cache
from django.core.files.storage import InMemoryStorage
from rest_framework import status

from social_integrations import email_mailbox
from social_integrations.email_utils import encode_imap_utf7, sync_imap_messages
from social_integrations.models import EmailMessage, EmailSignature
from social_integrations.tasks import email_mailbox_operation_task, email_post_send_task
from social_integrations.tests.conftest import SocialIntegrationTestCase

GM_HOST = os.environ.get('GREENMAIL_HOST')
IMAPS_PORT = int(os.environ.get('GREENMAIL_IMAPS_PORT', '3993'))
SMTPS_PORT = int(os.environ.get('GREENMAIL_SMTPS_PORT', '3465'))
PASSWORD = 'secret'
AGENT = 'agent@example.test'
CUSTOMER = 'customer@example.test'
GEORGIAN = 'ინვოისები'

SEND_URL = '/api/social/email/send/'
ACTION_URL = '/api/social/email/action/'
MOVE_URL = '/api/social/email-messages/move/'
LIST_URL = '/api/social/email-messages/'
STATUS_URL = '/api/social/email/status/'


def _login(user):
    imap = imaplib.IMAP4_SSL(GM_HOST, IMAPS_PORT, timeout=10)
    imap.login(user.split('@')[0], PASSWORD)
    return imap


def _purge(user):
    """Empty every folder of ``user``.

    Folders are kept: GreenMail 2.1 can crash a session on DELETE of a
    folder another (closed) session had selected.
    """
    imap = _login(user)
    try:
        for entry in email_mailbox.list_mailboxes(imap) or []:
            typ, _ = imap.select(email_mailbox.quote_mailbox(entry['raw_name']))
            if typ == 'OK':
                imap.store('1:*', '+FLAGS.SILENT', '(\\Deleted)')
                imap.expunge()  # test server cleanup only
    finally:
        imap.logout()


def _server_folder(user, folder):
    """{uid: {'flags': lower-case set, 'message_id': str}} for a decoded folder name, or None."""
    imap = _login(user)
    try:
        typ, _ = imap.select(email_mailbox.quote_mailbox(encode_imap_utf7(folder)), readonly=True)
        if typ != 'OK':
            return None
        typ, data = imap.uid('FETCH', '1:*', '(UID FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
        if typ != 'OK' or not data or data == [None]:
            return {}
        out = {}
        for entry in email_mailbox.parse_fetch_response(data):
            out[int(entry['uid'])] = {
                'flags': entry['flags'],
                'message_id': email_mailbox._header_message_id(entry['body']),
            }
        return out
    finally:
        imap.logout()


def _find(user, folder, message_id):
    """(uid, flags) of ``message_id`` in ``folder`` on the server, or (None, None)."""
    for uid, info in (_server_folder(user, folder) or {}).items():
        if info['message_id'] == message_id:
            return uid, info['flags']
    return None, None


def _wait_for(user, folder, message_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        uid, flags = _find(user, folder, message_id)
        if uid is not None:
            return uid, flags
        time.sleep(0.2)
    raise AssertionError(f'{message_id} never arrived in {user}/{folder}')


def _customer_sends(subject, body, in_reply_to=None, references=None, attachment=None):
    msg = MIMEText(body, 'plain', 'utf-8')
    if attachment:
        mixed = MIMEMultipart('mixed')
        mixed.attach(msg)
        part = MIMEApplication(attachment[1], Name=attachment[0])
        part['Content-Disposition'] = f'attachment; filename="{attachment[0]}"'
        mixed.attach(part)
        msg = mixed
    msg['From'] = f'Customer <{CUSTOMER}>'
    msg['To'] = AGENT
    msg['Subject'] = subject
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid(domain='example.test')
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = references or in_reply_to
    smtp = smtplib.SMTP_SSL(GM_HOST, SMTPS_PORT, timeout=10)
    try:
        smtp.login('customer', PASSWORD)
        smtp.sendmail(CUSTOMER, [AGENT], msg.as_string())
    finally:
        smtp.quit()
    _wait_for(AGENT, 'INBOX', msg['Message-ID'])
    return msg['Message-ID']


@unittest.skipUnless(GM_HOST, 'GREENMAIL_HOST not set (needs a local GreenMail container)')
class TestGreenMailIntegration(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        # Attachments go to memory, never to the configured (cloud) storage.
        self.storage = InMemoryStorage(base_url='https://cdn.example.test/media/')
        self._storage_patcher = mock.patch('social_integrations.email_utils.default_storage', self.storage)
        self._storage_patcher.start()
        self.addCleanup(self._storage_patcher.stop)
        _purge(AGENT)
        _purge(CUSTOMER)
        self.conn = self.create_email_connection(
            email_address=AGENT, display_name='Agent Desk',
            imap_server=GM_HOST, imap_port=IMAPS_PORT, imap_use_ssl=True,
            smtp_server=GM_HOST, smtp_port=SMTPS_PORT, smtp_use_ssl=True, smtp_use_tls=False,
            username='agent', sync_days_back=30,
        )
        self.conn.set_password(PASSWORD)
        self.conn.save()
        EmailSignature.objects.create(
            signature_html='<p>Kind regards,<br>EchoDesk Team</p>',
            signature_text='Kind regards,\nEchoDesk Team',
            is_enabled=True, include_on_reply=True,
        )
        self.user = self.create_user(email='gm-agent@test.com')

    # -- helpers --------------------------------------------------------

    def eager(self):
        """Run queued Celery tasks in-process when the request commits."""
        return _Eager(self)

    def post(self, url, payload):
        with self.eager():
            resp = self.api_post(url, payload, user=self.user)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return resp

    def sync(self):
        return sync_imap_messages(self.conn)

    def rows(self, message_id):
        return EmailMessage.objects.filter(connection=self.conn, message_id=message_id)

    # -- the scenario ---------------------------------------------------

    def test_full_mailbox_lifecycle(self):
        # C2: the composer sees the signature send will use.
        status_resp = self.api_get(STATUS_URL, user=self.user)
        self.assertEqual(status_resp.status_code, 200)
        conn_payload = next(
            c for c in status_resp.json()['connections'] if c['id'] == self.conn.id
        )
        self.assertEqual(conn_payload['effective_signature']['source'], 'tenant')

        # 1. Send (tenant signature appended).
        resp = self.post(SEND_URL, {
            'connection_id': self.conn.id,
            'to_emails': [CUSTOMER],
            'subject': 'Your order',
            'body_text': 'Hello from EchoDesk',
            'body_html': '<p>Hello from EchoDesk</p>',
        })
        sent_row = EmailMessage.objects.get(pk=resp.json()['message_id'])
        sent_id = sent_row.message_id
        self.assertEqual(sent_row.folder, 'Sent')
        self.assertIn('EchoDesk Team', sent_row.body_text)

        # ... arrives at the customer, and a \Seen copy sits in the agent's Sent.
        _wait_for(CUSTOMER, 'INBOX', sent_id)
        sent_uid, sent_flags = _find(AGENT, 'Sent', sent_id)
        self.assertIsNotNone(sent_uid, 'sent copy was not APPENDed to the Sent folder')
        self.assertIn('\\seen', sent_flags)
        sent_row.refresh_from_db()
        self.assertEqual(sent_row.uid, str(sent_uid))  # APPENDUID recorded

        # 2. Sync: exactly one Sent row (no duplicate), signature kept.
        self.sync()
        self.assertEqual(self.rows(sent_id).count(), 1)
        sent_row.refresh_from_db()
        self.assertEqual(sent_row.folder, 'Sent')
        self.assertIn('EchoDesk Team', sent_row.body_text)
        self.assertFalse(sent_row.is_deleted)

        # 3. Customer replies -> sync -> one conversation.
        reply_id = _customer_sends('Re: Your order', 'Thanks!', in_reply_to=sent_id)
        self.sync()
        reply_row = self.rows(reply_id).get()
        self.assertEqual(reply_row.folder, 'INBOX')
        self.assertEqual(reply_row.thread_id, sent_row.thread_id)
        listing = self.api_get(f'{LIST_URL}?thread_id={sent_row.thread_id}', user=self.user)
        self.assertEqual(listing.status_code, 200)
        results = listing.json()
        results = results.get('results', results) if isinstance(results, dict) else results
        self.assertEqual([m['message_id'] for m in results], [sent_id, reply_id])
        self.assertEqual({m['folder'] for m in results}, {'Sent', 'INBOX'})

        # 4. Reply from EchoDesk marks the customer's message \Answered on the server.
        resp = self.post(SEND_URL, {
            'connection_id': self.conn.id,
            'to_emails': [CUSTOMER],
            'subject': 'Re: Your order',
            'body_text': 'You are welcome',
            'reply_to_message_id': reply_row.id,
        })
        second_sent_id = EmailMessage.objects.get(pk=resp.json()['message_id']).message_id
        reply_row.refresh_from_db()
        self.assertTrue(reply_row.is_answered)
        reply_uid, flags = _find(AGENT, 'INBOX', reply_id)
        self.assertIn('\\answered', flags)
        self.assertIsNotNone(_find(AGENT, 'Sent', second_sent_id)[0])

        # 5. Read / unread / star write-back.
        self.post(ACTION_URL, {'message_ids': [reply_row.id], 'action': 'mark_read'})
        self.assertIn('\\seen', _find(AGENT, 'INBOX', reply_id)[1])
        self.post(ACTION_URL, {'message_ids': [reply_row.id], 'action': 'mark_unread'})
        self.post(ACTION_URL, {'message_ids': [reply_row.id], 'action': 'star'})
        flags = _find(AGENT, 'INBOX', reply_id)[1]
        self.assertNotIn('\\seen', flags)
        self.assertIn('\\flagged', flags)
        self.sync()  # server flags agree; sync must not undo them
        reply_row.refresh_from_db()
        self.assertEqual((reply_row.is_read, reply_row.is_starred), (False, True))

        # 6. Delete -> server Trash; restore -> back; delete in Trash -> only that uid.
        bystander_id = _customer_sends('Unrelated', 'keep me')
        self.sync()
        bystander = self.rows(bystander_id).get()
        self.post(ACTION_URL, {'message_ids': [bystander.id], 'action': 'delete'})
        bystander.refresh_from_db()
        self.assertEqual((bystander.folder, bystander.previous_folder), ('Trash', 'INBOX'))
        self.assertIsNone(_find(AGENT, 'INBOX', bystander_id)[0])
        self.assertIsNotNone(_find(AGENT, 'Trash', bystander_id)[0])

        self.post(ACTION_URL, {'message_ids': [reply_row.id], 'action': 'delete'})
        reply_row.refresh_from_db()
        self.assertEqual(reply_row.folder, 'Trash')
        self.assertIsNotNone(_find(AGENT, 'Trash', reply_id)[0])
        self.post(ACTION_URL, {'message_ids': [reply_row.id], 'action': 'restore'})
        reply_row.refresh_from_db()
        self.assertEqual((reply_row.folder, reply_row.previous_folder), ('INBOX', ''))
        self.assertIsNone(_find(AGENT, 'Trash', reply_id)[0])
        restored_uid, flags = _find(AGENT, 'INBOX', reply_id)
        self.assertIsNotNone(restored_uid)
        self.assertIn('\\flagged', flags)  # flags travel with the move

        # Permanent delete of the bystander from Trash; the other Trash
        # message (the first EchoDesk reply copy we move there) survives.
        self.post(ACTION_URL, {'message_ids': [sent_row.id], 'action': 'delete'})
        self.assertIsNotNone(_find(AGENT, 'Trash', sent_id)[0])
        self.post(ACTION_URL, {'message_ids': [bystander.id], 'action': 'delete'})
        bystander.refresh_from_db()
        self.assertTrue(bystander.is_deleted)
        trash = _server_folder(AGENT, 'Trash')
        self.assertNotIn(bystander_id, {m['message_id'] for m in trash.values()})
        self.assertIn(sent_id, {m['message_id'] for m in trash.values()})

        # 7. Move to a Georgian-named folder (C7 endpoint).
        imap = _login(AGENT)
        imap.create(email_mailbox.quote_mailbox(encode_imap_utf7(GEORGIAN)))
        imap.logout()
        self.post(MOVE_URL, {
            'message_id': reply_id, 'source_folder': 'INBOX', 'target_folder': GEORGIAN,
        })
        reply_row.refresh_from_db()
        self.assertEqual(reply_row.folder, GEORGIAN)
        self.assertIsNone(_find(AGENT, 'INBOX', reply_id)[0])
        geo_uid, _ = _find(AGENT, GEORGIAN, reply_id)
        self.assertIsNotNone(geo_uid)
        self.assertEqual(reply_row.uid, str(geo_uid))

        # The folder list shows it with role + counts (C5).
        with mock.patch('social_integrations.views.logger'):
            folders = self.api_get(f'/api/social/email/folders/?connection_id={self.conn.id}', user=self.user)
        self.assertEqual(folders.status_code, 200)
        details = {f['name']: f for f in folders.json()['folder_details']}
        self.assertEqual(details[GEORGIAN]['role'], 'custom')
        self.assertEqual(details[GEORGIAN]['total_count'], 1)
        self.assertEqual(details['Trash']['role'], 'trash')
        self.assertEqual(details['Sent']['role'], 'sent')

        # 8. A message deleted on the server (another client) is reconciled.
        self.sync()
        self.assertFalse(self.rows(reply_id).get().is_deleted)
        imap = _login(AGENT)
        imap.select(email_mailbox.quote_mailbox(encode_imap_utf7(GEORGIAN)))
        imap.uid('STORE', str(geo_uid), '+FLAGS.SILENT', '(\\Deleted)')
        imap.uid('EXPUNGE', str(geo_uid))
        imap.logout()
        self.sync()
        self.assertTrue(self.rows(reply_id).get().is_deleted)

        # 9. Another sync creates nothing new and no duplicates.
        before = EmailMessage.objects.filter(connection=self.conn).count()
        self.assertEqual(self.sync(), 0)
        self.assertEqual(EmailMessage.objects.filter(connection=self.conn).count(), before)
        for mid in (sent_id, second_sent_id, reply_id, bystander_id):
            self.assertEqual(self.rows(mid).count(), 1, mid)

    def test_forward_copy_fallback_and_special_folders(self):
        # Customer mail with an attachment, forwarded with the composer's own
        # signature (signature_included): attachment re-attached, signature once.
        original_id = _customer_sends('Invoice', 'see attached', attachment=('invoice.pdf', b'%PDF-1.4 test'))
        self.sync()
        original = self.rows(original_id).get()
        self.assertEqual([a['filename'] for a in original.attachments], ['invoice.pdf'])

        resp = self.post(SEND_URL, {
            'connection_id': self.conn.id,
            'to_emails': [CUSTOMER],
            'subject': 'Fwd: Invoice',
            'body_html': '<p>FYI</p><p>Kind regards,<br>EchoDesk Team</p>',
            'body_text': 'FYI\n\nKind regards,\nEchoDesk Team',
            'forward_message_id': original.id,
            'signature_included': True,
        })
        fwd = EmailMessage.objects.get(pk=resp.json()['message_id'])
        self.assertEqual(fwd.body_text.count('EchoDesk Team'), 1)
        self.assertEqual([a['filename'] for a in fwd.attachments], ['invoice.pdf'])
        self.assertIn('$forwarded', _find(AGENT, 'INBOX', original_id)[1])

        # What the customer received carries the attachment bytes.
        _wait_for(CUSTOMER, 'INBOX', fwd.message_id)
        imap = _login(CUSTOMER)
        imap.select('INBOX', readonly=True)
        uid, _ = _find(CUSTOMER, 'INBOX', fwd.message_id)
        typ, data = imap.uid('FETCH', str(uid), '(BODY.PEEK[])')
        imap.logout()
        received = email.message_from_bytes(email_mailbox.parse_fetch_response(data)[0]['body'])
        payloads = [p.get_payload(decode=True) for p in received.walk() if p.get_filename() == 'invoice.pdf']
        self.assertEqual(payloads, [b'%PDF-1.4 test'])

        # Junk is synced under its canonical name, Drafts is not synced.
        imap = _login(AGENT)
        for folder, mid in (('Junk', '<junk-1@example.test>'), ('Drafts', '<draft-1@example.test>')):
            imap.create(folder)
            raw = (f'From: x@example.test\r\nTo: {AGENT}\r\nSubject: {folder}\r\n'
                   f'Message-ID: {mid}\r\nDate: {formatdate(localtime=True)}\r\n\r\nbody\r\n').encode()
            imap.append(folder, None, imaplib.Time2Internaldate(time.time()), raw)
        imap.logout()
        self.sync()
        self.assertEqual(self.rows('<junk-1@example.test>').get().folder, 'Junk')
        self.assertFalse(self.rows('<draft-1@example.test>').exists())

        # Server without MOVE: UID COPY + UID STORE \Deleted + UID EXPUNGE <uid>,
        # leaving another \Deleted-flagged message in INBOX alone.
        other_id = _customer_sends('Other', 'flagged deleted by another client')
        imap = _login(AGENT)
        imap.select('INBOX')
        other_uid, _ = _find(AGENT, 'INBOX', other_id)
        imap.uid('STORE', str(other_uid), '+FLAGS.SILENT', '(\\Deleted)')
        imap.logout()
        real_caps = email_mailbox.imap_capabilities

        def without_move(imap):
            return real_caps(imap) - {'MOVE'}

        with mock.patch.object(email_mailbox, 'imap_capabilities', side_effect=without_move):
            self.post(ACTION_URL, {'message_ids': [original.id], 'action': 'move', 'folder': 'Archive'})
        original.refresh_from_db()
        self.assertEqual(original.folder, 'Archive')
        self.assertIsNone(_find(AGENT, 'INBOX', original_id)[0])
        archive_uid, _ = _find(AGENT, 'Archive', original_id)
        self.assertIsNotNone(archive_uid)
        self.assertEqual(original.uid, str(archive_uid))  # COPYUID recorded
        self.assertIsNotNone(_find(AGENT, 'INBOX', other_id)[0])  # never a bare EXPUNGE

        self.sync()  # picks up "Other"; the moved message must not reappear
        self.assertEqual(self.rows(original_id).get().folder, 'Archive')
        self.assertEqual(self.rows(original_id).count(), 1)
        before = EmailMessage.objects.filter(connection=self.conn).count()
        self.assertEqual(self.sync(), 0)
        self.assertEqual(EmailMessage.objects.filter(connection=self.conn).count(), before)


class _Eager:
    """Patch the email Celery tasks to run in-process and flush on_commit."""

    def __init__(self, case):
        self.case = case
        self.stack = []

    def __enter__(self):
        patches = [
            mock.patch.object(email_mailbox_operation_task, 'delay',
                              side_effect=lambda *a: email_mailbox_operation_task.apply(args=a)),
            mock.patch.object(email_post_send_task, 'delay',
                              side_effect=lambda *a: email_post_send_task.apply(args=a)),
        ]
        for p in patches:
            p.start()
            self.stack.append(p)
        self.capture = self.case.captureOnCommitCallbacks(execute=True)
        self.capture.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            self.capture.__exit__(*exc)
        finally:
            for p in reversed(self.stack):
                p.stop()
        return False
