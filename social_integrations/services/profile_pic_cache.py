"""
Profile-picture cache for social platform senders.

Meta's Graph API ``/{user_id}/picture`` calls count against the app-level
platform rate limit (200 x daily active users per rolling hour — a small
budget for a business app), and the CDN URLs it returns expire after a
while. This service fetches each sender's picture at most once per
REFRESH_AFTER window, copies the bytes to our own storage (permanent URL),
and negative-caches senders with no visible picture so webhooks never burn
rate budget re-asking about them.

A failed refresh never discards an existing stored copy: old pictures keep
working even when Meta later refuses the lookup (privacy, deleted account).
"""
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, connection
from django.utils import timezone

from social_integrations.models import CachedProfilePicture

logger = logging.getLogger(__name__)

# How long a cached picture is trusted before we ask the Graph API again.
REFRESH_AFTER = timedelta(days=7)

GRAPH_VERSION = 'v23.0'
GRAPH_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 15

_CONTENT_TYPE_EXT = {
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/gif': 'gif',
    'image/webp': 'webp',
}


def is_cached_url(url):
    """True if the URL already points at our own storage."""
    return bool(url) and url.startswith(settings.MEDIA_URL)


def _is_fresh(entry):
    return entry.fetched_at is not None and timezone.now() - entry.fetched_at < REFRESH_AFTER


def _save_entry(entry):
    try:
        entry.save()
    except IntegrityError:
        # Concurrent webhook delivery created the same row first — theirs wins.
        logger.info(f"Cache row for {entry.platform}/{entry.platform_id} created concurrently, skipping save")


def store_picture_copy(entry, source_url):
    """
    Download source_url (a CDN link — not a Graph API call, so it costs no
    rate budget) and copy it to our storage. Returns True on success; on
    failure the entry is left untouched so an existing copy keeps working.
    """
    try:
        resp = requests.get(source_url, timeout=DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '').split(';')[0].strip().lower()
        if content_type and not content_type.startswith('image/'):
            logger.warning(
                f"Profile picture source for {entry.platform}/{entry.platform_id} "
                f"returned non-image content type {content_type}"
            )
            return False
        ext = _CONTENT_TYPE_EXT.get(content_type, 'jpg')
        schema = getattr(connection, 'schema_name', 'public') or 'public'
        path = f"social_avatars/{schema}/{entry.platform}/{entry.platform_id}.{ext}"
        old_path = entry.storage_path
        saved_path = default_storage.save(path, ContentFile(resp.content))
        entry.image_url = default_storage.url(saved_path)
        entry.storage_path = saved_path
        if old_path and old_path != saved_path:
            try:
                default_storage.delete(old_path)
            except Exception as e:
                logger.warning(f"Could not delete old avatar copy {old_path}: {e}")
        return True
    except Exception as e:
        logger.warning(f"Could not copy profile picture for {entry.platform}/{entry.platform_id}: {e}")
        return False


def get_facebook_profile_pic(sender_id, access_token):
    """
    Return the sender's profile picture URL served from our own storage,
    or None if the sender has no visible picture. Costs at most one Graph
    API call per sender per REFRESH_AFTER window.
    """
    entry = CachedProfilePicture.objects.filter(
        platform='facebook', platform_id=sender_id
    ).first()
    if entry and _is_fresh(entry):
        return entry.image_url or None
    if entry is None:
        entry = CachedProfilePicture(platform='facebook', platform_id=sender_id)

    try:
        resp = requests.get(
            f"https://graph.facebook.com/{GRAPH_VERSION}/{sender_id}/picture",
            params={'type': 'large', 'redirect': 'false', 'access_token': access_token},
            timeout=GRAPH_TIMEOUT,
        )
    except requests.RequestException as e:
        # Transient network problem: keep what we have and let a later
        # message retry (fetched_at is deliberately not advanced).
        logger.warning(f"Profile picture fetch failed for facebook/{sender_id}: {e}")
        return entry.image_url or None

    if resp.status_code == 200:
        data = resp.json().get('data', {})
        if data.get('url') and not data.get('is_silhouette', True):
            store_picture_copy(entry, data['url'])
        # else: no visible picture — keep any existing copy; a row with an
        # empty image_url acts as a negative cache entry.
    else:
        logger.warning(
            f"Graph API refused picture for facebook/{sender_id}: status={resp.status_code}"
        )

    entry.fetched_at = timezone.now()
    _save_entry(entry)
    return entry.image_url or None


def get_instagram_profile(sender_id, access_token):
    """
    Return {'name', 'username', 'profile_pic'} for an Instagram-scoped
    sender id, using the cache. Costs at most two Graph API calls per
    sender per REFRESH_AFTER window (profile lookup + /picture fallback).
    """
    entry = CachedProfilePicture.objects.filter(
        platform='instagram', platform_id=sender_id
    ).first()
    if entry and _is_fresh(entry):
        return {
            'name': entry.display_name,
            'username': entry.username,
            'profile_pic': entry.image_url or None,
        }
    if entry is None:
        entry = CachedProfilePicture(platform='instagram', platform_id=sender_id)

    pic_source = None
    try:
        resp = requests.get(
            f"https://graph.facebook.com/{GRAPH_VERSION}/{sender_id}",
            params={'fields': 'name,username,profile_pic', 'access_token': access_token},
            timeout=GRAPH_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            entry.display_name = data.get('name', '') or entry.display_name
            entry.username = data.get('username', '') or entry.username
            pic_source = data.get('profile_pic')
        else:
            logger.warning(
                f"Graph API refused profile for instagram/{sender_id}: status={resp.status_code}"
            )
    except requests.RequestException as e:
        logger.warning(f"Instagram profile fetch failed for {sender_id}: {e}")
        return {
            'name': entry.display_name,
            'username': entry.username,
            'profile_pic': entry.image_url or None,
        }

    if not pic_source:
        # Same fallback the webhook used before: the Facebook-style
        # /picture endpoint sometimes works when profile_pic is absent.
        try:
            pic_resp = requests.get(
                f"https://graph.facebook.com/{GRAPH_VERSION}/{sender_id}/picture",
                params={'type': 'large', 'redirect': 'false', 'access_token': access_token},
                timeout=GRAPH_TIMEOUT,
            )
            if pic_resp.status_code == 200:
                pic_data = pic_resp.json().get('data', {})
                if pic_data.get('url') and not pic_data.get('is_silhouette', True):
                    pic_source = pic_data['url']
        except requests.RequestException as e:
            logger.warning(f"Instagram /picture fallback failed for {sender_id}: {e}")

    if pic_source:
        store_picture_copy(entry, pic_source)

    entry.fetched_at = timezone.now()
    _save_entry(entry)
    return {
        'name': entry.display_name,
        'username': entry.username,
        'profile_pic': entry.image_url or None,
    }
