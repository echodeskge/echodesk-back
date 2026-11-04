"""
IMAP LIST parsing: servers may send mailbox names quoted, as bare atoms
(Dovecot/cPanel, e.g. mail.amanati.ge) or as literals. Bare atoms used to
parse as the delimiter "/", so every folder failed to select and the sync
reported success with 0 messages.
"""
from unittest import mock

from social_integrations.email_utils import (
    imap_list_flags,
    parse_imap_list_entry,
    sync_imap_messages,
)
from social_integrations.tests.conftest import SocialIntegrationTestCase


AMANATI_LIST = [
    b'(\\HasNoChildren) "/" &ENgQ3BDVEN0Q2BDhENQQ0RDY-',
    b'(\\HasNoChildren \\UnMarked \\Archive) "/" Archive',
    b'(\\HasNoChildren \\UnMarked \\Junk) "/" Junk',
    b'(\\HasNoChildren \\UnMarked \\Drafts) "/" Drafts',
    b'(\\HasNoChildren \\UnMarked \\Trash) "/" Trash',
    b'(\\HasNoChildren \\UnMarked \\Sent) "/" Sent',
    b'(\\HasNoChildren) "/" INBOX',
]


class TestParseImapListEntry:

    def test_quoted_name(self):
        assert parse_imap_list_entry(b'(\\HasNoChildren) "/" "INBOX"') == 'INBOX'

    def test_atom_name(self):
        assert parse_imap_list_entry(b'(\\HasNoChildren) "/" INBOX') == 'INBOX'

    def test_quoted_name_with_spaces_and_escapes(self):
        entry = b'(\\HasNoChildren) "/" "[Gmail]/Sent \\"Mail\\""'
        assert parse_imap_list_entry(entry) == '[Gmail]/Sent "Mail"'

    def test_nil_delimiter(self):
        assert parse_imap_list_entry(b'(\\Noinferiors) NIL INBOX') == 'INBOX'

    def test_dot_delimiter(self):
        assert parse_imap_list_entry(b'(\\HasNoChildren) "." INBOX.Sent') == 'INBOX.Sent'

    def test_literal_tuple(self):
        entry = (b'(\\HasNoChildren) "/" {11}', b'Odd "name"!')
        assert parse_imap_list_entry(entry) == 'Odd "name"!'

    def test_garbage(self):
        assert parse_imap_list_entry(None) is None
        assert parse_imap_list_entry(b'') is None
        assert parse_imap_list_entry(b'not a list line') is None

    def test_flags(self):
        assert imap_list_flags(AMANATI_LIST[5]) == {'\\hasnochildren', '\\unmarked', '\\sent'}
        assert imap_list_flags(None) == set()


class TestSyncWithAtomFolderNames(SocialIntegrationTestCase):

    def test_atom_folders_are_synced_and_special_use_respected(self):
        conn = self.create_email_connection()
        imap = mock.MagicMock()
        imap.list.return_value = ('OK', AMANATI_LIST)

        with mock.patch('social_integrations.email_utils._connect_imap', return_value=imap), \
                mock.patch('social_integrations.email_utils._sync_folder', return_value=2) as sync_folder:
            total = sync_imap_messages(conn)

        synced = [(c.args[2], c.args[3]) for c in sync_folder.call_args_list]
        # Trash and Junk are synced too (C4); Drafts is still skipped.
        assert synced == [
            ('&ENgQ3BDVEN0Q2BDhENQQ0RDY-', 'ინვოისები'),
            ('Archive', 'Archive'),
            ('Junk', 'Junk'),
            ('Trash', 'Trash'),
            ('Sent', 'Sent'),
            ('INBOX', 'INBOX'),
        ]
        assert total == 12
        conn.refresh_from_db()
        assert conn.last_sync_error == ''

    def test_localised_sent_folder_detected_by_flag(self):
        conn = self.create_email_connection()
        imap = mock.MagicMock()
        imap.list.return_value = ('OK', [
            b'(\\HasNoChildren \\Sent) "/" "Gesendete Objekte"',
            b'(\\HasNoChildren \\Trash) "/" "Papierkorb"',
            b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
        ])

        with mock.patch('social_integrations.email_utils._connect_imap', return_value=imap), \
                mock.patch('social_integrations.email_utils._sync_folder', return_value=0) as sync_folder:
            sync_imap_messages(conn)

        synced = [(c.args[2], c.args[3]) for c in sync_folder.call_args_list]
        assert synced == [('Gesendete Objekte', 'Sent'), ('Papierkorb', 'Trash')]


class TestOutgoingMessageIdDomain(SocialIntegrationTestCase):

    def test_message_id_uses_sender_domain_not_host_name(self):
        from social_integrations.email_utils import send_email_smtp
        conn = self.create_email_connection(email_address='contact@amanati.ge')
        conn.smtp_use_ssl = False
        conn.smtp_use_tls = False
        conn.save()

        with mock.patch('social_integrations.email_utils.smtplib.SMTP') as smtp_cls:
            send_email_smtp(conn, ['client@example.com'], subject='Hi', body_text='Hello')

        raw = smtp_cls.return_value.sendmail.call_args.args[2]
        message_id = next(line for line in raw.splitlines() if line.lower().startswith('message-id:'))
        assert message_id.rstrip().endswith('@amanati.ge>')
