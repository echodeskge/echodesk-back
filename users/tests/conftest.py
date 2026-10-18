"""
Shared test infrastructure for users app tests.

Uses TenantTestCase from tenant_schemas as the base for all test classes,
with a custom subclass that properly creates the Tenant with required fields
and reuses the tenant/schema across multiple test classes.
"""
# Monkeypatch tenant_schemas introspection: it doesn't implement
# get_sequences(), which Django's migration framework needs.  Borrow
# the real implementation from the standard PostgreSQL backend.
from django.db.backends.postgresql.introspection import (
    DatabaseIntrospection as _PGIntrospection,
)
from tenant_schemas.postgresql_backend.introspection import (
    DatabaseSchemaIntrospection,
)
DatabaseSchemaIntrospection.get_sequences = _PGIntrospection.get_sequences

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from tenant_schemas.test.cases import TenantTestCase
from tenant_schemas.utils import get_public_schema_name, get_tenant_model

from users.models import TenantGroup

User = get_user_model()

# Build a test-safe middleware list: replace custom tenant middleware
# with the standard one, and remove non-essential custom middleware.
_STRIPPED_MIDDLEWARE = {
    'amanati_crm.middleware.EchoDeskTenantMiddleware',
    'amanati_crm.middleware.BotBlockerMiddleware',
    'tenants.subscription_middleware.SubscriptionMiddleware',
    'amanati_crm.debug_middleware.TransactionDebugMiddleware',
    'amanati_crm.middleware.RequestLoggingMiddleware',
}
TEST_MIDDLEWARE = (
    ['tenant_schemas.middleware.TenantMiddleware']
    + [m for m in settings.MIDDLEWARE if m not in _STRIPPED_MIDDLEWARE]
)


class EchoDeskTenantTestCase(TenantTestCase):
    """
    Base test case that creates a tenant with all required fields
    and provides helper methods for creating users and making API requests.

    The tenant and schema are created once and reused across all test classes
    in a single test run.  The test database itself is destroyed after all
    tests finish.
    """

    @classmethod
    def setUpClass(cls):
        cls.sync_shared()
        cls.add_allowed_test_domain()
        TenantModel = get_tenant_model()

        # Ensure we're querying public schema for tenant lookup
        connection.set_schema_to_public()

        try:
            cls.tenant = TenantModel.objects.get(domain_url='tenant.test.com')
        except TenantModel.DoesNotExist:
            cls.tenant = TenantModel(
                domain_url='tenant.test.com',
                schema_name='test',
                name='Test Tenant',
                admin_email='tenantadmin@test.com',
                admin_name='Test Admin',
            )
            cls.tenant.save(verbosity=0)

        # auto_create_schema is False on the Tenant model.
        # Ensure the test schema exists and has all tenant app tables.
        # We can't use call_command('migrate') because tenant_schemas
        # intercepts it and routes to migrate_schemas, which hits a
        # get_sequences() NotImplementedError.  Instead, use Django's
        # MigrationExecutor directly inside the tenant schema.
        from tenant_schemas.utils import schema_exists
        if not schema_exists('test'):
            connection.cursor().execute('CREATE SCHEMA test')
        connection.set_tenant(cls.tenant)
        cls._run_tenant_migrations()

    @classmethod
    def _run_tenant_migrations(cls):
        """Apply all pending migrations inside the current tenant schema
        using Django's MigrationExecutor directly, bypassing tenant_schemas'
        broken migrate_schemas command."""
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
        if plan:
            executor.migrate(targets)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.remove_allowed_test_domain()
        # Do NOT delete tenant or drop schema — reuse across test classes.
        # The test database is destroyed after the entire test run.

    @classmethod
    def setup_test_middleware(cls):
        """Apply test middleware via override_settings."""
        from django.test.utils import override_settings
        return override_settings(MIDDLEWARE=TEST_MIDDLEWARE)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        # The storefront response cache lives in Redis, which TestCase's
        # rollback doesn't reset; start every test from a fresh version so
        # one test's cached responses can't leak into the next.
        from ecommerce_crm.caching import bump_version
        bump_version(self.tenant.schema_name)
        # Apply test middleware for the duration of the test
        self._middleware_override = self.__class__.setup_test_middleware()
        self._middleware_override.enable()

    def tearDown(self):
        self._middleware_override.disable()
        super().tearDown()

    # ── Helper: create users ──

    def create_user(self, email='agent@test.com', password='testpass123',
                    role='agent', **kwargs):
        return User.objects.create_user(
            email=email, password=password, role=role, **kwargs
        )

    def create_admin(self, email='admin@test.com', password='testpass123'):
        return self.create_user(
            email=email, password=password, role='admin',
            is_staff=True, can_manage_users=True, can_manage_groups=True,
        )

    def create_manager(self, email='manager@test.com', password='testpass123'):
        return self.create_user(
            email=email, password=password, role='manager',
        )

    def create_viewer(self, email='viewer@test.com', password='testpass123'):
        return self.create_user(
            email=email, password=password, role='viewer',
        )

    def create_tenant_group(self, name='Test Group', **kwargs):
        return TenantGroup.objects.create(name=name, **kwargs)

    # ── Helper: authenticated client ──

    def authenticated_client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    # ── Helper: API shortcuts (always set HTTP_HOST for tenant resolution) ──

    def api_get(self, url, user=None, **kwargs):
        client = self.authenticated_client(user) if user else self.client
        return client.get(url, HTTP_HOST='tenant.test.com', **kwargs)

    def api_post(self, url, data=None, user=None, **kwargs):
        client = self.authenticated_client(user) if user else self.client
        return client.post(
            url, data, format='json', HTTP_HOST='tenant.test.com', **kwargs
        )

    def api_patch(self, url, data=None, user=None, **kwargs):
        client = self.authenticated_client(user) if user else self.client
        return client.patch(
            url, data, format='json', HTTP_HOST='tenant.test.com', **kwargs
        )

    def api_put(self, url, data=None, user=None, **kwargs):
        client = self.authenticated_client(user) if user else self.client
        return client.put(
            url, data, format='json', HTTP_HOST='tenant.test.com', **kwargs
        )

    def api_delete(self, url, user=None, **kwargs):
        client = self.authenticated_client(user) if user else self.client
        return client.delete(url, HTTP_HOST='tenant.test.com', **kwargs)
