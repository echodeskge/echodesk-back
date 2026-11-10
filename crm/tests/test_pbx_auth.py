"""PBX → backend authentication (``crm.pbx_auth``).

The four webhooks and ``/api/pbx/call-routing/`` accept only:

* ``X-PBX-Token`` = ``PbxServer.api_token`` (active / provisioning server),
* ``X-PBX-Token`` = ``PbxServer.enrollment_token`` of an *active* server
  (legacy drop-ins; logged as deprecated),
* ``Authorization: Bearer <PBX_SHARED_SECRET>`` while that setting is set,
  on a tenant host.

Everything else is 401, the tenant is the token's tenant, and nothing
about the failure (or a 500) leaks into the response body.
"""
import hashlib
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings

from crm import pbx_auth
from crm.models import CallLog, PbxServer
from crm.tests.conftest import CrmTestCase

WEBHOOKS = (
    '/api/webhooks/sip/',
    '/api/webhooks/recording/',
    '/api/webhooks/call-rating/',
    '/api/webhooks/call-recording-url/',
)
ROUTING = '/api/pbx/call-routing/?did=%2B995322421219'


class _PbxAuthMixin:

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='auth-admin@test.com')
        self.config = self.create_sip_config(
            created_by=self.admin, is_default=True, phone_number='+995322421219',
        )
        self.pbx = self.create_pbx_server()
        self.addCleanup(self._forget_tokens)

    def _forget_tokens(self):
        for token in (self.pbx.api_token, self.pbx.enrollment_token):
            cache.delete(pbx_auth._cache_key(token))

    def sip(self, call, **headers):
        return self.api_post(
            '/api/webhooks/sip/',
            {'event_type': 'call_answered', 'sip_call_id': call.sip_call_id},
            **headers,
        )

    def routing(self, **headers):
        return self.client.get(ROUTING, HTTP_HOST='tenant.test.com', **headers)


class TestPbxToken(_PbxAuthMixin, CrmTestCase):

    def test_api_token_authenticates_webhook_and_routing(self):
        call = self.create_call_log(handled_by=self.admin, sip_call_id='sip-auth-1')
        resp = self.sip(call, HTTP_X_PBX_TOKEN=self.pbx.api_token)
        self.assertEqual(resp.status_code, 200, resp.data)
        call.refresh_from_db()
        self.assertEqual(call.status, 'answered')

        resp = self.routing(HTTP_X_PBX_TOKEN=self.pbx.api_token)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['tenant_schema'], self.tenant.schema_name)

    def test_query_param_fallback(self):
        resp = self.client.get(
            f'{ROUTING}&pbx_token={self.pbx.api_token}', HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, 200)

    def test_no_credentials_is_401_everywhere(self):
        for url in WEBHOOKS:
            resp = self.api_post(url, {'event_type': 'call_answered', 'sip_call_id': 'x'})
            self.assertEqual(resp.status_code, 401, url)
            self.assertEqual(resp.data, {'error': 'Unauthorized'})
        resp = self.routing()
        self.assertEqual(resp.status_code, 401)

    def test_unknown_token_is_401(self):
        call = self.create_call_log(handled_by=self.admin, sip_call_id='sip-auth-2')
        resp = self.sip(call, HTTP_X_PBX_TOKEN='not-a-token')
        self.assertEqual(resp.status_code, 401)
        call.refresh_from_db()
        self.assertEqual(call.status, 'ringing')

    def test_session_or_jwt_auth_is_not_accepted(self):
        # The webhooks have no user authentication: a logged-in admin
        # without the PBX token is still refused.
        resp = self.api_post(
            '/api/webhooks/sip/',
            {'event_type': 'call_answered', 'sip_call_id': 'x'}, user=self.admin,
        )
        self.assertEqual(resp.status_code, 401)

    def test_api_token_rejected_unless_active_or_provisioning(self):
        for pbx_status, expected in (
            (PbxServer.STATUS_PENDING, 401),
            (PbxServer.STATUS_REVOKED, 401),
            (PbxServer.STATUS_ERROR, 401),
            (PbxServer.STATUS_PROVISIONING, 200),
            (PbxServer.STATUS_ACTIVE, 200),
        ):
            PbxServer.objects.filter(pk=self.pbx.pk).update(status=pbx_status)
            resp = self.routing(HTTP_X_PBX_TOKEN=self.pbx.api_token)
            self.assertEqual(resp.status_code, expected, pbx_status)

    def test_enrollment_token_legacy_only_for_active_servers(self):
        with self.assertLogs('crm.pbx_auth', level='WARNING') as logs:
            resp = self.routing(HTTP_X_PBX_TOKEN=self.pbx.enrollment_token)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any('deprecated' in line for line in logs.output), logs.output)

        for pbx_status in (PbxServer.STATUS_PENDING, PbxServer.STATUS_PROVISIONING,
                           PbxServer.STATUS_REVOKED):
            PbxServer.objects.filter(pk=self.pbx.pk).update(status=pbx_status)
            resp = self.routing(HTTP_X_PBX_TOKEN=self.pbx.enrollment_token)
            self.assertEqual(resp.status_code, 401, pbx_status)

    def test_cache_key_is_the_token_hash_never_the_token(self):
        token = self.pbx.api_token
        self.assertEqual(self.routing(HTTP_X_PBX_TOKEN=token).status_code, 200)
        key = pbx_auth.CACHE_PREFIX + hashlib.sha256(token.encode()).hexdigest()
        self.assertEqual(tuple(cache.get(key)), (self.tenant.schema_name, self.pbx.pk))
        self.assertIsNone(cache.get(pbx_auth.CACHE_PREFIX + token))
        self.assertIsNone(cache.get(f'pbx_token_tenant:{token}'))
        self.assertIsNone(cache.get(f'pbx_token_lookup:{token}'))

    def test_cached_mapping_is_reverified(self):
        old_token = self.pbx.api_token
        self.assertEqual(self.routing(HTTP_X_PBX_TOKEN=old_token).status_code, 200)
        # Rotation: the old token stops working at once despite the cache.
        self.pbx.regenerate_api_token()
        self.pbx.save(update_fields=['api_token'])
        self.assertEqual(self.routing(HTTP_X_PBX_TOKEN=old_token).status_code, 401)
        self.assertEqual(self.routing(HTTP_X_PBX_TOKEN=self.pbx.api_token).status_code, 200)
        # Revocation too.
        PbxServer.objects.filter(pk=self.pbx.pk).update(status=PbxServer.STATUS_REVOKED)
        self.assertEqual(self.routing(HTTP_X_PBX_TOKEN=self.pbx.api_token).status_code, 401)

    def test_cache_hit_skips_the_tenant_scan(self):
        token = self.pbx.api_token
        self.assertEqual(self.routing(HTTP_X_PBX_TOKEN=token).status_code, 200)
        with patch('crm.pbx_auth._scan_tenants') as scan:
            self.assertEqual(self.routing(HTTP_X_PBX_TOKEN=token).status_code, 200)
        scan.assert_not_called()

    def test_decorator_sets_request_pbx(self):
        seen = {}

        def fake_resolve(tenant_schema, did):
            seen['schema'] = tenant_schema
            return {'is_working_hours': True, 'action': 'queue'}

        with patch('crm.views._resolve_routing_for_tenant', side_effect=fake_resolve):
            resp = self.routing(HTTP_X_PBX_TOKEN=self.pbx.api_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seen['schema'], self.tenant.schema_name)

    def test_installer_accepts_only_the_enrollment_token(self):
        self.assertEqual(
            pbx_auth.find_pbx_by_enrollment_token(self.pbx.enrollment_token),
            (self.tenant.schema_name, self.pbx.pk),
        )
        self.assertEqual(pbx_auth.find_pbx_by_enrollment_token(self.pbx.api_token), (None, None))
        self.assertEqual(pbx_auth.find_pbx_by_enrollment_token(''), (None, None))
        # pending servers are found (the install script serves them).
        PbxServer.objects.filter(pk=self.pbx.pk).update(status=PbxServer.STATUS_PENDING)
        self.assertEqual(
            pbx_auth.find_pbx_by_enrollment_token(self.pbx.enrollment_token)[0],
            self.tenant.schema_name,
        )


class TestSharedSecret(_PbxAuthMixin, CrmTestCase):

    @override_settings(PBX_SHARED_SECRET='legacy-secret')
    def test_bearer_shared_secret_on_tenant_host(self):
        call = self.create_call_log(handled_by=self.admin, sip_call_id='sip-auth-3')
        resp = self.sip(call, HTTP_AUTHORIZATION='Bearer legacy-secret')
        self.assertEqual(resp.status_code, 200, resp.data)
        resp = self.routing(HTTP_AUTHORIZATION='Bearer legacy-secret')
        self.assertEqual(resp.status_code, 200)

    @override_settings(PBX_SHARED_SECRET='legacy-secret')
    def test_wrong_or_malformed_secret_is_401(self):
        call = self.create_call_log(handled_by=self.admin, sip_call_id='sip-auth-4')
        for header in ('Bearer other', 'Bearer legacy-secretX', 'legacy-secret',
                       'Basic legacy-secret', 'Bearer ', 'Bearer xlegacy-secret'):
            resp = self.sip(call, HTTP_AUTHORIZATION=header)
            self.assertEqual(resp.status_code, 401, header)
        call.refresh_from_db()
        self.assertEqual(call.status, 'ringing')

    @override_settings(PBX_SHARED_SECRET='')
    def test_empty_setting_disables_the_legacy_path(self):
        call = self.create_call_log(handled_by=self.admin, sip_call_id='sip-auth-5')
        resp = self.sip(call, HTTP_AUTHORIZATION='Bearer ')
        self.assertEqual(resp.status_code, 401)
        resp = self.sip(call, HTTP_AUTHORIZATION='Bearer legacy-secret')
        self.assertEqual(resp.status_code, 401)

    @override_settings(PBX_SHARED_SECRET='legacy-secret')
    def test_shared_secret_needs_a_tenant_host(self):
        # The public schema has no tenant: the host alone never picks one.
        with patch('crm.pbx_auth.connection') as conn:
            conn.schema_name = 'public'
            resp = self.routing(HTTP_AUTHORIZATION='Bearer legacy-secret')
        self.assertEqual(resp.status_code, 401)

    @override_settings(PBX_SHARED_SECRET='legacy-secret')
    def test_token_wins_over_secret(self):
        resp = self.routing(HTTP_AUTHORIZATION='Bearer legacy-secret',
                            HTTP_X_PBX_TOKEN='bogus')
        self.assertEqual(resp.status_code, 401)


class TestNoCrossTenantRouting(_PbxAuthMixin, CrmTestCase):

    def test_unknown_did_never_scans_other_tenants(self):
        with patch('crm.views._resolve_routing_for_tenant', return_value=None) as resolve:
            resp = self.routing(HTTP_X_PBX_TOKEN=self.pbx.api_token)
        self.assertEqual(resp.status_code, 404)
        resolve.assert_called_once_with(self.tenant.schema_name, '+995322421219')

    def test_no_sip_configuration_is_404(self):
        self.config.delete()
        resp = self.routing(HTTP_X_PBX_TOKEN=self.pbx.api_token)
        self.assertEqual(resp.status_code, 404)

    def test_resolution_error_is_a_generic_500(self):
        with patch('crm.views._resolve_routing_for_tenant',
                   side_effect=RuntimeError('db password hunter2')):
            resp = self.routing(HTTP_X_PBX_TOKEN=self.pbx.api_token)
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn('hunter2', str(resp.data))


class TestWebhookErrorBodies(_PbxAuthMixin, CrmTestCase):

    def test_500_bodies_are_generic(self):
        call = self.create_call_log(
            handled_by=self.admin, sip_call_id='sip-auth-6', direction='inbound',
            caller_number='+995555123456',
        )
        headers = self.pbx_headers(self.pbx)
        boom = RuntimeError('secret detail hunter2')
        cases = (
            ('/api/webhooks/sip/', {'event_type': 'call_answered', 'sip_call_id': call.sip_call_id},
             'crm.views.CallEvent.objects.create'),
            ('/api/webhooks/recording/', {'call_id': str(call.call_id), 'status': 'started'},
             'crm.views.CallRecording.objects.get_or_create'),
            ('/api/webhooks/call-rating/', {'caller_number': '+995555123456', 'rating': 4},
             'crm.views.CallEvent.objects.create'),
            ('/api/webhooks/call-recording-url/',
             {'caller_number': '+995555123456', 'recording_url': 'https://pbx.tenant.test/r.wav'},
             'crm.views.CallLog.save'),
        )
        for url, body, target in cases:
            with patch(target, side_effect=boom):
                resp = self.api_post(url, body, **headers)
            self.assertEqual(resp.status_code, 500, url)
            self.assertNotIn('hunter2', str(resp.data), url)
            self.assertNotIn('RuntimeError', str(resp.data), url)

    def test_sip_webhook_duplicate_sip_call_id_does_not_500(self):
        first = self.create_call_log(handled_by=self.admin, sip_call_id='dup-1', status='ended')
        CallLog.objects.filter(pk=first.pk).update(
            started_at=first.started_at.replace(year=first.started_at.year - 1),
        )
        latest = self.create_call_log(handled_by=self.admin, sip_call_id='dup-1', status='ringing')
        resp = self.sip(latest, **self.pbx_headers(self.pbx))
        self.assertEqual(resp.status_code, 200, resp.data)
        latest.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(latest.status, 'answered')
        self.assertEqual(first.status, 'ended')
