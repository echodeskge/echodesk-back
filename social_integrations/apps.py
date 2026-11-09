from django.apps import AppConfig


class SocialIntegrationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'social_integrations'

    def ready(self):
        # Keep the public-schema SocialPlatformRoute table in sync with the
        # per-platform connection models so inbound webhooks resolve their
        # tenant via an O(1) lookup instead of scanning every schema.
        try:
            from social_integrations.platform_routing import (
                register_platform_route_signals,
            )

            register_platform_route_signals()
        except Exception:  # noqa: BLE001 — never break app init on signal wiring
            import logging

            logging.getLogger(__name__).exception(
                "Failed to register social platform route signals"
            )

        # Copy Meta CDN attachments (they expire) into our storage after a
        # FB/IG message is saved — see services/media_mirror.py.
        try:
            from social_integrations.signals import register_media_mirror_signals

            register_media_mirror_signals()
        except Exception:  # noqa: BLE001 — never break app init on signal wiring
            import logging

            logging.getLogger(__name__).exception(
                "Failed to register social media mirror signals"
            )
