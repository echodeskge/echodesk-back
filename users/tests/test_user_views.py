"""
Tests for UserViewSet: CRUD, bulk actions, change/send password.
Endpoints under /api/users/.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase

User = get_user_model()

USERS_URL = '/api/users/'


def detail_url(user_id):
    return f'{USERS_URL}{user_id}/'


class TestUserList(EchoDeskTenantTestCase):
    """Tests for listing and retrieving users."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.agent = self.create_user(email='agent1@test.com')

    def test_list_users_authenticated(self):
        resp = self.api_get(USERS_URL, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        emails = [u['email'] for u in resp.data['results']]
        self.assertIn(self.admin.email, emails)
        self.assertIn(self.agent.email, emails)

    def test_list_users_unauthenticated_returns_401(self):
        resp = self.api_get(USERS_URL)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_retrieve_user(self):
        resp = self.api_get(detail_url(self.agent.id), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['email'], self.agent.email)

    def test_filter_by_role(self):
        resp = self.api_get(f'{USERS_URL}?role=agent', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for u in resp.data['results']:
            self.assertEqual(u['role'], 'agent')

    def test_filter_by_status(self):
        self.agent.status = 'inactive'
        self.agent.save()
        resp = self.api_get(f'{USERS_URL}?status=inactive', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        emails = [u['email'] for u in resp.data['results']]
        self.assertIn(self.agent.email, emails)

    def test_filter_by_group(self):
        """BUG 4 regression: filter by tenant_groups__id, not groups__id."""
        group = self.create_tenant_group(name='FilterGroup')
        self.agent.tenant_groups.add(group)

        resp = self.api_get(f'{USERS_URL}?group={group.id}', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        emails = [u['email'] for u in resp.data['results']]
        self.assertIn(self.agent.email, emails)
        self.assertNotIn(self.admin.email, emails)


class TestUserCreate(EchoDeskTenantTestCase):
    """Tests for creating users."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.agent = self.create_user(email='agent1@test.com')

    @patch('users.views.send_user_invitation_email_task')
    def test_create_user_as_admin(self, mock_email):
        resp = self.api_post(USERS_URL, {
            'email': 'newuser@test.com',
            'first_name': 'New',
            'last_name': 'User',
            'role': 'agent',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(email='newuser@test.com').exists())

    @patch('users.views.send_user_invitation_email_task')
    def test_create_user_as_agent_denied(self, mock_email):
        """BUG 1 regression: permission check uses 'manage_users' not 'can_manage_users'."""
        resp = self.api_post(USERS_URL, {
            'email': 'blocked@test.com',
            'first_name': 'Blocked',
            'role': 'agent',
        }, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    @patch('users.views.send_user_invitation_email_task')
    def test_create_user_generates_temp_password(self, mock_email):
        resp = self.api_post(USERS_URL, {
            'email': 'temppass@test.com',
            'role': 'agent',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email='temppass@test.com')
        self.assertIsNotNone(user.temporary_password)
        self.assertTrue(len(user.temporary_password) >= 12)

    @patch('users.views.send_user_invitation_email_task')
    def test_create_user_sets_password_change_required(self, mock_email):
        resp = self.api_post(USERS_URL, {
            'email': 'pcrequired@test.com',
            'role': 'agent',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email='pcrequired@test.com')
        self.assertTrue(user.password_change_required)


class TestUserUpdate(EchoDeskTenantTestCase):
    """Tests for updating users."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.agent = self.create_user(email='agent1@test.com', role='agent')
        self.other_agent = self.create_user(email='agent2@test.com', role='agent')

    def test_update_user_as_admin(self):
        resp = self.api_patch(
            detail_url(self.agent.id),
            {'first_name': 'Updated'},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.first_name, 'Updated')

    def test_update_own_profile_allowed(self):
        resp = self.api_patch(
            detail_url(self.agent.id),
            {'first_name': 'Self', 'last_name': 'Update'},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.first_name, 'Self')

    def test_update_other_user_as_agent_denied(self):
        resp = self.api_patch(
            detail_url(self.other_agent.id),
            {'first_name': 'Hacked'},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_self_update_cannot_escalate_role(self):
        """BUG 2 regression: non-privileged users cannot change their own role."""
        resp = self.api_patch(
            detail_url(self.agent.id),
            {'role': 'admin'},
            user=self.agent,
        )
        # The request succeeds but role should NOT be changed
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.role, 'agent')

    def test_self_update_safe_fields_applied(self):
        """Non-privileged users can update first_name, last_name, phone_number, job_title."""
        resp = self.api_patch(
            detail_url(self.agent.id),
            {
                'first_name': 'Safe',
                'last_name': 'Update',
                'phone_number': '555-1234',
                'job_title': 'Agent',
                'role': 'admin',  # should be stripped
                'is_active': False,  # should be stripped
            },
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.first_name, 'Safe')
        self.assertEqual(self.agent.phone_number, '555-1234')
        self.assertEqual(self.agent.role, 'agent')  # not escalated
        self.assertTrue(self.agent.is_active)  # not deactivated

    @patch('users.views.send_new_password_email_task')
    def test_admin_can_change_email(self, mock_email):
        resp = self.api_patch(
            detail_url(self.agent.id),
            {'email': 'newaddr@test.com'},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.email, 'newaddr@test.com')

    @patch('users.views.send_new_password_email_task')
    def test_email_change_resets_password_and_emails_new_address(self, mock_email):
        old_hash = self.agent.password

        resp = self.api_patch(
            detail_url(self.agent.id),
            {'email': 'moved@test.com'},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.agent.refresh_from_db()
        # A fresh password is issued and a change is required on next login.
        self.assertNotEqual(self.agent.password, old_hash)
        self.assertTrue(self.agent.password_change_required)
        # The new credentials are emailed to the NEW address.
        mock_email.delay.assert_called_once()
        self.assertEqual(
            mock_email.delay.call_args.kwargs['user_email'],
            'moved@test.com',
        )

    @patch('users.views.send_new_password_email_task')
    def test_non_email_update_does_not_reset_password(self, mock_email):
        old_hash = self.agent.password
        resp = self.api_patch(
            detail_url(self.agent.id),
            {'first_name': 'NoReset'},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.password, old_hash)
        mock_email.delay.assert_not_called()

    def test_duplicate_email_rejected(self):
        resp = self.api_patch(
            detail_url(self.agent.id),
            {'email': self.other_agent.email},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', resp.data)

    def test_duplicate_email_case_insensitive_rejected(self):
        resp = self.api_patch(
            detail_url(self.agent.id),
            {'email': self.other_agent.email.upper()},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unchanged_email_is_allowed(self):
        """Resending the user's own email must not trip the uniqueness check."""
        resp = self.api_patch(
            detail_url(self.agent.id),
            {'email': self.agent.email, 'first_name': 'Same'},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    @patch('users.views.send_new_password_email_task')
    def test_self_update_cannot_change_email(self, mock_email):
        original = self.agent.email
        resp = self.api_patch(
            detail_url(self.agent.id),
            {'email': 'selfchange@test.com'},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.email, original)  # email change stripped
        mock_email.delay.assert_not_called()


class TestUserDelete(EchoDeskTenantTestCase):
    """Tests for deleting users."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.agent = self.create_user(email='agent1@test.com')

    def test_delete_user_as_admin(self):
        agent_id = self.agent.id
        resp = self.api_delete(detail_url(agent_id), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(id=agent_id).exists())

    def test_delete_user_as_agent_denied(self):
        resp = self.api_delete(detail_url(self.admin.id), user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_delete_self(self):
        resp = self.api_delete(detail_url(self.admin.id), user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestBulkActions(EchoDeskTenantTestCase):
    """Tests for bulk_action endpoint."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.agent1 = self.create_user(email='bulk1@test.com')
        self.agent2 = self.create_user(email='bulk2@test.com')

    def test_bulk_activate(self):
        self.agent1.is_active = False
        self.agent1.status = 'inactive'
        self.agent1.save()

        resp = self.api_post(f'{USERS_URL}bulk_action/', {
            'user_ids': [self.agent1.id],
            'action': 'activate',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent1.refresh_from_db()
        self.assertTrue(self.agent1.is_active)

    def test_bulk_deactivate(self):
        resp = self.api_post(f'{USERS_URL}bulk_action/', {
            'user_ids': [self.agent1.id, self.agent2.id],
            'action': 'deactivate',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent1.refresh_from_db()
        self.assertFalse(self.agent1.is_active)

    def test_bulk_delete_excludes_self(self):
        resp = self.api_post(f'{USERS_URL}bulk_action/', {
            'user_ids': [self.admin.id, self.agent1.id],
            'action': 'delete',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Admin should NOT be deleted
        self.assertTrue(User.objects.filter(id=self.admin.id).exists())
        # Agent should be deleted
        self.assertFalse(User.objects.filter(id=self.agent1.id).exists())

    def test_bulk_change_role(self):
        resp = self.api_post(f'{USERS_URL}bulk_action/', {
            'user_ids': [self.agent1.id, self.agent2.id],
            'action': 'change_role',
            'role': 'manager',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent1.refresh_from_db()
        self.assertEqual(self.agent1.role, 'manager')

    def test_bulk_action_no_permission_denied(self):
        resp = self.api_post(f'{USERS_URL}bulk_action/', {
            'user_ids': [self.agent2.id],
            'action': 'activate',
        }, user=self.agent1)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_bulk_count_after_update(self):
        """BUG 5 regression: count captured before .update() call."""
        resp = self.api_post(f'{USERS_URL}bulk_action/', {
            'user_ids': [self.agent1.id, self.agent2.id],
            'action': 'activate',
        }, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('2', resp.data['message'])


class TestChangePasswordAction(EchoDeskTenantTestCase):
    """Tests for UserViewSet.change_password action."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.agent = self.create_user(email='cpagent@test.com', password='oldpass123')

    def test_change_own_password(self):
        resp = self.api_post(
            f'{detail_url(self.agent.id)}change_password/',
            {
                'old_password': 'oldpass123',
                'new_password': 'Newpass123!',
                'new_password_confirm': 'Newpass123!',
            },
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.check_password('Newpass123!'))

    def test_change_other_password_as_admin(self):
        # PasswordChangeSerializer.validate_old_password checks request.user
        # (the admin), so we must send the admin's password as old_password.
        resp = self.api_post(
            f'{detail_url(self.agent.id)}change_password/',
            {
                'old_password': 'testpass123',
                'new_password': 'Adminset123!',
                'new_password_confirm': 'Adminset123!',
            },
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_change_other_password_as_agent_denied(self):
        other = self.create_user(email='other@test.com', password='otherpass')
        resp = self.api_post(
            f'{detail_url(other.id)}change_password/',
            {
                'old_password': 'otherpass',
                'new_password': 'Hacked123!',
                'new_password_confirm': 'Hacked123!',
            },
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestSendNewPassword(EchoDeskTenantTestCase):
    """Tests for UserViewSet.send_new_password action."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.agent = self.create_user(email='snpagent@test.com')

    @patch('users.views.send_new_password_email_task')
    def test_send_new_password_as_admin(self, mock_email):
        resp = self.api_post(
            f'{detail_url(self.agent.id)}send_new_password/',
            {},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.password_change_required)

    @patch('users.views.send_new_password_email_task')
    def test_send_new_password_as_agent_denied(self, mock_email):
        resp = self.api_post(
            f'{detail_url(self.admin.id)}send_new_password/',
            {},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
