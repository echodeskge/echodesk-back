"""
Review fixes for the Outlook-style mailbox work:

- unified mark-read / mark-unread write \\Seen to the server, and sync never
  copies stale server flags over a change still queued (flag holds);
- \\Answered / $Forwarded only ever land on the original in its OWN mailbox,
  on a uid verified by Message-ID; sync never clears is_answered;
- legacy rows keyed by an un-normalised Message-ID are adopted, not duplicated;
- Message-ID-less mail keeps one row across a move;
- per-folder conversation delete, per-mailbox thread actions / listing;
- Trash / Junk stay out of the "all folders" lists;
- stale mailbox tasks (delete then quick restore) don't undo newer changes;
- the move endpoint stores the folder the server really moved to;
- APPEND goes to the folder sync calls Sent and records its UIDVALIDITY;
- whole-host match for providers that save Sent copies themselves.

IMAP is the in-memory double (tests/fake_imap.py); nothing touches a network.
"""
import email
from unittest import mock

from django.core.cache import cache
from rest_framework import status

from social_integrations.email_utils import (
    _flag_changes,
    imap_add_flag,
    perform_post_send_imap,
    provider_saves_sent_copy,
    send_email_smtp,
)
from social_integrations.models import EmailMessage
from social_integrations.tasks import email_mailbox_operation_task
from social_integrations.tests.fake_imap import FakeFolder, FakeIMAP, raw_message
from social_integrations.tests.test_email_mailbox import (
    ACTION_URL,
    CONNECT,
    LIST_URL,
    MailboxTestCase,
    now_date,
    run_task_eagerly,
    standard_server,
)

MARK_READ_URL = '/api/social/mark-read/'
MARK_UNREAD_URL = '/api/social/mark-unread/'
THREADS_URL = '/api/social/email-messages/threads/'
MOVE_URL = '/api/social/email-messages/move/'


class _Base(MailboxTestCase):

    def run_with(self, server_or_map, fn, *args, eager=True, **kwargs):
        """Call ``fn`` with IMAP patched to ``server_or_map`` and tasks run eagerly."""
        if isinstance(server_or_map, dict):
            connect = mock.patch(CONNECT, side_effect=lambda conn: server_or_map[conn.pk])
        else:
            connect = mock.patch(CONNECT, return_value=server_or_map)
        delay = mock.patch.object(
            email_mailbox_operation_task, 'delay', side_effect=run_task_eagerly if eager else None,
        )
        with connect, delay, self.captureOnCommitCallbacks(execute=True):
            return fn(*args, **kwargs)


# --------------------------------------------------------------------------
# Unified mark read / unread -> \Seen on the server; flag holds
# --------------------------------------------------------------------------

class TestUnifiedMarkReadWritesSeen(_Base):

    def setUp(self):
        super().setUp()
        self.server = standard_server()
        self.uid = self.server.folder('INBOX').add(raw_message('<u@x>', date=now_date()))
        self.sync(self.server)
        self.msg = EmailMessage.objects.get(message_id='<u@x>')
        self.assertFalse(self.msg.is_read)

    def test_mark_conversation_read_stores_seen_and_survives_sync(self):
        resp = self.run_with(self.server, self.api_post, MARK_READ_URL,
                             {'platform': 'email', 'conversation_id': self.msg.thread_id}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertIn('\\Seen', self.server.folder('INBOX').messages[self.uid]['flags'])
        self.sync(self.server)
        self.msg.refresh_from_db()
        self.assertTrue(self.msg.is_read and self.msg.is_read_by_staff)

    def test_read_held_while_server_store_is_still_queued(self):
        # The task never runs (broker slow): the next sync still sees the
        # message unseen on the server but must not flip it back to unread.
        resp = self.run_with(self.server, self.api_post, MARK_READ_URL,
                             {'platform': 'email', 'conversation_id': self.msg.thread_id},
                             user=self.agent, eager=False)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertNotIn('\\Seen', self.server.folder('INBOX').messages[self.uid]['flags'])
        self.sync(self.server)
        self.msg.refresh_from_db()
        self.assertTrue(self.msg.is_read and self.msg.is_read_by_staff)

        # Once the hold is gone, the server is the truth again (read elsewhere / unread elsewhere).
        cache.clear()
        self.sync(self.server)
        self.msg.refresh_from_db()
        self.assertFalse(self.msg.is_read)

    def test_mark_conversation_unread_clears_seen(self):
        self.server.folder('INBOX').messages[self.uid]['flags'] = {'\\Seen'}
        self.sync(self.server)
        self.msg.refresh_from_db()
        self.assertTrue(self.msg.is_read)
        resp = self.run_with(self.server, self.api_post, MARK_UNREAD_URL,
                             {'platform': 'email', 'conversation_id': self.msg.thread_id}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertNotIn('\\Seen', self.server.folder('INBOX').messages[self.uid]['flags'])
        self.sync(self.server)
        self.msg.refresh_from_db()
        self.assertFalse(self.msg.is_read or self.msg.is_read_by_staff)

    def test_flag_changes_held_keeps_read_star_but_takes_answered(self):
        row = {'is_read': True, 'is_read_by_staff': True, 'is_starred': True, 'is_answered': False}
        self.assertEqual(_flag_changes(row, {'\\answered'}, held=True), {'is_answered': True})
        self.assertEqual(_flag_changes(dict(row, is_answered=True), set()), {
            'is_read': False, 'is_read_by_staff': False, 'read_by_staff_at': None, 'is_starred': False,
        })


# --------------------------------------------------------------------------
# \Answered / $Forwarded only in the original's own mailbox
# --------------------------------------------------------------------------

class TestAnsweredFlagTargets(_Base):

    def setUp(self):
        super().setUp()
        # Sender mailbox A (gmail: no APPEND needed) and the original's mailbox B.
        self.conn.smtp_server = 'smtp.gmail.com'
        self.conn.imap_server = 'imap.gmail.com'
        self.conn.smtp_use_ssl = False
        self.conn.smtp_use_tls = False
        self.conn.save()
        self.other = self.create_email_connection(email_address='sales@example.com')
        self.server_a = standard_server()
        self.server_b = standard_server()
        self.unrelated = self.server_a.folder('INBOX').add(raw_message('<unrelated@x>', date=now_date()))
        self.orig_uid = self.server_b.folder('INBOX').add(raw_message('<orig@x>', date=now_date()))
        self.servers = {self.conn.pk: self.server_a, self.other.pk: self.server_b}
        self.sync(self.server_b, conn=self.other)
        self.original = EmailMessage.objects.get(connection=self.other, message_id='<orig@x>')
        self.assertEqual(self.original.uid, str(self.unrelated))  # same uid number in both mailboxes
        smtp = mock.patch('social_integrations.email_utils.smtplib.SMTP')
        self.smtp = smtp.start()
        self.smtp.return_value.sendmail.return_value = {}
        self.addCleanup(smtp.stop)

    def test_reply_from_other_mailbox_flags_original_in_its_own_mailbox(self):
        self.run_with(self.servers, send_email_smtp, self.conn, ['client@example.com'],
                      subject='Re', body_text='ok', reply_to_message=self.original)
        self.assertNotIn('\\Answered', self.server_a.folder('INBOX').messages[self.unrelated]['flags'])
        self.assertIn('\\Answered', self.server_b.folder('INBOX').messages[self.orig_uid]['flags'])
        self.original.refresh_from_db()
        self.assertTrue(self.original.is_answered)

    def test_post_send_ignores_targets_of_other_connections(self):
        with mock.patch(CONNECT, return_value=self.server_a):
            perform_post_send_imap(self.conn, None, None, [[self.original.pk, '\\Answered']])
        self.assertNotIn('\\Answered', self.server_a.folder('INBOX').messages[self.unrelated]['flags'])

    def test_stale_uid_is_verified_by_message_id(self):
        server = standard_server()
        server.folder('INBOX').add(raw_message('<someone-else@x>', date=now_date()))  # uid 1
        real_uid = server.folder('INBOX').add(raw_message('<mine@x>', date=now_date()))  # uid 2
        row = self.create_email_message(connection=self.other, message_id='<mine@x>', folder='INBOX',
                                        uid='1', uid_validity='100')
        self.assertTrue(imap_add_flag(server, row, '\\Answered'))
        self.assertNotIn('\\Answered', server.folder('INBOX').messages[1]['flags'])
        self.assertIn('\\Answered', server.folder('INBOX').messages[real_uid]['flags'])


# --------------------------------------------------------------------------
# Sync identity: legacy raw Message-IDs, Message-ID-less mail
# --------------------------------------------------------------------------

class TestSyncIdentity(_Base):

    def test_legacy_folded_message_id_row_is_adopted_and_normalised(self):
        raw = raw_message('<fold@x>', date=now_date()).replace(
            b'Message-ID: <fold@x>', b'Message-ID:\r\n <fold@x>',
        )
        legacy_id = email.message_from_bytes(raw)['Message-ID']
        self.assertNotEqual(legacy_id, '<fold@x>')  # what the pre-UID sync stored
        legacy = self.create_email_message(connection=self.conn, message_id=legacy_id, folder='INBOX', uid='')
        server = standard_server()
        server.folder('INBOX').add(raw)
        self.assertEqual(self.sync(server), 0)
        self.assertEqual(EmailMessage.objects.filter(connection=self.conn).count(), 1)
        legacy.refresh_from_db()
        self.assertEqual((legacy.message_id, legacy.uid, legacy.uid_validity), ('<fold@x>', '1', '100'))

    def test_message_without_message_id_keeps_one_row_after_move(self):
        server = standard_server()
        server.folder('INBOX').add(raw_message(None, date=now_date(), subject='No id'))
        self.sync(server)
        row = EmailMessage.objects.get()
        self.run_with(server, self.api_post, ACTION_URL,
                      {'message_ids': [row.id], 'action': 'delete'}, user=self.agent)
        self.assertEqual(len(server.folder('Deleted Items').messages), 1)
        self.sync(server)
        rows = EmailMessage.objects.filter(is_deleted=False)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().folder, 'Trash')


# --------------------------------------------------------------------------
# Conversation delete per folder / per mailbox
# --------------------------------------------------------------------------

class TestPerFolderConversationDelete(_Base):

    def setUp(self):
        super().setUp()
        self.server = standard_server()
        self.server.folder('INBOX').add(raw_message('<t1@x>', date=now_date(hours=2)))
        self.server.folder('Deleted Items').add(raw_message(
            '<t2@x>', date=now_date(hours=1), in_reply_to='<t1@x>', references='<t1@x>'))
        self.sync(self.server)
        self.thread_id = EmailMessage.objects.get(message_id='<t1@x>').thread_id
        self.assertEqual(EmailMessage.objects.filter(thread_id=self.thread_id).count(), 2)

    def test_delete_from_trash_view_purges_only_trash_copies(self):
        resp = self.run_with(self.server, self.api_post, ACTION_URL,
                             {'thread_id': self.thread_id, 'action': 'delete', 'folder': 'Trash'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        inbox = EmailMessage.objects.get(message_id='<t1@x>')
        trashed = EmailMessage.objects.get(message_id='<t2@x>')
        self.assertEqual((inbox.folder, inbox.is_deleted), ('INBOX', False))
        self.assertTrue(trashed.is_deleted)
        self.assertEqual(len(self.server.folder('INBOX').messages), 1)
        self.assertEqual(len(self.server.folder('Deleted Items').messages), 0)

    def test_delete_from_inbox_view_trashes_only_inbox_copies(self):
        self.run_with(self.server, self.api_post, ACTION_URL,
                      {'thread_id': self.thread_id, 'action': 'delete', 'folder': 'INBOX'}, user=self.agent)
        self.assertEqual(
            sorted(EmailMessage.objects.filter(thread_id=self.thread_id).values_list('folder', 'is_deleted')),
            [('Trash', False), ('Trash', False)],
        )
        self.assertEqual(len(self.server.folder('Deleted Items').messages), 2)

    def test_delete_conversation_endpoint_honours_folder(self):
        staff = self.create_user(email='staff-perfolder@test.com', is_staff=True)
        url = f'/api/social/delete-conversation/?platform=email&conversation_id={self.thread_id}&folder=Trash'
        resp = self.run_with(self.server, self.api_delete, url, user=staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(EmailMessage.objects.get(message_id='<t1@x>').folder, 'INBOX')
        self.assertTrue(EmailMessage.objects.get(message_id='<t2@x>').is_deleted)


class TestTwoMailboxes(_Base):

    def setUp(self):
        super().setUp()
        self.other = self.create_email_connection(email_address='sales@example.com')
        self.a = self.create_email_message(connection=self.conn, message_id='<same@x>', thread_id='TT')
        self.b = self.create_email_message(connection=self.other, message_id='<same@x>', thread_id='TT')

    def test_thread_listing_shows_the_mail_once(self):
        rows = self.api_get(f'{LIST_URL}?thread_id=TT', user=self.agent).json()['results']
        self.assertEqual([r['id'] for r in rows], [self.a.id])
        rows = self.api_get(f'{LIST_URL}?thread_id=TT&connection_id={self.other.id}', user=self.agent).json()['results']
        self.assertEqual([r['id'] for r in rows], [self.b.id])
        threads = self.api_get(THREADS_URL, user=self.agent).json()
        thread = next(t for t in threads if t['thread_id'] == 'TT')
        self.assertEqual(thread['message_count'], 1)
        self.assertEqual(thread['unread_count'], 1)

    def test_thread_action_scoped_by_connection_id(self):
        with mock.patch.object(email_mailbox_operation_task, 'delay'), self.captureOnCommitCallbacks(execute=True):
            resp = self.api_post(ACTION_URL, {'thread_id': 'TT', 'action': 'delete', 'connection_id': self.conn.id},
                                 user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual((self.a.folder, self.b.folder), ('Trash', 'INBOX'))


# --------------------------------------------------------------------------
# Trash / Junk out of the "all folders" lists
# --------------------------------------------------------------------------

class TestAllFoldersExcludeTrashJunk(_Base):

    def setUp(self):
        super().setUp()
        self.inbox = self.create_email_message(connection=self.conn, folder='INBOX', thread_id='A', is_starred=True)
        self.trash = self.create_email_message(connection=self.conn, folder='Trash', thread_id='B', is_starred=True)
        self.junk = self.create_email_message(connection=self.conn, folder='Junk', thread_id='C')
        self.trash_in_a = self.create_email_message(connection=self.conn, folder='Trash', thread_id='A')

    def ids(self, url):
        return {r['id'] for r in self.api_get(url, user=self.agent).json()['results']}

    def test_list_and_lite(self):
        self.assertEqual(self.ids(LIST_URL), {self.inbox.id})
        self.assertEqual(self.ids(f'{LIST_URL}lite/?starred=true'), {self.inbox.id})
        self.assertEqual(self.ids(f'{LIST_URL}?folder=All'), {self.inbox.id})
        self.assertEqual(self.ids(f'{LIST_URL}?folder=Trash'), {self.trash.id, self.trash_in_a.id})
        self.assertEqual(self.ids(f'{LIST_URL}?folder=Junk'), {self.junk.id})
        # A conversation still shows every folder.
        self.assertEqual(self.ids(f'{LIST_URL}?thread_id=A'), {self.inbox.id, self.trash_in_a.id})

    def test_threads(self):
        threads = self.api_get(THREADS_URL, user=self.agent).json()
        self.assertEqual({t['thread_id'] for t in threads}, {'A'})
        self.assertEqual(threads[0]['id'], self.inbox.id)  # never the trashed copy
        threads = self.api_get(f'{THREADS_URL}?folder=Trash', user=self.agent).json()
        self.assertEqual({t['thread_id'] for t in threads}, {'A', 'B'})


# --------------------------------------------------------------------------
# Out-of-order mailbox tasks
# --------------------------------------------------------------------------

class TestStaleTasksDoNotUndoNewerChanges(_Base):

    def setUp(self):
        super().setUp()
        self.server = standard_server()
        self.uid = self.server.folder('INBOX').add(raw_message('<r@x>', date=now_date()))
        self.sync(self.server)
        self.msg = EmailMessage.objects.get()

    def queue(self, payload):
        queued = []
        with mock.patch.object(email_mailbox_operation_task, 'delay', side_effect=lambda *a: queued.append(a)), \
                self.captureOnCommitCallbacks(execute=True):
            self.api_post(ACTION_URL, payload, user=self.agent)
        return queued

    def test_restore_task_running_before_trash_task(self):
        trash_task = self.queue({'message_ids': [self.msg.id], 'action': 'delete'})
        restore_task = self.queue({'message_ids': [self.msg.id], 'action': 'restore'})
        with mock.patch(CONNECT, return_value=self.server):
            for args in restore_task + trash_task:  # worst order
                email_mailbox_operation_task.apply(args=args)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.folder, 'INBOX')
        self.assertEqual(len(self.server.folder('INBOX').messages), 1)
        self.assertEqual(len(self.server.folder('Deleted Items').messages), 0)

    def test_read_then_unread_out_of_order(self):
        read_task = self.queue({'message_ids': [self.msg.id], 'action': 'mark_read'})
        unread_task = self.queue({'message_ids': [self.msg.id], 'action': 'mark_unread'})
        with mock.patch(CONNECT, return_value=self.server):
            for args in unread_task + read_task:
                email_mailbox_operation_task.apply(args=args)
        self.assertNotIn('\\Seen', self.server.folder('INBOX').messages[self.uid]['flags'])


# --------------------------------------------------------------------------
# Move endpoint stores the folder the server really moved to
# --------------------------------------------------------------------------

class TestMoveEndpointDbFolder(_Base):

    def test_custom_deleted_folder_is_not_stored_as_trash(self):
        server = FakeIMAP([
            FakeFolder('INBOX', uidvalidity='100'),
            FakeFolder('Trash', ['\\Trash'], uidvalidity='300'),
            FakeFolder('Deleted', [], uidvalidity='800'),
        ])
        server.folder('INBOX').add(raw_message('<mv@x>', date=now_date()))
        self.sync(server)
        resp = self.run_with(server, self.api_post, MOVE_URL,
                             {'message_id': '<mv@x>', 'source_folder': 'INBOX', 'target_folder': 'Deleted'},
                             user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(len(server.folder('Deleted').messages), 1)
        row = EmailMessage.objects.get()
        self.assertEqual((row.folder, row.previous_folder), ('Deleted', ''))


# --------------------------------------------------------------------------
# Sent APPEND target + provider detection
# --------------------------------------------------------------------------

class TestSentAppend(MailboxTestCase):

    def test_append_target_and_uidvalidity_recorded(self):
        sent_row = self.create_email_message(connection=self.conn, folder='Sent', message_id='<s@x>')
        imap = mock.MagicMock()
        imap.list.return_value = ('OK', [
            b'(\\HasNoChildren) "/" INBOX',
            b'(\\HasNoChildren) "/" "Sent Messages"',
            b'(\\HasNoChildren) "/" "Sent Items"',
        ])
        imap.append.return_value = ('OK', [b'[APPENDUID 555 17] Append completed.'])
        with mock.patch(CONNECT, return_value=imap):
            uid = perform_post_send_imap(self.conn, b'Subject: x\r\n\r\nbody', sent_row.pk, [])
        self.assertEqual(uid, '17')
        # The same folder sync stores as "Sent" (first by LIST order).
        self.assertEqual(imap.append.call_args.args[0], '"Sent Messages"')
        sent_row.refresh_from_db()
        self.assertEqual((sent_row.uid, sent_row.uid_validity), ('17', '555'))

    def test_provider_saves_sent_copy_whole_host(self):
        def conn(smtp, imap=''):
            return mock.Mock(smtp_server=smtp, imap_server=imap)
        self.assertTrue(provider_saves_sent_copy(conn('smtp.office365.com')))
        self.assertTrue(provider_saves_sent_copy(conn('mail.example.ge', 'outlook.office365.com')))
        self.assertTrue(provider_saves_sent_copy(conn('smtp.gmail.com')))
        self.assertTrue(provider_saves_sent_copy(conn('gmail.com')))
        self.assertFalse(provider_saves_sent_copy(conn('mail.olive.com')))
        self.assertFalse(provider_saves_sent_copy(conn('alive.com.ge', 'myoutlook.com')))
        self.assertFalse(provider_saves_sent_copy(conn('', '')))
