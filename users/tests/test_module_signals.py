"""
Tests for users/module_signals.py signal handlers:
- Invoice signal: creating an Invoice notifies active users
- Invoice status transitions: paid, overdue
- Leave signal: creating LeaveRequest with status='pending' notifies managers
- Leave signal: changing to approved notifies employee
- Leave signal: changing to rejected notifies employee
- Booking signal: changing status to 'confirmed' notifies staff
- Call signal: creating CallLog with status='missed' + direction='inbound' notifies
"""
from decimal import Decimal
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model

from users.tests.conftest import EchoDeskTenantTestCase
from users.models import Notification

User = get_user_model()

# Patch create_notification's websocket + push to avoid side effects.
# async_to_sync is imported inside create_notification's body, so patch at source.
_ws_patch = patch('asgiref.sync.async_to_sync', return_value=lambda f: (lambda **kw: None))
_push_patch = patch('users.notification_utils.increment_unread')


class TestInvoiceSignals(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='inv-sig-admin@test.com')

    @_ws_patch
    @_push_patch
    def test_invoice_created_notifies_users(self, mock_incr, mock_async):
        from invoices.models import Invoice
        from ecommerce_crm.models import EcommerceClient

        inv = Invoice.objects.create(
            invoice_number='INV-2026-001',
            status='draft',
            client_name='Acme Corp',
            due_date=date.today() + timedelta(days=30),
            total=Decimal('500.00'),
            currency='GEL',
            created_by=self.admin,
        )
        # Should have created notifications for active users
        notifs = Notification.objects.filter(
            notification_type='invoice_created',
        )
        self.assertTrue(notifs.exists())
        self.assertIn('INV-2026-001', notifs.first().title)

    @_ws_patch
    @_push_patch
    def test_invoice_paid_notifies_creator(self, mock_incr, mock_async):
        from invoices.models import Invoice

        inv = Invoice.objects.create(
            invoice_number='INV-2026-002',
            status='draft',
            client_name='Client X',
            due_date=date.today() + timedelta(days=30),
            total=Decimal('100.00'),
            currency='GEL',
            created_by=self.admin,
        )
        # Clear creation notifications
        Notification.objects.all().delete()

        # Transition to paid
        inv.status = 'paid'
        inv.save()

        notifs = Notification.objects.filter(
            notification_type='invoice_paid',
            user=self.admin,
        )
        self.assertTrue(notifs.exists())

    @_ws_patch
    @_push_patch
    def test_invoice_overdue_notifies_creator(self, mock_incr, mock_async):
        from invoices.models import Invoice

        inv = Invoice.objects.create(
            invoice_number='INV-2026-003',
            status='sent',
            client_name='Client Y',
            due_date=date.today() - timedelta(days=1),
            total=Decimal('200.00'),
            currency='GEL',
            created_by=self.admin,
        )
        Notification.objects.all().delete()

        inv.status = 'overdue'
        inv.save()

        notifs = Notification.objects.filter(
            notification_type='invoice_overdue',
            user=self.admin,
        )
        self.assertTrue(notifs.exists())


class TestLeaveSignals(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='leave-sig-admin@test.com')
        self.employee = self.create_user(email='leave-sig-emp@test.com', role='agent')

    @_ws_patch
    @_push_patch
    def test_leave_request_created_notifies_managers(self, mock_incr, mock_async):
        from leave_management.models import LeaveType, LeaveRequest

        lt = LeaveType.objects.create(
            tenant=self.tenant,
            code='VSIG',
            name={'en': 'Vacation'},
            is_paid=True,
            requires_approval=True,
            calculation_method='annual',
            default_days_per_year=Decimal('20'),
            accrual_rate_per_month=Decimal('0'),
            color='#3B82F6',
            is_active=True,
            sort_order=0,
            created_by=self.admin,
            updated_by=self.admin,
        )

        lr = LeaveRequest.objects.create(
            tenant=self.tenant,
            employee=self.employee,
            leave_type=lt,
            start_date=date.today() + timedelta(days=7),
            end_date=date.today() + timedelta(days=9),
            total_days=Decimal('3'),
            reason='Vacation',
            status='pending',
        )

        # Manager (admin) should be notified
        notifs = Notification.objects.filter(
            notification_type='leave_request_submitted',
            user=self.admin,
        )
        self.assertTrue(notifs.exists())
        self.assertEqual(notifs.first().link_url, f'/leave/team-requests?id={lr.id}')

    def _make_leave_type(self, code):
        from leave_management.models import LeaveType
        return LeaveType.objects.create(
            tenant=self.tenant,
            code=code,
            name={'en': 'Vacation'},
            is_paid=True,
            requires_approval=True,
            calculation_method='annual',
            default_days_per_year=Decimal('20'),
            accrual_rate_per_month=Decimal('0'),
            color='#3B82F6',
            is_active=True,
            sort_order=0,
            created_by=self.admin,
            updated_by=self.admin,
        )

    @_ws_patch
    @_push_patch
    def test_leave_request_created_notifies_only_direct_manager(self, mock_incr, mock_async):
        from leave_management.models import LeaveRequest

        manager = self.create_user(email='leave-sig-mgr@test.com', role='agent')
        self.employee.manager = manager
        self.employee.save()
        lt = self._make_leave_type('VSIG4')

        lr = LeaveRequest.objects.create(
            tenant=self.tenant,
            employee=self.employee,
            leave_type=lt,
            start_date=date.today() + timedelta(days=7),
            end_date=date.today() + timedelta(days=9),
            total_days=Decimal('3'),
            status='pending',
        )

        notifs = Notification.objects.filter(notification_type='leave_request_submitted')
        self.assertEqual(list(notifs.values_list('user_id', flat=True)), [manager.id])
        self.assertEqual(notifs.first().link_url, f'/leave/team-requests?id={lr.id}')

    @_ws_patch
    @_push_patch
    def test_leave_request_created_inactive_manager_falls_back_to_staff(self, mock_incr, mock_async):
        from leave_management.models import LeaveRequest

        manager = self.create_user(
            email='leave-sig-mgr-gone@test.com', role='agent', is_active=False
        )
        self.employee.manager = manager
        self.employee.save()
        lt = self._make_leave_type('VSIG5')

        LeaveRequest.objects.create(
            tenant=self.tenant,
            employee=self.employee,
            leave_type=lt,
            start_date=date.today() + timedelta(days=7),
            end_date=date.today() + timedelta(days=9),
            total_days=Decimal('3'),
            status='pending',
        )

        notifs = Notification.objects.filter(notification_type='leave_request_submitted')
        recipients = set(notifs.values_list('user_id', flat=True))
        self.assertIn(self.admin.id, recipients)
        self.assertNotIn(manager.id, recipients)

    @_ws_patch
    @_push_patch
    def test_staff_requester_is_not_notified_about_own_request(self, mock_incr, mock_async):
        from leave_management.models import LeaveRequest

        lt = self._make_leave_type('VSIG6')
        LeaveRequest.objects.create(
            tenant=self.tenant,
            employee=self.admin,
            leave_type=lt,
            start_date=date.today() + timedelta(days=7),
            end_date=date.today() + timedelta(days=9),
            total_days=Decimal('3'),
            status='pending',
        )
        notifs = Notification.objects.filter(
            notification_type='leave_request_submitted', user=self.admin
        )
        self.assertFalse(notifs.exists())

    @_ws_patch
    @_push_patch
    def test_leave_approved_notifies_employee(self, mock_incr, mock_async):
        from leave_management.models import LeaveType, LeaveRequest

        lt = LeaveType.objects.create(
            tenant=self.tenant,
            code='VSIG2',
            name={'en': 'Vacation'},
            is_paid=True,
            requires_approval=True,
            calculation_method='annual',
            default_days_per_year=Decimal('20'),
            accrual_rate_per_month=Decimal('0'),
            color='#3B82F6',
            is_active=True,
            sort_order=0,
            created_by=self.admin,
            updated_by=self.admin,
        )

        lr = LeaveRequest.objects.create(
            tenant=self.tenant,
            employee=self.employee,
            leave_type=lt,
            start_date=date.today() + timedelta(days=10),
            end_date=date.today() + timedelta(days=12),
            total_days=Decimal('3'),
            status='pending',
        )
        Notification.objects.all().delete()

        lr.status = 'approved'
        lr.save()

        notifs = Notification.objects.filter(
            notification_type='leave_request_approved',
            user=self.employee,
        )
        self.assertTrue(notifs.exists())
        self.assertEqual(notifs.first().link_url, f'/leave/my-requests?id={lr.id}')

    @_ws_patch
    @_push_patch
    def test_leave_rejected_notifies_employee(self, mock_incr, mock_async):
        from leave_management.models import LeaveType, LeaveRequest

        lt = LeaveType.objects.create(
            tenant=self.tenant,
            code='VSIG3',
            name={'en': 'Vacation'},
            is_paid=True,
            requires_approval=True,
            calculation_method='annual',
            default_days_per_year=Decimal('20'),
            accrual_rate_per_month=Decimal('0'),
            color='#3B82F6',
            is_active=True,
            sort_order=0,
            created_by=self.admin,
            updated_by=self.admin,
        )

        lr = LeaveRequest.objects.create(
            tenant=self.tenant,
            employee=self.employee,
            leave_type=lt,
            start_date=date.today() + timedelta(days=15),
            end_date=date.today() + timedelta(days=17),
            total_days=Decimal('3'),
            status='pending',
        )
        Notification.objects.all().delete()

        lr.status = 'rejected'
        lr.rejection_reason = 'Staffing constraints'
        lr.save()

        notifs = Notification.objects.filter(
            notification_type='leave_request_rejected',
            user=self.employee,
        )
        self.assertTrue(notifs.exists())
        self.assertEqual(notifs.first().link_url, f'/leave/my-requests?id={lr.id}')


class TestBookingSignals(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.staff_user = self.create_user(email='book-sig-staff@test.com', role='agent')

    @_ws_patch
    @_push_patch
    def test_booking_confirmed_notifies_staff(self, mock_incr, mock_async):
        from booking_management.models import (
            ServiceCategory, Service, BookingStaff, Booking,
        )
        from social_integrations.models import Client as SocialClient

        # Create required linked objects
        booking_staff = BookingStaff.objects.create(
            user=self.staff_user,
            is_active_for_bookings=True,
        )
        category = ServiceCategory.objects.create(
            name={'en': 'Test'},
            is_active=True,
        )
        service = Service.objects.create(
            name={'en': 'Haircut'},
            category=category,
            base_price=Decimal('30.00'),
            duration_minutes=30,
        )
        social_client = SocialClient.objects.create(name='John Doe')

        booking = Booking.objects.create(
            client=social_client,
            service=service,
            staff=booking_staff,
            date=date.today() + timedelta(days=3),
            start_time='10:00',
            end_time='10:30',
            status='pending',
            total_amount=Decimal('30.00'),
        )
        Notification.objects.all().delete()

        booking.status = 'confirmed'
        booking.save()

        notifs = Notification.objects.filter(
            notification_type='booking_confirmed',
            user=self.staff_user,
        )
        self.assertTrue(notifs.exists())
        self.assertIn('confirmed', notifs.first().title)


class TestCallSignals(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='call-sig-agent@test.com', role='agent')

    @_ws_patch
    @_push_patch
    def test_missed_inbound_call_notifies_handler(self, mock_incr, mock_async):
        from crm.models import CallLog

        call = CallLog.objects.create(
            caller_number='+995555123456',
            recipient_number='+995555654321',
            direction='inbound',
            status='missed',
            handled_by=self.agent,
        )

        notifs = Notification.objects.filter(
            notification_type='call_missed',
            user=self.agent,
        )
        self.assertTrue(notifs.exists())
        self.assertIn('+995555123456', notifs.first().message)

    @_ws_patch
    @_push_patch
    def test_missed_inbound_without_handler_notifies_staff(self, mock_incr, mock_async):
        """No handler and no InboundRoute for the DID: PBX admins (staff)
        are notified, not arbitrary agents."""
        from crm.models import CallLog

        staff = self.create_admin(email='call-sig-staff@test.com')
        call = CallLog.objects.create(
            caller_number='+995555111111',
            recipient_number='+995555222222',
            direction='inbound',
            status='missed',
            handled_by=None,
        )

        notifs = Notification.objects.filter(notification_type='call_missed')
        self.assertTrue(notifs.filter(user=staff).exists())
        self.assertFalse(notifs.filter(user=self.agent).exists())
        self.assertEqual(notifs.filter(user=staff).first().link_url, f'/calls?id={call.id}')

    @_ws_patch
    @_push_patch
    def test_outbound_missed_call_does_not_notify(self, mock_incr, mock_async):
        from crm.models import CallLog

        Notification.objects.all().delete()
        call = CallLog.objects.create(
            caller_number='+995555333333',
            recipient_number='+995555444444',
            direction='outbound',
            status='missed',
        )

        notifs = Notification.objects.filter(notification_type='call_missed')
        self.assertFalse(notifs.exists())

    @_ws_patch
    @_push_patch
    def test_inbound_answered_call_does_not_notify(self, mock_incr, mock_async):
        from crm.models import CallLog

        Notification.objects.all().delete()
        call = CallLog.objects.create(
            caller_number='+995555555555',
            recipient_number='+995555666666',
            direction='inbound',
            status='answered',
        )

        notifs = Notification.objects.filter(notification_type='call_missed')
        self.assertFalse(notifs.exists())
