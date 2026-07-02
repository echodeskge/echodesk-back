"""
Tests for CallLogViewSet endpoints: CRUD, call lifecycle, hold, blind transfer,
attended transfer (consultation), and merge conference.
"""
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone

from crm.models import CallLog, CallEvent
from crm.tests.conftest import CrmTestCase


# ============================================================================
# Basic CRUD
# ============================================================================


class TestCallLogCreate(CrmTestCase):
    pass


class TestCallLogList(CrmTestCase):

    def test_list_call_logs(self):
        """GET returns paginated list of call logs."""
        admin = self.create_admin()
        self.create_call_log(handled_by=admin)
        self.create_call_log(handled_by=admin, direction='outbound')
        self.create_call_log(handled_by=admin, status='answered')
        resp = self.api_get('/api/call-logs/', user=admin)
        self.assertEqual(resp.status_code, 200)
        results = self.get_results(resp)
        self.assertEqual(len(results), 3)

    def test_filter_by_status(self):
        """Filter by answered/missed/ended returns correct subset."""
        admin = self.create_admin()
        self.create_call_log(handled_by=admin, status='answered')
        self.create_call_log(handled_by=admin, status='missed')
        self.create_call_log(handled_by=admin, status='ended')
        for status_val, expected in [('answered', 1), ('missed', 1), ('ended', 1)]:
            resp = self.api_get(f'/api/call-logs/?status={status_val}', user=admin)
            self.assertEqual(resp.status_code, 200)
            results = self.get_results(resp)
            self.assertEqual(len(results), expected, f'status={status_val}')

    def test_filter_by_direction(self):
        """Filter by inbound/outbound returns correct subset."""
        admin = self.create_admin()
        self.create_call_log(handled_by=admin, direction='inbound')
        self.create_call_log(handled_by=admin, direction='inbound')
        self.create_call_log(handled_by=admin, direction='outbound')
        resp = self.api_get('/api/call-logs/?direction=inbound', user=admin)
        results = self.get_results(resp)
        self.assertEqual(len(results), 2)
        resp = self.api_get('/api/call-logs/?direction=outbound', user=admin)
        results = self.get_results(resp)
        self.assertEqual(len(results), 1)

    def test_list_unauthenticated_rejected(self):
        resp = self.api_get('/api/call-logs/')
        self.assertIn(resp.status_code, [401, 403])


# ============================================================================
# Call Lifecycle
# ============================================================================


class TestCallLifecycle(CrmTestCase):

    def test_initiate_call(self):
        """POST /initiate_call/ creates outbound log with status=initiated."""
        admin = self.create_admin()
        self.create_sip_config(created_by=admin, is_default=True, is_active=True)
        resp = self.api_post(
            '/api/call-logs/initiate_call/',
            {'recipient_number': '+995555333333'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['direction'], 'outbound')
        self.assertEqual(resp.data['status'], 'initiated')
        # An 'initiated' event should have been created
        call = CallLog.objects.get(id=resp.data['id'])
        self.assertTrue(call.events.filter(event_type='initiated').exists())

    def test_log_incoming_call(self):
        """POST /log_incoming_call/ creates inbound log with status=ringing."""
        admin = self.create_admin()
        self.create_sip_config(created_by=admin, is_default=True, is_active=True)
        resp = self.api_post(
            '/api/call-logs/log_incoming_call/',
            {
                'caller_number': '+995555444444',
                'recipient_number': '+995322421219',
                'sip_call_id': 'sip-incoming-001',
            },
            user=admin,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['direction'], 'inbound')
        self.assertEqual(resp.data['status'], 'ringing')
        call = CallLog.objects.get(id=resp.data['id'])
        self.assertTrue(call.events.filter(event_type='ringing').exists())

    def test_log_incoming_call_no_sip_config(self):
        """log_incoming_call requires a default SIP configuration."""
        admin = self.create_admin()
        resp = self.api_post(
            '/api/call-logs/log_incoming_call/',
            {
                'caller_number': '+995555444444',
                'recipient_number': '+995322421219',
            },
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_update_status_to_answered(self):
        """Sets status to answered and populates answered_at timestamp."""
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

    def test_update_status_answered_does_not_overwrite_answered_at(self):
        """If answered_at is already set, updating to answered again does not change it."""
        admin = self.create_admin()
        original_answered = timezone.now() - timedelta(minutes=10)
        call = self.create_call_log(
            handled_by=admin, status='on_hold',
            answered_at=original_answered,
        )
        self.api_patch(
            f'/api/call-logs/{call.id}/update_status/',
            {'status': 'answered'},
            user=admin,
        )
        call.refresh_from_db()
        # answered_at should remain the original value, not be overwritten
        self.assertEqual(call.answered_at, original_answered)

    def test_update_status_to_ended(self):
        """Sets ended_at and calculates duration from answered_at."""
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
        self.assertIsNotNone(call.ended_at)
        self.assertIsNotNone(call.duration)
        self.assertGreater(call.duration.total_seconds(), 0)

    def test_update_status_to_ended_unanswered_zero_duration(self):
        """Ending a call that was never answered gives zero duration."""
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='ringing')
        resp = self.api_patch(
            f'/api/call-logs/{call.id}/update_status/',
            {'status': 'ended'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.duration.total_seconds(), 0)

    def test_update_status_creates_event(self):
        """Status change creates a CallEvent with old/new status metadata."""
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='ringing')
        self.api_patch(
            f'/api/call-logs/{call.id}/update_status/',
            {'status': 'answered'},
            user=admin,
        )
        event = CallEvent.objects.filter(call_log=call, event_type='answered').first()
        self.assertIsNotNone(event)
        self.assertEqual(event.metadata['old_status'], 'ringing')
        self.assertEqual(event.metadata['new_status'], 'answered')

    def test_end_call(self):
        """POST /end_call/ sets ended_at and calculates duration."""
        admin = self.create_admin()
        call = self.create_call_log(
            handled_by=admin, status='answered',
            answered_at=timezone.now() - timedelta(minutes=3),
        )
        resp = self.api_post(f'/api/call-logs/{call.id}/end_call/', user=admin)
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'ended')
        self.assertIsNotNone(call.ended_at)
        self.assertIsNotNone(call.duration)
        self.assertGreater(call.duration.total_seconds(), 0)
        # Event should be created
        self.assertTrue(call.events.filter(event_type='ended').exists())

    def test_end_call_already_ended(self):
        """Cannot end a call that is already ended."""
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='ended')
        resp = self.api_post(f'/api/call-logs/{call.id}/end_call/', user=admin)
        self.assertEqual(resp.status_code, 400)

    def test_end_call_missed_status_rejected(self):
        """Cannot end a call with missed status."""
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='missed')
        resp = self.api_post(f'/api/call-logs/{call.id}/end_call/', user=admin)
        self.assertEqual(resp.status_code, 400)


# ============================================================================
# Hold
# ============================================================================


class TestToggleHold(CrmTestCase):

    def test_toggle_hold_on(self):
        """Answered call toggled to on_hold, creates hold event."""
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='answered')
        resp = self.api_post(f'/api/call-logs/{call.id}/toggle_hold/', user=admin)
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'on_hold')
        self.assertTrue(call.events.filter(event_type='hold').exists())

    def test_toggle_hold_off(self):
        """On-hold call toggled back to answered, creates unhold event."""
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='on_hold')
        resp = self.api_post(f'/api/call-logs/{call.id}/toggle_hold/', user=admin)
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'answered')
        self.assertTrue(call.events.filter(event_type='unhold').exists())

    def test_toggle_hold_invalid_status(self):
        """Rejects hold toggle if call is not answered or on_hold."""
        admin = self.create_admin()
        for bad_status in ['ringing', 'initiated', 'ended', 'missed', 'transferred']:
            call = self.create_call_log(handled_by=admin, status=bad_status)
            resp = self.api_post(f'/api/call-logs/{call.id}/toggle_hold/', user=admin)
            self.assertEqual(resp.status_code, 400, f'status={bad_status} should be rejected')


# ============================================================================
# Blind Transfer
# ============================================================================


class TestBlindTransfer(CrmTestCase):

    def test_transfer_call(self):
        """Blind transfer sets status=transferred, transferred_to, and creates events."""
        admin = self.create_admin()
        call = self.create_call_log(
            handled_by=admin, status='answered',
            answered_at=timezone.now() - timedelta(minutes=2),
        )
        resp = self.api_post(
            f'/api/call-logs/{call.id}/transfer_call/',
            {'transfer_to': '+995555999999'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'transferred')
        self.assertIsNotNone(call.ended_at)
        # Both transfer_initiated and transfer_completed events should exist
        self.assertTrue(call.events.filter(event_type='transfer_initiated').exists())
        self.assertTrue(call.events.filter(event_type='transfer_completed').exists())

    def test_transfer_requires_active_call(self):
        """Transfer rejects calls that are not answered or on_hold."""
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='ringing')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/transfer_call/',
            {'transfer_to': '+995555999999'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_transfer_call_from_on_hold(self):
        """Transfer is allowed from on_hold status."""
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='on_hold')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/transfer_call/',
            {'transfer_to': '+995555999999'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.status, 'transferred')

    def test_transfer_requires_transfer_to_number(self):
        """Transfer without transfer_to returns 400."""
        admin = self.create_admin()
        call = self.create_call_log(handled_by=admin, status='answered')
        resp = self.api_post(
            f'/api/call-logs/{call.id}/transfer_call/', {}, user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_transfer_calculates_duration(self):
        """After transfer, duration is set if the call was answered."""
        admin = self.create_admin()
        call = self.create_call_log(
            handled_by=admin, status='answered',
            answered_at=timezone.now() - timedelta(minutes=5),
        )
        self.api_post(
            f'/api/call-logs/{call.id}/transfer_call/',
            {'transfer_to': '+995555999999'},
            user=admin,
        )
        call.refresh_from_db()
        self.assertIsNotNone(call.duration)
        self.assertGreater(call.duration.total_seconds(), 0)


# ============================================================================
# Attended Transfer (Consultation)
# ============================================================================


class TestInitiateConsultation(CrmTestCase):

    def test_initiate_consultation(self):
        """Creates consultation log with parent_call link."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin, is_default=True)
        original = self.create_call_log(
            handled_by=admin, status='answered', sip_config=sip,
        )
        resp = self.api_post(
            f'/api/call-logs/{original.id}/initiate_consultation/',
            {'target_number': '+995555888888'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertIn('consultation_log_id', resp.data)
        self.assertIn('consultation_call_id', resp.data)
        self.assertIn('original_call_id', resp.data)

        # Verify the consultation call log
        consultation = CallLog.objects.get(id=resp.data['consultation_log_id'])
        self.assertEqual(consultation.parent_call, original)
        self.assertEqual(consultation.recipient_number, '+995555888888')
        self.assertEqual(consultation.direction, 'outbound')
        self.assertEqual(consultation.status, 'initiated')
        self.assertEqual(consultation.sip_configuration, sip)

    def test_initiate_consultation_puts_original_on_hold(self):
        """Original call status becomes on_hold after consultation is initiated."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin, is_default=True)
        original = self.create_call_log(
            handled_by=admin, status='answered', sip_config=sip,
        )
        self.api_post(
            f'/api/call-logs/{original.id}/initiate_consultation/',
            {'target_number': '+995555888888'},
            user=admin,
        )
        original.refresh_from_db()
        self.assertEqual(original.status, 'on_hold')

    def test_initiate_consultation_creates_event(self):
        """A transfer_initiated event is created on the original call."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin, is_default=True)
        original = self.create_call_log(
            handled_by=admin, status='answered', sip_config=sip,
        )
        resp = self.api_post(
            f'/api/call-logs/{original.id}/initiate_consultation/',
            {'target_number': '+995555888888'},
            user=admin,
        )
        event = CallEvent.objects.filter(
            call_log=original, event_type='transfer_initiated',
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.metadata['transfer_type'], 'attended')
        self.assertEqual(event.metadata['target_number'], '+995555888888')
        self.assertEqual(
            event.metadata['consultation_log_id'],
            resp.data['consultation_log_id'],
        )

    def test_initiate_consultation_requires_active_call(self):
        """Rejects if call is not answered or on_hold."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin, is_default=True)
        for bad_status in ['ringing', 'initiated', 'ended', 'missed', 'transferred']:
            call = self.create_call_log(
                handled_by=admin, status=bad_status, sip_config=sip,
            )
            resp = self.api_post(
                f'/api/call-logs/{call.id}/initiate_consultation/',
                {'target_number': '+995555888888'},
                user=admin,
            )
            self.assertEqual(resp.status_code, 400, f'status={bad_status} should be rejected')

    def test_initiate_consultation_from_on_hold(self):
        """Consultation can be initiated from an on_hold call."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin, is_default=True)
        original = self.create_call_log(
            handled_by=admin, status='on_hold', sip_config=sip,
        )
        resp = self.api_post(
            f'/api/call-logs/{original.id}/initiate_consultation/',
            {'target_number': '+995555888888'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 201)

    def test_initiate_consultation_with_target_user(self):
        """When target_user_id is provided, it is recorded on the consultation log."""
        admin = self.create_admin()
        target_user = self.create_user(email='target-agent@test.com')
        sip = self.create_sip_config(created_by=admin, is_default=True)
        original = self.create_call_log(
            handled_by=admin, status='answered', sip_config=sip,
        )
        resp = self.api_post(
            f'/api/call-logs/{original.id}/initiate_consultation/',
            {'target_number': '+995555888888', 'target_user_id': target_user.id},
            user=admin,
        )
        self.assertEqual(resp.status_code, 201)
        consultation = CallLog.objects.get(id=resp.data['consultation_log_id'])
        self.assertEqual(consultation.transferred_to_user, target_user)

    def test_initiate_consultation_missing_target_number(self):
        """Rejects if target_number is not provided."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin, is_default=True)
        original = self.create_call_log(
            handled_by=admin, status='answered', sip_config=sip,
        )
        resp = self.api_post(
            f'/api/call-logs/{original.id}/initiate_consultation/',
            {},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)


class TestCompleteAttendedTransfer(CrmTestCase):

    def _setup_consultation(self):
        """Helper to create an original call with an active consultation."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin, is_default=True)
        original = self.create_call_log(
            handled_by=admin, status='answered', sip_config=sip,
            answered_at=timezone.now() - timedelta(minutes=5),
        )
        # Initiate consultation
        resp = self.api_post(
            f'/api/call-logs/{original.id}/initiate_consultation/',
            {'target_number': '+995555888888'},
            user=admin,
        )
        consultation = CallLog.objects.get(id=resp.data['consultation_log_id'])
        original.refresh_from_db()
        return admin, original, consultation

    def test_complete_attended_transfer(self):
        """Completing attended transfer sets transfer_type=attended, creates events."""
        admin, original, consultation = self._setup_consultation()
        resp = self.api_post(
            f'/api/call-logs/{original.id}/complete_attended_transfer/',
            {
                'consultation_log_id': consultation.id,
                'target_number': '+995555888888',
            },
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        original.refresh_from_db()
        self.assertEqual(original.status, 'transferred')
        self.assertEqual(original.transfer_type, 'attended')
        self.assertIsNotNone(original.ended_at)
        self.assertIsNotNone(original.transferred_at)

    def test_complete_attended_transfer_sets_transferred_to(self):
        """The transferred_to field is populated with the target number."""
        admin, original, consultation = self._setup_consultation()
        self.api_post(
            f'/api/call-logs/{original.id}/complete_attended_transfer/',
            {
                'consultation_log_id': consultation.id,
                'target_number': '+995555888888',
            },
            user=admin,
        )
        original.refresh_from_db()
        self.assertEqual(original.transferred_to, '+995555888888')

    def test_complete_attended_transfer_calculates_duration(self):
        """Duration is calculated from answered_at."""
        admin, original, consultation = self._setup_consultation()
        self.api_post(
            f'/api/call-logs/{original.id}/complete_attended_transfer/',
            {
                'consultation_log_id': consultation.id,
                'target_number': '+995555888888',
            },
            user=admin,
        )
        original.refresh_from_db()
        self.assertIsNotNone(original.duration)
        self.assertGreater(original.duration.total_seconds(), 0)

    def test_complete_attended_transfer_creates_event(self):
        """A transfer_completed event is created on the original call."""
        admin, original, consultation = self._setup_consultation()
        self.api_post(
            f'/api/call-logs/{original.id}/complete_attended_transfer/',
            {
                'consultation_log_id': consultation.id,
                'target_number': '+995555888888',
            },
            user=admin,
        )
        event = CallEvent.objects.filter(
            call_log=original, event_type='transfer_completed',
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.metadata['transfer_type'], 'attended')

    def test_complete_attended_transfer_sets_user_from_consultation(self):
        """If the consultation log has a transferred_to_user, it is copied to the original."""
        admin = self.create_admin()
        target_user = self.create_user(email='target@test.com')
        sip = self.create_sip_config(created_by=admin, is_default=True)
        original = self.create_call_log(
            handled_by=admin, status='answered', sip_config=sip,
            answered_at=timezone.now() - timedelta(minutes=5),
        )
        resp = self.api_post(
            f'/api/call-logs/{original.id}/initiate_consultation/',
            {'target_number': '+995555888888', 'target_user_id': target_user.id},
            user=admin,
        )
        consultation = CallLog.objects.get(id=resp.data['consultation_log_id'])
        original.refresh_from_db()

        self.api_post(
            f'/api/call-logs/{original.id}/complete_attended_transfer/',
            {
                'consultation_log_id': consultation.id,
                'target_number': '+995555888888',
            },
            user=admin,
        )
        original.refresh_from_db()
        self.assertEqual(original.transferred_to_user, target_user)

    def test_complete_attended_transfer_requires_fields(self):
        """Missing consultation_log_id or target_number returns 400."""
        admin, original, consultation = self._setup_consultation()
        # Missing both
        resp = self.api_post(
            f'/api/call-logs/{original.id}/complete_attended_transfer/',
            {},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)
        # Missing target_number
        resp = self.api_post(
            f'/api/call-logs/{original.id}/complete_attended_transfer/',
            {'consultation_log_id': consultation.id},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_complete_attended_transfer_invalid_consultation(self):
        """Non-existent consultation_log_id returns 404."""
        admin, original, consultation = self._setup_consultation()
        resp = self.api_post(
            f'/api/call-logs/{original.id}/complete_attended_transfer/',
            {'consultation_log_id': 99999, 'target_number': '+995555888888'},
            user=admin,
        )
        self.assertEqual(resp.status_code, 404)


class TestCancelConsultation(CrmTestCase):

    def _setup_consultation(self):
        """Helper to create an original call with an active consultation."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin, is_default=True)
        original = self.create_call_log(
            handled_by=admin, status='answered', sip_config=sip,
            answered_at=timezone.now() - timedelta(minutes=5),
        )
        resp = self.api_post(
            f'/api/call-logs/{original.id}/initiate_consultation/',
            {'target_number': '+995555888888'},
            user=admin,
        )
        consultation = CallLog.objects.get(id=resp.data['consultation_log_id'])
        original.refresh_from_db()
        return admin, original, consultation

    def test_cancel_consultation(self):
        """Cancelling ends consultation log and resumes original to answered."""
        admin, original, consultation = self._setup_consultation()
        resp = self.api_post(
            f'/api/call-logs/{original.id}/cancel_consultation/',
            {'consultation_log_id': consultation.id},
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)

        # Original call resumed
        original.refresh_from_db()
        self.assertEqual(original.status, 'answered')

        # Consultation call ended
        consultation.refresh_from_db()
        self.assertEqual(consultation.status, 'ended')
        self.assertIsNotNone(consultation.ended_at)

    def test_cancel_consultation_creates_event(self):
        """Event created with cancellation metadata on original call."""
        admin, original, consultation = self._setup_consultation()
        self.api_post(
            f'/api/call-logs/{original.id}/cancel_consultation/',
            {'consultation_log_id': consultation.id},
            user=admin,
        )
        event = CallEvent.objects.filter(
            call_log=original, event_type='transfer_initiated',
        ).last()
        self.assertIsNotNone(event)
        self.assertEqual(event.metadata.get('action'), 'consultation_cancelled')

    def test_cancel_consultation_missing_id(self):
        """Missing consultation_log_id returns 400."""
        admin, original, _ = self._setup_consultation()
        resp = self.api_post(
            f'/api/call-logs/{original.id}/cancel_consultation/',
            {},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_cancel_consultation_invalid_id(self):
        """Non-existent consultation_log_id returns 404."""
        admin, original, _ = self._setup_consultation()
        resp = self.api_post(
            f'/api/call-logs/{original.id}/cancel_consultation/',
            {'consultation_log_id': 99999},
            user=admin,
        )
        self.assertEqual(resp.status_code, 404)


# ============================================================================
# Merge Conference
# ============================================================================


class TestMergeConference(CrmTestCase):

    def setUp(self):
        super().setUp()
        # merge_conference resolves the tenant's PbxServer (BYO-PBX migration)
        # for AMI credentials before touching Asterisk. Mock it to a configured
        # server so the tests exercise the AMI path; individual tests can set
        # ``self.mock_get_pbx.return_value = None`` to test the no-PBX branch.
        patcher = patch('crm.asterisk_db.get_active_pbx_for_current_tenant')
        self.mock_get_pbx = patcher.start()
        self.addCleanup(patcher.stop)
        fake_pbx = MagicMock()
        fake_pbx.ami_host = 'pbx.test.com'
        fake_pbx.fqdn = 'pbx.test.com'
        fake_pbx.ami_username = 'amiuser'
        fake_pbx.ami_password = 'amipass'
        fake_pbx.ami_port = 5038
        self.mock_get_pbx.return_value = fake_pbx

    def _setup_attended_transfer(self):
        """Create original + consultation with transfer_type='attended' on the original."""
        admin = self.create_admin()
        sip = self.create_sip_config(
            created_by=admin, is_default=True, is_active=True,
            sip_server='pbx.test.com',
        )
        self.create_phone_assignment(user=admin, sip_config=sip, extension='100')
        original = self.create_call_log(
            handled_by=admin, status='on_hold', sip_config=sip,
            caller_number='+995555111111',
            recipient_number='+995322421219',
            answered_at=timezone.now() - timedelta(minutes=5),
            transfer_type='attended',
        )
        consultation = self.create_call_log(
            handled_by=admin, status='answered', sip_config=sip,
            caller_number='+995322421219',
            recipient_number='+995555888888',
            parent_call=original,
        )
        return admin, sip, original, consultation

    @patch('crm.views._ami_redirect_to_confbridge')
    @patch('crm.views._ami_get_channels')
    def test_merge_conference_creates_events(self, mock_channels, mock_redirect):
        """Conference_started events created on both logs after merge."""
        admin, sip, original, consultation = self._setup_attended_transfer()
        mock_channels.return_value = [
            {'channel': 'PJSIP/100-00000001', 'calleridnum': '100'},
            {'channel': 'PJSIP/geo-provider-00000002', 'calleridnum': '995555111111'},
        ]
        mock_redirect.return_value = 'Success'

        resp = self.api_post(
            f'/api/call-logs/{original.id}/merge_conference/',
            {'consultation_log_id': consultation.id},
            user=admin,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('conference_room', resp.data)
        self.assertIn('channels_redirected', resp.data)

        # Both calls should have conference_started events
        self.assertTrue(
            CallEvent.objects.filter(
                call_log=original, event_type='conference_started',
            ).exists()
        )
        self.assertTrue(
            CallEvent.objects.filter(
                call_log=consultation, event_type='conference_started',
            ).exists()
        )

        # Call types should be updated to conference
        original.refresh_from_db()
        consultation.refresh_from_db()
        self.assertEqual(original.call_type, 'conference')
        self.assertEqual(consultation.call_type, 'conference')

    @patch('crm.views._ami_redirect_to_confbridge')
    @patch('crm.views._ami_get_channels')
    def test_merge_conference_requires_attended_transfer(self, mock_channels, mock_redirect):
        """Rejects if original call does not have transfer_type='attended'."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin, is_default=True)
        original = self.create_call_log(
            handled_by=admin, status='on_hold', sip_config=sip,
            transfer_type='',  # not an attended transfer
        )
        consultation = self.create_call_log(
            handled_by=admin, status='answered', sip_config=sip,
            parent_call=original,
        )
        resp = self.api_post(
            f'/api/call-logs/{original.id}/merge_conference/',
            {'consultation_log_id': consultation.id},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('attended', resp.data['error'].lower())

    @patch('crm.views._ami_redirect_to_confbridge')
    @patch('crm.views._ami_get_channels')
    def test_merge_conference_requires_on_hold(self, mock_channels, mock_redirect):
        """Rejects if original call is not on_hold."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin, is_default=True)
        original = self.create_call_log(
            handled_by=admin, status='answered', sip_config=sip,
            transfer_type='attended',
        )
        consultation = self.create_call_log(
            handled_by=admin, status='answered', sip_config=sip,
            parent_call=original,
        )
        resp = self.api_post(
            f'/api/call-logs/{original.id}/merge_conference/',
            {'consultation_log_id': consultation.id},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('on hold', resp.data['error'].lower())

    def test_merge_conference_requires_consultation_log(self):
        """Rejects without consultation_log_id."""
        admin = self.create_admin()
        sip = self.create_sip_config(created_by=admin, is_default=True)
        original = self.create_call_log(
            handled_by=admin, status='on_hold', sip_config=sip,
            transfer_type='attended',
        )
        resp = self.api_post(
            f'/api/call-logs/{original.id}/merge_conference/',
            {},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    def test_merge_conference_invalid_consultation_log(self):
        """Non-existent consultation_log_id returns 404."""
        admin, sip, original, _ = self._setup_attended_transfer()
        resp = self.api_post(
            f'/api/call-logs/{original.id}/merge_conference/',
            {'consultation_log_id': 99999},
            user=admin,
        )
        self.assertEqual(resp.status_code, 404)

    @patch('crm.views._ami_get_channels')
    def test_merge_conference_ami_channel_error(self, mock_channels):
        """AMI connection error returns 502."""
        admin, sip, original, consultation = self._setup_attended_transfer()
        mock_channels.side_effect = ConnectionError('AMI unreachable')
        resp = self.api_post(
            f'/api/call-logs/{original.id}/merge_conference/',
            {'consultation_log_id': consultation.id},
            user=admin,
        )
        self.assertEqual(resp.status_code, 502)

    @patch('crm.views._ami_redirect_to_confbridge')
    @patch('crm.views._ami_get_channels')
    def test_merge_conference_no_channels_found(self, mock_channels, mock_redirect):
        """When AMI returns no matching channels, returns 400."""
        admin, sip, original, consultation = self._setup_attended_transfer()
        mock_channels.return_value = []  # no active channels
        resp = self.api_post(
            f'/api/call-logs/{original.id}/merge_conference/',
            {'consultation_log_id': consultation.id},
            user=admin,
        )
        self.assertEqual(resp.status_code, 400)

    @patch('crm.views._ami_redirect_to_confbridge')
    @patch('crm.views._ami_get_channels')
    def test_merge_conference_no_pbx_configured(self, mock_channels, mock_redirect):
        """Returns 503 when the tenant has no PbxServer configured for AMI."""
        self.mock_get_pbx.return_value = None
        admin = self.create_admin()
        original = self.create_call_log(
            handled_by=admin, status='on_hold',
            transfer_type='attended',
        )
        consultation = self.create_call_log(
            handled_by=admin, status='answered',
            parent_call=original,
        )
        resp = self.api_post(
            f'/api/call-logs/{original.id}/merge_conference/',
            {'consultation_log_id': consultation.id},
            user=admin,
        )
        self.assertEqual(resp.status_code, 503)
