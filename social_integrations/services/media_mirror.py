"""
Mirror Facebook / Instagram message attachments into our own storage.

Meta's CDN links (``scontent.*.fbcdn.net``, ``lookaside.fbsbx.com``,
``*.cdninstagram.com``) are signed and expire (``oe=`` parameter) — a few
weeks after a message arrives the stored URL answers 403 "URL signature
expired" and the image breaks in the inbox. This service copies the bytes to
``default_storage`` (public DO Spaces in production) and rewrites the stored
URLs to our permanent copy, keeping Meta's original link under
``attachments[i]['source_url']``.

Three entry points share the same code:

* ``mirror_message`` — on ingest (Celery task queued by a post_save signal,
  see ``social_integrations/signals.py``) and from the backfill command.
  Downloads the stored CDN links directly; costs no Graph API budget.
* ``refresh_message`` — for links that already expired. Asks the Graph API
  for fresh attachment URLs (``GET /{mid}?fields=attachments{...}``) and
  mirrors those. Used by ``POST /api/social/attachments/refresh/`` and the
  backfill command.
* ``needs_mirroring`` — cheap check (no I/O) used by the signal.

Everything runs in the *current* tenant schema; callers wrap it in
``schema_context``. Rewrites are idempotent: storage paths are content
addressed (sha256) and already-mirrored URLs are skipped.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

GRAPH_VERSION = 'v23.0'
GRAPH_TIMEOUT = 10
# (connect, read) — read is per-chunk, so a slow trickle still finishes.
DOWNLOAD_TIMEOUT = (5, 30)
MAX_BYTES = 25 * 1024 * 1024
CHUNK_SIZE = 64 * 1024

PLATFORMS = ('facebook', 'instagram')

# Only Meta's media CDNs are ever fetched (also keeps arbitrary "fallback"
# link attachments and SSRF targets out of the downloader).
META_HOST_SUFFIXES = ('fbcdn.net', 'fbsbx.com', 'cdninstagram.com')

# Attachment types that never carry downloadable media.
NON_MEDIA_TYPES = {'location', 'fallback'}

_ALLOWED_CONTENT_PREFIXES = ('image/', 'video/', 'audio/', 'application/')
_ALLOWED_TEXT_TYPES = {'text/plain', 'text/csv'}

_EXT_OVERRIDES = {
    'image/jpeg': 'jpg',
    'image/jpg': 'jpg',
    'image/png': 'png',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'video/mp4': 'mp4',
    'video/quicktime': 'mov',
    'audio/mpeg': 'mp3',
    'audio/mp4': 'm4a',
    'audio/aac': 'aac',
    'audio/ogg': 'ogg',
    'audio/wav': 'wav',
    'audio/x-wav': 'wav',
    'application/pdf': 'pdf',
    'text/plain': 'txt',
    'text/csv': 'csv',
}

# Graph error codes meaning "rate limited" — see profile_pic_cache.
_THROTTLE_ERROR_CODES = {4, 17, 32, 613, 80001, 80002, 80006}

# Failure reasons worth retrying later (network blips, Meta 5xx).
TRANSIENT_REASONS = {'timeout', 'network', 'http_5xx', 'storage_error', 'graph_throttled', 'graph_network'}


class MirrorError(Exception):
    def __init__(self, reason: str, detail: str = ''):
        super().__init__(f'{reason}: {detail}' if detail else reason)
        self.reason = reason


@dataclass
class MirrorResult:
    mirrored: int = 0
    already: int = 0
    failures: list = field(default_factory=list)  # [{'index': i, 'reason': str}]
    attachments: list = field(default_factory=list)
    attachment_url: Optional[str] = None
    attachment_type: str = ''

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def transient(self) -> bool:
        return bool(self.failures) and all(f['reason'] in TRANSIENT_REASONS for f in self.failures)

    def as_dict(self) -> dict:
        return {
            'mirrored': self.mirrored,
            'already_mirrored': self.already,
            'failures': self.failures,
            'attachments': self.attachments,
            'attachment_url': self.attachment_url,
            'attachment_type': self.attachment_type,
        }


# ── URL classification ─────────────────────────────────────────────────────

def is_meta_cdn_url(url) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not host:
        return False
    return any(host == s or host.endswith('.' + s) for s in META_HOST_SUFFIXES)


def is_mirrored_url(url) -> bool:
    media_url = getattr(settings, 'MEDIA_URL', '') or ''
    return bool(url) and isinstance(url, str) and bool(media_url) and url.startswith(media_url)


def _working_attachments(msg) -> list:
    """The message's attachments as a fresh list of dicts. Legacy rows that
    only filled ``attachment_url`` get a synthetic one-element list so every
    code path deals with a single shape."""
    atts = [dict(a) for a in (msg.attachments or []) if isinstance(a, dict)]
    if not atts and msg.attachment_url:
        atts = [{'type': msg.attachment_type or 'file', 'url': msg.attachment_url}]
    return atts


def _is_candidate(att) -> bool:
    return att.get('type') not in NON_MEDIA_TYPES and is_meta_cdn_url(att.get('url'))


def needs_mirroring(msg) -> bool:
    """True if the message still references a Meta CDN link we could copy."""
    if is_meta_cdn_url(msg.attachment_url):
        return True
    return any(isinstance(a, dict) and _is_candidate(a) for a in (msg.attachments or []))


# ── Download + store ───────────────────────────────────────────────────────

def _content_type_ok(content_type: str) -> bool:
    if not content_type:
        return True  # Meta sometimes omits it on files; size cap still applies
    return content_type.startswith(_ALLOWED_CONTENT_PREFIXES) or content_type in _ALLOWED_TEXT_TYPES


def download(url: str) -> tuple[bytes, str]:
    """Fetch a Meta CDN URL. Returns (bytes, content_type) or raises
    MirrorError with a short machine-readable reason."""
    if not is_meta_cdn_url(url):
        raise MirrorError('host_not_allowed')
    try:
        resp = requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
    except requests.Timeout:
        raise MirrorError('timeout')
    except requests.RequestException as exc:
        raise MirrorError('network', type(exc).__name__)
    try:
        final_url = getattr(resp, 'url', None) or url
        if isinstance(final_url, str) and final_url != url and not is_meta_cdn_url(final_url):
            raise MirrorError('host_not_allowed', 'redirected off Meta CDN')
        code = resp.status_code
        if code in (401, 403, 410):
            raise MirrorError('expired', str(code))
        if code == 404:
            raise MirrorError('not_found')
        if code >= 500:
            raise MirrorError('http_5xx', str(code))
        if code != 200:
            raise MirrorError(f'http_{code}')

        content_type = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if not _content_type_ok(content_type):
            raise MirrorError('bad_content_type', content_type)
        try:
            declared = int(resp.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > MAX_BYTES:
            raise MirrorError('too_large', str(declared))

        chunks, total = [], 0
        try:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_BYTES:
                    raise MirrorError('too_large', f'>{MAX_BYTES}')
                chunks.append(chunk)
        except requests.Timeout:
            raise MirrorError('timeout')
        except requests.RequestException as exc:
            raise MirrorError('network', type(exc).__name__)
        data = b''.join(chunks)
        if not data:
            raise MirrorError('empty')
        return data, content_type
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def _extension(content_type: str, url: str) -> str:
    if content_type in _EXT_OVERRIDES:
        return _EXT_OVERRIDES[content_type]
    if content_type:
        guessed = mimetypes.guess_extension(content_type)
        if guessed:
            return guessed.lstrip('.')
    path_ext = os.path.splitext(urlparse(url).path)[1].lstrip('.').lower()
    if path_ext.isalnum() and 0 < len(path_ext) <= 5:
        return path_ext
    return 'bin'


def current_schema() -> str:
    return getattr(connection, 'schema_name', None) or 'public'


def store(data: bytes, content_type: str, platform: str, when, source_url: str) -> str:
    """Save bytes under a content-addressed, tenant-scoped path and return
    the public URL. Re-storing the same bytes is a no-op."""
    digest = hashlib.sha256(data).hexdigest()
    when = when or timezone.now()
    path = (
        f'social/{current_schema()}/{platform}/{when:%Y}/{when:%m}/'
        f'{digest}.{_extension(content_type, source_url)}'
    )
    try:
        if not default_storage.exists(path):
            path = default_storage.save(path, ContentFile(data))
        return default_storage.url(path)
    except Exception as exc:  # noqa: BLE001 — storage outage
        raise MirrorError('storage_error', type(exc).__name__)


# ── Message-level operations ───────────────────────────────────────────────

def model_for(platform: str):
    from social_integrations.models import FacebookMessage, InstagramMessage
    return {'facebook': FacebookMessage, 'instagram': InstagramMessage}[platform]


def mirror_message(msg, platform: str, fresh_urls: Optional[dict] = None) -> MirrorResult:
    """Copy every Meta-hosted attachment of ``msg`` to our storage and
    rewrite the stored URLs.

    ``fresh_urls`` maps an attachment index to a fresh download URL obtained
    from the Graph API; that URL is downloaded instead of the (expired)
    stored one, but the stored link is what ``source_url`` keeps. When given,
    only those indices are processed.
    """
    fresh_urls = fresh_urls or {}
    working = _working_attachments(msg)
    result = MirrorResult()
    replacements: dict[str, str] = {}  # stored url -> our url

    for idx, att in enumerate(working):
        stored_url = att.get('url')
        if is_mirrored_url(stored_url):
            result.already += 1
            continue
        if att.get('type') in NON_MEDIA_TYPES or not stored_url:
            continue
        if fresh_urls and idx not in fresh_urls:
            continue  # refresh pass: only the attachments Graph re-issued
        source = fresh_urls.get(idx) or stored_url
        if not is_meta_cdn_url(source):
            continue
        if stored_url in replacements:
            continue
        try:
            data, content_type = download(source)
            replacements[stored_url] = store(data, content_type, platform, msg.timestamp, source)
            result.mirrored += 1
        except MirrorError as exc:
            result.failures.append({'index': idx, 'reason': exc.reason})
            logger.info(
                'media_mirror: %s message %s attachment %s not mirrored (%s)',
                platform, msg.pk, idx, exc.reason,
            )

    if replacements:
        _apply_replacements(msg, platform, replacements)
    result.attachments = list(msg.attachments or [])
    result.attachment_url = msg.attachment_url
    result.attachment_type = msg.attachment_type
    return result


def _apply_replacements(msg, platform: str, replacements: dict) -> None:
    """Rewrite URLs on the *current* DB row (re-read under a row lock so a
    concurrent webhook update is merged, not overwritten). ``.update()``
    bypasses post_save, so this never re-queues the mirror task."""
    Model = model_for(platform)
    with transaction.atomic():
        row = Model.objects.select_for_update().filter(pk=msg.pk).first()
        if row is None:
            return
        atts = _working_attachments(row)
        for att in atts:
            url = att.get('url')
            if url in replacements:
                att.setdefault('source_url', url)
                att['url'] = replacements[url]
        attachment_url = row.attachment_url
        if attachment_url in replacements:
            attachment_url = replacements[attachment_url]
        attachment_type = row.attachment_type or (atts[0].get('type', '') if atts else '')
        Model.objects.filter(pk=row.pk).update(
            attachments=atts,
            attachment_url=attachment_url,
            attachment_type=attachment_type,
        )
    msg.attachments = atts
    msg.attachment_url = attachment_url
    msg.attachment_type = attachment_type


# ── Graph API refresh ──────────────────────────────────────────────────────

def access_token_for(msg, platform: str) -> str:
    if platform == 'facebook':
        return msg.page_connection.page_access_token
    account = msg.account_connection
    page = account.facebook_page
    # IG messaging goes through the Messenger Platform with the page token
    # (see instagram_send_message); fall back to the IG token if unlinked.
    return (page.page_access_token if page else '') or account.access_token


def _fresh_kind(item: dict) -> str:
    if item.get('image_data'):
        return 'image'
    if item.get('video_data'):
        return 'video'
    return 'file'


def _stored_kind(att: dict) -> str:
    t = att.get('type') or ''
    if t in ('image', 'sticker'):
        return 'image'
    if t == 'video':
        return 'video'
    return 'file'


def fetch_fresh_attachments(msg, platform: str) -> list[dict]:
    """Ask the Graph API for the message's current attachment URLs.
    Returns ``[{'url', 'mime_type', 'kind'}]``; raises MirrorError."""
    token = access_token_for(msg, platform)
    if not token:
        raise MirrorError('no_token')
    try:
        resp = requests.get(
            f'https://graph.facebook.com/{GRAPH_VERSION}/{msg.message_id}',
            params={
                'fields': 'attachments{mime_type,image_data,video_data,file_url,name}',
                'access_token': token,
            },
            timeout=GRAPH_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise MirrorError('graph_network', type(exc).__name__)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200:
        err = (body or {}).get('error') or {}
        if err.get('code') in _THROTTLE_ERROR_CODES or resp.status_code == 429:
            raise MirrorError('graph_throttled')
        raise MirrorError('graph_error', f"{resp.status_code}/{err.get('code')}")
    items = ((body or {}).get('attachments') or {}).get('data') or []
    fresh = []
    for item in items:
        url = (
            (item.get('image_data') or {}).get('url')
            or (item.get('video_data') or {}).get('url')
            or item.get('file_url')
        )
        if url:
            fresh.append({'url': url, 'mime_type': item.get('mime_type', ''), 'kind': _fresh_kind(item)})
    return fresh


def pair_fresh_urls(working: list, fresh: list) -> dict:
    """Match Graph attachments to stored ones → ``{stored_index: fresh_url}``.
    Same count → by position (Meta keeps the order); otherwise by kind in
    order (image↔image_data, video↔video_data, rest↔file_url)."""
    media = [(i, a) for i, a in enumerate(working) if a.get('type') not in NON_MEDIA_TYPES]
    if len(media) == len(fresh):
        return {i: f['url'] for (i, _), f in zip(media, fresh)}
    pairs, used = {}, set()
    for i, att in media:
        kind = _stored_kind(att)
        for j, f in enumerate(fresh):
            if j not in used and f['kind'] == kind:
                pairs[i] = f['url']
                used.add(j)
                break
    return pairs


def refresh_message(msg, platform: str, initial: Optional[MirrorResult] = None) -> MirrorResult:
    """Mirror ``msg``'s attachments, fetching fresh URLs from the Graph API
    for any that can't be downloaded from their stored link. ``initial`` is
    a ``mirror_message`` result the caller already has (skips re-trying the
    direct download)."""
    result = initial if initial is not None else mirror_message(msg, platform)
    if result.ok:
        return result
    working = _working_attachments(msg)
    failed_idx = {f['index'] for f in result.failures}
    try:
        fresh = fetch_fresh_attachments(msg, platform)
    except MirrorError as exc:
        result.failures = [{'index': i, 'reason': exc.reason} for i in sorted(failed_idx)]
        return result
    pairs = {i: u for i, u in pair_fresh_urls(working, fresh).items() if i in failed_idx}
    if not pairs:
        result.failures = [{'index': i, 'reason': 'not_in_graph'} for i in sorted(failed_idx)]
        return result
    second = mirror_message(msg, platform, fresh_urls=pairs)
    second.mirrored += result.mirrored
    unmatched = failed_idx - set(pairs)
    second.failures = [f for f in second.failures if f['index'] in failed_idx] + [
        {'index': i, 'reason': 'not_in_graph'} for i in sorted(unmatched)
    ]
    return second
