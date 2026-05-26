"""Webhook → tenant routing for social platforms.

Inbound webhooks (Facebook, Instagram, WhatsApp, TikTok, …) arrive with no
tenant context. This module resolves the owning tenant schema from the
platform's external account id using a single O(1) lookup against the
public-schema ``tenants.SocialPlatformRoute`` table, falling back to the legacy
O(N-tenants) scan (and self-healing the route) if the table has no row yet.

It also keeps ``SocialPlatformRoute`` eventually-consistent via ``post_save`` /
``post_delete`` signals on the per-platform connection models. The signal
wiring mirrors ``crm/signals.py``: handlers resolve the current tenant via
``connection.schema_name`` and bail on the public schema. Route writes are done
inside an explicit ``schema_context('public')`` so the row always lands in the
public table regardless of which schema the signal fired in.

All public functions are defensive — a routing-table hiccup must never crash an
inbound webhook or a connection save.
"""
from __future__ import annotations

import logging

from django.db import connection
from django.db.models.signals import post_delete, post_save
from tenant_schemas.utils import schema_context

logger = logging.getLogger(__name__)


# platform key → (connection model name in social_integrations.models, id field)
PLATFORM_CONFIG = {
    "facebook": ("FacebookPageConnection", "page_id"),
    "instagram": ("InstagramAccountConnection", "instagram_account_id"),
    "whatsapp": ("WhatsAppBusinessAccount", "phone_number_id"),
    "tiktok": ("TikTokShopAccount", "shop_id"),
}


def _current_schema():
    schema = getattr(connection, "schema_name", None)
    if not schema or schema == "public":
        return None
    return schema


def _connection_model(platform):
    from social_integrations import models as social_models

    model_name, _field = PLATFORM_CONFIG[platform]
    return getattr(social_models, model_name)


# ---------------------------------------------------------------------------
# Route table read/write (always against the public schema)
# ---------------------------------------------------------------------------


def route_lookup(platform, external_id):
    """Return the tenant schema for ``(platform, external_id)`` or ``None``."""
    if not external_id:
        return None
    try:
        from tenants.models import SocialPlatformRoute

        with schema_context("public"):
            route = (
                SocialPlatformRoute.objects.filter(
                    platform=platform, external_id=str(external_id), is_active=True
                )
                .values_list("tenant_schema", flat=True)
                .first()
            )
        return route
    except Exception:  # noqa: BLE001 — never break a webhook on a routing hiccup
        logger.exception(
            "route_lookup failed for platform=%s external_id=%s", platform, external_id
        )
        return None


def route_upsert(platform, external_id, tenant_schema, is_active=True):
    """Create/update the route row. Idempotent; safe to call from signals."""
    if not external_id or not tenant_schema:
        return
    try:
        from tenants.models import SocialPlatformRoute

        with schema_context("public"):
            SocialPlatformRoute.objects.update_or_create(
                platform=platform,
                external_id=str(external_id),
                defaults={"tenant_schema": tenant_schema, "is_active": is_active},
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "route_upsert failed for platform=%s external_id=%s tenant=%s",
            platform,
            external_id,
            tenant_schema,
        )


def route_deactivate(platform, external_id):
    """Mark a route inactive (on connection delete / deactivation)."""
    if not external_id:
        return
    try:
        from tenants.models import SocialPlatformRoute

        with schema_context("public"):
            SocialPlatformRoute.objects.filter(
                platform=platform, external_id=str(external_id)
            ).update(is_active=False)
    except Exception:  # noqa: BLE001
        logger.exception(
            "route_deactivate failed for platform=%s external_id=%s",
            platform,
            external_id,
        )


# ---------------------------------------------------------------------------
# Tenant resolution (O(1) lookup → legacy O(N) scan fallback → self-heal)
# ---------------------------------------------------------------------------


def _scan_tenants(platform, external_id):
    """Legacy O(N) fallback: iterate tenant schemas until the connection is found."""
    from tenants.models import Tenant

    _model_name, field = PLATFORM_CONFIG[platform]
    Model = _connection_model(platform)

    for tenant in Tenant.objects.exclude(schema_name="public"):
        try:
            with schema_context(tenant.schema_name):
                if Model.objects.filter(
                    **{field: external_id, "is_active": True}
                ).exists():
                    return tenant.schema_name
        except Exception:  # noqa: BLE001 — skip tenants whose tables aren't ready
            continue
    return None


def resolve_tenant(platform, external_id):
    """Resolve the owning tenant schema for an inbound webhook.

    Fast path: O(1) lookup in ``SocialPlatformRoute``. Slow path: the legacy
    full scan, which self-heals the route table on a hit so the next webhook is
    O(1). Returns ``None`` if no tenant owns the id.
    """
    if platform not in PLATFORM_CONFIG:
        raise ValueError(f"Unknown social platform: {platform}")
    if not external_id:
        return None

    schema = route_lookup(platform, external_id)
    if schema:
        return schema

    schema = _scan_tenants(platform, external_id)
    if schema:
        route_upsert(platform, external_id, schema)  # self-heal for next time
    return schema


# ---------------------------------------------------------------------------
# Signals — keep SocialPlatformRoute in sync with the connection models
# ---------------------------------------------------------------------------


def _make_save_handler(platform, field):
    def handler(sender, instance, **kwargs):
        schema = _current_schema()
        if not schema:
            return
        external_id = getattr(instance, field, None)
        if not external_id:
            return
        route_upsert(
            platform,
            external_id,
            schema,
            is_active=getattr(instance, "is_active", True),
        )

    return handler


def _make_delete_handler(platform, field):
    def handler(sender, instance, **kwargs):
        external_id = getattr(instance, field, None)
        route_deactivate(platform, external_id)

    return handler


# Keep strong references so the closures aren't garbage-collected (weak=False).
_REGISTERED_HANDLERS = []


def register_platform_route_signals():
    """Wire post_save/post_delete on every platform connection model.

    Called from ``SocialIntegrationsConfig.ready()``.
    """
    from social_integrations import models as social_models

    for platform, (model_name, field) in PLATFORM_CONFIG.items():
        Model = getattr(social_models, model_name, None)
        if Model is None:
            logger.warning("platform_routing: model %s not found", model_name)
            continue
        save_handler = _make_save_handler(platform, field)
        delete_handler = _make_delete_handler(platform, field)
        _REGISTERED_HANDLERS.extend([save_handler, delete_handler])
        post_save.connect(
            save_handler, sender=Model, weak=False,
            dispatch_uid=f"social_route_save_{platform}",
        )
        post_delete.connect(
            delete_handler, sender=Model, weak=False,
            dispatch_uid=f"social_route_delete_{platform}",
        )
