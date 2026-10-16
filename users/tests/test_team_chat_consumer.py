"""
Tests for the TeamChatConsumer websocket consumer (users.consumers.TeamChatConsumer):

- create_message: websocket-created messages are serialized with
  TeamChatMessageSerializer (same shape as the REST API) and keep a
  `conversation_id` alias; the recipient gets a `team_chat_message`
  notification; empty text / self-messaging / foreign conversations are
  rejected; the type is forced to text
- handle_send_message: the recipient's group event and the sender's
  `message_sent` echo both carry `conversation` and `conversation_id`;
  a failed create sends an error frame
- chat_message handler: the `new_message` frame carries both keys

Follows the mock-channel-layer pattern of notifications/tests/test_notification_consumers.py.
The database-backed `create_message` is exercised through its synchronous
implementation (the function wrapped by `database_sync_to_async`) so the
tenant test transaction stays on the test thread; the async handlers run via
`async_to_sync` with `create_message` stubbed.
"""
import json
from unittest.mock import patch, AsyncMock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from users.tests.conftest import EchoDeskTenantTestCase
from users.consumers import TeamChatConsumer
from users.models import (
    TeamChatConversation, TeamChatMessage, Notification, NotificationPreference,
)

User = get_user_model()

# The plain function behind the database_sync_to_async wrapper.
_create_message_sync = TeamChatConsumer.__dict__['create_message'].func


class RecordingChannelLayer:
    """Minimal channel layer that records group_send calls."""

    def __init__(self):
        self.groups = {}
        self.sent = []

    async def group_add(self, group, channel_name):
        self.groups.setdefault(group, set()).add(channel_name)

    async def group_discard(self, group, channel_name):
        if group in self.groups:
            self.groups[group].discard(channel_name)

    async def group_send(self, group, message):
        self.sent.append((group, message))


def _build_consumer(user, tenant_schema='test'):
    consumer = TeamChatConsumer()
    consumer.scope = {
        'type': 'websocket',
        'url_route': {'kwargs': {'tenant_schema': tenant_schema}},
        'user': user,
    }
    consumer.tenant_schema = tenant_schema
    consumer.user = user
    consumer.channel_name = 'test-chat-channel-001'
    consumer.channel_layer = RecordingChannelLayer()
    consumer.send = AsyncMock()
    consumer.accept = AsyncMock()
    consumer.close = AsyncMock()
    return consumer


class TeamChatConsumerTestBase(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.sender = self.create_user(email='ws-sender@test.com', first_name='Ana', last_name='B')
        self.recipient = self.create_user(email='ws-recipient@test.com')
        Notification.objects.filter(user__in=[self.sender, self.recipient]).delete()
        NotificationPreference.objects.filter(user__in=[self.sender, self.recipient]).delete()
        # create_notification broadcasts on the notifications socket and
        # enqueues the push; neither is under test here.
        self._ws = patch('asgiref.sync.async_to_sync', return_value=lambda **kw: None)
        self._push = patch('users.notification_utils._dispatch_push')
        self._ws.start()
        self.mock_push = self._push.start()

    def tearDown(self):
        self._push.stop()
        self._ws.stop()
        Notification.objects.filter(user__in=[self.sender, self.recipient]).delete()
        super().tearDown()

    def _make_conversation(self, user1, user2):
        conv = TeamChatConversation.objects.create()
        conv.participants.add(user1, user2)
        return conv

    def _create(self, consumer, **kwargs):
        return _create_message_sync(consumer, **kwargs)


class TestCreateMessage(TeamChatConsumerTestBase):

    def test_message_uses_rest_serializer_shape(self):
        consumer = _build_consumer(self.sender)
        result = self._create(consumer, recipient_id=self.recipient.pk, text='hi there')

        self.assertIsNotNone(result)
        self.assertEqual(result['recipient_id'], self.recipient.pk)
        msg = result['message']
        self.assertEqual(msg['conversation'], result['conversation_id'])
        self.assertEqual(msg['conversation_id'], result['conversation_id'])
        for key in ('id', 'sender', 'message_type', 'text', 'file', 'file_url', 'file_name',
                    'file_size', 'file_mime_type', 'voice_duration', 'is_read', 'read_at',
                    'created_at'):
            self.assertIn(key, msg)
        self.assertEqual(msg['text'], 'hi there')
        self.assertEqual(msg['message_type'], 'text')
        self.assertIsNone(msg['file_url'])
        self.assertIsNone(msg['read_at'])
        self.assertFalse(msg['is_read'])
        self.assertEqual(msg['sender']['id'], self.sender.pk)
        self.assertEqual(msg['sender']['full_name'], 'Ana B')
        self.assertIn('is_online', msg['sender'])
        # must be JSON serialisable as-is (datetimes already rendered)
        json.dumps(msg)

    def test_creates_conversation_and_notification(self):
        consumer = _build_consumer(self.sender)
        result = self._create(consumer, recipient_id=self.recipient.pk, text='first')

        conv = TeamChatConversation.objects.get(pk=result['conversation_id'])
        self.assertEqual(
            set(conv.participants.values_list('id', flat=True)),
            {self.sender.pk, self.recipient.pk},
        )

        notif = Notification.objects.get(user=self.recipient, notification_type='team_chat_message')
        self.assertEqual(notif.title, 'Ana B')
        self.assertEqual(notif.message, 'first')
        self.assertEqual(notif.metadata['conversation_id'], conv.pk)
        self.assertEqual(notif.metadata['sender_id'], self.sender.pk)
        self.assertEqual(notif.metadata['message_id'], result['message']['id'])
        self.assertEqual(notif.link_url, f'/team-chat?user={self.sender.pk}')
        self.assertFalse(Notification.objects.filter(user=self.sender).exists())

        self.mock_push.assert_called_once()
        push_kwargs = self.mock_push.call_args.kwargs
        self.assertEqual(push_kwargs['user'], self.recipient)
        self.assertEqual(push_kwargs['data']['notification_type'], 'team_chat_message')
        self.assertEqual(push_kwargs['data']['conversation_id'], conv.pk)
        self.assertEqual(push_kwargs['data']['sender_id'], self.sender.pk)

    def test_by_conversation_id_reuses_conversation(self):
        conv = self._make_conversation(self.sender, self.recipient)
        consumer = _build_consumer(self.sender)
        result = self._create(consumer, conversation_id=conv.pk, text='again')
        self.assertEqual(result['conversation_id'], conv.pk)
        self.assertEqual(result['recipient_id'], self.recipient.pk)
        self.assertEqual(TeamChatMessage.objects.filter(conversation=conv).count(), 1)

    def test_type_is_forced_to_text(self):
        consumer = _build_consumer(self.sender)
        result = self._create(
            consumer, recipient_id=self.recipient.pk, text='not a file', message_type='file',
        )
        self.assertEqual(result['message']['message_type'], 'text')

    def test_empty_text_rejected(self):
        consumer = _build_consumer(self.sender)
        result = self._create(consumer, recipient_id=self.recipient.pk, text='   ')
        self.assertIsNone(result)
        self.assertEqual(TeamChatMessage.objects.filter(sender=self.sender).count(), 0)
        self.mock_push.assert_not_called()

    def test_message_to_self_rejected(self):
        consumer = _build_consumer(self.sender)
        result = self._create(consumer, recipient_id=self.sender.pk, text='me')
        self.assertIsNone(result)
        self.assertEqual(TeamChatMessage.objects.filter(sender=self.sender).count(), 0)

    def test_foreign_conversation_rejected(self):
        stranger = self.create_user(email='ws-stranger@test.com')
        conv = self._make_conversation(self.recipient, stranger)
        consumer = _build_consumer(self.sender)
        result = self._create(consumer, conversation_id=conv.pk, text='intrude')
        self.assertIsNone(result)
        self.assertEqual(conv.messages.count(), 0)

    def test_preference_in_app_disabled_skips_notification(self):
        NotificationPreference.objects.create(
            user=self.recipient, notification_type='team_chat_message', in_app=False,
        )
        consumer = _build_consumer(self.sender)
        result = self._create(consumer, recipient_id=self.recipient.pk, text='quiet')
        self.assertIsNotNone(result)
        self.assertFalse(Notification.objects.filter(user=self.recipient).exists())
        self.mock_push.assert_not_called()

    def test_notification_failure_does_not_block_message(self):
        consumer = _build_consumer(self.sender)
        with patch('users.notification_utils.create_team_chat_message_notification',
                   side_effect=RuntimeError('boom')):
            result = self._create(consumer, recipient_id=self.recipient.pk, text='still')
        self.assertIsNotNone(result)
        self.assertEqual(result['message']['text'], 'still')
        self.assertEqual(TeamChatMessage.objects.filter(sender=self.sender).count(), 1)


class TestHandleSendMessage(TeamChatConsumerTestBase):

    def test_frames_carry_conversation_and_conversation_id(self):
        conv = self._make_conversation(self.sender, self.recipient)
        consumer = _build_consumer(self.sender)
        created = self._create(consumer, conversation_id=conv.pk, text='wire')

        with patch.object(consumer, 'create_message', new=AsyncMock(return_value=created)):
            async_to_sync(consumer.receive)(text_data=json.dumps({
                'type': 'message', 'conversation_id': conv.pk, 'text': 'wire',
            }))

        # recipient group event
        self.assertEqual(len(consumer.channel_layer.sent), 1)
        group, event = consumer.channel_layer.sent[0]
        self.assertEqual(group, f'team_chat_test_{self.recipient.pk}')
        self.assertEqual(event['type'], 'chat_message')
        self.assertEqual(event['conversation'], conv.pk)
        self.assertEqual(event['conversation_id'], conv.pk)
        self.assertEqual(event['message']['conversation'], conv.pk)
        self.assertEqual(event['message']['conversation_id'], conv.pk)
        self.assertEqual(event['message']['text'], 'wire')

        # sender echo
        consumer.send.assert_called_once()
        frame = json.loads(consumer.send.call_args.kwargs['text_data'])
        self.assertEqual(frame['type'], 'message_sent')
        self.assertEqual(frame['conversation'], conv.pk)
        self.assertEqual(frame['conversation_id'], conv.pk)
        self.assertEqual(frame['message']['id'], event['message']['id'])

    def test_missing_target_sends_error(self):
        consumer = _build_consumer(self.sender)
        async_to_sync(consumer.receive)(text_data=json.dumps({'type': 'message', 'text': 'nowhere'}))
        frame = json.loads(consumer.send.call_args.kwargs['text_data'])
        self.assertEqual(frame['type'], 'error')
        self.assertEqual(consumer.channel_layer.sent, [])

    def test_failed_create_sends_error(self):
        consumer = _build_consumer(self.sender)
        with patch.object(consumer, 'create_message', new=AsyncMock(return_value=None)):
            async_to_sync(consumer.receive)(text_data=json.dumps({
                'type': 'message', 'recipient_id': self.recipient.pk, 'text': '',
            }))
        frame = json.loads(consumer.send.call_args.kwargs['text_data'])
        self.assertEqual(frame['type'], 'error')
        self.assertEqual(consumer.channel_layer.sent, [])


class TestChatMessageHandler(TeamChatConsumerTestBase):

    def test_new_message_frame_has_both_keys(self):
        consumer = _build_consumer(self.recipient)
        async_to_sync(consumer.chat_message)({
            'type': 'chat_message', 'message': {'id': 1, 'text': 'x'}, 'conversation_id': 7,
        })
        frame = json.loads(consumer.send.call_args.kwargs['text_data'])
        self.assertEqual(frame['type'], 'new_message')
        self.assertEqual(frame['conversation'], 7)
        self.assertEqual(frame['conversation_id'], 7)
        self.assertEqual(frame['message'], {'id': 1, 'text': 'x'})

    def test_new_message_frame_accepts_legacy_event_without_conversation_key(self):
        consumer = _build_consumer(self.recipient)
        async_to_sync(consumer.chat_message)({
            'type': 'chat_message', 'message': {'id': 2}, 'conversation': 9,
        })
        frame = json.loads(consumer.send.call_args.kwargs['text_data'])
        self.assertEqual(frame['conversation_id'], 9)
        self.assertEqual(frame['conversation'], 9)
