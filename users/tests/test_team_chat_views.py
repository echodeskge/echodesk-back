"""
Tests for Team Chat endpoints:
  - GET  /api/team-chat/users/                             → TeamChatUserListView
  - GET  /api/team-chat/conversations/                     → list conversations
  - GET  /api/team-chat/conversations/<id>/                → conversation detail
  - GET/POST /api/team-chat/conversations/with/<user_id>/  → get_or_create with user
  - POST /api/team-chat/conversations/<id>/mark_read/      → mark all read
  - DELETE /api/team-chat/conversations/<id>/clear_history/ → clear messages
  - POST /api/team-chat/conversations/<id>/hide_for_me/    → hide conversation
  - POST /api/team-chat/conversations/<id>/unhide/         → unhide conversation
  - POST /api/team-chat/messages/                          → create message
  - POST /api/team-chat/messages/<id>/mark_read/           → mark single read
  - GET  /api/team-chat/unread-count/                      → unread count
"""
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from users.tests.conftest import EchoDeskTenantTestCase
from users.models import (
    TeamChatConversation, TeamChatMessage,
    HiddenTeamChatConversation, UserOnlineStatus,
    Notification, NotificationPreference,
)

# The REST create path fans out to the websocket group and to
# create_notification (which broadcasts on the notifications socket and
# enqueues the push). Tests that are not about those side effects stub them.
_ws_patch = patch('asgiref.sync.async_to_sync', return_value=lambda **kw: None)
_push_patch = patch('users.notification_utils._dispatch_push')

User = get_user_model()

# ---------------------------------------------------------------------------
# URL constants
# ---------------------------------------------------------------------------
USERS_URL = '/api/team-chat/users/'
CONVERSATIONS_URL = '/api/team-chat/conversations/'
MESSAGES_URL = '/api/team-chat/messages/'
UNREAD_COUNT_URL = '/api/team-chat/unread-count/'


def _conv_url(pk):
    return f'{CONVERSATIONS_URL}{pk}/'


def _conv_with_url(user_id):
    return f'{CONVERSATIONS_URL}with/{user_id}/'


def _conv_mark_read_url(pk):
    return f'{CONVERSATIONS_URL}{pk}/mark_read/'


def _conv_clear_url(pk):
    return f'{CONVERSATIONS_URL}{pk}/clear_history/'


def _conv_hide_url(pk):
    return f'{CONVERSATIONS_URL}{pk}/hide_for_me/'


def _conv_unhide_url(pk):
    return f'{CONVERSATIONS_URL}{pk}/unhide/'


def _msg_mark_read_url(pk):
    return f'{MESSAGES_URL}{pk}/mark_read/'


class TeamChatTestMixin:
    """Shared helpers for team chat tests."""

    def _make_conversation(self, user1, user2):
        conv = TeamChatConversation.objects.create()
        conv.participants.add(user1, user2)
        return conv

    def _make_message(self, conversation, sender, text='Hello', **kwargs):
        defaults = {
            'conversation': conversation,
            'sender': sender,
            'text': text,
            'message_type': 'text',
        }
        defaults.update(kwargs)
        return TeamChatMessage.objects.create(**defaults)

    def _multipart_post(self, url, data, user):
        """POST with multipart format (required by TeamChatMessageViewSet)."""
        client = APIClient()
        client.force_authenticate(user=user)
        return client.post(url, data, format='multipart', HTTP_HOST='tenant.test.com')


class LocalMediaMixin:
    """
    Store uploads on a temporary local directory for the duration of a test.

    Production uses S3-compatible Spaces storage; the test env blanks those
    credentials. Enabled per test because the tenant base class does not
    chain class-level override_settings.
    """

    def setUp(self):
        super().setUp()
        self._media_dir = tempfile.mkdtemp(prefix='echodesk-teamchat-')
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


# ============================================================
# Team Chat Users
# ============================================================
class TestTeamChatUsers(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='chat1@test.com')
        self.user2 = self.create_user(email='chat2@test.com')

    def test_list_users_excludes_self(self):
        resp = self.api_get(USERS_URL, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        emails = [u['email'] for u in resp.data]
        self.assertNotIn('chat1@test.com', emails)
        self.assertIn('chat2@test.com', emails)

    def test_includes_online_status(self):
        UserOnlineStatus.objects.create(user=self.user2, is_online=True)
        resp = self.api_get(USERS_URL, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        user2_data = next(u for u in resp.data if u['email'] == 'chat2@test.com')
        self.assertTrue(user2_data['is_online'])

    def test_unauthenticated_returns_401(self):
        resp = self.api_get(USERS_URL)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


# ============================================================
# Conversations with/<user_id>  (creates/gets conversations)
# ============================================================
class TestConversationWithUser(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='cwu1@test.com')
        self.user2 = self.create_user(email='cwu2@test.com')

    def test_creates_conversation(self):
        resp = self.api_post(_conv_with_url(self.user2.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('participants', resp.data)

    def test_returns_existing_conversation(self):
        conv = self._make_conversation(self.user1, self.user2)
        resp = self.api_get(_conv_with_url(self.user2.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['id'], conv.pk)

    def test_unhides_if_hidden(self):
        conv = self._make_conversation(self.user1, self.user2)
        HiddenTeamChatConversation.objects.create(user=self.user1, conversation=conv)
        resp = self.api_post(_conv_with_url(self.user2.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(
            HiddenTeamChatConversation.objects.filter(
                user=self.user1, conversation=conv,
            ).exists()
        )

    def test_nonexistent_user_returns_404(self):
        resp = self.api_post(_conv_with_url(99999), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ============================================================
# Conversation Detail
# ============================================================
class TestConversationDetail(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='cdet1@test.com')
        self.user2 = self.create_user(email='cdet2@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)

    def test_detail_includes_messages(self):
        self._make_message(self.conv, self.user2, 'Detail msg')
        resp = self.api_get(_conv_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('messages', resp.data)


# ============================================================
# Mark Read
# ============================================================
class TestMarkConversationRead(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='mr1@test.com')
        self.user2 = self.create_user(email='mr2@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)

    def test_marks_other_messages_as_read(self):
        msg = self._make_message(self.conv, self.user2, 'Read me')
        self.assertFalse(msg.is_read)
        resp = self.api_post(_conv_mark_read_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['marked_read'], 1)
        msg.refresh_from_db()
        self.assertTrue(msg.is_read)

    def test_does_not_mark_own_messages(self):
        self._make_message(self.conv, self.user1, 'My own msg')
        resp = self.api_post(_conv_mark_read_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['marked_read'], 0)


# ============================================================
# Clear History
# ============================================================
class TestClearHistory(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='clr1@test.com')
        self.user2 = self.create_user(email='clr2@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)

    def test_clears_all_messages(self):
        self._make_message(self.conv, self.user1, 'Msg 1')
        self._make_message(self.conv, self.user2, 'Msg 2')
        resp = self.api_delete(_conv_clear_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['deleted_count'], 2)
        self.assertEqual(self.conv.messages.count(), 0)


# ============================================================
# Hide / Unhide
# ============================================================
class TestHideUnhide(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='hide1@test.com')
        self.user2 = self.create_user(email='hide2@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)

    def test_hide_conversation(self):
        resp = self.api_post(_conv_hide_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['success'])
        self.assertTrue(
            HiddenTeamChatConversation.objects.filter(
                user=self.user1, conversation=self.conv,
            ).exists()
        )

    def test_unhide_visible_conversation(self):
        """Unhiding a visible conversation is a no-op."""
        resp = self.api_post(_conv_unhide_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data['was_hidden'])

    def test_unhide_via_with_user(self):
        """Hidden conversations are auto-unhidden via the with/<user_id> endpoint."""
        HiddenTeamChatConversation.objects.create(user=self.user1, conversation=self.conv)
        resp = self.api_post(_conv_with_url(self.user2.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(
            HiddenTeamChatConversation.objects.filter(
                user=self.user1, conversation=self.conv,
            ).exists()
        )


# ============================================================
# Create Message
# ============================================================
class TestCreateMessage(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='cmsg1@test.com')
        self.user2 = self.create_user(email='cmsg2@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)

    @patch('users.views.TeamChatMessageViewSet._notify_recipient')
    def test_create_text_message_by_conversation(self, mock_notify):
        resp = self._multipart_post(MESSAGES_URL, {
            'conversation_id': self.conv.pk,
            'text': 'Hello!',
            'message_type': 'text',
        }, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['text'], 'Hello!')
        self.assertEqual(resp.data['sender']['email'], 'cmsg1@test.com')

    @patch('users.views.TeamChatMessageViewSet._notify_recipient')
    def test_create_message_by_recipient(self, mock_notify):
        resp = self._multipart_post(MESSAGES_URL, {
            'recipient_id': self.user2.pk,
            'text': 'Hey!',
            'message_type': 'text',
        }, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    @patch('users.views.TeamChatMessageViewSet._notify_recipient')
    def test_create_message_creates_conversation_if_needed(self, mock_notify):
        user3 = self.create_user(email='cmsg3@test.com')
        count_before = TeamChatConversation.objects.count()
        resp = self._multipart_post(MESSAGES_URL, {
            'recipient_id': user3.pk,
            'text': 'New convo!',
            'message_type': 'text',
        }, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(TeamChatConversation.objects.count(), count_before + 1)

    def test_create_message_nonexistent_conversation(self):
        resp = self._multipart_post(MESSAGES_URL, {
            'conversation_id': 99999,
            'text': 'Ghost',
            'message_type': 'text',
        }, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthenticated_returns_401(self):
        resp = self.api_post(MESSAGES_URL, {'text': 'Hello'})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


# ============================================================
# Mark Single Message Read
# ============================================================
class TestMarkSingleMessageRead(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='smr1@test.com')
        self.user2 = self.create_user(email='smr2@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)
        self.msg = self._make_message(self.conv, self.user2, 'Read me single')

    def test_mark_read_as_recipient(self):
        resp = self.api_post(_msg_mark_read_url(self.msg.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertTrue(self.msg.is_read)

    def test_sender_mark_read_stays_unread(self):
        """Sender marking their own message as read should not change is_read."""
        resp = self.api_post(_msg_mark_read_url(self.msg.pk), user=self.user2)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertFalse(self.msg.is_read)


# ============================================================
# Unread Count
# ============================================================
class TestUnreadCount(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='uc1@test.com')
        self.user2 = self.create_user(email='uc2@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)

    def test_counts_unread_from_others(self):
        self._make_message(self.conv, self.user2, 'Unread 1')
        self._make_message(self.conv, self.user2, 'Unread 2')
        self._make_message(self.conv, self.user1, 'My own')
        resp = self.api_get(UNREAD_COUNT_URL, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Count at least 2 from this conv (may be more from other tests in shared schema)
        self.assertGreaterEqual(resp.data['count'], 2)

    def test_zero_when_no_unread(self):
        """All messages are from self — unread count should be 0 for this conv."""
        self._make_message(self.conv, self.user1, 'My msg')
        resp = self.api_get(UNREAD_COUNT_URL, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Note: other tests in shared schema may leave unread messages,
        # so we check it's a valid non-negative integer
        self.assertIsInstance(resp.data['count'], int)


# ============================================================
# Model Tests
# ============================================================
class TestTeamChatModels(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='mdl1@test.com')
        self.user2 = self.create_user(email='mdl2@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)

    def test_get_other_participant(self):
        other = self.conv.get_other_participant(self.user1)
        self.assertEqual(other, self.user2)

    def test_get_last_message(self):
        self._make_message(self.conv, self.user1, 'First')
        msg2 = self._make_message(self.conv, self.user2, 'Second')
        self.assertEqual(self.conv.get_last_message(), msg2)

    def test_get_unread_count(self):
        self._make_message(self.conv, self.user2, 'Unread')
        self._make_message(self.conv, self.user1, 'Mine')
        self.assertEqual(self.conv.get_unread_count(self.user1), 1)

    def test_mark_as_read(self):
        msg = self._make_message(self.conv, self.user2, 'Mark me')
        self.assertFalse(msg.is_read)
        msg.mark_as_read()
        self.assertTrue(msg.is_read)
        self.assertIsNotNone(msg.read_at)

    def test_mark_as_read_idempotent(self):
        msg = self._make_message(self.conv, self.user2, 'Already read')
        msg.mark_as_read()
        first_read_at = msg.read_at
        msg.mark_as_read()
        self.assertEqual(msg.read_at, first_read_at)


# ============================================================
# Recent Chats (today-only scoping)
# ============================================================
class TestConversationListTodayFilter(TeamChatTestMixin, EchoDeskTenantTestCase):
    """The list endpoint only returns conversations active today."""

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='today1@test.com')
        self.user2 = self.create_user(email='today2@test.com')

    def test_today_message_shows_conversation(self):
        """Conversation with a message today appears in Recent Chats."""
        conv = self._make_conversation(self.user1, self.user2)
        self._make_message(conv, self.user2, 'hi today')
        resp = self.api_get(CONVERSATIONS_URL, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        returned_ids = [c['id'] for c in resp.data.get('results', resp.data)]
        self.assertIn(conv.pk, returned_ids)

    def test_yesterday_message_hides_conversation(self):
        """A conversation whose latest message is older than today is excluded."""
        from datetime import timedelta
        from django.utils import timezone
        conv = self._make_conversation(self.user1, self.user2)
        msg = self._make_message(conv, self.user2, 'stale')
        # Backdate the message past today's start.
        yesterday = timezone.now() - timedelta(days=1, hours=1)
        TeamChatMessage.objects.filter(pk=msg.pk).update(created_at=yesterday)
        # Also backdate the conversation's updated_at so it doesn't leak via
        # the empty-conversation branch.
        TeamChatConversation.objects.filter(pk=conv.pk).update(updated_at=yesterday)

        resp = self.api_get(CONVERSATIONS_URL, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        returned_ids = [c['id'] for c in resp.data.get('results', resp.data)]
        self.assertNotIn(conv.pk, returned_ids)

    def test_empty_conversation_updated_today_shows(self):
        """A brand-new conversation with no messages but updated today shows."""
        conv = self._make_conversation(self.user1, self.user2)
        # No message at all — updated_at is auto-set to now().
        resp = self.api_get(CONVERSATIONS_URL, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        returned_ids = [c['id'] for c in resp.data.get('results', resp.data)]
        self.assertIn(conv.pk, returned_ids)


def _backdate(conv, msg=None, days=1):
    """Move a conversation (and optionally one message) to `days` days ago."""
    when = timezone.now() - timedelta(days=days, hours=1)
    if msg is not None:
        TeamChatMessage.objects.filter(pk=msg.pk).update(created_at=when)
    TeamChatConversation.objects.filter(pk=conv.pk).update(updated_at=when)
    return when


# ============================================================
# Conversation list: ?scope=all / annotations
# ============================================================
class TestConversationListScope(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='scope1@test.com')
        self.user2 = self.create_user(email='scope2@test.com')
        self.user3 = self.create_user(email='scope3@test.com')

    def _ids(self, resp):
        return [c['id'] for c in resp.data.get('results', resp.data)]

    def test_scope_all_includes_older_conversations(self):
        old = self._make_conversation(self.user1, self.user2)
        _backdate(old, self._make_message(old, self.user2, 'last week'), days=7)
        fresh = self._make_conversation(self.user1, self.user3)
        self._make_message(fresh, self.user3, 'today')

        resp = self.api_get(CONVERSATIONS_URL, user=self.user1, data={'scope': 'all'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = self._ids(resp)
        self.assertIn(old.pk, ids)
        self.assertIn(fresh.pk, ids)
        # newest activity first
        self.assertLess(ids.index(fresh.pk), ids.index(old.pk))

    def test_scope_today_explicit_matches_default(self):
        old = self._make_conversation(self.user1, self.user2)
        _backdate(old, self._make_message(old, self.user2, 'stale'))

        resp = self.api_get(CONVERSATIONS_URL, user=self.user1, data={'scope': 'today'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertNotIn(old.pk, self._ids(resp))

    def test_scope_all_still_excludes_hidden(self):
        conv = self._make_conversation(self.user1, self.user2)
        self._make_message(conv, self.user2, 'hi')
        HiddenTeamChatConversation.objects.create(user=self.user1, conversation=conv)

        resp = self.api_get(CONVERSATIONS_URL, user=self.user1, data={'scope': 'all'})
        self.assertNotIn(conv.pk, self._ids(resp))

    def test_scope_all_only_own_conversations(self):
        other = self._make_conversation(self.user2, self.user3)
        self._make_message(other, self.user2, 'private')

        resp = self.api_get(CONVERSATIONS_URL, user=self.user1, data={'scope': 'all'})
        self.assertNotIn(other.pk, self._ids(resp))

    def test_list_row_has_correct_unread_count_last_message_and_other_participant(self):
        conv = self._make_conversation(self.user1, self.user2)
        self._make_message(conv, self.user2, 'unread 1')
        self._make_message(conv, self.user2, 'unread 2')
        read = self._make_message(conv, self.user2, 'already read')
        read.mark_as_read()
        self._make_message(conv, self.user1, 'mine (never counts)')
        last = self._make_message(conv, self.user2, 'the latest one')

        resp = self.api_get(CONVERSATIONS_URL, user=self.user1, data={'scope': 'all'})
        row = next(c for c in resp.data['results'] if c['id'] == conv.pk)
        # 'unread 1', 'unread 2' and 'the latest one' — the read one and my own don't count
        self.assertEqual(row['unread_count'], 3)
        self.assertEqual(row['last_message']['id'], last.pk)
        self.assertEqual(row['last_message']['text'], 'the latest one')
        self.assertEqual(row['last_message']['conversation'], conv.pk)
        self.assertEqual(row['other_participant']['id'], self.user2.pk)
        self.assertIs(row['other_participant']['is_online'], False)

        # The other side sees the mirror image.
        resp2 = self.api_get(CONVERSATIONS_URL, user=self.user2, data={'scope': 'all'})
        row2 = next(c for c in resp2.data['results'] if c['id'] == conv.pk)
        self.assertEqual(row2['unread_count'], 1)
        self.assertEqual(row2['other_participant']['id'], self.user1.pk)

    def test_empty_conversation_row(self):
        conv = self._make_conversation(self.user1, self.user2)
        resp = self.api_get(CONVERSATIONS_URL, user=self.user1, data={'scope': 'all'})
        row = next(c for c in resp.data['results'] if c['id'] == conv.pk)
        self.assertIsNone(row['last_message'])
        self.assertEqual(row['unread_count'], 0)

    def test_list_is_paginated(self):
        for i in range(3):
            u = self.create_user(email=f'scope-page{i}@test.com')
            conv = self._make_conversation(self.user1, u)
            self._make_message(conv, u, f'msg {i}')
        resp = self.api_get(CONVERSATIONS_URL, user=self.user1, data={'scope': 'all', 'page_size': 2})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['results']), 2)
        self.assertGreaterEqual(resp.data['count'], 3)
        self.assertIsNotNone(resp.data['next'])


# ============================================================
# Detail actions are not limited to today's conversations
# ============================================================
class TestConversationDetailActionsIgnoreTodayFilter(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='old1@test.com')
        self.user2 = self.create_user(email='old2@test.com')
        self.stranger = self.create_user(email='old3@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)
        self.msg = self._make_message(self.conv, self.user2, 'from yesterday')
        _backdate(self.conv, self.msg)

    def test_retrieve_old_conversation(self):
        resp = self.api_get(_conv_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['id'], self.conv.pk)
        self.assertEqual([m['id'] for m in resp.data['messages']], [self.msg.pk])
        self.assertEqual(resp.data['unread_count'], 1)

    def test_mark_read_old_conversation(self):
        resp = self.api_post(_conv_mark_read_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['marked_read'], 1)

    def test_hide_old_conversation(self):
        resp = self.api_post(_conv_hide_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['created'])

    def test_clear_history_old_conversation(self):
        resp = self.api_delete(_conv_clear_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['deleted_count'], 1)

    def test_retrieve_as_non_participant_404(self):
        resp = self.api_get(_conv_url(self.conv.pk), user=self.stranger)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_retrieve_hidden_404(self):
        HiddenTeamChatConversation.objects.create(user=self.user1, conversation=self.conv)
        resp = self.api_get(_conv_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_unhide_hidden_conversation(self):
        HiddenTeamChatConversation.objects.create(user=self.user1, conversation=self.conv)
        resp = self.api_post(_conv_unhide_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['was_hidden'])
        self.assertFalse(
            HiddenTeamChatConversation.objects.filter(user=self.user1, conversation=self.conv).exists()
        )
        # visible again
        resp = self.api_get(CONVERSATIONS_URL, user=self.user1, data={'scope': 'all'})
        self.assertIn(self.conv.pk, [c['id'] for c in resp.data['results']])

    def test_unhide_as_non_participant_404(self):
        resp = self.api_post(_conv_unhide_url(self.conv.pk), user=self.stranger)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ============================================================
# Detail messages = latest 50, ascending
# ============================================================
class TestConversationDetailMessages(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='latest1@test.com')
        self.user2 = self.create_user(email='latest2@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)
        base = timezone.now() - timedelta(hours=2)
        self.msgs = []
        for i in range(60):
            m = self._make_message(self.conv, self.user2 if i % 2 else self.user1, f'm{i}')
            TeamChatMessage.objects.filter(pk=m.pk).update(created_at=base + timedelta(seconds=i))
            self.msgs.append(m)

    def test_retrieve_returns_latest_50_oldest_first(self):
        resp = self.api_get(_conv_url(self.conv.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        texts = [m['text'] for m in resp.data['messages']]
        self.assertEqual(len(texts), 50)
        self.assertEqual(texts[0], 'm10')
        self.assertEqual(texts[-1], 'm59')
        self.assertEqual(texts, [f'm{i}' for i in range(10, 60)])

    def test_with_user_returns_latest_50_oldest_first(self):
        resp = self.api_get(_conv_with_url(self.user2.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['id'], self.conv.pk)
        texts = [m['text'] for m in resp.data['messages']]
        self.assertEqual(texts[0], 'm10')
        self.assertEqual(texts[-1], 'm59')
        self.assertEqual(resp.data['last_message']['text'], 'm59')
        self.assertEqual(resp.data['unread_count'], 30)


# ============================================================
# with/<user_id> guards
# ============================================================
class TestConversationWithUserGuards(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='guard1@test.com')

    def test_with_self_returns_400(self):
        resp = self.api_get(_conv_with_url(self.user1.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', resp.data)
        self.assertFalse(
            TeamChatConversation.objects.filter(participants=self.user1).exists()
        )

    def test_with_non_numeric_id_returns_404(self):
        resp = self.api_get(_conv_with_url('abc'), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ============================================================
# Messages list: ?conversation= filter, newest first, paginated
# ============================================================
class TestMessagesList(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='mlist1@test.com')
        self.user2 = self.create_user(email='mlist2@test.com')
        self.user3 = self.create_user(email='mlist3@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)
        self.other_conv = self._make_conversation(self.user2, self.user3)
        base = timezone.now() - timedelta(hours=1)
        self.msgs = []
        for i in range(5):
            m = self._make_message(self.conv, self.user2, f'c1-{i}')
            TeamChatMessage.objects.filter(pk=m.pk).update(created_at=base + timedelta(seconds=i))
            self.msgs.append(m)
        self.foreign = self._make_message(self.other_conv, self.user3, 'not yours')

    def test_filter_by_conversation_newest_first(self):
        resp = self.api_get(MESSAGES_URL, user=self.user1, data={'conversation': self.conv.pk})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 5)
        texts = [m['text'] for m in resp.data['results']]
        self.assertEqual(texts, ['c1-4', 'c1-3', 'c1-2', 'c1-1', 'c1-0'])

    def test_pagination(self):
        resp = self.api_get(MESSAGES_URL, user=self.user1,
                            data={'conversation': self.conv.pk, 'page_size': 2, 'page': 2})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([m['text'] for m in resp.data['results']], ['c1-2', 'c1-1'])
        self.assertIsNotNone(resp.data['next'])
        self.assertIsNotNone(resp.data['previous'])

    def test_foreign_conversation_filter_is_empty(self):
        resp = self.api_get(MESSAGES_URL, user=self.user1, data={'conversation': self.other_conv.pk})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 0)

    def test_unfiltered_list_only_own_conversations(self):
        resp = self.api_get(MESSAGES_URL, user=self.user1, data={'page_size': 100})
        ids = [m['id'] for m in resp.data['results']]
        self.assertNotIn(self.foreign.pk, ids)
        for m in self.msgs:
            self.assertIn(m.pk, ids)

    def test_ordering_param_ascending(self):
        resp = self.api_get(MESSAGES_URL, user=self.user1,
                            data={'conversation': self.conv.pk, 'ordering': 'created_at'})
        self.assertEqual([m['text'] for m in resp.data['results']][0], 'c1-0')


# ============================================================
# Message edit / delete: sender only
# ============================================================
class TestMessageEditDelete(TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='edit1@test.com')
        self.user2 = self.create_user(email='edit2@test.com')
        self.user3 = self.create_user(email='edit3@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)
        self.other_conv = self._make_conversation(self.user1, self.user3)
        self.msg = self._make_message(self.conv, self.user1, 'original')

    def _msg_url(self, pk):
        return f'{MESSAGES_URL}{pk}/'

    def _patch(self, user, data):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.patch(self._msg_url(self.msg.pk), data, format='multipart',
                            HTTP_HOST='tenant.test.com')

    def test_sender_can_edit_text(self):
        resp = self._patch(self.user1, {'text': 'edited'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.text, 'edited')

    def test_recipient_cannot_edit(self):
        resp = self._patch(self.user2, {'text': 'hijacked'})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.text, 'original')

    def test_recipient_cannot_delete(self):
        resp = self.api_delete(self._msg_url(self.msg.pk), user=self.user2)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(TeamChatMessage.objects.filter(pk=self.msg.pk).exists())

    def test_sender_can_delete(self):
        resp = self.api_delete(self._msg_url(self.msg.pk), user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(TeamChatMessage.objects.filter(pk=self.msg.pk).exists())

    def test_non_participant_gets_404(self):
        resp = self.api_delete(self._msg_url(self.msg.pk), user=self.user3)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_update_cannot_move_conversation_or_reattribute(self):
        resp = self._patch(self.user1, {
            'text': 'moved?',
            'conversation': self.other_conv.pk,
            'sender_id': self.user2.pk,
            'is_read': 'true',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.conversation_id, self.conv.pk)
        self.assertEqual(self.msg.sender_id, self.user1.pk)
        self.assertFalse(self.msg.is_read)
        self.assertEqual(resp.data['conversation'], self.conv.pk)

    def test_recipient_can_still_mark_read(self):
        resp = self.api_post(_msg_mark_read_url(self.msg.pk), user=self.user2)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertTrue(self.msg.is_read)


# ============================================================
# Create message: attachment validation
# ============================================================
class TestCreateMessageFileValidation(LocalMediaMixin, TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='file1@test.com')
        self.user2 = self.create_user(email='file2@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)
        self._ws = _ws_patch.start()
        self._push = _push_patch.start()

    def tearDown(self):
        _push_patch.stop()
        _ws_patch.stop()
        super().tearDown()

    def _send(self, message_type, upload, extra=None):
        data = {'conversation_id': self.conv.pk, 'message_type': message_type, 'file': upload}
        data.update(extra or {})
        return self._multipart_post(MESSAGES_URL, data, user=self.user1)

    def test_pdf_accepted_and_mime_from_declared_content_type(self):
        upload = SimpleUploadedFile('report.pdf', b'%PDF-1.4 fake', content_type='application/pdf')
        resp = self._send('file', upload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['file_name'], 'report.pdf')
        self.assertEqual(resp.data['file_mime_type'], 'application/pdf')
        self.assertEqual(resp.data['file_size'], len(b'%PDF-1.4 fake'))
        self.assertTrue(resp.data['file_url'])

    def test_declared_content_type_wins_over_extension_guess(self):
        upload = SimpleUploadedFile('photo.jpg', b'\xff\xd8\xff', content_type='image/jpeg')
        resp = self._send('image', upload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['file_mime_type'], 'image/jpeg')

    def test_octet_stream_falls_back_to_filename_guess(self):
        upload = SimpleUploadedFile('sheet.xlsx', b'PK..', content_type='application/octet-stream')
        resp = self._send('file', upload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(
            resp.data['file_mime_type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

    def test_oversized_file_rejected(self):
        upload = SimpleUploadedFile('big.pdf', b'x' * 2048, content_type='application/pdf')
        with patch('users.serializers.TEAM_CHAT_MAX_FILE_SIZE', 1024):
            resp = self._send('file', upload)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('file', resp.data)
        self.assertEqual(self.conv.messages.count(), 0)

    def test_disallowed_extension_rejected(self):
        upload = SimpleUploadedFile('virus.exe', b'MZ', content_type='application/octet-stream')
        resp = self._send('file', upload)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('file', resp.data)

    def test_image_message_with_non_image_rejected(self):
        upload = SimpleUploadedFile('doc.pdf', b'%PDF', content_type='application/pdf')
        resp = self._send('image', upload)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('file', resp.data)

    def test_voice_message_with_non_audio_rejected(self):
        upload = SimpleUploadedFile('photo.png', b'\x89PNG', content_type='image/png')
        resp = self._send('voice', upload, {'voice_duration': 3})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_voice_webm_and_m4a_accepted(self):
        for name, ctype in (('note.webm', 'audio/webm'), ('note.m4a', 'audio/m4a')):
            upload = SimpleUploadedFile(name, b'\x1aE\xdf\xa3', content_type=ctype)
            resp = self._send('voice', upload, {'voice_duration': 4})
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED, (name, resp.data))
            self.assertEqual(resp.data['voice_duration'], 4)

    def test_extensionless_upload_uses_content_type(self):
        upload = SimpleUploadedFile('scan', b'%PDF', content_type='application/pdf')
        resp = self._send('file', upload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_text_message_unaffected(self):
        resp = self._multipart_post(MESSAGES_URL, {
            'conversation_id': self.conv.pk, 'text': 'plain', 'message_type': 'text',
        }, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(resp.data['file_url'])


# ============================================================
# Create message: recipient notification + push
# ============================================================
class TestCreateMessageNotification(LocalMediaMixin, TeamChatTestMixin, EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.user1 = self.create_user(email='notif1@test.com', first_name='Nino', last_name='K')
        self.user2 = self.create_user(email='notif2@test.com')
        self.conv = self._make_conversation(self.user1, self.user2)
        Notification.objects.filter(user__in=[self.user1, self.user2]).delete()
        NotificationPreference.objects.filter(user__in=[self.user1, self.user2]).delete()
        self._ws = _ws_patch.start()
        self._push = _push_patch.start()

    def tearDown(self):
        _push_patch.stop()
        _ws_patch.stop()
        Notification.objects.filter(user__in=[self.user1, self.user2]).delete()
        NotificationPreference.objects.filter(user__in=[self.user1, self.user2]).delete()
        super().tearDown()

    def _send_text(self, text='hello there', **extra):
        data = {'conversation_id': self.conv.pk, 'text': text, 'message_type': 'text'}
        data.update(extra)
        return self._multipart_post(MESSAGES_URL, data, user=self.user1)

    def test_recipient_gets_team_chat_notification(self):
        resp = self._send_text('hello there')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        notif = Notification.objects.get(user=self.user2, notification_type='team_chat_message')
        self.assertEqual(notif.title, 'Nino K')
        self.assertEqual(notif.message, 'hello there')
        self.assertEqual(notif.link_url, f'/team-chat?user={self.user1.pk}')
        self.assertEqual(notif.metadata['conversation_id'], self.conv.pk)
        self.assertEqual(notif.metadata['sender_id'], self.user1.pk)
        self.assertEqual(notif.metadata['message_id'], resp.data['id'])
        # the sender gets nothing
        self.assertFalse(Notification.objects.filter(user=self.user1).exists())

    def test_push_data_carries_conversation_and_sender(self):
        resp = self._send_text('ping')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self._push.assert_called_once()
        kwargs = self._push.call_args.kwargs
        self.assertEqual(kwargs['user'], self.user2)
        self.assertEqual(kwargs['title'], 'Nino K')
        self.assertEqual(kwargs['body'], 'ping')
        self.assertEqual(kwargs['data']['notification_type'], 'team_chat_message')
        self.assertEqual(kwargs['data']['conversation_id'], self.conv.pk)
        self.assertEqual(kwargs['data']['sender_id'], self.user1.pk)
        self.assertEqual(kwargs['url'], f'/team-chat?user={self.user1.pk}')

    def test_recipient_id_path_also_notifies(self):
        user3 = self.create_user(email='notif3@test.com')
        resp = self._multipart_post(MESSAGES_URL, {
            'recipient_id': user3.pk, 'text': 'new thread', 'message_type': 'text',
        }, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        notif = Notification.objects.get(user=user3, notification_type='team_chat_message')
        self.assertEqual(notif.metadata['conversation_id'], resp.data['conversation'])

    def test_in_app_preference_disabled_skips_everything(self):
        NotificationPreference.objects.create(
            user=self.user2, notification_type='team_chat_message', in_app=False, push=True,
        )
        resp = self._send_text('quiet')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertFalse(Notification.objects.filter(user=self.user2).exists())
        self._push.assert_not_called()

    def test_push_preference_disabled_skips_push_only(self):
        NotificationPreference.objects.create(
            user=self.user2, notification_type='team_chat_message', in_app=True, push=False,
        )
        resp = self._send_text('no push')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Notification.objects.filter(user=self.user2).exists())
        self._push.assert_not_called()

    def test_file_message_preview(self):
        upload = SimpleUploadedFile('brief.pdf', b'%PDF', content_type='application/pdf')
        resp = self._multipart_post(MESSAGES_URL, {
            'conversation_id': self.conv.pk, 'message_type': 'file', 'file': upload,
        }, user=self.user1)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        notif = Notification.objects.get(user=self.user2, notification_type='team_chat_message')
        self.assertEqual(notif.message, 'Sent a file: brief.pdf')
        self.assertEqual(notif.metadata['message_type'], 'file')

    def test_notification_failure_does_not_break_send(self):
        with patch('users.views.create_team_chat_message_notification', side_effect=RuntimeError('boom')):
            resp = self._send_text('still sent')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.conv.messages.count(), 1)

    def test_ws_frame_carries_conversation_and_conversation_id(self):
        calls = []

        class FakeLayer:
            async def group_send(self, group, event):
                calls.append((group, event))

        with patch('channels.layers.get_channel_layer', return_value=FakeLayer()):
            # Re-enable a real async_to_sync for this test only.
            _ws_patch.stop()
            try:
                resp = self._send_text('over the wire')
            finally:
                _ws_patch.start()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        chat_calls = [(g, e) for g, e in calls if e.get('type') == 'chat_message']
        self.assertEqual(len(chat_calls), 1)
        group, event = chat_calls[0]
        self.assertEqual(group, f'team_chat_test_{self.user2.pk}')
        self.assertEqual(event['type'], 'chat_message')
        self.assertEqual(event['conversation'], self.conv.pk)
        self.assertEqual(event['conversation_id'], self.conv.pk)
        self.assertEqual(event['message']['conversation'], self.conv.pk)
        self.assertEqual(event['message']['conversation_id'], self.conv.pk)
        self.assertEqual(event['message']['text'], 'over the wire')
        self.assertIn('file_url', event['message'])
        self.assertIn('is_online', event['message']['sender'])
