from django.apps import AppConfig


class EcommerceCrmConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ecommerce_crm'

    def ready(self):
        """Import schema extensions after app registry is ready"""
        from . import schema  # noqa
        from . import signals  # noqa: F401 - storefront cache invalidation
