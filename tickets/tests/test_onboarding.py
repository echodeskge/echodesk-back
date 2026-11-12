"""Tickets onboarding step: permissions, context/prefill shapes, apply
(board, columns, team group, form, tags, tenant flags, Telegram),
idempotency, seed_defaults and the Ticket.save default-column fallback."""
from django.core.signing import Signer

from onboarding.models import TenantOnboarding
from onboarding.tests.helpers import enable_features
from tenants.models import Tenant
from tickets.models import Board, BoardTelegramConnection, Tag, Ticket, TicketColumn, TicketForm
from tickets.onboarding import COLUMN_PRESETS, TicketsStep
from users.models import TenantGroup
from users.tests.conftest import EchoDeskTenantTestCase

ONB = '/api/onboarding/'
APPLY = f'{ONB}tickets/apply/'


def answers(**overrides):
    base = {
        'purpose': 'support',
        'board_name': ' Customer  Support ',
        'form_fields': ['phone', 'deadline', {'label': 'Contract number', 'type': 'string'}, 'phone'],
        'tags': ['Urgent', 'VIP', ' urgent '],
        'only_superadmin_can_delete_tickets': True,
        'min_users_per_ticket': 2,
        'telegram': {'bot_token': '123:abc', 'chat_id': '-100123'},
    }
    base.update(overrides)
    return base


class TicketsStepTests(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        enable_features(self.tenant, 'ticket_management')
        self.owner = self.create_user(
            email='owner@test.com', role='admin', is_staff=True, is_superuser=True,
            first_name='Owner', last_name='One',
        )
        self.agent = self.create_user(email='agent@test.com', first_name='Agent', last_name='A')
        self.agent2 = self.create_user(email='agent2@test.com')

    def _apply(self, data, user=None):
        return self.api_post(APPLY, {'answers': data}, user=user or self.owner)

    def _step(self):
        for step in self.api_get(ONB, user=self.owner).data['steps']:
            if step['key'] == 'tickets':
                return step
        self.fail('tickets step not listed')

    # ── permissions ──

    def test_non_admin_gets_403(self):
        self.assertEqual(self._apply(answers(), user=self.agent).status_code, 403)
        self.assertEqual(self.api_get(ONB, user=self.agent).status_code, 403)

    # ── read shapes ──

    def test_context_and_prefill_shapes(self):
        step = self._step()
        self.assertEqual(step['feature_key'], 'ticket_management')
        self.assertEqual(step['order'], 10)
        ctx = step['context']
        self.assertEqual(set(ctx['users'][0].keys()), {'id', 'email', 'name', 'role', 'is_active'})
        self.assertEqual(ctx['groups'], [])
        self.assertFalse(ctx['has_boards'])
        self.assertEqual(set(ctx['presets'].keys()), {'support', 'tasks', 'jobs'})
        self.assertEqual(ctx['presets']['support'][0], {'en': 'New', 'ka': 'ახალი'})
        self.assertEqual([c['en'] for c in ctx['presets']['jobs']], ['New', 'Scheduled', 'In progress', 'Done'])
        self.assertEqual(ctx['board_names']['tasks'], {'en': 'Tasks', 'ka': 'დავალებები'})
        self.assertEqual(set(ctx['form_field_presets'].keys()), {'phone', 'address', 'site', 'deadline'})
        self.assertIn('salon', ctx['tag_presets'])

        prefill = step['prefill']
        self.assertEqual(prefill['purpose'], 'support')
        self.assertEqual(prefill['board_name'], 'Support')
        self.assertEqual(sorted(prefill['team_user_ids']), sorted([self.owner.id, self.agent.id, self.agent2.id]))
        self.assertEqual(prefill['form_fields'], ['phone'])
        self.assertFalse(prefill['only_superadmin_can_delete_tickets'])
        self.assertEqual(prefill['min_users_per_ticket'], 0)
        self.assertIsNone(prefill['telegram'])

    def test_prefill_follows_industry(self):
        state = TenantOnboarding.get()
        state.industry = 'logistics'
        state.save()
        prefill = self._step()['prefill']
        self.assertEqual(prefill['purpose'], 'jobs')
        self.assertEqual(prefill['board_name'], 'Jobs')
        self.assertEqual(prefill['form_fields'], ['phone', 'address', 'deadline'])
        self.assertEqual(len(prefill['tags']), 3)

    # ── apply ──

    def test_apply_creates_everything(self):
        resp = self._apply(answers(team_user_ids=[self.agent.id, self.owner.id]))
        self.assertEqual(resp.status_code, 200, resp.data)
        result = resp.data['result']
        self.assertEqual(result['created']['boards'], 1)
        self.assertEqual(result['created']['columns'], 4)
        self.assertEqual(result['created']['groups'], 1)
        self.assertEqual(result['created']['forms'], 1)
        self.assertEqual(result['created']['form_fields'], 3)
        self.assertEqual(result['created']['tags'], 2)
        self.assertEqual(result['created']['telegram_connections'], 1)
        self.assertEqual(sorted(result['updated']), ['min_users_per_ticket', 'only_superadmin_can_delete_tickets'])
        self.assertEqual(resp.data['step']['status'], 'done')

        board = Board.objects.get(name='Customer Support')
        self.assertTrue(board.is_default)
        self.assertEqual(board.created_by, self.owner)
        columns = list(board.columns.order_by('position'))
        self.assertEqual([c.name for c in columns], ['New', 'In progress', 'Waiting for customer', 'Resolved'])
        self.assertEqual([c.is_default for c in columns], [True, False, False, False])
        self.assertEqual([c.track_time for c in columns], [False, True, False, False])
        self.assertEqual([c.is_closed_status for c in columns], [False, False, False, True])

        group = TenantGroup.objects.get(name='Customer Support')
        self.assertEqual(set(group.members.values_list('id', flat=True)), {self.agent.id, self.owner.id})
        self.assertIn(group, board.board_groups.all())
        self.assertTrue(group.features.filter(key='ticket_management').exists())

        form = TicketForm.objects.get(title='Customer Support')
        self.assertTrue(form.is_default)
        self.assertEqual(
            form.custom_fields,
            [
                {'name': 'phone', 'label': 'Phone', 'type': 'string', 'required': False},
                {'name': 'deadline', 'label': 'Deadline', 'type': 'date', 'required': False},
                {'name': 'contract_number', 'label': 'Contract number', 'type': 'string', 'required': False},
            ],
        )

        self.assertEqual(sorted(Tag.objects.values_list('name', flat=True)), ['Urgent', 'VIP'])

        tenant = Tenant.objects.get(pk=self.tenant.pk)
        self.assertTrue(tenant.only_superadmin_can_delete_tickets)
        self.assertEqual(tenant.min_users_per_ticket, 2)

        connection = BoardTelegramConnection.objects.get(board=board)
        self.assertEqual(connection.chat_id, '-100123')
        self.assertEqual(connection.get_bot_token(), '123:abc')
        self.assertNotEqual(connection.bot_token, '123:abc')
        self.assertEqual(Signer().unsign(connection.bot_token), '123:abc')

        # A ticket without a column lands in the new default column.
        ticket = Ticket.objects.create(title='t', created_by=self.owner)
        self.assertEqual(ticket.column, columns[0])

        self.assertEqual(TenantOnboarding.get().answers_of('tickets')['board_name'], 'Customer Support')

    def test_apply_twice_creates_nothing_new(self):
        data = answers(team_user_ids=[self.agent.id])
        self.assertEqual(self._apply(data).status_code, 200)
        counts = (
            Board.objects.count(), TicketColumn.objects.count(), TenantGroup.objects.count(),
            TicketForm.objects.count(), Tag.objects.count(), BoardTelegramConnection.objects.count(),
        )
        resp = self._apply(data)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['result']['created']['boards'], 0)
        self.assertEqual(resp.data['result']['created']['columns'], 0)
        self.assertEqual(resp.data['result']['created']['forms'], 0)
        self.assertEqual(resp.data['result']['created']['form_fields'], 0)
        self.assertEqual(resp.data['result']['created']['tags'], 0)
        self.assertEqual(resp.data['result']['created']['telegram_connections'], 0)
        self.assertEqual(
            counts,
            (
                Board.objects.count(), TicketColumn.objects.count(), TenantGroup.objects.count(),
                TicketForm.objects.count(), Tag.objects.count(), BoardTelegramConnection.objects.count(),
            ),
        )
        self.assertEqual(TicketForm.objects.get(title='Customer Support').custom_fields.__len__(), 3)

    def test_partial_answers_only_touch_what_is_present(self):
        resp = self._apply({'purpose': 'tasks'})
        self.assertEqual(resp.status_code, 200, resp.data)
        board = Board.objects.get(name='Tasks')
        self.assertEqual([c.name for c in board.columns.order_by('position')], ['To do', 'In progress', 'Review', 'Done'])
        self.assertFalse(TenantGroup.objects.exists())
        self.assertFalse(TicketForm.objects.exists())
        self.assertFalse(Tag.objects.exists())
        self.assertFalse(BoardTelegramConnection.objects.exists())
        self.assertFalse(Tenant.objects.get(pk=self.tenant.pk).only_superadmin_can_delete_tickets)

    def test_georgian_names_when_tenant_language_is_ka(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(preferred_language='ka')
        resp = self._apply({'purpose': 'jobs', 'form_fields': ['phone']})
        self.assertEqual(resp.status_code, 200, resp.data)
        board = Board.objects.get(name='შეკვეთები')
        self.assertEqual(
            [c.name for c in board.columns.order_by('position')],
            [ka for _, ka, _ in COLUMN_PRESETS['jobs']],
        )
        self.assertEqual(TicketForm.objects.get().custom_fields[0]['label'], 'ტელეფონი')
        # The English preset names match the Georgian columns: nothing duplicated.
        Tenant.objects.filter(pk=self.tenant.pk).update(preferred_language='en')
        self._apply({'purpose': 'jobs', 'board_name': 'შეკვეთები'})
        self.assertEqual(board.columns.count(), 4)

    def test_second_board_is_not_default_and_says_so(self):
        self._apply({'purpose': 'support'})
        resp = self._apply({'purpose': 'tasks'})
        self.assertFalse(Board.objects.get(name='Tasks').is_default)
        self.assertTrue(Board.objects.get(name='Support').is_default)
        self.assertIn('"Support" stays the default board.', resp.data['result']['notes'])

    def test_validation_errors(self):
        resp = self._apply({'purpose': 'nope'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('purpose', resp.data['errors'])
        resp = self._apply({'purpose': 'support', 'team_user_ids': [999999]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('team_user_ids', resp.data['errors'])
        resp = self._apply({'purpose': 'support', 'form_fields': ['nope']})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('form_fields', resp.data['errors'])
        resp = self._apply({'purpose': 'support', 'min_users_per_ticket': 101})
        self.assertEqual(resp.status_code, 400)

    def test_delete_restriction_needs_staff_like_tenant_settings(self):
        manager = self.create_user(email='settings@test.com', can_manage_settings=True)
        resp = self._apply({'purpose': 'support', 'only_superadmin_can_delete_tickets': True}, user=manager)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('only_superadmin_can_delete_tickets', resp.data['errors'])
        self.assertFalse(Board.objects.exists())   # nothing saved
        # Sending the current value is fine.
        resp = self._apply({'purpose': 'support', 'only_superadmin_can_delete_tickets': False}, user=manager)
        self.assertEqual(resp.status_code, 200, resp.data)

    # ── seed_defaults + Ticket.save ──

    def test_seed_defaults_is_idempotent(self):
        step = TicketsStep()
        step.seed_defaults(self.tenant)
        step.seed_defaults(self.tenant)
        board = Board.objects.get()
        self.assertEqual(board.name, 'Main')
        self.assertTrue(board.is_default)
        self.assertEqual(board.created_by, self.owner)
        columns = list(board.columns.order_by('position'))
        self.assertEqual([c.name for c in columns], ['To do', 'In progress', 'Review', 'Done'])
        self.assertTrue(columns[0].is_default)
        self.assertTrue(columns[1].track_time)
        self.assertTrue(columns[3].is_closed_status)
        ticket = Ticket.objects.create(title='t', created_by=self.owner)
        self.assertEqual(ticket.column, columns[0])
        # An existing board (any name) means nothing is seeded.
        board.delete()
        Board.objects.create(name='Custom', created_by=self.owner)
        step.seed_defaults(self.tenant)
        self.assertEqual(Board.objects.count(), 1)

    def test_apply_adopts_the_untouched_seeded_board(self):
        step = TicketsStep()
        step.seed_defaults(self.tenant)
        resp = self._apply({'purpose': 'support'})
        self.assertEqual(resp.status_code, 200, resp.data)
        board = Board.objects.get()            # one board, not Main + Support
        self.assertEqual(board.name, 'Support')
        self.assertTrue(board.is_default)
        self.assertEqual(
            [c.name for c in board.columns.order_by('position')],
            ['New', 'In progress', 'Waiting for customer', 'Resolved'],
        )
        # A seeded board that already holds tickets is left alone.
        board.delete()
        step.seed_defaults(self.tenant)
        Ticket.objects.create(title='t', created_by=self.owner)
        resp = self._apply({'purpose': 'tasks'})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(sorted(Board.objects.values_list('name', flat=True)), ['Main', 'Tasks'])

    def test_seed_defaults_without_users_does_nothing(self):
        from django.contrib.auth import get_user_model
        get_user_model().objects.all().delete()
        TicketsStep().seed_defaults(self.tenant)
        self.assertFalse(Board.objects.exists())

    def test_ticket_save_falls_back_to_first_column_of_default_board(self):
        board = Board.objects.create(name='Main', is_default=True, created_by=self.owner)
        second = TicketColumn.objects.create(board=board, name='B', position=1, created_by=self.owner)
        first = TicketColumn.objects.create(board=board, name='A', position=0, created_by=self.owner)
        self.assertFalse(board.columns.filter(is_default=True).exists())
        ticket = Ticket.objects.create(title='t', created_by=self.owner)
        self.assertEqual(ticket.column, first)
        self.assertNotEqual(ticket.column, second)
