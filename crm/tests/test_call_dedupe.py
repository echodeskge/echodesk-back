"""Call-log dedupe, status transitions and the ``pbx_call_id`` exact match.

API contract (reports/PLAN.md):

* ``log_incoming_call`` requires ``sip_call_id``, accepts ``pbx_call_id``;
  the same user re-posting the same ``sip_call_id`` within 10 minutes gets
  the existing row back with 200, otherwise 201;
* ``update_status`` follows a transition table (``ringing→answered``,
  ``answered↔on_hold``, any live status → final) and answers 409 to an
  illegal move; repeating the current status is a 200 no-op;
* ``end_call`` on a leg the server already relabelled ``cancelled``
  (answered elsewhere) is a 200 no-op; other final statuses stay 400;
* legs sharing a ``pbx_call_id`` (Asterisk LINKEDID) are matched exactly
  when a sibling is answered, before the timing heuristic;
* ``call_recording_url_webhook`` matches ``pbx_call_id`` / ``sip_call_id``
  before the last-7-digits fallback and refuses foreign hosts.
"""
import sys
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.utils import timezone

from crm.call_notifications import find_answered_sibling, relabel_legs_answered_elsewhere
from crm.models import CallEvent, CallLog
from crm.tests.conftest import CrmTestCase
from users.models import Notification

LOG_URL = '/api/call-logs/log_incoming_call/'


class _CallsMixin:

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='dd-admin@test.com')
        self.agent_a = self.create_user(email='dd-a@test.com', can_make_calls=True)
        self.agent_b = self.create_user(email='dd-b@test.com', can_make_calls=True)
        self.config = self.create_sip_config(
            created_by=self.admin, is_default=True, sip_server='pbx.tenant.test',
        )

    def log(self, user, sip_call_id='sip-1', **extra):
        body = {'caller_number': '+995599123456', 'recipient_number': '+995322421219',
                'sip_call_id': sip_call_id}
        body.update(extra)
        return self.api_post(LOG_URL, body, user=user)

    def status(self, call, new_status, user=None, **extra):
        body = {'status': new_status}
        body.update(extra)
        return self.api_patch(f'/api/call-logs/{call.id}/update_status/', body,
                              user=user or call.handled_by)

    def end(self, call, user=None):
        return self.api_post(f'/api/call-logs/{call.id}/end_call/', {}, user=user or call.handled_by)


class TestLogIncomingCallDedupe(_CallsMixin, CrmTestCase):

    def test_sip_call_id_is_required(self):
        resp = self.api_post(LOG_URL, {'caller_number': '+995599123456'}, user=self.agent_a)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('sip_call_id', resp.data)
        resp = self.log(self.agent_a, sip_call_id='   ')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(CallLog.objects.count(), 0)

    def test_same_invite_logged_twice_returns_the_existing_row(self):
        first = self.log(self.agent_a, sip_call_id='invite-1', pbx_call_id='1700000000.1')
        self.assertEqual(first.status_code, 201, first.data)
        again = self.log(self.agent_a, sip_call_id='invite-1')
        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(again.data['id'], first.data['id'])
        self.assertEqual(CallLog.objects.filter(sip_call_id='invite-1').count(), 1)
        self.assertEqual(CallEvent.objects.filter(call_log_id=first.data['id']).count(), 1)

    def test_pbx_call_id_is_stored_and_backfilled(self):
        first = self.log(self.agent_a, sip_call_id='invite-2')
        self.assertEqual(first.status_code, 201)
        call = CallLog.objects.get(pk=first.data['id'])
        self.assertEqual(call.pbx_call_id, '')
        again = self.log(self.agent_a, sip_call_id='invite-2', pbx_call_id='1700000000.2')
        self.assertEqual(again.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.pbx_call_id, '1700000000.2')

    def test_other_agent_same_invite_id_is_a_new_leg(self):
        # Ring-all sends every agent its own INVITE; even if two happened to
        # share a Call-ID the other agent's leg is a separate row.
        self.assertEqual(self.log(self.agent_a, sip_call_id='invite-3').status_code, 201)
        self.assertEqual(self.log(self.agent_b, sip_call_id='invite-3').status_code, 201)
        self.assertEqual(CallLog.objects.filter(sip_call_id='invite-3').count(), 2)

    def test_old_leg_outside_the_window_is_not_reused(self):
        first = self.log(self.agent_a, sip_call_id='invite-4')
        CallLog.objects.filter(pk=first.data['id']).update(
            started_at=timezone.now() - timedelta(minutes=11),
        )
        again = self.log(self.agent_a, sip_call_id='invite-4')
        self.assertEqual(again.status_code, 201)
        self.assertNotEqual(again.data['id'], first.data['id'])

    def test_requires_make_calls(self):
        loner = self.create_user(email='dd-loner@test.com')
        resp = self.log(loner, sip_call_id='invite-5')
        self.assertEqual(resp.status_code, 403)


class TestStatusTransitions(_CallsMixin, CrmTestCase):

    def test_legal_moves(self):
        call = self.create_call_log(handled_by=self.agent_a, status='ringing')
        for new_status in ('answered', 'on_hold', 'answered', 'ended'):
            resp = self.status(call, new_status)
            self.assertEqual(resp.status_code, 200, (new_status, resp.data))
        call.refresh_from_db()
        self.assertEqual(call.status, 'ended')
        self.assertIsNotNone(call.answered_at)
        self.assertIsNotNone(call.ended_at)

    def test_outbound_initiated_to_answered(self):
        call = self.create_call_log(handled_by=self.agent_a, status='initiated', direction='outbound')
        self.assertEqual(self.status(call, 'ringing').status_code, 200)
        self.assertEqual(self.status(call, 'answered').status_code, 200)

    def test_illegal_moves_are_409(self):
        cases = (
            ('ringing', 'on_hold'),
            ('ringing', 'initiated'),
            ('on_hold', 'ringing'),
            ('answered', 'ringing'),
            ('ended', 'answered'),
            ('missed', 'ringing'),
            ('cancelled', 'ended'),
            ('ended', 'missed'),
        )
        for old, new in cases:
            call = self.create_call_log(handled_by=self.agent_a, status=old)
            resp = self.status(call, new)
            self.assertEqual(resp.status_code, 409, (old, new, resp.data))
            self.assertEqual(resp.data['status'], old)
            call.refresh_from_db()
            self.assertEqual(call.status, old, (old, new))
            self.assertFalse(CallEvent.objects.filter(call_log=call).exists(), (old, new))

    def test_repeating_the_status_is_a_no_op_200(self):
        answered_at = timezone.now() - timedelta(minutes=2)
        call = self.create_call_log(handled_by=self.agent_a, status='answered',
                                    answered_at=answered_at)
        resp = self.status(call, 'answered', recording_url='https://pbx.tenant.test/x.wav')
        self.assertEqual(resp.status_code, 200, resp.data)
        call.refresh_from_db()
        self.assertEqual(call.answered_at, answered_at)
        self.assertEqual(call.recording_url, 'https://pbx.tenant.test/x.wav')
        # Even on a finished call (the softphone may re-send on reconnect).
        call = self.create_call_log(handled_by=self.agent_a, status='ended')
        self.assertEqual(self.status(call, 'ended').status_code, 200)

    def test_any_live_status_may_end(self):
        for old in ('initiated', 'ringing', 'answered', 'on_hold', 'recording'):
            for final in ('ended', 'failed', 'cancelled', 'busy', 'no_answer', 'transferred'):
                call = self.create_call_log(handled_by=self.agent_a, status=old,
                                            direction='outbound')
                resp = self.status(call, final)
                self.assertEqual(resp.status_code, 200, (old, final, resp.data))
                call.refresh_from_db()
                self.assertIsNotNone(call.ended_at, (old, final))


@patch('users.notification_utils._dispatch_push')
class TestEndCall(_CallsMixin, CrmTestCase):

    def test_end_call_on_cancelled_leg_is_idempotent(self, mock_push):
        call = self.create_call_log(handled_by=self.agent_a, status='cancelled',
                                    ended_at=timezone.now())
        resp = self.end(call)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'cancelled')
        self.assertFalse(CallEvent.objects.filter(call_log=call).exists())
        self.assertEqual(Notification.objects.filter(notification_type='call_missed').count(), 0)

    def test_end_call_on_other_final_statuses_stays_400(self, mock_push):
        for final in ('ended', 'missed', 'failed', 'no_answer', 'busy', 'transferred'):
            call = self.create_call_log(handled_by=self.agent_a, status=final)
            self.assertEqual(self.end(call).status_code, 400, final)

    def test_ringall_exact_match_cancels_the_sibling_immediately(self, mock_push):
        leg_a = self.create_call_log(handled_by=self.agent_a, pbx_call_id='1700.5',
                                     caller_number='+995599000001')
        leg_b = self.create_call_log(handled_by=self.agent_b, pbx_call_id='1700.5',
                                     caller_number='+995599000001')
        # A third leg of another call with the same caller is untouched.
        other = self.create_call_log(handled_by=self.agent_b, pbx_call_id='1700.9',
                                     caller_number='+995599000001')
        resp = self.status(leg_b, 'answered')
        self.assertEqual(resp.status_code, 200, resp.data)

        leg_a.refresh_from_db()
        self.assertEqual(leg_a.status, 'cancelled')
        self.assertIsNotNone(leg_a.ended_at)
        self.assertEqual(leg_a.duration, timedelta(0))
        event = CallEvent.objects.get(call_log=leg_a, event_type='ended')
        self.assertTrue(event.metadata['answered_elsewhere'])
        self.assertEqual(event.metadata['answered_call_log_id'], leg_b.pk)
        self.assertEqual(event.metadata['previous_status'], 'ringing')
        other.refresh_from_db()
        self.assertEqual(other.status, 'ringing')

        # The softphone of agent A then ends its (already cancelled) leg.
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            resp = self.end(leg_a)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'cancelled')
        self.assertEqual(len(callbacks), 0)
        self.assertEqual(Notification.objects.filter(notification_type='call_missed').count(), 0)

    def test_end_call_matches_exact_sibling_before_the_heuristic(self, mock_push):
        # Different callers (so the digits heuristic cannot match) but the
        # same LINKEDID: answered elsewhere.
        leg_a = self.create_call_log(handled_by=self.agent_a, pbx_call_id='1700.6',
                                     caller_number='anonymous')
        leg_b = self.create_call_log(handled_by=self.agent_b, pbx_call_id='1700.6',
                                     caller_number='+995599000002', status='answered',
                                     answered_at=timezone.now())
        self.assertEqual(find_answered_sibling(leg_a), leg_b)
        # Force leg_a back to ringing as if the relabel had not run yet.
        CallLog.objects.filter(pk=leg_a.pk).update(status='ringing', ended_at=None)
        leg_a.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            resp = self.end(leg_a)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'cancelled')
        self.assertEqual(len(callbacks), 0)

    def test_relabel_also_covers_legs_that_already_timed_out(self, mock_push):
        leg_a = self.create_call_log(handled_by=self.agent_a, pbx_call_id='1700.7',
                                     status='missed', ended_at=timezone.now())
        leg_b = self.create_call_log(handled_by=self.agent_b, pbx_call_id='1700.7',
                                     status='answered', answered_at=timezone.now())
        self.assertEqual(relabel_legs_answered_elsewhere(leg_b), 0)  # already done by the signal
        leg_a.refresh_from_db()
        self.assertEqual(leg_a.status, 'cancelled')

    def test_relabel_broadcasts_when_realtime_module_exists(self, mock_push):
        fake = SimpleNamespace(broadcast_call_event=MagicMock())
        with patch.dict(sys.modules, {'crm.call_realtime': fake}):
            leg_a = self.create_call_log(handled_by=self.agent_a, pbx_call_id='1700.8')
            leg_b = self.create_call_log(handled_by=self.agent_b, pbx_call_id='1700.8')
            self.assertEqual(self.status(leg_b, 'answered').status_code, 200)
        fake.broadcast_call_event.assert_called_once()
        self.assertEqual(fake.broadcast_call_event.call_args.args[0].pk, leg_a.pk)
        self.assertEqual(fake.broadcast_call_event.call_args.args[0].status, 'cancelled')

    def test_relabel_survives_a_broken_broadcast(self, mock_push):
        fake = SimpleNamespace(broadcast_call_event=MagicMock(side_effect=RuntimeError('redis down')))
        with patch.dict(sys.modules, {'crm.call_realtime': fake}):
            leg_a = self.create_call_log(handled_by=self.agent_a, pbx_call_id='1700.10')
            leg_b = self.create_call_log(handled_by=self.agent_b, pbx_call_id='1700.10')
            self.assertEqual(self.status(leg_b, 'answered').status_code, 200)
        leg_a.refresh_from_db()
        self.assertEqual(leg_a.status, 'cancelled')


class TestRecordingUrlWebhookMatching(_CallsMixin, CrmTestCase):

    URL = '/api/webhooks/call-recording-url/'

    def setUp(self):
        super().setUp()
        self.pbx = self.create_pbx_server()
        self.headers = self.pbx_headers(self.pbx)

    def post(self, body):
        return self.api_post(self.URL, body, **self.headers)

    def test_pbx_call_id_picks_the_answered_leg(self):
        ringing = self.create_call_log(handled_by=self.agent_a, pbx_call_id='1700.11',
                                       status='cancelled', caller_number='+995599000003')
        answered = self.create_call_log(handled_by=self.agent_b, pbx_call_id='1700.11',
                                        status='ended', answered_at=timezone.now(),
                                        caller_number='+995599000003')
        # A later call from the same number must not steal the recording.
        later = self.create_call_log(handled_by=self.agent_a, caller_number='+995599000003')
        resp = self.post({'caller_number': '+995599000003', 'pbx_call_id': '1700.11',
                          'recording_url': 'https://pbx.tenant.test/rec/1700.11.wav'})
        self.assertEqual(resp.status_code, 200, resp.data)
        answered.refresh_from_db(); ringing.refresh_from_db(); later.refresh_from_db()
        self.assertEqual(answered.recording_url, 'https://pbx.tenant.test/rec/1700.11.wav')
        self.assertEqual(ringing.recording_url, '')
        self.assertEqual(later.recording_url, '')

    def test_sip_call_id_match(self):
        target = self.create_call_log(handled_by=self.agent_a, sip_call_id='abc@host',
                                      caller_number='+995599000004')
        self.create_call_log(handled_by=self.agent_a, caller_number='+995599000004')
        resp = self.post({'sip_call_id': 'abc@host',
                          'recording_url': 'https://pbx.tenant.test/rec/abc.wav'})
        self.assertEqual(resp.status_code, 200, resp.data)
        target.refresh_from_db()
        self.assertEqual(target.recording_url, 'https://pbx.tenant.test/rec/abc.wav')

    def test_digits_fallback_when_ids_are_unknown(self):
        target = self.create_call_log(handled_by=self.agent_a, caller_number='+995599000005')
        resp = self.post({'caller_number': '995599000005', 'pbx_call_id': 'unknown',
                          'recording_url': 'https://pbx.tenant.test/rec/d.wav'})
        self.assertEqual(resp.status_code, 200, resp.data)
        target.refresh_from_db()
        self.assertEqual(target.recording_url, 'https://pbx.tenant.test/rec/d.wav')

    def test_foreign_host_is_refused(self):
        target = self.create_call_log(handled_by=self.agent_a, caller_number='+995599000006')
        for url in ('https://evil.example.com/rec.wav', 'ftp://pbx.tenant.test/rec.wav',
                    'http://169.254.169.254/latest/meta-data', 'not a url'):
            resp = self.post({'caller_number': '+995599000006', 'recording_url': url})
            self.assertEqual(resp.status_code, 400, url)
        target.refresh_from_db()
        self.assertEqual(target.recording_url, '')

    def test_missing_fields(self):
        resp = self.post({'recording_url': 'https://pbx.tenant.test/rec.wav'})
        self.assertEqual(resp.status_code, 400)
        resp = self.post({'caller_number': '+995599000007'})
        self.assertEqual(resp.status_code, 400)
        resp = self.post({'caller_number': '+999999999999',
                          'recording_url': 'https://pbx.tenant.test/rec.wav'})
        self.assertEqual(resp.status_code, 404)
