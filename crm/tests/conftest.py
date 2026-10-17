"""
Shared test infrastructure for CRM app tests.
Extends EchoDeskTenantTestCase with CRM-specific helpers.
"""
from datetime import timedelta
from django.utils import timezone
from users.tests.conftest import EchoDeskTenantTestCase
from crm.models import (
    SipConfiguration, UserPhoneAssignment, CallLog, CallEvent,
    CallRecording, PbxSettings, Client,
)


class CrmTestCase(EchoDeskTenantTestCase):
    """
    CRM-specific test case with factory helpers for all CRM models.
    """

    # Every calls endpoint needs the tenant subscription to include
    # ``ip_calling``; tests that check the gate set this to False.
    enable_ip_calling = True

    def setUp(self):
        super().setUp()
        if self.enable_ip_calling:
            self.set_ip_calling(True)

    def set_ip_calling(self, enabled):
        """Add / remove ``ip_calling`` on the test tenant's subscription."""
        from decimal import Decimal
        from tenants.feature_models import Feature
        from tenants.models import TenantSubscription

        feature, _ = Feature.objects.get_or_create(
            key='ip_calling',
            defaults={
                'name': 'IP Calling',
                'price_per_user_gel': Decimal('0'),
                'price_unlimited_gel': Decimal('0'),
                'category': 'communication',
                'is_active': True,
            },
        )
        sub, _ = TenantSubscription.objects.get_or_create(
            tenant=self.tenant,
            defaults={'is_active': True, 'starts_at': timezone.now(), 'agent_count': 10},
        )
        if enabled:
            sub.selected_features.add(feature)
        else:
            sub.selected_features.remove(feature)
        return sub

    @staticmethod
    def get_results(resp):
        """Extract results from a paginated or non-paginated response."""
        if isinstance(resp.data, dict) and 'results' in resp.data:
            return resp.data['results']
        return resp.data

    # ── SIP Configuration ──

    def create_sip_config(self, name='Test SIP', created_by=None, **kwargs):
        if created_by is None:
            created_by = self.create_admin(
                email=f'sip-admin-{SipConfiguration.objects.count()}@test.com'
            )
        defaults = {
            'sip_server': 'pbx.test.com',
            'sip_port': 5060,
            'username': 'testuser',
            'password': 'testpass',
            'realm': 'test.com',
            'phone_number': '+995322421219',
            'is_active': True,
            'is_default': False,
        }
        defaults.update(kwargs)
        return SipConfiguration.objects.create(
            name=name, created_by=created_by, **defaults
        )

    # ── User Phone Assignment ──

    def create_phone_assignment(self, user=None, sip_config=None, **kwargs):
        if user is None:
            user = self.create_user(
                email=f'phone-user-{UserPhoneAssignment.objects.count()}@test.com'
            )
        if sip_config is None:
            sip_config = self.create_sip_config()
        defaults = {
            'extension': str(100 + UserPhoneAssignment.objects.count()),
            'extension_password': 'extpass123',
            'phone_number': '+995322421219',
            'display_name': 'Test Agent',
            'is_primary': True,
            'is_active': True,
        }
        defaults.update(kwargs)
        return UserPhoneAssignment.objects.create(
            user=user, sip_configuration=sip_config, **defaults
        )

    # ── Client ──

    def create_client(self, name='Test Client', **kwargs):
        defaults = {
            'email': f'client-{Client.objects.count()}@test.com',
            'phone': '+995555123456',
            'company': 'Test Corp',
            'is_active': True,
        }
        defaults.update(kwargs)
        return Client.objects.create(name=name, **defaults)

    # ── Call Log ──

    def create_call_log(self, handled_by=None, sip_config=None, **kwargs):
        if handled_by is None:
            handled_by = self.create_user(
                email=f'handler-{CallLog.objects.count()}@test.com'
            )
        defaults = {
            'caller_number': '+995555111111',
            'recipient_number': '+995555222222',
            'direction': 'inbound',
            'call_type': 'voice',
            'status': 'ringing',
        }
        defaults.update(kwargs)
        return CallLog.objects.create(
            handled_by=handled_by,
            sip_configuration=sip_config,
            **defaults,
        )

    # ── Call Event ──

    def create_call_event(self, call_log, event_type='initiated', user=None, **kwargs):
        defaults = {
            'metadata': {},
        }
        defaults.update(kwargs)
        return CallEvent.objects.create(
            call_log=call_log,
            event_type=event_type,
            user=user,
            **defaults,
        )

    # ── Call Recording ──

    def create_call_recording(self, call_log, **kwargs):
        defaults = {
            'status': 'pending',
            'format': 'wav',
        }
        defaults.update(kwargs)
        return CallRecording.objects.create(
            call_log=call_log,
            **defaults,
        )

    # ── PBX Settings ──

    def create_pbx_settings(self, sip_config=None, **kwargs):
        if sip_config is None:
            sip_config = self.create_sip_config()
        defaults = {
            'working_hours_enabled': False,
            'working_hours_schedule': {},
            'timezone': 'Asia/Tbilisi',
            'holidays': [],
            'after_hours_action': 'announcement',
        }
        defaults.update(kwargs)
        return PbxSettings.objects.create(
            sip_configuration=sip_config,
            **defaults,
        )
