"""
Tests for ``manage.py seed_demo_tenant``.

The command creates a real ``demo`` schema (migrated from scratch) inside the
test transaction, so everything it writes is rolled back afterwards.
"""
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from tenant_schemas.utils import get_public_schema_name, schema_context, schema_exists

from tenants.demo_seed import DEMO_PASSWORD
from tenants.feature_models import Feature
from tenants.management.commands.seed_demo_tenant import Command as SeedCommand
from tenants.models import SocialPlatformRoute, Tenant, TenantSubscription
from users.tests.conftest import EchoDeskTenantTestCase


def _seed(*args):
    out = StringIO()
    call_command('seed_demo_tenant', *args, stdout=out)
    return out.getvalue()


class SeedDemoTenantTests(EchoDeskTenantTestCase):

    def _counts(self):
        from tenants.demo_seed import demo_counts
        tenant = Tenant.objects.get(schema_name='demo')
        with schema_context('demo'):
            return demo_counts(tenant)

    def test_seed_twice_is_idempotent_and_fills_every_module(self):
        # Push delivery must never be reached while seeding.
        with mock.patch('notifications.tasks.deliver_push_notification_task.delay') as push:
            first_out = _seed()
            first = self._counts()
            second_out = _seed()
            second = self._counts()
        push.assert_not_called()

        self.assertIn(DEMO_PASSWORD, first_out)
        self.assertIn('admin@demo.ge', first_out)
        self.assertIn('already seeded', second_out)
        self.assertEqual(first, second, 'second run must not create or delete any row')

        # Per-module row counts
        expected_exact = {
            'users': 9, 'boards': 2, 'tickets': 30, 'leave_requests': 10, 'invoices': 8,
            'bookings': 25, 'products': 20, 'promo_codes': 3, 'shipping_methods': 2,
            'ecommerce_clients': 15, 'orders': 25, 'wa_templates': 3, 'quick_replies': 5,
            'team_chat_conversations': 4, 'widget_sessions': 2,
        }
        for key, value in expected_exact.items():
            self.assertEqual(first[key], value, key)
        expected_min = {
            'columns': 9, 'tags': 6, 'ticket_comments': 20, 'checklist_items': 10, 'ticket_attachments': 3,
            'time_logs': 5, 'leave_types': 4, 'leave_balances': 20, 'holidays': 10, 'invoice_payments': 3,
            'service_categories': 2, 'services': 5, 'booking_staff': 3, 'staff_availability': 18,
            'languages': 2, 'variants': 8, 'reviews': 10, 'social_clients': 20, 'facebook_messages': 5,
            'instagram_messages': 5, 'whatsapp_messages': 5, 'telegram_messages': 3, 'widget_messages': 3,
            'phone_assignments': 5, 'call_logs': 15, 'call_recordings': 3, 'team_chat_messages': 8,
            'notifications': 24,
        }
        for key, value in expected_min.items():
            self.assertGreaterEqual(first[key], value, key)

        tenant = Tenant.objects.get(schema_name='demo')
        self.assertEqual(tenant.domain_url, 'demo.api.lvh.me')
        self.assertEqual(tenant.deployment_status, 'deployed')  # never picked up by process_pending_tenants
        sub = TenantSubscription.objects.get(tenant=tenant)
        self.assertTrue(sub.is_active)
        self.assertEqual(
            set(sub.selected_features.values_list('key', flat=True)),
            set(Feature.objects.filter(is_active=True).values_list('key', flat=True)),
        )
        from tenants.management.commands.seed_demo_tenant import BASELINE_FEATURES
        self.assertTrue(
            {k for k, *_ in BASELINE_FEATURES} <= set(sub.selected_features.values_list('key', flat=True)))
        # post_migrate ran in the new schema (content types + permissions)
        from django.contrib.contenttypes.models import ContentType
        with schema_context('demo'):
            self.assertTrue(ContentType.objects.filter(app_label='tickets', model='ticket').exists())
        self.assertTrue(SocialPlatformRoute.objects.filter(tenant_schema='demo', platform='facebook').exists())

        self._assert_module_details()

    def _assert_module_details(self):
        from booking_management.models import Booking
        from crm.models import CallLog
        from ecommerce_crm.models import Language, Order, Product
        from invoices.models import Invoice
        from leave_management.models import LeaveRequest
        from social_integrations.models import (
            FacebookPageConnection, TelegramAccount, WhatsAppBusinessAccount,
        )
        from tickets.models import TicketColumn
        from users.models import Notification, User
        from widget_registry.models import WidgetConnection

        with schema_context('demo'):
            admin = User.objects.get(email='admin@demo.ge')
            manager = User.objects.get(email='manager@demo.ge')
            agent = User.objects.get(email='agent@demo.ge')
            self.assertTrue(admin.is_superuser and admin.is_staff)
            self.assertEqual(admin.role, 'admin')
            self.assertEqual(manager.role, 'manager')
            self.assertEqual(agent.role, 'agent')
            self.assertEqual(agent.manager_id, manager.id)
            for u in User.objects.all():
                self.assertTrue(u.check_password(DEMO_PASSWORD), u.email)
            # Non-superusers get every feature through their group.
            self.assertTrue(agent.get_feature_keys())
            self.assertEqual(set(agent.get_feature_keys()), set(admin.get_feature_keys()))

            self.assertTrue(TicketColumn.objects.filter(track_time=True).exists())
            self.assertEqual(
                set(LeaveRequest.objects.values_list('status', flat=True)),
                {'pending', 'manager_approved', 'hr_approved', 'approved', 'rejected', 'cancelled'},
            )
            self.assertGreaterEqual(len(set(Invoice.objects.values_list('status', flat=True))), 6)
            self.assertTrue(Invoice.objects.filter(status='paid', payments__isnull=False).exists())
            self.assertTrue(Invoice.objects.filter(status='partially_paid').exists())
            self.assertEqual(
                set(Order.objects.values_list('status', flat=True)),
                {'pending', 'confirmed', 'processing', 'shipped', 'delivered', 'cancelled', 'refunded'},
            )
            self.assertEqual(Product.objects.filter(variants__isnull=False).distinct().count(), 2)
            self.assertEqual(set(Language.objects.values_list('code', flat=True)) >= {'en', 'ka'}, True)

            from django.utils import timezone
            today = timezone.localdate()
            self.assertTrue(Booking.objects.filter(date__lt=today).exists())
            self.assertTrue(Booking.objects.filter(date__gt=today).exists())
            self.assertTrue(Booking.objects.filter(date__gte=today - timezone.timedelta(days=7),
                                                   date__lte=today + timezone.timedelta(days=14)).count() == 25)

            self.assertEqual(set(CallLog.objects.values_list('direction', flat=True)), {'inbound', 'outbound'})
            self.assertTrue(CallLog.objects.filter(status='missed', direction='inbound').exists())

            for conn in (FacebookPageConnection.objects.get(), WhatsAppBusinessAccount.objects.get()):
                token = getattr(conn, 'page_access_token', None) or conn.access_token
                self.assertIn('demo-fake', token)
            self.assertIn('demo-fake', TelegramAccount.objects.get().session_string)

            self.assertGreaterEqual(
                Notification.objects.values('notification_type').distinct().count(), 6)
            self.assertTrue(Notification.objects.filter(user=agent).exists())

        self.assertEqual(WidgetConnection.objects.filter(tenant_schema='demo').count(), 1)
        from help_center.models import HelpArticle
        self.assertTrue(HelpArticle.objects.exists())

    def test_reset_rebuilds_the_schema(self):
        _seed()
        before = self._counts()
        out = _seed('--reset')
        self.assertIn('Dropping schema', out)
        self.assertTrue(schema_exists('demo'))
        after = self._counts()
        self.assertEqual(after['tickets'], 30)
        self.assertEqual(after['orders'], 25)
        self.assertEqual(after['users'], before['users'])
        from widget_registry.models import WidgetConnection
        self.assertEqual(WidgetConnection.objects.filter(tenant_schema='demo').count(), 1)

    def test_refuses_to_seed_a_tenant_it_did_not_create(self):
        # The shared test tenant ("test") belongs to someone else.
        with self.assertRaises(CommandError):
            call_command('seed_demo_tenant', '--schema', 'test', stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command('seed_demo_tenant', '--schema', 'test', '--reset', stdout=StringIO())
        self.assertTrue(schema_exists('test'))

    def test_rejects_public_and_invalid_schema(self):
        for bad in (get_public_schema_name(), 'drop table;'):
            with self.assertRaises(CommandError):
                call_command('seed_demo_tenant', '--schema', bad, stdout=StringIO())

    def test_help_documents_the_password(self):
        self.assertIn(DEMO_PASSWORD, SeedCommand.help)
        self.assertIn('admin@demo.ge', SeedCommand.help)
        self.assertEqual(connection.schema_name, 'test')
