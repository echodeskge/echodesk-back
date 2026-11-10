"""``GET /api/pbx/call-routing/`` (BK-03 + BK-01): resolution is scoped
to the calling PBX's tenant, identified by ``crm.pbx_auth``. No
credentials / unknown token → 401, DID the tenant does not own → 404,
never another tenant's route; nothing is written on a GET.
"""
import json
from unittest.mock import patch

from django.test import override_settings

from crm.models import PbxServer, PbxSettings, SipConfiguration
from crm.pbx_auth import PbxAuth
from crm.tests.conftest import CrmTestCase

URL = '/api/pbx/call-routing/'


def _body(resp):
    return json.dumps(resp.data, default=str)


class RoutingTestCase(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='routing-admin@test.com')

    def _config(self, **kwargs):
        defaults = {'is_default': True, 'is_active': True, 'phone_number': '+995322421219'}
        defaults.update(kwargs)
        return self.create_sip_config(created_by=self.admin, **defaults)

    def _pbx(self, **kwargs):
        defaults = {
            'name': 'PBX', 'fqdn': 'pbx.test.com', 'realtime_db_host': 'db',
            'realtime_db_name': 'asterisk_test', 'realtime_db_user': 'rw',
            'status': PbxServer.STATUS_ACTIVE,
        }
        defaults.update(kwargs)
        return PbxServer.objects.create(**defaults)

    def get(self, did='+995322421219', auth=True, **extra):
        """GET as this tenant's PBX (``X-PBX-Token`` = its api_token) unless
        the test passes its own credentials or ``auth=False``."""
        if auth and 'HTTP_X_PBX_TOKEN' not in extra and 'HTTP_AUTHORIZATION' not in extra:
            extra.update(self.pbx_headers())
        return self.client.get(URL, {'did': did}, HTTP_HOST='tenant.test.com', **extra)


class TestTenantScopedResolution(RoutingTestCase):

    def test_known_did_resolves_in_own_tenant(self):
        config = self._config()
        self.create_phone_assignment(user=self.admin, sip_config=config, extension='100')
        resp = self.get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['tenant_schema'], self.tenant.schema_name)
        self.assertIn('100', resp.data['extensions'])

    def test_unknown_did_without_default_config_is_404(self):
        """A non-default configuration that does not own the DID is not
        picked "because it is the first one"."""
        self._config(is_default=False, phone_number='+995322421219')
        resp = self.get('+995322000000')
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('extensions', resp.data)
        self.assertNotIn('tenant_schema', resp.data)

    def test_unknown_did_with_default_config_uses_it(self):
        self._config(is_default=True, phone_number='')
        resp = self.get('+995322000000')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['tenant_schema'], self.tenant.schema_name)

    def test_no_config_is_404(self):
        self.assertEqual(self.get().status_code, 404)

    def test_short_did_does_not_suffix_match(self):
        """Fewer than 7 digits never suffix-match a configuration (an
        empty suffix would match every phone number)."""
        self._config(is_default=False, phone_number='+995322421219')
        resp = self.get('123')
        self.assertEqual(resp.status_code, 404)

    def test_get_does_not_create_pbx_settings(self):
        self._config()
        self.assertEqual(PbxSettings.objects.count(), 0)
        resp = self.get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(PbxSettings.objects.count(), 0)
        # Model defaults are what an unsaved PbxSettings would give.
        self.assertTrue(resp.data['is_working_hours'])
        self.assertEqual(resp.data['action'], 'queue')
        self.assertIn('greeting', resp.data['sounds'])

    def test_existing_pbx_settings_are_used(self):
        config = self._config()
        self.create_pbx_settings(sip_config=config, after_hours_action='forward',
                                 forward_number='+995555000000')
        resp = self.get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['after_hours_action'], 'forward')
        self.assertEqual(resp.data['forward_number'], '+995555000000')
        self.assertEqual(PbxSettings.objects.count(), 1)

    def test_missing_did_is_400(self):
        self._config()
        resp = self.client.get(URL, HTTP_HOST='tenant.test.com', **self.pbx_headers())
        self.assertEqual(resp.status_code, 400)


class TestTokenScoping(RoutingTestCase):

    def test_unknown_token_is_401_and_resolves_nothing(self):
        """An unknown X-PBX-Token never falls back to scanning tenants."""
        self._config()
        with patch('crm.views._resolve_routing_for_tenant') as resolve:
            resp = self.get(HTTP_X_PBX_TOKEN='not-a-real-token')
        self.assertEqual(resp.status_code, 401)
        resolve.assert_not_called()
        body = _body(resp)
        for leaked in ('extensions', 'sounds', 'tenant_schema', 'queue'):
            self.assertNotIn(leaked, body)

    def test_api_token_identifies_tenant(self):
        self._config()
        pbx = self._pbx()
        resp = self.get(HTTP_X_PBX_TOKEN=pbx.api_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['tenant_schema'], self.tenant.schema_name)

    def test_legacy_enrollment_token_identifies_active_tenant(self):
        self._config()
        pbx = self._pbx()
        resp = self.get(HTTP_X_PBX_TOKEN=pbx.enrollment_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['tenant_schema'], self.tenant.schema_name)

    def test_token_with_unknown_did_is_404_not_another_route(self):
        self._config(is_default=False, phone_number='+995322421219')
        pbx = self._pbx()
        resp = self.get('+995322000000', HTTP_X_PBX_TOKEN=pbx.enrollment_token)
        self.assertEqual(resp.status_code, 404)

    def test_token_tenant_only_never_the_request_tenant_or_a_scan(self):
        """The token's tenant is the only schema resolution runs in: a miss
        there is a 404, not a retry in the request's tenant (which owns
        the DID here) or in any other."""
        self._config()  # the request's own tenant owns the DID ...
        acme = PbxAuth(schema='acme', pbx=None, via='api_token')
        with patch('crm.pbx_auth.resolve_pbx', return_value=acme), \
                patch('crm.views._resolve_routing_for_tenant', return_value=None) as resolve:
            resp = self.get(HTTP_X_PBX_TOKEN='acme-token')
        # ... but the PBX belongs to "acme", so only acme is consulted.
        resolve.assert_called_once_with('acme', '+995322421219')
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('extensions', _body(resp))

    @override_settings(PBX_SHARED_SECRET='shared-secret-value')
    def test_shared_secret_exact_bearer_only(self):
        self._config()
        self.assertEqual(self.get(auth=False).status_code, 401)
        self.assertEqual(self.get(HTTP_AUTHORIZATION='Bearer wrong').status_code, 401)
        # The old ``endswith`` check accepted any prefix.
        self.assertEqual(
            self.get(HTTP_AUTHORIZATION='Bearer junkshared-secret-value').status_code, 401)
        self.assertEqual(self.get(HTTP_AUTHORIZATION='shared-secret-value').status_code, 401)
        resp = self.get(HTTP_AUTHORIZATION='Bearer shared-secret-value')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['tenant_schema'], self.tenant.schema_name)

    @override_settings(PBX_SHARED_SECRET='')
    def test_no_credentials_is_401_even_on_the_tenant_host(self):
        """The tenant never comes from the host name alone: without a
        token and with no shared secret configured the request is refused
        (the host-only path of the old code)."""
        self._config()
        with patch('crm.views._resolve_routing_for_tenant') as resolve:
            resp = self.get(auth=False)
        self.assertEqual(resp.status_code, 401)
        resolve.assert_not_called()
        self.assertNotIn('tenant_schema', _body(resp))

    def test_resolution_error_is_generic_500(self):
        self._config()
        with patch('crm.views._resolve_routing_for_tenant', side_effect=RuntimeError('db down')):
            resp = self.get()
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn('db down', _body(resp))
