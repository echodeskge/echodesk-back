"""A leg that carries the PBX linked id is classified by that id alone."""
from datetime import timedelta

from django.utils import timezone

from crm.call_notifications import find_answered_sibling, missed_call_state
from crm.tests.conftest import CrmTestCase


class TestExactIdNeverFallsBackToHeuristic(CrmTestCase):
    CALLER = '+995598211409'

    def _leg(self, user, *, pbx_call_id, started_offset=0, **kwargs):
        started = timezone.now() - timedelta(seconds=60 - started_offset)
        return self.create_call_log(
            handled_by=user, direction='inbound', caller_number=self.CALLER,
            recipient_number='101', pbx_call_id=pbx_call_id,
            started_at=started, **kwargs,
        )

    def test_unrelated_answered_call_from_same_number_does_not_count(self):
        admin = self.create_admin()
        other = self.create_user(email='other-agent@test.com')
        # An earlier, separate call from the same number that a colleague
        # answered (different linked id, still inside the heuristic window).
        self._leg(other, pbx_call_id='1700000000.10', started_offset=5,
                  status='answered', answered_at=timezone.now() - timedelta(seconds=50),
                  ended_at=timezone.now() - timedelta(seconds=20))
        # Our leg: a new attempt nobody answered.
        mine = self._leg(admin, pbx_call_id='1700000000.42', started_offset=30,
                         status='ringing', ended_at=timezone.now())
        state, sibling = missed_call_state(mine)
        self.assertEqual(state, 'missed')
        self.assertIsNone(sibling)
        self.assertIsNone(find_answered_sibling(mine))

    def test_same_id_answered_by_colleague_is_answered_elsewhere(self):
        admin = self.create_admin()
        other = self.create_user(email='other-agent@test.com')
        winner = self._leg(other, pbx_call_id='1700000000.42', started_offset=30,
                           status='answered', answered_at=timezone.now())
        mine = self._leg(admin, pbx_call_id='1700000000.42', started_offset=30,
                         status='ringing', ended_at=timezone.now())
        state, sibling = missed_call_state(mine)
        self.assertEqual(state, 'answered_elsewhere')
        self.assertEqual(sibling.pk, winner.pk)
        self.assertEqual(find_answered_sibling(mine).pk, winner.pk)

    def test_same_id_still_ringing_elsewhere_is_pending(self):
        admin = self.create_admin()
        other = self.create_user(email='other-agent@test.com')
        self._leg(other, pbx_call_id='1700000000.42', started_offset=30, status='ringing')
        mine = self._leg(admin, pbx_call_id='1700000000.42', started_offset=30,
                         status='ringing', ended_at=timezone.now())
        state, _ = missed_call_state(mine)
        self.assertEqual(state, 'pending')
