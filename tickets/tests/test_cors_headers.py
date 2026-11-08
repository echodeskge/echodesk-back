"""
CORS settings fix (tickets redesign E11 / R4, BE-05).

django-cors-headers reads CORS_ALLOW_HEADERS / CORS_ALLOW_METHODS. The old
settings used names the library never reads (CORS_ALLOWED_HEADERS, ...), so
custom headers such as X-Client-Mutation would fail the browser preflight.
"""
from corsheaders.defaults import default_headers
from django.conf import settings

from tickets.tests.conftest import TicketTestCase

REQUESTED_HEADERS = 'x-client-mutation, authorization, x-tenant-subdomain, x-tenant-domain, cache-control'


def _split(value):
    return {part.strip().lower() for part in (value or '').split(',') if part.strip()}


class TestCorsSettings(TicketTestCase):

    def test_allow_headers_keep_defaults_and_add_custom_headers(self):
        allowed = {h.lower() for h in settings.CORS_ALLOW_HEADERS}
        self.assertTrue(set(default_headers) <= allowed)
        for header in ('x-client-mutation', 'x-tenant-subdomain', 'x-tenant-domain', 'cache-control', 'dnt'):
            self.assertIn(header, allowed)

    def test_allow_methods(self):
        self.assertEqual(
            {m.upper() for m in settings.CORS_ALLOW_METHODS},
            {'DELETE', 'GET', 'OPTIONS', 'PATCH', 'POST', 'PUT'},
        )

    def test_dead_setting_names_are_gone(self):
        for name in ('CORS_ALLOW_ALL_HEADERS', 'CORS_ALLOWED_HEADERS', 'CORS_ALLOWED_METHODS'):
            self.assertFalse(hasattr(settings, name), name)

    def test_origin_policy_unchanged(self):
        self.assertFalse(settings.CORS_ALLOW_ALL_ORIGINS)
        self.assertTrue(settings.CORS_ALLOW_CREDENTIALS)


class TestCorsPreflight(TicketTestCase):
    url = '/api/tickets/'

    @property
    def allowed_origin(self):
        return f'https://acme.{settings.MAIN_DOMAIN}'

    def _preflight(self, origin, method='PATCH', headers=REQUESTED_HEADERS):
        return self.client.options(
            self.url,
            HTTP_HOST='tenant.test.com',
            HTTP_ORIGIN=origin,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD=method,
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS=headers,
        )

    def test_preflight_from_allowed_origin_allows_every_requested_header(self):
        resp = self._preflight(self.allowed_origin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Access-Control-Allow-Origin'], self.allowed_origin)
        self.assertEqual(resp['Access-Control-Allow-Credentials'], 'true')
        allowed = _split(resp['Access-Control-Allow-Headers'])
        self.assertTrue(_split(REQUESTED_HEADERS) <= allowed, allowed)

    def test_preflight_allows_patch_and_delete(self):
        resp = self._preflight(self.allowed_origin, method='DELETE')
        methods = {m.upper() for m in _split(resp['Access-Control-Allow-Methods'])}
        self.assertIn('PATCH', methods)
        self.assertIn('DELETE', methods)

    def test_preflight_from_disallowed_origin_gets_no_allow_origin(self):
        resp = self._preflight('https://evil.example')
        self.assertNotIn('Access-Control-Allow-Origin', resp)

    def test_actual_request_from_allowed_origin_carries_allow_origin(self):
        admin = self.create_admin(email='cors-admin@test.com')
        resp = self.api_get(self.url, user=admin, HTTP_ORIGIN=self.allowed_origin,
                            HTTP_X_CLIENT_MUTATION='3f0c1a52-8f6e-4d7e-9c1b-0a3b5c7d9e11')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Access-Control-Allow-Origin'], self.allowed_origin)
