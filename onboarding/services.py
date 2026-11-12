"""
Glue between the registry, the state row and the rest of the system:

- ``enabled_feature_keys(tenant)`` — the same source ``request.tenant_has_feature``
  uses (active subscription, active selected features).
- ``build_state_payload`` / ``build_step_payload`` — the GET body.
- ``initialize_for_tenant`` — provisioning trigger (``process_pending_tenants``).
- ``on_features_changed`` — subscription trigger (``sync_tenant_features``).

The two triggers never raise: provisioning and feature sync must not fail
because of onboarding.
"""
import logging

from django.db import transaction
from tenant_schemas.utils import schema_context, schema_exists

from . import registry
from .models import TenantOnboarding

logger = logging.getLogger(__name__)


def enabled_feature_keys(tenant):
    """Keys of the active features on the tenant's active subscription."""
    from tenants.models import TenantSubscription

    if tenant is None:
        return set()
    return set(
        TenantSubscription.objects.filter(
            tenant=tenant,
            is_active=True,
            selected_features__is_active=True,
        ).values_list('selected_features__key', flat=True)
    )


def visible_steps(tenant, enabled=None):
    """The steps shown to ``tenant`` (workspace + enabled modules)."""
    if enabled is None:
        enabled = enabled_feature_keys(tenant)
    return registry.steps_for(enabled)


def build_step_payload(step, state, tenant, request=None, include_context=True):
    entry = state.entry(step.key)
    payload = {
        'key': step.key,
        'feature_key': step.feature_key,
        'order': step.order,
        'status': entry['status'],
        'answers': entry['answers'],
        'prefill': _safe_call(step, 'prefill', tenant, state),
        'context': _safe_call(step, 'context', tenant, request) if include_context else {},
        'completed_at': entry['completed_at'],
    }
    return payload


def build_state_payload(state, tenant, request=None):
    enabled = enabled_feature_keys(tenant)
    steps = visible_steps(tenant, enabled)
    step_payloads = [build_step_payload(step, state, tenant, request) for step in steps]
    return {
        'industry': state.industry,
        'started_at': state.started_at,
        'finished_at': state.finished_at,
        'pending_count': sum(1 for p in step_payloads if p['status'] == 'pending'),
        'total': len(step_payloads),
        'steps': step_payloads,
    }


def _safe_call(step, method_name, *args):
    """``prefill`` / ``context`` are read helpers; a bug in one module's step
    must not take the whole wizard down."""
    try:
        value = getattr(step, method_name)(*args)
    except Exception:  # noqa: BLE001
        logger.exception('Onboarding step %r: %s failed', step.key, method_name)
        return {}
    return value if isinstance(value, dict) else {}


# ── triggers ──────────────────────────────────────────────────────────────

def initialize_for_tenant(tenant):
    """Provisioning hook: create the row, mark every shown step pending and
    run ``seed_defaults`` of every enabled module's step.

    Returns ``{"pending": [...keys...], "seeded": [...keys...], "errors": [...keys...]}``.
    Runs inside ``schema_context(tenant.schema_name)``.
    """
    summary = {'pending': [], 'seeded': [], 'errors': []}
    with schema_context(tenant.schema_name):
        enabled = enabled_feature_keys(tenant)
        steps = registry.steps_for(enabled)
        state = TenantOnboarding.get()
        state.mark_pending([s.key for s in steps])
        summary['pending'] = state.pending_keys(enabled)
        for step in steps:
            try:
                with transaction.atomic():
                    step.seed_defaults(tenant)
                summary['seeded'].append(step.key)
            except Exception:  # noqa: BLE001 - never fail provisioning
                logger.exception(
                    'Onboarding seed_defaults failed for step %r on tenant %s',
                    step.key, tenant.schema_name,
                )
                summary['errors'].append(step.key)
    return summary


def on_features_changed(tenant, enabled_keys, disabled_keys):
    """Subscription hook: newly enabled features → their step pending;
    disabled features → entry removed. Silently skips tenants whose schema
    (or onboarding table) does not exist yet."""
    enabled_keys = set(enabled_keys or ())
    disabled_keys = set(disabled_keys or ())
    if not enabled_keys and not disabled_keys:
        return None
    schema_name = getattr(tenant, 'schema_name', None)
    if not schema_name:
        return None
    try:
        if not schema_exists(schema_name):
            return None
        with schema_context(schema_name):
            steps_by_feature = {
                s.feature_key: s.key for s in registry.all_steps() if s.feature_key
            }
            to_add = [steps_by_feature[k] for k in enabled_keys if k in steps_by_feature]
            to_remove = [steps_by_feature[k] for k in disabled_keys if k in steps_by_feature]
            if not to_add and not to_remove:
                return None
            # Own savepoint: if the table is missing (schema mid-migration) the
            # enclosing transaction stays usable.
            with transaction.atomic():
                state = TenantOnboarding.get()
                added = state.mark_pending(to_add, save=False)
                removed = state.remove(to_remove, save=False)
                if added or removed:
                    state.refresh_finished(enabled_feature_keys(tenant), save=False)
                    state.save()
                return {'added': added, 'removed': removed}
    except Exception as exc:  # noqa: BLE001 - never break feature sync
        message = str(exc).lower()
        if 'does not exist' in message or 'relation' in message:
            logger.info('Onboarding tables not ready for %s, skipping (%s)', schema_name, exc)
        else:
            logger.exception('Onboarding feature-change hook failed for %s', schema_name)
    return None
