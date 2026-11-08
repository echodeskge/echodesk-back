"""
Known ticket fixtures for the local Playwright suite (tickets redesign, FE-16).

    python manage.py prepare_tickets_e2e --schema demo --reset \\
        --json ~/Echodesk/.wt/front-tickets-shots/localdev/e2e-ids.json

LOCAL DEVELOPMENT ONLY. It rewrites the permissions and group memberships of
the e2e users, so it refuses to run unless ``settings.DEBUG`` is true and the
default database host is ``127.0.0.1`` or ``localhost``.

What it builds (in the given tenant schema):

- Boards "E2E Support" (default, #3b82f6; columns New / In Progress
  (track_time) / Waiting / Resolved (closed)), "E2E Sales" (#22c55e),
  "E2E Open" (no users or groups), "E2E Restricted" (the agent has no access)
  and "E2E Empty" (zero columns).
- Users ``admin`` (staff), ``agent`` (non-staff; on "E2E Support" only through
  the board group "E2E Support team"; no manage_columns / create_boards /
  access_orders) and ``agent2`` (for two-browser tests). By default these are
  the demo seed's admin@demo.ge, agent@demo.ge and staff1@demo.ge (the users
  the local tokens.json was minted for); a missing user is created with an
  unusable password.
- 12 tickets titled ``E2E-001`` .. ``E2E-012`` covering every priority, the
  VIP / Bug tags, assignees and an unassigned ticket, due today / overdue,
  checklist 2/5, two attachments, a customer with an internal comment, an
  order with a payment, two tickets with equal ``position_in_column``, and one
  ticket each on the Restricted and Open boards.

``--reset`` deletes every ``E2E-`` ticket, every ticket on an ``E2E `` board
and every ``E2E `` board, then recreates them all; without ``--reset`` the
command refuses when the fixtures already exist. The id map is written to
``--json`` (or printed to stdout).

``--agent-without-social`` is the "second tenant variant" (run it on another
schema, e.g. demo2): the agent loses the ``social_integrations`` feature (the
"E2E Support team" group gets every feature except it, and the agent is taken
out of every other group that grants it). That removal is not undone by a
later run without the flag.

Attachments are rows only: their ``file`` names point at paths that are never
uploaded, because the default file storage is the S3 (DO Spaces) backend and
a fixture command must not write to remote storage.
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from tenant_schemas.utils import get_public_schema_name, schema_context

TICKET_PREFIX = 'E2E-'
BOARD_PREFIX = 'E2E '
SUPPORT_GROUP_NAME = 'E2E Support team'
CUSTOMER_NAME = 'E2E Customer'
CUSTOMER_EMAIL = 'e2e.customer@example.com'
LOCAL_DB_HOSTS = ('127.0.0.1', 'localhost')

# key -> (name, color, is_default, access, columns)
# access: 'support_group' | 'agents' | 'open' | 'admin_only'
# columns: [(key, name, color, extra flags)]
BOARDS = [
    ('support', 'E2E Support', '#3b82f6', True, 'support_group', [
        ('new', 'New', '#6B7280', {'is_default': True}),
        ('in_progress', 'In Progress', '#3B82F6', {'track_time': True}),
        ('waiting', 'Waiting', '#F59E0B', {}),
        ('resolved', 'Resolved', '#10B981', {'is_closed_status': True}),
    ]),
    ('sales', 'E2E Sales', '#22c55e', False, 'agents', [
        ('new', 'New Lead', '#8B5CF6', {'is_default': True}),
        ('won', 'Won', '#10B981', {'is_closed_status': True}),
    ]),
    ('open', 'E2E Open', '#a855f7', False, 'open', [
        ('todo', 'To Do', '#6B7280', {'is_default': True}),
    ]),
    ('restricted', 'E2E Restricted', '#ef4444', False, 'admin_only', [
        ('todo', 'To Do', '#6B7280', {'is_default': True}),
    ]),
    ('empty', 'E2E Empty', '#64748b', False, 'support_group', []),
]

# number -> spec. ``role`` is a stable alias written to the id map so specs
# don't depend on the numbering. ``due`` is a day offset from tickets_today().
TICKETS = [
    dict(n=1, role='due_today', title='Login page shows a blank screen', board='support', column='new',
         priority='critical', tags=['Bug'], assignee='agent', collaborators=['agent2'], due=0),
    dict(n=2, role='overdue', title='Invoice copy for March', board='support', column='new',
         priority='high', tags=['VIP'], assignee='agent2', due=-3),
    dict(n=3, role='unassigned', title='Unassigned printer request', board='support', column='new',
         priority='low', tags=[], assignee=None),
    dict(n=4, role='checklist', title='Onboard the new branch', board='support', column='in_progress',
         priority='medium', tags=[], assignee='agent', checklist=(5, 2)),
    dict(n=5, role='attachments', title='Damaged package photos', board='support', column='in_progress',
         priority='medium', tags=['Bug', 'VIP'], assignee='agent', attachments=True),
    dict(n=6, role='customer', title='Customer asks about delivery', board='support', column='waiting',
         priority='high', tags=['VIP'], assignee='agent', customer=True),
    dict(n=7, role='equal_position_a', title='Equal position A', board='support', column='waiting',
         priority='low', tags=[], assignee='agent2'),
    dict(n=8, role='equal_position_b', title='Equal position B', board='support', column='waiting',
         priority='medium', tags=[], assignee='agent2'),
    dict(n=9, role='resolved', title='Password reset done', board='support', column='resolved',
         priority='low', tags=[], assignee='agent', due=-5),
    dict(n=10, role='order', title='Order for 20 coffee cups', board='sales', column='new',
         priority='medium', tags=[], assignee='agent2', order=True),
    dict(n=11, role='restricted', title='Restricted board ticket', board='restricted', column='todo',
         priority='high', tags=[], assignee=None),
    dict(n=12, role='open_board', title='Open board ticket', board='open', column='todo',
         priority='medium', tags=[], assignee=None),
]

TAG_COLORS = {'VIP': '#F59E0B', 'Bug': '#EF4444'}
CHECKLIST_TEXTS = ['Order hardware', 'Create accounts', 'Install software', 'Train staff', 'Sign off']


def _ticket_title(spec):
    return f"{TICKET_PREFIX}{spec['n']:03d} {spec['title']}"


class Command(BaseCommand):
    help = 'Create the known tickets e2e fixtures in a local tenant schema (local development only)'

    def add_arguments(self, parser):
        parser.add_argument('--schema', required=True, help='Tenant schema to write the fixtures into')
        parser.add_argument('--reset', action='store_true',
                            help='Delete existing E2E boards and tickets first, then recreate them')
        parser.add_argument('--json', dest='json_path', default=None,
                            help='Write the id map to this file (default: print it to stdout)')
        parser.add_argument('--admin-email', default='admin@demo.ge')
        parser.add_argument('--agent-email', default='agent@demo.ge')
        parser.add_argument('--agent2-email', default='staff1@demo.ge')
        parser.add_argument('--agent-without-social', action='store_true',
                            help='Variant: the agent loses the social_integrations feature')

    # ---------------------------------------------------------------- guards
    def _check_local(self):
        if not settings.DEBUG:
            raise CommandError('prepare_tickets_e2e is for local development only: settings.DEBUG must be True')
        host = (settings.DATABASES['default'].get('HOST') or '').strip()
        if host not in LOCAL_DB_HOSTS:
            raise CommandError(
                f'prepare_tickets_e2e refuses to run against database host "{host}"; '
                f'only {", ".join(LOCAL_DB_HOSTS)} are allowed')

    def handle(self, *args, **options):
        self._check_local()
        from tenants.models import Tenant

        schema = (options['schema'] or '').strip()
        if not schema or schema == get_public_schema_name():
            raise CommandError('--schema must name a tenant schema (not public)')
        if not Tenant.objects.filter(schema_name=schema).exists():
            raise CommandError(f"Tenant with schema '{schema}' not found")

        from tenants.demo_seed import muted_asterisk_sync, muted_push

        with schema_context(schema), muted_push(), muted_asterisk_sync(), transaction.atomic():
            if self._existing().exists() or self._existing_boards().exists():
                if not options['reset']:
                    raise CommandError('E2E fixtures already exist in this schema; pass --reset to recreate them')
                self._reset()
            ids = self._build(options)
        ids['schema'] = schema

        payload = json.dumps(ids, indent=2, sort_keys=True)
        if options['json_path']:
            with open(options['json_path'], 'w', encoding='utf-8') as fh:
                fh.write(payload + '\n')
            self.stderr.write(f"E2E fixtures ready in '{schema}': {len(ids['tickets'])} tickets, "
                              f"{len(ids['boards'])} boards; ids written to {options['json_path']}")
        else:
            self.stdout.write(payload)

    # ---------------------------------------------------------------- reset
    @staticmethod
    def _existing_boards():
        from tickets.models import Board
        return Board.objects.filter(name__startswith=BOARD_PREFIX)

    def _existing(self):
        from tickets.models import Ticket
        return Ticket.objects.filter(
            Q(title__startswith=TICKET_PREFIX) | Q(column__board__in=self._existing_boards())
        )

    def _reset(self):
        # Tickets first (the board delete would only null their column), then
        # boards (their columns cascade).
        from tickets.models import Ticket
        Ticket.objects.filter(pk__in=list(self._existing().values_list('pk', flat=True))).delete()
        self._existing_boards().delete()

    # ---------------------------------------------------------------- build
    def _user(self, email, *, staff):
        from users.models import User
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            local = email.split('@')[0]
            user = User.objects.create_user(
                email=email, password=None, first_name=local.capitalize(), last_name='E2E',
                role='admin' if staff else 'agent', is_staff=staff, is_superuser=staff,
            )
        return user

    def _build(self, options):
        from social_integrations.models import Client, SocialAccount
        from tenants.feature_models import Feature
        from tickets.dates import tickets_today
        from tickets.models import (
            Board, ChecklistItem, Tag, Ticket, TicketAssignment, TicketAttachment, TicketColumn,
            TicketComment, TicketPayment,
        )
        from users.models import TenantGroup, User

        now = timezone.now()
        today = tickets_today()
        without_social = options['agent_without_social']

        admin = self._user(options['admin_email'], staff=True)
        agent = self._user(options['agent_email'], staff=False)
        agent2 = self._user(options['agent2_email'], staff=False)
        if len({admin.pk, agent.pk, agent2.pk}) != 3:
            raise CommandError('--admin-email, --agent-email and --agent2-email must be three different users')
        users = {'admin': admin, 'agent': agent, 'agent2': agent2}

        # The agent: non-staff, none of the board-admin / orders permissions.
        User.objects.filter(pk=agent.pk).update(
            is_staff=False, is_superuser=False, role='agent', is_active=True,
            can_manage_columns=False, can_create_boards=False, can_access_orders=False,
            can_view_boards=False, can_edit_boards=False, can_delete_boards=False,
            can_view_all_tickets=False,
        )
        if not admin.is_staff:
            User.objects.filter(pk=admin.pk).update(is_staff=True)
        for u in users.values():
            u.refresh_from_db()

        features = Feature.objects.filter(is_active=True)
        if without_social:
            features = features.exclude(key='social_integrations')
        group, _ = TenantGroup.objects.get_or_create(
            name=SUPPORT_GROUP_NAME,
            defaults={'description': 'E2E fixtures: board group of "E2E Support" (prepare_tickets_e2e)'},
        )
        if not group.is_active:
            group.is_active = True
            group.save(update_fields=['is_active'])
        group.features.set(features)
        if without_social:
            for other in agent.tenant_groups.exclude(pk=group.pk).filter(features__key='social_integrations'):
                agent.tenant_groups.remove(other)
        for u in (admin, agent, agent2):
            u.tenant_groups.add(group)

        # Boards and columns.
        board_ids, boards, columns = {}, {}, {}
        for position, (key, name, color, is_default, access, column_specs) in enumerate(BOARDS):
            board = Board.objects.create(
                name=name, description=f'E2E fixture board ({key})', color=color, position=100 + position,
                is_default=is_default, created_by=admin,
            )
            if access == 'support_group':
                board.board_groups.add(group)
            elif access == 'agents':
                board.board_users.add(agent, agent2)
            elif access == 'admin_only':
                board.board_users.add(admin)
            boards[key] = board
            columns[key] = {}
            for col_pos, (col_key, col_name, col_color, extra) in enumerate(column_specs):
                columns[key][col_key] = TicketColumn.objects.create(
                    board=board, name=col_name, color=col_color, position=col_pos, created_by=admin, **extra)
            board_ids[key] = {
                'id': board.id, 'name': name, 'color': color, 'access': access,
                'columns': {k: c.id for k, c in columns[key].items()},
            }

        tags = {}
        for name, color in TAG_COLORS.items():
            tags[name], _ = Tag.objects.get_or_create(name=name, defaults={'color': color, 'created_by': admin})

        customer = Client.objects.filter(name=CUSTOMER_NAME, email=CUSTOMER_EMAIL).first()
        if customer is None:
            customer = Client.objects.create(
                name=CUSTOMER_NAME, first_name='E2E', last_name='Customer', email=CUSTOMER_EMAIL,
                phone='+995555000123', notes='E2E fixture customer (prepare_tickets_e2e)', created_by=admin)
        SocialAccount.objects.get_or_create(
            platform='facebook', platform_id='e2e-customer-psid', account_connection_id='e2e-fb-page',
            defaults={'client': customer, 'display_name': CUSTOMER_NAME, 'is_auto_created': False},
        )

        # Tickets.
        ticket_ids, roles = {}, {}
        for spec in TICKETS:
            title = _ticket_title(spec)
            column = columns[spec['board']][spec['column']]
            assignee = users[spec['assignee']] if spec.get('assignee') else None
            ticket = Ticket(
                title=title,
                description=f'{spec["title"]}. E2E fixture ticket.',
                priority=spec['priority'],
                column=column,
                created_by=admin,
                assigned_to=assignee,
            )
            if 'due' in spec:
                ticket.due_date = today + timedelta(days=spec['due'])
            if spec.get('customer'):
                ticket.customer = customer
            if spec.get('order'):
                ticket.is_order = True
                ticket.price = Decimal('100.00')
                ticket.currency = 'GEL'
                ticket.amount_paid = Decimal('40.00')
                ticket.payment_due_date = today + timedelta(days=10)
            ticket.save()

            if assignee is not None:
                TicketAssignment.objects.create(ticket=ticket, user=assignee, role='primary', assigned_by=admin)
            for helper in spec.get('collaborators', ()):
                TicketAssignment.objects.create(ticket=ticket, user=users[helper], role='collaborator',
                                                assigned_by=admin)
            if spec['tags']:
                ticket.tags.add(*[tags[t] for t in spec['tags']])

            if spec.get('checklist'):
                total, checked = spec['checklist']
                for p in range(total):
                    is_checked = p < checked
                    ChecklistItem.objects.create(
                        ticket=ticket, text=CHECKLIST_TEXTS[p], position=p + 1, created_by=admin,
                        is_checked=is_checked, checked_by=agent if is_checked else None,
                        checked_at=now - timedelta(hours=total - p) if is_checked else None)

            if spec.get('attachments'):
                TicketAttachment.objects.create(
                    ticket=ticket, file=f'ticket_attachments/e2e/{ticket.id}-pixel.png', filename='pixel.png',
                    file_size=68, content_type='image/png', uploaded_by=admin)
                TicketAttachment.objects.create(
                    ticket=ticket, file=f'ticket_attachments/e2e/{ticket.id}-brief.pdf', filename='brief.pdf',
                    file_size=1024, content_type='application/pdf', uploaded_by=agent)

            if spec.get('customer'):
                TicketComment.objects.create(ticket=ticket, user=admin, is_internal=True,
                                             comment='Internal: customer is a VIP, reply within the hour.')
                TicketComment.objects.create(ticket=ticket, user=agent, is_internal=False,
                                             comment='Asked the courier for an ETA.')

            if spec.get('order'):
                TicketPayment.objects.create(ticket=ticket, amount=Decimal('40.00'), currency='GEL',
                                             payment_method='cash', processed_by=admin,
                                             payment_reference='E2E-PAY-1')

            ticket_ids[f"{TICKET_PREFIX}{spec['n']:03d}"] = {
                'id': ticket.id, 'title': title, 'role': spec['role'],
                'board': spec['board'], 'column': spec['column'],
            }
            roles[spec['role']] = ticket.id

        # Positions: a concrete 0..n-1 index per column in fixture order, then
        # the two "equal position" tickets share one index (R14 tie-breaks).
        by_column = {}
        for spec in TICKETS:
            by_column.setdefault((spec['board'], spec['column']), []).append(roles[spec['role']])
        for ids in by_column.values():
            for index, ticket_id in enumerate(ids):
                Ticket.objects.filter(pk=ticket_id).update(position_in_column=index)
        equal = Ticket.objects.get(pk=roles['equal_position_a']).position_in_column
        Ticket.objects.filter(pk=roles['equal_position_b']).update(position_in_column=equal)

        return {
            'users': {k: {'id': u.id, 'email': u.email, 'is_staff': u.is_staff} for k, u in users.items()},
            'groups': {'support_team': group.id},
            'boards': board_ids,
            'tags': {name: tag.id for name, tag in tags.items()},
            'customer': {'id': customer.id, 'name': customer.name, 'email': customer.email},
            'tickets': ticket_ids,
            'roles': roles,
            'variant': {'agent_without_social': without_social},
            'today': today.isoformat(),
        }
