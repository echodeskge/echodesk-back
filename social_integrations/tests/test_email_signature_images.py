"""Base64 signature images: inline cid parts on send, public URLs on save."""
import base64
import email
from unittest.mock import MagicMock, patch

from social_integrations.email_utils import externalize_data_images, inline_data_images
from social_integrations.models import EmailSignature
from social_integrations.tests.conftest import SocialIntegrationTestCase

PNG = base64.b64encode(b'\x89PNG\r\n\x1a\nfake-logo-bytes').decode()
LOGO = f'<p>Thanks</p><img src="data:image/png;base64,{PNG}" width="120" alt="logo">'


class TestInlineDataImages(SocialIntegrationTestCase):
    def test_replaces_with_cid_and_dedupes(self):
        html, parts = inline_data_images(LOGO + LOGO, 'acme.ge')
        self.assertNotIn('data:', html)
        self.assertEqual(len(parts), 1)
        cid = parts[0]['Content-ID'].strip('<>')
        self.assertEqual(html.count(f'cid:{cid}'), 2)
        self.assertTrue(cid.endswith('@acme.ge'))
        self.assertIn('inline', parts[0]['Content-Disposition'])

    def test_leaves_invalid_and_plain_html(self):
        html, parts = inline_data_images('<img src="data:image/png;base64,@@@">')
        self.assertEqual(parts, [])
        self.assertIn('data:image/png', html)
        self.assertEqual(inline_data_images('<p>hi</p>'), ('<p>hi</p>', []))

    def test_sent_mime_is_related_with_inline_image(self):
        from social_integrations.email_utils import send_email_smtp
        conn = self.create_email_connection()
        smtp = MagicMock()
        smtp.sendmail.return_value = {}
        with patch('social_integrations.email_utils.smtplib.SMTP', return_value=smtp), \
                patch('social_integrations.email_utils.smtplib.SMTP_SSL', return_value=smtp):
            try:
                send_email_smtp(conn, ['client@example.com'], subject='Hi', body_text='Thanks',
                                body_html=LOGO, signature_included=True)
            except Exception:
                pass  # post-send bookkeeping is not under test
        self.assertTrue(smtp.sendmail.called, 'SMTP send was not reached')
        raw = smtp.sendmail.call_args[0][2]
        msg = email.message_from_string(raw)
        types = [p.get_content_type() for p in msg.walk()]
        self.assertIn('multipart/related', types)
        self.assertIn('image/png', types)
        html = next(p for p in msg.walk() if p.get_content_type() == 'text/html').get_payload(decode=True).decode()
        self.assertNotIn('data:image', html)
        self.assertIn('cid:', html)


class TestExternalizeOnSave(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.storage = patch('social_integrations.email_utils.default_storage')
        storage = self.storage.start()
        storage.save.side_effect = lambda path, content: path
        storage.url.side_effect = lambda path: f'https://cdn.example/media/{path}'

    def tearDown(self):
        self.storage.stop()
        super().tearDown()

    def test_helper_swaps_in_public_url(self):
        out = externalize_data_images(LOGO)
        self.assertNotIn('data:', out)
        self.assertIn('src="https://cdn.example/media/email_signatures/', out)
        self.assertIn('width="120"', out)

    def test_connection_signature_update(self):
        admin = self.create_admin(email='sig-admin@test.com')
        conn = self.create_email_connection()
        resp = self.api_post('/api/social/email/update/', {
            'connection_id': conn.id, 'signature_html': LOGO,
        }, user=admin)
        self.assertIn(resp.status_code, (200, 201), resp.content)
        conn.refresh_from_db()
        self.assertNotIn('data:', conn.signature_html)
        self.assertIn('https://cdn.example/media/', conn.signature_html)

    def test_tenant_signature_update(self):
        admin = self.create_admin(email='sig-admin2@test.com')
        resp = self.api_patch('/api/social/email/signature/', {'signature_html': LOGO}, user=admin)
        self.assertIn(resp.status_code, (200, 201), resp.content)
        sig = EmailSignature.objects.first()
        self.assertNotIn('data:', sig.signature_html)
