from django.apps import AppConfig


class TicketsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tickets'
    verbose_name = 'Tickets'

    def ready(self):
        """Import signals when app is ready."""
        import tickets.signals  # noqa
        import tickets.timer  # noqa
