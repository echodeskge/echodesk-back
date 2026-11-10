"""
Tests for CRM call model logic: duration calculation, event types, parent call
relationships, transfer type choices, and PBX settings auto-creation.
"""
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from crm.models import (
    CallLog, CallEvent, CallRecording, PbxSettings, SipConfiguration,
)
from crm.tests.conftest import CrmTestCase


# ============================================================================
# CallLog → CRM client auto-match (BK-03 guard)
# ============================================================================


class TestCallLogClientMatch(CrmTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='cm-agent@test.com')
        # Stored unformatted: the match is ``phone__endswith`` on the raw
        # column, so a number saved with spaces would not be found.
        self.client_a = self.create_client(name='A', phone='+995555123456')
        self.client_b = self.create_client(name='B', phone='+995555999999')

    def test_inbound_matches_caller_by_last_7_digits(self):
        call = self.create_call_log(handled_by=self.agent, caller_number='0555123456',
                                    recipient_number='+995322421219')
        self.assertEqual(call.client, self.client_a)

    def test_outbound_matches_recipient(self):
        call = self.create_call_log(handled_by=self.agent, direction='outbound',
                                    caller_number='100', recipient_number='+995555999999')
        self.assertEqual(call.client, self.client_b)

    def test_endswith_not_contains(self):
        # The old ``icontains`` matched a client whose number merely
        # contained the digits somewhere in the middle.
        self.create_client(name='Mid', phone='+1555123456789')
        call = self.create_call_log(handled_by=self.agent, caller_number='+995555123456',
                                    recipient_number='+995322421219')
        self.assertEqual(call.client, self.client_a)

    def test_short_numbers_never_match(self):
        for number in ('', '100', 'anonymous', '123456'):
            call = self.create_call_log(handled_by=self.agent, caller_number=number,
                                        recipient_number='+995322421219')
            self.assertIsNone(call.client, number)

    def test_social_client_set_skips_lookup(self):
        from social_integrations.models import Client as SocialClient

        social = SocialClient.objects.create(name='Social', phone='+995555123456')
        # 2 = tenant-schemas' ``SET search_path`` + the INSERT; no client lookup.
        with self.assertNumQueries(2):
            call = CallLog.objects.create(
                caller_number='+995555123456', recipient_number='+995322421219',
                direction='inbound', status='ringing', handled_by=self.agent,
                social_client=social,
            )
        self.assertIsNone(call.client)

    def test_update_fields_without_client_skips_lookup(self):
        call = self.create_call_log(handled_by=self.agent, caller_number='+995555000000',
                                    recipient_number='+995322421219')
        self.assertIsNone(call.client)
        self.create_client(name='Late', phone='+995555000000')
        call.notes = 'n'
        # 2 = ``SET search_path`` + the UPDATE; no client lookup.
        with self.assertNumQueries(2):
            call.save(update_fields=['notes'])
        call.refresh_from_db()
        self.assertIsNone(call.client)
        # A full save still matches.
        call.save()
        self.assertEqual(call.client.name, 'Late')

    def test_existing_client_is_kept(self):
        call = self.create_call_log(handled_by=self.agent, caller_number='+995555999999',
                                    recipient_number='+995322421219', client=self.client_a)
        self.assertEqual(call.client, self.client_a)


# ============================================================================
# CallLog Duration Calculation
# ============================================================================


class TestCallLogDuration(CrmTestCase):

    def test_duration_calculation(self):
        """Duration = ended_at - answered_at when both are set."""
        now = timezone.now()
        call = self.create_call_log(
            answered_at=now - timedelta(minutes=5),
            ended_at=now,
            duration=now - (now - timedelta(minutes=5)),
            status='ended',
        )
        self.assertEqual(call.duration.total_seconds(), 300)

    def test_duration_zero_when_unanswered(self):
        """When answered_at is None, duration should be set to zero by the view."""
        call = self.create_call_log(
            status='ended',
            duration=timedelta(seconds=0),
        )
        self.assertEqual(call.duration.total_seconds(), 0)

    def test_duration_none_when_active(self):
        """Active calls have no duration set."""
        call = self.create_call_log(status='answered')
        self.assertIsNone(call.duration)

    def test_duration_fractional_seconds(self):
        """Duration correctly handles sub-minute values."""
        now = timezone.now()
        call = self.create_call_log(
            answered_at=now - timedelta(seconds=45),
            ended_at=now,
            duration=timedelta(seconds=45),
            status='ended',
        )
        self.assertEqual(call.duration.total_seconds(), 45)

    def test_duration_long_call(self):
        """Duration correctly handles multi-hour calls."""
        now = timezone.now()
        call = self.create_call_log(
            answered_at=now - timedelta(hours=2, minutes=30),
            ended_at=now,
            duration=timedelta(hours=2, minutes=30),
            status='ended',
        )
        self.assertEqual(call.duration.total_seconds(), 9000)


# ============================================================================
# CallEvent Types
# ============================================================================


class TestCallEventTypes(CrmTestCase):

    def test_all_event_types_are_valid_choices(self):
        """All defined event types should be valid for CallEvent creation."""
        valid_types = [choice[0] for choice in CallEvent.EVENT_TYPES]
        expected_types = [
            'initiated', 'ringing', 'answered', 'hold', 'unhold',
            'transfer_initiated', 'transfer_completed',
            'recording_started', 'recording_stopped',
            'muted', 'unmuted', 'dtmf', 'quality_change',
            'ended', 'failed', 'error', 'conference_started',
        ]
        for event_type in expected_types:
            self.assertIn(event_type, valid_types)

    def test_create_event_for_each_type(self):
        """Each event type can be used to create a CallEvent."""
        call = self.create_call_log()
        valid_types = [choice[0] for choice in CallEvent.EVENT_TYPES]
        for event_type in valid_types:
            event = self.create_call_event(call, event_type=event_type)
            self.assertEqual(event.event_type, event_type)
            self.assertEqual(event.call_log, call)

    def test_event_metadata_preserved(self):
        """Metadata dict is stored and retrieved correctly."""
        call = self.create_call_log()
        metadata = {
            'transfer_to': '+995555999999',
            'duration_seconds': 120,
            'nested': {'key': 'value'},
        }
        event = self.create_call_event(call, metadata=metadata)
        event.refresh_from_db()
        self.assertEqual(event.metadata['transfer_to'], '+995555999999')
        self.assertEqual(event.metadata['nested']['key'], 'value')


# ============================================================================
# CallLog Parent Call Relationship
# ============================================================================


class TestParentCallRelationship(CrmTestCase):

    def test_call_log_parent_call_relationship(self):
        """consultation_calls reverse relation returns child calls."""
        admin = self.create_admin()
        original = self.create_call_log(handled_by=admin, status='on_hold')
        consultation1 = self.create_call_log(
            handled_by=admin, status='answered',
            parent_call=original, direction='outbound',
        )
        consultation2 = self.create_call_log(
            handled_by=admin, status='ended',
            parent_call=original, direction='outbound',
        )
        consultation_ids = set(
            original.consultation_calls.values_list('id', flat=True)
        )
        self.assertEqual(consultation_ids, {consultation1.id, consultation2.id})

    def test_parent_call_nullable(self):
        """parent_call is optional (most calls have no parent)."""
        call = self.create_call_log()
        self.assertIsNone(call.parent_call)

    def test_parent_call_cascade_set_null(self):
        """Deleting parent call sets child's parent_call to None (SET_NULL)."""
        admin = self.create_admin()
        original = self.create_call_log(handled_by=admin)
        child = self.create_call_log(
            handled_by=admin, parent_call=original,
        )
        original_id = original.id
        original.delete()
        child.refresh_from_db()
        self.assertIsNone(child.parent_call)

    def test_consultation_calls_empty_by_default(self):
        """A fresh call has no consultation_calls."""
        call = self.create_call_log()
        self.assertEqual(call.consultation_calls.count(), 0)


# ============================================================================
# CallLog Transfer Type Choices
# ============================================================================


class TestTransferTypeChoices(CrmTestCase):

    def test_blank_transfer_type(self):
        """Default transfer_type is blank (empty string)."""
        call = self.create_call_log()
        self.assertEqual(call.transfer_type, '')

    def test_blind_transfer_type(self):
        """transfer_type can be set to 'blind'."""
        call = self.create_call_log(transfer_type='blind')
        call.refresh_from_db()
        self.assertEqual(call.transfer_type, 'blind')

    def test_attended_transfer_type(self):
        """transfer_type can be set to 'attended'."""
        call = self.create_call_log(transfer_type='attended')
        call.refresh_from_db()
        self.assertEqual(call.transfer_type, 'attended')

    def test_valid_transfer_type_choices(self):
        """All transfer type choices are '', 'blind', and 'attended'."""
        valid_choices = [choice[0] for choice in CallLog._meta.get_field('transfer_type').choices]
        self.assertIn('', valid_choices)
        self.assertIn('blind', valid_choices)
        self.assertIn('attended', valid_choices)


# ============================================================================
# CallLog Status Choices
# ============================================================================


class TestCallLogStatusChoices(CrmTestCase):

    def test_all_statuses_are_valid(self):
        """Verify all expected statuses are in STATUS_CHOICES."""
        valid_statuses = [choice[0] for choice in CallLog.STATUS_CHOICES]
        expected = [
            'initiated', 'ringing', 'answered', 'missed', 'busy',
            'no_answer', 'failed', 'cancelled', 'transferred', 'ended',
            'recording', 'on_hold',
        ]
        for s in expected:
            self.assertIn(s, valid_statuses)


# ============================================================================
# CallLog Direction Choices
# ============================================================================


class TestCallLogDirectionChoices(CrmTestCase):

    def test_inbound_direction(self):
        call = self.create_call_log(direction='inbound')
        self.assertEqual(call.direction, 'inbound')

    def test_outbound_direction(self):
        call = self.create_call_log(direction='outbound')
        self.assertEqual(call.direction, 'outbound')


# ============================================================================
# CallLog Call Type Choices
# ============================================================================


class TestCallLogCallTypeChoices(CrmTestCase):

    def test_voice_call_type(self):
        call = self.create_call_log(call_type='voice')
        self.assertEqual(call.call_type, 'voice')

    def test_video_call_type(self):
        call = self.create_call_log(call_type='video')
        self.assertEqual(call.call_type, 'video')

    def test_conference_call_type(self):
        call = self.create_call_log(call_type='conference')
        self.assertEqual(call.call_type, 'conference')


# ============================================================================
# PbxSettings Auto-Create
# ============================================================================


class TestPbxSettingsAutoCreate(CrmTestCase):

    def test_get_or_create_on_sip_config(self):
        """PbxSettings.objects.get_or_create works for a SIP configuration."""
        sip_config = self.create_sip_config()
        settings, created = PbxSettings.objects.get_or_create(
            sip_configuration=sip_config,
        )
        self.assertTrue(created)
        self.assertEqual(settings.sip_configuration, sip_config)

    def test_get_or_create_returns_existing(self):
        """Second get_or_create call returns the existing PbxSettings."""
        sip_config = self.create_sip_config()
        settings1, created1 = PbxSettings.objects.get_or_create(
            sip_configuration=sip_config,
        )
        settings2, created2 = PbxSettings.objects.get_or_create(
            sip_configuration=sip_config,
        )
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(settings1.id, settings2.id)

    def test_default_values_on_auto_create(self):
        """Auto-created PbxSettings has sensible defaults."""
        sip_config = self.create_sip_config()
        settings, _ = PbxSettings.objects.get_or_create(
            sip_configuration=sip_config,
        )
        self.assertFalse(settings.working_hours_enabled)
        self.assertEqual(settings.timezone, 'Asia/Tbilisi')
        self.assertEqual(settings.after_hours_action, 'announcement')
        self.assertFalse(settings.voicemail_enabled)


# ============================================================================
# CallRecording
# ============================================================================


class TestCallRecordingModel(CrmTestCase):

    def test_recording_linked_to_call(self):
        """CallRecording has a one-to-one relationship with CallLog."""
        call = self.create_call_log()
        recording = self.create_call_recording(call)
        self.assertEqual(recording.call_log, call)
        self.assertEqual(call.recording, recording)

    def test_recording_uuid_unique(self):
        """Each recording gets a unique UUID."""
        call1 = self.create_call_log()
        call2 = self.create_call_log()
        rec1 = self.create_call_recording(call1)
        rec2 = self.create_call_recording(call2)
        self.assertNotEqual(rec1.recording_id, rec2.recording_id)


# ============================================================================
# CallLog UUID Uniqueness
# ============================================================================


class TestCallLogUUID(CrmTestCase):

    def test_call_id_is_uuid(self):
        """Each call log gets a unique UUID call_id."""
        call1 = self.create_call_log()
        call2 = self.create_call_log()
        self.assertIsNotNone(call1.call_id)
        self.assertIsNotNone(call2.call_id)
        self.assertNotEqual(call1.call_id, call2.call_id)

    def test_call_id_is_not_editable(self):
        """call_id field is not editable."""
        field = CallLog._meta.get_field('call_id')
        self.assertFalse(field.editable)
