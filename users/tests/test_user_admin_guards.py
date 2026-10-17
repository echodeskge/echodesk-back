"""
P6 users/security: privilege-escalation guards on /api/users/, session
revocation when a user loses access, the new bulk actions, writable
department_id / job_title, is_staff only via set-staff, account-takeover
guards, and the user list / group members query counts.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.authtoken.models import Token

from users.models import AuthDeviceToken, Department
from users.tests.conftest import EchoDeskTenantTestCase

User = get_user_model()

USERS_URL = '/api/users/'
BULK_URL = '/api/users/bulk_action/'


def detail_url(user_id):
    return f'{USERS_URL}{user_id}/'


def send_password_url(user_id):
    return f'{USERS_URL}{user_id}/send_new_password/'


def change_password_url(user_id):
    return f'{USERS_URL}{user_id}/change_password/'


def set_staff_url(user_id):
    return f'{USERS_URL}{user_id}/set-staff/'


NEW_PASSWORD = {
    'new_password': 'N3w-pass-word!', 'new_password_confirm': 'N3w-pass-word!',
}


class GuardTestCase(EchoDeskTenantTestCase):
    def setUp(self):
        super().setUp()
        self.superuser = self.create_user(
            email='owner@test.com', role='admin', is_superuser=True, is_staff=True,
        )
        # Tenant admin by role, user manager by flag; not a superuser.
        self.admin = self.create_admin(email='tadmin@test.com')
        # Plain agent who was given the manage-users flag.
        self.user_manager = self.create_user(email='um@test.com', can_manage_users=True)
        self.agent = self.create_user(email='agent@test.com')

    def give_sessions(self, user):
        """A web token and a device token for ``user``."""
        Token.objects.get_or_create(user=user)
        AuthDeviceToken.issue(user, platform='ios', device_id=f'dev-{user.pk}', device_name='Phone')

    def assert_signed_out(self, user):
        self.assertFalse(Token.objects.filter(user=user).exists())
        self.assertFalse(AuthDeviceToken.objects.filter(user=user).exists())

    def assert_signed_in(self, user):
        self.assertTrue(Token.objects.filter(user=user).exists())
        self.assertTrue(AuthDeviceToken.objects.filter(user=user).exists())


class TestRoleEscalation(GuardTestCase):

    def test_non_superuser_cannot_grant_admin(self):
        resp = self.api_patch(detail_url(self.agent.id), {'role': 'admin'}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.role, 'agent')

    def test_admin_role_user_cannot_grant_admin_either(self):
        resp = self.api_patch(detail_url(self.agent.id), {'role': 'admin'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_superuser_cannot_demote_admin(self):
        resp = self.api_patch(detail_url(self.admin.id), {'role': 'agent'}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, 'admin')

    def test_superuser_can_grant_admin(self):
        resp = self.api_patch(detail_url(self.agent.id), {'role': 'admin'}, user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.role, 'admin')

    def test_non_admin_role_changes_still_allowed(self):
        resp = self.api_patch(detail_url(self.agent.id), {'role': 'manager'}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.role, 'manager')

    def test_unchanged_admin_role_in_payload_is_fine(self):
        resp = self.api_patch(
            detail_url(self.admin.id), {'role': 'admin', 'job_title': 'Lead'}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    @patch('users.views.send_user_invitation_email_task')
    def test_non_superuser_cannot_create_admin(self, _mock):
        resp = self.api_post(USERS_URL, {
            'email': 'newadmin@test.com', 'first_name': 'N', 'last_name': 'A', 'role': 'admin',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(User.objects.filter(email='newadmin@test.com').exists())

    @patch('users.views.send_user_invitation_email_task')
    def test_superuser_can_create_admin(self, _mock):
        resp = self.api_post(USERS_URL, {
            'email': 'newadmin2@test.com', 'first_name': 'N', 'last_name': 'A', 'role': 'admin',
        }, user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)


class TestSuperuserProtection(GuardTestCase):

    @patch('users.views.send_new_password_email_task')
    def test_non_superuser_cannot_change_superuser_email(self, mock_email):
        resp = self.api_patch(
            detail_url(self.superuser.id), {'email': 'attacker@evil.com'}, user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.superuser.refresh_from_db()
        self.assertEqual(self.superuser.email, 'owner@test.com')
        mock_email.delay.assert_not_called()

    def test_non_superuser_cannot_deactivate_superuser(self):
        resp = self.api_patch(detail_url(self.superuser.id), {'is_active': False}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.is_active)

    def test_non_superuser_cannot_delete_superuser(self):
        resp = self.api_delete(detail_url(self.superuser.id), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(User.objects.filter(pk=self.superuser.pk).exists())

    @patch('users.views.send_new_password_email_task')
    def test_non_superuser_cannot_reset_superuser_password(self, mock_email):
        old_hash = self.superuser.password
        resp = self.api_post(send_password_url(self.superuser.id), {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.superuser.refresh_from_db()
        self.assertEqual(self.superuser.password, old_hash)
        mock_email.delay.assert_not_called()

    def test_non_superuser_cannot_set_superuser_password(self):
        resp = self.api_post(change_password_url(self.superuser.id), {
            'old_password': 'testpass123', 'new_password': 'N3w-pass-word!',
            'new_password_confirm': 'N3w-pass-word!',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_superuser_can_edit_superuser(self):
        other = self.create_user(email='owner2@test.com', is_superuser=True, is_staff=True)
        resp = self.api_patch(detail_url(other.id), {'job_title': 'Co-owner'}, user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_cannot_deactivate_or_suspend_self(self):
        for payload in ({'is_active': False}, {'status': 'suspended'}, {'status': 'inactive'}):
            resp = self.api_patch(detail_url(self.admin.id), payload, user=self.admin)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, payload)
            # The web shows ``detail``; ``error`` stays for older clients.
            self.assertEqual(resp.data['detail'], 'You cannot deactivate or suspend your own account.')
            self.assertEqual(resp.data['error'], resp.data['detail'])
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertEqual(self.admin.status, 'active')


class TestAccountTakeover(GuardTestCase):
    """Credential changes need at least the target's privileges."""

    def setUp(self):
        super().setUp()
        self.staff_only = self.create_user(email='staffonly@test.com', is_staff=True)
        self.role_admin = self.create_user(
            email='roleadmin@test.com', role='admin', can_manage_users=True,
        )

    def _set_password(self, target, actor):
        return self.api_post(change_password_url(target.id), {
            'old_password': 'testpass123', **NEW_PASSWORD,
        }, user=actor)

    def test_role_admin_cannot_take_over_staff(self):
        old_hash = self.staff_only.password
        resp = self._set_password(self.staff_only, self.role_admin)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.staff_only.refresh_from_db()
        self.assertEqual(self.staff_only.password, old_hash)

    def test_user_manager_cannot_take_over_admin_or_staff(self):
        for target in (self.admin, self.role_admin, self.staff_only):
            resp = self._set_password(target, self.user_manager)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, target.email)

    def test_staff_without_admin_role_cannot_take_over_role_admin(self):
        resp = self._set_password(self.role_admin, self.staff_only)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    @patch('users.views.send_new_password_email_task')
    def test_email_change_on_privileged_target_refused(self, mock_email):
        resp = self.api_patch(
            detail_url(self.staff_only.id), {'email': 'attacker@evil.com'}, user=self.role_admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.staff_only.refresh_from_db()
        self.assertEqual(self.staff_only.email, 'staffonly@test.com')
        mock_email.delay.assert_not_called()

    @patch('users.views.send_new_password_email_task')
    def test_unchanged_email_in_payload_is_fine(self, _mock):
        resp = self.api_patch(
            detail_url(self.staff_only.id),
            {'email': 'staffonly@test.com', 'job_title': 'Ops'}, user=self.role_admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    @patch('users.views.send_new_password_email_task')
    def test_send_new_password_on_privileged_target_refused(self, mock_email):
        resp = self.api_post(send_password_url(self.admin.id), {}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        mock_email.delay.assert_not_called()

    @patch('users.views.send_new_password_email_task')
    def test_bulk_reset_skips_privileged_targets(self, mock_email):
        resp = self.api_post(BULK_URL, {
            'action': 'reset_password',
            'user_ids': [self.agent.id, self.admin.id, self.staff_only.id],
        }, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['affected'], 1)
        self.assertEqual(sorted(resp.data['skipped_user_ids']), sorted([self.admin.id, self.staff_only.id]))
        self.assertEqual(mock_email.delay.call_count, 1)

    def test_equal_privilege_may_change_credentials(self):
        # self.admin is staff + admin role: holds every privilege of both.
        for target in (self.staff_only, self.role_admin, self.agent):
            resp = self._set_password(target, self.admin)
            self.assertEqual(resp.status_code, status.HTTP_200_OK, target.email)

    def test_superuser_may_change_anyone(self):
        resp = self._set_password(self.admin, self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_own_password_always_allowed(self):
        resp = self._set_password(self.user_manager, self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_plain_agent_targets_still_managed_by_user_manager(self):
        resp = self._set_password(self.agent, self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class TestIsStaffAndDepartment(GuardTestCase):

    def test_patch_is_staff_is_ignored_even_for_superuser(self):
        # The dashboard's booking-staff checkbox PATCHes is_staff. A tenant
        # owner (superuser) ticking it must not grant staff, nor un-ticking
        # it strip a real staff admin.
        for actor in (self.superuser, self.admin):
            resp = self.api_patch(detail_url(self.agent.id), {'is_staff': True}, user=actor)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.agent.refresh_from_db()
            self.assertFalse(self.agent.is_staff)
        resp = self.api_patch(detail_url(self.admin.id), {'is_staff': False}, user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_staff)

    def test_superuser_sets_staff_via_explicit_endpoint(self):
        resp = self.api_post(set_staff_url(self.agent.id), {'is_staff': True}, user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, {'id': self.agent.id, 'is_staff': True})
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.is_staff)
        resp = self.api_post(set_staff_url(self.agent.id), {'is_staff': False}, user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.is_staff)

    def test_set_staff_refused_for_non_superusers_and_self(self):
        for actor in (self.admin, self.user_manager, self.agent):
            resp = self.api_post(set_staff_url(self.agent.id), {'is_staff': True}, user=actor)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, actor.email)
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.is_staff)
        resp = self.api_post(set_staff_url(self.superuser.id), {'is_staff': False}, user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_set_staff_needs_boolean(self):
        resp = self.api_post(set_staff_url(self.agent.id), {}, user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_department_can_be_changed_and_cleared(self):
        dept = Department.objects.create(name='Support P6')
        resp = self.api_patch(detail_url(self.agent.id), {'department_id': dept.id}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.department_id, dept.id)

        resp = self.api_patch(detail_url(self.agent.id), {'department_id': None}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertIsNone(self.agent.department_id)

    def test_unknown_department_rejected(self):
        resp = self.api_patch(detail_url(self.agent.id), {'department_id': 999999}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('department_id', resp.data)

    @patch('users.views.send_user_invitation_email_task')
    def test_create_accepts_job_title(self, _mock):
        resp = self.api_post(USERS_URL, {
            'email': 'jt@test.com', 'first_name': 'J', 'last_name': 'T', 'job_title': 'Cashier',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.get(email='jt@test.com').job_title, 'Cashier')


class TestSessionRevocation(GuardTestCase):

    def setUp(self):
        super().setUp()
        self.give_sessions(self.agent)

    @patch('users.views.send_new_password_email_task')
    def test_send_new_password_signs_user_out(self, _mock):
        resp = self.api_post(send_password_url(self.agent.id), {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, {'message': 'New password sent successfully'})
        self.assert_signed_out(self.agent)

    @patch('users.views.send_new_password_email_task')
    def test_email_change_signs_user_out(self, _mock):
        resp = self.api_patch(detail_url(self.agent.id), {'email': 'agent-new@test.com'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assert_signed_out(self.agent)

    def test_suspend_signs_user_out(self):
        resp = self.api_patch(detail_url(self.agent.id), {'status': 'suspended'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assert_signed_out(self.agent)

    def test_inactive_status_signs_user_out(self):
        resp = self.api_patch(detail_url(self.agent.id), {'status': 'inactive'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assert_signed_out(self.agent)

    def test_is_active_false_signs_user_out(self):
        resp = self.api_patch(detail_url(self.agent.id), {'is_active': False}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assert_signed_out(self.agent)

    def test_unrelated_edit_keeps_sessions(self):
        resp = self.api_patch(detail_url(self.agent.id), {'job_title': 'Senior'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assert_signed_in(self.agent)

    def test_admin_password_change_signs_target_out(self):
        resp = self.api_post(change_password_url(self.agent.id), {
            'old_password': 'testpass123', 'new_password': 'N3w-pass-word!',
            'new_password_confirm': 'N3w-pass-word!',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assert_signed_out(self.agent)

    def test_other_users_sessions_untouched(self):
        self.give_sessions(self.user_manager)
        self.api_patch(detail_url(self.agent.id), {'status': 'suspended'}, user=self.admin)
        self.assert_signed_in(self.user_manager)


class TestBulkActionGuards(GuardTestCase):

    def test_bulk_deactivate_skips_self_and_superusers_and_signs_out(self):
        self.give_sessions(self.agent)
        resp = self.api_post(BULK_URL, {
            'action': 'deactivate',
            'user_ids': [self.admin.id, self.superuser.id, self.agent.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['affected'], 1)
        self.assertEqual(sorted(resp.data['skipped_user_ids']), sorted([self.admin.id, self.superuser.id]))
        self.assertEqual(resp.data['message'], 'Deactivated 1 users')
        for user, active in ((self.admin, True), (self.superuser, True), (self.agent, False)):
            user.refresh_from_db()
            self.assertEqual(user.is_active, active, user.email)
        self.assert_signed_out(self.agent)

    def test_bulk_delete_skips_superuser(self):
        resp = self.api_post(BULK_URL, {
            'action': 'delete', 'user_ids': [self.superuser.id, self.agent.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(User.objects.filter(pk=self.superuser.pk).exists())
        self.assertFalse(User.objects.filter(pk=self.agent.pk).exists())

    def test_bulk_grant_admin_needs_superuser(self):
        resp = self.api_post(BULK_URL, {
            'action': 'change_role', 'role': 'admin', 'user_ids': [self.agent.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.role, 'agent')

    def test_bulk_role_change_skips_admins_for_non_superuser(self):
        resp = self.api_post(BULK_URL, {
            'action': 'change_role', 'role': 'viewer', 'user_ids': [self.admin.id, self.agent.id],
        }, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['skipped_user_ids'], [self.admin.id])
        self.admin.refresh_from_db()
        self.agent.refresh_from_db()
        self.assertEqual(self.admin.role, 'admin')
        self.assertEqual(self.agent.role, 'viewer')

    def test_superuser_bulk_grants_admin(self):
        resp = self.api_post(BULK_URL, {
            'action': 'change_role', 'role': 'admin', 'user_ids': [self.agent.id],
        }, user=self.superuser)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.role, 'admin')

    def test_change_role_without_role_keeps_error_shape(self):
        resp = self.api_post(BULK_URL, {
            'action': 'change_role', 'user_ids': [self.agent.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', resp.data)

    def test_agent_cannot_bulk(self):
        resp = self.api_post(BULK_URL, {
            'action': 'activate', 'user_ids': [self.agent.id],
        }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    @patch('users.views.send_new_password_email_task')
    def test_bulk_reset_password(self, mock_email):
        self.give_sessions(self.agent)
        old_hash = self.agent.password
        resp = self.api_post(BULK_URL, {
            'action': 'reset_password', 'user_ids': [self.agent.id, self.admin.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['affected'], 1)
        self.assertEqual(resp.data['skipped_user_ids'], [self.admin.id])
        self.agent.refresh_from_db()
        self.assertNotEqual(self.agent.password, old_hash)
        self.assertTrue(self.agent.password_change_required)
        self.assert_signed_out(self.agent)
        self.assertEqual(mock_email.delay.call_count, 1)
        self.assertEqual(mock_email.delay.call_args.kwargs['user_email'], self.agent.email)

    def test_bulk_add_and_remove_group(self):
        group = self.create_tenant_group(name='Bulk P6 group')
        resp = self.api_post(BULK_URL, {
            'action': 'add_to_group', 'group_id': group.id, 'user_ids': [self.agent.id, self.admin.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(set(group.members.values_list('id', flat=True)), {self.agent.id, self.admin.id})

        resp = self.api_post(BULK_URL, {
            'action': 'remove_from_group', 'group_id': group.id, 'user_ids': [self.agent.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(list(group.members.values_list('id', flat=True)), [self.admin.id])

    def test_bulk_group_actions_need_manage_groups(self):
        group = self.create_tenant_group(name='Bulk P6 gated group')
        for action in ('add_to_group', 'remove_from_group'):
            resp = self.api_post(BULK_URL, {
                'action': action, 'group_id': group.id, 'user_ids': [self.agent.id],
            }, user=self.user_manager)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, action)
        self.assertFalse(group.members.exists())

    def test_bulk_group_action_needs_valid_group(self):
        resp = self.api_post(BULK_URL, {
            'action': 'add_to_group', 'user_ids': [self.agent.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        resp = self.api_post(BULK_URL, {
            'action': 'add_to_group', 'group_id': 987654, 'user_ids': [self.agent.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestUserListQueryCount(EchoDeskTenantTestCase):
    """The full serializer no longer costs queries per row."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='qcadmin@test.com')
        self.group = self.create_tenant_group(name='QC group')
        self.dept = Department.objects.create(name='QC dept')
        self.boss = self.create_user(email='qcboss@test.com')

    def _add_users(self, n, start):
        for i in range(start, start + n):
            u = self.create_user(email=f'qc{i}@test.com', department=self.dept, manager=self.boss)
            u.tenant_groups.add(self.group)

    def _count(self):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.api_get(f'{USERS_URL}?page_size=100', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self._last_sql = '\n'.join(q['sql'][:150] for q in ctx.captured_queries)
        return len(ctx.captured_queries), resp

    def test_query_count_does_not_grow_with_rows(self):
        self._add_users(3, 0)
        small, _ = self._count()
        self._add_users(6, 100)
        large, resp = self._count()
        self.assertGreaterEqual(resp.data['count'], 10)
        self.assertEqual(small, large, getattr(self, '_last_sql', ''))


class TestGroupMembersQueryCount(EchoDeskTenantTestCase):
    """GET /api/tenant-groups/{id}/members/ (full rows) is constant in queries."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='gmadmin@test.com')
        self.group = self.create_tenant_group(name='GM group')
        self.dept = Department.objects.create(name='GM dept')

    def _add(self, n, start):
        for i in range(start, start + n):
            self.create_user(email=f'gm{i}@test.com', department=self.dept).tenant_groups.add(self.group)

    def _count(self):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.api_get(f'/api/tenant-groups/{self.group.id}/members/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return len(ctx.captured_queries), resp

    def test_members_query_count_does_not_grow(self):
        self._add(2, 0)
        small, _ = self._count()
        self._add(6, 100)
        large, resp = self._count()
        self.assertEqual(resp.data['count'], 8)
        self.assertEqual(resp.data['members'][0]['department']['employee_count'], 8)
        self.assertEqual(small, large)
