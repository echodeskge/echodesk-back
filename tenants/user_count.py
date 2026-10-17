"""
Billable user counting for tenant subscriptions.

``TenantSubscription.current_users`` is a stored counter that nothing used to
maintain (it was seeded with 1 at registration, or 0 for hand-created
tenants), so billing screens showed e.g. "Active users 0/50".

A *billable* user is an ``is_active`` user in the tenant's schema — the same
definition the tenant statistics / admin dashboards already use. The tenant's
own admin counts (registration flows seed ``current_users=1`` for them).

* ``count_billable_users(schema)`` — live count, the source of truth.
* ``sync_subscription_user_count(tenant)`` — recount and persist into
  ``current_users`` when it drifted. Called at read time by the subscription
  endpoints / limit checks, by the ``users.User`` signals and by the
  ``recount_tenant_users`` management command.
"""
import logging

from django.contrib.auth import get_user_model
from django.db import connection
from tenant_schemas.utils import get_public_schema_name, schema_context

logger = logging.getLogger(__name__)


def count_billable_users(schema_name):
    """Live count of billable (active) users in ``schema_name``."""
    User = get_user_model()
    if connection.schema_name == schema_name:
        return User.objects.filter(is_active=True).count()
    with schema_context(schema_name):
        return User.objects.filter(is_active=True).count()


def sync_subscription_user_count(tenant, subscription=None, dry_run=False):
    """
    Recount ``tenant``'s billable users and store it on its subscription.

    Returns ``(old_count, new_count)``, or ``None`` when the tenant has no
    subscription. ``subscription`` (if passed) is updated in memory too, so
    callers can keep using the instance. Only ``current_users`` is written
    (via a queryset update — no save signals, ``updated_at`` untouched).
    """
    from tenants.models import TenantSubscription

    if subscription is None:
        subscription = TenantSubscription.objects.filter(tenant=tenant).first()
        if subscription is None:
            return None

    old = subscription.current_users
    new = count_billable_users(tenant.schema_name)
    if new != old and not dry_run:
        TenantSubscription.objects.filter(pk=subscription.pk).update(current_users=new)
        subscription.current_users = new
    return old, new


def sync_current_schema_user_count():
    """Recount for the tenant whose schema is currently active (no-op on public)."""
    from tenants.models import Tenant

    schema = connection.schema_name
    if not schema or schema == get_public_schema_name():
        return None
    tenant = getattr(connection, 'tenant', None)
    if tenant is None or getattr(tenant, 'schema_name', None) != schema:
        tenant = Tenant.objects.filter(schema_name=schema).first()
        if tenant is None:
            return None
    try:
        return sync_subscription_user_count(tenant)
    except Exception:
        # Never let usage bookkeeping break a user write.
        logger.exception('Failed to sync user count for schema %s', schema)
        return None
