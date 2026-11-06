"""Regression tests for the Sentry issues fixed on 2026-11-06:

- ECHODESK-BACKEND-7X: Instagram/Facebook auto-reply hit a unique-constraint
  error when the echo webhook had already stored the same message_id.
- ECHODESK-BACKEND-7Y/7Z: email attachments with RFC 2047 encoded names were
  uploaded under keys the object store rejected.
- ECHODESK-BACKEND-80/81/82: overlapping email syncs of the same mailbox.
"""
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

from django.utils import timezone

from social_integrations.models import FacebookMessage, InstagramMessage
from social_integrations.tests.conftest import SocialIntegrationTestCase


def _ok(message_id):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'message_id': message_id}
    return resp


class TestAutoReplyAfterEcho(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.page = self.create_fb_connection(page_id='page_ar')
        self.account = self.create_ig_connection(fb_page=self.page, username='shop')

    @patch('social_integrations.views.requests.post')
    def test_instagram_echo_stored_first(self, post):
        from social_integrations.views import send_instagram_auto_reply

        InstagramMessage.objects.create(
            account_connection=self.account, message_id='ig_mid_1', sender_id='cust1',
            message_text='Hi!', timestamp=timezone.now(), is_from_business=True, is_echo=True,
        )
        post.return_value = _ok('ig_mid_1')
        self.assertTrue(send_instagram_auto_reply('cust1', 'Hi!', self.account))
        msg = InstagramMessage.objects.get(message_id='ig_mid_1')
        self.assertEqual(msg.source, 'echodesk')
        self.assertFalse(msg.is_echo)
        self.assertEqual(InstagramMessage.objects.filter(message_id='ig_mid_1').count(), 1)

    @patch('social_integrations.views.requests.post')
    def test_facebook_echo_stored_first(self, post):
        from social_integrations.views import send_facebook_auto_reply

        FacebookMessage.objects.create(
            page_connection=self.page, message_id='fb_mid_1', sender_id='cust1',
            message_text='Hi!', timestamp=timezone.now(), is_from_page=True, is_echo=True,
        )
        post.return_value = _ok('fb_mid_1')
        self.assertTrue(send_facebook_auto_reply('cust1', 'Hi!', self.page))
        self.assertEqual(FacebookMessage.objects.filter(message_id='fb_mid_1').count(), 1)
        self.assertEqual(FacebookMessage.objects.get(message_id='fb_mid_1').source, 'echodesk')

    @patch('social_integrations.views.requests.post')
    def test_instagram_no_echo_still_creates(self, post):
        from social_integrations.views import send_instagram_auto_reply

        post.return_value = _ok('ig_mid_2')
        self.assertTrue(send_instagram_auto_reply('cust2', 'Hello', self.account))
        self.assertTrue(InstagramMessage.objects.filter(message_id='ig_mid_2', sender_id='cust2').exists())


class TestEmailAttachmentNames(SocialIntegrationTestCase):

    def _message(self, raw_filename):
        msg = MIMEMultipart()
        msg.attach(MIMEText('body'))
        part = MIMEApplication(b'%PDF-1.4 test', _subtype='pdf')
        # Set the header verbatim, as some mail clients do (RFC 2047 word
        # inside a quoted filename instead of RFC 2231).
        part['Content-Disposition'] = f'attachment; filename="{raw_filename}"'
        msg.attach(part)
        return msg

    def test_encoded_georgian_filename_is_decoded_and_key_is_ascii(self):
        from social_integrations.email_utils import extract_attachments

        connection = self.create_email_connection()
        saved = {}

        def fake_save(path, content):
            saved['path'] = path
            return path

        with patch('social_integrations.email_utils.default_storage') as storage:
            storage.save.side_effect = fake_save
            storage.url.side_effect = lambda p: f'https://cdn.test/{p}'
            atts = extract_attachments(self._message('=?utf-8?B?4YOT4YOm4YOSLnBkZg==?='), connection)

        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0]['filename'], 'დღგ.pdf')
        key = saved['path']
        self.assertTrue(key.isascii(), key)
        self.assertNotIn('?', key)
        self.assertNotIn('=', key)
        self.assertTrue(key.endswith('.pdf'), key)

    def test_safe_filename_strips_paths_and_keeps_ascii_names(self):
        from social_integrations.email_utils import _storage_safe_filename

        name = _storage_safe_filename('../../etc/invoice 2026 (final).PDF')
        self.assertNotIn('/', name)
        self.assertTrue(name.endswith('invoice-2026-final.PDF'), name)
        self.assertTrue(_storage_safe_filename('ინვოისი').endswith('-attachment'))


class TestEmailSyncLock(SocialIntegrationTestCase):

    def test_second_sync_of_same_mailbox_is_skipped(self):
        from django.core.cache import cache
        from social_integrations import tasks

        connection = self.create_email_connection()
        key = f'email-sync-lock:{self.tenant.schema_name}:{connection.id}'
        cache.delete(key)
        with patch('social_integrations.email_utils.sync_imap_messages', return_value=4) as sync:
            cache.add(key, '1', 60)  # another worker holds the mailbox
            self.assertEqual(tasks._sync_connection_locked(self.tenant.schema_name, connection), 0)
            sync.assert_not_called()
            cache.delete(key)
            self.assertEqual(tasks._sync_connection_locked(self.tenant.schema_name, connection), 4)
            sync.assert_called_once()
        # Lock released after the run.
        self.assertTrue(cache.add(key, '1', 1))
        cache.delete(key)
