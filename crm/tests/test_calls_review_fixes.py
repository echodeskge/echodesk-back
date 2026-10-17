"""
Review follow-ups for the calls hardening (parity package P3):

* a ring-all leg answered by a colleague is ``cancelled`` and counted
  neither as answered nor as missed anywhere (stats, ``?missed=true``);
* the answered-elsewhere match ignores a call that was already over;
* ``call_missed`` is queued only after the save commits;
* agents may PATCH only ``notes`` on a call log;
* recording URLs must be on a tenant PBX host (update_status, streaming);
* client call history / phone lookup need ``ip_calling``;
* a non-admin read of the PBX settings creates nothing;
* list endpoints run a constant number of queries;
* request component names match the documented contract.
"""
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone

from crm.models import CallLog, InboundRoute, PbxServer, PbxSettings, Queue, QueueMember
from crm.tests.conftest import CrmTestCase
from users.models import Notification


def _run_on_commit_now():
    return patch('django.db.transaction.on_commit',
                 side_effect=lambda fn, using=None, robust=False: fn())


# ============================================================================
# Answered elsewhere is not missed
# ============================================================================


@patch('users.notification_utils._dispatch_push')
class TestAnsweredElsewhereNotMissed(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='ae-admin@test.com')
        self.agent = self.create_user(email='ae-agent@test.com')
        self.colleague = self.create_user(email='ae-colleague@test.com')
        on_commit = _run_on_commit_now()
        on_commit.start()
        self.addCleanup(on_commit.stop)

    def _ring_all_call(self):
        group = self.create_tenant_group(name='AE group')
        queue = Queue.objects.create(name='AE', slug='ae', group=group)
        InboundRoute.objects.create(did='+995322000111', destination_type='queue',
                                    destination_queue=queue)
        now = timezone.now()
        answered = self.create_call_log(
            handled_by=self.colleague, status='answered', direction='inbound',
            caller_number='+995555600700', recipient_number='+995322000111',
            answered_at=now,
        )
        mine = self.create_call_log(
            handled_by=self.agent, status='ringing', direction='inbound',
            caller_number='+995555600700', recipient_number='+995322000111',
        )
        resp = self.api_post(f'/api/call-logs/{mine.id}/end_call/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'cancelled')
        # The colleague hangs up after talking.
        resp = self.api_post(f'/api/call-logs/{answered.id}/end_call/', {}, user=self.colleague)
        self.assertEqual(resp.data['status'], 'ended')
        return queue, answered, mine

    def test_stats_and_missed_filter_do_not_count_the_leg(self, mock_push):
        queue, answered, mine = self._ring_all_call()

        resp = self.api_get('/api/call-stats/overview/?range=today', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['missed_calls'], 0)
        self.assertEqual(resp.data['answered_calls'], 1)

        resp = self.api_get('/api/call-stats/users/?range=today', user=self.admin)
        rows = {r['user_id']: r for r in resp.data['results']}
        self.assertEqual(rows[self.agent.id]['missed_count'], 0)
        self.assertEqual(rows[self.agent.id]['answered_count'], 0)
        self.assertEqual(rows[self.colleague.id]['answered_count'], 1)

        resp = self.api_get(f'/api/call-stats/queues/?queue_id={queue.id}&range=today',
                            user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['abandoned_count'], 0)
        self.assertEqual(resp.data['answered_count'], 1)

        resp = self.api_get('/api/call-logs/?missed=true', user=self.agent)
        self.assertNotIn(mine.id, {r['id'] for r in self.get_results(resp)})

        resp = self.api_get('/api/call-logs/statistics/?period=today', user=self.admin)
        self.assertEqual(resp.data['missed_calls'], 0)
        self.assertEqual(resp.data['answered_calls'], 1)

        self.assertFalse(Notification.objects.filter(notification_type='call_missed').exists())

    def test_late_relabel_is_also_not_missed(self, mock_push):
        mine = self.create_call_log(handled_by=self.agent, status='ringing',
                                    caller_number='+995555600701')
        self.create_call_log(handled_by=self.colleague, status='answered',
                             caller_number='+995555600701', answered_at=timezone.now())
        # Our leg goes missed before the colleague's answer arrived; the
        # delayed notification task relabels it.
        CallLog.objects.filter(pk=mine.pk).update(status='missed')
        from crm.call_notifications import send_missed_call_notifications
        self.assertEqual(send_missed_call_notifications(mine.pk), 0)
        resp = self.api_get('/api/call-logs/?missed=true', user=self.agent)
        self.assertNotIn(mine.id, {r['id'] for r in self.get_results(resp)})
        resp = self.api_get('/api/call-stats/overview/?range=today', user=self.admin)
        self.assertEqual(resp.data['missed_calls'], 0)

    def test_callback_after_hangup_is_missed(self, mock_push):
        """Answered, hung up, then called back unanswered within the window:
        the second call is a real missed call."""
        now = timezone.now()
        first = self.create_call_log(
            handled_by=self.colleague, status='ended', direction='inbound',
            caller_number='+995555600702',
        )
        CallLog.objects.filter(pk=first.pk).update(
            started_at=now - timedelta(seconds=90),
            answered_at=now - timedelta(seconds=85),
            ended_at=now - timedelta(seconds=30),
            duration=timedelta(seconds=55),
        )
        callback = self.create_call_log(
            handled_by=self.agent, status='ringing', direction='inbound',
            caller_number='+995555600702',
        )
        resp = self.api_post(f'/api/call-logs/{callback.id}/end_call/', {}, user=self.agent)
        self.assertEqual(resp.data['status'], 'missed')
        self.assertTrue(Notification.objects.filter(
            notification_type='call_missed', user=self.agent).exists())


# ============================================================================
# call_missed waits for the commit
# ============================================================================


@patch('users.notification_utils._dispatch_push')
class TestMissedCallOnCommit(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='oc-agent@test.com')

    def _notifs(self):
        return Notification.objects.filter(notification_type='call_missed', user=self.agent)

    def test_queued_only_after_commit(self, mock_push):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.create_call_log(handled_by=self.agent, status='missed')
            self.assertFalse(self._notifs().exists())
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(self._notifs().count(), 1)

    def test_rolled_back_save_queues_nothing(self, mock_push):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    self.create_call_log(handled_by=self.agent, status='missed')
                    raise RuntimeError('roll back')
            except RuntimeError:
                pass
        self.assertEqual(len(callbacks), 0)
        self.assertFalse(self._notifs().exists())


# ============================================================================
# Agents may only edit notes; recording hosts are allow-listed
# ============================================================================


class TestCallLogWriteLimits(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='wl-admin@test.com')
        self.agent = self.create_user(email='wl-agent@test.com')
        self.other = self.create_user(email='wl-other@test.com')
        self.config = self.create_sip_config(created_by=self.admin, sip_server='pbx.tenant.test')
        self.call = self.create_call_log(handled_by=self.other, status='answered',
                                         sip_config=self.config)

    def test_agent_can_patch_notes_only(self):
        url = f'/api/call-logs/{self.call.id}/'
        resp = self.api_patch(url, {'notes': 'called back'}, user=self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['notes'], 'called back')
        for body in ({'handled_by': self.agent.id}, {'status': 'ended'},
                     {'notes': 'x', 'duration': '00:05:00'},
                     {'answered_at': timezone.now().isoformat()}):
            resp = self.api_patch(url, body, user=self.agent)
            self.assertEqual(resp.status_code, 403, body)
        resp = self.api_put(url, {'caller_number': '1', 'recipient_number': '2',
                                  'status': 'ended'}, user=self.agent)
        self.assertEqual(resp.status_code, 403)
        self.call.refresh_from_db()
        self.assertEqual(self.call.handled_by, self.other)
        self.assertEqual(self.call.status, 'answered')

    def test_admin_can_patch_other_fields(self):
        resp = self.api_patch(f'/api/call-logs/{self.call.id}/',
                              {'handled_by': self.agent.id}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.call.refresh_from_db()
        self.assertEqual(self.call.handled_by, self.agent)

    def test_update_status_rejects_foreign_recording_host(self):
        url = f'/api/call-logs/{self.call.id}/update_status/'
        resp = self.api_patch(url, {
            'status': 'answered', 'recording_url': 'http://169.254.169.254/latest/meta-data/',
        }, user=self.agent)
        self.assertEqual(resp.status_code, 400)
        self.call.refresh_from_db()
        self.assertEqual(self.call.recording_url, '')

    def test_update_status_accepts_pbx_host(self):
        PbxServer.objects.create(
            name='Main', fqdn='pbx.acme.test', realtime_db_host='db', realtime_db_name='a',
            realtime_db_user='u',
        )
        url = f'/api/call-logs/{self.call.id}/update_status/'
        for rec in ('https://pbx.acme.test:8443/recordings/a.wav',
                    'https://pbx.tenant.test/rec/b.wav',
                    'https://pbx2.echodesk.cloud:8443/recordings/c.wav'):
            resp = self.api_patch(url, {'status': 'answered', 'recording_url': rec},
                                  user=self.agent)
            self.assertEqual(resp.status_code, 200, (rec, resp.data))
            self.call.refresh_from_db()
            self.assertEqual(self.call.recording_url, rec)

    @patch('requests.get')
    def test_stream_refuses_foreign_host(self, mock_get):
        CallLog.objects.filter(pk=self.call.pk).update(
            recording_url='http://169.254.169.254/latest/meta-data/')
        resp = self.api_get(f'/api/call-logs/{self.call.id}/recording/', user=self.admin)
        self.assertEqual(resp.status_code, 404)
        mock_get.assert_not_called()

    @patch('requests.get')
    def test_stream_allowed_host_does_not_follow_redirects(self, mock_get):
        CallLog.objects.filter(pk=self.call.pk).update(
            recording_url='https://pbx.tenant.test/rec/a.wav')
        upstream = mock_get.return_value
        upstream.status_code = 302
        upstream.headers = {'Location': 'http://169.254.169.254/'}
        resp = self.api_get(f'/api/call-logs/{self.call.id}/recording/', user=self.admin)
        self.assertEqual(resp.status_code, 502)
        self.assertIs(mock_get.call_args.kwargs['allow_redirects'], False)

        upstream.status_code = 200
        upstream.headers = {'Content-Type': 'audio/wav'}
        upstream.iter_content.return_value = [b'RIFF']
        resp = self.api_get(f'/api/call-logs/{self.call.id}/recording/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b''.join(resp.streaming_content), b'RIFF')


# ============================================================================
# Client call history / lookup need ip_calling
# ============================================================================


class TestClientCallsGate(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='cg-agent@test.com')
        self.client_obj = self.create_client(phone='+995555909090')

    def test_gated_without_feature(self):
        self.set_ip_calling(False)
        self.assertEqual(self.api_get(
            f'/api/clients/{self.client_obj.id}/call_history/', user=self.agent).status_code, 403)
        self.assertEqual(self.api_get(
            '/api/clients/lookup-by-phone/?phone=555909090', user=self.agent).status_code, 403)
        # The client records themselves are not part of the calls surface.
        self.assertEqual(self.api_get('/api/clients/', user=self.agent).status_code, 200)

    def test_open_with_feature(self):
        self.assertEqual(self.api_get(
            f'/api/clients/{self.client_obj.id}/call_history/', user=self.agent).status_code, 200)
        resp = self.api_get('/api/clients/lookup-by-phone/?phone=555909090', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 1)


# ============================================================================
# PBX settings reads never write
# ============================================================================


class TestPbxSettingsReadNoWrite(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='ps-admin@test.com')
        self.agent = self.create_user(email='ps-agent@test.com')
        self.config = self.create_sip_config(created_by=self.admin)

    def test_agent_read_does_not_create(self):
        resp = self.api_get(f'/api/pbx-settings/{self.config.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data['id'])
        self.assertIn('working_hours_enabled', resp.data)
        self.assertFalse(PbxSettings.objects.filter(sip_configuration=self.config).exists())

    def test_agent_reads_existing(self):
        stored = self.create_pbx_settings(sip_config=self.config, forward_number='777')
        resp = self.api_get(f'/api/pbx-settings/{self.config.id}/', user=self.agent)
        self.assertEqual(resp.data['id'], stored.id)
        self.assertEqual(resp.data['forward_number'], '777')

    def test_admin_read_creates(self):
        resp = self.api_get(f'/api/pbx-settings/{self.config.id}/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.data['id'])
        self.assertTrue(PbxSettings.objects.filter(sip_configuration=self.config).exists())


# ============================================================================
# Constant query counts on the PBX lists
# ============================================================================


class TestPbxListQueryCounts(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='qc-admin@test.com')

    def _count(self, url):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.api_get(url, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        return len(ctx.captured_queries), resp

    def _add_queue(self, i):
        group = self.create_tenant_group(name=f'QC group {i}')
        queue = Queue.objects.create(name=f'QC {i}', slug=f'qc-{i}', group=group)
        config = self.create_sip_config(created_by=self.admin, name=f'QC cfg {i}')
        for n in range(3):
            user = self.create_user(email=f'qc-{i}-{n}@test.com')
            assignment = self.create_phone_assignment(user=user, sip_config=config,
                                                      extension=f'{i}{n}9')
            QueueMember.objects.create(queue=queue, user_phone_assignment=assignment,
                                       is_active=True)
        return queue, config

    def test_queue_list_query_count_is_constant(self):
        self._add_queue(1)
        warm, _ = self._count('/api/queues/')  # warm caches (feature check etc.)
        one, _ = self._count('/api/queues/')
        for i in range(2, 6):
            self._add_queue(i)
        many, resp = self._count('/api/queues/')
        self.assertEqual(one, many)
        counts = {r['name']: r['member_count'] for r in self.get_results(resp)}
        self.assertEqual(counts['QC 5'], 3)

    def test_sip_configuration_list_query_count_is_constant(self):
        self._add_queue(1)
        self._count('/api/sip-configurations/')
        one, _ = self._count('/api/sip-configurations/')
        for i in range(2, 6):
            self._add_queue(i)
        many, resp = self._count('/api/sip-configurations/')
        self.assertEqual(one, many)
        counts = {r['name']: r['assignment_count'] for r in self.get_results(resp)}
        self.assertEqual(counts['QC cfg 5'], 3)


# ============================================================================
# Schema component names
# ============================================================================


class TestCallsSchemaComponentNames(CrmTestCase):
    _schema = None

    @classmethod
    def schema(cls):
        if cls._schema is None:
            from amanati_crm.schema import TenantAwareSchemaGenerator
            cls._schema = TenantAwareSchemaGenerator().get_schema(request=None, public=True)
        return cls._schema

    def test_request_components_have_contract_names(self):
        components = self.schema()['components']['schemas']
        for name in ('CallEndRequest', 'CallTransferRequest',
                     'PbxSoundUploadRequest', 'PbxSoundRemoveRequest'):
            self.assertIn(name, components)
            self.assertNotIn(f'{name}Request', components)
        paths = self.schema()['paths']
        body = paths['/api/call-logs/{id}/end_call/']['post']['requestBody']
        self.assertEqual(body['content']['application/json']['schema']['$ref'],
                         '#/components/schemas/CallEndRequest')

    def test_pbx_settings_id_is_nullable(self):
        component = self.schema()['components']['schemas']['PbxSettings']
        self.assertTrue(component['properties']['id'].get('nullable'))
