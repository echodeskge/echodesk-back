"""
Tests for the people directory surface (quick-wins package F2):

* ``?lite=1`` on /api/users/ (list + retrieve), /api/users/directory/ and
  /api/tenant-groups/{id}/members/ → slim UserDirectory rows with a constant
  number of queries per page.
* search_fields / filterset_fields / ordering_fields on UserViewSet.
* Department write permissions (manage_users or is_staff).
* Duplicate tenant-group name on update → 400.
* Typing fixes on the full UserSerializer (full_name present, no phantom groups).
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status

from users.models import Department, TenantGroup
from users.tests.conftest import EchoDeskTenantTestCase

User = get_user_model()

USERS_URL = '/api/users/'
DIRECTORY_URL = '/api/users/directory/'
TENANT_GROUPS_URL = '/api/tenant-groups/'
DEPARTMENTS_URL = '/api/departments/'

DIRECTORY_KEYS = {
    'id', 'email', 'first_name', 'last_name', 'full_name',
    'phone_number', 'job_title', 'role', 'status', 'is_active', 'is_staff',
    'department', 'manager', 'manager_name', 'tenant_groups', 'last_login',
}


class DirectoryFixtureMixin:
    """A small org chart: a department, a manager, an agent in a group."""

    def build_directory(self):
        self.admin = self.create_admin()
        self.support = Department.objects.create(name='Support')
        self.sales = Department.objects.create(name='Sales')
        self.group = self.create_tenant_group(name='Tier 1')
        self.manager = self.create_user(
            email='nino@test.com', role='manager',
            first_name='Nino', last_name='Kapanadze',
            department=self.sales, job_title='Head of Sales',
        )
        self.agent = self.create_user(
            email='giorgi@test.com', role='agent',
            first_name='Giorgi', last_name='Beridze',
            phone_number='+995 555 12 34 56', job_title='Support Agent',
            department=self.support, manager=self.manager,
            last_login=timezone.now(),
        )
        self.agent.tenant_groups.add(self.group)
        self.inactive = self.create_user(
            email='old@test.com', role='viewer', is_active=False, status='inactive',
            first_name='Former', last_name='Employee',
        )


class TestUserListLite(DirectoryFixtureMixin, EchoDeskTenantTestCase):
    """``?lite=1`` on /api/users/ returns UserDirectory rows."""

    def setUp(self):
        super().setUp()
        self.build_directory()

    def _row(self, results, email):
        return next(r for r in results if r['email'] == email)

    def test_default_shape_unchanged(self):
        """Without lite the full serializer (with permissions etc.) is used."""
        resp = self.api_get(USERS_URL, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        row = self._row(resp.data['results'], self.agent.email)
        self.assertIn('permissions', row)
        self.assertIn('feature_keys', row)
        self.assertIn('all_permissions', row)
        # The full department object, not the slim reference.
        self.assertIn('employee_count', row['department'])

    def test_lite_rows_have_exactly_the_directory_keys(self):
        resp = self.api_get(f'{USERS_URL}?lite=1', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('results', resp.data)  # still paginated
        for row in resp.data['results']:
            self.assertEqual(set(row.keys()), DIRECTORY_KEYS)

    def test_lite_true_also_accepted(self):
        resp = self.api_get(f'{USERS_URL}?lite=true', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(set(resp.data['results'][0].keys()), DIRECTORY_KEYS)

    def test_lite_zero_is_the_full_shape(self):
        resp = self.api_get(f'{USERS_URL}?lite=0', user=self.agent)
        self.assertIn('permissions', resp.data['results'][0])

    def test_lite_row_values(self):
        resp = self.api_get(f'{USERS_URL}?lite=1', user=self.agent)
        row = self._row(resp.data['results'], self.agent.email)
        self.assertEqual(row['full_name'], 'Giorgi Beridze')
        self.assertEqual(row['phone_number'], '+995 555 12 34 56')
        self.assertEqual(row['job_title'], 'Support Agent')
        self.assertEqual(row['role'], 'agent')
        self.assertEqual(row['status'], 'active')
        self.assertTrue(row['is_active'])
        self.assertFalse(row['is_staff'])
        self.assertEqual(row['department'], {'id': self.support.id, 'name': 'Support'})
        self.assertEqual(row['manager'], self.manager.id)
        self.assertEqual(row['manager_name'], 'Nino Kapanadze')
        self.assertEqual(row['tenant_groups'], [{'id': self.group.id, 'name': 'Tier 1'}])
        self.assertIsNotNone(row['last_login'])

    def test_lite_row_nullables(self):
        resp = self.api_get(f'{USERS_URL}?lite=1', user=self.agent)
        row = self._row(resp.data['results'], self.admin.email)
        self.assertIsNone(row['department'])
        self.assertIsNone(row['manager'])
        self.assertIsNone(row['manager_name'])
        self.assertIsNone(row['last_login'])
        self.assertEqual(row['tenant_groups'], [])
        # full_name falls back to the email when the name is blank
        self.assertEqual(row['full_name'], self.admin.email)

    def test_lite_retrieve(self):
        resp = self.api_get(f'{USERS_URL}{self.agent.id}/?lite=1', user=self.manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(set(resp.data.keys()), DIRECTORY_KEYS)
        self.assertEqual(resp.data['manager_name'], 'Nino Kapanadze')

    def test_lite_is_readable_by_any_authenticated_user(self):
        viewer = self.create_viewer()
        resp = self.api_get(f'{USERS_URL}?lite=1', user=viewer)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_lite_page_costs_a_constant_number_of_queries(self):
        """select_related + prefetch_related: no per-row queries."""
        def query_count():
            with CaptureQueriesContext(connection) as ctx:
                resp = self.api_get(f'{USERS_URL}?lite=1&page_size=100', user=self.admin)
                self.assertEqual(resp.status_code, status.HTTP_200_OK)
            # tenant-schemas issues a `SET search_path` before every statement;
            # count the statements themselves.
            real = [q for q in ctx.captured_queries if not q['sql'].startswith('SET search_path')]
            return len(real), resp.data['count']

        small_queries, small_count = query_count()

        other_group = self.create_tenant_group(name='Tier 2')
        for i in range(12):
            u = self.create_user(
                email=f'bulk{i}@test.com', first_name=f'Bulk{i}',
                department=self.support if i % 2 else self.sales,
                manager=self.manager,
            )
            u.tenant_groups.add(self.group, other_group)

        big_queries, big_count = query_count()
        self.assertEqual(big_count, small_count + 12)
        self.assertEqual(
            big_queries, small_queries,
            'the lite list must not issue per-row queries',
        )
        # tenant lookup + count + rows + tenant_groups prefetch, nothing more
        self.assertLessEqual(big_queries, 4)

    def test_full_serializer_still_has_full_name_and_no_groups(self):
        resp = self.api_get(f'{USERS_URL}{self.agent.id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['full_name'], 'Giorgi Beridze')
        self.assertNotIn('groups', resp.data)
        self.assertIs(resp.data['is_booking_staff'], False)
        self.assertIsInstance(resp.data['permissions'], list)
        self.assertIsInstance(resp.data['feature_keys'], list)
        self.assertEqual(
            set(resp.data['all_permissions'].keys()), {'individual', 'group', 'all'}
        )

    def test_update_with_legacy_group_ids_does_not_crash(self):
        """User has no Django groups; the legacy ids are accepted and ignored."""
        resp = self.api_patch(
            f'{USERS_URL}{self.agent.id}/',
            {'first_name': 'Still', 'group_ids': [1, 2], 'user_permission_ids': [3]},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.first_name, 'Still')


class TestDirectoryAction(DirectoryFixtureMixin, EchoDeskTenantTestCase):
    """/api/users/directory/ is the typed, always-lite variant of the list."""

    def setUp(self):
        super().setUp()
        self.build_directory()

    def test_directory_is_paginated_lite_rows(self):
        resp = self.api_get(DIRECTORY_URL, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(set(resp.data.keys()), {'count', 'next', 'previous', 'results'})
        self.assertEqual(resp.data['count'], User.objects.count())
        for row in resp.data['results']:
            self.assertEqual(set(row.keys()), DIRECTORY_KEYS)

    def test_directory_honours_filters_and_search(self):
        resp = self.api_get(f'{DIRECTORY_URL}?department={self.support.id}', user=self.agent)
        self.assertEqual([r['email'] for r in resp.data['results']], [self.agent.email])

        resp = self.api_get(f'{DIRECTORY_URL}?search=kapan', user=self.agent)
        self.assertEqual([r['email'] for r in resp.data['results']], [self.manager.email])

        resp = self.api_get(f'{DIRECTORY_URL}?group={self.group.id}', user=self.agent)
        self.assertEqual([r['email'] for r in resp.data['results']], [self.agent.email])

    def test_directory_page_size(self):
        resp = self.api_get(f'{DIRECTORY_URL}?page_size=2', user=self.agent)
        self.assertEqual(len(resp.data['results']), 2)
        self.assertIsNotNone(resp.data['next'])


class TestUserSearchFilterOrdering(DirectoryFixtureMixin, EchoDeskTenantTestCase):
    """search_fields, filterset_fields and the ordering whitelist on /api/users/."""

    def setUp(self):
        super().setUp()
        self.build_directory()

    def emails(self, resp):
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return [u['email'] for u in resp.data['results']]

    # ── search ──

    def test_search_by_first_name(self):
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?search=giorgi', user=self.admin)),
                         [self.agent.email])

    def test_search_by_last_name(self):
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?search=Kapanadze', user=self.admin)),
                         [self.manager.email])

    def test_search_by_email(self):
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?search=old@test', user=self.admin)),
                         [self.inactive.email])

    def test_search_by_phone_number(self):
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?search=555 12', user=self.admin)),
                         [self.agent.email])

    def test_search_by_job_title(self):
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?search=head of', user=self.admin)),
                         [self.manager.email])

    def test_search_no_match_is_empty(self):
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?search=zzzz', user=self.admin)), [])

    # ── filters ──

    def test_filter_by_department(self):
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?department={self.sales.id}', user=self.admin)),
                         [self.manager.email])

    def test_filter_by_manager(self):
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?manager={self.manager.id}', user=self.admin)),
                         [self.agent.email])

    def test_filter_by_is_active(self):
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?is_active=false', user=self.admin)),
                         [self.inactive.email])
        self.assertNotIn(self.inactive.email,
                         self.emails(self.api_get(f'{USERS_URL}?is_active=true', user=self.admin)))

    def test_filter_by_is_staff(self):
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?is_staff=true', user=self.admin)),
                         [self.admin.email])
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?is_staff=1', user=self.admin)),
                         [self.admin.email])

    def test_existing_role_status_group_filters_still_work(self):
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?role=manager', user=self.admin)),
                         [self.manager.email])
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?status=inactive', user=self.admin)),
                         [self.inactive.email])
        self.assertEqual(self.emails(self.api_get(f'{USERS_URL}?group={self.group.id}', user=self.admin)),
                         [self.agent.email])

    def test_filters_combine(self):
        resp = self.api_get(
            f'{USERS_URL}?department={self.support.id}&role=agent&is_active=true&search=beridze',
            user=self.admin,
        )
        self.assertEqual(self.emails(resp), [self.agent.email])

    def test_invalid_choice_filter_is_a_400(self):
        resp = self.api_get(f'{USERS_URL}?role=bogus', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('role', resp.data)

    # ── ordering ──

    def test_ordering_by_whitelisted_field(self):
        resp = self.api_get(f'{USERS_URL}?ordering=first_name', user=self.admin)
        names = [u['first_name'] for u in resp.data['results']]
        self.assertEqual(names, sorted(names))

        resp = self.api_get(f'{USERS_URL}?ordering=-first_name', user=self.admin)
        names = [u['first_name'] for u in resp.data['results']]
        self.assertEqual(names, sorted(names, reverse=True))

    def test_ordering_by_department_name(self):
        resp = self.api_get(f'{USERS_URL}?ordering=department__name&lite=1', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_ordering_by_non_whitelisted_field_is_ignored(self):
        """full_name is computed; before the whitelist this was a 500."""
        resp = self.api_get(f'{USERS_URL}?ordering=full_name', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp = self.api_get(f'{USERS_URL}?ordering=password', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class TestTenantGroupMembersLite(DirectoryFixtureMixin, EchoDeskTenantTestCase):
    """``?lite=1`` on /api/tenant-groups/{id}/members/."""

    def setUp(self):
        super().setUp()
        self.build_directory()
        self.manager.tenant_groups.add(self.group)
        self.url = f'{TENANT_GROUPS_URL}{self.group.id}/members/'

    def test_default_members_shape_unchanged(self):
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 2)
        self.assertEqual(resp.data['group'], 'Tier 1')
        self.assertIn('permissions', resp.data['members'][0])

    def test_lite_members(self):
        resp = self.api_get(f'{self.url}?lite=1', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 2)
        self.assertEqual(resp.data['group'], 'Tier 1')
        self.assertEqual({m['email'] for m in resp.data['members']},
                         {self.agent.email, self.manager.email})
        for row in resp.data['members']:
            self.assertEqual(set(row.keys()), DIRECTORY_KEYS)
        agent_row = next(m for m in resp.data['members'] if m['email'] == self.agent.email)
        self.assertEqual(agent_row['manager_name'], 'Nino Kapanadze')
        self.assertEqual(agent_row['tenant_groups'], [{'id': self.group.id, 'name': 'Tier 1'}])

    def test_lite_members_constant_queries(self):
        def query_count():
            with CaptureQueriesContext(connection) as ctx:
                resp = self.api_get(f'{self.url}?lite=1', user=self.admin)
                self.assertEqual(resp.status_code, status.HTTP_200_OK)
            return len(ctx.captured_queries), resp.data['count']

        before, n_before = query_count()
        for i in range(10):
            u = self.create_user(email=f'member{i}@test.com', manager=self.manager,
                                 department=self.support)
            u.tenant_groups.add(self.group)
        after, n_after = query_count()
        self.assertEqual(n_after, n_before + 10)
        self.assertEqual(after, before)

    def test_tenant_group_search(self):
        self.create_tenant_group(name='Escalations', description='second line')
        resp = self.api_get(f'{TENANT_GROUPS_URL}?search=escal', user=self.agent)
        self.assertEqual([g['name'] for g in resp.data['results']], ['Escalations'])
        resp = self.api_get(f'{TENANT_GROUPS_URL}?search=second line', user=self.agent)
        self.assertEqual([g['name'] for g in resp.data['results']], ['Escalations'])

    def test_tenant_group_member_count_is_int(self):
        resp = self.api_get(f'{TENANT_GROUPS_URL}{self.group.id}/', user=self.agent)
        self.assertEqual(resp.data['member_count'], 2)
        self.assertEqual(resp.data['feature_keys'], [])


class TestTenantGroupDuplicateNameOnUpdate(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.a = self.create_tenant_group(name='Alpha')
        self.b = self.create_tenant_group(name='Beta')

    def test_patch_to_existing_name_is_400(self):
        resp = self.api_patch(f'{TENANT_GROUPS_URL}{self.b.id}/', {'name': 'Alpha'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('name', resp.data)
        self.b.refresh_from_db()
        self.assertEqual(self.b.name, 'Beta')

    def test_put_to_existing_name_is_400(self):
        resp = self.api_put(
            f'{TENANT_GROUPS_URL}{self.b.id}/',
            {'name': 'Alpha', 'description': 'x'},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('name', resp.data)

    def test_keeping_own_name_is_fine(self):
        resp = self.api_patch(
            f'{TENANT_GROUPS_URL}{self.b.id}/',
            {'name': 'Beta', 'description': 'renamed nothing'},
            user=self.admin,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_perform_update_integrity_error_maps_to_400(self):
        """A race past the serializer validator still yields 400, not 500."""
        from unittest.mock import patch
        from django.db import IntegrityError

        with patch('users.serializers.TenantGroupCreateSerializer.save',
                   side_effect=IntegrityError('duplicate key')):
            resp = self.api_patch(
                f'{TENANT_GROUPS_URL}{self.b.id}/', {'name': 'Gamma'}, user=self.admin,
            )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('name', resp.data)


class TestDepartmentPermissions(EchoDeskTenantTestCase):
    """Writes need manage_users or is_staff; reads stay open."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.agent = self.create_user(email='deptagent@test.com')
        self.dept = Department.objects.create(name='Existing')

    def test_agent_can_list_and_retrieve(self):
        resp = self.api_get(DEPARTMENTS_URL, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['results'][0]['employee_count'], 0)
        resp = self.api_get(f'{DEPARTMENTS_URL}{self.dept.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_agent_cannot_create(self):
        resp = self.api_post(DEPARTMENTS_URL, {'name': 'Rogue'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Department.objects.filter(name='Rogue').exists())

    def test_agent_cannot_update(self):
        resp = self.api_patch(f'{DEPARTMENTS_URL}{self.dept.id}/', {'name': 'Renamed'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.dept.refresh_from_db()
        self.assertEqual(self.dept.name, 'Existing')

    def test_agent_cannot_delete(self):
        resp = self.api_delete(f'{DEPARTMENTS_URL}{self.dept.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Department.objects.filter(id=self.dept.id).exists())

    def test_admin_can_write(self):
        resp = self.api_post(DEPARTMENTS_URL, {'name': 'Finance'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        dept_id = resp.data['id']
        resp = self.api_patch(f'{DEPARTMENTS_URL}{dept_id}/', {'name': 'Finance & Ops'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp = self.api_delete(f'{DEPARTMENTS_URL}{dept_id}/', user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_manage_users_flag_without_staff_can_write(self):
        hr = self.create_user(email='hr@test.com', role='agent', can_manage_users=True)
        resp = self.api_post(DEPARTMENTS_URL, {'name': 'People'}, user=hr)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_role_admin_can_write(self):
        boss = self.create_user(email='boss@test.com', role='admin')
        resp = self.api_post(DEPARTMENTS_URL, {'name': 'Board'}, user=boss)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_department_search(self):
        Department.objects.create(name='Logistics', description='warehouse')
        resp = self.api_get(f'{DEPARTMENTS_URL}?search=logi', user=self.agent)
        self.assertEqual([d['name'] for d in resp.data['results']], ['Logistics'])

    def test_include_inactive(self):
        Department.objects.create(name='Closed', is_active=False)
        names = [d['name'] for d in self.api_get(DEPARTMENTS_URL, user=self.agent).data['results']]
        self.assertNotIn('Closed', names)
        names = [d['name'] for d in
                 self.api_get(f'{DEPARTMENTS_URL}?include_inactive=true', user=self.agent).data['results']]
        self.assertIn('Closed', names)
