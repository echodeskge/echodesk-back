"""
Sentry ECHODESK-BACKEND-6S: "Failed to get domains: Vercel Project not found".

The admin settings ``domains`` action polled a ``vercel_project_id`` whose
project no longer existed on Vercel, logging an error on every poll. Now the
service reports the 404 as ``code='project_not_found'`` at warning level and
the view forgets the stale id and answers 409 ``vercel_project_missing``.
"""
from unittest.mock import MagicMock, patch


from ecommerce_crm.models import EcommerceSettings
from ecommerce_crm.services.vercel_deployment import VercelDeploymentService
from ecommerce_crm.tests.base import EcommerceTenantTestCase

DOMAINS_URL = '/api/ecommerce/admin/settings/domains/'
SERVICE_LOGGER = 'ecommerce_crm.services.vercel_deployment'


def _response(status_code, payload):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = 'x'
    resp.json.return_value = payload
    return resp


def _service():
    """A service instance without touching VERCEL_TOKEN settings."""
    with patch.object(VercelDeploymentService, '__init__', return_value=None):
        service = VercelDeploymentService()
    service.token, service.team_id, service.headers = 'test-token', '', {}
    return service


class TestServiceGetDomainsNotFound(EcommerceTenantTestCase):

    def test_404_not_found_is_flagged_and_logged_at_warning(self):
        payload = {'error': {'code': 'not_found', 'message': 'Project not found'}}
        with patch(f'{SERVICE_LOGGER}.requests.get', return_value=_response(404, payload)), \
                self.assertLogs(SERVICE_LOGGER, level='WARNING') as logs:
            result = _service().get_domains('prj_gone')
        self.assertEqual(result, {
            'success': False, 'error': 'Project not found',
            'code': VercelDeploymentService.PROJECT_NOT_FOUND,
        })
        self.assertEqual([r.levelname for r in logs.records], ['WARNING'])

    def test_other_failures_still_log_error_without_the_flag(self):
        payload = {'error': {'code': 'forbidden', 'message': 'Not authorized'}}
        with patch(f'{SERVICE_LOGGER}.requests.get', return_value=_response(403, payload)), \
                self.assertLogs(SERVICE_LOGGER, level='ERROR'):
            result = _service().get_domains('prj_x')
        self.assertEqual(result, {'success': False, 'error': 'Not authorized'})
        self.assertNotIn('code', result)


class TestDomainsActionProjectMissing(EcommerceTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='vercel-admin@test.com')
        self.settings_obj, _ = EcommerceSettings.objects.get_or_create(
            tenant=self.tenant, defaults={'store_name': 'Shop'},
        )
        self.settings_obj.vercel_project_id = 'prj_stale'
        self.settings_obj.ecommerce_frontend_url = 'https://shop.example.com'
        self.settings_obj.deployment_status = 'deployed'
        self.settings_obj.save()

    def _get_domains(self, result):
        with patch.object(VercelDeploymentService, '__init__', return_value=None), \
                patch.object(VercelDeploymentService, 'get_domains', return_value=result):
            return self.api_get(DOMAINS_URL, user=self.admin)

    def test_missing_project_resets_deployment_and_returns_409(self):
        with self.assertLogs('ecommerce_crm.views', level='WARNING') as logs:
            resp = self._get_domains({
                'success': False, 'error': 'Project not found',
                'code': VercelDeploymentService.PROJECT_NOT_FOUND,
            })
        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertEqual(resp.data['error'], 'vercel_project_missing')
        self.assertFalse(resp.data['success'])
        self.assertEqual([r.levelname for r in logs.records], ['WARNING'])

        self.settings_obj.refresh_from_db()
        self.assertIsNone(self.settings_obj.vercel_project_id)
        self.assertIsNone(self.settings_obj.ecommerce_frontend_url)
        self.assertEqual(self.settings_obj.deployment_status, 'pending')

        # The next poll no longer hits Vercel at all.
        with patch.object(VercelDeploymentService, '__init__', return_value=None) as init:
            resp = self.api_get(DOMAINS_URL, user=self.admin)
        self.assertEqual(resp.status_code, 400)
        init.assert_not_called()

    def test_other_failures_keep_the_project_id(self):
        resp = self._get_domains({'success': False, 'error': 'Not authorized'})
        self.assertEqual(resp.status_code, 500)
        self.settings_obj.refresh_from_db()
        self.assertEqual(self.settings_obj.vercel_project_id, 'prj_stale')
        self.assertEqual(self.settings_obj.deployment_status, 'deployed')
