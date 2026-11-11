"""Regression tests for ECHODESK-BACKEND-7Y/7Z + 80/81/82 (2026-11-11):

The email sync swallowed Celery's SoftTimeLimitExceeded (logged as "Failed
to save attachment ...: SoftTimeLimitExceeded()") and kept going until the
600s hard limit SIGKILLed the worker.
"""
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

from celery.exceptions import SoftTimeLimitExceeded

from social_integrations.tests.conftest import SocialIntegrationTestCase


class TestEmailSyncSoftTimeLimit(SocialIntegrationTestCase):

    @staticmethod
    def _message_with_attachment():
        msg = MIMEMultipart()
        msg['From'] = 'a@example.com'
        msg['To'] = 'b@example.com'
        msg['Subject'] = 'invoice'
        msg.attach(MIMEText('see attached'))
        part = MIMEApplication(b'%PDF-1.4 test', _subtype='pdf')
        part['Content-Disposition'] = 'attachment; filename="invoice.pdf"'
        msg.attach(part)
        return msg

    def test_attachment_upload_does_not_swallow_the_soft_limit(self):
        from social_integrations.email_utils import extract_attachments

        connection_obj = self.create_email_connection()
        with patch('social_integrations.email_utils.default_storage') as storage:
            storage.save.side_effect = SoftTimeLimitExceeded()
            with self.assertRaises(SoftTimeLimitExceeded):
                extract_attachments(self._message_with_attachment(), connection_obj)

    def test_attachment_upload_errors_are_still_skipped(self):
        from social_integrations.email_utils import extract_attachments

        connection_obj = self.create_email_connection()
        with patch('social_integrations.email_utils.default_storage') as storage:
            storage.save.side_effect = RuntimeError('BadRequest')
            self.assertEqual(extract_attachments(self._message_with_attachment(), connection_obj), [])

    def test_folder_sync_soft_limit_stops_the_run_without_a_failure_mark(self):
        from social_integrations import email_utils

        connection_obj = self.create_email_connection()
        imap = MagicMock()
        folders = [{'flags': set(), 'name': 'INBOX', 'role': 'inbox', 'raw_name': 'INBOX', 'db_name': 'INBOX'}]
        with patch.object(email_utils, '_connect_imap', return_value=imap), \
                patch('social_integrations.email_mailbox.list_mailboxes', return_value=folders), \
                patch.object(email_utils, '_sync_folder', side_effect=SoftTimeLimitExceeded()), \
                patch.object(email_utils, '_record_sync_failure') as record_failure:
            with self.assertRaises(SoftTimeLimitExceeded):
                email_utils._sync_imap_messages_locked(connection_obj, 500)

        record_failure.assert_not_called()
        imap.logout.assert_called_once()
        connection_obj.refresh_from_db()
        self.assertEqual(connection_obj.sync_failure_count, 0)
        self.assertTrue(connection_obj.is_active)

    def test_other_folder_errors_are_still_recorded(self):
        from social_integrations import email_utils

        connection_obj = self.create_email_connection()
        with patch.object(email_utils, '_connect_imap', side_effect=OSError('network down')), \
                patch.object(email_utils, '_record_sync_failure') as record_failure:
            self.assertEqual(email_utils._sync_imap_messages_locked(connection_obj, 500), 0)
        record_failure.assert_called_once()
