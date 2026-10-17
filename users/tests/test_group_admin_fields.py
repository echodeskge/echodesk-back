"""
P6 tenant groups: is_active / is_booking_staff writable and readable, create
returns the id, available_features validates group_id, permission denials on
member changes.
"""
from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase

TENANT_GROUPS_URL = '/api/tenant-groups/'


class TestTenantGroupAdminFields(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='grp-admin@test.com')
        self.agent = self.create_user(email='grp-agent@test.com')

    def test_create_returns_id_and_accepts_flags(self):
        resp = self.api_post(TENANT_GROUPS_URL, {
            'name': 'P6 Stylists', 'description': 'd', 'is_booking_staff': True, 'is_active': False,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIsInstance(resp.data['id'], int)
        detail = self.api_get(f"{TENANT_GROUPS_URL}{resp.data['id']}/", user=self.admin)
        self.assertTrue(detail.data['is_booking_staff'])
        self.assertFalse(detail.data['is_active'])

    def test_update_flags(self):
        group = self.create_tenant_group(name='P6 flags')
        resp = self.api_patch(f'{TENANT_GROUPS_URL}{group.id}/', {
            'is_active': False, 'is_booking_staff': True,
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        group.refresh_from_db()
        self.assertFalse(group.is_active)
        self.assertTrue(group.is_booking_staff)

    def test_patch_without_flags_keeps_them(self):
        group = self.create_tenant_group(name='P6 keep', is_booking_staff=True)
        self.api_patch(f'{TENANT_GROUPS_URL}{group.id}/', {'description': 'x'}, user=self.admin)
        group.refresh_from_db()
        self.assertTrue(group.is_booking_staff)
        self.assertTrue(group.is_active)

    def test_agent_cannot_change_flags_or_members(self):
        group = self.create_tenant_group(name='P6 locked')
        resp = self.api_patch(f'{TENANT_GROUPS_URL}{group.id}/', {'is_active': False}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_post(f'{TENANT_GROUPS_URL}{group.id}/add_users/', {'user_ids': [self.agent.id]}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(group.members.exists())

    def test_add_and_remove_users(self):
        group = self.create_tenant_group(name='P6 members')
        resp = self.api_post(f'{TENANT_GROUPS_URL}{group.id}/add_users/', {'user_ids': [self.agent.id]}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['added_users'], [self.agent.email])
        resp = self.api_post(f'{TENANT_GROUPS_URL}{group.id}/remove_users/', {'user_ids': [self.agent.id]}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['removed_users'], [self.agent.email])

    def test_available_features_rejects_bad_group_id(self):
        resp = self.api_get(f'{TENANT_GROUPS_URL}available_features/?group_id=abc', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', resp.data)

    def test_available_features_shape(self):
        group = self.create_tenant_group(name='P6 feats')
        resp = self.api_get(f'{TENANT_GROUPS_URL}available_features/?group_id={group.id}', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('categories', resp.data)
