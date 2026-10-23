"""
Eager invalidation of the storefront response cache
(see ``ecommerce_crm.caching``).

Any write to data rendered by the public storefront endpoints rotates the
current tenant's cache version after commit. The version is per tenant, so
one product edit invalidates that tenant's whole storefront cache -- simple,
always correct, and cheap (one Redis SET).
"""
from django.db.models.signals import post_delete, post_save, pre_delete

from .caching import schedule_version_bump
from .models import (
    AttributeDefinition,
    EcommerceSettings,
    OrderItem,
    Product,
    ProductAttributeValue,
    ProductImage,
    ProductReview,
    ProductVariant,
    ProductVariantAttributeValue,
    ShippingMethod,
)

STOREFRONT_MODELS = (
    Product,
    ProductVariant,
    ProductImage,
    ProductAttributeValue,
    ProductVariantAttributeValue,
    AttributeDefinition,
    ProductReview,
    ShippingMethod,
    EcommerceSettings,
)


def _invalidate(sender, instance=None, raw=False, **kwargs):
    if raw:  # fixture loading
        return
    schedule_version_bump()


for _model in STOREFRONT_MODELS:
    post_save.connect(_invalidate, sender=_model, dispatch_uid=f'storefront_cache_save_{_model.__name__}')
    post_delete.connect(_invalidate, sender=_model, dispatch_uid=f'storefront_cache_delete_{_model.__name__}')


def _snapshot_variant_on_order_items(sender, instance, **kwargs):
    """Order lines keep a readable copy of a variant before it is deleted
    (directly or with its product): the FK is SET_NULL, and without this the
    order would no longer say which colour or length was sold."""
    if not OrderItem.objects.filter(variant=instance).exists():
        return
    from .serializers import order_item_variant_details
    OrderItem.objects.filter(variant=instance, variant_snapshot__isnull=True).update(
        variant_snapshot=order_item_variant_details(instance)
    )


pre_delete.connect(
    _snapshot_variant_on_order_items, sender=ProductVariant,
    dispatch_uid='order_item_variant_snapshot',
)
