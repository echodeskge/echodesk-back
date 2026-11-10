"""Two agents' browsers never race here; one browser can: a retried
``log_incoming_call`` for the same INVITE (two tabs, a timeout retry) may
land while the first post is still inside its transaction. A row lock
cannot cover a row that does not exist yet, so the dedupe needs a lock
keyed on ``(user, sip_call_id)`` taken before the lookup.

Real threads and real transactions (``TransactionTestCase``); the tenant
schema is the one every other test class shares, so nothing is flushed
and the rows created here are deleted by hand.
"""
import threading
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from crm.models import CallEvent, CallLog, SipConfiguration
from crm.tests.conftest import CrmTestCase
from crm.views import CallLogViewSet
from users.models import User

LOG_URL = '/api/call-logs/log_incoming_call/'


class TestLogIncomingCallRace(TransactionTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        CrmTestCase.setUpClass()  # tenant row + schema + migrations, shared with every test class
        cls.tenant = CrmTestCase.tenant

    @classmethod
    def tearDownClass(cls):
        CrmTestCase.tearDownClass()
        super().tearDownClass()

    def _fixture_teardown(self):
        # No flush: the tenant schema is shared by the whole run.
        pass

    def setUp(self):
        connection.set_tenant(self.tenant)
        helper = CrmTestCase()
        helper.tenant = self.tenant
        helper.set_ip_calling(True)
        self.admin = User.objects.create_user(email='race-admin@test.com', password='x', role='admin', is_staff=True)
        self.agent = User.objects.create_user(email='race-agent@test.com', password='x', role='agent', can_make_calls=True)
        self.config = SipConfiguration.objects.create(
            name='Race SIP', sip_server='pbx.tenant.test', sip_port=5060, username='u', password='p',
            phone_number='+995322421219', is_active=True, is_default=True, created_by=self.admin,
        )

    def tearDown(self):
        connection.set_tenant(self.tenant)
        CallEvent.objects.filter(call_log__handled_by=self.agent).delete()
        CallLog.objects.filter(handled_by=self.agent).delete()
        self.config.delete()
        self.agent.delete()
        self.admin.delete()

    def _post(self, results, index, barrier):
        """One browser post, on this thread's own DB connection."""
        try:
            connection.set_tenant(self.tenant)
            factory = APIRequestFactory()
            request = factory.post(LOG_URL, {
                'caller_number': '+995599123456', 'recipient_number': '+995322421219',
                'sip_call_id': 'race-invite-1',
            }, format='json')
            force_authenticate(request, user=self.agent)
            request.tenant = self.tenant
            view = CallLogViewSet.as_view({'post': 'log_incoming_call'})
            results[index] = view(request).status_code
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            results[index] = exc
        finally:
            connection.close()

    def test_concurrent_posts_of_one_invite_yield_one_row(self):
        barrier = threading.Barrier(2)
        original = CallLogViewSet._match_client

        def match_client_rendezvous(number):
            # Both requests have done their lookup (inside their transaction)
            # before either inserts; with the lock the second one never gets
            # here until the first commits, so the wait simply times out.
            try:
                barrier.wait(timeout=3)
            except threading.BrokenBarrierError:
                pass
            return original(number)

        results = [None, None]
        with patch.object(CallLogViewSet, '_match_client', staticmethod(match_client_rendezvous)):
            threads = [threading.Thread(target=self._post, args=(results, i, barrier)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        connection.set_tenant(self.tenant)
        self.assertEqual(sorted(results, key=str), [200, 201], results)
        self.assertEqual(CallLog.objects.filter(sip_call_id='race-invite-1').count(), 1)
