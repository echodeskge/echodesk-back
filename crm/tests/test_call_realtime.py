"""Realtime ``call_event`` frames (BK-03, :mod:`crm.call_realtime`).

Runs over an in-memory channel layer: a CallLog create or status change
puts exactly one ``call_event`` in the handler's (and transfer target's)
notifications group after commit; notes-only saves put nothing; a broken
channel layer never fails the save.
"""
import json
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.db import connection
from django.test import override_settings

from crm.call_realtime import broadcast_call_event, call_event_payload, notifications_group
from crm.models import CallLog
from crm.tests.conftest import CrmTestCase

IN_MEMORY_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}

PAYLOAD_KEYS = {
    'id', 'call_id', 'pbx_call_id', 'sip_call_id', 'status', 'direction',
    'caller_number', 'recipient_number', 'answered_at', 'ended_at',
    'handled_by', 'updated_at',
}


def _reset_channel_layers():
    from channels.layers import channel_layers
    channel_layers.backends = {}


class CallRealtimeTestCase(CrmTestCase):

    def setUp(self):
        # Per test (the tenant base class's setUpClass skips super(), so a
        # class-level override would not apply); dropped on both sides so
        # no Redis-backed layer instance survives.
        override = override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYERS)
        override.enable()
        _reset_channel_layers()
        self.addCleanup(_reset_channel_layers)
        self.addCleanup(override.disable)
        super().setUp()
        self.agent = self.create_user(email='rt-agent@test.com')
        self.other = self.create_user(email='rt-other@test.com')
        self.schema = connection.schema_name
        from channels.layers import get_channel_layer
        self.layer = get_channel_layer()

    def listen(self, user):
        """Subscribe a fake socket to ``user``'s notifications group and
        return its channel name."""
        channel = f'test-channel-{user.id}'
        async_to_sync(self.layer.group_add)(notifications_group(self.schema, user.id), channel)
        return channel

    def frames(self, channel):
        """Every message currently queued on ``channel``."""
        queue = self.layer.channels.get(channel)
        out = []
        while queue is not None and not queue.empty():
            _, message = queue.get_nowait()
            out.append(message)
        return out

    def committed(self):
        return self.captureOnCommitCallbacks(execute=True)


class TestCallEventPush(CallRealtimeTestCase):

    def test_create_sends_one_event_to_handler(self):
        channel = self.listen(self.agent)
        with self.committed():
            call = self.create_call_log(handled_by=self.agent, status='ringing',
                                        sip_call_id='sip-1', pbx_call_id='1700000000.1')
        frames = self.frames(channel)
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(frame['type'], 'call_event')
        self.assertEqual(set(frame['call']), PAYLOAD_KEYS)
        self.assertEqual(frame['call']['id'], call.id)
        self.assertEqual(frame['call']['call_id'], str(call.call_id))
        self.assertEqual(frame['call']['status'], 'ringing')
        self.assertEqual(frame['call']['direction'], 'inbound')
        self.assertEqual(frame['call']['sip_call_id'], 'sip-1')
        self.assertEqual(frame['call']['pbx_call_id'], '1700000000.1')
        self.assertEqual(frame['call']['handled_by'], self.agent.id)
        self.assertIsNone(frame['call']['answered_at'])
        self.assertIsInstance(frame['call']['updated_at'], str)
        # JSON serialisable as the consumer will dump it.
        json.dumps(frame)

    def test_status_change_sends_one_event(self):
        with self.committed():
            call = self.create_call_log(handled_by=self.agent, status='ringing')
        channel = self.listen(self.agent)
        with self.committed():
            call.status = 'answered'
            call.save()
        frames = self.frames(channel)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]['call']['status'], 'answered')

    def test_notes_only_save_sends_nothing(self):
        with self.committed():
            call = self.create_call_log(handled_by=self.agent, status='answered')
        channel = self.listen(self.agent)
        with self.committed():
            call.notes = 'customer asked for a callback'
            call.save()
            call.recording_url = 'https://pbx.test.com/rec/1.wav'
            call.save(update_fields=['recording_url'])
        self.assertEqual(self.frames(channel), [])

    def test_update_fields_without_status_sends_nothing(self):
        with self.committed():
            call = self.create_call_log(handled_by=self.agent, status='answered')
        channel = self.listen(self.agent)
        with self.committed():
            call.status = 'ended'  # changed in memory ...
            call.save(update_fields=['notes'])  # ... but not written
        self.assertEqual(self.frames(channel), [])

    def test_transfer_target_also_receives_it(self):
        with self.committed():
            call = self.create_call_log(handled_by=self.agent, status='answered')
        mine = self.listen(self.agent)
        theirs = self.listen(self.other)
        with self.committed():
            call.status = 'transferred'
            call.transferred_to_user = self.other
            call.save()
        self.assertEqual(len(self.frames(mine)), 1)
        frames = self.frames(theirs)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]['call']['status'], 'transferred')

    def test_other_users_get_nothing(self):
        stranger = self.listen(self.other)
        with self.committed():
            self.create_call_log(handled_by=self.agent, status='ringing')
        self.assertEqual(self.frames(stranger), [])

    def test_nothing_is_sent_before_commit(self):
        channel = self.listen(self.agent)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.create_call_log(handled_by=self.agent, status='ringing')
            self.assertEqual(self.frames(channel), [])
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(len(self.frames(channel)), 1)

    def test_channel_layer_error_never_fails_the_save(self):
        broken = AsyncMock(side_effect=ConnectionError('redis down'))
        with patch.object(type(self.layer), 'group_send', broken):
            with self.assertLogs('crm.call_realtime', level='WARNING'):
                with self.committed():
                    call = self.create_call_log(handled_by=self.agent, status='ringing')
        self.assertTrue(CallLog.objects.filter(pk=call.pk).exists())
        broken.assert_awaited()

    def test_missing_channel_layer_is_ignored(self):
        with patch('channels.layers.get_channel_layer', return_value=None):
            with self.committed():
                call = self.create_call_log(handled_by=self.agent, status='ringing')
        self.assertTrue(CallLog.objects.filter(pk=call.pk).exists())


class TestBroadcastCallEvent(CallRealtimeTestCase):
    """The public entry point for rows changed without ``save()``."""

    def test_broadcast_after_queryset_update(self):
        with self.committed():
            call = self.create_call_log(handled_by=self.agent, status='ringing')
        channel = self.listen(self.agent)
        with self.committed():
            CallLog.objects.filter(pk=call.pk).update(status='cancelled')
            self.assertEqual(self.frames(channel), [])  # no signal fired
            call.refresh_from_db()
            broadcast_call_event(call)
        frames = self.frames(channel)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]['call']['status'], 'cancelled')

    def test_explicit_schema(self):
        with self.committed():
            call = self.create_call_log(handled_by=self.agent, status='ringing')
        channel = self.listen(self.agent)
        with self.committed():
            broadcast_call_event(call, schema=self.schema)
        self.assertEqual(len(self.frames(channel)), 1)

    def test_no_recipients_is_a_noop(self):
        with self.committed():
            call = self.create_call_log(handled_by=self.agent, status='ringing')
        call.handled_by = None
        call.transferred_to_user = None
        with self.captureOnCommitCallbacks() as callbacks:
            broadcast_call_event(call)
        self.assertEqual(callbacks, [])

    def test_payload_shape(self):
        call = self.create_call_log(handled_by=self.agent, status='ringing')
        payload = call_event_payload(call)
        self.assertEqual(set(payload), PAYLOAD_KEYS)
        self.assertEqual(payload['handled_by'], self.agent.id)


class TestNotificationConsumerCallEvent(CallRealtimeTestCase):

    def test_consumer_forwards_call_event(self):
        from users.consumers import NotificationConsumer

        consumer = NotificationConsumer()
        consumer.send = AsyncMock()
        call = {'id': 1, 'status': 'answered', 'handled_by': self.agent.id}
        async_to_sync(consumer.call_event)({'type': 'call_event', 'call': call})
        consumer.send.assert_awaited_once()
        sent = json.loads(consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(sent, {'type': 'call_event', 'call': call})
