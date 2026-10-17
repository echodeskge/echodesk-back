"""
Review fixes on the P6 users/security package:

1. ``tenant_group_ids`` on POST / PATCH /api/users/ needs manage_groups (or
   staff / superuser), like the bulk add_to_group action.
2. Account takeover: credential changes need every permission the target
   has; staff / role=admin targets are only deactivated, suspended or
   deleted by staff.
3. Suspended users (status='suspended', is_active=true) cannot sign in and
   their tokens stop working.
5. Changing your own password via /api/users/{id}/change_password/ signs
   out your other sessions and keeps the current one.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from users.models import AuthDeviceToken
from users.tests.test_user_admin_guards import (
    BULK_URL, NEW_PASSWORD, USERS_URL, GuardTestCase, change_password_url, detail_url,
    send_password_url,
)

User = get_user_model()

LOGIN_URL = '/api/auth/login/'
FORCED_PASSWORD_CHANGE_URL = '/api/auth/forced-password-change/'
PROFILE_URL = '/api/auth/profile/'
HOST = 'tenant.test.com'


def _mock_security(mock_security):
    mock_security.get_client_ip.return_value = '127.0.0.1'
    mock_security.is_ip_whitelisted.return_value = True
    mock_security.log_security_event.return_value = None


def token_client(key):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {key}')
    return client


# ---------------------------------------------------------------------------
# 1. tenant_group_ids needs manage_groups
# ---------------------------------------------------------------------------

class TestTenantGroupIdsNeedManageGroups(GuardTestCase):

    def setUp(self):
        super().setUp()
        self.group_a = self.create_tenant_group(name='FX Group A')
        self.group_b = self.create_tenant_group(name='FX Group B')
        self.agent.tenant_groups.set([self.group_a])
        # manage_users and manage_groups, but not staff.
        self.group_manager = self.create_user(
            email='gm@test.com', can_manage_users=True, can_manage_groups=True,
        )

    def group_ids(self, user):
        return set(user.tenant_groups.values_list('id', flat=True))

    def test_review_repro_user_manager_cannot_grant_groups_via_patch(self):
        # The bypass from the review: manage_users alone, PATCH tenant_group_ids.
        resp = self.api_patch(
            detail_url(self.user_manager.id),
            {'tenant_group_ids': [self.group_a.id, self.group_b.id]}, user=self.user_manager,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('groups', str(resp.data['detail']))
        self.assertEqual(self.group_ids(self.user_manager), set())

    def test_user_manager_cannot_change_another_users_groups(self):
        for ids in ([self.group_b.id], []):
            resp = self.api_patch(detail_url(self.agent.id), {'tenant_group_ids': ids}, user=self.user_manager)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, ids)
        self.assertEqual(self.group_ids(self.agent), {self.group_a.id})

    def test_unchanged_groups_resent_by_the_dashboard_are_fine(self):
        resp = self.api_patch(detail_url(self.agent.id), {
            'tenant_group_ids': [self.group_a.id], 'first_name': 'Renamed',
        }, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.first_name, 'Renamed')
        self.assertEqual(self.group_ids(self.agent), {self.group_a.id})

    def test_group_managers_and_staff_may_change_groups(self):
        resp = self.api_patch(detail_url(self.agent.id), {'tenant_group_ids': [self.group_b.id]}, user=self.group_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self.group_ids(self.agent), {self.group_b.id})
        staff = self.create_user(email='fx-staff@test.com', is_staff=True)
        resp = self.api_patch(detail_url(self.agent.id), {'tenant_group_ids': []}, user=staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self.group_ids(self.agent), set())

    @patch('users.views.send_user_invitation_email_task')
    def test_create_with_groups_needs_manage_groups(self, mock_invite):
        resp = self.api_post(USERS_URL, {
            'email': 'new-grp@test.com', 'first_name': 'N', 'last_name': 'G',
            'tenant_group_ids': [self.group_a.id],
        }, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(User.objects.filter(email='new-grp@test.com').exists())
        mock_invite.delay.assert_not_called()

        # Without groups the user manager can still invite.
        resp = self.api_post(USERS_URL, {
            'email': 'new-nogrp@test.com', 'first_name': 'N', 'last_name': 'G', 'tenant_group_ids': [],
        }, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        resp = self.api_post(USERS_URL, {
            'email': 'new-grp2@test.com', 'first_name': 'N', 'last_name': 'G',
            'tenant_group_ids': [self.group_a.id],
        }, user=self.group_manager)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.group_ids(User.objects.get(email='new-grp2@test.com')), {self.group_a.id})


# ---------------------------------------------------------------------------
# 2. Takeover guard
# ---------------------------------------------------------------------------

class TestLateralTakeover(GuardTestCase):
    """A target's permissions must be a subset of the caller's."""

    def setUp(self):
        super().setUp()
        # Same role as user_manager (agent) but holds a flag the caller lacks.
        self.group_holder = self.create_user(email='gh@test.com', can_manage_groups=True)
        self.manager_target = self.create_user(email='mgr-target@test.com', role='manager')
        self.give_sessions(self.group_holder)

    def _set_password(self, target, actor):
        return self.api_post(change_password_url(target.id), {
            'old_password': 'testpass123', **NEW_PASSWORD,
        }, user=actor)

    def test_review_repro_change_password_of_wider_permission_target(self):
        old_hash = self.group_holder.password
        resp = self._set_password(self.group_holder, self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.group_holder.refresh_from_db()
        self.assertEqual(self.group_holder.password, old_hash)
        self.assert_signed_in(self.group_holder)

    @patch('users.views.send_new_password_email_task')
    def test_review_repro_send_new_password_and_email_change(self, mock_email):
        resp = self.api_post(send_password_url(self.group_holder.id), {}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_patch(detail_url(self.group_holder.id), {'email': 'evil@test.com'}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.group_holder.refresh_from_db()
        self.assertEqual(self.group_holder.email, 'gh@test.com')
        mock_email.delay.assert_not_called()
        self.assert_signed_in(self.group_holder)

    @patch('users.views.send_new_password_email_task')
    def test_bulk_reset_skips_wider_permission_target(self, mock_email):
        resp = self.api_post(BULK_URL, {
            'action': 'reset_password', 'user_ids': [self.agent.id, self.group_holder.id],
        }, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['affected'], 1)
        self.assertEqual(resp.data['skipped_user_ids'], [self.group_holder.id])
        self.assertEqual(mock_email.delay.call_count, 1)

    def test_higher_role_target_refused(self):
        # A manager's role grants permissions an agent caller lacks.
        resp = self._set_password(self.manager_target, self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_superset_caller_may_change_subset_target(self):
        caller = self.create_user(email='wide@test.com', can_manage_users=True, can_manage_groups=True)
        resp = self._set_password(self.group_holder, caller)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_staff_caller_skips_permission_comparison(self):
        staff = self.create_user(email='fx-staff2@test.com', is_staff=True)
        resp = self._set_password(self.group_holder, staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class TestDisableGuard(GuardTestCase):
    """Deactivate / suspend / delete need staff for staff and admin targets."""

    def setUp(self):
        super().setUp()
        self.staff_only = self.create_user(email='fx-staffonly@test.com', is_staff=True)
        self.role_admin = self.create_user(email='fx-roleadmin@test.com', role='admin', can_manage_users=True)
        self.other_role_admin = self.create_user(email='fx-roleadmin2@test.com', role='admin')
        self.group_holder = self.create_user(email='fx-gh@test.com', can_manage_groups=True)
        for user in (self.staff_only, self.other_role_admin, self.group_holder):
            self.give_sessions(user)

    def assert_still_active(self, user):
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(user.status, 'active')
        self.assert_signed_in(user)

    def test_review_repro_user_manager_cannot_disable_staff_or_admin(self):
        for target in (self.staff_only, self.other_role_admin):
            for payload in ({'is_active': False}, {'status': 'suspended'}, {'status': 'inactive'}):
                resp = self.api_patch(detail_url(target.id), payload, user=self.user_manager)
                self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, (target.email, payload))
            resp = self.api_delete(detail_url(target.id), user=self.user_manager)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, target.email)
            self.assert_still_active(target)

    def test_role_admin_without_staff_cannot_disable_admin_or_staff(self):
        for target in (self.staff_only, self.other_role_admin):
            resp = self.api_patch(detail_url(target.id), {'status': 'suspended'}, user=self.role_admin)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, target.email)
            resp = self.api_delete(detail_url(target.id), user=self.role_admin)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, target.email)
            self.assert_still_active(target)

    def test_wider_permission_target_cannot_be_disabled(self):
        resp = self.api_patch(detail_url(self.group_holder.id), {'is_active': False}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assert_still_active(self.group_holder)

    def test_bulk_deactivate_and_delete_skip_protected_targets(self):
        ids = [self.agent.id, self.staff_only.id, self.other_role_admin.id, self.group_holder.id]
        resp = self.api_post(BULK_URL, {'action': 'deactivate', 'user_ids': ids}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['affected'], 1)
        self.assertEqual(
            sorted(resp.data['skipped_user_ids']),
            sorted([self.staff_only.id, self.other_role_admin.id, self.group_holder.id]),
        )
        for user in (self.staff_only, self.other_role_admin, self.group_holder):
            self.assert_still_active(user)
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.is_active)

        resp = self.api_post(BULK_URL, {'action': 'delete', 'user_ids': ids}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['affected'], 1)
        self.assertTrue(User.objects.filter(pk=self.staff_only.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.other_role_admin.pk).exists())
        self.assertFalse(User.objects.filter(pk=self.agent.pk).exists())

    def test_user_manager_still_disables_plain_agents(self):
        resp = self.api_patch(detail_url(self.agent.id), {'status': 'suspended'}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp = self.api_delete(detail_url(self.agent.id), user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_staff_may_disable_admins_and_staff(self):
        resp = self.api_patch(detail_url(self.other_role_admin.id), {'status': 'suspended'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp = self.api_patch(detail_url(self.staff_only.id), {'is_active': False}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assert_signed_out(self.staff_only)

    def test_unchanged_state_in_dashboard_payload_is_not_a_disable(self):
        # The web edit dialog always resends is_active and status.
        resp = self.api_patch(detail_url(self.other_role_admin.id), {
            'is_active': True, 'status': 'active', 'first_name': 'Same',
        }, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# 3. Suspended users cannot sign in
# ---------------------------------------------------------------------------

class TestSuspendedCannotSignIn(GuardTestCase):

    def setUp(self):
        super().setUp()
        self.suspended = self.create_user(
            email='susp@test.com', password='correctpass123', status='suspended',
        )
        self.assertTrue(self.suspended.is_active)

    @patch('tenants.views.SecurityService')
    def test_login_refused(self, mock_security):
        _mock_security(mock_security)
        for extra in ({}, {'platform': 'ios', 'device_id': 'p1'}):
            resp = self.api_post(LOGIN_URL, {'email': 'susp@test.com', 'password': 'correctpass123', **extra})
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn('User account is disabled', str(resp.data))
        self.assertFalse(Token.objects.filter(user=self.suspended).exists())
        self.assertFalse(AuthDeviceToken.objects.filter(user=self.suspended).exists())

    @patch('tenants.views.SecurityService')
    def test_active_user_still_logs_in(self, mock_security):
        _mock_security(mock_security)
        self.create_user(email='ok@test.com', password='correctpass123')
        resp = self.api_post(LOGIN_URL, {'email': 'ok@test.com', 'password': 'correctpass123'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_forced_password_change_refused(self):
        self.suspended.password_change_required = True
        self.suspended.save()
        old_hash = self.suspended.password
        resp = self.api_post(FORCED_PASSWORD_CHANGE_URL, {
            'email': 'susp@test.com', 'current_password': 'correctpass123', 'new_password': 'newstrongpass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['error'], 'User account is disabled')
        self.suspended.refresh_from_db()
        self.assertEqual(self.suspended.password, old_hash)
        self.assertFalse(Token.objects.filter(user=self.suspended).exists())

    def test_forced_password_change_refused_for_inactive(self):
        user = self.create_user(email='inact@test.com', password='correctpass123',
                                is_active=False, password_change_required=True)
        resp = self.api_post(FORCED_PASSWORD_CHANGE_URL, {
            'email': user.email, 'current_password': 'correctpass123', 'new_password': 'newstrongpass1',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Token.objects.filter(user=user).exists())

    def test_existing_tokens_stop_working(self):
        web = Token.objects.create(user=self.suspended)
        device = AuthDeviceToken.issue(self.suspended, 'ios', device_id='susp-dev')
        for key in (web.key, device.key):
            resp = token_client(key).get(PROFILE_URL, HTTP_HOST=HOST)
            self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED, key)

    def test_suspend_revokes_tokens(self):
        self.give_sessions(self.agent)
        resp = self.api_patch(detail_url(self.agent.id), {'status': 'suspended'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assert_signed_out(self.agent)


# ---------------------------------------------------------------------------
# 5. Own password change signs out the other sessions
# ---------------------------------------------------------------------------

class TestOwnPasswordChangeRevokesOthers(GuardTestCase):

    def setUp(self):
        super().setUp()
        self.web = Token.objects.create(user=self.agent)
        self.phone = AuthDeviceToken.issue(self.agent, 'ios', device_id='own-a')
        self.tablet = AuthDeviceToken.issue(self.agent, 'android', device_id='own-b')

    def change_own(self, key):
        return token_client(key).post(change_password_url(self.agent.id), {
            'old_password': 'testpass123', **NEW_PASSWORD,
        }, format='json', HTTP_HOST=HOST)

    def test_via_web_token_keeps_web_session_only(self):
        resp = self.change_own(self.web.key)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(Token.objects.filter(key=self.web.key).exists())
        self.assertFalse(AuthDeviceToken.objects.filter(user=self.agent).exists())
        self.assertEqual(token_client(self.web.key).get(PROFILE_URL, HTTP_HOST=HOST).status_code, 200)

    def test_via_device_token_keeps_that_device_only(self):
        resp = self.change_own(self.phone.key)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(Token.objects.filter(user=self.agent).exists())
        self.assertEqual(
            list(AuthDeviceToken.objects.filter(user=self.agent).values_list('key', flat=True)),
            [self.phone.key],
        )

    def test_wrong_old_password_revokes_nothing(self):
        resp = token_client(self.web.key).post(change_password_url(self.agent.id), {
            'old_password': 'wrong', **NEW_PASSWORD,
        }, format='json', HTTP_HOST=HOST)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(AuthDeviceToken.objects.filter(user=self.agent).count(), 2)
        self.assertTrue(Token.objects.filter(key=self.web.key).exists())

    def test_other_users_sessions_untouched(self):
        self.give_sessions(self.user_manager)
        self.change_own(self.web.key)
        self.assert_signed_in(self.user_manager)


# ---------------------------------------------------------------------------
# 2 (review round 2). Role changes cannot be used to get around the guard
# ---------------------------------------------------------------------------

class TestRoleChangeTakeover(GuardTestCase):
    """
    demote -> set password -> promote back was a takeover in three requests,
    and raising your own role defeated every rank / permission comparison.
    """

    def setUp(self):
        super().setUp()
        self.manager_target = self.create_user(email='fx-mgr@test.com', role='manager')
        self.give_sessions(self.manager_target)

    def test_review_repro_demote_reset_promote(self):
        old_hash = self.manager_target.password
        resp = self.api_patch(detail_url(self.manager_target.id), {'role': 'agent'}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_post(change_password_url(self.manager_target.id), {
            'old_password': 'testpass123', **NEW_PASSWORD,
        }, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.manager_target.refresh_from_db()
        self.assertEqual(self.manager_target.role, 'manager')
        self.assertEqual(self.manager_target.password, old_hash)
        self.assert_signed_in(self.manager_target)

    def test_review_repro_demote_then_disable(self):
        resp = self.api_patch(detail_url(self.manager_target.id), {'role': 'viewer'}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_patch(detail_url(self.manager_target.id), {'is_active': False}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.manager_target.refresh_from_db()
        self.assertTrue(self.manager_target.is_active)

    def test_review_repro_cannot_raise_own_role(self):
        resp = self.api_patch(detail_url(self.user_manager.id), {'role': 'manager'}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.user_manager.refresh_from_db()
        self.assertEqual(self.user_manager.role, 'agent')
        # So the takeover guard still holds afterwards.
        resp = self.api_post(change_password_url(self.manager_target.id), {
            'old_password': 'testpass123', **NEW_PASSWORD,
        }, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_cannot_raise_own_role_either(self):
        staff = self.create_user(email='fx-staff-agent@test.com', is_staff=True)
        resp = self.api_patch(detail_url(staff.id), {'role': 'manager'}, user=staff)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        # Lowering your own role is fine.
        resp = self.api_patch(detail_url(staff.id), {'role': 'viewer'}, user=staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_cannot_give_a_role_above_your_own(self):
        resp = self.api_patch(detail_url(self.agent.id), {'role': 'manager'}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.role, 'agent')

    @patch('users.views.send_user_invitation_email_task')
    def test_cannot_create_a_user_above_your_own_role(self, mock_invite):
        resp = self.api_post(USERS_URL, {
            'email': 'fx-new-mgr@test.com', 'first_name': 'N', 'last_name': 'M', 'role': 'manager',
        }, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(User.objects.filter(email='fx-new-mgr@test.com').exists())
        mock_invite.delay.assert_not_called()

    def test_cannot_change_role_of_wider_permission_target(self):
        holder = self.create_user(email='fx-holder@test.com', can_manage_groups=True)
        resp = self.api_patch(detail_url(holder.id), {'role': 'viewer'}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_allowed_role_changes(self):
        # Within your own rank, on a target you outrank.
        resp = self.api_patch(detail_url(self.agent.id), {'role': 'viewer'}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        manager_caller = self.create_user(email='fx-mgr-caller@test.com', role='manager', can_manage_users=True)
        resp = self.api_patch(detail_url(self.agent.id), {'role': 'manager'}, user=manager_caller)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Staff are exempt from the rank comparison (admin still needs a superuser).
        staff = self.create_user(email='fx-staff-r@test.com', is_staff=True)
        resp = self.api_patch(detail_url(self.manager_target.id), {'role': 'agent'}, user=staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_bulk_change_role_same_rules(self):
        resp = self.api_post(BULK_URL, {
            'action': 'change_role', 'role': 'manager', 'user_ids': [self.agent.id],
        }, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        ids = [self.agent.id, self.manager_target.id, self.user_manager.id]
        resp = self.api_post(BULK_URL, {
            'action': 'change_role', 'role': 'viewer', 'user_ids': ids,
        }, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Lowering your own role is allowed; the manager is out of reach.
        self.assertEqual(resp.data['skipped_user_ids'], [self.manager_target.id])
        self.manager_target.refresh_from_db()
        self.assertEqual(self.manager_target.role, 'manager')
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.role, 'viewer')

    def test_bulk_change_role_cannot_raise_own_role(self):
        staff_agent = self.create_user(email='fx-staff-b@test.com', is_staff=True)
        resp = self.api_post(BULK_URL, {
            'action': 'change_role', 'role': 'manager', 'user_ids': [staff_agent.id, self.agent.id],
        }, user=staff_agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['skipped_user_ids'], [staff_agent.id])
        staff_agent.refresh_from_db()
        self.assertEqual(staff_agent.role, 'agent')
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.role, 'manager')


class TestStaffCallerRoleRank(GuardTestCase):
    """Staff callers are exempt from the comparison, except for admin-role targets."""

    def setUp(self):
        super().setUp()
        self.staff_agent = self.create_user(email='fx-staff-agent2@test.com', is_staff=True)
        self.manager_target = self.create_user(email='fx-mgr2@test.com', role='manager')
        self.admin_target = self.create_user(email='fx-adm2@test.com', role='admin')

    def _set_password(self, target):
        return self.api_post(change_password_url(target.id), {
            'old_password': 'testpass123', **NEW_PASSWORD,
        }, user=self.staff_agent)

    @patch('users.views.send_new_password_email_task')
    def test_staff_agent_may_reset_a_manager(self, _mock_email):
        self.assertEqual(self._set_password(self.manager_target).status_code, status.HTTP_200_OK)
        resp = self.api_post(send_password_url(self.manager_target.id), {}, user=self.staff_agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_staff_agent_still_cannot_reset_an_admin(self):
        self.assertEqual(self._set_password(self.admin_target).status_code, status.HTTP_403_FORBIDDEN)


class TestReactivateGuard(GuardTestCase):
    """Reactivating a staff / admin account undoes a staff decision."""

    def setUp(self):
        super().setUp()
        self.staff_off = self.create_user(email='fx-staff-off@test.com', is_staff=True,
                                          is_active=False, status='inactive')
        self.admin_off = self.create_user(email='fx-admin-off@test.com', role='admin', status='suspended')
        self.agent_off = self.create_user(email='fx-agent-off@test.com', is_active=False, status='inactive')

    def test_user_manager_cannot_reactivate_staff_or_admin(self):
        resp = self.api_patch(detail_url(self.staff_off.id), {'is_active': True, 'status': 'active'},
                              user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.api_patch(detail_url(self.admin_off.id), {'status': 'active'}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.staff_off.refresh_from_db()
        self.admin_off.refresh_from_db()
        self.assertFalse(self.staff_off.is_active)
        self.assertEqual(self.admin_off.status, 'suspended')

    def test_user_manager_may_reactivate_plain_agent(self):
        resp = self.api_patch(detail_url(self.agent_off.id), {'is_active': True, 'status': 'active'},
                              user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_bulk_activate_skips_staff_and_admin(self):
        ids = [self.staff_off.id, self.admin_off.id, self.agent_off.id]
        resp = self.api_post(BULK_URL, {'action': 'activate', 'user_ids': ids}, user=self.user_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['affected'], 1)
        self.assertEqual(sorted(resp.data['skipped_user_ids']), sorted([self.staff_off.id, self.admin_off.id]))
        self.agent_off.refresh_from_db()
        self.assertTrue(self.agent_off.is_active)
        self.staff_off.refresh_from_db()
        self.assertFalse(self.staff_off.is_active)

    def test_staff_may_reactivate(self):
        resp = self.api_post(BULK_URL, {
            'action': 'activate', 'user_ids': [self.staff_off.id, self.admin_off.id],
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['affected'], 2)


class TestWebSocketRefusesSuspended(GuardTestCase):

    def test_suspended_or_inactive_tokens_refused(self):
        from amanati_crm.websocket_auth import _user_for_token
        schema = self.tenant.schema_name
        self.assertEqual(_user_for_token(Token.objects.create(user=self.agent).key, schema), self.agent)
        for i, kwargs in enumerate(({'status': 'suspended'}, {'is_active': False})):
            user = self.create_user(email=f'fx-ws-{i}@test.com', **kwargs)
            web = Token.objects.create(user=user)
            device = AuthDeviceToken.issue(user, 'ios', device_id=f'ws-{user.pk}')
            for key in (web.key, device.key):
                self.assertFalse(_user_for_token(key, schema).is_authenticated, (kwargs, key))
