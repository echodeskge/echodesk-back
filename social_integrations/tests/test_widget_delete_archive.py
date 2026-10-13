"""Deleting and archiving website (widget) chats — bugs #228 and #229.

The dashboard names a widget chat by session id, by the composite list id
"widget_<conn>_<session>", or by the visitor id; all must reach the chat.
"""
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
from rest_framework import status

from social_integrations.models import ConversationArchive, WidgetMessage, WidgetSession
from social_integrations.tests.conftest import SocialIntegrationTestCase
from social_integrations.views import widget_session_ids

DELETE_URL = '/api/social/delete-conversation/'
ARCHIVE_URL = '/api/social/conversations/archive/'
UNARCHIVE_URL = '/api/social/conversations/unarchive/'
ARCHIVE_ALL_URL = '/api/social/conversations/archive-all/'


class WidgetDeleteArchiveTests(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='widget-admin@test.com')
        self.session = WidgetSession.objects.create(
            connection_id=3, session_id='c6d9aabc55174af299ee8a27b8dc0125',
            visitor_id='797b3a7d-54fd-40bf-9fde-8a81c9db72bf',
        )
        now = timezone.now()
        for i, from_visitor in enumerate([True, False, True]):
            WidgetMessage.objects.create(
                session=self.session, message_id=f'wm-{i}', message_text=f'msg {i}',
                is_from_visitor=from_visitor, timestamp=now - timedelta(minutes=5 - i),
            )

    def test_resolves_every_id_form(self):
        sid = self.session.session_id
        self.assertEqual(widget_session_ids(sid), [sid])
        self.assertEqual(widget_session_ids(f'widget_3_{sid}'), [sid])
        self.assertEqual(widget_session_ids(self.session.visitor_id), [sid])

    def _delete(self, conversation_id):
        # delete-conversation is staff-only; the test admin is staff
        return self.api_delete(
            DELETE_URL, user=self.admin,
            data={'platform': 'widget', 'conversation_id': conversation_id}, format='json',
        )

    def test_delete_by_visitor_id_hides_the_chat(self):
        # What the dashboard actually sent in the bug report
        resp = self._delete(self.session.visitor_id)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(WidgetMessage.objects.filter(is_deleted=False).count(), 0)

    def test_delete_by_composite_id(self):
        resp = self._delete(f'widget_3_{self.session.session_id}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(WidgetMessage.objects.filter(is_deleted=False).count(), 0)

    def test_archive_and_unarchive_by_visitor_id_use_session_key(self):
        payload = {'conversations': [{
            'platform': 'widget', 'conversation_id': self.session.visitor_id, 'account_id': '3',
        }]}
        resp = self.api_post(ARCHIVE_URL, payload, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(ConversationArchive.objects.filter(
            platform='widget', conversation_id=self.session.session_id, account_id='3').exists())
        resp = self.api_post(UNARCHIVE_URL, payload, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertFalse(ConversationArchive.objects.filter(platform='widget').exists())

    def test_archive_all_includes_website_chats(self):
        resp = self.api_post(ARCHIVE_ALL_URL, {'platform': 'widget'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(ConversationArchive.objects.filter(
            platform='widget', conversation_id=self.session.session_id, account_id='3').exists())
        self.assertEqual(WidgetMessage.objects.filter(is_from_visitor=True, is_read_by_staff=False).count(), 0)
