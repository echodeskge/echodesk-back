"""Calls onboarding step: admin gate, hosted request → Telegram, BYO server
through the pbx-servers create path (provisioning patched), trunk / agents /
queue / routes / hours / rating, idempotent, context (install command) and
prefill."""
from unittest.mock import patch

from crm.models import (
    InboundRoute, PbxServer, PbxSettings, Queue, SipConfiguration, Trunk, UserPhoneAssignment,
)
from crm.tests.conftest import CrmTestCase
from onboarding.models import TenantOnboarding
from users.models import TenantGroup

ONB = '/api/onboarding/'
APPLY = f'{ONB}ip_calling/apply/'

HOURS = {
    'timezone': 'Asia/Tbilisi',
    'schedule': {
        'mon': {'from': '09:00', 'to': '18:00', 'break_from': '13:00', 'break_to': '14:00'},
        'tue': {'from': '09:00', 'to': '18:00', 'break_from': None, 'break_to': None},
        'wed': {'from': '09:00', 'to': '18:00'},
        'thu': {'from': '09:00', 'to': '18:00'},
        'fri': {'from': '09:30', 'to': '17:30'},
        'sat': None,
        'sun': None,
    },
    'after_hours_action': 'forward',
    'forward_number': '+995555000000',
}


def fake_provision(pbx):
    """Stand-in for ``provision_and_bootstrap``: what a real run leaves on the row."""
    pbx.realtime_db_host = 'db.internal'
    pbx.realtime_db_name = 'asterisk_test'
    pbx.realtime_db_user = 'asterisk_rw_test'
    pbx.status = pbx.STATUS_PROVISIONING
    pbx.save()


class CallsStepTests(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.owner = self.create_user(email='owner@test.com', role='admin', is_staff=True, is_superuser=True)
        self.nino = self.create_user(email='nino@test.com', first_name='Nino', last_name='K')
        self.giga = self.create_user(email='giga@test.com', first_name='Giga')
        self.agent = self.create_user(email='agent@test.com', role='agent')

    def answers(self, **overrides):
        base = {
            'hosting': 'byo',
            'server': {'name': 'Main PBX', 'fqdn': 'PBX.Acme.ge', 'public_ip': '203.0.113.10', 'ami_port': 5038},
            'trunk': {'provider': 'magti', 'sip_server': '89.150.1.11', 'sip_port': 5060, 'username': 'acme-trunk',
                      'password': 'trunk-secret', 'realm': '', 'phone_numbers': ['+995 32 242 12 19', '+995322421220'],
                      'caller_id_number': ''},
            'agent_user_ids': [self.nino.id, self.giga.id],
            'queue': {'strategy': 'rrmemory', 'timeout_seconds': 25, 'max_wait_seconds': 240},
            'hours': HOURS,
            'rating': {'review_method': 'sms', 'sms_api_key': 'sender-key'},
        }
        base.update(overrides)
        return base

    def _apply(self, data, user=None):
        with patch('crm.pbx_provisioning.provision_and_bootstrap', side_effect=fake_provision) as prov:
            resp = self.api_post(APPLY, {'answers': data}, user=user or self.owner)
        resp.provision_calls = prov.call_count
        return resp

    def test_non_admin_gets_403(self):
        self.assertEqual(self._apply(self.answers(), user=self.agent).status_code, 403)

    def test_byo_apply_writes_everything(self):
        resp = self._apply(self.answers())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.provision_calls, 1)

        pbx = PbxServer.objects.get()
        self.assertEqual(pbx.fqdn, 'pbx.acme.ge')
        self.assertEqual(pbx.name, 'Main PBX')
        self.assertEqual(pbx.public_ip, '203.0.113.10')
        self.assertEqual(pbx.status, PbxServer.STATUS_PROVISIONING)
        self.assertEqual(pbx.realtime_db_name, 'asterisk_test')
        self.assertTrue(pbx.enrollment_token)
        self.assertEqual(pbx.wss_url, 'wss://pbx.acme.ge:8089/ws')

        trunk = Trunk.objects.get()
        self.assertEqual(trunk.name, 'Magti')
        self.assertEqual(trunk.provider, 'Magti')
        self.assertEqual(trunk.username, 'acme-trunk')
        self.assertEqual(trunk.password, 'trunk-secret')
        self.assertTrue(trunk.register)
        self.assertTrue(trunk.is_default)
        self.assertEqual(trunk.phone_numbers, ['+995322421219', '+995322421220'])
        self.assertEqual(trunk.caller_id_number, '+995322421219')

        config = SipConfiguration.objects.get(name='EchoDesk')
        self.assertEqual(config.sip_server, 'pbx.acme.ge')
        self.assertEqual(config.sip_port, 8089)
        self.assertEqual(config.websocket_path, '/ws')
        self.assertTrue(config.is_default)
        self.assertEqual(config.phone_number, '+995322421219')
        assignments = {a.user_id: a for a in UserPhoneAssignment.objects.all()}
        self.assertEqual(sorted(assignments), sorted([self.nino.id, self.giga.id]))
        self.assertEqual(assignments[self.nino.id].extension, '100')
        self.assertEqual(assignments[self.giga.id].extension, '101')
        self.assertEqual(assignments[self.nino.id].sip_configuration, config)
        self.assertEqual(assignments[self.nino.id].display_name, 'Nino K')
        self.assertTrue(assignments[self.nino.id].extension_password)

        group = TenantGroup.objects.get(name='Calls')
        self.assertEqual(set(group.members.values_list('id', flat=True)), {self.nino.id, self.giga.id})
        queue = Queue.objects.get(slug='support')
        self.assertEqual(queue.group, group)
        self.assertEqual(queue.strategy, 'rrmemory')
        self.assertEqual(queue.timeout_seconds, 25)
        self.assertEqual(queue.max_wait_seconds, 240)
        self.assertTrue(queue.is_default)
        routes = {r.did: r for r in InboundRoute.objects.all()}
        self.assertEqual(sorted(routes), ['+995322421219', '+995322421220'])
        self.assertEqual(routes['+995322421219'].destination_queue, queue)
        self.assertEqual(routes['+995322421219'].trunk, trunk)
        self.assertEqual(routes['+995322421219'].destination_type, 'queue')

        pbx_settings = PbxSettings.objects.get(sip_configuration=config)
        self.assertTrue(pbx_settings.working_hours_enabled)
        self.assertEqual(pbx_settings.timezone, 'Asia/Tbilisi')
        self.assertEqual(pbx_settings.working_hours_schedule['monday'], [9, 10, 11, 12, 14, 15, 16, 17])
        self.assertEqual(pbx_settings.working_hours_schedule['tuesday'], list(range(9, 18)))
        self.assertEqual(pbx_settings.working_hours_schedule['friday'], list(range(9, 18)))
        self.assertEqual(pbx_settings.working_hours_schedule['saturday'], [])
        self.assertEqual(pbx_settings.working_hours_schedule['sunday'], [])
        self.assertEqual(pbx_settings.after_hours_action, 'forward')
        self.assertEqual(pbx_settings.forward_number, '+995555000000')
        self.assertEqual(pbx_settings.review_method, 'sms')
        self.assertEqual(pbx_settings.sms_api_key, 'sender-key')

        # The apply response's step already carries the fresh server context
        # (status + install command) — the wizard caches ``response.step``.
        server = resp.data['step']['context']['server']
        self.assertEqual(server['status'], 'provisioning')
        self.assertEqual(server['fqdn'], 'pbx.acme.ge')
        self.assertIn(f'/api/pbx/install/{pbx.enrollment_token}/', server['install_command'])
        self.assertEqual(resp.data['step']['status'], 'done')

        created = resp.data['result']['created']
        self.assertEqual(created['pbx_servers'], 1)
        self.assertEqual(created['trunks'], 1)
        self.assertEqual(created['sip_configurations'], 1)
        self.assertEqual(created['user_phone_assignments'], 2)
        self.assertEqual(created['groups'], 1)
        self.assertEqual(created['queues'], 1)
        self.assertEqual(created['inbound_routes'], 2)
        self.assertEqual(created['pbx_settings'], 1)
        self.assertNotIn('hosted_request_sent', resp.data['result']['notes'])
        self.assertEqual(TenantOnboarding.get().status_of('ip_calling'), 'done')

    def test_apply_twice_creates_nothing_twice(self):
        self.assertEqual(self._apply(self.answers()).status_code, 200)
        resp = self._apply(self.answers())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.provision_calls, 0)
        self.assertEqual(PbxServer.objects.count(), 1)
        self.assertEqual(Trunk.objects.count(), 1)
        self.assertEqual(SipConfiguration.objects.count(), 1)
        self.assertEqual(UserPhoneAssignment.objects.count(), 2)
        self.assertEqual(TenantGroup.objects.filter(name='Calls').count(), 1)
        self.assertEqual(Queue.objects.count(), 1)
        self.assertEqual(InboundRoute.objects.count(), 2)
        self.assertEqual(PbxSettings.objects.count(), 1)
        self.assertEqual(resp.data['result']['created'], {})
        self.assertEqual(resp.data['result']['updated'], [])

    def test_extensions_skip_taken_numbers_and_existing_users(self):
        config = SipConfiguration.objects.create(name='Old', sip_server='old.example', username='u', password='p', is_default=True)
        UserPhoneAssignment.objects.create(user=self.agent, sip_configuration=config, extension='100', extension_password='x', phone_number='1')
        UserPhoneAssignment.objects.create(user=self.nino, sip_configuration=config, extension='250', extension_password='x', phone_number='1')
        resp = self._apply({'agent_user_ids': [self.nino.id, self.giga.id, self.owner.id]})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(UserPhoneAssignment.objects.get(user=self.nino).extension, '250')   # kept
        self.assertEqual(UserPhoneAssignment.objects.get(user=self.giga).extension, '101')   # 100 is taken
        self.assertEqual(UserPhoneAssignment.objects.get(user=self.owner).extension, '102')
        self.assertEqual(resp.data['result']['created']['user_phone_assignments'], 2)
        # No PbxServer: the existing default configuration is reused, no "EchoDesk" one.
        self.assertFalse(SipConfiguration.objects.filter(name='EchoDesk').exists())
        self.assertEqual(UserPhoneAssignment.objects.get(user=self.giga).sip_configuration, config)

    def test_hosted_sends_the_team_notification_and_still_applies_the_rest(self):
        data = self.answers(hosting='hosted')
        with patch('tenants.telegram_notifications.send_telegram_message', return_value=True) as send:
            resp = self._apply(data)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.provision_calls, 0)
        self.assertFalse(PbxServer.objects.exists())
        send.assert_called_once()
        message = send.call_args.args[0]
        self.assertIn(self.tenant.name, message)
        self.assertIn('owner@test.com', message)
        self.assertIn('pbx.acme.ge', message)
        self.assertIn('Main PBX', message)
        self.assertNotIn('trunk-secret', message)
        self.assertIn('hosted_request_sent', resp.data['result']['notes'])
        self.assertEqual(resp.data['result']['created']['hosted_request'], 1)
        # The rest is applied: trunk, agents on a server-less config, queue, routes, hours.
        self.assertEqual(Trunk.objects.count(), 1)
        config = SipConfiguration.objects.get(name='EchoDesk')
        self.assertEqual(config.sip_server, '')
        self.assertEqual(UserPhoneAssignment.objects.count(), 2)
        self.assertEqual(Queue.objects.filter(slug='support').count(), 1)
        self.assertEqual(InboundRoute.objects.count(), 2)
        self.assertTrue(PbxSettings.objects.get(sip_configuration=config).working_hours_enabled)
        # context reports the request from the stored answers
        step = next(s for s in self.api_get(ONB, user=self.owner).data['steps'] if s['key'] == 'ip_calling')
        self.assertTrue(step['context']['hosted_request_sent'])
        self.assertIsNone(step['context']['server'])

    def test_hosted_notification_failure_is_recorded_not_raised(self):
        with patch('tenants.telegram_notifications.send_telegram_message', return_value=False):
            resp = self._apply({'hosting': 'hosted'})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn('hosted_request_sent=false', resp.data['result']['notes'])

    def test_provisioning_failure_marks_the_server_error_and_keeps_the_rest(self):
        with patch('crm.pbx_provisioning.provision_and_bootstrap', side_effect=RuntimeError('db down')):
            resp = self.api_post(APPLY, {'answers': self.answers()}, user=self.owner)
        self.assertEqual(resp.status_code, 200, resp.data)
        pbx = PbxServer.objects.get()
        self.assertEqual(pbx.status, PbxServer.STATUS_ERROR)
        self.assertIn('db down', pbx.notes)
        self.assertTrue(any('provisioning failed' in n for n in resp.data['result']['notes']))
        self.assertEqual(Trunk.objects.count(), 1)
        self.assertEqual(UserPhoneAssignment.objects.count(), 2)

    def test_second_server_with_another_fqdn_is_not_added(self):
        self.assertEqual(self._apply({'server': {'name': 'A', 'fqdn': 'a.example'}}).status_code, 200)
        resp = self._apply({'server': {'name': 'B', 'fqdn': 'b.example'}})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(PbxServer.objects.count(), 1)
        self.assertEqual(resp.provision_calls, 0)
        self.assertTrue(any('already registered' in n for n in resp.data['result']['notes']))

    def test_validation(self):
        resp = self._apply({'hours': {'after_hours_action': 'forward', 'forward_number': ''}})
        self.assertEqual(resp.status_code, 400)
        resp = self._apply({'hours': {'schedule': {'mon': {'from': '18:00', 'to': '09:00'}}}})
        self.assertEqual(resp.status_code, 400)
        resp = self._apply({'hours': {'timezone': 'Mars/Olympus'}})
        self.assertEqual(resp.status_code, 400)
        resp = self._apply({'agent_user_ids': [999999]})
        self.assertEqual(resp.status_code, 400)
        resp = self._apply({'trunk': {'sip_server': 'x'}})
        self.assertEqual(resp.status_code, 400)

    def test_context_and_prefill(self):
        step = next(s for s in self.api_get(ONB, user=self.owner).data['steps'] if s['key'] == 'ip_calling')
        self.assertEqual(step['order'], 70)
        ctx = step['context']
        self.assertIsNone(ctx['server'])
        self.assertEqual(ctx['trunks'], 0)
        self.assertEqual([u['email'] for u in ctx['users']], ['agent@test.com', 'giga@test.com', 'nino@test.com', 'owner@test.com'])
        self.assertFalse(ctx['hosted_request_sent'])
        self.assertEqual(ctx['agents'], [])
        pre = step['prefill']
        self.assertEqual(pre['hosting'], 'byo')
        self.assertIsNone(pre['server'])
        self.assertEqual(pre['queue']['strategy'], 'ringall')
        self.assertEqual(pre['hours']['schedule']['mon'], {'from': '09:00', 'to': '18:00', 'break_from': None, 'break_to': None})
        self.assertIsNone(pre['hours']['schedule']['sun'])

        self._apply(self.answers())
        step = next(s for s in self.api_get(ONB, user=self.owner).data['steps'] if s['key'] == 'ip_calling')
        ctx = step['context']
        pbx = PbxServer.objects.get()
        self.assertEqual(ctx['server']['fqdn'], 'pbx.acme.ge')
        self.assertEqual(ctx['server']['status'], 'provisioning')
        # Exactly what /settings/pbx/server shows (PbxServerSerializer.install_command).
        self.assertEqual(
            ctx['server']['install_command'],
            f'curl -sSL https://tenant.test.com/api/pbx/install/{pbx.enrollment_token}/ | sudo bash',
        )
        self.assertEqual(ctx['trunks'], 1)
        self.assertEqual(sorted(ctx['agents']), sorted([self.nino.id, self.giga.id]))
        pre = step['prefill']
        self.assertEqual(pre['server']['fqdn'], 'pbx.acme.ge')
        self.assertEqual(pre['trunk']['provider'], 'magti')
        self.assertEqual(pre['trunk']['password'], '')
        self.assertEqual(pre['trunk']['phone_numbers'], ['+995322421219', '+995322421220'])
        self.assertEqual(sorted(pre['agent_user_ids']), sorted([self.nino.id, self.giga.id]))
        self.assertEqual(pre['queue']['strategy'], 'rrmemory')
        self.assertEqual(pre['hours']['schedule']['mon'], {'from': '09:00', 'to': '18:00', 'break_from': '13:00', 'break_to': '14:00'})
        self.assertEqual(pre['hours']['after_hours_action'], 'forward')
        self.assertEqual(pre['rating'], {'review_method': 'sms', 'sms_api_key': None})

    def test_seed_defaults_is_a_noop(self):
        from crm.onboarding import CallsStep
        CallsStep().seed_defaults(self.tenant)
        self.assertFalse(SipConfiguration.objects.exists())
        self.assertFalse(PbxServer.objects.exists())
