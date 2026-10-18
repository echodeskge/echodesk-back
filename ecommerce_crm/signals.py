"""
Eager invalidation of the storefront response cache
(see ``ecommerce_crm.caching``).

Any write to data rendered by the public storefront endpoints rotates the
current tenant's cache version after commit. The version is per tenant, so
one product edit invalidates that tenant's whole storefront cache -- simple,
always correct, and cheap (one Redis SET).
"""
from django.db.models.signals import post_delete, post_save

from .caching import schedule_version_bump
from .models import (
    AttributeDefinition,
    EcommerceSettings,
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
