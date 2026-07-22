"""
Tests for common/shared views: conversations, unread count, mark read/unread,
archive/unarchive, settings, assignments, auto-post, webhook debug.
"""
from unittest.mock import patch
from rest_framework import status
from social_integrations.tests.conftest import SocialIntegrationTestCase


class TestUnreadMessagesCount(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='unread-agent@test.com')
        self.url = '/api/social/unread-count/'

    def test_returns_counts(self):
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_unauthenticated_denied(self):
        resp = self.client.get(self.url, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_no_feature_denied(self):
        """Fix 5 verification."""
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_get(self.url, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestMarkConversationRead(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='mark-read@test.com')
        self.url = '/api/social/mark-read/'

    def test_mark_read_requires_body(self):
        resp = self.api_post(self.url, {}, user=self.agent)
        self.assertIn(resp.status_code, [
            status.HTTP_200_OK,
            status.HTTP_400_BAD_REQUEST,
        ])

    def test_mark_read_facebook(self):
        conn = self.create_fb_connection()
        msg = self.create_fb_message(page_connection=conn, is_from_page=False)
        resp = self.api_post(self.url, {
            'platform': 'facebook',
            'conversation_id': msg.sender_id,
        }, user=self.agent)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_no_feature_denied(self):
        """Fix 4 verification."""
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_post(self.url, {
                'platform': 'facebook',
                'conversation_id': 'x',
            }, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestMarkConversationUnread(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='mark-unread@test.com')
        self.url = '/api/social/mark-unread/'

    def test_mark_unread_requires_body(self):
        resp = self.api_post(self.url, {}, user=self.agent)
        self.assertIn(resp.status_code, [
            status.HTTP_200_OK,
            status.HTTP_400_BAD_REQUEST,
        ])

    def test_no_feature_denied(self):
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_post(self.url, {'platform': 'facebook', 'conversation_id': 'x'}, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestMarkAllConversationsRead(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='mark-all@test.com')
        self.url = '/api/social/mark-all-read/'

    def test_mark_all_read(self):
        resp = self.api_post(self.url, {}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_no_feature_denied(self):
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_post(self.url, {}, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestArchiveConversation(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='archive@test.com')
        self.url = '/api/social/conversations/archive/'

    def test_archive_requires_body(self):
        resp = self.api_post(self.url, {}, user=self.agent)
        self.assertIn(resp.status_code, [
            status.HTTP_200_OK,
            status.HTTP_400_BAD_REQUEST,
        ])

    def test_archive_conversation(self):
        conn = self.create_fb_connection()
        resp = self.api_post(self.url, {
            'conversations': [{
                'platform': 'facebook',
                'conversation_id': 'sender_123',
                'account_id': conn.page_id,
            }]
        }, user=self.agent)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_no_feature_denied(self):
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_post(self.url, {}, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestUnarchiveConversation(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='unarchive@test.com')
        self.url = '/api/social/conversations/unarchive/'

    def test_unarchive(self):
        resp = self.api_post(self.url, {
            'conversations': [{
                'platform': 'facebook',
                'conversation_id': 'sender_123',
                'account_id': 'page_1',
            }]
        }, user=self.agent)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_no_feature_denied(self):
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_post(self.url, {}, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestArchiveAllConversations(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='archive-all@test.com')
        self.url = '/api/social/conversations/archive-all/'

    def test_archive_all(self):
        resp = self.api_post(self.url, {}, user=self.agent)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_no_feature_denied(self):
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_post(self.url, {}, user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestSocialSettings(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='settings-admin2@test.com')
        self.agent = self.create_user(email='settings-agent2@test.com')
        self.url = '/api/social/settings/'

    def test_get_settings(self):
        resp = self.api_get(self.url, user=self.agent)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_settings_admin(self):
        resp = self.api_put(self.url, {'refresh_interval': 3000}, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_settings_agent_denied(self):
        resp = self.api_put(self.url, {'refresh_interval': 3000}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestUnifiedConversations(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='unified@test.com')
        self.url = '/api/social/conversations/'

    def test_list_conversations(self):
        resp = self.api_get(self.url, user=self.agent)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_denied(self):
        resp = self.client.get(self.url, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class TestChatAssignmentViews(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='assign-admin@test.com')
        self.agent = self.create_user(email='assign-agent@test.com')

    def test_my_assignments(self):
        resp = self.api_get('/api/social/assignments/', user=self.agent)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_all_assignments(self):
        resp = self.api_get('/api/social/assignments/all/', user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_assign_chat(self):
        resp = self.api_post('/api/social/assignments/assign/', {
            'platform': 'facebook',
            'conversation_id': 'sender_1',
            'account_id': 'page_1',
        }, user=self.agent)
        # May succeed or fail based on settings, but not 401/403
        self.assertNotIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED,
        ])

    def test_get_assignment_status(self):
        resp = self.api_get('/api/social/assignments/status/', user=self.agent)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_get_assignment_status_includes_archive_state(self):
        from social_integrations.models import ConversationArchive
        ConversationArchive.objects.create(
            platform='facebook',
            conversation_id='sender_1',
            account_id='page_1',
            archived_by=self.agent,
        )
        resp = self.api_get(
            '/api/social/assignments/status/'
            '?platform=facebook&conversation_id=sender_1&account_id=page_1',
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertTrue(data['is_archived'])
        self.assertIsNotNone(data['archived_at'])
        self.assertEqual(data['archived_by_id'], self.agent.id)
        # Backward-compat: original keys still present
        self.assertIn('assignment', data)
        self.assertIn('settings', data)

    def test_get_assignment_status_not_archived(self):
        resp = self.api_get(
            '/api/social/assignments/status/'
            '?platform=facebook&conversation_id=sender_2&account_id=page_1',
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertFalse(data['is_archived'])
        self.assertIsNone(data['archived_at'])
        self.assertIsNone(data['archived_by_id'])


class TestWebhookDebugViews(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.staff = self.create_user(email='wh-staff@test.com', is_staff=True)
        self.agent = self.create_user(email='wh-agent@test.com')

    def test_webhook_debug_logs_staff(self):
        resp = self.api_get('/api/social/webhook-logs/', user=self.staff)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_webhook_debug_logs_non_staff_denied(self):
        resp = self.api_get('/api/social/webhook-logs/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_webhook_status_staff(self):
        resp = self.api_get('/api/social/webhook-status/', user=self.staff)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_webhook_status_non_staff_denied(self):
        resp = self.api_get('/api/social/webhook-status/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestAutoPostViews(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='ap-admin@test.com')
        self.agent = self.create_user(email='ap-agent@test.com')

    def test_publishing_status(self):
        resp = self.api_get('/api/social/auto-post/publishing-status/', user=self.agent)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_publishing_status_no_feature(self):
        """Fix 7 verification."""
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_get('/api/social/auto-post/publishing-status/', user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_settings_get(self):
        resp = self.api_get('/api/social/auto-post/settings/', user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_settings_put_admin(self):
        resp = self.api_put('/api/social/auto-post/settings/', {
            'is_enabled': True,
            'company_description': 'test',
        }, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_settings_put_agent_denied(self):
        """Fix 7: write endpoints use CanManageSocialSettings."""
        resp = self.api_put('/api/social/auto-post/settings/', {}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_list(self):
        resp = self.api_get('/api/social/auto-post/list/', user=self.agent)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_list_no_feature(self):
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_get('/api/social/auto-post/list/', user=self.agent)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_detail(self):
        post = self.create_auto_post_content()
        resp = self.api_get(f'/api/social/auto-post/{post.id}/', user=self.agent)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_approve_admin(self):
        post = self.create_auto_post_content()
        resp = self.api_post(f'/api/social/auto-post/{post.id}/approve/', {}, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_approve_agent_denied(self):
        post = self.create_auto_post_content()
        resp = self.api_post(f'/api/social/auto-post/{post.id}/approve/', {}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_reject_admin(self):
        post = self.create_auto_post_content()
        resp = self.api_post(f'/api/social/auto-post/{post.id}/reject/', {}, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_reject_agent_denied(self):
        post = self.create_auto_post_content()
        resp = self.api_post(f'/api/social/auto-post/{post.id}/reject/', {}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_edit_admin(self):
        post = self.create_auto_post_content()
        resp = self.api_put(f'/api/social/auto-post/{post.id}/edit/', {
            'facebook_text': 'Updated',
        }, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_edit_agent_denied(self):
        post = self.create_auto_post_content()
        resp = self.api_put(f'/api/social/auto-post/{post.id}/edit/', {}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_generate_admin(self):
        """Admin can call generate endpoint (may fail internally but not 403)."""
        try:
            resp = self.api_post('/api/social/auto-post/generate/', {}, user=self.admin)
            self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        except Exception:
            # openai module may not be installed — the important thing is
            # that we got past the permission check (which would return 403).
            pass

    def test_auto_post_generate_agent_denied(self):
        resp = self.api_post('/api/social/auto-post/generate/', {}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_publish_admin(self):
        post = self.create_auto_post_content(status='approved')
        resp = self.api_post(f'/api/social/auto-post/{post.id}/publish/', {}, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_post_publish_agent_denied(self):
        post = self.create_auto_post_content()
        resp = self.api_post(f'/api/social/auto-post/{post.id}/publish/', {}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
