"""
Phase 4 review fixes for the calls package (parity-fix/calls):

1. sequential queue strategies (rrmemory, the default): a leg that timed
   out before a colleague answered is "answered elsewhere", not missed:
   no notification, not counted in overview / users / queue stats;
2. a blind-transfer leg nobody answered stays a missed call;
3. the recording proxy refuses non-public upstream addresses and only
   serves audio content types;
4. the shared SIP password reaches staff / superusers / its creator only,
   and the softphone endpoints say whether SIP can register;
5. only the handling agent or a PBX admin may change a call's status;
6. the fallback recipients are the PBX admins (incl. manage_settings).
"""
import http.server
import threading
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import override_settings
from django.utils import timezone

from crm.call_notifications import (
    MISSED_CALL_MAX_DEFERS,
    find_answered_sibling,
    resolve_missed_call_recipients,
    send_missed_call_notifications,
)
from crm.models import CallLog, InboundRoute, Queue, QueueMember
from crm.recording_hosts import (
    UnsafeRecordingURL,
    _pinned_adapter,
    is_public_ip,
    open_recording,
    resolve_public_addresses,
    safe_recording_content_type,
)
from crm.tests.conftest import CrmTestCase
from users.models import Notification

DID = '+995322777000'
CALLER = '+995599123456'


def _set(leg, **fields):
    CallLog.objects.filter(pk=leg.pk).update(**fields)
    leg.refresh_from_db()
    return leg


class _QueueCallMixin:
    """A queue with two agents (extensions + queue memberships) behind a DID."""

    strategy = 'rrmemory'

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='fx-admin@test.com')
        self.agent_a = self.create_user(email='fx-a@test.com')
        self.agent_b = self.create_user(email='fx-b@test.com')
        self.config = self.create_sip_config(created_by=self.admin, is_default=True)
        group = self.create_tenant_group(name=f'FX {self.strategy}')
        self.queue = Queue.objects.create(
            name='FX', slug=f'fx-{self.strategy}', group=group, strategy=self.strategy,
            timeout_seconds=30,
        )
        for user, ext in ((self.agent_a, '201'), (self.agent_b, '202')):
            assignment = self.create_phone_assignment(user=user, sip_config=self.config,
                                                      extension=ext)
            QueueMember.objects.update_or_create(
                queue=self.queue, user_phone_assignment=assignment,
                defaults={'is_active': True},
            )
        InboundRoute.objects.create(did=DID, destination_type='queue',
                                    destination_queue=self.queue)
        Notification.objects.filter(notification_type='call_missed').delete()

    def leg(self, user, **kwargs):
        defaults = dict(status='ringing', direction='inbound', caller_number=CALLER,
                        recipient_number=DID)
        defaults.update(kwargs)
        return self.create_call_log(handled_by=user, **defaults)

    def missed_notifs(self):
        return Notification.objects.filter(notification_type='call_missed')

    def assert_one_served_call(self, answered_by, other):
        resp = self.api_get('/api/call-stats/overview/?range=today', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['answered_calls'], 1)
        self.assertEqual(resp.data['missed_calls'], 0)

        resp = self.api_get('/api/call-stats/users/?range=today', user=self.admin)
        rows = {r['user_id']: r for r in resp.data['results']}
        self.assertEqual(rows[other.id]['missed_count'], 0)
        self.assertEqual(rows[answered_by.id]['answered_count'], 1)
        self.assertEqual(rows[answered_by.id]['missed_count'], 0)

        resp = self.api_get(f'/api/call-stats/queues/?queue_id={self.queue.id}&range=today',
                            user=self.admin)
        self.assertEqual(resp.data['answered_count'], 1)
        self.assertEqual(resp.data['abandoned_count'], 0)

        resp = self.api_get('/api/call-logs/statistics/?period=today', user=self.admin)
        self.assertEqual(resp.data['missed_calls'], 0)
        self.assertEqual(resp.data['answered_calls'], 1)


# ============================================================================
# 1. rrmemory: sequential legs
# ============================================================================


@patch('users.notification_utils._dispatch_push')
class TestRrmemoryAnsweredElsewhere(_QueueCallMixin, CrmTestCase):

    def test_review_reproduction_one_served_call_zero_missed(self, mock_push):
        """Agent A's leg rings and times out; the queue waits ``retry`` and
        rings B, who answers 45 s after A's leg ended. Before the fix A got
        a 'Missed call' and overview said answered=1 / missed=1."""
        now = timezone.now()
        leg_a = _set(self.leg(self.agent_a), started_at=now - timedelta(seconds=80))
        with self.captureOnCommitCallbacks(execute=False) as delayed:
            resp = self.api_post(f'/api/call-logs/{leg_a.id}/end_call/', {}, user=self.agent_a)
        self.assertEqual(resp.status_code, 200)
        # Nobody has answered yet, so it is a missed leg for now.
        self.assertEqual(resp.data['status'], 'missed')
        self.assertEqual(len(delayed), 1)
        _set(leg_a, ended_at=now - timedelta(seconds=45))

        leg_b = _set(self.leg(self.agent_b), started_at=now - timedelta(seconds=40))
        resp = self.api_patch(f'/api/call-logs/{leg_b.id}/update_status/',
                              {'status': 'answered'}, user=self.agent_b)
        self.assertEqual(resp.status_code, 200, resp.data)
        leg_a.refresh_from_db()
        self.assertEqual(leg_a.status, 'cancelled')
        self.assertTrue(leg_a.events.filter(metadata__answered_elsewhere=True,
                                            metadata__answered_call_log_id=leg_b.id).exists())
        resp = self.api_post(f'/api/call-logs/{leg_b.id}/end_call/', {}, user=self.agent_b)
        self.assertEqual(resp.data['status'], 'ended')

        # The delayed task runs later and notifies nobody.
        for callback in delayed:
            callback()
        self.assertEqual(self.missed_notifs().count(), 0)
        self.assertFalse(mock_push.called)
        leg_a.refresh_from_db()
        self.assertEqual(leg_a.status, 'cancelled')
        self.assert_one_served_call(answered_by=self.agent_b, other=self.agent_a)

    def test_task_rechecks_at_send_time(self, mock_push):
        """The answer reaches the DB without the status-transition signal
        (e.g. a bulk update): the delayed task still relabels at send time."""
        now = timezone.now()
        leg_a = _set(self.leg(self.agent_a), started_at=now - timedelta(seconds=80))
        with self.captureOnCommitCallbacks(execute=False) as delayed:
            self.api_post(f'/api/call-logs/{leg_a.id}/end_call/', {}, user=self.agent_a)
        _set(leg_a, ended_at=now - timedelta(seconds=45))
        leg_b = self.leg(self.agent_b)
        _set(leg_b, started_at=now - timedelta(seconds=40), status='ended',
             answered_at=now - timedelta(seconds=30), ended_at=now,
             duration=timedelta(seconds=30))
        leg_a.refresh_from_db()
        self.assertEqual(leg_a.status, 'missed')

        for callback in delayed:
            callback()
        leg_a.refresh_from_db()
        self.assertEqual(leg_a.status, 'cancelled')
        self.assertEqual(self.missed_notifs().count(), 0)
        self.assert_one_served_call(answered_by=self.agent_b, other=self.agent_a)

    def test_third_agent_answering_covers_the_whole_chain(self, mock_push):
        """A and B time out one after the other, C answers: neither A nor B
        missed it (C answers > one attempt gap after A ended)."""
        agent_c = self.create_user(email='fx-c@test.com')
        assignment = self.create_phone_assignment(user=agent_c, sip_config=self.config,
                                                  extension='203')
        QueueMember.objects.update_or_create(queue=self.queue, user_phone_assignment=assignment)
        now = timezone.now()
        leg_a = _set(self.leg(self.agent_a, status='missed'),
                     started_at=now - timedelta(seconds=120), ended_at=now - timedelta(seconds=90))
        leg_b = _set(self.leg(self.agent_b, status='missed'),
                     started_at=now - timedelta(seconds=85), ended_at=now - timedelta(seconds=55))
        leg_c = _set(self.leg(agent_c), started_at=now - timedelta(seconds=50))
        resp = self.api_patch(f'/api/call-logs/{leg_c.id}/update_status/',
                              {'status': 'answered'}, user=agent_c)
        self.assertEqual(resp.status_code, 200)
        leg_a.refresh_from_db()
        leg_b.refresh_from_db()
        self.assertEqual((leg_a.status, leg_b.status), ('cancelled', 'cancelled'))

    def test_task_defers_while_the_next_agent_rings(self, mock_push):
        from crm.tasks import notify_missed_call

        now = timezone.now()
        leg_a = _set(self.leg(self.agent_a, status='missed'),
                     started_at=now - timedelta(seconds=50), ended_at=now - timedelta(seconds=20))
        _set(self.leg(self.agent_b), started_at=now - timedelta(seconds=15))  # still ringing
        with patch.object(notify_missed_call, 'apply_async') as requeue:
            self.assertEqual(notify_missed_call(leg_a.id, connection.schema_name), 0)
        requeue.assert_called_once()
        self.assertEqual(requeue.call_args.kwargs['args'], [leg_a.id, connection.schema_name, 1])
        self.assertGreaterEqual(requeue.call_args.kwargs['countdown'], 60)
        self.assertEqual(self.missed_notifs().count(), 0)

        # After the last allowed deferral it decides with what it knows.
        with patch.object(notify_missed_call, 'apply_async') as requeue:
            self.assertEqual(
                notify_missed_call(leg_a.id, connection.schema_name, MISSED_CALL_MAX_DEFERS), 1)
        requeue.assert_not_called()
        self.assertEqual(self.missed_notifs().filter(user=self.agent_a).count(), 1)

    def test_schedule_waits_at_least_one_attempt_gap(self, mock_push):
        with patch('crm.tasks.notify_missed_call.apply_async') as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                self.leg(self.agent_a, status='missed')
        enqueue.assert_called_once()
        # Queue timeout 30 + retry 5 + slack 25.
        self.assertEqual(enqueue.call_args.kwargs['countdown'], 60)

    def test_genuinely_missed_call_notifies(self, mock_push):
        """Both agents' legs time out and nobody answers: a real missed
        call, notified to each leg's agent."""
        now = timezone.now()
        leg_a = _set(self.leg(self.agent_a, status='missed'),
                     started_at=now - timedelta(seconds=150), ended_at=now - timedelta(seconds=120))
        leg_b = _set(self.leg(self.agent_b, status='missed'),
                     started_at=now - timedelta(seconds=115), ended_at=now - timedelta(seconds=85))
        self.assertIsNone(find_answered_sibling(leg_a))
        self.assertEqual(send_missed_call_notifications(leg_a.id), 1)
        self.assertEqual(send_missed_call_notifications(leg_b.id), 1)
        self.assertEqual(set(self.missed_notifs().values_list('user_id', flat=True)),
                         {self.agent_a.id, self.agent_b.id})
        resp = self.api_get('/api/call-stats/overview/?range=today', user=self.admin)
        self.assertEqual(resp.data['answered_calls'], 0)
        self.assertEqual(resp.data['missed_calls'], 2)

    def test_later_call_from_same_number_is_a_new_call(self, mock_push):
        """Answered more than one attempt gap after the missed leg's chain
        ended: a callback, so the earlier leg stays missed."""
        now = timezone.now()
        leg_a = _set(self.leg(self.agent_a, status='missed'),
                     started_at=now - timedelta(seconds=230), ended_at=now - timedelta(seconds=200))
        leg_b = _set(self.leg(self.agent_b), started_at=now - timedelta(seconds=100))
        self.api_patch(f'/api/call-logs/{leg_b.id}/update_status/', {'status': 'answered'},
                       user=self.agent_b)
        leg_a.refresh_from_db()
        self.assertEqual(leg_a.status, 'missed')
        self.assertEqual(send_missed_call_notifications(leg_a.id), 1)

    def test_other_queue_answer_does_not_hide_a_missed_leg(self, mock_push):
        other_agent = self.create_user(email='fx-other-q@test.com')
        other_queue = Queue.objects.create(name='Other', slug='fx-other',
                                           group=self.create_tenant_group(name='FX other'))
        assignment = self.create_phone_assignment(user=other_agent, sip_config=self.config,
                                                  extension='299')
        QueueMember.objects.update_or_create(queue=other_queue, user_phone_assignment=assignment)
        now = timezone.now()
        leg_a = _set(self.leg(self.agent_a, status='missed'),
                     started_at=now - timedelta(seconds=100), ended_at=now - timedelta(seconds=70))
        _set(self.leg(other_agent, status='ended'), started_at=now - timedelta(seconds=65),
             answered_at=now - timedelta(seconds=60), ended_at=now - timedelta(seconds=10))
        self.assertIsNone(find_answered_sibling(leg_a))
        self.assertEqual(send_missed_call_notifications(leg_a.id), 1)


@patch('users.notification_utils._dispatch_push')
class TestRingallAnsweredElsewhere(_QueueCallMixin, CrmTestCase):
    strategy = 'ringall'

    def test_ringall_leg_is_cancelled_and_nobody_notified(self, mock_push):
        leg_a = self.leg(self.agent_a)
        leg_b = self.leg(self.agent_b)
        resp = self.api_patch(f'/api/call-logs/{leg_b.id}/update_status/',
                              {'status': 'answered'}, user=self.agent_b)
        self.assertEqual(resp.status_code, 200)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            resp = self.api_post(f'/api/call-logs/{leg_a.id}/end_call/', {}, user=self.agent_a)
        self.assertEqual(resp.data['status'], 'cancelled')
        self.assertEqual(len(callbacks), 0)
        self.api_post(f'/api/call-logs/{leg_b.id}/end_call/', {}, user=self.agent_b)
        self.assertEqual(self.missed_notifs().count(), 0)
        self.assert_one_served_call(answered_by=self.agent_b, other=self.agent_a)


# ============================================================================
# 2. Blind transfer to a colleague who does not answer
# ============================================================================


@patch('users.notification_utils._dispatch_push')
class TestBlindTransferUnanswered(_QueueCallMixin, CrmTestCase):

    def test_transfer_leg_is_not_matched_to_the_original_answered_leg(self, mock_push):
        now = timezone.now()
        # Agent A took the call (still talking) ...
        original = _set(self.leg(self.agent_a, status='answered'),
                        started_at=now - timedelta(seconds=120),
                        answered_at=now - timedelta(seconds=115))
        # ... and blind-transfers it to B, whose leg rings out.
        transfer = _set(self.leg(self.agent_b), started_at=now - timedelta(seconds=100))
        with self.captureOnCommitCallbacks(execute=False) as delayed:
            resp = self.api_post(f'/api/call-logs/{transfer.id}/end_call/', {}, user=self.agent_b)
        self.assertEqual(resp.data['status'], 'missed')
        _set(transfer, ended_at=now - timedelta(seconds=70))
        self.assertIsNone(find_answered_sibling(transfer))
        for callback in delayed:
            callback()
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'missed')
        self.assertEqual(
            list(self.missed_notifs().values_list('user_id', flat=True)), [self.agent_b.id])
        original.refresh_from_db()
        self.assertEqual(original.status, 'answered')


# ============================================================================
# 3. Recording proxy: public addresses and audio types only
# ============================================================================


def _addrinfo(*ips):
    return [(2 if ':' not in ip else 10, 1, 6, '', (ip, 443)) for ip in ips]


class TestRecordingAddressChecks(CrmTestCase):

    def test_is_public_ip(self):
        for ip in ('127.0.0.1', '10.0.0.5', '172.17.0.2', '192.168.1.1', '169.254.169.254',
                   '224.0.0.1', '240.0.0.1', '0.0.0.0', '100.64.0.1', '::1', 'fe80::1',
                   'fc00::1', '::ffff:127.0.0.1', 'ff02::1', 'not-an-ip'):
            self.assertFalse(is_public_ip(ip), ip)
        for ip in ('93.184.216.34', '185.229.109.65', '2606:4700:4700::1111'):
            self.assertTrue(is_public_ip(ip), ip)

    def test_every_resolved_address_must_be_public(self):
        with patch('crm.recording_hosts.socket.getaddrinfo',
                   return_value=_addrinfo('93.184.216.34', '10.0.0.7')):
            with self.assertRaises(UnsafeRecordingURL):
                resolve_public_addresses('pbx.tenant.test', 443)
        with patch('crm.recording_hosts.socket.getaddrinfo', side_effect=OSError('nx')):
            with self.assertRaises(UnsafeRecordingURL):
                resolve_public_addresses('nx.tenant.test', 443)
        with patch('crm.recording_hosts.socket.getaddrinfo',
                   return_value=_addrinfo('93.184.216.34')):
            self.assertEqual(resolve_public_addresses('pbx.tenant.test', 443), ['93.184.216.34'])

    def test_safe_content_type(self):
        self.assertEqual(safe_recording_content_type('audio/x-wav', 'https://h/a'), 'audio/x-wav')
        self.assertEqual(safe_recording_content_type('audio/mpeg; charset=x', 'https://h/a'),
                         'audio/mpeg')
        self.assertEqual(safe_recording_content_type('text/html', 'https://h/a.wav'), 'audio/wav')
        self.assertEqual(safe_recording_content_type('application/octet-stream',
                                                     'https://h/a.mp3'), 'audio/mpeg')
        self.assertEqual(safe_recording_content_type('text/html', 'https://h/latest/meta-data/'),
                         'application/octet-stream')
        self.assertEqual(safe_recording_content_type('', 'https://h/x'), 'application/octet-stream')

    @override_settings(CALL_RECORDING_ALLOWED_HOSTS=['ftp.tenant.test'])
    def test_only_http_and_https(self):
        with self.assertRaises(UnsafeRecordingURL):
            open_recording('ftp://ftp.tenant.test/a.wav')
        with self.assertRaises(UnsafeRecordingURL):
            open_recording('file://ftp.tenant.test/etc/passwd')

    def test_https_adapter_keeps_hostname_for_sni_and_certificate(self):
        adapter = _pinned_adapter('pbx2.echodesk.cloud')
        kw = adapter.poolmanager.connection_pool_kw
        self.assertEqual(kw['server_hostname'], 'pbx2.echodesk.cloud')
        self.assertEqual(kw['assert_hostname'], 'pbx2.echodesk.cloud')

    @override_settings(CALL_RECORDING_ALLOWED_HOSTS=['localhost'])
    def test_request_goes_to_the_checked_address_with_the_original_host(self):
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen['path'] = self.path
                seen['host'] = self.headers.get('Host')
                seen['range'] = self.headers.get('Range')
                body = b'RIFF'
                self.send_response(200)
                self.send_header('Content-Type', 'audio/wav')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        # The loopback test server is only reachable with the public-address
        # check relaxed, and DNS for "localhost" pinned to it.
        with patch('crm.recording_hosts.is_public_ip', return_value=True), \
                patch('crm.recording_hosts.socket.getaddrinfo',
                      return_value=[(2, 1, 6, '', ('127.0.0.1', port))]):
            resp = open_recording(f'http://localhost:{port}/rec/a.wav?x=1',
                                  headers={'Range': 'bytes=0-'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b'RIFF')
        self.assertEqual(seen, {'path': '/rec/a.wav?x=1', 'host': f'localhost:{port}',
                                'range': 'bytes=0-'})


class TestStreamRecordingSsrf(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='ssrf-admin@test.com')
        self.config = self.create_sip_config(created_by=self.admin, sip_server='pbx.tenant.test')
        self.call = self.create_call_log(handled_by=self.admin, status='ended',
                                         sip_config=self.config)
        self.url = f'/api/call-logs/{self.call.id}/recording/'

    def _record(self, recording_url):
        CallLog.objects.filter(pk=self.call.pk).update(recording_url=recording_url)

    @patch('requests.Session.get')
    def test_allowed_host_resolving_to_internal_address_is_refused(self, mock_get):
        # A PBX admin set sip_server to a name that points at the metadata
        # service / a docker host.
        self._record('http://pbx.tenant.test/latest/meta-data/')
        for ips in (('169.254.169.254',), ('127.0.0.1',), ('172.18.0.3',),
                    ('93.184.216.34', '10.1.2.3')):
            with patch('crm.recording_hosts.socket.getaddrinfo', return_value=_addrinfo(*ips)):
                resp = self.api_get(self.url, user=self.admin)
            self.assertEqual(resp.status_code, 404, ips)
        mock_get.assert_not_called()

    @patch('requests.Session.get')
    def test_ip_literal_sip_server_is_refused(self, mock_get):
        self.config.sip_server = '169.254.169.254'
        self.config.save()
        self._record('http://169.254.169.254/latest/meta-data/')
        resp = self.api_get(self.url, user=self.admin)
        self.assertEqual(resp.status_code, 404)
        mock_get.assert_not_called()

    @patch('crm.recording_hosts.socket.getaddrinfo', return_value=_addrinfo('93.184.216.34'))
    @patch('requests.Session.get')
    def test_html_upstream_is_never_served_as_html(self, mock_get, _dns):
        self._record('https://pbx.tenant.test:8443/recordings/a.wav')
        upstream = mock_get.return_value
        upstream.status_code = 200
        upstream.headers = {'Content-Type': 'text/html; charset=utf-8'}
        upstream.iter_content.return_value = [b'<script>alert(1)</script>']
        resp = self.api_get(self.url, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'audio/wav')
        self.assertEqual(resp['X-Content-Type-Options'], 'nosniff')
        self.assertTrue(resp['Content-Disposition'].startswith('inline'))
        # Connected to the checked address, Host header kept, no redirects.
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], 'https://93.184.216.34:8443/recordings/a.wav')
        self.assertEqual(kwargs['headers']['Host'], 'pbx.tenant.test:8443')
        self.assertIs(kwargs['allow_redirects'], False)

        self._record('https://pbx.tenant.test/latest/meta-data/')
        resp = self.api_get(self.url, user=self.admin)
        self.assertEqual(resp['Content-Type'], 'application/octet-stream')

    @patch('crm.recording_hosts.socket.getaddrinfo', return_value=_addrinfo('93.184.216.34'))
    @patch('requests.Session.get')
    def test_audio_upstream_type_is_kept(self, mock_get, _dns):
        self._record('https://pbx.tenant.test/recordings/a')
        upstream = mock_get.return_value
        upstream.status_code = 206
        upstream.headers = {'Content-Type': 'audio/ogg', 'Content-Range': 'bytes 0-3/4'}
        upstream.iter_content.return_value = [b'OggS']
        resp = self.api_get(self.url, user=self.admin, HTTP_RANGE='bytes=0-3')
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp['Content-Type'], 'audio/ogg')
        self.assertEqual(resp['Content-Range'], 'bytes 0-3/4')
        self.assertEqual(mock_get.call_args.kwargs['headers']['Range'], 'bytes=0-3')


# ============================================================================
# 4. Shared SIP password and sip_enabled
# ============================================================================


def _body(resp):
    return resp.content.decode()


class TestSharedSipPassword(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.owner = self.create_admin(email='sh-owner@test.com')
        self.config = self.create_sip_config(
            created_by=self.owner, is_default=True, username='trunk-login',
            password='TRUNK-SECRET',
        )
        self.agent = self.create_user(email='sh-agent@test.com')
        self.create_phone_assignment(user=self.agent, sip_config=self.config, extension='300',
                                     extension_password='AGENT-PW')
        # Non-staff PBX admins: manage_settings flag, or the admin role.
        self.settings_admin = self.create_user(email='sh-settings@test.com',
                                               can_manage_settings=True)
        self.role_admin = self.create_user(email='sh-role@test.com', role='admin')
        self.loner = self.create_user(email='sh-loner@test.com')

    def _webrtc(self, user):
        return self.api_get(f'/api/sip-configurations/{self.config.id}/webrtc_config/', user=user)

    def test_non_staff_pbx_admins_never_get_the_shared_password(self):
        for user in (self.settings_admin, self.role_admin, self.loner):
            resp = self.api_get('/api/sip-configurations/my_config/', user=user)
            self.assertEqual(resp.status_code, 404, user.email)
            self.assertNotIn('TRUNK-SECRET', _body(resp))
            resp = self._webrtc(user)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data['username'], '')
            self.assertEqual(resp.data['password'], '')
            self.assertIs(resp.data['sip_enabled'], False)
            self.assertNotIn('TRUNK-SECRET', _body(resp))
            # Same shape as before: the softphone fields are all there.
            for key in ('sip_server', 'sip_port', 'websocket_path', 'stun_server',
                        'turn_server', 'user_assignments'):
                self.assertIn(key, resp.data)

    def test_staff_superuser_and_creator_get_it(self):
        staff = self.create_user(email='sh-staff@test.com', is_staff=True)
        superuser = self.create_user(email='sh-super@test.com', is_superuser=True)
        creator = self.create_user(email='sh-creator@test.com', can_manage_settings=True)
        created = self.create_sip_config(created_by=creator, name='Mine', username='own',
                                         password='OWN-SECRET')
        for user in (staff, superuser, self.owner):
            resp = self.api_get('/api/sip-configurations/my_config/', user=user)
            self.assertEqual(resp.status_code, 200, user.email)
            self.assertIsNone(resp.data['id'])
            self.assertEqual(resp.data['extension_password'], 'TRUNK-SECRET')
            self.assertIs(resp.data['sip_enabled'], True)
            self.assertIs(resp.data['sip_configuration']['sip_enabled'], True)
            resp = self._webrtc(user)
            self.assertEqual(resp.data['password'], 'TRUNK-SECRET')
            self.assertIs(resp.data['sip_enabled'], True)
        resp = self.api_get(f'/api/sip-configurations/{created.id}/webrtc_config/', user=creator)
        self.assertEqual(resp.data['password'], 'OWN-SECRET')
        self.assertIs(resp.data['sip_enabled'], True)
        # The creator of another config does not get the default one's.
        resp = self._webrtc(creator)
        self.assertEqual(resp.data['password'], '')

    def test_agent_with_extension_is_sip_enabled(self):
        resp = self.api_get('/api/sip-configurations/my_config/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['extension_password'], 'AGENT-PW')
        self.assertIs(resp.data['sip_enabled'], True)
        resp = self._webrtc(self.agent)
        self.assertEqual(resp.data['password'], 'AGENT-PW')
        self.assertIs(resp.data['sip_enabled'], True)
        self.assertNotIn('TRUNK-SECRET', _body(resp))

    def test_plain_retrieve_has_no_sip_enabled_field_change(self):
        resp = self.api_get(f'/api/sip-configurations/{self.config.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('sip_enabled', resp.data)
        self.assertEqual(resp.data['password'], '')


# ============================================================================
# 5. Who may change a call's status
# ============================================================================


class TestCallStatusOwnership(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.handler = self.create_user(email='own-handler@test.com')
        self.colleague = self.create_user(email='own-colleague@test.com')
        self.config = self.create_sip_config(sip_server='pbx.tenant.test', is_default=True)
        self.call = self.create_call_log(handled_by=self.handler, status='answered',
                                         answered_at=timezone.now(), sip_config=self.config)

    def test_colleague_cannot_update_status_or_recording(self):
        url = f'/api/call-logs/{self.call.id}/update_status/'
        for body in ({'status': 'ended'},
                     {'status': 'answered', 'recording_url': 'https://pbx.tenant.test/x.wav'}):
            resp = self.api_patch(url, body, user=self.colleague)
            self.assertEqual(resp.status_code, 403, body)
        self.call.refresh_from_db()
        self.assertEqual(self.call.status, 'answered')
        self.assertEqual(self.call.recording_url, '')

    def test_colleague_cannot_end_call(self):
        resp = self.api_post(f'/api/call-logs/{self.call.id}/end_call/', {}, user=self.colleague)
        self.assertEqual(resp.status_code, 403)
        self.call.refresh_from_db()
        self.assertEqual(self.call.status, 'answered')
        self.assertIsNone(self.call.ended_at)

    def test_unowned_call_only_admins(self):
        CallLog.objects.filter(pk=self.call.pk).update(handled_by=None)
        resp = self.api_post(f'/api/call-logs/{self.call.id}/end_call/', {}, user=self.colleague)
        self.assertEqual(resp.status_code, 403)

    def test_handler_can(self):
        url = f'/api/call-logs/{self.call.id}/update_status/'
        resp = self.api_patch(url, {'status': 'answered',
                                    'recording_url': 'https://pbx.tenant.test/x.wav'},
                              user=self.handler)
        self.assertEqual(resp.status_code, 200, resp.data)
        resp = self.api_post(f'/api/call-logs/{self.call.id}/end_call/', {}, user=self.handler)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'ended')

    def test_staff_superuser_and_pbx_admins_can(self):
        users = [
            self.create_user(email='own-staff@test.com', is_staff=True),
            self.create_user(email='own-super@test.com', is_superuser=True),
            self.create_user(email='own-settings@test.com', can_manage_settings=True),
            self.create_user(email='own-role@test.com', role='admin'),
        ]
        url = f'/api/call-logs/{self.call.id}/update_status/'
        for user in users:
            resp = self.api_patch(url, {'status': 'answered'}, user=user)
            self.assertEqual(resp.status_code, 200, user.email)
        resp = self.api_post(f'/api/call-logs/{self.call.id}/end_call/', {}, user=users[2])
        self.assertEqual(resp.status_code, 200)

    def test_create_sets_handler_to_the_caller(self):
        resp = self.api_post('/api/call-logs/', {
            'caller_number': '+995599000111', 'recipient_number': '100',
            'direction': 'inbound', 'handled_by': self.handler.id,
        }, user=self.colleague)
        self.assertEqual(resp.status_code, 201, resp.data)
        created = CallLog.objects.filter(caller_number='+995599000111').get()
        self.assertEqual(created.handled_by, self.colleague)
        # The creator can then drive their own call.
        resp = self.api_post(f'/api/call-logs/{created.id}/end_call/', {}, user=self.colleague)
        self.assertEqual(resp.status_code, 200)


# ============================================================================
# 6. Fallback recipients = PBX admins
# ============================================================================


class TestFallbackRecipients(CrmTestCase):

    def test_fallback_includes_manage_settings_holders(self):
        staff = self.create_admin(email='fb-staff@test.com')
        settings_admin = self.create_user(email='fb-settings@test.com', can_manage_settings=True)
        role_admin = self.create_user(email='fb-role@test.com', role='admin')
        agent = self.create_user(email='fb-agent@test.com')
        manager = self.create_manager(email='fb-manager@test.com')
        inactive = self.create_user(email='fb-inactive@test.com', can_manage_settings=True,
                                    is_active=False)
        with self.captureOnCommitCallbacks(execute=False):
            call = CallLog.objects.create(caller_number='+995599222333',
                                          recipient_number='+995000000001',
                                          direction='inbound', status='missed', handled_by=None)
        ids = {u.id for u in resolve_missed_call_recipients(call)}
        self.assertTrue({staff.id, settings_admin.id, role_admin.id} <= ids)
        self.assertFalse({agent.id, manager.id, inactive.id} & ids)
        self.assertLessEqual(len(ids), 10)
