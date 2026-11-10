"""
Tests for PbxSettings model methods and PBX settings API endpoints:
working hours, holidays, sound upload/remove, and call routing.
"""
from datetime import datetime
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from zoneinfo import ZoneInfo

from crm.models import PbxSettings, SipConfiguration
from crm.tests.conftest import CrmTestCase


# ============================================================================
# PbxSettings.is_working_hours_now()
# ============================================================================


class TestIsWorkingHours(CrmTestCase):

    @patch('django.utils.timezone.now')
    def test_is_working_hours_during_schedule(self, mock_now):
        """Returns True during configured working hours."""
        # Wednesday 10:00 Tbilisi = 06:00 UTC
        mock_now.return_value = datetime(2026, 4, 8, 6, 0, 0, tzinfo=ZoneInfo('UTC'))
        settings = self.create_pbx_settings(
            working_hours_enabled=True,
            working_hours_schedule={
                'wednesday': [9, 10, 11, 12, 13, 14, 15, 16, 17],
            },
            timezone='Asia/Tbilisi',
        )
        self.assertTrue(settings.is_working_hours_now())

    @patch('django.utils.timezone.now')
    def test_is_working_hours_outside_schedule(self, mock_now):
        """Returns False outside configured working hours."""
        # Wednesday 02:00 Tbilisi = Monday 22:00 UTC
        mock_now.return_value = datetime(2026, 4, 7, 22, 0, 0, tzinfo=ZoneInfo('UTC'))
        settings = self.create_pbx_settings(
            working_hours_enabled=True,
            working_hours_schedule={
                'wednesday': [9, 10, 11, 12, 13, 14, 15, 16, 17],
            },
            timezone='Asia/Tbilisi',
        )
        self.assertFalse(settings.is_working_hours_now())

    def test_is_working_hours_disabled(self):
        """Returns True when working_hours_enabled is False (always open)."""
        settings = self.create_pbx_settings(working_hours_enabled=False)
        self.assertTrue(settings.is_working_hours_now())

    def test_is_working_hours_empty_schedule(self):
        """Returns True when schedule is empty, even if enabled."""
        settings = self.create_pbx_settings(
            working_hours_enabled=True,
            working_hours_schedule={},
        )
        self.assertTrue(settings.is_working_hours_now())

    @patch('django.utils.timezone.now')
    def test_is_working_hours_holiday(self, mock_now):
        """Returns False on a holiday date even if within working hours."""
        # Wednesday 10:00 Tbilisi = normally open
        mock_now.return_value = datetime(2026, 4, 8, 6, 0, 0, tzinfo=ZoneInfo('UTC'))
        settings = self.create_pbx_settings(
            working_hours_enabled=True,
            working_hours_schedule={
                'wednesday': [9, 10, 11, 12, 13, 14, 15, 16, 17],
            },
            timezone='Asia/Tbilisi',
            holidays=[{'date': '2026-04-08', 'name': 'Test Holiday'}],
        )
        self.assertFalse(settings.is_working_hours_now())

    @patch('django.utils.timezone.now')
    def test_is_working_hours_holiday_as_string(self, mock_now):
        """Holiday list with plain date strings (not dicts) is also supported."""
        mock_now.return_value = datetime(2026, 4, 8, 6, 0, 0, tzinfo=ZoneInfo('UTC'))
        settings = self.create_pbx_settings(
            working_hours_enabled=True,
            working_hours_schedule={
                'wednesday': [9, 10, 11, 12, 13, 14, 15, 16, 17],
            },
            timezone='Asia/Tbilisi',
            holidays=['2026-04-08'],
        )
        self.assertFalse(settings.is_working_hours_now())

    @patch('django.utils.timezone.now')
    def test_is_working_hours_day_not_in_schedule(self, mock_now):
        """Returns False for a day not listed in schedule."""
        # Saturday 10:00 Tbilisi
        mock_now.return_value = datetime(2026, 4, 11, 6, 0, 0, tzinfo=ZoneInfo('UTC'))
        settings = self.create_pbx_settings(
            working_hours_enabled=True,
            working_hours_schedule={
                'monday': [9, 10, 11, 12, 13, 14, 15, 16, 17],
            },
            timezone='Asia/Tbilisi',
        )
        self.assertFalse(settings.is_working_hours_now())

    @patch('django.utils.timezone.now')
    def test_is_working_hours_boundary_start(self, mock_now):
        """Returns True at the first hour of working hours."""
        # Wednesday 09:00 Tbilisi = 05:00 UTC
        mock_now.return_value = datetime(2026, 4, 8, 5, 0, 0, tzinfo=ZoneInfo('UTC'))
        settings = self.create_pbx_settings(
            working_hours_enabled=True,
            working_hours_schedule={
                'wednesday': [9, 10, 11, 12, 13, 14, 15, 16, 17],
            },
            timezone='Asia/Tbilisi',
        )
        self.assertTrue(settings.is_working_hours_now())

    @patch('django.utils.timezone.now')
    def test_is_working_hours_boundary_end(self, mock_now):
        """Returns False at the hour right after working hours end."""
        # Wednesday 18:00 Tbilisi = 14:00 UTC (schedule ends at 17)
        mock_now.return_value = datetime(2026, 4, 8, 14, 0, 0, tzinfo=ZoneInfo('UTC'))
        settings = self.create_pbx_settings(
            working_hours_enabled=True,
            working_hours_schedule={
                'wednesday': [9, 10, 11, 12, 13, 14, 15, 16, 17],
            },
            timezone='Asia/Tbilisi',
        )
        self.assertFalse(settings.is_working_hours_now())


# ============================================================================
# PbxSettings.get_sound_urls()
# ============================================================================


class TestGetSoundUrls(CrmTestCase):

    def test_get_sound_urls_empty(self):
        """Returns dict with None values when no sounds are uploaded."""
        settings = self.create_pbx_settings()
        urls = settings.get_sound_urls()
        self.assertIsInstance(urls, dict)
        self.assertIsNone(urls['greeting'])
        self.assertIsNone(urls['after_hours'])
        self.assertIsNone(urls['queue_hold'])
        self.assertIsNone(urls['voicemail_prompt'])
        self.assertIsNone(urls['thank_you'])
        self.assertIsNone(urls['transfer_hold'])
        self.assertIsNone(urls['review_prompt'])
        self.assertIsNone(urls['review_invalid'])
        self.assertIsNone(urls['review_thanks'])

    def test_get_sound_urls_contains_all_keys(self):
        """Sound URL dict includes all expected keys, including queue positions."""
        settings = self.create_pbx_settings()
        urls = settings.get_sound_urls()
        expected_keys = [
            'greeting', 'after_hours', 'queue_hold', 'voicemail_prompt',
            'thank_you', 'transfer_hold', 'review_prompt', 'review_invalid',
            'review_thanks',
        ]
        for key in expected_keys:
            self.assertIn(key, urls)

    def test_get_sound_urls_queue_positions(self):
        """Sound URL dict includes queue_position_1 through queue_position_10."""
        settings = self.create_pbx_settings()
        urls = settings.get_sound_urls()
        for i in range(1, 11):
            key = f'queue_position_{i}'
            self.assertIn(key, urls)
            self.assertIsNone(urls[key])


# ============================================================================
# PBX Settings API Endpoints
# ============================================================================


class TestPbxSettingsAPI(CrmTestCase):

    def test_get_pbx_settings_auto_creates(self):
        """GET auto-creates PbxSettings if it does not exist for the SIP config."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        self.assertFalse(PbxSettings.objects.filter(sip_configuration=sip).exists())
        resp = self.api_get(f'/api/pbx-settings/{sip.id}/', user=admin)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(PbxSettings.objects.filter(sip_configuration=sip).exists())

    def test_patch_pbx_settings(self):
        """PATCH updates working hours and other fields."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        self.create_pbx_settings(sip_config=sip)
        resp = self.api_patch(
            f'/api/pbx-settings/{sip.id}/',
            {
                'working_hours_enabled': True,
                'working_hours_schedule': {
                    'monday': [9, 10, 11, 12, 13, 14, 15, 16, 17],
                    'tuesday': [9, 10, 11, 12, 13, 14, 15, 16, 17],
                },
                'holidays': [{'date': '2026-01-01', 'name': 'New Year'}],
            },
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        settings = PbxSettings.objects.get(sip_configuration=sip)
        self.assertTrue(settings.working_hours_enabled)
        self.assertIn('monday', settings.working_hours_schedule)
        self.assertEqual(len(settings.holidays), 1)

    def test_get_pbx_settings_invalid_sip_config(self):
        admin = self.create_admin()
        resp = self.api_get('/api/pbx-settings/9999/', user=admin)
        self.assertEqual(resp.status_code, 404)

    def test_unauthenticated_rejected(self):
        resp = self.api_get('/api/pbx-settings/1/')
        self.assertIn(resp.status_code, [401, 403])


# ============================================================================
# Sound Upload
# ============================================================================


class TestUploadSound(CrmTestCase):

    def test_upload_sound_invalid_type(self):
        """Rejects unknown sound_type."""
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

    def test_upload_sound_wrong_extension(self):
        """Rejects non-audio files (e.g. .txt)."""
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

    def test_upload_sound_too_large(self):
        """Rejects files over 10MB."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        self.create_pbx_settings(sip_config=sip)
        # Create a file just over 10MB
        large_content = b'x' * (10 * 1024 * 1024 + 1)
        large_file = SimpleUploadedFile('large.wav', large_content, content_type='audio/wav')
        client = self.authenticated_client(admin)
        resp = client.post(
            f'/api/pbx-settings/{sip.id}/upload-sound/',
            {'sound_type': 'greeting', 'file': large_file},
            format='multipart',
            HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_sound_no_file(self):
        """Rejects when no file is provided."""
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

# ============================================================================
# Sound Removal
# ============================================================================


class TestRemoveSound(CrmTestCase):

    def test_remove_sound(self):
        """POST removes the file and clears the field."""
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
        """Rejects invalid sound_type."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin)
        self.create_pbx_settings(sip_config=sip)
        resp = self.api_post(
            f'/api/pbx-settings/{sip.id}/remove-sound/',
            {'sound_type': 'nonexistent_type'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)



# ============================================================================
# Call Routing API
# ============================================================================


class TestCallRouting(CrmTestCase):

    def _create_routing_setup(self, working_hours_enabled=True, schedule=None, holidays=None):
        """Create SIP config, PBX settings, and a phone assignment for routing tests."""
        admin = self.create_admin()
        sip = self.create_sip_config(
            created_by=admin, is_default=True, is_active=True,
            phone_number='+995322421219',
        )
        settings = self.create_pbx_settings(
            sip_config=sip,
            working_hours_enabled=working_hours_enabled,
            working_hours_schedule=schedule or {},
            holidays=holidays or [],
        )
        self.create_phone_assignment(user=admin, sip_config=sip, extension='100')
        return admin, sip, settings

    @patch('django.utils.timezone.now')
    def test_call_routing_working_hours(self, mock_now):
        """Returns is_working_hours=True during working hours."""
        mock_now.return_value = datetime(2026, 4, 8, 6, 0, 0, tzinfo=ZoneInfo('UTC'))
        self._create_routing_setup(
            working_hours_enabled=True,
            schedule={'wednesday': [9, 10, 11, 12, 13, 14, 15, 16, 17]},
        )
        resp = self.client.get(
            '/api/pbx/call-routing/?did=+995322421219',
            HTTP_HOST='tenant.test.com', **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['is_working_hours'])
        self.assertEqual(resp.data['action'], 'queue')

    @patch('django.utils.timezone.now')
    def test_call_routing_after_hours(self, mock_now):
        """Returns is_working_hours=False outside working hours."""
        # Wednesday 02:00 Tbilisi
        mock_now.return_value = datetime(2026, 4, 7, 22, 0, 0, tzinfo=ZoneInfo('UTC'))
        self._create_routing_setup(
            working_hours_enabled=True,
            schedule={'wednesday': [9, 10, 11, 12, 13, 14, 15, 16, 17]},
        )
        resp = self.client.get(
            '/api/pbx/call-routing/?did=+995322421219',
            HTTP_HOST='tenant.test.com', **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['is_working_hours'])
        self.assertEqual(resp.data['action'], 'after_hours')

    def test_call_routing_sounds_included(self):
        """Response includes sounds dict with all expected keys."""
        self._create_routing_setup(working_hours_enabled=False)
        resp = self.client.get(
            '/api/pbx/call-routing/?did=+995322421219',
            HTTP_HOST='tenant.test.com', **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('sounds', resp.data)
        sounds = resp.data['sounds']
        self.assertIn('greeting', sounds)
        self.assertIn('after_hours', sounds)

    def test_call_routing_queue_position_sounds(self):
        """Sounds dict includes queue_position_1 through queue_position_10."""
        self._create_routing_setup(working_hours_enabled=False)
        resp = self.client.get(
            '/api/pbx/call-routing/?did=+995322421219',
            HTTP_HOST='tenant.test.com', **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        sounds = resp.data['sounds']
        for i in range(1, 11):
            self.assertIn(f'queue_position_{i}', sounds)

    def test_call_routing_extensions_included(self):
        """Response includes list of active extensions."""
        self._create_routing_setup(working_hours_enabled=False)
        resp = self.client.get(
            '/api/pbx/call-routing/?did=+995322421219',
            HTTP_HOST='tenant.test.com', **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('extensions', resp.data)
        self.assertIn('100', resp.data['extensions'])

    def test_call_routing_missing_did(self):
        """Returns 400 if did parameter is missing."""
        resp = self.client.get(
            '/api/pbx/call-routing/',
            HTTP_HOST='tenant.test.com', **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_call_routing_unknown_did_falls_back_to_tenant_default(self):
        """A DID no configuration owns resolves to the tenant's *own*
        default configuration (single-trunk tenants); never another
        tenant's. See test_call_routing for the 404 when there is no default."""
        self._create_routing_setup(working_hours_enabled=False)
        resp = self.client.get(
            '/api/pbx/call-routing/?did=+999999999999',
            HTTP_HOST='tenant.test.com', **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['is_working_hours'])
        self.assertEqual(resp.data['tenant_schema'], self.tenant.schema_name)

    def test_call_routing_no_sip_configuration_is_404(self):
        """No SIP configuration in the token's tenant: 404, no scan of
        other tenants."""
        resp = self.client.get(
            '/api/pbx/call-routing/?did=+995322421219',
            HTTP_HOST='tenant.test.com', **self.pbx_headers(),
        )
        self.assertEqual(resp.status_code, 404)

    def test_call_routing_requires_pbx_token(self):
        resp = self.client.get(
            '/api/pbx/call-routing/?did=+995322421219',
            HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, 401)
