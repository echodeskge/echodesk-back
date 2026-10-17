"""
Notification callers render their texts in each recipient's language
(tickets, leave, e-commerce, social, bookings). See test_notification_language
for the core mechanism.
"""
from datetime import date, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from users.models import Notification
from tickets.tests.conftest import TicketTestCase

_ws_patch = patch('asgiref.sync.async_to_sync', return_value=lambda f: (lambda **kw: None))
_tickets_ws_patch = patch('tickets.signals.async_to_sync', return_value=lambda **kw: None)


@_ws_patch
@_tickets_ws_patch
class NotificationCallerLanguageTests(TicketTestCase):
    def setUp(self):
        super().setUp()
        self._original = self.tenant.preferred_language
        self.tenant.preferred_language = 'en'
        self.tenant.save(update_fields=['preferred_language'])
        self.ka_user = self.create_user(email='caller-ka@test.com', preferred_language='ka',
                                        first_name='Nino', last_name='K')
        self.en_user = self.create_user(email='caller-en@test.com', preferred_language='en',
                                        first_name='John', last_name='D')

    def tearDown(self):
        self.tenant.preferred_language = self._original
        self.tenant.save(update_fields=['preferred_language'])
        super().tearDown()

    # --- Tickets ---------------------------------------------------------
    def test_ticket_assignment_and_comment(self, *_):
        from tickets.models import TicketAssignment, TicketComment

        admin = self.create_admin(email='caller-admin@test.com')
        ticket = self.create_ticket(title='Printer broken', created_by=admin)
        TicketAssignment.objects.create(ticket=ticket, user=self.ka_user, role='reviewer', assigned_by=admin)
        TicketAssignment.objects.create(ticket=ticket, user=self.en_user, role='reviewer', assigned_by=admin)

        ka = Notification.objects.get(user=self.ka_user, notification_type='ticket_assigned')
        en = Notification.objects.get(user=self.en_user, notification_type='ticket_assigned')
        self.assertEqual(ka.title, 'დაგენიშნათ ტიკეტი: Printer broken')
        self.assertEqual(ka.message, f'{admin.get_full_name()} — დაგნიშნათ ამ ტიკეტზე როლით „შემმოწმებელი“')
        self.assertEqual(en.title, 'Assigned to: Printer broken')
        self.assertEqual(en.message, f'{admin.get_full_name()} assigned you to this ticket as Reviewer')

        TicketComment.objects.create(ticket=ticket, user=admin, comment='Looking into it')
        ka = Notification.objects.get(user=self.ka_user, notification_type='ticket_commented')
        en = Notification.objects.get(user=self.en_user, notification_type='ticket_commented')
        self.assertEqual(ka.title, 'ახალი კომენტარი: Printer broken')
        self.assertEqual(en.title, 'New comment on: Printer broken')

    # --- Leave -----------------------------------------------------------
    def test_leave_request_texts(self, *_):
        from leave_management.models import LeaveRequest, LeaveType

        admin = self.create_admin(email='caller-leave-admin@test.com')
        self.en_user.manager = self.ka_user
        self.en_user.save(update_fields=['manager'])
        lt = LeaveType.objects.create(
            tenant=self.tenant, code='LANG', name={'en': 'Vacation', 'ka': 'შვებულება'},
            is_paid=True, requires_approval=True, calculation_method='annual',
            default_days_per_year=Decimal('20'), accrual_rate_per_month=Decimal('0'),
            color='#3B82F6', is_active=True, sort_order=0, created_by=admin, updated_by=admin,
        )
        start = date(2030, 1, 10)
        lr = LeaveRequest.objects.create(
            tenant=self.tenant, employee=self.en_user, leave_type=lt,
            start_date=start, end_date=start + timedelta(days=2),
            total_days=Decimal('3'), status='pending',
        )
        manager_notice = Notification.objects.get(user=self.ka_user, notification_type='leave_request_submitted')
        self.assertEqual(manager_notice.title, 'შვებულების ახალი მოთხოვნა')
        self.assertEqual(manager_notice.message, 'John D — გამოაგზავნა შვებულების მოთხოვნა (შვებულება)')
        self.assertEqual(manager_notice.metadata['leave_type'], 'Vacation')  # metadata unchanged

        lr.status = 'approved'
        lr.save()
        employee_notice = Notification.objects.get(user=self.en_user, notification_type='leave_request_approved')
        self.assertEqual(employee_notice.title, 'Leave request approved')
        self.assertEqual(employee_notice.message,
                         'Your leave request for 2030-01-10 - 2030-01-12 has been approved')

    # --- E-commerce ------------------------------------------------------
    def test_low_stock_digest_mixed_languages(self, *_):
        from ecommerce_crm import notifications

        product = SimpleNamespace(id=1, sku='SKU-1', quantity=2, low_stock_threshold=5,
                                  name={'en': 'Shirt', 'ka': 'მაისური'})
        with patch.object(notifications, 'ecommerce_notification_recipients',
                          return_value=[self.ka_user, self.en_user]):
            self.assertEqual(notifications.notify_low_stock([product]), 2)
        ka = Notification.objects.get(user=self.ka_user, notification_type='ecommerce_low_stock')
        en = Notification.objects.get(user=self.en_user, notification_type='ecommerce_low_stock')
        self.assertEqual(ka.title, 'დაბალი მარაგი: მაისური')
        self.assertEqual(ka.message, 'SKU-1 — დარჩა 2 (ზღვარი: 5)')
        self.assertEqual(en.title, 'Low stock: Shirt')
        self.assertEqual(en.message, 'SKU-1 has 2 left (alert at 5)')
        self.assertEqual(ka.link_url, en.link_url)
        self.assertEqual(ka.metadata, en.metadata)

    def test_low_stock_many(self, *_):
        from ecommerce_crm import notifications

        products = [
            SimpleNamespace(id=i, sku=f'S{i}', quantity=0, low_stock_threshold=1,
                            name={'en': f'P{i}', 'ka': f'პ{i}'})
            for i in range(1, 5)
        ]
        with patch.object(notifications, 'ecommerce_notification_recipients', return_value=[self.ka_user]):
            notifications.notify_low_stock(products)
        n = Notification.objects.get(user=self.ka_user, notification_type='ecommerce_low_stock')
        self.assertEqual(n.title, '4 პროდუქტს დაბალი მარაგი აქვს')
        self.assertEqual(n.message, 'პ1, პ2, პ3 და კიდევ 1')

    # --- Social ----------------------------------------------------------
    def test_whatsapp_template_status(self, *_):
        from social_integrations.notifications import notify_whatsapp_template_status

        template = SimpleNamespace(
            id=3, name='welcome', language='ka', status='REJECTED', created_by=None,
            business_account=SimpleNamespace(waba_id='w1'),
        )
        with patch('users.notification_utils.social_admin_recipients',
                   return_value=[self.ka_user, self.en_user]):
            notify_whatsapp_template_status(template, 'PENDING', reason='INVALID_FORMAT')
        ka = Notification.objects.get(user=self.ka_user, notification_type='whatsapp_template_status')
        en = Notification.objects.get(user=self.en_user, notification_type='whatsapp_template_status')
        self.assertEqual(ka.title, 'WhatsApp შაბლონი განხილულია')
        self.assertEqual(ka.message, 'შაბლონის „welcome“ (ka) სტატუსი: უარყოფილი. მიზეზი: INVALID_FORMAT')
        self.assertEqual(en.message, 'Template "welcome" (ka) is now rejected. Reason: INVALID_FORMAT')

    def test_auto_post_fallback_localized_content_kept(self, *_):
        from social_integrations.notifications import notify_auto_post

        post = SimpleNamespace(id=4, status='draft', scheduled_for=None,
                               facebook_text='', instagram_text='', error_message='')
        with patch('users.notification_utils.social_admin_recipients', return_value=[self.ka_user]):
            notify_auto_post(post, 'draft')
        n = Notification.objects.get(user=self.ka_user, notification_type='auto_post_draft')
        self.assertEqual(n.title, 'ახალი AI პოსტი ელოდება დამტკიცებას')
        self.assertEqual(n.message, 'ავტომატურად შექმნილი პოსტი თქვენს განხილვას საჭიროებს.')

        Notification.objects.all().delete()
        post.facebook_text = 'Spring sale!'
        with patch('users.notification_utils.social_admin_recipients', return_value=[self.ka_user]):
            notify_auto_post(post, 'draft')
        n = Notification.objects.get(user=self.ka_user, notification_type='auto_post_draft')
        self.assertEqual(n.message, 'Spring sale!')

    # --- Bookings --------------------------------------------------------
    def test_booking_staff_notice_follows_recipient_not_tenant(self, *_):
        from booking_management.staff_notifications import notify_assigned_staff

        booking = SimpleNamespace(
            id=11, booking_number='B-11', date=date(2030, 2, 1), start_time=time(10, 30),
            cancelled_by='', staff=SimpleNamespace(user=self.ka_user),
            client=SimpleNamespace(full_name='Ana', name='', phone=''),
            service=SimpleNamespace(name={'en': 'Haircut', 'ka': 'თმის შეჭრა'}),
        )
        self.assertTrue(notify_assigned_staff(booking, 'confirmed'))
        n = Notification.objects.get(user=self.ka_user, notification_type='booking_confirmed')
        self.assertEqual(n.title, 'ჯავშანი დადასტურდა')
        self.assertEqual(n.message, 'Ana — თმის შეჭრა, 01.02.2030 10:30.')

        booking.staff = SimpleNamespace(user=self.en_user)
        self.assertTrue(notify_assigned_staff(booking, 'reminder', minutes=15))
        n = Notification.objects.get(user=self.en_user, notification_type='booking_reminder')
        self.assertEqual(n.title, 'Next client in 15 min')
        self.assertEqual(n.message, 'Ana — Haircut, 01.02.2030 10:30.')
