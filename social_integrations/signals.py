"""Queue attachment mirroring when a Facebook / Instagram message is saved.

The webhook only pays for a cheap in-memory URL check plus (after commit) a
Celery enqueue; the download itself runs in
``tasks.mirror_social_attachments_task``. See services/media_mirror.py.
"""
import logging

from django.conf import settings
from django.db import connection, transaction
from django.db.models.signals import post_save

logger = logging.getLogger(__name__)

_WATCHED_FIELDS = {'attachments', 'attachment_url'}


def _enqueue(schema_name, platform, pk):
    try:
        from social_integrations.tasks import mirror_social_attachments_task
        mirror_social_attachments_task.delay(schema_name, platform, pk)
    except Exception as exc:  # noqa: BLE001 — broker down: the lazy refresh endpoint / backfill catch up
        logger.warning('Could not queue attachment mirroring for %s/%s: %s', platform, pk, exc)


def _make_handler(platform):
    def handler(sender, instance, created=False, update_fields=None, raw=False, **kwargs):
        if raw or not getattr(settings, 'SOCIAL_MEDIA_MIRROR_ENABLED', True):
            return
        if update_fields is not None and not (_WATCHED_FIELDS & set(update_fields)):
            return
        from social_integrations.services.media_mirror import needs_mirroring
        if not needs_mirroring(instance):
            return
        schema_name = getattr(connection, 'schema_name', None)
        if not schema_name or schema_name == 'public':
            return
        pk = instance.pk
        transaction.on_commit(lambda: _enqueue(schema_name, platform, pk))
    handler.__name__ = f'mirror_{platform}_attachments_on_save'
    return handler


_facebook_handler = _make_handler('facebook')
_instagram_handler = _make_handler('instagram')


def register_media_mirror_signals():
    from social_integrations.models import FacebookMessage, InstagramMessage

    post_save.connect(_facebook_handler, sender=FacebookMessage,
                      dispatch_uid='social_media_mirror_facebook')
    post_save.connect(_instagram_handler, sender=InstagramMessage,
                      dispatch_uid='social_media_mirror_instagram')
