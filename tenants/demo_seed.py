"""
Demo tenant seed data (used by ``manage.py seed_demo_tenant``).

Builds a realistic, fully-featured tenant for local development, screenshots
and the mobile app's end-to-end flows. Everything is created with the real
models; nothing here talks to an external service (social / e-mail / PBX
connections are plain DB rows with fake tokens, push delivery is muted while
seeding).

Idempotency: every module seeder first checks for its own marker rows and
skips itself when they already exist, so re-running the command never
duplicates data. ``--reset`` (handled by the command) drops the schema for a
clean slate.
"""
from __future__ import annotations

import contextlib
import random
import secrets
import zlib
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.hashers import make_password
from django.db import connection
from django.utils import timezone
from tenant_schemas.utils import get_public_schema_name, schema_context

DEMO_PASSWORD = 'Demo12345!'
DEMO_EMAIL_DOMAIN = 'demo.ge'
DEMO_MARKER = 'echodesk-demo-seed'

# (email local part, first, last, role, extra flags)
CORE_USERS = [
    ('admin', 'Nino', 'Beridze', 'admin', {'is_staff': True, 'is_superuser': True}),
    ('manager', 'Giorgi', 'Kapanadze', 'manager', {}),
    ('agent', 'Mariam', 'Lomidze', 'agent', {}),
]
EXTRA_STAFF = [
    ('staff1', 'Luka', 'Tsiklauri', 'agent'),
    ('staff2', 'Ana', 'Javakhishvili', 'agent'),
    ('staff3', 'Davit', 'Gelashvili', 'agent'),
    ('staff4', 'Salome', 'Abashidze', 'agent'),
    ('staff5', 'Irakli', 'Chkheidze', 'manager'),
    ('staff6', 'Tamar', 'Kvaratskhelia', 'viewer'),
]

CUSTOMER_NAMES = [
    ('Levan', 'Maisuradze'), ('Ketevan', 'Gogoladze'), ('Zurab', 'Japaridze'),
    ('Elene', 'Bakradze'), ('Nikoloz', 'Kiknadze'), ('Sophia', 'Mchedlidze'),
    ('Tornike', 'Shengelia'), ('Natia', 'Dolidze'), ('Beka', 'Gvaramia'),
    ('Mariami', 'Tsereteli'), ('Saba', 'Kobakhidze'), ('Lika', 'Khutsishvili'),
    ('Dachi', 'Papava'), ('Anano', 'Nadiradze'), ('Gela', 'Sharashidze'),
]


def demo_email(local):
    return f'{local}@{DEMO_EMAIL_DOMAIN}'


def _aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


def _backdate(obj, **fields):
    """Rewrite auto_now / auto_now_add timestamps after the row exists."""
    type(obj).objects.filter(pk=obj.pk).update(**fields)
    for k, v in fields.items():
        setattr(obj, k, v)


def _clean_save(obj, exclude=None):
    """full_clean() then save(): seed rows must pass model validation too."""
    obj.full_clean(exclude=exclude)
    obj.save()
    return obj


@contextlib.contextmanager
def muted_push():
    """Seeding must never push to a real phone / browser."""
    with mock.patch('users.notification_utils._dispatch_push', lambda **kw: None):
        yield


class DemoSeeder:
    """Seeds one tenant schema. Call :meth:`run` inside ``schema_context``."""

    def __init__(self, tenant, stdout=None, seed=42):
        self.tenant = tenant
        self.schema = tenant.schema_name
        self.stdout = stdout
        self.rng = random.Random(seed)
        self.now = timezone.now()
        self.today = timezone.localdate()
        self.password_hash = make_password(DEMO_PASSWORD)
        self.report = []  # [(module, 'created' | 'skipped')]

    # ------------------------------------------------------------------ utils
    def log(self, msg):
        if self.stdout is not None:
            self.stdout.write(msg)

    def _module(self, name, already_seeded, fn):
        if already_seeded:
            self.report.append((name, 'skipped'))
            self.log(f'  - {name}: already seeded, skipping')
            return
        fn()
        self.report.append((name, 'created'))
        self.log(f'  + {name}: seeded')

    def run(self):
        assert connection.schema_name == self.schema, 'run() must be called inside the tenant schema'
        from booking_management.models import Service
        from crm.models import CallLog
        from ecommerce_crm.models import Product
        from invoices.models import Invoice
        from leave_management.models import LeaveType
        from social_integrations.models import FacebookPageConnection
        from tickets.models import Board
        from users.models import Notification, TeamChatConversation

        with muted_push():
            self.seed_users()  # always: get_or_create, cheap, needed by everything
            self._module('tickets', Board.objects.filter(name='Customer Support').exists(), self.seed_tickets)
            self._module('leave', LeaveType.objects.filter(tenant=self.tenant, code='VAC').exists(), self.seed_leave)
            self._module('ecommerce', Product.objects.filter(sku='DEMO-001').exists(), self.seed_ecommerce)
            self._module('invoices', Invoice.objects.filter(invoice_number__startswith='DEMO-').exists(), self.seed_invoices)
            self._module('bookings', Service.objects.filter(name__en='Classic haircut').exists(), self.seed_bookings)
            self._module('social', FacebookPageConnection.objects.filter(page_id=self._ext('fb-page')).exists(), self.seed_social)
            self._module('calls', CallLog.objects.filter(notes__startswith='[demo]').exists(), self.seed_calls)
            self._module('team_chat', TeamChatConversation.objects.filter(participants=self.admin).exists(), self.seed_team_chat)
            self._module('notifications', Notification.objects.filter(metadata__demo_seed=True).exists(), self.seed_notifications)
        with schema_context(get_public_schema_name()):
            from help_center.models import HelpCategory
            self._module('help_center', HelpCategory.objects.exists(), self.seed_help_center)
        return self.report

    def _ext(self, suffix):
        """External ids (page ids, tokens) unique per seeded schema."""
        return f'{self.schema}-{suffix}'

    # ------------------------------------------------------------------ users
    def seed_users(self):
        from tenants.feature_models import Feature
        from users.models import Department, TenantGroup, User

        departments = {}
        for name in ('Support', 'Sales', 'Operations'):
            departments[name], _ = Department.objects.get_or_create(
                name=name, defaults={'description': f'{name} department'}
            )

        everyone, created = TenantGroup.objects.get_or_create(
            name='All features',
            defaults={'description': 'Demo group with every subscription feature'},
        )
        everyone.features.set(Feature.objects.filter(is_active=True))
        booking_group, _ = TenantGroup.objects.get_or_create(
            name='Booking staff',
            defaults={'description': 'Staff who take bookings', 'is_booking_staff': True},
        )
        support_group, _ = TenantGroup.objects.get_or_create(
            name='Support team', defaults={'description': 'First-line support'},
        )
        self.groups = {'everyone': everyone, 'booking': booking_group, 'support': support_group}

        users = {}
        specs = [(l, f, la, r, x) for (l, f, la, r, x) in CORE_USERS]
        specs += [(l, f, la, r, {}) for (l, f, la, r) in EXTRA_STAFF]
        dept_cycle = ['Support', 'Sales', 'Operations']
        for idx, (local, first, last, role, extra) in enumerate(specs):
            email = demo_email(local)
            user = User.objects.filter(email=email).first()
            if user is None:
                user = User(
                    email=email, first_name=first, last_name=last, role=role,
                    phone_number=f'+99559900{idx:04d}',
                    job_title={'admin': 'Owner', 'manager': 'Team lead', 'agent': 'Support agent',
                               'viewer': 'Accountant'}[role],
                    department=departments[dept_cycle[idx % 3]],
                    is_staff=extra.get('is_staff', False),
                    is_superuser=extra.get('is_superuser', False),
                    can_make_calls=role != 'viewer',
                    can_view_social_messages=role != 'viewer',
                    can_send_social_messages=role in ('agent', 'manager', 'admin'),
                    can_access_orders=True,
                )
                user.password = self.password_hash
                user.save()
            users[local] = user

        self.admin = users['admin']
        self.manager = users['manager']
        self.agent = users['agent']
        self.users = users
        self.staff = [users[l] for (l, *_rest) in EXTRA_STAFF]
        self.all_users = list(users.values())

        # Leave reporting lines: agent + staff1..4 report to the manager,
        # the manager reports to the admin.
        for u in [self.agent] + self.staff[:4]:
            if u.manager_id != self.manager.id:
                u.manager = self.manager
                u.save(update_fields=['manager'])
        if self.manager.manager_id != self.admin.id:
            self.manager.manager = self.admin
            self.manager.save(update_fields=['manager'])

        for u in self.all_users:
            u.tenant_groups.add(everyone)
        for u in (self.agent, users['staff1'], users['staff2']):
            u.tenant_groups.add(booking_group)
        for u in (self.agent, users['staff1'], users['staff3'], self.manager):
            u.tenant_groups.add(support_group)
        for u in self.all_users:
            if u.primary_group_id is None:
                u.primary_group = everyone
                u.save(update_fields=['primary_group'])

    # ---------------------------------------------------------------- tickets
    def seed_tickets(self):
        from tickets.models import (
            Board, ChecklistItem, Tag, Ticket, TicketAssignment, TicketAttachment,
            TicketColumn, TicketComment, TicketHistory, TicketPayment, TicketTimeLog,
        )

        rng = self.rng
        support = Board(name='Customer Support', description='Customer issues and requests', is_default=True,
                        created_by=self.admin)
        _clean_save(support)
        sales = Board(name='Sales Pipeline', description='Leads and deals', created_by=self.manager)
        _clean_save(sales)
        for board in (support, sales):
            board.board_groups.add(self.groups['everyone'])
            board.board_users.add(*self.all_users)

        def columns(board, specs):
            cols = []
            for pos, (name, color, extra) in enumerate(specs):
                col = TicketColumn(name=name, color=color, position=pos, board=board,
                                   created_by=board.created_by, **extra)
                _clean_save(col)
                cols.append(col)
            return cols

        sup_cols = columns(support, [
            ('To Do', '#6B7280', {'is_default': True}),
            ('In Progress', '#3B82F6', {'track_time': True}),
            ('Waiting on Customer', '#F59E0B', {}),
            ('Done', '#10B981', {'is_closed_status': True}),
        ])
        sales_cols = columns(sales, [
            ('New Lead', '#8B5CF6', {'is_default': True}),
            ('Qualified', '#3B82F6', {}),
            ('Proposal Sent', '#F59E0B', {'track_time': True}),
            ('Won', '#10B981', {'is_closed_status': True}),
            ('Lost', '#EF4444', {'is_closed_status': True}),
        ])

        tags = {}
        for name, color in [('Bug', '#EF4444'), ('Feature request', '#3B82F6'), ('Urgent', '#DC2626'),
                            ('VIP', '#F59E0B'), ('Billing', '#10B981'), ('Follow-up', '#8B5CF6')]:
            tags[name] = _clean_save(Tag(name=name, color=color, created_by=self.admin))

        support_titles = [
            'Cannot log in to the customer portal', 'Order arrived damaged', 'Refund request for ORD-1007',
            'Change delivery address', 'Website checkout shows an error', 'Invoice copy requested',
            'Product size exchange', 'Booking confirmation SMS not received', 'Wrong item in the package',
            'Question about warranty', 'Promo code SUMMER10 does not apply', 'Update company details on invoice',
            'Instagram order follow-up', 'Damaged screen on delivery', 'Loyalty points missing',
            'Request to cancel subscription', 'Courier did not call before delivery', 'Bulk order quote',
        ]
        sales_titles = [
            'Tbilisi Coffee House - 20 seats', 'Batumi Hotel group onboarding', 'Kutaisi clinic pilot',
            'Online store migration', 'Beauty salon chain (5 branches)', 'Logistics company call center',
            'University admissions desk', 'Restaurant delivery integration', 'Fitness club memberships',
            'Real estate agency CRM', 'Pharmacy network support line', 'Car service booking system',
        ]
        comment_texts = [
            'Called the customer, waiting for photos.',
            'Escalated to the warehouse team.',
            '@{agent} can you take a look at this today?',
            'Customer confirmed the new address.',
            'Sent a follow-up e-mail with the details.',
            'Discount approved by @{manager}.',
        ]
        checklist_texts = ['Verify customer identity', 'Check order history', 'Prepare replacement',
                           'Notify courier', 'Close with summary']
        assignees = [self.agent, self.manager, self.users['staff1'], self.users['staff2'],
                     self.users['staff3'], self.admin]
        priorities = ['low', 'medium', 'medium', 'high', 'critical']

        specs = [(support, sup_cols, t) for t in support_titles] + [(sales, sales_cols, t) for t in sales_titles]
        for i, (board, cols, title) in enumerate(specs):
            col = cols[i % len(cols)]
            creator = [self.admin, self.manager, self.agent][i % 3]
            assignee = assignees[i % len(assignees)]
            created_at = self.now - timedelta(days=rng.randint(1, 30), hours=rng.randint(0, 23))
            ticket = Ticket(
                title=title,
                description=f'{title}. Reported via {["phone", "e-mail", "Facebook", "website chat"][i % 4]}.',
                priority=priorities[i % len(priorities)],
                column=col,
                created_by=creator,
                assigned_to=assignee,
                assigned_department=assignee.department,
            )
            if board is sales:
                price = Decimal(rng.choice([450, 900, 1500, 2400, 3600]))
                ticket.is_order = True
                ticket.price = price
                ticket.currency = 'GEL'
                ticket.payment_due_date = self.today + timedelta(days=rng.randint(-10, 30))
                if col.name == 'Won':
                    ticket.amount_paid = price
                elif i % 3 == 0:
                    ticket.amount_paid = (price / 2).quantize(Decimal('0.01'))
            _clean_save(ticket, exclude=['rich_description', 'metadata'])
            _backdate(ticket, created_at=created_at)

            TicketAssignment.objects.create(ticket=ticket, user=assignee, role='primary', assigned_by=creator)
            if i % 4 == 0:
                helper = assignees[(i + 1) % len(assignees)]
                if helper.id != assignee.id:
                    TicketAssignment.objects.create(ticket=ticket, user=helper, role='collaborator',
                                                    assigned_by=creator)
            if i % 5 == 0:
                ticket.assigned_groups.add(self.groups['support'])
            tag_names = list(tags)
            ticket.tags.add(tags[tag_names[i % len(tag_names)]])
            if i % 3 == 0:
                ticket.tags.add(tags[tag_names[(i + 2) % len(tag_names)]])

            for c in range(i % 4):
                author = [self.agent, self.manager, self.admin, self.users['staff1']][(i + c) % 4]
                text = comment_texts[(i + c) % len(comment_texts)].format(
                    agent=self.agent.email, manager=self.manager.email)
                comment = TicketComment.objects.create(ticket=ticket, user=author, comment=text)
                _backdate(comment, created_at=created_at + timedelta(hours=c + 1))

            for p in range(i % 5 if board is support else 0):
                ChecklistItem.objects.create(ticket=ticket, text=checklist_texts[p], is_checked=p < 2,
                                             position=p, created_by=creator)

            if i % 6 == 0:
                fname = ['photo-damage.jpg', 'invoice.pdf', 'screenshot.png'][i % 3]
                TicketAttachment.objects.create(
                    ticket=ticket, file=f'ticket_attachments/demo/{ticket.id}-{fname}', filename=fname,
                    file_size=rng.randint(40_000, 900_000),
                    content_type={'jpg': 'image/jpeg', 'pdf': 'application/pdf', 'png': 'image/png'}[fname[-3:]],
                    uploaded_by=creator,
                )

            # Time tracking: tickets that sit in, or already went through, the
            # track_time column get time logs (closed segment + open segment).
            tracked = next(c for c in cols if c.track_time)
            if col.position >= tracked.position:
                entered = created_at + timedelta(hours=2)
                if col.id == tracked.id:
                    log = TicketTimeLog.objects.create(ticket=ticket, column=tracked, user=assignee)
                    _backdate(log, entered_at=entered)
                else:
                    exited = entered + timedelta(hours=rng.randint(1, 30), minutes=rng.randint(0, 59))
                    log = TicketTimeLog.objects.create(
                        ticket=ticket, column=tracked, user=assignee, exited_at=exited,
                        duration_seconds=int((exited - entered).total_seconds()))
                    _backdate(log, entered_at=entered)
                    TicketHistory.objects.create(
                        ticket=ticket, action='status_changed', field_name='column',
                        old_value=tracked.name, new_value=col.name, user=assignee,
                        description=f'Moved from {tracked.name} to {col.name}')

            if ticket.amount_paid:
                TicketPayment.objects.create(ticket=ticket, amount=ticket.amount_paid, currency='GEL',
                                             payment_method='bank_transfer', processed_by=self.manager,
                                             payment_reference=f'DEMO-PAY-{ticket.id}')

    # ------------------------------------------------------------------ leave
    def seed_leave(self):
        from leave_management.models import (
            LeaveBalance, LeaveRequest, LeaveSettings, LeaveType, PublicHoliday,
        )
        from leave_management.utils import calculate_working_days

        tenant = self.tenant
        LeaveSettings.objects.update_or_create(
            tenant=tenant,
            defaults={'require_manager_approval': True, 'require_hr_approval': True,
                      'allow_negative_balance': False, 'working_days_per_week': 5, 'weekend_days': [5, 6]},
        )
        year = self.today.year
        holidays = [
            (1, 1, "New Year's Day", 'ახალი წელი'), (1, 2, 'New Year holiday', 'ბედობა'),
            (1, 7, 'Orthodox Christmas', 'შობა'), (1, 19, 'Epiphany', 'ნათლისღება'),
            (3, 3, "Mother's Day", 'დედის დღე'), (3, 8, "International Women's Day", 'ქალთა დღე'),
            (4, 9, 'National Unity Day', 'ეროვნული ერთიანობის დღე'), (5, 9, 'Victory Day', 'გამარჯვების დღე'),
            (5, 12, "St. Andrew's Day", 'ანდრია პირველწოდებულის დღე'),
            (5, 26, 'Independence Day', 'დამოუკიდებლობის დღე'), (8, 28, 'Mariamoba', 'მარიამობა'),
            (10, 14, 'Svetitskhovloba', 'სვეტიცხოვლობა'), (11, 23, 'Giorgoba', 'გიორგობა'),
        ]
        for m, d, en, ka in holidays:
            _clean_save(PublicHoliday(tenant=tenant, name={'en': en, 'ka': ka}, date=date(year, m, d),
                                      is_recurring=True, created_by=self.admin, updated_by=self.admin))

        types = {}
        for order, (code, en, ka, days, paid, color) in enumerate([
            ('VAC', 'Annual vacation', 'ყოველწლიური შვებულება', 24, True, '#3B82F6'),
            ('SICK', 'Sick leave', 'ავადმყოფობის შვებულება', 10, True, '#EF4444'),
            ('PERS', 'Personal day', 'პირადი დღე', 3, True, '#8B5CF6'),
            ('UNPAID', 'Unpaid leave', 'უხელფასო შვებულება', 0, False, '#6B7280'),
        ]):
            lt = LeaveType(tenant=tenant, name={'en': en, 'ka': ka}, code=code,
                           description={'en': f'{en} policy', 'ka': ka}, is_paid=paid,
                           calculation_method='annual' if days else 'manual',
                           default_days_per_year=Decimal(days), max_carry_forward_days=5 if code == 'VAC' else 0,
                           color=color, sort_order=order, created_by=self.admin, updated_by=self.admin)
            types[code] = _clean_save(lt)

        employees = [self.agent, self.manager] + self.staff[:4]
        # Ten requests across every status; dates never overlap per employee.
        base = self.today - timedelta(days=self.today.weekday())  # this Monday
        plan = [
            # (employee, type, start offset days, length days, status)
            (self.agent, 'VAC', 14, 5, 'pending'),
            (self.agent, 'SICK', -28, 2, 'approved'),
            (self.agent, 'PERS', 35, 1, 'manager_approved'),
            (self.users['staff1'], 'VAC', 21, 5, 'pending'),
            (self.users['staff1'], 'SICK', -14, 1, 'rejected'),
            (self.users['staff2'], 'VAC', -56, 5, 'approved'),
            (self.users['staff2'], 'UNPAID', 42, 2, 'cancelled'),
            (self.users['staff3'], 'PERS', 7, 1, 'hr_approved'),
            (self.users['staff4'], 'VAC', 28, 3, 'pending'),
            (self.manager, 'VAC', 49, 5, 'pending'),
        ]
        used = {}
        pending = {}
        for emp, code, offset, length, status in plan:
            start = base + timedelta(days=offset)
            end = start + timedelta(days=length - 1)
            if start.year != year:  # keep balances in one year for a clean demo
                start = date(year, 1, 12) if start.year < year else date(year, 12, 1)
                end = start + timedelta(days=length - 1)
            days = calculate_working_days(start, end, tenant) or Decimal('1')
            req = LeaveRequest(tenant=tenant, employee=emp, leave_type=types[code], start_date=start,
                               end_date=end, total_days=days, status=status,
                               reason={'VAC': 'Family trip', 'SICK': 'Flu', 'PERS': 'Moving apartments',
                                       'UNPAID': 'Personal matters'}[code])
            approver = self.manager if emp.id != self.manager.id else self.admin
            if status in ('manager_approved', 'hr_approved', 'approved'):
                req.manager_approver = approver
                req.manager_approved_at = self.now - timedelta(days=2)
                req.manager_comments = 'Enjoy!'
            if status in ('hr_approved', 'approved'):
                req.hr_approver = self.admin
                req.hr_approved_at = self.now - timedelta(days=1)
            if status == 'approved':
                req.final_approver = self.admin
                req.final_approved_at = self.now - timedelta(days=1)
            if status == 'rejected':
                req.rejected_by = approver
                req.rejected_at = self.now - timedelta(days=3)
                req.rejection_reason = 'Team is short-staffed that week'
            if status == 'cancelled':
                req.cancelled_at = self.now - timedelta(days=1)
                req.cancellation_reason = 'Plans changed'
            _clean_save(req)
            key = (emp.id, code)
            if status == 'approved':
                used[key] = used.get(key, Decimal('0')) + days
            elif status in ('pending', 'manager_approved', 'hr_approved'):
                pending[key] = pending.get(key, Decimal('0')) + days

        for emp in employees + [self.admin]:
            for code, lt in types.items():
                LeaveBalance.objects.create(
                    tenant=tenant, user=emp, leave_type=lt, year=year,
                    allocated_days=lt.default_days_per_year,
                    used_days=used.get((emp.id, code), Decimal('0')),
                    pending_days=pending.get((emp.id, code), Decimal('0')),
                    carried_forward_days=Decimal('2') if code == 'VAC' else Decimal('0'),
                )

    # -------------------------------------------------------------- ecommerce
    def seed_ecommerce(self):
        from ecommerce_crm.models import (
            AttributeDefinition, ClientAddress, EcommerceClient, EcommerceSettings, Language, Order,
            OrderItem, Product, ProductAttributeValue, ProductImage, ProductReview, ProductVariant,
            ProductVariantAttributeValue, PromoCode, ShippingMethod,
        )

        rng = self.rng
        Language.objects.update_or_create(code='en', defaults={
            'name': {'en': 'English', 'ka': 'ინგლისური'}, 'is_default': True, 'is_active': True, 'sort_order': 0})
        Language.objects.update_or_create(code='ka', defaults={
            'name': {'en': 'Georgian', 'ka': 'ქართული'}, 'is_default': False, 'is_active': True, 'sort_order': 1})
        EcommerceSettings.objects.get_or_create(tenant=self.tenant)

        color = AttributeDefinition.objects.create(
            key='color', name={'en': 'Color', 'ka': 'ფერი'}, attribute_type='multiselect', sort_order=0,
            options=[{'value': 'black', 'en': 'Black', 'ka': 'შავი'},
                     {'value': 'white', 'en': 'White', 'ka': 'თეთრი'},
                     {'value': 'red', 'en': 'Red', 'ka': 'წითელი'}])
        size = AttributeDefinition.objects.create(
            key='size', name={'en': 'Size', 'ka': 'ზომა'}, attribute_type='multiselect', sort_order=1,
            options=[{'value': v, 'en': v, 'ka': v} for v in ('S', 'M', 'L', 'XL', '40', '41', '42', '43')])

        catalog = [
            ('Cotton T-shirt', 'ბამბის მაისური', 35), ('Running sneakers', 'სარბენი ბოტასები', 220),
            ('Leather wallet', 'ტყავის საფულე', 85), ('Ceramic mug', 'კერამიკული ფინჯანი', 18),
            ('Churchkhela gift box', 'ჩურჩხელის საჩუქრის ყუთი', 40), ('Saperavi wine 0.75L', 'საფერავი 0.75ლ', 32),
            ('Wool scarf', 'შალის შარფი', 60), ('Backpack 25L', 'ზურგჩანთა 25ლ', 140),
            ('Wireless earbuds', 'უსადენო ყურსასმენი', 180), ('Phone case', 'ტელეფონის ქეისი', 25),
            ('Honey 500g', 'თაფლი 500გ', 28), ('Scented candle', 'სურნელოვანი სანთელი', 30),
            ('Notebook A5', 'რვეული A5', 12), ('Sunglasses', 'მზის სათვალე', 95),
            ('Yoga mat', 'იოგას ხალიჩა', 75), ('Water bottle', 'წყლის ბოთლი', 22),
            ('Linen shirt', 'სელის პერანგი', 110), ('Desk lamp', 'მაგიდის ლამპა', 65),
            ('Tea selection', 'ჩაის ასორტი', 26), ('Gift card 100 GEL', 'სასაჩუქრე ბარათი 100₾', 100),
        ]
        products = []
        for i, (en, ka, price) in enumerate(catalog):
            status = 'active'
            qty = rng.randint(15, 120)
            if i == 17:
                status = 'draft'
            elif i == 18:
                status = 'inactive'
            elif i == 13:
                status, qty = 'out_of_stock', 0
            elif i in (3, 9):
                qty = rng.randint(2, 6)  # below low_stock_threshold
            p = Product(
                sku=f'DEMO-{i + 1:03d}', name={'en': en, 'ka': ka},
                description={'en': f'{en} from our demo catalog.', 'ka': f'{ka} ჩვენი კატალოგიდან.'},
                short_description={'en': en, 'ka': ka}, price=Decimal(price),
                compare_at_price=Decimal(price) * Decimal('1.2') if i % 4 == 0 else None,
                cost_price=(Decimal(price) * Decimal('0.55')).quantize(Decimal('0.01')),
                image=f'https://picsum.photos/seed/echodesk-demo-{i + 1}/800/800', quantity=qty,
                status=status, is_featured=i in (0, 1, 4, 8), weight=Decimal('0.50'),
                meta_title={'en': en}, meta_description={'en': f'Buy {en.lower()} online'},
                created_by=self.admin,
            )
            p.save()
            for s in range(2 if i < 6 else 1):
                ProductImage.objects.create(product=p, sort_order=s, alt_text={'en': en},
                                            image=f'https://picsum.photos/seed/echodesk-demo-{i + 1}-{s}/800/800')
            products.append(p)
        ProductAttributeValue.objects.create(product=products[0], attribute=color, value_json=['black', 'white'])
        ProductAttributeValue.objects.create(product=products[1], attribute=color, value_json=['black', 'red'])

        variants = {}
        for p, combos in ((products[0], [('black', 'S'), ('black', 'M'), ('white', 'M'), ('white', 'L')]),
                          (products[1], [('black', '41'), ('black', '42'), ('red', '42'), ('red', '43')])):
            for n, (c, s) in enumerate(combos):
                v = ProductVariant.objects.create(
                    product=p, sku=f'{p.sku}-{c[:2].upper()}-{s}', sort_order=n,
                    name={'en': f'{c.title()} / {s}', 'ka': f'{c} / {s}'}, quantity=rng.randint(3, 25),
                    price=p.price + (Decimal('10') if s in ('L', '43') else Decimal('0')))
                ProductVariantAttributeValue.objects.create(variant=v, attribute=color, value_json=c)
                ProductVariantAttributeValue.objects.create(variant=v, attribute=size, value_json=s)
                variants.setdefault(p.id, []).append(v)

        promos = [
            PromoCode.objects.create(code='WELCOME10', discount_type='percentage', discount_value=Decimal('10'),
                                     valid_from=self.now - timedelta(days=60),
                                     valid_until=self.now + timedelta(days=120), max_uses=500),
            PromoCode.objects.create(code='FREESHIP', discount_type='fixed', discount_value=Decimal('7'),
                                     min_order_amount=Decimal('50'), valid_from=self.now - timedelta(days=10),
                                     valid_until=self.now + timedelta(days=30)),
            PromoCode.objects.create(code='SUMMER25', discount_type='percentage', discount_value=Decimal('25'),
                                     valid_from=self.now - timedelta(days=150),
                                     valid_until=self.now - timedelta(days=30), is_active=False),
        ]
        shipping = [
            ShippingMethod.objects.create(name={'en': 'Courier (Tbilisi)', 'ka': 'კურიერი (თბილისი)'},
                                          description={'en': 'Next-day delivery in Tbilisi'},
                                          price=Decimal('7'), free_shipping_threshold=Decimal('150'),
                                          estimated_days=1, position=0),
            ShippingMethod.objects.create(name={'en': 'Regions', 'ka': 'რეგიონები'},
                                          description={'en': 'Delivery to all regions of Georgia'},
                                          price=Decimal('12'), estimated_days=3, position=1),
        ]

        clients = []
        addresses = []
        cities = ['Tbilisi', 'Batumi', 'Kutaisi', 'Rustavi', 'Zugdidi']
        for i, (first, last) in enumerate(CUSTOMER_NAMES):
            c = EcommerceClient(first_name=first, last_name=last,
                                email=f'{first.lower()}.{last.lower()}@example.com',
                                phone_number=f'+99555510{i:04d}', is_verified=i % 4 != 0,
                                date_of_birth=date(1980 + i, (i % 12) + 1, 10))
            c.password = self.password_hash
            c.save()
            _backdate(c, created_at=self.now - timedelta(days=90 - i * 5))
            addr = ClientAddress.objects.create(client=c, label='Home', city=cities[i % len(cities)],
                                                address=f'{10 + i} Rustaveli Ave, apt {i + 3}',
                                                country='Georgia', postal_code=f'0{100 + i}', is_default=True)
            clients.append(c)
            addresses.append(addr)
        self.ecommerce_clients = clients

        statuses = (['pending'] * 4 + ['confirmed'] * 4 + ['processing'] * 4 + ['shipped'] * 4
                    + ['delivered'] * 5 + ['cancelled'] * 2 + ['refunded'] * 2)
        sellable = [p for p in products if p.status == 'active']
        for i, status in enumerate(statuses):
            ci = i % len(clients)
            client, addr = clients[ci], addresses[ci]
            created = self.now - timedelta(days=(len(statuses) - i) * 1.2, hours=rng.randint(0, 10))
            lines = []
            for p in rng.sample(sellable, k=1 + i % 3):
                variant = variants.get(p.id, [None])[i % len(variants.get(p.id, [None]))]
                unit = variant.price if variant and variant.price else p.price
                lines.append((p, variant, 1 + (i + p.id) % 3, unit))
            subtotal = sum(q * u for _, _, q, u in lines)
            method = shipping[i % 2]
            ship_cost = Decimal('0') if method.free_shipping_threshold and subtotal >= method.free_shipping_threshold \
                else method.price
            promo = promos[0] if i % 5 == 0 else None
            discount = (subtotal * promo.discount_value / 100).quantize(Decimal('0.01')) if promo else Decimal('0')
            payment_status = {'pending': 'pending', 'cancelled': 'failed', 'refunded': 'refunded'}.get(status, 'paid')
            order = Order(
                order_number=f'ORD-DEMO-{1001 + i}', client=client, delivery_address=addr,
                customer_first_name=client.first_name, customer_last_name=client.last_name,
                customer_email=client.email, customer_phone=client.phone_number,
                status=status, payment_status=payment_status,
                payment_method='cash_on_delivery' if status == 'pending' or i % 4 == 0 else 'card',
                subtotal=subtotal, shipping_cost=ship_cost, discount_amount=discount,
                total_amount=subtotal + ship_cost - discount, shipping_method=method, promo_code=promo,
                notes='Please call before delivery' if i % 3 == 0 else '',
                tracking_number=f'GE{900000 + i}' if status in ('shipped', 'delivered') else '',
                courier_provider='Demo Courier' if status in ('shipped', 'delivered') else '',
                estimated_delivery_date=(created + timedelta(days=method.estimated_days)).date(),
            )
            order.save()
            for p, variant, qty, unit in lines:
                OrderItem.objects.create(order=order, product=p, variant=variant, quantity=qty, price=unit,
                                         product_name=p.name)
            stamps = {'created_at': created}
            if payment_status in ('paid', 'refunded'):
                stamps['paid_at'] = created + timedelta(minutes=5)
            flow = ['confirmed', 'processing', 'shipped', 'delivered']
            if status in flow or status == 'refunded':
                reached = flow.index(status) if status in flow else 3
                for step, field in enumerate(['confirmed_at', 'processing_at', 'shipped_at', 'delivered_at']):
                    if step <= reached:
                        stamps[field] = created + timedelta(hours=6 * (step + 1))
            if status == 'cancelled':
                stamps['cancelled_at'] = created + timedelta(hours=3)
            _backdate(order, **stamps)
            if promo:
                PromoCode.objects.filter(pk=promo.pk).update(times_used=promo.times_used + 1)
                promo.times_used += 1

        review_texts = [
            (5, 'Excellent quality', 'Exactly as described, fast delivery.'),
            (4, 'Very good', 'Nice product, packaging could be better.'),
            (3, 'Okay', 'Does the job.'),
            (5, 'Love it', 'Bought a second one as a gift.'),
            (2, 'Not great', 'Colour differs from the photo.'),
        ]
        for i in range(12):
            rating, title, content = review_texts[i % len(review_texts)]
            ProductReview.objects.create(product=sellable[i % 8], client=clients[i], rating=rating, title=title,
                                         content=content, is_verified_purchase=i % 2 == 0, is_approved=i != 7)

    # --------------------------------------------------------------- invoices
    def seed_invoices(self):
        from invoices.models import Invoice, InvoiceLineItem, InvoicePayment, InvoiceSettings

        if not InvoiceSettings.objects.exists():
            InvoiceSettings.objects.create(
                company_name='EchoDesk Demo LLC', tax_id='400000000', address='1 Freedom Square, Tbilisi',
                phone='+995322000111', email='billing@demo.ge', website='https://demo.ge', invoice_prefix='DEMO',
                default_tax_rate=Decimal('18'), default_due_days=14,
                bank_accounts=[{'bank_name': 'Demo Bank', 'iban': 'GE00DM0000000000000000', 'currency': 'GEL'}],
                footer_text='Thank you for your business!')
        from ecommerce_crm.models import EcommerceClient
        clients = list(EcommerceClient.objects.order_by('id')[:8])
        year = self.today.year
        plan = [
            # (status, issue offset, due offset, paid fraction)
            ('draft', -1, 13, 0), ('sent', -5, 9, 0), ('viewed', -8, 6, 0),
            ('partially_paid', -20, 10, Decimal('0.4')), ('paid', -35, -21, 1), ('paid', -50, -36, 1),
            ('overdue', -40, -10, 0), ('cancelled', -25, -11, 0),
        ]
        items = [('Website support plan (monthly)', 1, 'month', 450), ('Call center seats', 5, 'seat', 60),
                 ('Onboarding workshop', 1, 'unit', 300), ('SMS bundle 5000', 1, 'bundle', 120)]
        for i, (status, issue_off, due_off, paid_frac) in enumerate(plan):
            client = clients[i % len(clients)] if clients else None
            inv = Invoice(
                invoice_number=f'DEMO-{year}-{i + 1:04d}', status='draft' if status != 'cancelled' else 'cancelled',
                client=client, client_name=client.full_name if client else 'Walk-in client',
                issue_date=self.today + timedelta(days=issue_off), due_date=self.today + timedelta(days=due_off),
                currency='GEL', notes='Demo invoice', terms_and_conditions='Payment within 14 days.',
                created_by=self.manager if i % 2 else self.admin,
            )
            inv.save()
            for pos in range(1 + i % 3):
                desc, qty, unit, price = items[(i + pos) % len(items)]
                InvoiceLineItem.objects.create(invoice=inv, description=desc, quantity=Decimal(qty), unit=unit,
                                               unit_price=Decimal(price), tax_rate=Decimal('18'), position=pos)
            inv.refresh_from_db()
            if status in ('sent', 'viewed', 'overdue', 'partially_paid', 'paid'):
                inv.status = 'sent' if status in ('partially_paid', 'paid') else status
                inv.sent_date = _aware(inv.issue_date, time(10, 0))
                inv.save()
            if paid_frac:
                amount = (inv.total * paid_frac).quantize(Decimal('0.01')) if paid_frac != 1 else inv.total
                InvoicePayment.objects.create(invoice=inv, amount=amount,
                                              payment_date=_aware(inv.issue_date + timedelta(days=5), time(12, 0)),
                                              payment_method=['bank_transfer', 'card', 'cash'][i % 3],
                                              reference_number=f'TRX-{i + 1:05d}', recorded_by=self.manager)

    # --------------------------------------------------------------- bookings
    def seed_bookings(self):
        from booking_management.models import (
            Booking, BookingSettings, BookingStaff, Service, ServiceCategory, StaffAvailability, StaffException,
        )
        from social_integrations.models import Client

        BookingSettings.objects.get_or_create(tenant=self.tenant, defaults={
            'public_description': {'en': 'Demo salon - book online in seconds', 'ka': 'დემო სალონი'},
            'public_address': '12 Chavchavadze Ave, Tbilisi', 'public_phone': '+995322000222',
            'bank_name': 'Demo Bank', 'bank_iban': 'GE00DM0000000000000000',
            'bank_account_holder': 'EchoDesk Demo LLC'})
        hair = ServiceCategory.objects.create(name={'en': 'Hair', 'ka': 'თმა'}, icon='scissors', display_order=0)
        beauty = ServiceCategory.objects.create(name={'en': 'Beauty', 'ka': 'სილამაზე'}, icon='sparkles',
                                                display_order=1)
        staff_users = [self.agent, self.users['staff1'], self.users['staff2']]
        staff = []
        for u in staff_users:
            bs, _ = BookingStaff.objects.get_or_create(user=u, defaults={
                'bio': f'{u.first_name} has 5+ years of experience.', 'average_rating': Decimal('4.7'),
                'total_ratings': 23})
            staff.append(bs)
            for dow in range(6):  # Mon-Sat
                StaffAvailability.objects.create(staff=bs, day_of_week=dow, start_time=time(10, 0),
                                                 end_time=time(19, 0), break_start=time(14, 0),
                                                 break_end=time(14, 30))
            StaffAvailability.objects.create(staff=bs, day_of_week=6, start_time=time(10, 0),
                                             end_time=time(16, 0), is_available=False)
        StaffException.objects.create(staff=staff[1], date=self.today + timedelta(days=9),
                                      reason='Training day', is_available=False)

        services = []
        for cat, en, ka, price, minutes, staff_ix in [
            (hair, 'Classic haircut', 'კლასიკური შეჭრა', 40, 45, [0, 1]),
            (hair, 'Hair colouring', 'თმის შეღებვა', 120, 90, [1]),
            (hair, 'Blow-dry', 'ფენით დავარცხნა', 30, 30, [0, 1, 2]),
            (beauty, 'Manicure', 'მანიკური', 35, 60, [2]),
            (beauty, 'Facial treatment', 'სახის მოვლა', 90, 60, [2, 0]),
        ]:
            s = Service.objects.create(name={'en': en, 'ka': ka}, description={'en': f'{en} at our demo salon'},
                                       category=cat, base_price=Decimal(price), duration_minutes=minutes,
                                       buffer_time_minutes=15, deposit_percentage=20 if price >= 90 else 0)
            s.staff_members.set([staff[j] for j in staff_ix])
            services.append(s)

        clients = []
        for i, (first, last) in enumerate(CUSTOMER_NAMES[:10]):
            clients.append(Client.objects.create(
                name=f'{first} {last}', first_name=first, last_name=last,
                email=f'{first.lower()}.{last.lower()}@example.com', phone=f'+99559910{i:04d}',
                is_booking_enabled=True, is_verified=True, password_hash=self.password_hash,
                created_by=self.admin, notes='Booking client (demo)'))
        self.booking_clients = clients

        days = [self.today + timedelta(days=o) for o in range(-7, 15)]
        days = [d for d in days if d.weekday() != 6]
        slots = [time(10, 0), time(11, 30), time(13, 0), time(15, 0), time(16, 30), time(18, 0)]
        for i in range(25):
            day = days[i % len(days)]
            # A day hosts booking i and i+len(days): morning vs afternoon slot,
            # so no staff member is ever double-booked.
            start = slots[(i // len(days)) * 3 + i % 2]
            service = services[i % len(services)]
            eligible = list(service.staff_members.all())
            member = eligible[i % len(eligible)] if i % 7 else None  # every 7th booking is unassigned
            end = (datetime.combine(day, start) + timedelta(minutes=service.duration_minutes)).time()
            total = service.base_price
            if day < self.today:
                status = 'cancelled' if i % 6 == 0 else 'completed'
            elif day == self.today:
                status = 'in_progress' if start <= timezone.localtime().time() else 'confirmed'
            else:
                status = 'pending' if i % 3 == 0 else 'confirmed'
            payment_status, paid = 'pending', Decimal('0')
            if status == 'completed':
                payment_status, paid = 'fully_paid', total
            elif status == 'confirmed' and service.deposit_percentage:
                payment_status = 'deposit_paid'
                paid = (total * service.deposit_percentage / 100).quantize(Decimal('0.01'))
            b = Booking(client=clients[i % len(clients)], service=service, staff=member, date=day,
                        start_time=start, end_time=end, status=status, payment_status=payment_status,
                        total_amount=total, paid_amount=paid,
                        deposit_amount=(total * service.deposit_percentage / 100).quantize(Decimal('0.01')),
                        payment_method='card' if payment_status != 'pending' else 'cash',
                        client_notes='First visit' if i % 5 == 0 else '',
                        staff_notes='Prefers a quiet room' if i % 8 == 0 else '',
                        contact_email=clients[i % len(clients)].email, contact_language='ka' if i % 2 else 'en',
                        manage_token=secrets.token_urlsafe(24))
            if status == 'cancelled':
                b.cancelled_at = _aware(day, start) - timedelta(days=1)
                b.cancelled_by = 'client'
                b.cancellation_reason = 'Could not make it'
            if status in ('confirmed', 'completed', 'in_progress'):
                b.confirmed_at = _aware(day, start) - timedelta(days=2)
            if status == 'completed':
                b.completed_at = _aware(day, end)
                if i % 2 == 0:
                    b.rating, b.review = 5, 'Great service!'
            b.save()
            _backdate(b, created_at=_aware(day, start) - timedelta(days=3))

    # ----------------------------------------------------------------- social
    def seed_social(self):
        from social_integrations.models import (
            ChatAssignment, Client, FacebookMessage, FacebookPageConnection, InstagramAccountConnection,
            InstagramMessage, QuickReply, SocialAccount, TelegramAccount, TelegramMessage,
            WhatsAppBusinessAccount, WhatsAppMessage, WhatsAppMessageTemplate, WidgetMessage, WidgetSession,
        )

        fake = 'demo-fake-token-not-valid'
        fb = FacebookPageConnection.objects.create(
            page_id=self._ext('fb-page'), page_name='EchoDesk Demo Shop', page_access_token=fake,
            connected_by=self.admin, connected_by_fb_user_id=self._ext('fb-admin'), webhook_subscribed=False)
        ig = InstagramAccountConnection.objects.create(
            instagram_account_id=self._ext('ig-account'), username='echodesk.demo', access_token=fake,
            facebook_page=fb)
        wa = WhatsAppBusinessAccount.objects.create(
            waba_id=self._ext('waba'), business_name='EchoDesk Demo Shop', phone_number_id=self._ext('wa-phone'),
            phone_number='995322000111', display_phone_number='+995 32 200 01 11', access_token=fake,
            quality_rating='GREEN', sync_status='completed', platform_type='CLOUD_API', onboarded_at=self.now)
        tg = TelegramAccount.objects.create(
            telegram_user_id=900000000000 + zlib.crc32(self.schema.encode()) % 99999, phone_number='+995599000999',
            first_name='Demo', last_name='Shop', username='echodesk_demo_shop', session_string=fake,
            connected_by=self.admin, last_seen_at=self.now)
        from widget_registry.models import WidgetConnection
        with schema_context(get_public_schema_name()):
            widget, _ = WidgetConnection.objects.get_or_create(
                tenant_schema=self.schema, label='Demo website',
                defaults={'widget_token': self._ext('widget-') + secrets.token_hex(8),
                          'allowed_origins': ['http://localhost:3000', 'https://demo.lvh.me'],
                          'welcome_message': {'en': 'Hi! How can we help?', 'ka': 'გამარჯობა! რით დაგეხმაროთ?'}})

        templates = [
            WhatsAppMessageTemplate.objects.create(
                business_account=wa, template_id=self._ext('tpl-1'), name='order_update', language='en',
                status='APPROVED', category='UTILITY', created_by=self.admin,
                components=[{'type': 'BODY', 'text': 'Hi {{1}}, your order {{2}} is on its way!',
                             'example': {'body_text': [['Nino', 'ORD-DEMO-1001']]}}]),
            WhatsAppMessageTemplate.objects.create(
                business_account=wa, template_id=self._ext('tpl-2'), name='appointment_reminder', language='ka',
                status='APPROVED', category='UTILITY', created_by=self.admin,
                components=[{'type': 'HEADER', 'format': 'TEXT', 'text': 'შეხსენება'},
                            {'type': 'BODY', 'text': 'გამარჯობა {{1}}, გახსენებთ ვიზიტს {{2}}-ზე.',
                             'example': {'body_text': [['ნინო', '15:00']]}},
                            {'type': 'FOOTER', 'text': 'EchoDesk Demo'}]),
            WhatsAppMessageTemplate.objects.create(
                business_account=wa, template_id=self._ext('tpl-3'), name='weekend_offer', language='en',
                status='PENDING', category='MARKETING', created_by=self.manager,
                components=[{'type': 'BODY', 'text': 'This weekend only: {{1}} off everything!',
                             'example': {'body_text': [['20%']]}},
                            {'type': 'BUTTONS', 'buttons': [{'type': 'URL', 'text': 'Shop now',
                                                             'url': 'https://demo.ge/shop'}]}]),
        ]

        scripts = [
            [('in', 'Hello! Is the cotton T-shirt available in size M?'),
             ('out', 'Hi! Yes, we have it in black and white.'),
             ('in', 'Great, I will take a white one. Delivery to Vake?'),
             ('out', 'Sure, courier delivery tomorrow, 7 GEL.'), ('in', 'Perfect, thanks!')],
            [('in', 'My order has not arrived yet'), ('out', 'Sorry about that! Could you share the order number?'),
             ('in', 'ORD-DEMO-1004')],
            [('in', 'Do you have gift boxes?'), ('out', 'Yes - churchkhela gift box for 40 GEL.'),
             ('in', 'Can you deliver it to Batumi?'), ('out', 'Yes, 3 days to the regions.')],
            [('in', 'გამარჯობა, ხვალ თავისუფალი დრო გაქვთ?')],
        ]

        def social_client(i, platform, platform_id, account_id, display, username=''):
            first, last = CUSTOMER_NAMES[i % len(CUSTOMER_NAMES)]
            client = Client.objects.create(name=f'{first} {last}', first_name=first, last_name=last,
                                           phone=f'+99557720{i:04d}', email=f'{first.lower()}{i}@example.com',
                                           created_by=self.admin, notes=f'Contacted us via {platform}')
            SocialAccount.objects.create(client=client, platform=platform, platform_id=platform_id,
                                         account_connection_id=account_id, display_name=display,
                                         username=username, last_message_at=self.now)
            return client

        def ts(conv_ix, msg_ix, n):
            # newest conversation first; messages a few minutes apart
            return self.now - timedelta(hours=conv_ix * 5 + 1, minutes=(n - msg_ix) * 4)

        ci = 0
        # Facebook
        for c, script in enumerate(scripts[:3]):
            psid = self._ext(f'fb-user-{c + 1}')
            first, last = CUSTOMER_NAMES[ci % len(CUSTOMER_NAMES)]
            social_client(ci, 'facebook', psid, fb.page_id, f'{first} {last}')
            for m, (direction, text) in enumerate(script):
                out = direction == 'out'
                FacebookMessage.objects.create(
                    page_connection=fb, message_id=self._ext(f'fb-{c}-{m}'), sender_id=psid,
                    sender_name=fb.page_name if out else f'{first} {last}', message_text=text,
                    timestamp=ts(c, m, len(script)), is_from_page=out, is_delivered=True, is_read=out,
                    is_read_by_staff=out or c > 0, sent_by=self.agent if out else None)
            ci += 1
        # Instagram
        for c, script in enumerate(scripts[:3]):
            igsid = self._ext(f'ig-user-{c + 1}')
            first, last = CUSTOMER_NAMES[ci % len(CUSTOMER_NAMES)]
            handle = f'{first.lower()}_{last.lower()}'
            social_client(ci, 'instagram', igsid, ig.instagram_account_id, f'{first} {last}', handle)
            for m, (direction, text) in enumerate(script):
                out = direction == 'out'
                InstagramMessage.objects.create(
                    account_connection=ig, message_id=self._ext(f'ig-{c}-{m}'), sender_id=igsid,
                    sender_name=ig.username if out else f'{first} {last}', sender_username=handle,
                    message_text=text, timestamp=ts(c, m, len(script)) - timedelta(minutes=30),
                    is_from_business=out, is_delivered=True, is_read_by_staff=out or c != 1,
                    sent_by=self.manager if out else None)
            ci += 1
        # WhatsApp
        for c, script in enumerate(scripts[:3] + [scripts[3]]):
            number = f'99555530{c:04d}'
            first, last = CUSTOMER_NAMES[ci % len(CUSTOMER_NAMES)]
            social_client(ci, 'whatsapp', number, wa.phone_number_id, f'{first} {last}')
            for m, (direction, text) in enumerate(script):
                out = direction == 'out'
                WhatsAppMessage.objects.create(
                    business_account=wa, message_id=self._ext(f'wamid-{c}-{m}'),
                    from_number=wa.phone_number if out else number, to_number=number if out else wa.phone_number,
                    contact_name=f'{first} {last}', message_text=text, timestamp=ts(c, m, len(script)),
                    is_from_business=out, status='read' if out else 'delivered', is_delivered=True,
                    is_read_by_staff=out or c == 0, sent_by=self.agent if out else None)
            ci += 1
        WhatsAppMessage.objects.create(
            business_account=wa, message_id=self._ext('wamid-template'), from_number=wa.phone_number,
            to_number='995555300000', contact_name='Customer', template=templates[0],
            template_parameters=['Levan', 'ORD-DEMO-1006'], message_text='Hi Levan, your order ORD-DEMO-1006 is on its way!',
            timestamp=self.now - timedelta(minutes=20), is_from_business=True, status='delivered',
            is_delivered=True, is_read_by_staff=True, sent_by=self.agent)
        # Telegram
        for c, script in enumerate(scripts[1:3]):
            peer = 700000000 + c
            first, last = CUSTOMER_NAMES[ci % len(CUSTOMER_NAMES)]
            social_client(ci, 'telegram', str(peer), str(tg.telegram_user_id), f'{first} {last}',
                          f'{first.lower()}{c}')
            for m, (direction, text) in enumerate(script):
                out = direction == 'out'
                TelegramMessage.objects.create(
                    account=tg, message_id=self._ext(f'tg-{c}-{m}'), telegram_msg_id=1000 + c * 100 + m,
                    peer_id=peer, peer_name=f'{first} {last}', peer_username=f'{first.lower()}{c}',
                    message_text=text, timestamp=ts(c, m, len(script)) - timedelta(minutes=50),
                    is_from_business=out, source='echodesk' if out else 'telegram_app',
                    status='read' if out else 'delivered', is_delivered=True,
                    is_read_by_staff=out or c == 1, sent_by=self.agent if out else None)
            ci += 1
        # Website widget
        for c, script in enumerate([scripts[0], scripts[3]]):
            first, last = CUSTOMER_NAMES[ci % len(CUSTOMER_NAMES)]
            visitor = self._ext(f'visitor-{c + 1}')
            session = WidgetSession.objects.create(
                connection_id=widget.id, session_id=self._ext(f'widget-session-{c + 1}'), visitor_id=visitor,
                visitor_name=f'{first} {last}', visitor_email=f'{first.lower()}@example.com',
                page_url='https://demo.ge/shop', referrer_url='https://www.google.com/',
                user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)', ip_address='127.0.0.1')
            _backdate(session, started_at=ts(c, 0, len(script)) - timedelta(minutes=5))
            social_client(ci, 'widget', visitor, str(widget.id), f'{first} {last}')
            for m, (direction, text) in enumerate(script):
                out = direction == 'out'
                WidgetMessage.objects.create(
                    session=session, message_id=self._ext(f'widget-{c}-{m}'), message_text=text,
                    is_from_visitor=not out, sent_by=self.agent if out else None, is_delivered=True,
                    is_read_by_visitor=out, is_read_by_staff=out or c == 0,
                    timestamp=ts(c, m, len(script)) - timedelta(minutes=10))
            ci += 1

        ChatAssignment.objects.create(platform='facebook', conversation_id=self._ext('fb-user-1'),
                                      account_id=fb.page_id, assigned_user=self.agent, status='in_session',
                                      session_started_at=self.now - timedelta(hours=1))
        ChatAssignment.objects.create(platform='whatsapp', conversation_id='995555300001',
                                      account_id=wa.phone_number_id, assigned_user=self.manager, status='active')

        for pos, (title, msg, platforms, shortcut, cat) in enumerate([
            ('Greeting', 'Hello! Thanks for reaching out. How can I help you today?', [], '/hi', 'General'),
            ('Delivery times', 'Courier delivery takes 1 day in Tbilisi and 3 days to the regions.', [],
             '/delivery', 'Shipping'),
            ('Order number', 'Could you please share your order number (ORD-...)?',
             ['facebook', 'instagram', 'whatsapp'], '/order', 'Orders'),
            ('Working hours', 'We are open Mon-Sat 10:00-19:00.', [], '/hours', 'General'),
            ('Thanks', 'Thank you! Have a great day.', ['whatsapp', 'telegram', 'widget'], '/thanks', 'General'),
        ]):
            QuickReply.objects.create(title=title, message=msg, platforms=platforms, shortcut=shortcut,
                                      category=cat, position=pos, created_by=self.admin, use_count=pos * 3)

    # ------------------------------------------------------------------ calls
    def seed_calls(self):
        from crm.models import CallEvent, CallLog, CallRecording, SipConfiguration, UserPhoneAssignment
        from social_integrations.models import Client

        sip = SipConfiguration.objects.create(
            name='Demo PBX', sip_server='pbx.demo.invalid', username='demo-trunk',
            password='demo-fake-password', realm='pbx.demo.invalid', phone_number='+995322000111',
            is_default=True, created_by=self.admin)
        exts = {}
        for n, u in enumerate([self.admin, self.manager, self.agent, self.users['staff1'], self.users['staff2']]):
            UserPhoneAssignment.objects.create(user=u, sip_configuration=sip, extension=str(100 + n),
                                               extension_password=f'demo-ext-{100 + n}',
                                               phone_number='+995322000111', display_name=u.get_full_name())
            exts[u.id] = str(100 + n)
        social = list(Client.objects.exclude(phone='').order_by('id')[:6])
        handlers = [self.agent, self.users['staff1'], self.manager, self.users['staff2']]
        # (direction, status, answered seconds)
        plan = ([('inbound', 'answered', 180)] * 4 + [('inbound', 'ended', 95)] * 3
                + [('outbound', 'answered', 240)] * 3 + [('outbound', 'ended', 60)] * 2
                + [('inbound', 'missed', 0)] * 3 + [('inbound', 'no_answer', 0)] * 2
                + [('outbound', 'no_answer', 0), ('outbound', 'busy', 0), ('inbound', 'failed', 0)])
        for i, (direction, status, secs) in enumerate(plan):
            handler = handlers[i % len(handlers)]
            customer = f'+99559955{i:04d}'
            client = social[i % len(social)] if social and i % 3 == 0 else None
            if client:
                customer = client.phone
            started = self.now - timedelta(days=i % 10, hours=(i * 3) % 9, minutes=i * 7 % 60)
            ext = exts[handler.id]
            log = CallLog(
                caller_number=customer if direction == 'inbound' else ext,
                recipient_number='+995322000111' if direction == 'inbound' else customer,
                direction=direction, status=status, sip_configuration=sip,
                handled_by=handler if (secs or status in ('missed', 'no_answer') and i % 2) else None,
                social_client=client, notes=f'[demo] {direction} call',
                sip_call_id=self._ext(f'sip-{i}'), call_quality_score=4.2 if secs else None)
            if secs:
                log.answered_at = started + timedelta(seconds=8)
                log.ended_at = log.answered_at + timedelta(seconds=secs + i)
                log.duration = log.ended_at - log.answered_at
            else:
                log.ended_at = started + timedelta(seconds=25)
            log.save()
            _backdate(log, started_at=started)
            CallEvent.objects.create(call_log=log, event_type='initiated', user=log.handled_by)
            if secs:
                CallEvent.objects.create(call_log=log, event_type='answered', user=handler)
                CallEvent.objects.create(call_log=log, event_type='ended', user=handler,
                                         metadata={'duration': secs + i})
                if i % 2 == 0:
                    CallRecording.objects.create(
                        call_log=log, file_path=f'/var/spool/asterisk/monitor/demo-{log.id}.wav',
                        file_url=f'https://pbx.demo.invalid/recordings/demo-{log.id}.wav',
                        file_size=(secs + i) * 16000, duration=log.duration, status='completed',
                        started_at=log.answered_at, completed_at=log.ended_at)
            else:
                CallEvent.objects.create(call_log=log, event_type='failed' if status == 'failed' else 'ended',
                                         metadata={'reason': status})

    # -------------------------------------------------------------- team chat
    def seed_team_chat(self):
        from users.models import TeamChatConversation, TeamChatMessage

        threads = [
            (self.admin, self.manager, ['Can you review the weekly report?', 'Sure, sending notes in an hour.',
                                        'Thanks!']),
            (self.manager, self.agent, ['Please cover the Facebook inbox after lunch.', 'On it 👍',
                                        'The courier delayed ORD-DEMO-1004, I told the customer.',
                                        'Good, add a note to the ticket.']),
            (self.agent, self.users['staff1'], ['Do you have the new price list?', 'Uploaded it to the board.']),
            (self.admin, self.agent, ['Welcome to the demo workspace!', 'Thank you 😊',
                                      'Ping me if anything is unclear.']),
        ]
        for t, (a, b, texts) in enumerate(threads):
            conv = TeamChatConversation.objects.create()
            conv.participants.set([a, b])
            last = None
            for m, text in enumerate(texts):
                sender = a if m % 2 == 0 else b
                is_last = m == len(texts) - 1
                at = self.now - timedelta(hours=t * 6 + 1, minutes=(len(texts) - m) * 3)
                msg = TeamChatMessage.objects.create(conversation=conv, sender=sender, text=text,
                                                     is_read=not is_last, read_at=None if is_last else at)
                _backdate(msg, created_at=at)
                last = at
            _backdate(conv, created_at=last - timedelta(hours=1), updated_at=last)

    # ---------------------------------------------------------- notifications
    def seed_notifications(self):
        from users.models import Notification
        from tickets.models import Ticket

        ticket = Ticket.objects.order_by('id').first()
        items = [
            ('ticket_assigned', 'Assigned to: {t}', 'Nino assigned you to this ticket as Primary Assignee',
             {'ticket_id': ticket.id if ticket else None}, f'/tickets/{ticket.id}' if ticket else ''),
            ('ticket_mentioned', 'Mentioned in: {t}', 'Giorgi mentioned you in a comment',
             {'ticket_id': ticket.id if ticket else None}, f'/tickets/{ticket.id}' if ticket else ''),
            ('message_received', 'New Facebook message', 'Levan Maisuradze: Perfect, thanks!',
             {'platform': 'facebook', 'conversation_id': self._ext('fb-user-1')}, '/messages'),
            ('team_chat_message', 'Giorgi Kapanadze', 'Please cover the Facebook inbox after lunch.',
             {'sender_id': self.manager.id}, '/chat'),
            ('booking_created', 'New online booking', 'Ketevan booked a Manicure for tomorrow at 13:00',
             {}, '/bookings'),
            ('invoice_overdue', 'Invoice DEMO-{y}-0007 overdue', 'Invoice for a client is now overdue',
             {}, '/invoices'),
            ('leave_request_submitted', 'New leave request', 'Mariam Lomidze submitted a leave request',
             {}, '/leave/team-requests'),
            ('call_missed', 'Missed call', 'Missed inbound call from +995599550000', {}, '/calls'),
        ]
        rows = []
        for user in (self.admin, self.manager, self.agent):
            for n, (ntype, title, message, meta, link) in enumerate(items):
                meta = {k: v for k, v in meta.items() if v is not None}
                ticket_id = meta.pop('ticket_id', None)
                rows.append(Notification(
                    user=user, notification_type=ntype,
                    title=title.format(t=ticket.title if ticket else 'Ticket', y=self.today.year),
                    message=message, ticket_id=ticket_id, link_url=link,
                    metadata={**meta, 'demo_seed': True}, is_read=n >= 5,
                    read_at=self.now if n >= 5 else None))
        created = Notification.objects.bulk_create(rows)
        for n, row in enumerate(created):
            _backdate(row, created_at=self.now - timedelta(minutes=15 * (n % len(items)) + 1))

    # ------------------------------------------------------------ help center
    def seed_help_center(self):
        from help_center.models import HelpArticle, HelpCategory

        cats = [
            ('getting-started', 'Getting started', 'დაწყება', 'rocket', ''),
            ('tickets-boards', 'Tickets & boards', 'თიკეტები და დაფები', 'kanban', 'tickets'),
            ('messaging', 'Messaging', 'მესიჯები', 'message', 'social_integrations'),
        ]
        articles = {
            'getting-started': [
                ('welcome', 'Welcome to EchoDesk', 'კეთილი იყოს თქვენი მობრძანება', 'article', True),
                ('invite-team', 'Invite your team', 'გუნდის მოწვევა', 'guide', False),
            ],
            'tickets-boards': [
                ('create-board', 'Create your first board', 'პირველი დაფის შექმნა', 'guide', True),
                ('time-tracking', 'Track time in columns', 'დროის აღრიცხვა', 'article', False),
            ],
            'messaging': [
                ('connect-facebook', 'Connect a Facebook page', 'Facebook გვერდის დაკავშირება', 'video', False),
                ('messaging-faq', 'Messaging FAQ', 'ხშირად დასმული კითხვები', 'faq', False),
            ],
        }
        for pos, (slug, en, ka, icon, feature) in enumerate(cats):
            cat = HelpCategory.objects.create(slug=slug, name={'en': en, 'ka': ka}, icon=icon, position=pos,
                                              description={'en': f'{en} guides', 'ka': ka},
                                              required_feature_key=feature)
            for apos, (aslug, aen, aka, ctype, featured) in enumerate(articles[slug]):
                HelpArticle.objects.create(
                    category=cat, slug=aslug, title={'en': aen, 'ka': aka}, content_type=ctype,
                    summary={'en': f'{aen} in a few minutes.', 'ka': aka},
                    content={'en': f'<p>{aen}: step-by-step instructions.</p>', 'ka': f'<p>{aka}</p>'},
                    video_url='https://www.youtube.com/watch?v=dQw4w9WgXcQ' if ctype == 'video' else '',
                    video_duration='3:20' if ctype == 'video' else '',
                    guide_steps=[{'title': {'en': 'Open settings'}, 'content': {'en': 'Go to Settings.'}},
                                 {'title': {'en': 'Save'}, 'content': {'en': 'Click Save.'}}]
                    if ctype == 'guide' else [],
                    faq_items=[{'question': {'en': 'Is it free?'}, 'answer': {'en': 'There is a trial.'}}]
                    if ctype == 'faq' else [],
                    position=apos, is_featured=featured, published_at=self.now)


def demo_counts(tenant):
    """Row counts per module for the seeded tenant (inside its schema)."""
    from booking_management.models import Booking, BookingStaff, Service, ServiceCategory, StaffAvailability
    from crm.models import CallLog, CallRecording, UserPhoneAssignment
    from ecommerce_crm.models import (
        EcommerceClient, Language, Order, Product, ProductReview, ProductVariant, PromoCode, ShippingMethod,
    )
    from invoices.models import Invoice, InvoicePayment
    from leave_management.models import LeaveBalance, LeaveRequest, LeaveType, PublicHoliday
    from social_integrations.models import (
        Client, FacebookMessage, InstagramMessage, QuickReply, TelegramMessage, WhatsAppMessage,
        WhatsAppMessageTemplate, WidgetMessage, WidgetSession,
    )
    from tickets.models import (
        Board, ChecklistItem, Tag, Ticket, TicketAttachment, TicketColumn, TicketComment, TicketTimeLog,
    )
    from users.models import Notification, TeamChatConversation, TeamChatMessage, TenantGroup, User

    return {
        'users': User.objects.count(),
        'groups': TenantGroup.objects.count(),
        'boards': Board.objects.count(),
        'columns': TicketColumn.objects.count(),
        'tags': Tag.objects.count(),
        'tickets': Ticket.objects.count(),
        'ticket_comments': TicketComment.objects.count(),
        'checklist_items': ChecklistItem.objects.count(),
        'ticket_attachments': TicketAttachment.objects.count(),
        'time_logs': TicketTimeLog.objects.count(),
        'leave_types': LeaveType.objects.filter(tenant=tenant).count(),
        'leave_balances': LeaveBalance.objects.filter(tenant=tenant).count(),
        'leave_requests': LeaveRequest.objects.filter(tenant=tenant).count(),
        'holidays': PublicHoliday.objects.filter(tenant=tenant).count(),
        'invoices': Invoice.objects.count(),
        'invoice_payments': InvoicePayment.objects.count(),
        'service_categories': ServiceCategory.objects.count(),
        'services': Service.objects.count(),
        'booking_staff': BookingStaff.objects.count(),
        'staff_availability': StaffAvailability.objects.count(),
        'bookings': Booking.objects.count(),
        'languages': Language.objects.count(),
        'products': Product.objects.count(),
        'variants': ProductVariant.objects.count(),
        'promo_codes': PromoCode.objects.count(),
        'shipping_methods': ShippingMethod.objects.count(),
        'ecommerce_clients': EcommerceClient.objects.count(),
        'orders': Order.objects.count(),
        'reviews': ProductReview.objects.count(),
        'social_clients': Client.objects.count(),
        'facebook_messages': FacebookMessage.objects.count(),
        'instagram_messages': InstagramMessage.objects.count(),
        'whatsapp_messages': WhatsAppMessage.objects.count(),
        'telegram_messages': TelegramMessage.objects.count(),
        'widget_sessions': WidgetSession.objects.count(),
        'widget_messages': WidgetMessage.objects.count(),
        'quick_replies': QuickReply.objects.count(),
        'wa_templates': WhatsAppMessageTemplate.objects.count(),
        'phone_assignments': UserPhoneAssignment.objects.count(),
        'call_logs': CallLog.objects.count(),
        'call_recordings': CallRecording.objects.count(),
        'team_chat_conversations': TeamChatConversation.objects.count(),
        'team_chat_messages': TeamChatMessage.objects.count(),
        'notifications': Notification.objects.count(),
    }
