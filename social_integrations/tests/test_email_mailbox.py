"""
Outlook-style mailbox behaviour (be-mailbox slice):

- C3 delete -> Trash, delete in Trash -> permanent (UID EXPUNGE of our uid
  only), restore, \\Seen / \\Flagged write-back, all via a Celery task.
- C4 sync: canonical special folders incl. Trash/Junk, per-connection dedupe,
  deterministic ids, flag refresh, server deletion/move reconciliation,
  UIDVALIDITY safety, sync lock, auth-only auto-disable.
- C5 folder roles/counts, C6 thread listing, C7 move endpoint.

IMAP is an in-memory double (tests/fake_imap.py); nothing touches a network.
"""
from email.utils import format_datetime
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status

from social_integrations import email_mailbox
from social_integrations.email_utils import (
    _sync_lock_key,
    move_email_to_folder,
    sync_imap_messages,
)
from social_integrations.models import EmailConnectionUserAssignment, EmailMessage
from social_integrations.tasks import email_mailbox_operation_task
from social_integrations.tests.conftest import SocialIntegrationTestCase
from social_integrations.tests.fake_imap import FakeFolder, FakeIMAP, raw_message

ACTION_URL = '/api/social/email/action/'
LIST_URL = '/api/social/email-messages/'
FOLDERS_URL = '/api/social/email/folders/'
CONNECT = 'social_integrations.email_utils._connect_imap'
GEORGIAN = 'ინვოისები'
GEORGIAN_RAW = '&ENgQ3BDVEN0Q2BDhENQQ0RDY-'


def now_date(**delta):
    return format_datetime(timezone.now() - timezone.timedelta(**delta))


def standard_server(inbox_uidvalidity='100'):
    return FakeIMAP([
        FakeFolder('INBOX', uidvalidity=inbox_uidvalidity),
        FakeFolder('Sent Items', ['\\Sent'], uidvalidity='200'),
        FakeFolder('Deleted Items', ['\\Trash'], uidvalidity='300'),
        FakeFolder('Spam', ['\\Junk'], uidvalidity='400'),
        FakeFolder('Drafts', ['\\Drafts'], uidvalidity='500'),
        FakeFolder('Archive', [], uidvalidity='600'),
        FakeFolder(GEORGIAN_RAW, [], uidvalidity='700'),
    ])


def run_task_eagerly(*args):
    """Stand-in for email_mailbox_operation_task.delay: run it in-process."""
    return email_mailbox_operation_task.apply(args=args)


class MailboxTestCase(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.conn = self.create_email_connection(email_address='info@example.com')
        self.agent = self.create_user(email='mailbox-agent@test.com')

    def sync(self, server, conn=None):
        with mock.patch(CONNECT, return_value=server):
            return sync_imap_messages(conn or self.conn)

    def post_action(self, payload, server, user=None):
        """POST email/action and run the queued IMAP task against ``server``."""
        with mock.patch(CONNECT, return_value=server), \
                mock.patch.object(email_mailbox_operation_task, 'delay', side_effect=run_task_eagerly), \
                self.captureOnCommitCallbacks(execute=True):
            return self.api_post(ACTION_URL, payload, user=user or self.agent)


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

class TestSyncFolders(MailboxTestCase):

    def test_special_folders_are_canonical_and_trash_junk_synced(self):
        server = standard_server()
        server.folder('INBOX').add(raw_message('<in@x>', date=now_date(hours=1),
                                               reply_to='Billing <billing@client.com>'), {'\\Seen'})
        server.folder('Sent Items').add(raw_message('<sent@x>', sender='info@example.com', date=now_date()))
        server.folder('Deleted Items').add(raw_message('<trash@x>', date=now_date()))
        server.folder('Spam').add(raw_message('<junk@x>', date=now_date()))
        server.folder('Drafts').add(raw_message('<draft@x>', date=now_date()))
        server.folder('Archive').add(raw_message('<arch@x>', date=now_date()))
        server.folder(GEORGIAN_RAW).add(raw_message('<geo@x>', date=now_date()))

        self.assertEqual(self.sync(server), 6)

        folders = dict(EmailMessage.objects.values_list('message_id', 'folder'))
        self.assertEqual(folders, {
            '<in@x>': 'INBOX', '<sent@x>': 'Sent', '<trash@x>': 'Trash',
            '<junk@x>': 'Junk', '<arch@x>': 'Archive', '<geo@x>': GEORGIAN,
        })
        inbox = EmailMessage.objects.get(message_id='<in@x>')
        self.assertEqual((inbox.uid, inbox.uid_validity), ('1', '100'))
        self.assertTrue(inbox.is_read)
        self.assertTrue(inbox.is_read_by_staff)
        self.assertEqual(inbox.reply_to, 'billing@client.com')
        self.assertTrue(EmailMessage.objects.get(message_id='<sent@x>').is_from_business)
        # Bodies are downloaded with BODY.PEEK (never marks mail read) and
        # every SELECT during sync is read-only.
        self.assertTrue(all(c[2] for c in server.commands if c[0] == 'SELECT'))

    def test_resync_downloads_nothing_new(self):
        server = standard_server()
        server.folder('INBOX').add(raw_message('<in@x>', date=now_date()))
        self.sync(server)
        server.commands.clear()
        self.assertEqual(self.sync(server), 0)
        self.assertEqual(EmailMessage.objects.count(), 1)
        body_fetches = [c for c in server.commands if c[:2] == ('UID', 'FETCH') and 'BODY.PEEK[]' in c[3]]
        self.assertEqual(body_fetches, [])

    def test_missing_message_id_is_deterministic(self):
        server = standard_server()
        server.folder('INBOX').add(raw_message(None, date=now_date()))
        self.assertEqual(self.sync(server), 1)
        self.assertEqual(self.sync(server), 0)
        row = EmailMessage.objects.get()
        self.assertTrue(row.message_id.startswith('<echodesk-'))
        self.assertTrue(row.message_id.endswith('@sync.local>'))

    def test_message_id_and_threading_headers_are_stripped(self):
        server = standard_server()
        raw = raw_message('<x@y>', date=now_date(), in_reply_to='<parent@y>').replace(
            b'Message-ID: <x@y>', b'Message-ID:  <x@y>  ',
        ).replace(b'In-Reply-To: <parent@y>', b'In-Reply-To:   <parent@y> ')
        server.folder('INBOX').add(raw)
        self.sync(server)
        row = EmailMessage.objects.get()
        self.assertEqual(row.message_id, '<x@y>')
        self.assertEqual(row.in_reply_to, '<parent@y>')

    def test_dedupe_is_per_connection(self):
        other = self.create_email_connection(email_address='sales@example.com')
        server_a, server_b = standard_server(), standard_server()
        server_a.folder('Sent Items').add(raw_message('<shared@x>', sender='info@example.com', date=now_date()))
        server_b.folder('INBOX').add(raw_message('<shared@x>', sender='info@example.com', date=now_date()))
        self.sync(server_a)
        self.sync(server_b, conn=other)
        rows = EmailMessage.objects.filter(message_id='<shared@x>')
        self.assertEqual(
            sorted(rows.values_list('connection__email_address', 'folder')),
            [('info@example.com', 'Sent'), ('sales@example.com', 'INBOX')],
        )

    def test_flags_refresh_on_existing_rows(self):
        server = standard_server()
        uid = server.folder('INBOX').add(raw_message('<f@x>', date=now_date()))
        self.sync(server)
        row = EmailMessage.objects.get()
        self.assertFalse(row.is_read)

        server.folder('INBOX').messages[uid]['flags'] = {'\\Seen', '\\Flagged', '\\Answered'}
        self.sync(server)
        row.refresh_from_db()
        self.assertTrue(row.is_read and row.is_starred and row.is_answered and row.is_read_by_staff)

        server.folder('INBOX').messages[uid]['flags'] = set()
        self.sync(server)
        row.refresh_from_db()
        self.assertFalse(row.is_read or row.is_starred or row.is_read_by_staff)
        # \Answered is never cleared by sync (EchoDesk sets it on send).
        self.assertTrue(row.is_answered)

    def test_message_in_two_folders_does_not_flip(self):
        server = standard_server()
        server.folder('INBOX').add(raw_message('<self@x>', date=now_date()))
        server.folder('Sent Items').add(raw_message('<self@x>', date=now_date()))
        for _ in range(3):
            self.sync(server)
            row = EmailMessage.objects.get()
            self.assertEqual(row.folder, 'INBOX')
            self.assertFalse(row.is_deleted)


class TestSyncReconciliation(MailboxTestCase):

    def test_deleted_on_server_is_marked_deleted(self):
        server = standard_server()
        keep = server.folder('INBOX').add(raw_message('<keep@x>', date=now_date()))
        gone = server.folder('INBOX').add(raw_message('<gone@x>', date=now_date()))
        self.sync(server)
        del server.folder('INBOX').messages[gone]
        self.sync(server)
        self.assertTrue(EmailMessage.objects.get(message_id='<gone@x>').is_deleted)
        self.assertFalse(EmailMessage.objects.get(message_id='<keep@x>').is_deleted)
        self.assertIn(keep, server.folder('INBOX').messages)

    def test_moved_on_server_follows_the_message(self):
        server = standard_server()
        uid = server.folder('INBOX').add(raw_message('<mv@x>', date=now_date()))
        self.sync(server)
        msg = server.folder('INBOX').messages.pop(uid)
        new_uid = server.folder('Archive').add(msg['raw'])
        self.sync(server)
        row = EmailMessage.objects.get()
        self.assertFalse(row.is_deleted)
        self.assertEqual((row.folder, row.uid, row.uid_validity), ('Archive', str(new_uid), '600'))

    def test_moved_to_trash_in_outlook(self):
        server = standard_server()
        uid = server.folder('INBOX').add(raw_message('<t@x>', date=now_date()))
        self.sync(server)
        msg = server.folder('INBOX').messages.pop(uid)
        server.folder('Deleted Items').add(msg['raw'])
        self.sync(server)
        row = EmailMessage.objects.get()
        self.assertEqual(row.folder, 'Trash')
        self.assertFalse(row.is_deleted)

    def test_uidvalidity_change_rekeys_instead_of_deleting(self):
        server = standard_server()
        server.folder('INBOX').add(raw_message('<a@x>', date=now_date()))
        server.folder('INBOX').add(raw_message('<b@x>', date=now_date()))
        self.sync(server)
        # Server rebuilt the mailbox: new UIDVALIDITY, new UIDs, and <b@x>
        # is not even in the window we fetch any more.
        folder = server.folder('INBOX')
        old = list(folder.messages.values())
        folder.messages.clear()
        folder.uidvalidity = '101'
        folder.add(old[0]['raw'], uid=50)
        self.sync(server)
        a = EmailMessage.objects.get(message_id='<a@x>')
        b = EmailMessage.objects.get(message_id='<b@x>')
        self.assertEqual((a.uid, a.uid_validity, a.is_deleted), ('50', '101', False))
        self.assertFalse(b.is_deleted)  # stale uid: never mass-delete

    def test_failed_folder_never_mass_deletes(self):
        server = standard_server()
        server.folder('INBOX').add(raw_message('<a@x>', date=now_date()))
        self.sync(server)
        server.folder('INBOX').messages.clear()
        with mock.patch.object(FakeIMAP, 'uid', side_effect=lambda *a: ('NO', [b'busy'])):
            self.sync(server)
        self.assertFalse(EmailMessage.objects.get().is_deleted)

    def test_uid_list_disagreeing_with_exists_skips_reconciliation(self):
        server = standard_server()
        server.folder('INBOX').add(raw_message('<a@x>', date=now_date()))
        self.sync(server)
        real_select = FakeIMAP.select

        def select_claiming_messages(self_, mailbox='INBOX', readonly=False):
            typ, data = real_select(self_, mailbox, readonly)
            return typ, [b'5']
        server.folder('INBOX').messages.clear()
        with mock.patch.object(FakeIMAP, 'select', select_claiming_messages):
            self.sync(server)
        self.assertFalse(EmailMessage.objects.get().is_deleted)

    def test_legacy_rows_without_uidvalidity(self):
        """Rows synced before uid_validity existed: reconciled only when the
        stored uids provably still match the server."""
        server = standard_server()
        a = server.folder('INBOX').add(raw_message('<a@x>', date=now_date()))
        self.create_email_message(connection=self.conn, message_id='<a@x>', folder='INBOX', uid=str(a))
        self.create_email_message(connection=self.conn, message_id='<gone@x>', folder='INBOX', uid='77')
        self.sync(server)
        self.assertTrue(EmailMessage.objects.get(message_id='<gone@x>').is_deleted)
        self.assertEqual(EmailMessage.objects.get(message_id='<a@x>').uid_validity, '100')

    def test_legacy_rows_with_mismatching_uids_are_left_alone(self):
        server = standard_server()
        a = server.folder('INBOX').add(raw_message('<a@x>', date=now_date()))
        self.create_email_message(connection=self.conn, message_id='<a@x>', folder='INBOX', uid=str(a + 10))
        self.create_email_message(connection=self.conn, message_id='<gone@x>', folder='INBOX', uid='77')
        self.sync(server)
        self.assertFalse(EmailMessage.objects.get(message_id='<gone@x>').is_deleted)

    def test_old_non_canonical_folder_name_is_adopted(self):
        server = standard_server()
        server.folder('Archive').add(raw_message('<old@x>', date=now_date()))
        self.create_email_message(connection=self.conn, message_id='<old@x>', folder='INBOX.Archive')
        self.sync(server)
        self.assertEqual(EmailMessage.objects.get().folder, 'Archive')


class TestSyncLockAndErrors(MailboxTestCase):

    def test_lock_skips_overlapping_run(self):
        cache.add(_sync_lock_key(self.conn), 'busy', 60)
        with mock.patch(CONNECT) as connect:
            self.assertEqual(sync_imap_messages(self.conn), 0)
        connect.assert_not_called()

    def test_lock_released_after_run(self):
        self.sync(standard_server())
        self.assertIsNone(cache.get(_sync_lock_key(self.conn)))

    def test_non_auth_error_does_not_disable(self):
        import imaplib
        with mock.patch(CONNECT, side_effect=imaplib.IMAP4.error('LIST: BAD command')):
            sync_imap_messages(self.conn)
        self.conn.refresh_from_db()
        self.assertTrue(self.conn.is_active)
        self.assertEqual(self.conn.sync_failure_count, 1)
        self.assertIn('BAD', self.conn.last_sync_error)

    def test_auth_error_disables(self):
        import imaplib
        with mock.patch(CONNECT, side_effect=imaplib.IMAP4.error(
                b'[AUTHENTICATIONFAILED] Authentication failed.')):
            sync_imap_messages(self.conn)
        self.conn.refresh_from_db()
        self.assertFalse(self.conn.is_active)
        self.assertIsNotNone(self.conn.auto_disabled_at)


# --------------------------------------------------------------------------
# email/action (C3)
# --------------------------------------------------------------------------

class TestEmailActionOnServer(MailboxTestCase):

    def setUp(self):
        super().setUp()
        self.server = standard_server()
        self.uid = self.server.folder('INBOX').add(raw_message('<d@x>', date=now_date()))
        # Another client's message already flagged \Deleted must survive.
        self.bystander = self.server.folder('Deleted Items').add(
            raw_message('<other@x>', date=now_date()), {'\\Deleted'},
        )
        self.sync(self.server)
        self.msg = EmailMessage.objects.get(message_id='<d@x>')

    def test_delete_moves_to_trash_then_purges(self):
        resp = self.post_action({'message_ids': [self.msg.id], 'action': 'delete'}, self.server)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertEqual((self.msg.folder, self.msg.previous_folder, self.msg.is_deleted), ('Trash', 'INBOX', False))
        self.assertNotIn(self.uid, self.server.folder('INBOX').messages)
        trash = self.server.folder('Deleted Items')
        self.assertEqual(self.msg.uid_validity, '300')
        self.assertIn(int(self.msg.uid), trash.messages)

        resp = self.post_action({'message_ids': [self.msg.id], 'action': 'delete'}, self.server)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertTrue(self.msg.is_deleted)
        self.assertEqual(self.msg.deleted_by, self.agent)
        self.assertNotIn(int(self.msg.uid), trash.messages)
        self.assertIn(self.bystander, trash.messages)  # UID EXPUNGE only touched ours
        self.assertEqual(self.server.bare_expunges, 0)

    def test_restore_moves_back_to_previous_folder(self):
        self.post_action({'message_ids': [self.msg.id], 'action': 'delete'}, self.server)
        resp = self.post_action({'message_ids': [self.msg.id], 'action': 'restore'}, self.server)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertEqual((self.msg.folder, self.msg.previous_folder), ('INBOX', ''))
        inbox = self.server.folder('INBOX')
        self.assertEqual(len(inbox.messages), 1)
        self.assertIn(int(self.msg.uid), inbox.messages)

    def test_restore_of_custom_folder(self):
        self.msg.folder = GEORGIAN
        self.msg.save()
        geo = self.server.folder(GEORGIAN_RAW)
        geo.messages[5] = self.server.folder('INBOX').messages.pop(self.uid)
        self.msg.uid, self.msg.uid_validity = '5', '700'
        self.msg.save()
        self.post_action({'message_ids': [self.msg.id], 'action': 'delete'}, self.server)
        self.post_action({'message_ids': [self.msg.id], 'action': 'restore'}, self.server)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.folder, GEORGIAN)
        self.assertEqual(len(geo.messages), 1)

    def test_flags_written_back(self):
        self.post_action({'message_ids': [self.msg.id], 'action': 'mark_read'}, self.server)
        self.post_action({'message_ids': [self.msg.id], 'action': 'star'}, self.server)
        flags = self.server.folder('INBOX').messages[self.uid]['flags']
        self.assertEqual(flags, {'\\Seen', '\\Flagged'})
        self.post_action({'message_ids': [self.msg.id], 'action': 'mark_unread'}, self.server)
        self.post_action({'message_ids': [self.msg.id], 'action': 'unstar'}, self.server)
        self.assertEqual(self.server.folder('INBOX').messages[self.uid]['flags'], set())
        self.msg.refresh_from_db()
        self.assertFalse(self.msg.is_read or self.msg.is_starred)

    def test_trash_created_when_missing(self):
        server = FakeIMAP([FakeFolder('INBOX')])
        server.folder('INBOX').add(raw_message('<d@x>', date=now_date()), uid=int(self.msg.uid))
        self.post_action({'message_ids': [self.msg.id], 'action': 'delete'}, server)
        self.assertIn(('CREATE', 'Trash'), server.commands)
        self.assertEqual(len(server.folder('Trash').messages), 1)
        self.assertEqual(len(server.folder('INBOX').messages), 0)

    def test_without_move_uses_copy_store_uid_expunge(self):
        server = standard_server()
        server.caps = ['IMAP4REV1', 'UIDPLUS']
        server.folder('INBOX').add(raw_message('<d@x>', date=now_date()), uid=int(self.msg.uid))
        server.folder('INBOX').add(raw_message('<keep@x>', date=now_date()), {'\\Deleted'}, uid=99)
        self.post_action({'message_ids': [self.msg.id], 'action': 'delete'}, server)
        uid_cmds = [c[1] for c in server.commands if c[0] == 'UID']
        self.assertIn('COPY', uid_cmds)
        self.assertIn('EXPUNGE', uid_cmds)
        self.assertNotIn('MOVE', uid_cmds)
        self.assertEqual(list(server.folder('INBOX').messages), [99])
        self.assertEqual(server.bare_expunges, 0)

    def test_without_uidplus_never_expunges(self):
        server = standard_server()
        server.caps = ['IMAP4REV1']
        server.folder('INBOX').add(raw_message('<d@x>', date=now_date()), uid=int(self.msg.uid))
        self.post_action({'message_ids': [self.msg.id], 'action': 'delete'}, server)
        self.assertNotIn('EXPUNGE', [c[1] for c in server.commands if c[0] == 'UID'])
        self.assertEqual(server.bare_expunges, 0)
        self.assertIn('\\Deleted', server.folder('INBOX').messages[int(self.msg.uid)]['flags'])

    def test_stale_uid_is_resolved_by_message_id(self):
        # A stored uid that now points at another message must not be used.
        self.server.folder('INBOX').messages[self.uid]['raw'] = raw_message('<someone-else@x>')
        real = self.server.folder('INBOX').add(raw_message('<d@x>', date=now_date()))
        self.post_action({'message_ids': [self.msg.id], 'action': 'delete'}, self.server)
        self.assertIn(self.uid, self.server.folder('INBOX').messages)
        self.assertNotIn(real, self.server.folder('INBOX').messages)

    def test_server_failure_never_fails_request(self):
        with mock.patch(CONNECT, side_effect=OSError('down')), \
                mock.patch.object(email_mailbox_operation_task, 'delay', side_effect=RuntimeError('no broker')), \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api_post(ACTION_URL, {'message_ids': [self.msg.id], 'action': 'delete'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.folder, 'Trash')

    def test_move_action_moves_on_server(self):
        resp = self.post_action({'message_ids': [self.msg.id], 'action': 'move', 'folder': GEORGIAN}, self.server)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.folder, GEORGIAN)
        self.assertEqual(len(self.server.folder(GEORGIAN_RAW).messages), 1)
        self.assertIn(('UID', 'MOVE', str(self.uid), f'"{GEORGIAN_RAW}"'), self.server.commands)

    def test_view_only_user_cannot_delete(self):
        with mock.patch('social_integrations.permissions.CanSendSocialMessages.has_permission', return_value=False):
            resp = self.api_post(ACTION_URL, {'message_ids': [self.msg.id], 'action': 'delete'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.folder, 'INBOX')

    def test_task_is_queued_with_schema_and_snapshot(self):
        with mock.patch.object(email_mailbox_operation_task, 'delay') as delay, \
                self.captureOnCommitCallbacks(execute=True):
            self.api_post(ACTION_URL, {'message_ids': [self.msg.id], 'action': 'delete'}, user=self.agent)
        schema, conn_id, operation, items, options = delay.call_args.args
        self.assertEqual((schema, conn_id, operation), ('test', self.conn.id, 'move'))
        self.assertEqual(items[0]['folder'], 'INBOX')
        self.assertEqual(items[0]['uid'], str(self.uid))
        self.assertEqual(options, {'target_folder': 'Trash'})


class TestConversationDelete(MailboxTestCase):

    def test_moves_thread_to_trash_with_access_check(self):
        staff = self.create_user(email='staff-mailbox@test.com', is_staff=True)
        server = standard_server()
        server.folder('INBOX').add(raw_message('<c1@x>', date=now_date()))
        server.folder('Sent Items').add(raw_message('<c2@x>', sender='info@example.com', date=now_date(),
                                                    in_reply_to='<c1@x>', references='<c1@x>'))
        self.sync(server)
        thread_id = EmailMessage.objects.get(message_id='<c1@x>').thread_id
        self.assertEqual(EmailMessage.objects.filter(thread_id=thread_id).count(), 2)
        # A different mailbox the staff user isn't... staff sees everything,
        # so restrict via a non-staff assignee path below.
        url = f'/api/social/delete-conversation/?platform=email&conversation_id={thread_id}'
        with mock.patch(CONNECT, return_value=server), \
                mock.patch.object(email_mailbox_operation_task, 'delay', side_effect=run_task_eagerly), \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api_delete(url, user=staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(
            sorted(EmailMessage.objects.filter(thread_id=thread_id).values_list('folder', 'previous_folder')),
            [('Trash', 'INBOX'), ('Trash', 'Sent')],
        )
        self.assertEqual(len(server.folder('Deleted Items').messages), 2)
        self.assertEqual(len(server.folder('INBOX').messages), 0)
        self.assertEqual(server.bare_expunges, 0)

    def test_restricted_mailbox_untouched(self):
        hidden = self.create_email_connection(email_address='hidden@example.com')
        owner = self.create_user(email='hidden-owner@test.com')
        EmailConnectionUserAssignment.objects.create(connection=hidden, user=owner, assigned_by=owner)
        staff_without_access = self.create_user(email='staffish@test.com', is_staff=True)
        visible = self.create_email_message(connection=self.conn, thread_id='shared-thread')
        secret = self.create_email_message(connection=hidden, thread_id='shared-thread')
        with mock.patch('social_integrations.views._email_connection_access', return_value={self.conn.id}), \
                mock.patch.object(email_mailbox_operation_task, 'delay'), \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api_delete(
                '/api/social/delete-conversation/?platform=email&conversation_id=shared-thread',
                user=staff_without_access,
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        visible.refresh_from_db()
        secret.refresh_from_db()
        self.assertEqual(visible.folder, 'Trash')
        self.assertEqual((secret.folder, secret.is_deleted), ('INBOX', False))


# --------------------------------------------------------------------------
# Move endpoint (C7)
# --------------------------------------------------------------------------

class TestMoveEndpoint(MailboxTestCase):

    def setUp(self):
        super().setUp()
        self.server = standard_server()
        self.uid = self.server.folder('INBOX').add(raw_message('<mv@x>', date=now_date()))
        self.server.folder('INBOX').add(raw_message('<other@x>', date=now_date()), {'\\Deleted'})
        self.sync(self.server)
        self.msg = EmailMessage.objects.get(message_id='<mv@x>')

    def test_move_to_georgian_folder(self):
        with mock.patch(CONNECT, return_value=self.server):
            ok = move_email_to_folder(self.conn, '<mv@x>', 'INBOX', GEORGIAN)
        self.assertTrue(ok)
        self.assertIn(('UID', 'MOVE', str(self.uid), f'"{GEORGIAN_RAW}"'), self.server.commands)
        self.assertEqual(len(self.server.folder(GEORGIAN_RAW).messages), 1)
        self.msg.refresh_from_db()
        self.assertEqual((self.msg.uid, self.msg.uid_validity), ('1', '700'))

    def test_move_with_space_and_copy_fallback(self):
        self.server.caps = ['IMAP4REV1', 'UIDPLUS']
        with mock.patch(CONNECT, return_value=self.server):
            ok = move_email_to_folder(self.conn, '<mv@x>', 'INBOX', 'Sent Items')
        self.assertTrue(ok)
        self.assertEqual(len(self.server.folder('Sent Items').messages), 1)
        # Only our uid was expunged; the other \Deleted message survives.
        self.assertEqual(len(self.server.folder('INBOX').messages), 1)
        self.assertEqual(self.server.bare_expunges, 0)

    def test_endpoint_move_to_trash_sets_previous_folder(self):
        with mock.patch(CONNECT, return_value=self.server):
            resp = self.api_post(f'{LIST_URL}move/', {
                'message_id': '<mv@x>', 'source_folder': 'INBOX', 'target_folder': 'Deleted Items',
            }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertEqual((self.msg.folder, self.msg.previous_folder), ('Trash', 'INBOX'))
        self.assertEqual(len(self.server.folder('Deleted Items').messages), 1)

    def test_endpoint_connection_id_disambiguates(self):
        other = self.create_email_connection(email_address='sales@example.com')
        twin = self.create_email_message(connection=other, message_id='<mv@x>', folder='INBOX')
        with mock.patch('social_integrations.email_utils.move_email_to_folder', return_value=True) as move:
            resp = self.api_post(f'{LIST_URL}move/', {
                'message_id': '<mv@x>', 'source_folder': 'INBOX', 'target_folder': 'Archive',
                'connection_id': other.id,
            }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(move.call_args.args[0].id, other.id)
        twin.refresh_from_db()
        self.msg.refresh_from_db()
        self.assertEqual((twin.folder, self.msg.folder), ('Archive', 'INBOX'))


# --------------------------------------------------------------------------
# Folders (C5) and thread listing (C6)
# --------------------------------------------------------------------------

class TestFoldersAndThreads(MailboxTestCase):

    def test_folder_details_roles_and_counts(self):
        server = standard_server()
        server.folder('INBOX').add(raw_message('<1@x>', date=now_date()))
        server.folder('INBOX').add(raw_message('<2@x>', date=now_date()), {'\\Seen'})
        server.folder('Deleted Items').add(raw_message('<3@x>', date=now_date()))
        self.sync(server)
        EmailMessage.objects.filter(message_id='<1@x>').update()  # stays unread
        with mock.patch('social_integrations.email_utils.imaplib.IMAP4_SSL') as ssl_cls:
            ssl_cls.return_value.list.side_effect = lambda *a: server.list()
            resp = self.api_get(f'{FOLDERS_URL}?connection_id={self.conn.id}', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertIn('Sent Items', data['folders'])  # legacy shape unchanged
        details = {d['name']: d for d in data['folder_details']}
        self.assertEqual(details['INBOX']['role'], 'inbox')
        self.assertEqual((details['INBOX']['total_count'], details['INBOX']['unread_count']), (2, 1))
        self.assertEqual(details['Trash']['role'], 'trash')
        self.assertEqual(details['Trash']['display_name'], 'Deleted Items')
        self.assertEqual(details['Trash']['total_count'], 1)
        self.assertEqual(details['Sent']['role'], 'sent')
        self.assertEqual(details['Junk']['role'], 'junk')
        self.assertEqual(details['Drafts']['role'], 'drafts')
        self.assertEqual(details[GEORGIAN]['role'], 'custom')

    def test_folder_details_all_connections_scoped(self):
        self.create_email_message(connection=self.conn, folder='INBOX')
        with mock.patch('social_integrations.email_utils.get_imap_folders', return_value={
            'success': True, 'folders': ['INBOX'],
            'details': [{'name': 'INBOX', 'display_name': 'INBOX', 'raw_name': 'INBOX', 'role': 'inbox'}],
        }):
            resp = self.api_get(FOLDERS_URL, user=self.agent)
        conn = next(c for c in resp.json()['connections'] if c['connection_id'] == self.conn.id)
        self.assertEqual(conn['folder_details'][0]['total_count'], 1)
        self.assertEqual(conn['folder_details'][0]['unread_count'], 1)

    def test_legacy_folders_gain_role_and_counts(self):
        self.create_email_message(connection=self.conn, folder='Sent', is_read=True)
        with mock.patch('social_integrations.email_utils.get_available_folders', return_value=[
            {'name': 'Sent Items', 'display_name': 'Sent Items', 'db_name': 'Sent', 'raw_name': 'Sent Items', 'role': 'sent'},
        ]):
            resp = self.api_get(f'{LIST_URL}folders/', user=self.agent)
        row = resp.json()[0]
        self.assertEqual((row['role'], row['total_count'], row['unread_count']), ('sent', 1, 0))

    def test_thread_listing_across_folders(self):
        now = timezone.now()
        first = self.create_email_message(connection=self.conn, thread_id='T', folder='INBOX',
                                          timestamp=now - timezone.timedelta(hours=2))
        reply = self.create_email_message(connection=self.conn, thread_id='T', folder='Sent',
                                          is_from_business=True, reply_to='r@example.com',
                                          timestamp=now - timezone.timedelta(hours=1))
        self.create_email_message(connection=self.conn, thread_id='T', folder='INBOX', is_deleted=True)
        self.create_email_message(connection=self.conn, thread_id='other')
        resp = self.api_get(f'{LIST_URL}?thread_id=T&ordering=timestamp', user=self.agent)
        rows = resp.json()['results']
        self.assertEqual([r['id'] for r in rows], [first.id, reply.id])
        self.assertEqual(rows[1]['folder'], 'Sent')
        self.assertEqual(rows[1]['reply_to'], 'r@example.com')
        for key in ('previous_folder', 'uid', 'folder', 'reply_to'):
            self.assertIn(key, rows[0])
        # Default order is unchanged (newest first) for existing clients.
        resp = self.api_get(f'{LIST_URL}?thread_id=T', user=self.agent)
        self.assertEqual([r['id'] for r in resp.json()['results']], [reply.id, first.id])


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

class TestMessageIdUniqueness(MailboxTestCase):

    def test_unique_per_connection(self):
        other = self.create_email_connection(email_address='second@example.com')
        self.create_email_message(connection=self.conn, message_id='<same@x>')
        self.create_email_message(connection=other, message_id='<same@x>')
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create_email_message(connection=self.conn, message_id='<same@x>')

    def test_new_fields_default_blank(self):
        msg = self.create_email_message(connection=self.conn)
        self.assertEqual((msg.previous_folder, msg.uid_validity), ('', ''))


class TestHelpers(SocialIntegrationTestCase):

    def test_parse_fetch_response_with_trailing_flags(self):
        data = [
            (b'1 (UID 7 BODY[HEADER.FIELDS (MESSAGE-ID)] {20}', b'Message-ID: <a@b>\r\n\r\n'),
            b' FLAGS (\\Seen \\Flagged))',
            b'2 (UID 9 FLAGS ())',
        ]
        parsed = email_mailbox.parse_fetch_response(data)
        self.assertEqual([p['uid'] for p in parsed], ['7', '9'])
        self.assertEqual(parsed[0]['flags'], {'\\seen', '\\flagged'})
        self.assertEqual(parsed[1]['flags'], set())

    def test_parse_copyuid(self):
        self.assertEqual(
            email_mailbox.parse_copyuid(b'38505 304,319:320 3956:3958'),
            ('38505', {'304': '3956', '319': '3957', '320': '3958'}),
        )

    def test_roles(self):
        self.assertEqual(email_mailbox.folder_role('Papierkorb', {'\\trash'}), 'trash')
        self.assertEqual(email_mailbox.folder_role('INBOX.Sent'), 'sent')
        self.assertEqual(email_mailbox.folder_role('Junk E-mail'), 'junk')
        self.assertEqual(email_mailbox.folder_role(GEORGIAN), 'custom')
        self.assertEqual(email_mailbox.canonical_db_folder('Deleted Items'), 'Trash')
        self.assertEqual(email_mailbox.canonical_db_folder(GEORGIAN), GEORGIAN)

    def test_only_one_folder_per_role(self):
        entries = email_mailbox.mailboxes_from_list_data([
            b'(\\HasNoChildren) "/" "Trash"',
            b'(\\HasNoChildren \\Trash) "/" "Deleted Items"',
        ])
        self.assertEqual([(e['raw_name'], e['db_name'], e['role']) for e in entries],
                         [('Deleted Items', 'Trash', 'trash')])
        entries = email_mailbox.mailboxes_from_list_data([
            b'(\\HasNoChildren) "/" "Sent Items"',
            b'(\\HasNoChildren) "/" "Sent Messages"',
        ])
        self.assertEqual([(e['name'], e['db_name'], e['role']) for e in entries],
                         [('Sent Items', 'Sent', 'sent'), ('Sent Messages', 'Sent Messages', 'custom')])
