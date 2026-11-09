"""Tests for TicketAttachmentViewSet (BE-06 / A12, GAP j)."""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from tickets.models import TicketAttachment
from tickets.tests.conftest import TicketTestCase

# Attachments live on S3 in production; keep test uploads in memory.
IN_MEMORY_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


class InMemoryStorageMixin:
    """Enable IN_MEMORY_STORAGES per test (EchoDeskTenantTestCase.setUpClass
    does not call super(), so class-level override_settings never applies)."""

    def setUp(self):
        # Enable before super().setUp() so the overrides nest correctly with
        # the base class's middleware override (disabled in tearDown, before
        # this cleanup runs); the other order leaks TEST_MIDDLEWARE.
        override = override_settings(STORAGES=IN_MEMORY_STORAGES)
        override.enable()
        self.addCleanup(override.disable)
        super().setUp()


class AttachmentBase(InMemoryStorageMixin, TicketTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='att-admin@test.com')
        self.owner = self.create_user(email='att-owner@test.com')
        self.viewer = self.create_user(email='att-viewer@test.com')
        self.stranger = self.create_user(email='att-stranger@test.com')
        self.board = self.create_board('Att', created_by=self.admin, users=[self.owner, self.viewer])
        self.col = self.create_column(self.board, 'To Do', 0)
        self.ticket = self.create_ticket('T', column=self.col, created_by=self.owner)

    def upload(self, user, ticket=None, name='notes.txt', content=b'hello'):
        client = self.authenticated_client(user)
        return client.post('/api/attachments/', {
            'ticket': (ticket or self.ticket).id,
            'file': SimpleUploadedFile(name, content, content_type='text/plain'),
        }, format='multipart', HTTP_HOST='tenant.test.com')


class TestAttachmentCRUD(AttachmentBase):

    def test_upload_to_visible_ticket(self):
        resp = self.upload(self.viewer)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['uploaded_by']['id'], self.viewer.id)
        self.assertEqual(resp.data['filename'], 'notes.txt')
        self.assertEqual(resp.data['file_size'], 5)
        self.assertTrue(resp.data['file_url'])

    def test_filename_is_sanitised(self):
        resp = self.upload(self.owner, name='../../etc/pass\x07wd.txt')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['filename'], 'passwd.txt')
        self.assertNotIn('/', TicketAttachment.objects.get(id=resp.data['id']).filename)

    def test_list_carries_file_url(self):
        self.create_attachment(self.ticket)
        resp = self.api_get(f'/api/attachments/?ticket={self.ticket.id}', user=self.viewer)
        rows = self.get_results(resp)
        self.assertEqual(len(rows), 1)
        self.assertIn('file_url', rows[0])
        self.assertTrue(rows[0]['file_url'])


class TestAttachmentPermissions(AttachmentBase):

    def test_upload_to_invisible_ticket_is_403(self):
        resp = self.upload(self.stranger)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(TicketAttachment.objects.exists())

    def test_list_is_scoped(self):
        self.create_attachment(self.ticket)
        resp = self.api_get('/api/attachments/', user=self.stranger)
        self.assertEqual(self.get_results(resp), [])
        resp = self.api_get('/api/attachments/', user=self.viewer)
        self.assertEqual(len(self.get_results(resp)), 1)

    def test_delete_by_uploader(self):
        att = self.create_attachment(self.ticket, uploaded_by=self.viewer)
        self.assertEqual(self.api_delete(f'/api/attachments/{att.id}/', user=self.viewer).status_code, 204)

    def test_delete_by_staff(self):
        att = self.create_attachment(self.ticket, uploaded_by=self.viewer)
        self.assertEqual(self.api_delete(f'/api/attachments/{att.id}/', user=self.admin).status_code, 204)

    def test_delete_by_editor(self):
        att = self.create_attachment(self.ticket, uploaded_by=self.viewer)
        # owner created the ticket, so can edit it
        self.assertEqual(self.api_delete(f'/api/attachments/{att.id}/', user=self.owner).status_code, 204)

    def test_delete_by_other_viewer_is_403(self):
        att = self.create_attachment(self.ticket, uploaded_by=self.owner)
        resp = self.api_delete(f'/api/attachments/{att.id}/', user=self.viewer)
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(TicketAttachment.objects.filter(id=att.id).exists())

    def test_delete_by_stranger_is_404(self):
        att = self.create_attachment(self.ticket, uploaded_by=self.owner)
        self.assertEqual(self.api_delete(f'/api/attachments/{att.id}/', user=self.stranger).status_code, 404)
