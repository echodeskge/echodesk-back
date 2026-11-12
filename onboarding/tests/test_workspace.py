"""Workspace step: apply writes tenant name/language, industry, departments
and invites the team through the users invitation path; idempotent."""
from unittest.mock import patch

from django.contrib.auth import get_user_model

from onboarding.models import TenantOnboarding
from tenants.models import Tenant
from users.models import Department
from users.tests.conftest import EchoDeskTenantTestCase

User = get_user_model()
ONB = '/api/onboarding/'
APPLY = f'{ONB}workspace/apply/'


def answers(**overrides):
    base = {
        'company_name': '  Acme   Dental ',
        'preferred_language': 'ka',
        'industry': 'clinic',
        'departments': ['Reception', 'Doctors', 'reception '],
        'team': [
            {'email': 'Nino@Example.com', 'first_name': 'Nino', 'last_name': 'K.',
             'role': 'manager', 'department': 'Reception'},
            {'email': 'giga@example.com', 'first_name': 'Giga', 'last_name': '',
             'role': 'agent', 'department': None},
        ],
    }
    base.update(overrides)
    return base


@patch('users.invitations.send_user_invitation_email_task')
class WorkspaceStepTests(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        # Provisioning creates the first admin as a superuser.
        self.owner = self.create_user(
            email='owner@test.com', role='admin', is_staff=True, is_superuser=True,
            first_name='Owner', last_name='One',
        )

    def _apply(self, data, user=None):
        return self.api_post(APPLY, {'answers': data}, user=user or self.owner)

    def test_apply_writes_everything(self, mock_task):
        resp = self._apply(answers())
        self.assertEqual(resp.status_code, 200, resp.data)

        tenant = Tenant.objects.get(pk=self.tenant.pk)
        self.assertEqual(tenant.name, 'Acme Dental')
        self.assertEqual(tenant.preferred_language, 'ka')
        state = TenantOnboarding.get()
        self.assertEqual(state.industry, 'clinic')
        self.assertEqual(state.status_of('workspace'), 'done')
        self.assertEqual(state.answers_of('workspace')['company_name'], 'Acme Dental')
        self.assertIsNotNone(state.started_at)
        self.assertIsNotNone(state.finished_at)  # only step → finished

        self.assertEqual(
            sorted(Department.objects.values_list('name', flat=True)), ['Doctors', 'Reception'],
        )

        nino = User.objects.get(email='nino@example.com')
        self.assertEqual(nino.role, 'manager')
        self.assertEqual(nino.department.name, 'Reception')
        self.assertEqual(nino.first_name, 'Nino')
        self.assertTrue(nino.password_change_required)
        self.assertTrue(nino.temporary_password and len(nino.temporary_password) >= 12)
        self.assertTrue(nino.check_password(nino.temporary_password))
        self.assertEqual(nino.invited_by, self.owner)
        self.assertIsNotNone(nino.invitation_sent_at)
        giga = User.objects.get(email='giga@example.com')
        self.assertEqual(giga.role, 'agent')
        self.assertIsNone(giga.department)

        self.assertEqual(mock_task.delay.call_count, 2)
        kwargs = mock_task.delay.call_args_list[0].kwargs
        self.assertEqual(kwargs['user_email'], 'nino@example.com')
        self.assertEqual(kwargs['tenant_name'], 'Acme Dental')
        self.assertEqual(kwargs['language'], 'ka')
        self.assertEqual(kwargs['invited_by'], 'Owner One')

        result = resp.data['result']
        self.assertEqual(result['created']['departments'], 2)
        self.assertEqual(result['created']['users'], 2)
        self.assertEqual(sorted(result['created']['user_ids']), sorted([nino.id, giga.id]))
        self.assertEqual(result['updated'], ['name', 'preferred_language', 'industry'])
        self.assertEqual(result['notes'], [])
        body = resp.data['step']
        self.assertEqual(body['status'], 'done')
        self.assertEqual(body['answers']['industry'], 'clinic')
        self.assertEqual(body['prefill']['industry'], 'clinic')

    def test_apply_twice_creates_nothing_twice(self, mock_task):
        first = self._apply(answers())
        self.assertEqual(first.status_code, 200)
        second = self._apply(answers())
        self.assertEqual(second.status_code, 200, second.data)

        self.assertEqual(Department.objects.count(), 2)
        self.assertEqual(User.objects.filter(email__in=['nino@example.com', 'giga@example.com']).count(), 2)
        self.assertEqual(mock_task.delay.call_count, 2)
        result = second.data['result']
        self.assertEqual(result['created'], {'departments': 0, 'users': 0, 'user_ids': []})
        self.assertEqual(result['updated'], [])
        self.assertEqual(len(result['notes']), 2)
        self.assertTrue(all('already a member' in n for n in result['notes']))
        self.assertEqual(TenantOnboarding.objects.count(), 1)

    def test_existing_emails_are_skipped_and_noted(self, mock_task):
        self.create_user(email='giga@example.com', role='viewer')
        resp = self._apply(answers(team=answers()['team'] + [
            {'email': 'OWNER@test.com', 'first_name': 'Me', 'last_name': '', 'role': 'agent', 'department': None},
        ]))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['result']['created']['users'], 1)
        self.assertEqual(User.objects.get(email='giga@example.com').role, 'viewer')  # untouched
        notes = resp.data['result']['notes']
        self.assertEqual(len(notes), 2)
        self.assertIn('giga@example.com: already a member, skipped.', notes)
        self.assertTrue(any(n.startswith('owner@test.com') for n in notes))
        self.assertEqual(mock_task.delay.call_count, 1)

    def test_department_named_only_on_a_team_row_is_created(self, mock_task):
        resp = self._apply(answers(departments=[], team=[
            {'email': 'a@example.com', 'first_name': 'A', 'last_name': '', 'role': 'agent', 'department': 'Sales'},
        ]))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(User.objects.get(email='a@example.com').department.name, 'Sales')
        self.assertEqual(resp.data['result']['created']['departments'], 1)

    def test_existing_department_matched_case_insensitively(self, mock_task):
        Department.objects.create(name='reception')
        resp = self._apply(answers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Department.objects.filter(name__iexact='reception').count(), 1)
        self.assertEqual(User.objects.get(email='nino@example.com').department.name, 'reception')

    def test_admin_role_requires_a_superuser(self, mock_task):
        staff_admin = self.create_admin(email='staff@test.com')  # is_staff, not superuser
        data = answers(team=[
            {'email': 'boss@example.com', 'first_name': 'B', 'last_name': '', 'role': 'admin', 'department': None},
        ])
        resp = self._apply(data, user=staff_admin)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('team', resp.data['errors'])
        self.assertFalse(User.objects.filter(email='boss@example.com').exists())

        resp = self._apply(data, user=self.owner)
        self.assertEqual(resp.status_code, 200, resp.data)
        boss = User.objects.get(email='boss@example.com')
        self.assertEqual(boss.role, 'admin')
        self.assertFalse(boss.is_superuser)
        self.assertTrue(boss.is_admin)

    def test_validation_errors(self, mock_task):
        resp = self._apply(answers(preferred_language='de'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('preferred_language', resp.data['errors'])

        resp = self._apply(answers(industry='bakery'))
        self.assertIn('industry', resp.data['errors'])

        resp = self._apply(answers(company_name='   '))
        self.assertIn('company_name', resp.data['errors'])

        team = answers()['team']
        resp = self._apply(answers(team=team + [dict(team[0])]))
        self.assertIn('team', resp.data['errors'])

        resp = self._apply(answers(team=[{'email': 'not-an-email', 'role': 'agent'}]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('team', resp.data['errors'])

        self.assertEqual(User.objects.filter(email='nino@example.com').count(), 0)
        self.assertEqual(TenantOnboarding.get().status_of('workspace'), 'pending')

    def test_context_and_prefill(self, mock_task):
        Department.objects.create(name='Support')
        agent = self.create_user(email='zed@test.com', first_name='Zed', last_name='Z')
        body = self.api_get(ONB, user=self.owner).data
        step = body['steps'][0]
        users = {u['email']: u for u in step['context']['users']}
        self.assertEqual(users['zed@test.com'], {
            'id': agent.id, 'email': 'zed@test.com', 'name': 'Zed Z', 'role': 'agent', 'is_active': True,
        })
        self.assertEqual(step['context']['departments'], [{'id': Department.objects.get(name='Support').id, 'name': 'Support'}])
        self.assertEqual(step['context']['roles'], ['admin', 'manager', 'agent'])
        self.assertEqual([lang['key'] for lang in step['context']['languages']], ['ka', 'en', 'ru'])
        self.assertEqual(step['prefill'], {
            'company_name': self.tenant.name,
            'preferred_language': self.tenant.preferred_language,
            'industry': None,
            'departments': ['Support'],
            'team': [],
        })
