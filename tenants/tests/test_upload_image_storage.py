"""Sentry ECHODESK-BACKEND-72/73/74: an object-store failure behind
``POST /api/upload/image/`` (503 "Slow Down", GatewayTimeout, a bare
BadRequest during an outage) is upstream, not a code bug — the client gets
a 503 it can retry and the log stays at warning level."""
import logging
from unittest.mock import patch

from botocore.exceptions import ClientError, EndpointConnectionError
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile

from users.tests.conftest import EchoDeskTenantTestCase

URL = '/api/upload/image/'


class TestUploadImageStorageErrors(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user = self.create_user(email='uploader@test.com')

    def upload(self):
        client = self.authenticated_client(self.user)
        image = SimpleUploadedFile('photo.png', b'\x89PNG\r\n\x1a\n', content_type='image/png')
        return client.post(URL, {'image': image}, format='multipart', HTTP_HOST='tenant.test.com')

    @staticmethod
    def _errors(cm):
        return [r for r in cm.records if r.levelno >= logging.ERROR]

    def test_storage_client_error_is_503_and_warning(self):
        err = ClientError(
            {'Error': {'Code': 'ServiceUnavailable', 'Message': 'Slow Down'}}, 'PutObject'
        )
        with patch.object(default_storage, 'save', side_effect=err):
            with self.assertLogs('tenants.views', level='WARNING') as cm:
                resp = self.upload()

        self.assertEqual(resp.status_code, 503, resp.data)
        self.assertIn('temporarily unavailable', resp.data['error'])
        self.assertNotIn('PutObject', resp.data['error'])
        self.assertEqual(self._errors(cm), [])
        self.assertTrue(any('PutObject' in r.getMessage() for r in cm.records))

    def test_storage_connection_error_is_503(self):
        err = EndpointConnectionError(endpoint_url='https://fsn1.your-objectstorage.com')
        with patch.object(default_storage, 'save', side_effect=err):
            with self.assertLogs('tenants.views', level='WARNING') as cm:
                resp = self.upload()

        self.assertEqual(resp.status_code, 503, resp.data)
        self.assertEqual(self._errors(cm), [])

    def test_unexpected_exception_still_logs_error_and_500(self):
        with patch.object(default_storage, 'save', side_effect=ValueError('boom')):
            with self.assertLogs('tenants.views', level='ERROR') as cm:
                resp = self.upload()

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(len(self._errors(cm)), 1)
