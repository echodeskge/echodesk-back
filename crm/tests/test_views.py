"""Tests for CRM views: SipConfiguration, UserPhoneAssignment, CallLog, PBX settings, and webhooks."""
from datetime import timedelta
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from crm.models import (
    SipConfiguration, UserPhoneAssignment, CallLog, CallEvent,
    CallRecording, PbxSettings, Client,
)
from crm.tests.conftest import CrmTestCase


# ============================================================================
# SipConfigurationViewSet
# ============================================================================


class TestSipConfigurationList(CrmTestCase):

    def test_list_sip_configs(self):
        admin = self.create_admin()
        self.create_sip_config(name='Config A', created_by=admin)
        self.create_sip_config(
            name='Config B', created_by=admin,
            username='u2', sip_server='s2.test.com',
        )
        resp = self.api_get('/api/sip-configurations/', user=admin)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 2)

    def test_list_unauthenticated(self):
        resp = self.api_get('/api/sip-configurations/')
        self.assertIn(resp.status_code, [401, 403])


class TestSipConfigurationCreate(CrmTestCase):

    def test_create_sip_config(self):
        admin = self.create_admin()
        data = {
            'name': 'New SIP',
            'sip_server': 'sip.new.com',
            'sip_port': 5060,
            'username': 'newuser',
            'password': 'newpass',
            'phone_number': '+995322421220',
        }
        resp = self.api_post('/api/sip-configurations/', data, user=admin)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['name'], 'New SIP')

    def test_create_sets_created_by(self):
        admin = self.create_admin()
        data = {
            'name': 'Auto Creator',
            'sip_server': 'sip.auto.com',
            'username': 'auto',
            'password': 'autopass',
        }
        resp = self.api_post('/api/sip-configurations/', data, user=admin)
        self.assertEqual(resp.status_code, 201)
        config = SipConfiguration.objects.get(id=resp.data['id'])
        self.assertEqual(config.created_by, admin)


class TestSipConfigurationRetrieveUpdateDelete(CrmTestCase):

    def test_retrieve(self):
        admin = self.create_admin()
        config = self.create_sip_config(created_by=admin)
        resp = self.api_get(f'/api/sip-configurations/{config.id}/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['name'], config.name)

    def test_update(self):
        admin = self.create_admin()
        config = self.create_sip_config(created_by=admin)
        resp = self.api_patch(
            f'/api/sip-configurations/{config.id}/',
            {'name': 'Updated SIP'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        config.refresh_from_db()
        self.assertEqual(config.name, 'Updated SIP')

    def test_delete(self):
        admin = self.create_admin()
        config = self.create_sip_config(created_by=admin)
        resp = self.api_delete(f'/api/sip-configurations/{config.id}/', user=admin)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(SipConfiguration.objects.filter(id=config.id).exists())


class TestSipConfigurationActions(CrmTestCase):

    def test_set_default(self):
        admin = self.create_admin()
        c1 = self.create_sip_config(name='C1', is_default=True, created_by=admin)
        c2 = self.create_sip_config(
            name='C2', created_by=admin,
            username='u2', sip_server='s2.test.com',
        )
        resp = self.api_post(
            f'/api/sip-configurations/{c2.id}/set_default/', user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertFalse(c1.is_default)
        self.assertTrue(c2.is_default)

    def test_test_connection_success(self):
        admin = self.create_admin()
        config = self.create_sip_config(created_by=admin)
        resp = self.api_post(
            f'/api/sip-configurations/{config.id}/test_connection/', user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('successful', resp.data['message'])

    def test_test_connection_incomplete(self):
        admin = self.create_admin()
        config = self.create_sip_config(
            created_by=admin, sip_server='', username='', password='',
        )
        resp = self.api_post(
            f'/api/sip-configurations/{config.id}/test_connection/', user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_my_config_with_assignment(self):
        admin = self.create_admin()
        sip_config = self.create_sip_config(created_by=admin, is_default=True)
        assignment = self.create_phone_assignment(
            user=admin, sip_config=sip_config, extension='100',
        )
        resp = self.api_get('/api/sip-configurations/my_config/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['extension'], '100')

    def test_my_config_fallback_to_default(self):
        admin = self.create_admin()
        self.create_sip_config(created_by=admin, is_default=True)
        resp = self.api_get('/api/sip-configurations/my_config/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data['id'])

    def test_my_config_no_config(self):
        admin = self.create_admin()
        resp = self.api_get('/api/sip-configurations/my_config/', user=admin)
        self.assertEqual(resp.status_code, 404)

    def test_webrtc_config(self):
        admin = self.create_admin()
        config = self.create_sip_config(created_by=admin, is_active=True)
        resp = self.api_get(
            f'/api/sip-configurations/{config.id}/webrtc_config/', user=admin,
        )
        self.assertEqual(resp.status_code, 200)

    def test_webrtc_config_inactive(self):
        admin = self.create_admin()
        config = self.create_sip_config(created_by=admin, is_active=False)
        resp = self.api_get(
            f'/api/sip-configurations/{config.id}/webrtc_config/', user=admin,
        )
        self.assertEqual(resp.status_code, 400)


# ============================================================================
# UserPhoneAssignmentViewSet
# ============================================================================


class TestUserPhoneAssignmentCRUD(CrmTestCase):

    def test_list_assignments(self):
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        self.create_phone_assignment(user=admin, sip_config=sip, extension='100')
        resp = self.api_get('/api/phone-assignments/', user=admin)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 1)

    def test_create_assignment(self):
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        user = self.create_user(email='newagent@test.com')
        data = {
            'user': user.id,
            'sip_configuration': sip.id,
            'extension': '200',
            'extension_password': 'pass200',
            'phone_number': '+995322421220',
            'is_primary': True,
        }
        resp = self.api_post('/api/phone-assignments/', data, user=admin)
        self.assertEqual(resp.status_code, 201)

    def test_update_assignment(self):
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        assignment = self.create_phone_assignment(
            user=admin, sip_config=sip, extension='100',
        )
        resp = self.api_patch(
            f'/api/phone-assignments/{assignment.id}/',
            {'display_name': 'Updated Name'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)

    def test_delete_assignment(self):
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        assignment = self.create_phone_assignment(
            user=admin, sip_config=sip, extension='100',
        )
        resp = self.api_delete(
            f'/api/phone-assignments/{assignment.id}/', user=admin,
        )
        self.assertEqual(resp.status_code, 204)


# ============================================================================
# CallLogViewSet
# ============================================================================


class TestCallLogList(CrmTestCase):

    def test_list_calls(self):
        admin = self.create_admin()
        self.create_call_log(handled_by=admin)
        self.create_call_log(handled_by=admin, direction='outbound')
        resp = self.api_get('/api/call-logs/', user=admin)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 2)

    def test_filter_by_direction(self):
        admin = self.create_admin()
        self.create_call_log(handled_by=admin, direction='inbound')
        self.create_call_log(handled_by=admin, direction='outbound')
        resp = self.api_get('/api/call-logs/?direction=inbound', user=admin)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 1)

    def test_filter_by_status(self):
        admin = self.create_admin()
        self.create_call_log(handled_by=admin, status='ringing')
        self.create_call_log(handled_by=admin, status='answered')
        resp = self.api_get('/api/call-logs/?status=answered', user=admin)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 1)

    def test_search_by_caller_number(self):
        admin = self.create_admin()
        self.create_call_log(handled_by=admin, caller_number='+995555111111')
        self.create_call_log(handled_by=admin, caller_number='+995555999999')
        resp = self.api_get('/api/call-logs/?search=111111', user=admin)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 1)

    def test_list_unauthenticated(self):
        resp = self.api_get('/api/call-logs/')
        self.assertIn(resp.status_code, [401, 403])


class TestCallLogCreate(CrmTestCase):

    def test_create_call_log(self):
        admin = self.create_admin()
        data = {
            'caller_number': '+995555111111',
            'recipient_number': '+995555222222',
            'direction': 'inbound',
        }
        resp = self.api_post('/api/call-logs/', data, user=admin)
        self.assertEqual(resp.status_code, 201)
        self.assertIn('caller_number', resp.data)


class TestCallLogInitiateCall(CrmTestCase):

    def test_initiate_outbound_call(self):
        admin = self.create_admin()
        sip = self.create_sip_config(
            created_by=admin, is_default=True, is_active=True,
        )
        resp = self.api_post(
            '/api/call-logs/initiate_call/',
            {'recipient_number': '+995555333333'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['direction'], 'outbound')
        self.assertEqual(resp.data['status'], 'initiated')

    def test_initiate_call_no_sip_config(self):
        admin = self.create_admin()
        resp = self.api_post(
            '/api/call-logs/initiate_call/',
            {'recipient_number': '+995555333333'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_initiate_call_with_specific_sip_config(self):
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin, is_active=True)
        resp = self.api_post(
            '/api/call-logs/initiate_call/',
            {'recipient_number': '+995555333333', 'sip_configuration': sip.id},
            user=admin,
        )
        self.assertEqual(resp.status_code, 201)

    def test_initiate_call_invalid_sip_config(self):
        admin = self.create_admin()
        resp = self.api_post(
            '/api/call-logs/initiate_call/',
            {'recipient_number': '+995555333333', 'sip_configuration': 9999},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_initiate_call_invalid_phone(self):
        admin = self.create_admin()
        self.create_sip_config(created_by=admin, is_default=True)
        resp = self.api_post(
            '/api/call-logs/initiate_call/',
            {'recipient_number': '123'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_initiate_call_uses_assignment_caller_number(self):
        admin = self.create_admin()
        sip = self.create_sip_config(
            created_by=admin, is_default=True, is_active=True,
        )
        self.create_phone_assignment(
            user=admin, sip_config=sip, extension='100',
            phone_number='+995322421219',
        )
        resp = self.api_post(
            '/api/call-logs/initiate_call/',
            {'recipient_number': '+995555333333'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['caller_number'], '+995322421219')


class TestCallLogUpdateStatus(CrmTestCase):

    def test_update_status_to_answered(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='ringing')
        resp = self.api_patch(
            f'/api/call-logs/{call.id}/update_status/',
            {'status': 'answered'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'answered')
        self.assertIsNotNone(call.answered_at)

    def test_update_status_to_ended_calculates_duration(self):
        admin = self.create_admin()
        call = self.create_call_log(
            handled_by=admin, status='answered',
            answered_at=timezone.now() - timedelta(minutes=5),
        )
        resp = self.api_patch(
            f'/api/call-logs/{call.id}/update_status/',
            {'status': 'ended'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'ended')
        self.assertIsNotNone(call.duration)
        self.assertGreater(call.duration.total_seconds(), 0)

    def test_update_status_creates_event(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin)
        self.api_patch(
            f'/api/call-logs/{call.id}/update_status/',
            {'status': 'answered'},
            user=admin,
        )
        self.assertTrue(CallEvent.objects.filter(call_log=call).exists())

    def test_update_status_invalid(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin)
        resp = self.api_patch(
            f'/api/call-logs/{call.id}/update_status/',
            {'status': 'nonexistent'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)


class TestCallLogEndCall(CrmTestCase):

    def test_end_call(self):
        admin = self.create_admin()
        call = self.create_call_log(
            handled_by=admin, status='answered',
            answered_at=timezone.now() - timedelta(minutes=3),
        )
        resp = self.api_post(
            f'/api/call-logs/{call.id}/end_call/', user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'ended')
        self.assertIsNotNone(call.ended_at)
        self.assertIsNotNone(call.duration)

    def test_end_already_ended_call(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='ended')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/end_call/', user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_end_unanswered_call(self):
        """Ending a call that was never answered should set zero duration."""
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='ringing')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/end_call/', user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.duration.total_seconds(), 0)


class TestCallLogStatistics(CrmTestCase):

    def test_statistics_today(self):
        admin = self.create_admin()
        self.create_call_log(handled_by=admin, status='answered', direction='inbound')
        self.create_call_log(handled_by=admin, status='missed', direction='inbound')
        self.create_call_log(handled_by=admin, status='answered', direction='outbound')
        resp = self.api_get('/api/call-logs/statistics/?period=today', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['total_calls'], 3)
        self.assertEqual(resp.data['answered_calls'], 2)
        self.assertEqual(resp.data['missed_calls'], 1)
        self.assertEqual(resp.data['inbound_calls'], 2)
        self.assertEqual(resp.data['outbound_calls'], 1)

    def test_statistics_week(self):
        admin = self.create_admin()
        resp = self.api_get('/api/call-logs/statistics/?period=week', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('total_calls', resp.data)

    def test_statistics_month(self):
        admin = self.create_admin()
        resp = self.api_get('/api/call-logs/statistics/?period=month', user=admin)
        self.assertEqual(resp.status_code, 200)


class TestCallLogRecording(CrmTestCase):

    def test_start_recording(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='answered')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/start_recording/', user=admin,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(CallRecording.objects.filter(call_log=call).exists())

    def test_start_recording_not_answered(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='ringing')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/start_recording/', user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_start_recording_already_exists(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='answered')
        self.create_call_recording(call, status='recording')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/start_recording/', user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_stop_recording(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='recording')
        self.create_call_recording(
            call, status='recording', started_at=timezone.now() - timedelta(minutes=2),
        )
        resp = self.api_post(
            f'/api/call-logs/{call.id}/stop_recording/', user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        recording = CallRecording.objects.get(call_log=call)
        self.assertEqual(recording.status, 'processing')

    def test_stop_recording_restores_the_pre_recording_status(self):
        """Recording a held call and stopping it must not resume the call
        on the backend: the status goes back to what start_recording saw."""
        admin = self.create_admin()
        for before in ('on_hold', 'answered'):
            call = self.create_call_log(handled_by=admin, status=before)
            resp = self.api_post(f'/api/call-logs/{call.id}/start_recording/', user=admin)
            self.assertEqual(resp.status_code, 201, resp.data)
            call.refresh_from_db()
            self.assertEqual(call.status, 'recording')
            resp = self.api_post(f'/api/call-logs/{call.id}/stop_recording/', user=admin)
            self.assertEqual(resp.status_code, 200, resp.data)
            call.refresh_from_db()
            self.assertEqual(call.status, before)

    def test_stop_recording_keeps_a_final_status(self):
        """A call that ended while it was recording is not revived."""
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='ended')
        self.create_call_recording(call, status='recording', started_at=timezone.now())
        resp = self.api_post(f'/api/call-logs/{call.id}/stop_recording/', user=admin)
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'ended')

    def test_stop_recording_no_recording(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='answered')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/stop_recording/', user=admin,
        )
        self.assertEqual(resp.status_code, 400)


class TestCallLogTransfer(CrmTestCase):

    def test_transfer_call(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='answered')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/transfer_call/',
            {'transfer_to': '+995555999999'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'transferred')

    def test_transfer_call_no_number(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='answered')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/transfer_call/', {}, user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_transfer_call_not_active(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='ringing')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/transfer_call/',
            {'transfer_to': '+995555999999'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)


class TestCallLogToggleHold(CrmTestCase):

    def test_put_on_hold(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='answered')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/toggle_hold/', user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'on_hold')

    def test_resume_from_hold(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='on_hold')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/toggle_hold/', user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'answered')

    def test_toggle_hold_invalid_status(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='ringing')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/toggle_hold/', user=admin,
        )
        self.assertEqual(resp.status_code, 400)


# ============================================================================
# PBX Settings Endpoints
# ============================================================================


class TestPbxSettingsEndpoints(CrmTestCase):

    def test_get_pbx_settings(self):
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        resp = self.api_get(f'/api/pbx-settings/{sip.id}/', user=admin)
        self.assertEqual(resp.status_code, 200)
        # Should auto-create PbxSettings if not exists
        self.assertTrue(PbxSettings.objects.filter(sip_configuration=sip).exists())

    def test_patch_pbx_settings(self):
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        self.create_pbx_settings(sip_config=sip)
        resp = self.api_patch(
            f'/api/pbx-settings/{sip.id}/',
            {
                'working_hours_enabled': True,
                'working_hours_schedule': {
                    'monday': [9, 10, 11, 12, 13, 14, 15, 16, 17],
                },
            },
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        settings = PbxSettings.objects.get(sip_configuration=sip)
        self.assertTrue(settings.working_hours_enabled)

    def test_get_pbx_settings_invalid_sip_config(self):
        admin = self.create_admin()
        resp = self.api_get('/api/pbx-settings/9999/', user=admin)
        self.assertEqual(resp.status_code, 404)

    def test_upload_sound_invalid_type(self):
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        self.create_pbx_settings(sip_config=sip)
        audio_file = SimpleUploadedFile('greeting.wav', b'fake-wav', content_type='audio/wav')
        client = self.authenticated_client(admin)
        resp = client.post(
            f'/api/pbx-settings/{sip.id}/upload-sound/',
            {'sound_type': 'invalid_type', 'file': audio_file},
            format='multipart',
            HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_sound_no_file(self):
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        self.create_pbx_settings(sip_config=sip)
        client = self.authenticated_client(admin)
        resp = client.post(
            f'/api/pbx-settings/{sip.id}/upload-sound/',
            {'sound_type': 'greeting'},
            format='multipart',
            HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_sound_wrong_extension(self):
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        self.create_pbx_settings(sip_config=sip)
        bad_file = SimpleUploadedFile('greeting.txt', b'not-audio', content_type='text/plain')
        client = self.authenticated_client(admin)
        resp = client.post(
            f'/api/pbx-settings/{sip.id}/upload-sound/',
            {'sound_type': 'greeting', 'file': bad_file},
            format='multipart',
            HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, 400)

    def test_remove_sound(self):
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        self.create_pbx_settings(sip_config=sip)
        resp = self.api_post(
            f'/api/pbx-settings/{sip.id}/remove-sound/',
            {'sound_type': 'greeting'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)

    def test_remove_sound_invalid_type(self):
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        self.create_pbx_settings(sip_config=sip)
        resp = self.api_post(
            f'/api/pbx-settings/{sip.id}/remove-sound/',
            {'sound_type': 'bad_type'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)


# ============================================================================
# Webhook Endpoints
# ============================================================================


class TestSipWebhook(CrmTestCase):

    def test_webhook_update_existing_call(self):
        admin = self.create_admin()
        call = self.create_call_log(
            handled_by=admin, sip_call_id='sip-123', status='ringing',
        )
        resp = self.api_post(
            '/api/webhooks/sip/',
            {
                'event_type': 'call_answered',
                'sip_call_id': 'sip-123',
            },
            **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'answered')
        self.assertIsNotNone(call.answered_at)

    def test_webhook_create_new_call(self):
        sip = self.create_sip_config(is_default=True)
        resp = self.api_post(
            '/api/webhooks/sip/',
            {
                'event_type': 'call_ringing',
                'sip_call_id': 'sip-new-456',
                'caller_number': '+995555111111',
                'recipient_number': '+995555222222',
            },
            **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(CallLog.objects.filter(sip_call_id='sip-new-456').exists())

    def test_webhook_end_call(self):
        admin = self.create_admin()
        call = self.create_call_log(
            handled_by=admin, sip_call_id='sip-end-789', status='answered',
            answered_at=timezone.now() - timedelta(minutes=5),
        )
        resp = self.api_post(
            '/api/webhooks/sip/',
            {
                'event_type': 'call_ended',
                'sip_call_id': 'sip-end-789',
            },
            **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'ended')
        self.assertIsNotNone(call.duration)

    def test_webhook_missing_fields(self):
        resp = self.api_post('/api/webhooks/sip/', {'event_type': 'call_answered'}, **self.pbx_headers())
        self.assertEqual(resp.status_code, 400)

    def test_webhook_call_not_found(self):
        resp = self.api_post(
            '/api/webhooks/sip/',
            {
                'event_type': 'call_ended',
                'sip_call_id': 'nonexistent-id',
            },
            **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 404)


class TestRecordingWebhook(CrmTestCase):

    def test_recording_started(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin)
        resp = self.api_post(
            '/api/webhooks/recording/',
            {
                'call_id': str(call.call_id),
                'status': 'started',
            },
            **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        recording = CallRecording.objects.get(call_log=call)
        self.assertIsNotNone(recording.started_at)

    def test_recording_completed(self):
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin)
        headers = self.pbx_headers()  # creates the tenant's PbxServer (fqdn pbx.tenant.test)
        file_url = 'https://pbx.tenant.test/recordings/recording.wav'
        resp = self.api_post(
            '/api/webhooks/recording/',
            {
                'call_id': str(call.call_id),
                'status': 'completed',
                'file_url': file_url,
                'file_size': 1024000,
                'duration': 120,
            },
            **headers,
        )
        self.assertEqual(resp.status_code, 200)
        recording = CallRecording.objects.get(call_log=call)
        self.assertEqual(recording.status, 'completed')
        self.assertEqual(recording.file_url, file_url)
        call.refresh_from_db()
        self.assertEqual(call.recording_url, file_url)

    def test_recording_file_url_must_be_on_a_pbx_host(self):
        """``file_url`` is mirrored onto CallLog.recording_url, which the
        play button fetches server-side: a URL off the tenant's PBX hosts
        is refused and nothing is written (same rule as the
        recording-url webhook)."""
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin)
        headers = self.pbx_headers()
        for bad in ('https://storage.example.com/recording.wav',
                    'http://169.254.169.254/latest/meta-data/',
                    'file:///etc/passwd'):
            resp = self.api_post(
                '/api/webhooks/recording/',
                {'call_id': str(call.call_id), 'status': 'completed', 'file_url': bad},
                **headers,
            )
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIn('file_url', resp.data)
        self.assertFalse(CallRecording.objects.filter(call_log=call).exists())
        call.refresh_from_db()
        self.assertFalse(call.recording_url)

    def test_recording_webhook_missing_fields(self):
        resp = self.api_post('/api/webhooks/recording/', {'status': 'started'}, **self.pbx_headers())
        self.assertEqual(resp.status_code, 400)

    def test_recording_webhook_call_not_found(self):
        import uuid
        resp = self.api_post(
            '/api/webhooks/recording/',
            {
                'call_id': str(uuid.uuid4()),
                'status': 'started',
            },
            **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 404)


class TestCallRatingWebhook(CrmTestCase):

    def test_rate_call(self):
        admin = self.create_admin()
        call = self.create_call_log(
            handled_by=admin,
            direction='inbound',
            caller_number='+995555123456',
        )
        resp = self.api_post(
            '/api/webhooks/call-rating/',
            {'caller_number': '+995555123456', 'rating': 5},
            **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.call_quality_score, 5.0)

    def test_rate_call_missing_fields(self):
        resp = self.api_post('/api/webhooks/call-rating/', {'caller_number': '+99555'}, **self.pbx_headers())
        self.assertEqual(resp.status_code, 400)

    def test_rate_call_invalid_rating(self):
        resp = self.api_post(
            '/api/webhooks/call-rating/',
            {'caller_number': '+995555123456', 'rating': 10},
            **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_rate_call_no_matching_call(self):
        resp = self.api_post(
            '/api/webhooks/call-rating/',
            {'caller_number': '+999999999999', 'rating': 3},
            **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 404)


class TestCallRecordingUrlWebhook(CrmTestCase):

    def test_save_recording_url(self):
        admin = self.create_admin()
        call = self.create_call_log(
            handled_by=admin, caller_number='+995555123456',
        )
        resp = self.api_post(
            '/api/webhooks/call-recording-url/',
            {
                'caller_number': '+995555123456',
                'recording_url': 'https://pbx.tenant.test/rec.wav',
            },
            **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.recording_url, 'https://pbx.tenant.test/rec.wav')

    def test_save_recording_url_missing_fields(self):
        resp = self.api_post(
            '/api/webhooks/call-recording-url/',
            {'caller_number': '+995555123456'},
            **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_save_recording_url_no_call_found(self):
        resp = self.api_post(
            '/api/webhooks/call-recording-url/',
            {
                'caller_number': '+999999999999',
                'recording_url': 'https://pbx.tenant.test/rec.wav',
            },
            **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 404)


# ============================================================================
# Client ViewSet
# ============================================================================


class TestClientViewSet(CrmTestCase):

    def test_list_clients(self):
        admin = self.create_admin()
        self.create_client(name='Client A')
        self.create_client(name='Client B')
        resp = self.api_get('/api/clients/', user=admin)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 2)

    def test_create_client(self):
        admin = self.create_admin()
        data = {
            'name': 'New Client',
            'email': 'newclient@test.com',
            'phone': '+995555444444',
            'company': 'New Corp',
        }
        resp = self.api_post('/api/clients/', data, user=admin)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['name'], 'New Client')

    def test_client_call_history(self):
        admin = self.create_admin()
        client = self.create_client(phone='+995555123456')
        self.create_call_log(
            handled_by=admin, caller_number='+995555123456', direction='inbound',
        )
        resp = self.api_get(f'/api/clients/{client.id}/call_history/', user=admin)
        self.assertEqual(resp.status_code, 200)
