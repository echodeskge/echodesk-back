"""
Tests for Email-related views.
"""
from unittest.mock import patch, MagicMock
from django.utils import timezone
from rest_framework import status
from social_integrations.tests.conftest import SocialIntegrationTestCase
from social_integrations.models import (
    EmailConnection, EmailMessage, EmailDraft,
    EmailConnectionUserAssignment, EmailSignature,
)


class TestEmailConnectionStatus(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='email-agent@test.com')
        self.url = '/api/social/email/status/'

    def test_returns_status(self):
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_unauthenticated_denied(self):
        resp = self.client.get(self.url, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_no_feature_denied(self):
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_get(self.url, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_with_connection(self):
        self.create_email_connection()
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_status_returns_connection_data(self):
        """Verify response structure contains connected flag and connections list."""
        conn = self.create_email_connection(email_address='status-test@example.com')
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertTrue(data['connected'])
        self.assertIsInstance(data['connections'], list)
        self.assertEqual(len(data['connections']), 1)
        self.assertEqual(data['connections'][0]['email_address'], 'status-test@example.com')
        # Backwards-compat: single connection dict
        self.assertIsNotNone(data['connection'])

    def test_status_no_connections_returns_false(self):
        """When no connections exist, connected should be False."""
        resp = self.api_get(self.url, user=self.agent)
        data = resp.json()
        self.assertFalse(data['connected'])
        self.assertEqual(data['connections'], [])
        self.assertIsNone(data['connection'])

    def test_status_multiple_connections(self):
        """Status endpoint returns all accessible connections."""
        self.create_email_connection(email_address='multi1@example.com')
        self.create_email_connection(email_address='multi2@example.com')
        resp = self.api_get(self.url, user=self.agent)
        data = resp.json()
        self.assertEqual(len(data['connections']), 2)

    def test_status_hides_assigned_connections_from_non_assigned_agent(self):
        """When a connection has user assignments, non-assigned agents cannot see it."""
        conn = self.create_email_connection(email_address='private@example.com')
        other_agent = self.create_user(email='other-agent-status@test.com')
        EmailConnectionUserAssignment.objects.create(
            connection=conn, user=other_agent, assigned_by=other_agent,
        )
        resp = self.api_get(self.url, user=self.agent)
        data = resp.json()
        self.assertEqual(len(data['connections']), 0)


class TestEmailConnect(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='email-conn-admin@test.com')
        self.agent = self.create_user(email='email-conn-agent@test.com')
        self.url = '/api/social/email/connect/'

    def test_admin_can_connect(self):
        data = {
            'email_address': 'new@example.com',
            'imap_server': 'imap.example.com',
            'imap_port': 993,
            'smtp_server': 'smtp.example.com',
            'smtp_port': 587,
            'username': 'new@example.com',
            'password': 'pass123',
        }
        resp = self.api_post(self.url, data, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_agent_cannot_connect(self):
        resp = self.api_post(self.url, {}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_connect_missing_required_fields(self):
        """Submitting without required fields returns 400."""
        data = {'email_address': 'incomplete@example.com'}
        resp = self.api_post(self.url, data, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)



class TestEmailDisconnect(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='email-disc-admin@test.com')
        self.agent = self.create_user(email='email-disc-agent@test.com')
        self.url = '/api/social/email/disconnect/'

    def test_admin_can_disconnect(self):
        resp = self.api_post(self.url, {}, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_agent_cannot_disconnect(self):
        resp = self.api_post(self.url, {}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_disconnect_deletes_connection(self):
        """Disconnecting removes the connection and cascades to messages/drafts."""
        conn = self.create_email_connection(email_address='disc@example.com')
        self.create_email_message(connection=conn)
        self.create_email_draft(connection=conn, created_by=self.admin)
        resp = self.api_post(self.url, {'connection_id': conn.id}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp_data = resp.json()
        self.assertEqual(resp_data['status'], 'success')
        self.assertFalse(EmailConnection.objects.filter(id=conn.id).exists())
        self.assertEqual(EmailMessage.objects.filter(connection_id=conn.id).count(), 0)

    def test_disconnect_missing_connection_id(self):
        """Disconnect without connection_id returns 400."""
        resp = self.api_post(self.url, {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_disconnect_nonexistent_connection(self):
        """Disconnect with invalid connection_id returns 404."""
        resp = self.api_post(self.url, {'connection_id': 99999}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class TestEmailFolders(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='email-folders@test.com')
        self.url = '/api/social/email/folders/'

    def test_returns_folders(self):
        resp = self.api_get(self.url, user=self.agent)
        # May return 200 or 404 if no connection, but not 403
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_no_feature_denied(self):
        """Fix 8 verification: email_folders requires feature."""
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_get(self.url, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_folders_no_connections_returns_404(self):
        """When no active connections exist, returns 404."""
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)



class TestEmailMessageViewSet(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='email-msgs@test.com')
        self.url = '/api/social/email-messages/'

    def test_list_messages(self):
        conn = self.create_email_connection()
        self.create_email_message(connection=conn)
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_empty(self):
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_messages_paginated(self):
        """Response has paginated structure (results list)."""
        conn = self.create_email_connection()
        for i in range(3):
            self.create_email_message(
                connection=conn,
                message_id=f'<paginated-{i}@test.com>',
                subject=f'Paginated {i}',
            )
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        # DRF pagination wraps results; or flat list -- either way should have items
        results = data.get('results', data)
        self.assertGreaterEqual(len(results), 3)

    def test_has_business_reply_true_when_thread_answered(self):
        """A customer message whose thread has a business-sent message is
        flagged has_business_reply=True (answered from EchoDesk or synced
        from the external client's Sent folder)."""
        conn = self.create_email_connection()
        customer_msg = self.create_email_message(
            connection=conn,
            thread_id='thread_answered',
            folder='INBOX',
            is_from_business=False,
        )
        self.create_email_message(
            connection=conn,
            thread_id='thread_answered',
            folder='Sent',
            from_email=conn.email_address,
            is_from_business=True,
        )
        resp = self.api_get(f'{self.url}?folder=INBOX', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()['results']
        row = next(r for r in results if r['id'] == customer_msg.id)
        self.assertTrue(row['has_business_reply'])

    def test_has_business_reply_false_when_unanswered(self):
        conn = self.create_email_connection()
        msg = self.create_email_message(
            connection=conn,
            thread_id='thread_unanswered',
            folder='INBOX',
            is_from_business=False,
        )
        resp = self.api_get(self.url, user=self.agent)
        results = resp.json()['results']
        row = next(r for r in results if r['id'] == msg.id)
        self.assertFalse(row['has_business_reply'])

    def test_has_business_reply_ignores_other_threads_and_connections(self):
        conn = self.create_email_connection()
        other_conn = self.create_email_connection()
        msg = self.create_email_message(
            connection=conn,
            thread_id='thread_x',
            folder='INBOX',
            is_from_business=False,
        )
        # Business reply in a DIFFERENT thread on the same connection
        self.create_email_message(
            connection=conn, thread_id='thread_y', folder='Sent',
            is_from_business=True,
        )
        # Business reply in the SAME thread id but a different connection
        self.create_email_message(
            connection=other_conn, thread_id='thread_x', folder='Sent',
            is_from_business=True,
        )
        resp = self.api_get(f'{self.url}?connection_id={conn.id}', user=self.agent)
        results = resp.json()['results']
        row = next(r for r in results if r['id'] == msg.id)
        self.assertFalse(row['has_business_reply'])

    def test_filter_by_folder(self):
        """folder query param filters messages by IMAP folder."""
        conn = self.create_email_connection()
        self.create_email_message(connection=conn, folder='INBOX', message_id='<inbox1@test.com>')
        self.create_email_message(connection=conn, folder='Sent', message_id='<sent1@test.com>')
        resp = self.api_get(f'{self.url}?folder=Sent', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json().get('results', resp.json())
        for msg in results:
            self.assertEqual(msg['folder'], 'Sent')

    def test_filter_by_connection_id(self):
        """connection_id query param filters messages to a specific account."""
        conn1 = self.create_email_connection(email_address='conn1-filter@example.com')
        conn2 = self.create_email_connection(email_address='conn2-filter@example.com')
        self.create_email_message(connection=conn1, message_id='<c1msg@test.com>')
        self.create_email_message(connection=conn2, message_id='<c2msg@test.com>')
        resp = self.api_get(f'{self.url}?connection_id={conn1.id}', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json().get('results', resp.json())
        for msg in results:
            self.assertEqual(msg['connection_id'], conn1.id)

    def test_filter_by_starred(self):
        """starred=true returns only starred messages."""
        conn = self.create_email_connection()
        self.create_email_message(connection=conn, is_starred=True, message_id='<star@test.com>')
        self.create_email_message(connection=conn, is_starred=False, message_id='<nostar@test.com>')
        resp = self.api_get(f'{self.url}?starred=true', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json().get('results', resp.json())
        for msg in results:
            self.assertTrue(msg['is_starred'])

    def test_filter_by_is_read(self):
        """is_read query param filters by read status."""
        conn = self.create_email_connection()
        self.create_email_message(connection=conn, is_read=True, message_id='<read@test.com>')
        self.create_email_message(connection=conn, is_read=False, message_id='<unread@test.com>')
        resp = self.api_get(f'{self.url}?is_read=false', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json().get('results', resp.json())
        for msg in results:
            self.assertFalse(msg['is_read'])

    def test_filter_by_search(self):
        """search query param filters by subject/sender."""
        conn = self.create_email_connection()
        self.create_email_message(
            connection=conn, subject='Invoice #42', message_id='<search1@test.com>',
        )
        self.create_email_message(
            connection=conn, subject='Hello World', message_id='<search2@test.com>',
        )
        resp = self.api_get(f'{self.url}?search=Invoice', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json().get('results', resp.json())
        self.assertTrue(all('Invoice' in msg['subject'] for msg in results))

    def test_thread_grouping(self):
        """threads endpoint groups messages by thread_id and returns latest per thread."""
        conn = self.create_email_connection()
        now = timezone.now()
        self.create_email_message(
            connection=conn,
            thread_id='thread-group-A',
            message_id='<tgA1@test.com>',
            subject='Thread A msg 1',
            timestamp=now - timezone.timedelta(hours=2),
        )
        self.create_email_message(
            connection=conn,
            thread_id='thread-group-A',
            message_id='<tgA2@test.com>',
            subject='Thread A msg 2',
            timestamp=now - timezone.timedelta(hours=1),
        )
        self.create_email_message(
            connection=conn,
            thread_id='thread-group-B',
            message_id='<tgB1@test.com>',
            subject='Thread B msg 1',
            timestamp=now,
        )
        resp = self.api_get(f'{self.url}threads/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        threads = resp.json()
        thread_ids = [t['thread_id'] for t in threads]
        self.assertIn('thread-group-A', thread_ids)
        self.assertIn('thread-group-B', thread_ids)
        # Each thread_id should appear exactly once
        self.assertEqual(len(thread_ids), len(set(thread_ids)))

    def test_no_feature_denied(self):
        """Users without social_integrations feature get 403."""
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_get(self.url, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_deleted_messages_excluded(self):
        """Messages with is_deleted=True are excluded from the list."""
        conn = self.create_email_connection()
        self.create_email_message(
            connection=conn, is_deleted=True, message_id='<deleted@test.com>',
        )
        self.create_email_message(
            connection=conn, is_deleted=False, message_id='<active@test.com>',
        )
        resp = self.api_get(self.url, user=self.agent)
        results = resp.json().get('results', resp.json())
        message_ids = [m['message_id'] for m in results]
        self.assertNotIn('<deleted@test.com>', message_ids)
        self.assertIn('<active@test.com>', message_ids)

    def test_filter_by_thread_id(self):
        """thread_id query param filters to a single thread."""
        conn = self.create_email_connection()
        self.create_email_message(
            connection=conn, thread_id='specific-thread', message_id='<thr1@test.com>',
        )
        self.create_email_message(
            connection=conn, thread_id='other-thread', message_id='<thr2@test.com>',
        )
        resp = self.api_get(f'{self.url}?thread_id=specific-thread', user=self.agent)
        results = resp.json().get('results', resp.json())
        for msg in results:
            self.assertEqual(msg['thread_id'], 'specific-thread')


class TestEmailDraftViewSet(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='email-drafts@test.com')
        self.url = '/api/social/email-drafts/'

    def test_list_drafts(self):
        conn = self.create_email_connection()
        self.create_email_draft(connection=conn, created_by=self.agent)
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_only_own_drafts(self):
        """Users only see their own drafts, not other users' drafts."""
        conn = self.create_email_connection()
        other_user = self.create_user(email='other-draft-user@test.com')
        self.create_email_draft(connection=conn, created_by=self.agent, subject='My Draft')
        self.create_email_draft(connection=conn, created_by=other_user, subject='Their Draft')
        resp = self.api_get(self.url, user=self.agent)
        results = resp.json().get('results', resp.json())
        subjects = [d['subject'] for d in results]
        self.assertIn('My Draft', subjects)
        self.assertNotIn('Their Draft', subjects)

    def test_create_draft(self):
        """POST creates a draft linked to the user and an active connection."""
        conn = self.create_email_connection()
        data = {
            'connection': conn.id,
            'to_emails': [{'email': 'recipient@example.com'}],
            'subject': 'New Draft',
            'body_text': 'Draft body content',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertIn(resp.status_code, [status.HTTP_201_CREATED, status.HTTP_200_OK])

    def test_create_draft_no_connection_fails(self):
        """Creating a draft when no active email connection exists fails."""
        data = {
            'to_emails': [{'email': 'recipient@example.com'}],
            'subject': 'Orphan Draft',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_draft(self):
        """PATCH updates an existing draft's fields."""
        conn = self.create_email_connection()
        draft = self.create_email_draft(
            connection=conn, created_by=self.agent, subject='Old Subject',
        )
        resp = self.api_patch(
            f'{self.url}{draft.id}/',
            {'subject': 'Updated Subject'},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        draft.refresh_from_db()
        self.assertEqual(draft.subject, 'Updated Subject')

    def test_delete_draft(self):
        """DELETE removes the draft record."""
        conn = self.create_email_connection()
        draft = self.create_email_draft(connection=conn, created_by=self.agent)
        resp = self.api_delete(f'{self.url}{draft.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(EmailDraft.objects.filter(id=draft.id).exists())

    def test_no_feature_denied(self):
        """Users without social_integrations feature get 403."""
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_get(self.url, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestEmailSend(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='email-send-agent@test.com')
        self.url = '/api/social/email/send/'

    def test_send_requires_recipient(self):
        """Sending without to_emails is rejected."""
        self.create_email_connection()
        data = {
            'subject': 'No recipients',
            'body_text': 'Test',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_send_requires_subject_or_body(self):
        """Sending without body_text and body_html is rejected."""
        self.create_email_connection()
        data = {
            'to_emails': ['to@example.com'],
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_send_no_connection_returns_404(self):
        """Sending when no active connection exists returns 404."""
        data = {
            'to_emails': ['to@example.com'],
            'subject': 'No connection',
            'body_text': 'Test',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_send_no_feature_denied(self):
        """Users without social_integrations feature get 403."""
        with patch('users.models.User.has_feature', return_value=False):
            data = {
                'to_emails': ['to@example.com'],
                'subject': 'Denied',
                'body_text': 'Test',
            }
            resp = self.api_post(self.url, data, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestEmailAction(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='email-action-agent@test.com')
        self.conn = self.create_email_connection()
        self.url = '/api/social/email/action/'

    def test_mark_read(self):
        """mark_read action sets is_read=True on specified messages."""
        msg = self.create_email_message(
            connection=self.conn, is_read=False, message_id='<markread@test.com>',
        )
        data = {
            'message_ids': [msg.id],
            'action': 'mark_read',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msg.refresh_from_db()
        self.assertTrue(msg.is_read)
        self.assertTrue(msg.is_read_by_staff)

    def test_mark_unread(self):
        """mark_unread action sets is_read=False."""
        msg = self.create_email_message(
            connection=self.conn, is_read=True, message_id='<markunread@test.com>',
        )
        data = {
            'message_ids': [msg.id],
            'action': 'mark_unread',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msg.refresh_from_db()
        self.assertFalse(msg.is_read)
        self.assertFalse(msg.is_read_by_staff)

    def test_star_email(self):
        """star action sets is_starred=True."""
        msg = self.create_email_message(
            connection=self.conn, is_starred=False, message_id='<star-action@test.com>',
        )
        data = {
            'message_ids': [msg.id],
            'action': 'star',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msg.refresh_from_db()
        self.assertTrue(msg.is_starred)

    def test_unstar_email(self):
        """unstar action sets is_starred=False."""
        msg = self.create_email_message(
            connection=self.conn, is_starred=True, message_id='<unstar-action@test.com>',
        )
        data = {
            'message_ids': [msg.id],
            'action': 'unstar',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msg.refresh_from_db()
        self.assertFalse(msg.is_starred)

    def test_delete_email(self):
        """delete action soft-deletes by setting is_deleted=True."""
        msg = self.create_email_message(
            connection=self.conn, message_id='<delete-action@test.com>',
        )
        data = {
            'message_ids': [msg.id],
            'action': 'delete',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msg.refresh_from_db()
        self.assertTrue(msg.is_deleted)
        self.assertIsNotNone(msg.deleted_at)
        self.assertEqual(msg.deleted_by, self.agent)

    def test_restore_email(self):
        """restore action reverses a soft delete."""
        msg = self.create_email_message(
            connection=self.conn,
            is_deleted=True,
            deleted_at=timezone.now(),
            message_id='<restore-action@test.com>',
        )
        data = {
            'message_ids': [msg.id],
            'action': 'restore',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msg.refresh_from_db()
        self.assertFalse(msg.is_deleted)
        self.assertIsNone(msg.deleted_at)
        self.assertIsNone(msg.deleted_by)

    def test_bulk_action(self):
        """Action applied to multiple message_ids in one call."""
        msg1 = self.create_email_message(
            connection=self.conn, is_read=False, message_id='<bulk1@test.com>',
        )
        msg2 = self.create_email_message(
            connection=self.conn, is_read=False, message_id='<bulk2@test.com>',
        )
        data = {
            'message_ids': [msg1.id, msg2.id],
            'action': 'mark_read',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp_data = resp.json()
        self.assertEqual(resp_data['affected_count'], 2)
        msg1.refresh_from_db()
        msg2.refresh_from_db()
        self.assertTrue(msg1.is_read)
        self.assertTrue(msg2.is_read)

    def test_action_by_thread_id(self):
        """Action can be applied to all messages in a thread by thread_id."""
        msg1 = self.create_email_message(
            connection=self.conn,
            thread_id='action-thread',
            is_starred=False,
            message_id='<athrd1@test.com>',
        )
        msg2 = self.create_email_message(
            connection=self.conn,
            thread_id='action-thread',
            is_starred=False,
            message_id='<athrd2@test.com>',
        )
        data = {
            'thread_id': 'action-thread',
            'action': 'star',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msg1.refresh_from_db()
        msg2.refresh_from_db()
        self.assertTrue(msg1.is_starred)
        self.assertTrue(msg2.is_starred)

    def test_action_missing_identifiers(self):
        """Action without message_ids or thread_id is rejected."""
        data = {'action': 'mark_read'}
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_action_nonexistent_messages(self):
        """Action on nonexistent message IDs returns 404."""
        data = {
            'message_ids': [999999],
            'action': 'mark_read',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_label_action(self):
        """label action adds a label to messages."""
        msg = self.create_email_message(
            connection=self.conn, labels=[], message_id='<label@test.com>',
        )
        data = {
            'message_ids': [msg.id],
            'action': 'label',
            'label': 'Important',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msg.refresh_from_db()
        self.assertIn('Important', msg.labels)

    def test_unlabel_action(self):
        """unlabel action removes a label from messages."""
        msg = self.create_email_message(
            connection=self.conn, labels=['ToRemove'], message_id='<unlabel@test.com>',
        )
        data = {
            'message_ids': [msg.id],
            'action': 'unlabel',
            'label': 'ToRemove',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msg.refresh_from_db()
        self.assertNotIn('ToRemove', msg.labels)

    def test_move_action(self):
        """move action changes the folder of messages."""
        msg = self.create_email_message(
            connection=self.conn, folder='INBOX', message_id='<move@test.com>',
        )
        data = {
            'message_ids': [msg.id],
            'action': 'move',
            'folder': 'Archive',
        }
        resp = self.api_post(self.url, data, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msg.refresh_from_db()
        self.assertEqual(msg.folder, 'Archive')

    def test_no_feature_denied(self):
        """Users without social_integrations feature get 403."""
        with patch('users.models.User.has_feature', return_value=False):
            data = {
                'message_ids': [1],
                'action': 'mark_read',
            }
            resp = self.api_post(self.url, data, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestEmailConnectionUpdate(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='email-upd-admin@test.com')
        self.agent = self.create_user(email='email-upd-agent@test.com')
        self.url = '/api/social/email/update/'

    def test_update_connection_display_name(self):
        """Admin can update a connection's display name."""
        conn = self.create_email_connection(display_name='Old Name')
        resp = self.api_post(self.url, {
            'connection_id': conn.id,
            'display_name': 'New Name',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        conn.refresh_from_db()
        self.assertEqual(conn.display_name, 'New Name')

    def test_update_connection_signature(self):
        """Admin can update connection-level signature settings."""
        conn = self.create_email_connection()
        resp = self.api_post(self.url, {
            'connection_id': conn.id,
            'signature_enabled': True,
            'signature_html': '<p>Regards</p>',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        conn.refresh_from_db()
        self.assertTrue(conn.signature_enabled)
        self.assertEqual(conn.signature_html, '<p>Regards</p>')

    def test_update_missing_connection_id(self):
        """Update without connection_id returns 400."""
        resp = self.api_post(self.url, {'display_name': 'X'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_nonexistent_connection(self):
        """Update for nonexistent connection returns 404."""
        resp = self.api_post(self.url, {
            'connection_id': 99999,
            'display_name': 'X',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_agent_cannot_update_connection(self):
        """Non-admin agent cannot update connections."""
        resp = self.api_post(self.url, {}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestEmailSignatureView(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='email-sig-admin@test.com')
        self.agent = self.create_user(email='email-sig-agent@test.com')
        self.url = '/api/social/email/signature/'

    def test_get_signature(self):
        resp = self.api_get(self.url, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_signature(self):
        resp = self.api_put(self.url, {
            'sender_name': 'Support',
            'signature_html': '<p>Thanks</p>',
            'is_enabled': True,
        }, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_get_signature_creates_singleton(self):
        """GET creates the signature record if it does not exist yet."""
        self.assertFalse(EmailSignature.objects.exists())
        resp = self.api_get(self.url, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(EmailSignature.objects.exists())

    def test_patch_signature_updates_fields(self):
        """PATCH updates specific fields on the signature."""
        resp = self.api_patch(self.url, {
            'sender_name': 'Engineering Team',
            'signature_html': '<b>Best regards</b>',
            'is_enabled': True,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        sig = EmailSignature.objects.first()
        self.assertEqual(sig.sender_name, 'Engineering Team')
        self.assertEqual(sig.signature_html, '<b>Best regards</b>')
        self.assertTrue(sig.is_enabled)

    def test_patch_partial_update(self):
        """PATCH with only one field does not clear others."""
        self.api_patch(self.url, {
            'sender_name': 'First',
            'signature_html': '<p>HTML</p>',
        }, user=self.admin)
        self.api_patch(self.url, {
            'sender_name': 'Updated',
        }, user=self.admin)
        sig = EmailSignature.objects.first()
        self.assertEqual(sig.sender_name, 'Updated')
        self.assertEqual(sig.signature_html, '<p>HTML</p>')

    def test_agent_can_read_signature(self):
        """Agents with the feature can read the signature (GET is SAFE_METHODS)."""
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_agent_cannot_update_signature(self):
        """Non-admin agents cannot PATCH the signature."""
        resp = self.api_patch(self.url, {'sender_name': 'Hacked'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_no_feature_denied(self):
        """Users without social_integrations feature get 403."""
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_get(self.url, user=self.admin)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestEmailAssignments(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='email-assign-admin@test.com')
        self.agent = self.create_user(email='email-assign-agent@test.com')

    def test_list_assignments(self):
        resp = self.api_get('/api/social/email/assignments/', user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_assignment(self):
        conn = self.create_email_connection()
        resp = self.api_post('/api/social/email/assignments/create/', {
            'connection_id': conn.id,
            'user_id': self.agent.id,
        }, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_assignments_returns_connections(self):
        """List returns all connections with their assigned users."""
        conn = self.create_email_connection(email_address='assign-list@example.com')
        EmailConnectionUserAssignment.objects.create(
            connection=conn, user=self.agent, assigned_by=self.admin,
        )
        resp = self.api_get('/api/social/email/assignments/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)
        emails = [c['email_address'] for c in data]
        self.assertIn('assign-list@example.com', emails)

    def test_create_assignment_returns_201(self):
        """Creating a valid assignment returns 201 with the assignment data."""
        conn = self.create_email_connection()
        resp = self.api_post('/api/social/email/assignments/create/', {
            'connection_id': conn.id,
            'user_id': self.agent.id,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            EmailConnectionUserAssignment.objects.filter(
                connection=conn, user=self.agent,
            ).exists()
        )

    def test_create_duplicate_assignment_rejected(self):
        """Creating an assignment that already exists returns 400."""
        conn = self.create_email_connection()
        EmailConnectionUserAssignment.objects.create(
            connection=conn, user=self.agent, assigned_by=self.admin,
        )
        resp = self.api_post('/api/social/email/assignments/create/', {
            'connection_id': conn.id,
            'user_id': self.agent.id,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_assignment_invalid_connection(self):
        """Creating an assignment for a nonexistent connection returns 404."""
        resp = self.api_post('/api/social/email/assignments/create/', {
            'connection_id': 99999,
            'user_id': self.agent.id,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_create_assignment_invalid_user(self):
        """Creating an assignment for a nonexistent user returns 404."""
        conn = self.create_email_connection()
        resp = self.api_post('/api/social/email/assignments/create/', {
            'connection_id': conn.id,
            'user_id': 99999,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_create_assignment_missing_fields(self):
        """Missing connection_id or user_id returns 400."""
        resp = self.api_post('/api/social/email/assignments/create/', {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_assignment(self):
        """DELETE removes an assignment."""
        conn = self.create_email_connection()
        assignment = EmailConnectionUserAssignment.objects.create(
            connection=conn, user=self.agent, assigned_by=self.admin,
        )
        resp = self.api_delete(
            f'/api/social/email/assignments/{assignment.id}/', user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(
            EmailConnectionUserAssignment.objects.filter(id=assignment.id).exists()
        )

    def test_delete_nonexistent_assignment(self):
        """Deleting a nonexistent assignment returns 404."""
        resp = self.api_delete(
            '/api/social/email/assignments/99999/', user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_agent_cannot_create_assignment(self):
        """Non-admin agents cannot create assignments."""
        conn = self.create_email_connection()
        resp = self.api_post('/api/social/email/assignments/create/', {
            'connection_id': conn.id,
            'user_id': self.agent.id,
        }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_agent_cannot_delete_assignment(self):
        """Non-admin agents cannot delete assignments."""
        conn = self.create_email_connection()
        assignment = EmailConnectionUserAssignment.objects.create(
            connection=conn, user=self.agent, assigned_by=self.admin,
        )
        resp = self.api_delete(
            f'/api/social/email/assignments/{assignment.id}/', user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestEmailConnectionAssignedUsers(SocialIntegrationTestCase):
    """Tests for the GET/PUT assigned-users endpoint per connection."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='email-cu-admin@test.com')
        self.agent1 = self.create_user(email='email-cu-agent1@test.com')
        self.agent2 = self.create_user(email='email-cu-agent2@test.com')
        self.conn = self.create_email_connection(email_address='cu-conn@example.com')

    def test_get_assigned_users_empty(self):
        """GET returns empty list when no users are assigned."""
        url = f'/api/social/email/{self.conn.id}/assigned-users/'
        resp = self.api_get(url, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data['assigned_users'], [])

    def test_get_assigned_users_with_data(self):
        """GET returns assigned users for the connection."""
        EmailConnectionUserAssignment.objects.create(
            connection=self.conn, user=self.agent1, assigned_by=self.admin,
        )
        url = f'/api/social/email/{self.conn.id}/assigned-users/'
        resp = self.api_get(url, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(len(data['assigned_users']), 1)
        self.assertEqual(data['assigned_users'][0]['user_email'], 'email-cu-agent1@test.com')

    def test_put_replaces_assigned_users(self):
        """PUT replaces the entire set of assigned users."""
        EmailConnectionUserAssignment.objects.create(
            connection=self.conn, user=self.agent1, assigned_by=self.admin,
        )
        url = f'/api/social/email/{self.conn.id}/assigned-users/'
        resp = self.api_put(url, {
            'user_ids': [self.agent2.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        assignments = EmailConnectionUserAssignment.objects.filter(connection=self.conn)
        self.assertEqual(assignments.count(), 1)
        self.assertEqual(assignments.first().user, self.agent2)

    def test_put_invalid_user_ids(self):
        """PUT with invalid user IDs returns 400."""
        url = f'/api/social/email/{self.conn.id}/assigned-users/'
        resp = self.api_put(url, {
            'user_ids': [99999],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_nonexistent_connection_returns_404(self):
        """Request for a nonexistent connection returns 404."""
        url = '/api/social/email/99999/assigned-users/'
        resp = self.api_get(url, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

