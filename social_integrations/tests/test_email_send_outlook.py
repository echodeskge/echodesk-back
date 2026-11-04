"""
Send behaves like Outlook: the delivered MIME is APPENDed to the server's
Sent folder (\\Seen, raw folder name), the DB Sent row equals what was
delivered, replies mark the original answered, forwards re-attach the
original attachments, and the signature the composer shows is the one the
server would append (effective_signature). smtplib/imaplib are mocked; no
network.
"""
import email
import smtplib
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from rest_framework import status

from social_integrations.email_utils import (
    _find_sent_folder,
    append_to_sent_folder,
    build_references,
    get_effective_signature,
    perform_post_send_imap,
    send_email_smtp,
)
from social_integrations.models import (
    EmailConnectionUserAssignment,
    EmailDraft,
    EmailMessage,
    EmailSignature,
)
from social_integrations.tests.conftest import SocialIntegrationTestCase

SEND_URL = '/api/social/email/send/'
STATUS_URL = '/api/social/email/status/'

DOVECOT_LIST = [
    b'(\\HasNoChildren) "/" INBOX',
    b'(\\HasNoChildren) "/" "Unsent ideas"',
    b'(\\HasNoChildren \\Sent) "/" &ENgQ0BDSENAQ3BDYENsQmA-',
    b'(\\HasNoChildren \\Trash) "/" Trash',
]


def _imap_mock(list_entries=None, append_uid=b'4242'):
    imap = mock.MagicMock()
    imap.list.return_value = ('OK', list_entries if list_entries is not None else DOVECOT_LIST)
    data = [b'[APPENDUID 1700000000 ' + append_uid + b'] Append completed.'] if append_uid else [b'Append completed.']
    imap.append.return_value = ('OK', data)
    imap.select.return_value = ('OK', [b'1'])
    imap.uid.return_value = ('OK', [b''])
    imap.create.return_value = ('OK', [b'Created'])
    return imap


class TestFindSentFolder(SocialIntegrationTestCase):

    def test_special_use_flag_wins_and_raw_name_is_kept(self):
        imap = _imap_mock()
        self.assertEqual(_find_sent_folder(imap), '&ENgQ0BDSENAQ3BDYENsQmA-')

    def test_same_folder_as_sync_first_sent_name_in_list_order(self):
        # Sync stores the first sent-like folder as "Sent"; send must APPEND
        # to that same folder, not to the other one sync treats as custom.
        imap = _imap_mock([
            b'(\\HasNoChildren) "/" "Unsent ideas"',
            b'(\\HasNoChildren) "." "INBOX.Sent Items"',
            b'(\\HasNoChildren) "/" "Sent Items"',
        ])
        self.assertEqual(_find_sent_folder(imap), 'INBOX.Sent Items')

    def test_partial_name_is_not_sent(self):
        # Sync keeps "Sent-2024" as a custom folder, so it is not the Sent target.
        imap = _imap_mock([b'(\\HasNoChildren) "/" INBOX', b'(\\HasNoChildren) "/" "Sent-2024"'])
        self.assertIsNone(_find_sent_folder(imap))

    def test_none_when_nothing_matches(self):
        imap = _imap_mock([b'(\\HasNoChildren) "/" INBOX'])
        self.assertIsNone(_find_sent_folder(imap))


class TestAppendToSentFolder(SocialIntegrationTestCase):

    def test_append_uses_quoted_raw_name_seen_flag_and_returns_uid(self):
        conn = self.create_email_connection()
        imap = _imap_mock()
        uid = append_to_sent_folder(conn, b'Subject: x\r\n\r\nbody', imap=imap)
        self.assertEqual(uid, '4242')
        args = imap.append.call_args.args
        self.assertEqual(args[0], '"&ENgQ0BDSENAQ3BDYENsQmA-"')
        self.assertEqual(args[1], '(\\Seen)')
        self.assertEqual(args[3], b'Subject: x\r\n\r\nbody')
        imap.create.assert_not_called()

    def test_creates_sent_when_missing(self):
        conn = self.create_email_connection()
        imap = _imap_mock([b'(\\HasNoChildren) "/" INBOX'], append_uid=None)
        self.assertIsNone(append_to_sent_folder(conn, b'x', imap=imap))
        imap.create.assert_called_once_with('"Sent"')
        self.assertEqual(imap.append.call_args.args[0], '"Sent"')

    def test_append_no_raises(self):
        conn = self.create_email_connection()
        imap = _imap_mock()
        imap.append.return_value = ('NO', [b'Quota exceeded'])
        with self.assertRaises(Exception):
            append_to_sent_folder(conn, b'x', imap=imap)


class _SendTestBase(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.conn = self.create_email_connection(
            email_address='info@amanati.ge', smtp_server='mail.amanati.ge', imap_server='mail.amanati.ge',
        )
        self.conn.smtp_use_ssl = False
        self.conn.smtp_use_tls = False
        self.conn.save()
        self.smtp_patcher = mock.patch('social_integrations.email_utils.smtplib.SMTP')
        self.smtp_cls = self.smtp_patcher.start()
        self.smtp_cls.return_value.sendmail.return_value = {}
        self.imap = _imap_mock()
        self.imap_patcher = mock.patch('social_integrations.email_utils._connect_imap', return_value=self.imap)
        self.connect_imap = self.imap_patcher.start()
        self.storage = InMemoryStorage(base_url='https://cdn.example.com/media/')
        self.storage_patcher = mock.patch('social_integrations.email_utils.default_storage', self.storage)
        self.storage_patcher.start()
        # Run the post-send Celery task synchronously.
        from social_integrations.tasks import email_post_send_task
        self.delay_patcher = mock.patch.object(
            email_post_send_task, 'delay',
            side_effect=lambda *a: email_post_send_task.apply(args=a),
        )
        self.delay = self.delay_patcher.start()

    def tearDown(self):
        mock.patch.stopall()
        super().tearDown()

    def serve_message_ids(self, uid_to_message_id):
        """Answer the uid-verification FETCH (Message-ID by UID) on the mock."""
        def uid(command, *args):
            if command == 'FETCH':
                data = []
                for u in args[0].split(','):
                    if u in uid_to_message_id:
                        literal = f'Message-ID: {uid_to_message_id[u]}\r\n\r\n'.encode()
                        meta = f'1 (UID {u} BODY[HEADER.FIELDS (MESSAGE-ID)] {{{len(literal)}}}'.encode()
                        data += [(meta, literal), b')']
                return 'OK', data or [None]
            return 'OK', [b'']
        self.imap.uid.side_effect = uid

    def smtp_payload(self):
        return self.smtp_cls.return_value.sendmail.call_args.args[2]

    def send(self, *args, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return send_email_smtp(self.conn, *args, **kwargs)


class TestSendAppendsToSent(_SendTestBase):

    def test_sent_copy_appended_with_bcc_and_uid_stored(self):
        _, sent = self.send(['to@example.com'], bcc_emails=['hidden@example.com'],
                            subject='Hi', body_text='Hello')
        self.assertNotIn('hidden@example.com', self.smtp_payload())
        envelope = self.smtp_cls.return_value.sendmail.call_args.args[1]
        self.assertIn('hidden@example.com', envelope)

        self.imap.append.assert_called_once()
        mailbox, flags, _, raw = self.imap.append.call_args.args
        self.assertEqual(mailbox, '"&ENgQ0BDSENAQ3BDYENsQmA-"')
        self.assertEqual(flags, '(\\Seen)')
        appended = email.message_from_bytes(raw)
        self.assertEqual(appended['Bcc'], 'hidden@example.com')
        self.assertEqual(appended['Message-ID'], sent.message_id)

        sent.refresh_from_db()
        self.assertEqual(sent.uid, '4242')
        self.assertEqual(sent.folder, 'Sent')
        self.smtp_cls.return_value.quit.assert_called_once()

    def test_gmail_skips_append(self):
        self.conn.smtp_server = 'smtp.gmail.com'
        self.conn.imap_server = 'imap.gmail.com'
        self.conn.save()
        self.send(['to@example.com'], subject='Hi', body_text='Hello')
        self.imap.append.assert_not_called()
        self.delay.assert_not_called()

    def test_office365_skips_append_but_still_flags_original(self):
        self.conn.smtp_server = 'smtp.office365.com'
        self.conn.save()
        original = self.create_email_message(connection=self.conn, uid='77', folder='INBOX')
        self.serve_message_ids({'77': original.message_id})
        self.send(['to@example.com'], subject='Re', body_text='x', reply_to_message=original)
        self.imap.append.assert_not_called()
        self.imap.uid.assert_any_call('STORE', '77', '+FLAGS', '(\\Answered)')

    def test_append_failure_does_not_fail_send(self):
        self.imap.append.side_effect = OSError('connection reset')
        message_id, sent = self.send(['to@example.com'], subject='Hi', body_text='Hello')
        self.assertIsNotNone(sent)
        self.assertTrue(EmailMessage.objects.filter(pk=sent.pk, folder='Sent').exists())

    def test_broker_down_falls_back_inline_and_swallows_imap_errors(self):
        self.delay.side_effect = ConnectionError('redis down')
        self.connect_imap.side_effect = OSError('imap down')
        _, sent = self.send(['to@example.com'], subject='Hi', body_text='Hello')
        self.assertIsNotNone(sent)
        self.connect_imap.assert_called_once()


class TestSendDbRowAndSignature(_SendTestBase):

    def setUp(self):
        super().setUp()
        EmailSignature.objects.all().delete()
        EmailSignature.objects.create(
            signature_text='Best,\nAmanati', signature_html='<p>Best, <b>Amanati</b></p>',
            is_enabled=True, include_on_reply=False,
        )

    def test_db_row_matches_delivered_body_and_attachments_have_urls(self):
        _, sent = self.send(
            ['to@example.com'], subject='Hi', body_text='Hello', body_html='<p>Hello</p>',
            attachments=[{'filename': 'r.pdf', 'content': b'%PDF', 'content_type': 'application/pdf'}],
        )
        self.assertIn('Amanati', sent.body_text)
        self.assertIn('<b>Amanati</b>', sent.body_html)
        payload = self.smtp_payload()
        self.assertIn(sent.body_text.split('\n')[-1], email.message_from_string(payload).get_payload(0)
                      .get_payload(0).get_payload(decode=True).decode())
        att = sent.attachments[0]
        self.assertEqual(att['filename'], 'r.pdf')
        self.assertTrue(att['url'].startswith('https://cdn.example.com/media/email_attachments/'))
        # unguessable uuid4-hex path segment before the filename
        segment = att['url'].rsplit('/', 2)[-2]
        self.assertEqual(len(segment), 32)

    def test_signature_included_skips_signature(self):
        _, sent = self.send(['to@example.com'], subject='Hi', body_text='Hello\n\n--\nMine',
                            signature_included=True)
        self.assertEqual(sent.body_text, 'Hello\n\n--\nMine')
        self.assertNotIn('Amanati', self.smtp_payload())

    def test_tenant_signature_respects_include_on_reply(self):
        original = self.create_email_message(connection=self.conn)
        _, sent = self.send(['to@example.com'], subject='Re', body_text='Thanks', reply_to_message=original)
        self.assertEqual(sent.body_text, 'Thanks')


class TestEffectiveSignature(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        EmailSignature.objects.all().delete()

    def test_none(self):
        conn = self.create_email_connection()
        sig = get_effective_signature(conn)
        self.assertEqual(sig['source'], 'none')
        self.assertFalse(sig['is_enabled'])
        self.assertFalse(sig['applies'])

    def test_tenant(self):
        EmailSignature.objects.create(signature_html='<b>T</b>', is_enabled=True, include_on_reply=False)
        conn = self.create_email_connection()
        sig = get_effective_signature(conn)
        self.assertEqual((sig['source'], sig['signature_html'], sig['signature_text']), ('tenant', '<b>T</b>', 'T'))
        self.assertTrue(sig['applies'])
        self.assertFalse(get_effective_signature(conn, is_reply=True)['applies'])

    def test_disabled_tenant_is_none(self):
        EmailSignature.objects.create(signature_html='<b>T</b>', is_enabled=False)
        self.assertEqual(get_effective_signature(self.create_email_connection())['source'], 'none')

    def test_connection_overrides_tenant(self):
        EmailSignature.objects.create(signature_html='<b>T</b>', is_enabled=True)
        conn = self.create_email_connection(signature_enabled=True, signature_text='Conn sig')
        sig = get_effective_signature(conn, is_reply=True)
        self.assertEqual(sig['source'], 'connection')
        self.assertEqual(sig['signature_html'], 'Conn sig')
        self.assertTrue(sig['include_on_reply'])
        self.assertTrue(sig['applies'])

    def test_enabled_but_empty_connection_signature_falls_back(self):
        EmailSignature.objects.create(signature_text='T', is_enabled=True)
        conn = self.create_email_connection(signature_enabled=True)
        self.assertEqual(get_effective_signature(conn)['source'], 'tenant')

    def test_status_endpoint_exposes_effective_signature(self):
        EmailSignature.objects.create(signature_text='Tenant sig', is_enabled=True, include_on_reply=True)
        self.create_email_connection(email_address='sig-status@example.com')
        agent = self.create_user(email='sig-status-agent@test.com')
        resp = self.api_get(STATUS_URL, user=agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        conn = next(c for c in resp.json()['connections'] if c['email_address'] == 'sig-status@example.com')
        self.assertEqual(conn['effective_signature'], {
            'source': 'tenant', 'is_enabled': True, 'include_on_reply': True,
            'signature_html': 'Tenant sig', 'signature_text': 'Tenant sig',
        })


class TestReferences(SocialIntegrationTestCase):

    def test_in_reply_to_used_when_references_missing(self):
        original = EmailMessage(message_id='<b@x>', in_reply_to='<a@x>', references='')
        self.assertEqual(build_references(original), '<a@x> <b@x>')

    def test_deduped_and_capped(self):
        refs = ' '.join(f'<r{i}@x>' for i in range(30))
        original = EmailMessage(message_id='<r29@x>', references=refs)
        result = build_references(original).split()
        self.assertEqual(len(result), 20)
        self.assertEqual(result[0], '<r0@x>')
        self.assertEqual(result[-1], '<r29@x>')
        self.assertEqual(len(set(result)), 20)


class TestSendView(_SendTestBase):

    def setUp(self):
        super().setUp()
        EmailSignature.objects.all().delete()
        self.agent = self.create_user(email='send-view-agent@test.com')

    def post(self, body):
        with self.captureOnCommitCallbacks(execute=True):
            return self.api_post(SEND_URL, body, user=self.agent)

    def test_reply_marks_answered_threads_and_flags_server(self):
        original = self.create_email_message(
            connection=self.conn, message_id='<orig@client.com>', in_reply_to='<root@client.com>',
            references='', uid='31', folder='INBOX',
        )
        self.serve_message_ids({'31': '<orig@client.com>'})
        resp = self.post({'to_emails': ['client@client.com'], 'body_text': 'ok',
                          'reply_to_message_id': original.id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        original.refresh_from_db()
        self.assertTrue(original.is_answered)
        payload = self.smtp_payload()
        self.assertIn('In-Reply-To: <orig@client.com>', payload)
        self.assertIn('References: <root@client.com> <orig@client.com>', payload)
        self.imap.uid.assert_any_call('STORE', '31', '+FLAGS', '(\\Answered)')
        self.assertEqual(resp.json()['refused_recipients'], [])

    def test_reply_to_inaccessible_message_400(self):
        other = self.create_email_connection(email_address='private@example.com')
        EmailConnectionUserAssignment.objects.create(
            connection=other, user=self.create_user(email='owner-x@test.com'),
        )
        foreign = self.create_email_message(connection=other)
        resp = self.post({'to_emails': ['a@example.com'], 'body_text': 'x', 'reply_to_message_id': foreign.id})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        resp = self.post({'to_emails': ['a@example.com'], 'body_text': 'x', 'reply_to_message_id': 999999})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.smtp_cls.return_value.sendmail.assert_not_called()

    def test_forward_reattaches_original_attachments(self):
        name = self.storage.save('email_attachments/1/2026/01/01/spec.pdf', ContentFile(b'%PDF-orig'))
        original = self.create_email_message(
            connection=self.conn, uid='9', folder='INBOX',
            attachments=[{'filename': 'spec.pdf', 'content_type': 'application/pdf',
                          'url': self.storage.url(name), 'size': 9}],
        )
        self.serve_message_ids({'9': original.message_id})
        resp = self.post({'to_emails': ['fw@example.com'], 'body_text': 'FYI', 'subject': 'Fwd: spec',
                          'forward_message_id': original.id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        sent_mime = email.message_from_string(self.smtp_payload())
        parts = [p for p in sent_mime.walk() if p.get_filename() == 'spec.pdf']
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_payload(decode=True), b'%PDF-orig')
        self.assertEqual(resp.json()['data']['attachments'][0]['filename'], 'spec.pdf')
        self.imap.uid.assert_any_call('STORE', '9', '+FLAGS', '($Forwarded)')

    def test_forward_unknown_message_400(self):
        resp = self.post({'to_emails': ['fw@example.com'], 'body_text': 'x', 'forward_message_id': 999999})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_draft_deleted_after_send(self):
        draft = self.create_email_draft(connection=self.conn, created_by=self.agent)
        someone_else = self.create_email_draft(connection=self.conn)
        resp = self.post({'to_emails': ['a@example.com'], 'body_text': 'x', 'draft_id': draft.id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertFalse(EmailDraft.objects.filter(pk=draft.pk).exists())
        resp = self.post({'to_emails': ['a@example.com'], 'body_text': 'x', 'draft_id': someone_else.id})
        self.assertTrue(EmailDraft.objects.filter(pk=someone_else.pk).exists())

    def test_draft_kept_when_send_fails(self):
        draft = self.create_email_draft(connection=self.conn, created_by=self.agent)
        self.smtp_cls.return_value.sendmail.side_effect = smtplib.SMTPDataError(554, b'Spam detected')
        resp = self.post({'to_emails': ['a@example.com'], 'body_text': 'x', 'draft_id': draft.id})
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertIn('Spam detected', resp.json()['details'])
        self.assertTrue(EmailDraft.objects.filter(pk=draft.pk).exists())
        self.smtp_cls.return_value.quit.assert_called_once()
        self.assertFalse(EmailMessage.objects.filter(connection=self.conn, folder='Sent').exists())

    def test_partial_refusal_still_saves_and_reports(self):
        self.smtp_cls.return_value.sendmail.return_value = {'bad@example.com': (550, b'No such user')}
        resp = self.post({'to_emails': ['good@example.com', 'bad@example.com'], 'body_text': 'x'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()['refused_recipients'], ['bad@example.com'])
        self.assertTrue(resp.json()['warnings'])
        self.assertIsNotNone(resp.json()['data'])

    def test_all_refused_is_400(self):
        self.smtp_cls.return_value.sendmail.side_effect = smtplib.SMTPRecipientsRefused(
            {'bad@example.com': (550, b'No such user')})
        resp = self.post({'to_emails': ['bad@example.com'], 'body_text': 'x'})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()['refused_recipients'], ['bad@example.com'])

    def test_auth_failure_is_400_with_clear_message(self):
        self.smtp_cls.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b'bad creds')
        resp = self.post({'to_emails': ['a@example.com'], 'body_text': 'x'})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Mailbox login failed', resp.json()['error'])
        self.smtp_cls.return_value.quit.assert_called_once()

    def test_imap_failure_never_fails_send(self):
        self.connect_imap.side_effect = OSError('imap down')
        resp = self.post({'to_emails': ['a@example.com'], 'body_text': 'x'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)


class TestPerformPostSendImap(SocialIntegrationTestCase):

    def test_flag_by_message_id_search_when_uid_missing(self):
        conn = self.create_email_connection()
        original = self.create_email_message(connection=conn, uid='', folder='INBOX', message_id='<m@x>')
        imap = _imap_mock()

        def uid_side_effect(command, *args):
            if command == 'SEARCH':
                return ('OK', [b'5 8'])
            return ('OK', [b''])
        imap.uid.side_effect = uid_side_effect
        with mock.patch('social_integrations.email_utils._connect_imap', return_value=imap):
            perform_post_send_imap(conn, None, None, [[original.pk, '\\Answered']])
        imap.uid.assert_any_call('SEARCH', None, 'HEADER', 'Message-ID', '"<m@x>"')
        imap.uid.assert_any_call('STORE', '8', '+FLAGS', '(\\Answered)')
        imap.append.assert_not_called()
