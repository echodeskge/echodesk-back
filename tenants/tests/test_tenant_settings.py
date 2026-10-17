"""
Tests for the workspace settings endpoints and the profile permissions list:
  - GET/PATCH /api/tenant-settings/
  - POST      /api/tenant-settings/upload-logo/
  - DELETE    /api/tenant-settings/remove-logo/
  - GET       /api/auth/profile/  (permissions_list)
"""
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from tenants.models import Tenant
from users.tests.conftest import EchoDeskTenantTestCase

SETTINGS_URL = '/api/tenant-settings/'
UPLOAD_URL = '/api/tenant-settings/upload-logo/'
REMOVE_URL = '/api/tenant-settings/remove-logo/'
PROFILE_URL = '/api/auth/profile/'


class LocalMediaMixin:
    """Keep uploads on a temp directory (production storage is S3)."""

    def setUp(self):
        super().setUp()
        self._media_dir = tempfile.mkdtemp(prefix='echodesk-logo-')
        self._storage_override = override_settings(
            DEFAULT_FILE_STORAGE='django.core.files.storage.FileSystemStorage',
            MEDIA_ROOT=self._media_dir,
            MEDIA_URL='/media/',
        )
        self._storage_override.enable()

    def tearDown(self):
        self._storage_override.disable()
        shutil.rmtree(self._media_dir, ignore_errors=True)
        super().tearDown()


def current_tenant():
    return Tenant.objects.get(schema_name='test')


class TestTenantSettings(EchoDeskTenantTestCase):

    def test_get_returns_ticket_rules_and_language(self):
        user = self.create_user(email='ts-reader@test.com')
        resp = self.api_get(SETTINGS_URL, user=user)
        self.assertEqual(resp.status_code, 200)
        tenant = current_tenant()
        self.assertEqual(resp.data['company_name'], tenant.name)
        self.assertIsNone(resp.data['logo'])
        self.assertEqual(resp.data['min_users_per_ticket'], tenant.min_users_per_ticket)
        self.assertEqual(resp.data['only_superadmin_can_delete_tickets'], tenant.only_superadmin_can_delete_tickets)
        self.assertEqual(resp.data['preferred_language'], tenant.preferred_language)

    def test_staff_can_patch(self):
        staff = self.create_user(email='ts-staff@test.com', is_staff=True)
        resp = self.api_patch(SETTINGS_URL, {
            'min_users_per_ticket': 2,
            'only_superadmin_can_delete_tickets': True,
            'preferred_language': 'ka',
        }, user=staff)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['min_users_per_ticket'], 2)
        self.assertTrue(resp.data['only_superadmin_can_delete_tickets'])
        self.assertEqual(resp.data['preferred_language'], 'ka')
        tenant = current_tenant()
        self.assertEqual(tenant.min_users_per_ticket, 2)
        self.assertTrue(tenant.only_superadmin_can_delete_tickets)
        self.assertEqual(tenant.preferred_language, 'ka')

    def test_partial_patch_leaves_other_fields(self):
        before = current_tenant()
        user = self.create_user(email='ts-flag@test.com', can_manage_settings=True)
        resp = self.api_patch(SETTINGS_URL, {'min_users_per_ticket': 3}, user=user)
        self.assertEqual(resp.status_code, 200)
        after = current_tenant()
        self.assertEqual(after.min_users_per_ticket, 3)
        self.assertEqual(after.only_superadmin_can_delete_tickets, before.only_superadmin_can_delete_tickets)
        self.assertEqual(after.preferred_language, before.preferred_language)

    def _set_delete_restriction(self, value, min_users=0):
        Tenant.objects.filter(schema_name='test').update(
            only_superadmin_can_delete_tickets=value, min_users_per_ticket=min_users,
        )

    def test_admin_role_can_patch_other_fields(self):
        self._set_delete_restriction(True)
        admin_role = self.create_user(email='ts-adminrole@test.com', role='admin')
        resp = self.api_patch(SETTINGS_URL, {'min_users_per_ticket': 4}, user=admin_role)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(current_tenant().min_users_per_ticket, 4)

    def test_admin_role_cannot_turn_off_delete_restriction(self):
        # Review repro: a non-staff manage_settings user PATCHes the toggle
        # off. The whole request is rejected and nothing is saved.
        self._set_delete_restriction(True, min_users=1)
        for user in (self.create_user(email='ts-adminrole-off@test.com', role='admin'),
                     self.create_user(email='ts-flag-off@test.com', can_manage_settings=True)):
            resp = self.api_patch(SETTINGS_URL, {
                'min_users_per_ticket': 5,
                'only_superadmin_can_delete_tickets': False,
            }, user=user)
            self.assertEqual(resp.status_code, 403, resp.data)
            self.assertIn('only_superadmin_can_delete_tickets', resp.data)
            self.assertIn('error', resp.data)
            tenant = current_tenant()
            self.assertTrue(tenant.only_superadmin_can_delete_tickets)
            self.assertEqual(tenant.min_users_per_ticket, 1)

    def test_admin_role_cannot_turn_on_delete_restriction(self):
        self._set_delete_restriction(False)
        admin_role = self.create_user(email='ts-adminrole-on@test.com', role='admin')
        resp = self.api_patch(SETTINGS_URL, {'only_superadmin_can_delete_tickets': True}, user=admin_role)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(current_tenant().only_superadmin_can_delete_tickets)

    def test_admin_role_may_resend_current_delete_restriction(self):
        # The web general-settings form always sends the toggle with
        # min_users_per_ticket; an unchanged value must not block the save.
        self._set_delete_restriction(True)
        admin_role = self.create_user(email='ts-adminrole-same@test.com', role='admin')
        resp = self.api_patch(SETTINGS_URL, {
            'min_users_per_ticket': 2,
            'only_superadmin_can_delete_tickets': True,
        }, user=admin_role)
        self.assertEqual(resp.status_code, 200, resp.data)
        tenant = current_tenant()
        self.assertEqual(tenant.min_users_per_ticket, 2)
        self.assertTrue(tenant.only_superadmin_can_delete_tickets)

    def test_staff_and_superuser_can_change_delete_restriction(self):
        self._set_delete_restriction(True)
        staff = self.create_user(email='ts-staff-off@test.com', is_staff=True)
        resp = self.api_patch(SETTINGS_URL, {'only_superadmin_can_delete_tickets': False}, user=staff)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(current_tenant().only_superadmin_can_delete_tickets)
        su = self.create_user(email='ts-su-on@test.com', is_superuser=True)
        resp = self.api_patch(SETTINGS_URL, {'only_superadmin_can_delete_tickets': True}, user=su)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(current_tenant().only_superadmin_can_delete_tickets)

    def test_agent_and_manager_cannot_patch(self):
        before = current_tenant().min_users_per_ticket
        for user in (self.create_user(email='ts-agent@test.com'),
                     self.create_manager(email='ts-mgr@test.com')):
            resp = self.api_patch(SETTINGS_URL, {'min_users_per_ticket': 7}, user=user)
            self.assertEqual(resp.status_code, 403)
        self.assertEqual(current_tenant().min_users_per_ticket, before)

    def test_validation_errors(self):
        staff = self.create_user(email='ts-val@test.com', is_staff=True)
        self.assertEqual(self.api_patch(SETTINGS_URL, {'min_users_per_ticket': -1}, user=staff).status_code, 400)
        self.assertEqual(self.api_patch(SETTINGS_URL, {'preferred_language': 'xx'}, user=staff).status_code, 400)

    def test_read_only_fields_ignored(self):
        staff = self.create_user(email='ts-ro@test.com', is_staff=True)
        name = current_tenant().name
        resp = self.api_patch(SETTINGS_URL, {'company_name': 'Hijacked', 'logo': 'x'}, user=staff)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(current_tenant().name, name)

    def test_unauthenticated(self):
        self.assertIn(self.api_get(SETTINGS_URL).status_code, (401, 403))
        self.assertIn(self.api_patch(SETTINGS_URL, {'min_users_per_ticket': 1}).status_code, (401, 403))


class TestTenantLanguageUpdate(EchoDeskTenantTestCase):
    URL = '/api/tenant/language/update/'

    def test_agent_and_manager_cannot_change_tenant_language(self):
        before = current_tenant().preferred_language
        target = 'ru' if before != 'ru' else 'ka'
        for user in (self.create_user(email='lang-agent@test.com'),
                     self.create_manager(email='lang-mgr@test.com')):
            resp = self.api_put(self.URL, {'preferred_language': target}, user=user)
            self.assertEqual(resp.status_code, 403)
        self.assertEqual(current_tenant().preferred_language, before)

    def test_staff_and_admin_role_can_change_tenant_language(self):
        staff = self.create_user(email='lang-staff@test.com', is_staff=True)
        resp = self.api_put(self.URL, {'preferred_language': 'ka'}, user=staff)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(current_tenant().preferred_language, 'ka')
        admin_role = self.create_user(email='lang-adminrole@test.com', role='admin')
        resp = self.api_patch(self.URL, {'preferred_language': 'en'}, user=admin_role)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(current_tenant().preferred_language, 'en')


class TestTenantLogo(LocalMediaMixin, EchoDeskTenantTestCase):

    def logo(self, name='logo.webp', content_type='image/webp'):
        return SimpleUploadedFile(name, b'RIFF\x00\x00\x00\x00WEBPVP8 ', content_type=content_type)

    def upload(self, user, file):
        client = self.authenticated_client(user)
        return client.post(UPLOAD_URL, {'logo': file}, format='multipart', HTTP_HOST='tenant.test.com')

    def test_agent_cannot_upload_or_remove(self):
        agent = self.create_user(email='logo-agent@test.com')
        resp = self.upload(agent, self.logo())
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(current_tenant().logo)
        self.assertEqual(self.api_delete(REMOVE_URL, user=agent).status_code, 403)

    def test_staff_can_upload_webp_then_remove(self):
        staff = self.create_user(email='logo-staff@test.com', is_staff=True)
        resp = self.upload(staff, self.logo())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn('logo_url', resp.data)
        self.assertTrue(current_tenant().logo)
        settings_resp = self.api_get(SETTINGS_URL, user=staff)
        self.assertTrue(settings_resp.data['logo'])
        resp = self.api_delete(REMOVE_URL, user=staff)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(current_tenant().logo)

    def test_manage_settings_user_can_upload_png(self):
        user = self.create_user(email='logo-flag@test.com', can_manage_settings=True)
        resp = self.upload(user, self.logo('logo.png', 'image/png'))
        self.assertEqual(resp.status_code, 200)

    def test_rejects_other_types(self):
        staff = self.create_user(email='logo-type@test.com', is_staff=True)
        resp = self.upload(staff, SimpleUploadedFile('logo.svg', b'<svg/>', content_type='image/svg+xml'))
        self.assertEqual(resp.status_code, 400)


class TestProfilePermissionsList(EchoDeskTenantTestCase):

    def test_manager_role_grants_are_listed(self):
        manager = self.create_manager(email='perm-mgr@test.com')
        resp = self.api_get(PROFILE_URL, user=manager)
        self.assertEqual(resp.status_code, 200)
        perms = resp.data['permissions_list']
        self.assertEqual(perms, sorted(perms))
        for codename in ('manage_columns', 'manage_tags', 'create_boards', 'edit_boards', 'view_boards'):
            self.assertIn(codename, perms)
        self.assertNotIn('manage_settings', perms)
        self.assertNotIn('delete_boards', perms)
        self.assertEqual(sorted(resp.data['all_permissions']), perms)

    def test_individual_flags_and_admin_role(self):
        agent = self.create_user(email='perm-agent@test.com', can_manage_settings=True)
        perms = self.api_get(PROFILE_URL, user=agent).data['permissions_list']
        self.assertIn('manage_settings', perms)
        self.assertNotIn('manage_columns', perms)
        admin_role = self.create_user(email='perm-admin@test.com', role='admin')
        perms = self.api_get(PROFILE_URL, user=admin_role).data['permissions_list']
        self.assertIn('manage_settings', perms)
        self.assertIn('delete_boards', perms)

    def test_plain_agent_has_no_admin_permissions(self):
        agent = self.create_user(email='perm-plain@test.com')
        perms = self.api_get(PROFILE_URL, user=agent).data['permissions_list']
        self.assertIsInstance(perms, list)
        for codename in ('manage_columns', 'manage_tags', 'manage_settings', 'edit_boards'):
            self.assertNotIn(codename, perms)
