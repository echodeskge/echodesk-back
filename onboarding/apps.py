from django.apps import AppConfig


class OnboardingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'onboarding'

    def ready(self):
        # Import every "<app>.onboarding" module of the tenant apps so their
        # steps register themselves (plus the built-in workspace step).
        from .registry import autodiscover
        autodiscover()
