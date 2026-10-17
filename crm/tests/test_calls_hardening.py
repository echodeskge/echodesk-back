"""
Calls hardening (parity package P3): ip_calling gate, PBX-admin writes,
write-only secrets, softphone credentials, call-log filters, stats
alignment and permissions, missed-call notifications.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from crm.models import (
    CallLog, CallEvent, InboundRoute, PbxServer, PbxSettings, Queue, QueueMember,
    SipConfiguration, Trunk, UserPhoneAssignment,
)
from crm.tests.conftest import CrmTestCase
from users.models import Notification


def _body(resp):
    return json.dumps(resp.data, default=str)


# ============================================================================
# ip_calling feature gate
# ============================================================================


class TestIpCallingGate(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='gate-admin@test.com')
        self.set_ip_calling(False)

    def test_endpoints_forbidden_without_feature(self):
        config = self.create_sip_config(created_by=self.admin, is_default=True)
        for url in (
            '/api/call-logs/',
            '/api/sip-configurations/',
            '/api/sip-configurations/my_config/',
            '/api/phone-assignments/',
            '/api/extensions/status/',
            f'/api/pbx-settings/{config.id}/',
            '/api/trunks/',
            '/api/call-stats/overview/',
        ):
            resp = self.api_get(url, user=self.admin)
            self.assertEqual(resp.status_code, 403, url)

    def test_feature_enables_access(self):
        self.set_ip_calling(True)
        resp = self.api_get('/api/call-logs/', user=self.admin)
        self.assertEqual(resp.status_code, 200)


# ============================================================================
# Call log filters
# ============================================================================


class TestCallLogFilters(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='flt-agent@test.com')
        self.other = self.create_user(email='flt-other@test.com')

    def _ids(self, query, user=None):
        resp = self.api_get(f'/api/call-logs/?page_size=100&{query}', user=user or self.agent)
        self.assertEqual(resp.status_code, 200, resp.data)
        return {row['id'] for row in self.get_results(resp)}

    def test_handled_by_and_mine(self):
        mine = self.create_call_log(handled_by=self.agent)
        to_me = self.create_call_log(handled_by=self.other, transferred_to_user=self.agent)
        theirs = self.create_call_log(handled_by=self.other)
        self.assertEqual(self._ids(f'handled_by={self.other.id}'), {to_me.id, theirs.id})
        self.assertEqual(self._ids('mine=true'), {mine.id, to_me.id})
        self.assertEqual(self._ids('mine=false'), {theirs.id})
        # Default stays the whole tenant log (web parity).
        self.assertEqual(self._ids(''), {mine.id, to_me.id, theirs.id})

    def test_started_after_and_before(self):
        old = self.create_call_log(handled_by=self.agent)
        new = self.create_call_log(handled_by=self.agent)
        CallLog.objects.filter(pk=old.pk).update(started_at=timezone.now() - timedelta(days=10))
        cutoff = (timezone.now() - timedelta(days=5)).date().isoformat()
        self.assertEqual(self._ids(f'started_after={cutoff}'), {new.id})
        self.assertEqual(self._ids(f'started_before={cutoff}'), {old.id})
        iso = (timezone.now() - timedelta(days=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.assertEqual(self._ids(f'started_after={iso}'), {new.id})

    def test_status_in_and_status(self):
        a = self.create_call_log(handled_by=self.agent, status='missed')
        b = self.create_call_log(handled_by=self.agent, status='no_answer')
        self.create_call_log(handled_by=self.agent, status='answered')
        self.assertEqual(self._ids('status__in=missed,no_answer'), {a.id, b.id})
        self.assertEqual(self._ids('status=missed'), {a.id})

    def test_missed_filter(self):
        missed = self.create_call_log(handled_by=self.agent, status='missed')
        legacy_unanswered = self.create_call_log(
            handled_by=self.agent, status='ended', duration=timedelta(0),
        )
        answered = self.create_call_log(
            handled_by=self.agent, status='ended', answered_at=timezone.now(),
            duration=timedelta(seconds=30),
        )
        outbound = self.create_call_log(handled_by=self.agent, status='ended', direction='outbound')
        self.assertEqual(self._ids('missed=true'), {missed.id, legacy_unanswered.id})
        self.assertEqual(self._ids('missed=false'), {answered.id, outbound.id})

    def test_client_filter_and_social_search(self):
        from social_integrations.models import Client as SocialClient
        crm_client = self.create_client(name='Acme')
        social = SocialClient.objects.create(name='Nino Social', phone='+995599000111')
        a = self.create_call_log(handled_by=self.agent, client=crm_client)
        b = self.create_call_log(handled_by=self.agent, social_client=social)
        self.assertEqual(self._ids(f'client={crm_client.id}'), {a.id})
        self.assertEqual(self._ids(f'social_client={social.id}'), {b.id})
        self.assertEqual(self._ids('search=Nino'), {b.id})


# ============================================================================
# Call log permissions + serializers
# ============================================================================


class TestCallLogPermissions(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='clp-admin@test.com')
        self.agent = self.create_user(email='clp-agent@test.com')

    def test_agent_cannot_delete_call_log(self):
        call = self.create_call_log(handled_by=self.agent)
        resp = self.api_delete(f'/api/call-logs/{call.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(CallLog.objects.filter(pk=call.pk).exists())

    def test_admin_can_delete_call_log(self):
        call = self.create_call_log(handled_by=self.agent)
        resp = self.api_delete(f'/api/call-logs/{call.id}/', user=self.admin)
        self.assertEqual(resp.status_code, 204)

    def test_settings_manager_can_delete_call_log(self):
        manager = self.create_user(email='clp-settings@test.com', can_manage_settings=True)
        call = self.create_call_log(handled_by=self.agent)
        resp = self.api_delete(f'/api/call-logs/{call.id}/', user=manager)
        self.assertEqual(resp.status_code, 204)

    def test_agent_can_patch_notes(self):
        call = self.create_call_log(handled_by=self.agent)
        resp = self.api_patch(f'/api/call-logs/{call.id}/', {'notes': 'called back'}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.notes, 'called back')

    def test_statistics_needs_stats_permission(self):
        resp = self.api_get('/api/call-logs/statistics/', user=self.agent)
        self.assertEqual(resp.status_code, 403)
        manager = self.create_manager(email='clp-manager@test.com')
        resp = self.api_get('/api/call-logs/statistics/', user=manager)
        self.assertEqual(resp.status_code, 200)

    def test_statistics_counts_unanswered_ended_as_missed(self):
        now = timezone.now()
        self.create_call_log(handled_by=self.agent, status='ended', answered_at=now,
                             duration=timedelta(seconds=60))
        self.create_call_log(handled_by=self.agent, status='ended', duration=timedelta(0))
        self.create_call_log(handled_by=self.agent, status='no_answer')
        resp = self.api_get('/api/call-logs/statistics/?period=today', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['total_calls'], 3)
        self.assertEqual(resp.data['answered_calls'], 1)
        self.assertEqual(resp.data['missed_calls'], 2)
        self.assertEqual(resp.data['average_duration_seconds'], 60)

    def test_send_sms_review_admin_only(self):
        call = self.create_call_log(handled_by=self.agent, status='ended')
        resp = self.api_post(f'/api/call-logs/{call.id}/send_sms_review/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 403)
        # Admin passes the permission check (then 400: no SIP config here).
        resp = self.api_post(f'/api/call-logs/{call.id}/send_sms_review/', {}, user=self.admin)
        self.assertEqual(resp.status_code, 400)

    def test_action_responses_include_admin_fields_for_admin(self):
        call = self.create_call_log(
            handled_by=self.agent, status='answered',
            recording_url='https://pbx.test/rec.wav', answered_at=timezone.now(),
        )
        resp = self.api_post(f'/api/call-logs/{call.id}/end_call/', {}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['recording_url'], 'https://pbx.test/rec.wav')
        call2 = self.create_call_log(
            handled_by=self.agent, status='answered',
            recording_url='https://pbx.test/rec2.wav', answered_at=timezone.now(),
        )
        resp = self.api_post(f'/api/call-logs/{call2.id}/end_call/', {}, user=self.agent)
        self.assertEqual(resp.data['recording_url'], '')

    def test_detail_has_social_client_and_transfer_fields(self):
        from social_integrations.models import Client as SocialClient
        social = SocialClient.objects.create(name='Social Caller', phone='+995599000222')
        call = self.create_call_log(handled_by=self.agent, social_client=social)
        resp = self.api_get(f'/api/call-logs/{call.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['client_name'], 'Social Caller')
        self.assertEqual(resp.data['social_client'], social.id)
        for key in ('transferred_to', 'transferred_to_user_name', 'parent_call',
                    'transfer_type', 'events', 'recording'):
            self.assertIn(key, resp.data)
        self.assertIsNone(resp.data['recording'])

    def test_add_event(self):
        call = self.create_call_log(handled_by=self.agent, status='answered')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/add_event/',
            {'event_type': 'muted', 'metadata': {'by': 'agent'}}, user=self.agent,
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(CallEvent.objects.filter(call_log=call, event_type='muted').exists())

    def test_end_call_refuses_transferred(self):
        call = self.create_call_log(handled_by=self.agent, status='transferred')
        resp = self.api_post(f'/api/call-logs/{call.id}/end_call/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 400)
        call.refresh_from_db()
        self.assertEqual(call.status, 'transferred')

    def test_consultation_marks_and_clears_attended(self):
        call = self.create_call_log(handled_by=self.agent, status='answered')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/initiate_consultation/',
            {'target_number': '101'}, user=self.agent,
        )
        self.assertEqual(resp.status_code, 201)
        call.refresh_from_db()
        self.assertEqual(call.status, 'on_hold')
        self.assertEqual(call.transfer_type, 'attended')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/cancel_consultation/',
            {'consultation_log_id': resp.data['consultation_log_id']}, user=self.agent,
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'answered')
        self.assertEqual(call.transfer_type, '')

    def test_client_call_history_is_bare_list_with_context(self):
        client = self.create_client(name='History Client')
        self.create_call_log(handled_by=self.agent, client=client,
                             recording_url='https://pbx.test/h.wav')
        resp = self.api_get(f'/api/clients/{client.id}/call_history/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.data, list)
        self.assertEqual(resp.data[0]['recording_url'], 'https://pbx.test/h.wav')


# ============================================================================
# Secrets are write-only
# ============================================================================


class TestWriteOnlySecrets(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='sec-admin@test.com')
        self.agent = self.create_user(email='sec-agent@test.com')
        self.config = self.create_sip_config(
            created_by=self.admin, is_default=True,
            password='TRUNK-SECRET', turn_password='TURN-SECRET', turn_username='turnuser',
        )
        self.assignment = self.create_phone_assignment(
            user=self.agent, sip_config=self.config, extension='100',
            extension_password='EXT-SECRET-100',
        )

    def test_phone_assignments_list_hides_passwords(self):
        resp = self.api_get('/api/phone-assignments/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('EXT-SECRET-100', _body(resp))
        row = self.get_results(resp)[0]
        self.assertEqual(row['extension_password'], '')
        self.assertTrue(row['has_extension_password'])

    def test_phone_assignment_detail_hides_passwords(self):
        resp = self.api_get(f'/api/phone-assignments/{self.assignment.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        body = _body(resp)
        for secret in ('EXT-SECRET-100', 'TRUNK-SECRET', 'TURN-SECRET'):
            self.assertNotIn(secret, body)

    def test_phone_assignments_is_active_filter(self):
        other = self.create_user(email='sec-inactive@test.com')
        self.create_phone_assignment(user=other, sip_config=self.config, extension='199',
                                     is_active=False, is_primary=False)
        resp = self.api_get('/api/phone-assignments/?is_active=true', user=self.agent)
        self.assertEqual({r['extension'] for r in self.get_results(resp)}, {'100'})

    def test_sip_configuration_detail_and_list_hide_secrets(self):
        for url in ('/api/sip-configurations/', f'/api/sip-configurations/{self.config.id}/'):
            resp = self.api_get(url, user=self.admin)
            self.assertEqual(resp.status_code, 200, url)
            body = _body(resp)
            for secret in ('EXT-SECRET-100', 'TRUNK-SECRET', 'TURN-SECRET'):
                self.assertNotIn(secret, body, url)
        resp = self.api_get(f'/api/sip-configurations/{self.config.id}/', user=self.admin)
        self.assertTrue(resp.data['has_password'])
        self.assertTrue(resp.data['has_turn_password'])
        # Shape kept for the web types, value masked.
        self.assertEqual(resp.data['password'], '')
        self.assertEqual(resp.data['turn_password'], '')
        self.assertEqual(resp.data['user_assignments'][0]['extension_password'], '')

    def test_sip_configuration_create_without_password(self):
        """The web settings form creates the config without a password."""
        resp = self.api_post('/api/sip-configurations/', {
            'name': 'No PW yet', 'sip_server': 'pbx.test.com', 'username': 'u',
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertFalse(resp.data['has_password'])

    def test_sip_configuration_list_assignment_count(self):
        resp = self.api_get('/api/sip-configurations/', user=self.admin)
        row = next(r for r in self.get_results(resp) if r['id'] == self.config.id)
        self.assertEqual(row['assignment_count'], 1)

    def test_sip_configuration_update_blank_password_keeps_secret(self):
        resp = self.api_put(f'/api/sip-configurations/{self.config.id}/', {
            'name': 'Renamed', 'sip_server': 'pbx.test.com', 'sip_port': 8089,
            'username': 'testuser', 'password': '', 'turn_server': '', 'turn_username': '',
        }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['password'], '')
        self.config.refresh_from_db()
        self.assertEqual(self.config.name, 'Renamed')
        self.assertEqual(self.config.password, 'TRUNK-SECRET')
        self.assertEqual(self.config.turn_password, 'TURN-SECRET')

    def test_sip_configuration_update_new_password(self):
        resp = self.api_patch(f'/api/sip-configurations/{self.config.id}/',
                              {'password': 'NEW-SECRET'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.config.refresh_from_db()
        self.assertEqual(self.config.password, 'NEW-SECRET')

    def test_phone_assignment_put_without_password_keeps_it(self):
        resp = self.api_put(f'/api/phone-assignments/{self.assignment.id}/', {
            'user': self.agent.id, 'sip_configuration': self.config.id,
            'extension': '100', 'phone_number': '+995322421219',
            'display_name': 'Renamed agent', 'is_primary': True, 'is_active': True,
        }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.display_name, 'Renamed agent')
        self.assertEqual(self.assignment.extension_password, 'EXT-SECRET-100')

    def test_phone_assignment_create_requires_password(self):
        user = self.create_user(email='sec-new@test.com')
        payload = {
            'user': user.id, 'sip_configuration': self.config.id, 'extension': '150',
            'phone_number': '+995322421219',
        }
        resp = self.api_post('/api/phone-assignments/', payload, user=self.admin)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('extension_password', resp.data)
        payload['extension_password'] = 'pw150'
        resp = self.api_post('/api/phone-assignments/', payload, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['extension_password'], '')

    def test_trunk_password_write_only(self):
        trunk = Trunk.objects.create(
            name='Magti', sip_server='89.150.1.11', username='u1', password='TRUNK-PW',
        )
        resp = self.api_get(f'/api/trunks/{trunk.id}/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('TRUNK-PW', _body(resp))
        self.assertTrue(resp.data['has_password'])
        resp = self.api_patch(f'/api/trunks/{trunk.id}/', {'provider': 'Magti', 'password': ''},
                              user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.data)
        trunk.refresh_from_db()
        self.assertEqual(trunk.password, 'TRUNK-PW')
        self.assertEqual(trunk.provider, 'Magti')


# ============================================================================
# Softphone credentials (my_config / webrtc_config)
# ============================================================================


class TestSoftphoneCredentials(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='sp-admin@test.com')
        self.agent = self.create_user(email='sp-agent@test.com')
        self.colleague = self.create_user(email='sp-colleague@test.com')
        self.config = self.create_sip_config(
            created_by=self.admin, is_default=True, username='shared',
            password='TRUNK-SECRET', turn_server='turn:t.test', turn_username='tu',
            turn_password='TURN-PW',
        )
        self.mine = self.create_phone_assignment(
            user=self.agent, sip_config=self.config, extension='100',
            extension_password='MY-EXT-PW',
        )
        self.create_phone_assignment(
            user=self.colleague, sip_config=self.config, extension='101',
            extension_password='COLLEAGUE-PW',
        )

    def test_my_config_returns_only_own_credentials(self):
        resp = self.api_get('/api/sip-configurations/my_config/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        data = resp.data
        self.assertEqual(data['id'], self.mine.id)
        self.assertEqual(data['extension'], '100')
        self.assertEqual(data['extension_password'], 'MY-EXT-PW')
        sip = data['sip_configuration']
        # Fields the web SipService reads.
        for key in ('sip_server', 'sip_port', 'realm', 'websocket_path', 'stun_server',
                    'turn_server', 'turn_username', 'turn_password'):
            self.assertIn(key, sip)
        self.assertEqual(sip['turn_password'], 'TURN-PW')
        self.assertEqual(sip['username'], '100')
        self.assertEqual(sip['password'], 'MY-EXT-PW')
        self.assertEqual(sip['user_assignments'], [])
        body = _body(resp)
        self.assertNotIn('COLLEAGUE-PW', body)
        self.assertNotIn('TRUNK-SECRET', body)

    def test_my_config_no_shared_account_for_agent_once_extensions_exist(self):
        loner = self.create_user(email='sp-loner@test.com')
        resp = self.api_get('/api/sip-configurations/my_config/', user=loner)
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('TRUNK-SECRET', _body(resp))
        resp = self.api_get(f'/api/sip-configurations/{self.config.id}/webrtc_config/', user=loner)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['username'], '')
        self.assertEqual(resp.data['password'], '')
        self.assertNotIn('TRUNK-SECRET', _body(resp))

    def test_my_config_fallback_for_pbx_admin(self):
        resp = self.api_get('/api/sip-configurations/my_config/', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data['id'])
        self.assertEqual(resp.data['extension'], 'shared')
        self.assertEqual(resp.data['extension_password'], 'TRUNK-SECRET')
        body = _body(resp)
        self.assertNotIn('COLLEAGUE-PW', body)
        self.assertNotIn('MY-EXT-PW', body)

    def test_my_config_no_shared_password_for_agent_even_without_extensions(self):
        # Legacy single-account tenant: no extensions at all. The shared
        # account may be the trunk login, so agents never get it; the
        # webrtc_config shape stays the same with sip_enabled false.
        UserPhoneAssignment.objects.all().delete()
        loner = self.create_user(email='sp-loner3@test.com')
        resp = self.api_get('/api/sip-configurations/my_config/', user=loner)
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('TRUNK-SECRET', _body(resp))
        resp = self.api_get(f'/api/sip-configurations/{self.config.id}/webrtc_config/', user=loner)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['password'], '')
        self.assertIs(resp.data['sip_enabled'], False)
        self.assertNotIn('TRUNK-SECRET', _body(resp))

    def test_my_config_404_without_any_config(self):
        SipConfiguration.objects.update(is_default=False)
        loner = self.create_user(email='sp-loner2@test.com')
        resp = self.api_get('/api/sip-configurations/my_config/', user=loner)
        self.assertEqual(resp.status_code, 404)

    def test_webrtc_config_uses_callers_extension(self):
        resp = self.api_get(f'/api/sip-configurations/{self.config.id}/webrtc_config/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['username'], '100')
        self.assertEqual(resp.data['password'], 'MY-EXT-PW')
        self.assertEqual(resp.data['user_assignments'], [])
        self.assertNotIn('COLLEAGUE-PW', _body(resp))

    def test_webrtc_config_non_default_without_extension_has_no_credentials(self):
        other = self.create_sip_config(created_by=self.admin, name='Other', password='OTHER-PW')
        resp = self.api_get(f'/api/sip-configurations/{other.id}/webrtc_config/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['username'], '')
        self.assertEqual(resp.data['password'], '')
        # A PBX admin still gets the shared account.
        resp = self.api_get(f'/api/sip-configurations/{other.id}/webrtc_config/', user=self.admin)
        self.assertEqual(resp.data['password'], 'OTHER-PW')


# ============================================================================
# PBX admin writes
# ============================================================================


class TestPbxAdminWrites(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='pw-admin@test.com')
        self.agent = self.create_user(email='pw-agent@test.com')
        self.settings_user = self.create_user(email='pw-settings@test.com', can_manage_settings=True)
        self.config = self.create_sip_config(created_by=self.admin, is_default=True)

    def test_agent_cannot_write_sip_configurations(self):
        resp = self.api_post('/api/sip-configurations/', {
            'name': 'New', 'sip_server': 's', 'username': 'u', 'password': 'p',
        }, user=self.agent)
        self.assertEqual(resp.status_code, 403)
        resp = self.api_patch(f'/api/sip-configurations/{self.config.id}/', {'name': 'x'}, user=self.agent)
        self.assertEqual(resp.status_code, 403)
        resp = self.api_delete(f'/api/sip-configurations/{self.config.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 403)
        resp = self.api_post(f'/api/sip-configurations/{self.config.id}/set_default/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 403)
        resp = self.api_post(f'/api/sip-configurations/{self.config.id}/test_connection/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 403)

    def test_agent_can_read_sip_configurations(self):
        self.assertEqual(self.api_get('/api/sip-configurations/', user=self.agent).status_code, 200)
        self.assertEqual(
            self.api_get(f'/api/sip-configurations/{self.config.id}/', user=self.agent).status_code, 200,
        )

    def test_settings_permission_can_write(self):
        resp = self.api_post('/api/sip-configurations/', {
            'name': 'New', 'sip_server': 's', 'username': 'u', 'password': 'p',
        }, user=self.settings_user)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['password'], '')

    def test_agent_cannot_write_phone_assignments(self):
        assignment = self.create_phone_assignment(user=self.agent, sip_config=self.config)
        resp = self.api_patch(f'/api/phone-assignments/{assignment.id}/', {'extension': '999'}, user=self.agent)
        self.assertEqual(resp.status_code, 403)
        resp = self.api_delete(f'/api/phone-assignments/{assignment.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 403)

    def test_trunks_queues_routes_read_open_write_admin(self):
        trunk = Trunk.objects.create(name='T1', sip_server='s', username='u', password='p')
        group = self.create_tenant_group(name='Support group')
        queue = Queue.objects.create(name='Support', slug='support', group=group)
        for url in ('/api/trunks/', f'/api/trunks/{trunk.id}/', '/api/queues/',
                    '/api/queue-members/', '/api/inbound-routes/'):
            self.assertEqual(self.api_get(url, user=self.agent).status_code, 200, url)
        self.assertEqual(self.api_patch(f'/api/trunks/{trunk.id}/', {'provider': 'x'},
                                        user=self.agent).status_code, 403)
        self.assertEqual(self.api_delete(f'/api/queues/{queue.id}/', user=self.agent).status_code, 403)
        self.assertEqual(self.api_post('/api/inbound-routes/', {
            'did': '+995322000000', 'destination_type': 'hangup',
        }, user=self.agent).status_code, 403)
        resp = self.api_post('/api/inbound-routes/', {
            'did': '+995322000000', 'destination_type': 'hangup',
        }, user=self.admin)
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_pbx_servers_admin_only(self):
        self.assertEqual(self.api_get('/api/pbx-servers/', user=self.agent).status_code, 403)
        self.assertEqual(self.api_get('/api/pbx-servers/', user=self.admin).status_code, 200)

    def test_queue_member_count(self):
        group = self.create_tenant_group(name='Count group')
        queue = Queue.objects.create(name='Sales', slug='sales', group=group)
        a1 = self.create_phone_assignment(user=self.agent, sip_config=self.config, extension='120')
        a2 = self.create_phone_assignment(sip_config=self.config, extension='121')
        QueueMember.objects.create(queue=queue, user_phone_assignment=a1, is_active=True)
        QueueMember.objects.create(queue=queue, user_phone_assignment=a2, is_active=False)
        resp = self.api_get('/api/queues/', user=self.agent)
        row = next(r for r in self.get_results(resp) if r['id'] == queue.id)
        self.assertEqual(row['member_count'], 1)
        resp = self.api_get(f'/api/queues/{queue.id}/', user=self.agent)
        self.assertEqual(resp.data['member_count'], 1)


# ============================================================================
# PBX settings endpoints
# ============================================================================


class TestPbxSettingsPermissions(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='ps-admin@test.com')
        self.agent = self.create_user(email='ps-agent@test.com')
        self.config = self.create_sip_config(created_by=self.admin)

    def test_agent_can_read(self):
        resp = self.api_get(f'/api/pbx-settings/{self.config.id}/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('sms_api_key', resp.data)

    def test_agent_cannot_write(self):
        url = f'/api/pbx-settings/{self.config.id}/'
        self.assertEqual(self.api_patch(url, {'forward_number': '123'}, user=self.agent).status_code, 403)
        self.assertEqual(self.api_post(url + 'upload-sound/', {'sound_type': 'greeting'},
                                       user=self.agent).status_code, 403)
        self.assertEqual(self.api_post(url + 'remove-sound/', {'sound_type': 'greeting'},
                                       user=self.agent).status_code, 403)
        self.assertFalse(PbxSettings.objects.filter(forward_number='123').exists())

    def test_admin_can_write(self):
        url = f'/api/pbx-settings/{self.config.id}/'
        resp = self.api_patch(url, {'forward_number': '555'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['forward_number'], '555')
        resp = self.api_post(url + 'remove-sound/', {'sound_type': 'greeting'}, user=self.admin)
        self.assertEqual(resp.status_code, 200)

    @patch('requests.get')
    def test_extension_status_shape(self, mock_get):
        mock_get.return_value.json.return_value = {'extensions': [{'extension': '100', 'status': 'online'}]}
        self.create_sip_config(created_by=self.admin, name='Default', is_default=True,
                               sip_server='pbx-status.test')
        resp = self.api_get('/api/extensions/status/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['extensions'][0]['extension'], '100')


# ============================================================================
# Call statistics
# ============================================================================


class TestCallStatsPermissions(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='st-admin@test.com')
        self.agent = self.create_user(email='st-agent@test.com')
        self.other = self.create_user(email='st-other@test.com')
        self.manager = self.create_manager(email='st-manager@test.com')

    def test_agent_forbidden_on_tenant_stats(self):
        for url in ('/api/call-stats/overview/', '/api/call-stats/users/',
                    '/api/call-stats/queues/?queue_id=1'):
            self.assertEqual(self.api_get(url, user=self.agent).status_code, 403, url)

    def test_manager_and_admin_allowed(self):
        for user in (self.manager, self.admin):
            for url in ('/api/call-stats/overview/', '/api/call-stats/users/'):
                self.assertEqual(self.api_get(url, user=user).status_code, 200, url)

    def test_timeline_own_only_for_agent(self):
        resp = self.api_get(f'/api/call-stats/users/{self.agent.id}/timeline/', user=self.agent)
        self.assertEqual(resp.status_code, 200)
        resp = self.api_get(f'/api/call-stats/users/{self.other.id}/timeline/', user=self.agent)
        self.assertEqual(resp.status_code, 403)
        resp = self.api_get(f'/api/call-stats/users/{self.other.id}/timeline/', user=self.manager)
        self.assertEqual(resp.status_code, 200)

    def test_overview_answered_missed_alignment(self):
        now = timezone.now()
        self.create_call_log(handled_by=self.agent, status='ended', answered_at=now,
                             duration=timedelta(seconds=40))
        self.create_call_log(handled_by=self.agent, status='ended', duration=timedelta(0))
        self.create_call_log(handled_by=self.agent, status='missed')
        resp = self.api_get('/api/call-stats/overview/?range=today', user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['answered_calls'], 1)
        self.assertEqual(resp.data['missed_calls'], 2)
        self.assertEqual(resp.data['avg_talk_seconds'], 40)
        resp = self.api_get('/api/call-stats/users/', user=self.admin)
        row = next(r for r in resp.data['results'] if r['user_id'] == self.agent.id)
        self.assertEqual(row['answered_count'], 1)
        self.assertEqual(row['missed_count'], 2)


# ============================================================================
# Missed-call notifications
# ============================================================================


@patch('users.notification_utils._dispatch_push')
class TestMissedCallNotifications(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='mc-admin@test.com')
        self.agent = self.create_user(email='mc-agent@test.com')
        Notification.objects.filter(notification_type='call_missed').delete()
        # The test transaction never commits: run on_commit callbacks at
        # once (TestMissedCallOnCommit covers the deferral itself).
        on_commit = patch('django.db.transaction.on_commit',
                          side_effect=lambda fn, using=None, robust=False: fn())
        on_commit.start()
        self.addCleanup(on_commit.stop)

    def _notifs(self, user=None):
        qs = Notification.objects.filter(notification_type='call_missed')
        if user is not None:
            qs = qs.filter(user=user)
        return qs

    def test_end_call_unanswered_inbound_becomes_missed_and_notifies(self, mock_push):
        call = self.create_call_log(handled_by=self.agent, status='ringing', direction='inbound')
        resp = self.api_post(f'/api/call-logs/{call.id}/end_call/', {}, user=self.agent)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'missed')
        notifs = self._notifs()
        self.assertEqual(notifs.count(), 1)
        n = notifs.first()
        self.assertEqual(n.user, self.agent)
        self.assertEqual(n.link_url, f'/calls?id={call.id}')
        self.assertEqual(n.metadata['call_id'], call.id)
        self.assertEqual(n.metadata['call_log_id'], call.id)
        self.assertEqual(n.metadata['call_uuid'], str(call.call_id))
        self.assertEqual(n.metadata['caller_number'], call.caller_number)
        # Push data carries the call id so a tap can open the call.
        self.assertTrue(mock_push.called)
        self.assertEqual(mock_push.call_args.kwargs['data']['call_log_id'], call.id)

    def test_end_call_answered_elsewhere_is_cancelled_without_notification(self, mock_push):
        colleague = self.create_user(email='mc-colleague@test.com')
        self.create_call_log(
            handled_by=colleague, status='answered', direction='inbound',
            caller_number='+995555777888', answered_at=timezone.now(),
        )
        mine = self.create_call_log(
            handled_by=self.agent, status='ringing', direction='inbound',
            caller_number='555777888',
        )
        resp = self.api_post(f'/api/call-logs/{mine.id}/end_call/', {}, user=self.agent)
        self.assertEqual(resp.data['status'], 'cancelled')
        self.assertFalse(self._notifs().exists())
        self.assertTrue(mine.events.filter(metadata__outcome='answered_elsewhere').exists())

    def test_late_answer_elsewhere_relabels_missed_leg(self, mock_push):
        # The colleague's "answered" arrives after our leg already went
        # missed; the (delayed) notification task re-checks and relabels.
        mine = self.create_call_log(handled_by=self.agent, status='ringing',
                                    caller_number='+995555123123')
        self.create_call_log(handled_by=self.admin, status='answered',
                             caller_number='+995555123123', answered_at=timezone.now())
        from crm.call_notifications import send_missed_call_notifications
        CallLog.objects.filter(pk=mine.pk).update(status='missed')
        self.assertEqual(send_missed_call_notifications(mine.pk), 0)
        mine.refresh_from_db()
        self.assertEqual(mine.status, 'cancelled')
        self.assertFalse(self._notifs().exists())

    def test_answered_call_ends_normally(self, mock_push):
        call = self.create_call_log(handled_by=self.agent, status='answered',
                                    answered_at=timezone.now() - timedelta(seconds=30))
        resp = self.api_post(f'/api/call-logs/{call.id}/end_call/', {}, user=self.agent)
        self.assertEqual(resp.data['status'], 'ended')
        self.assertFalse(self._notifs().exists())

    def test_outbound_unanswered_is_not_missed(self, mock_push):
        call = self.create_call_log(handled_by=self.agent, status='ringing', direction='outbound')
        resp = self.api_post(f'/api/call-logs/{call.id}/end_call/', {}, user=self.agent)
        self.assertEqual(resp.data['status'], 'ended')
        self.assertFalse(self._notifs().exists())

    def test_fires_once_per_transition(self, mock_push):
        call = self.create_call_log(handled_by=self.agent, status='ringing')
        call.status = 'missed'
        call.save()
        self.assertEqual(self._notifs().count(), 1)
        # Later saves of the same row do not notify again.
        call.notes = 'called back'
        call.save()
        call.recording_url = 'https://pbx.test/x.wav'
        call.save(update_fields=['recording_url'])
        Notification.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        call.save()
        self.assertEqual(self._notifs().count(), 1)
        # missed -> no_answer is not a new transition into "missed".
        call.status = 'no_answer'
        call.save()
        self.assertEqual(self._notifs().count(), 1)

    def test_sip_webhook_no_answer_notifies(self, mock_push):
        call = self.create_call_log(handled_by=self.agent, status='ringing', sip_call_id='abc-123')
        resp = self.client.post('/api/webhooks/sip/', {
            'event_type': 'call_no_answer', 'sip_call_id': 'abc-123',
        }, format='json', HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'no_answer')
        self.assertEqual(self._notifs(self.agent).count(), 1)

    def test_answered_status_does_not_notify(self, mock_push):
        self.create_call_log(handled_by=self.agent, status='answered')
        self.assertFalse(self._notifs().exists())

    def test_two_missed_calls_are_two_notifications(self, mock_push):
        self.create_call_log(handled_by=self.agent, status='missed', caller_number='+995555000001')
        self.create_call_log(handled_by=self.agent, status='missed', caller_number='+995555000002')
        self.assertEqual(self._notifs(self.agent).count(), 2)

    def test_unhandled_call_notifies_queue_members(self, mock_push):
        config = self.create_sip_config(created_by=self.admin)
        member_user = self.create_user(email='mc-member@test.com')
        inactive_member = self.create_user(email='mc-inactive-member@test.com')
        a1 = self.create_phone_assignment(user=member_user, sip_config=config, extension='130')
        a2 = self.create_phone_assignment(user=inactive_member, sip_config=config, extension='131')
        group = self.create_tenant_group(name='MC Queue group')
        queue = Queue.objects.create(name='MC', slug='mc', group=group)
        QueueMember.objects.create(queue=queue, user_phone_assignment=a1, is_active=True)
        QueueMember.objects.create(queue=queue, user_phone_assignment=a2, is_active=False)
        InboundRoute.objects.create(did='+995322421219', destination_type='queue',
                                    destination_queue=queue)
        CallLog.objects.create(caller_number='+995555444333', recipient_number='+995322421219',
                               direction='inbound', status='no_answer', handled_by=None)
        self.assertEqual(set(self._notifs().values_list('user_id', flat=True)), {member_user.id})

    def test_unhandled_call_notifies_extension_owner(self, mock_push):
        config = self.create_sip_config(created_by=self.admin)
        owner = self.create_user(email='mc-owner@test.com')
        ext = self.create_phone_assignment(user=owner, sip_config=config, extension='140')
        InboundRoute.objects.create(did='+995322111222', destination_type='extension',
                                    destination_extension=ext)
        CallLog.objects.create(caller_number='+995555444000', recipient_number='995322111222',
                               direction='inbound', status='missed', handled_by=None)
        self.assertEqual(set(self._notifs().values_list('user_id', flat=True)), {owner.id})

    def test_unresolvable_call_notifies_staff_only(self, mock_push):
        CallLog.objects.create(caller_number='+995555444111', recipient_number='+995000000000',
                               direction='inbound', status='missed', handled_by=None)
        notified = set(self._notifs().values_list('user_id', flat=True))
        self.assertIn(self.admin.id, notified)
        self.assertNotIn(self.agent.id, notified)
