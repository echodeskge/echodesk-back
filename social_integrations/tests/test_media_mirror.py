"""
Mirroring Facebook / Instagram attachments off Meta's expiring CDN.

All network access is mocked (``requests.get`` inside media_mirror) and
storage is an in-memory fake, so these tests never reach Meta or Spaces and
do not depend on the wall clock (message timestamps are fixed).
"""
from datetime import datetime, timezone as dt_timezone
from io import StringIO
from unittest.mock import MagicMock, patch

import requests as requests_lib
from django.core.management import call_command
from django.test import override_settings

from social_integrations import signals as mirror_signals
from social_integrations import tasks
from social_integrations.models import FacebookMessage, InstagramMessage
from social_integrations.services import media_mirror
from .conftest import SocialIntegrationTestCase

MEDIA = 'https://spaces.test/media/'
FIXED_TS = datetime(2026, 6, 14, 10, 30, tzinfo=dt_timezone.utc)
CDN = 'https://scontent.xx.fbcdn.net/v/t1.15752-9/{}.jpg?oe=6870AAAA&oh=sig'
IG_CDN = 'https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id={}&signature=sig'

LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                            'LOCATION': 'media-mirror-tests'}}


def cdn_response(content=b'\x89PNGdata', content_type='image/jpeg', status_code=200,
                 length=None, chunks=None, url=None):
    resp = MagicMock(status_code=status_code)
    resp.headers = {'Content-Type': content_type}
    if length is not None:
        resp.headers['Content-Length'] = str(length)
    resp.url = url
    resp.iter_content.return_value = chunks if chunks is not None else [content]
    return resp


def graph_response(items, status_code=200, error=None):
    resp = MagicMock(status_code=status_code)
    body = {'error': error} if error else {'attachments': {'data': items}, 'id': 'm'}
    resp.json.return_value = body
    return resp


class FakeStorage:
    def __init__(self):
        self.files = {}
        self.saves = 0

    def exists(self, path):
        return path in self.files

    def save(self, path, content):
        self.saves += 1
        self.files[path] = content.read()
        return path

    def url(self, path):
        return MEDIA + path


class MediaMirrorTestCase(SocialIntegrationTestCase):
    def setUp(self):
        # Enabled per test: the tenant base class's setUpClass doesn't run
        # SimpleTestCase's class-level override_settings machinery. Enable it
        # BEFORE super().setUp() so it's undone after the base class's
        # middleware override (LIFO); the other order leaves TEST_MIDDLEWARE
        # in settings for every later test.
        overrides = override_settings(MEDIA_URL=MEDIA, CACHES=LOCMEM_CACHE)
        overrides.enable()
        self.addCleanup(overrides.disable)
        super().setUp()
        self.storage = FakeStorage()
        p = patch.object(media_mirror, 'default_storage', self.storage)
        p.start()
        self.addCleanup(p.stop)
        from django.core.cache import cache
        cache.clear()

    def fb_msg(self, attachments=None, **kwargs):
        attachments = attachments if attachments is not None else [{'type': 'image', 'url': CDN.format('a')}]
        kwargs.setdefault('timestamp', FIXED_TS)
        return self.create_fb_message(
            attachments=attachments,
            attachment_type=attachments[0]['type'] if attachments else '',
            attachment_url=attachments[0]['url'] if attachments else None,
            **kwargs,
        )

    def get_mock(self, *responses):
        return patch.object(media_mirror.requests, 'get', side_effect=list(responses))


class TestUrlClassification(MediaMirrorTestCase):
    def test_meta_hosts_only(self):
        self.assertTrue(media_mirror.is_meta_cdn_url(CDN.format('x')))
        self.assertTrue(media_mirror.is_meta_cdn_url(IG_CDN.format('x')))
        self.assertTrue(media_mirror.is_meta_cdn_url('https://scontent.cdninstagram.com/a.jpg'))
        self.assertFalse(media_mirror.is_meta_cdn_url('http://scontent.xx.fbcdn.net/a.jpg'))  # not https
        self.assertFalse(media_mirror.is_meta_cdn_url('https://evil.com/fbcdn.net/a.jpg'))
        self.assertFalse(media_mirror.is_meta_cdn_url('https://notfbcdn.net/a.jpg'))
        self.assertFalse(media_mirror.is_meta_cdn_url(None))

    def test_needs_mirroring(self):
        msg = self.fb_msg()
        self.assertTrue(media_mirror.needs_mirroring(msg))
        mirrored = self.fb_msg([{'type': 'image', 'url': MEDIA + 'social/x.jpg'}])
        self.assertFalse(media_mirror.needs_mirroring(mirrored))
        loc = self.fb_msg([{'type': 'location', 'url': 'https://l.facebook.com/l.php?u=maps'}])
        self.assertFalse(media_mirror.needs_mirroring(loc))


class TestMirrorOnIngest(MediaMirrorTestCase):
    def test_mirrors_and_rewrites_urls(self):
        original = CDN.format('a')
        msg = self.fb_msg([{'type': 'image', 'url': original}, {'type': 'video', 'url': CDN.format('v')}])
        with self.get_mock(cdn_response(b'img'), cdn_response(b'vid', 'video/mp4')) as get:
            result = media_mirror.mirror_message(msg, 'facebook')

        self.assertEqual(get.call_count, 2)
        self.assertTrue(result.ok)
        self.assertEqual(result.mirrored, 2)
        msg.refresh_from_db()
        first, second = msg.attachments
        self.assertTrue(first['url'].startswith(MEDIA + f'social/{self.tenant.schema_name}/facebook/2026/06/'))
        self.assertTrue(first['url'].endswith('.jpg'))
        self.assertEqual(first['source_url'], original)
        self.assertTrue(second['url'].endswith('.mp4'))
        self.assertEqual(msg.attachment_url, first['url'])
        self.assertFalse(media_mirror.needs_mirroring(msg))

    def test_idempotent(self):
        msg = self.fb_msg()
        with self.get_mock(cdn_response(b'img')):
            media_mirror.mirror_message(msg, 'facebook')
        msg.refresh_from_db()
        snapshot = list(msg.attachments)
        with self.get_mock() as get:  # any network call would raise StopIteration
            result = media_mirror.mirror_message(msg, 'facebook')
        self.assertEqual(get.call_count, 0)
        self.assertEqual(result.mirrored, 0)
        self.assertEqual(result.already, 1)
        msg.refresh_from_db()
        self.assertEqual(msg.attachments, snapshot)

    def test_same_bytes_stored_once(self):
        msg1, msg2 = self.fb_msg(), self.fb_msg([{'type': 'image', 'url': CDN.format('b')}])
        with self.get_mock(cdn_response(b'same'), cdn_response(b'same')):
            media_mirror.mirror_message(msg1, 'facebook')
            media_mirror.mirror_message(msg2, 'facebook')
        self.assertEqual(self.storage.saves, 1)
        msg1.refresh_from_db(); msg2.refresh_from_db()
        self.assertEqual(msg1.attachment_url, msg2.attachment_url)

    def test_instagram_lookaside_redirect(self):
        ig = self.create_ig_message(
            timestamp=FIXED_TS, attachment_type='image', attachment_url=IG_CDN.format(1),
            attachments=[{'type': 'image', 'url': IG_CDN.format(1)}],
        )
        resp = cdn_response(b'ig', 'image/png', url='https://scontent.cdninstagram.com/x.png')
        with self.get_mock(resp):
            result = media_mirror.mirror_message(ig, 'instagram')
        self.assertTrue(result.ok)
        ig.refresh_from_db()
        self.assertIn('/instagram/2026/06/', ig.attachments[0]['url'])
        self.assertTrue(ig.attachment_url.endswith('.png'))

    def test_redirect_off_meta_rejected(self):
        msg = self.fb_msg()
        with self.get_mock(cdn_response(url='https://evil.example/x.jpg')):
            result = media_mirror.mirror_message(msg, 'facebook')
        self.assertEqual(result.failures, [{'index': 0, 'reason': 'host_not_allowed'}])

    def test_legacy_attachment_url_only(self):
        msg = self.create_fb_message(timestamp=FIXED_TS, attachment_type='image',
                                     attachment_url=CDN.format('legacy'), attachments=[])
        with self.get_mock(cdn_response(b'old')):
            media_mirror.mirror_message(msg, 'facebook')
        msg.refresh_from_db()
        self.assertTrue(msg.attachment_url.startswith(MEDIA))
        self.assertEqual(msg.attachments[0]['source_url'], CDN.format('legacy'))

    def test_non_meta_urls_untouched(self):
        msg = self.fb_msg([{'type': 'fallback', 'url': 'https://example.com/page'}])
        with self.get_mock() as get:
            result = media_mirror.mirror_message(msg, 'facebook')
        self.assertEqual(get.call_count, 0)
        self.assertTrue(result.ok)
        msg.refresh_from_db()
        self.assertEqual(msg.attachments, [{'type': 'fallback', 'url': 'https://example.com/page'}])


class TestDownloadFailures(MediaMirrorTestCase):
    def assert_failure(self, response_or_exc, reason):
        msg = self.fb_msg()
        before = list(msg.attachments)
        with self.get_mock(response_or_exc):
            result = media_mirror.mirror_message(msg, 'facebook')
        self.assertEqual(result.failures, [{'index': 0, 'reason': reason}])
        msg.refresh_from_db()
        self.assertEqual(msg.attachments, before)  # untouched on failure
        self.assertEqual(self.storage.saves, 0)
        return result

    def test_expired_403(self):
        self.assert_failure(cdn_response(status_code=403, content_type='text/plain'), 'expired')

    def test_declared_oversize(self):
        self.assert_failure(cdn_response(length=media_mirror.MAX_BYTES + 1), 'too_large')

    def test_streamed_oversize(self):
        with patch.object(media_mirror, 'MAX_BYTES', 10):
            self.assert_failure(cdn_response(chunks=[b'123456', b'789012']), 'too_large')

    def test_timeout(self):
        result = self.assert_failure(requests_lib.Timeout('slow'), 'timeout')
        self.assertTrue(result.transient)

    def test_html_error_page_rejected(self):
        self.assert_failure(cdn_response(b'<html>', 'text/html'), 'bad_content_type')

    def test_server_error_is_transient(self):
        result = self.assert_failure(cdn_response(status_code=502), 'http_5xx')
        self.assertTrue(result.transient)


class TestSignalAndTask(MediaMirrorTestCase):
    def test_save_queues_task_on_commit(self):
        with patch.object(tasks.mirror_social_attachments_task, 'delay') as delay:
            with self.captureOnCommitCallbacks(execute=True):
                msg = self.fb_msg()
        delay.assert_called_once_with(self.tenant.schema_name, 'facebook', msg.pk)

    def test_text_only_and_unrelated_updates_do_not_queue(self):
        with patch.object(tasks.mirror_social_attachments_task, 'delay') as delay:
            with self.captureOnCommitCallbacks(execute=True):
                msg = self.create_fb_message(timestamp=FIXED_TS)  # no attachments
                media_msg = self.fb_msg()
            delay.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                media_msg.is_read_by_staff = True
                media_msg.save(update_fields=['is_read_by_staff'])
                msg.save()
        delay.assert_not_called()

    @override_settings(SOCIAL_MEDIA_MIRROR_ENABLED=False)
    def test_kill_switch(self):
        with patch.object(tasks.mirror_social_attachments_task, 'delay') as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.fb_msg()
        delay.assert_not_called()

    def test_broker_down_does_not_raise(self):
        with patch.object(tasks.mirror_social_attachments_task, 'delay', side_effect=OSError('broker')):
            with self.captureOnCommitCallbacks(execute=True):
                self.fb_msg()  # must not raise into the webhook

    def test_task_mirrors_in_tenant_schema(self):
        msg = self.fb_msg()
        with self.get_mock(cdn_response(b'img')):
            out = tasks.mirror_social_attachments_task.apply(
                args=[self.tenant.schema_name, 'facebook', msg.pk]).get()
        self.assertEqual(out['mirrored'], 1)
        msg.refresh_from_db()
        self.assertTrue(msg.attachment_url.startswith(MEDIA))

    def test_task_falls_back_to_graph_when_expired(self):
        msg = self.fb_msg()
        fresh = graph_response([{'mime_type': 'image/jpeg', 'image_data': {'url': CDN.format('fresh')}}])
        with self.get_mock(cdn_response(status_code=403), fresh, cdn_response(b'new')) as get:
            out = tasks.mirror_social_attachments_task.apply(
                args=[self.tenant.schema_name, 'facebook', msg.pk]).get()
        self.assertEqual(out, {'mirrored': 1, 'failures': []})
        graph_call = get.call_args_list[1]
        self.assertIn(f'/{msg.message_id}', graph_call.args[0])
        self.assertEqual(graph_call.kwargs['params']['access_token'], msg.page_connection.page_access_token)
        msg.refresh_from_db()
        self.assertEqual(msg.attachments[0]['source_url'], CDN.format('a'))


class TestRefreshEndpoint(MediaMirrorTestCase):
    URL = '/api/social/attachments/refresh/'

    def setUp(self):
        super().setUp()
        self.user = self.create_user(email='agent@test.com')

    def post(self, data, user=None):
        return self.api_post(self.URL, data, user=user or self.user)

    def test_success_returns_updated_attachments(self):
        msg = self.fb_msg()
        fresh = graph_response([{'mime_type': 'image/png', 'image_data': {'url': CDN.format('new')}}])
        with self.get_mock(cdn_response(status_code=403), fresh, cdn_response(b'p', 'image/png')):
            resp = self.post({'platform': 'facebook', 'message_id': msg.message_id})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['id'], msg.pk)
        self.assertEqual(resp.data['mirrored'], 1)
        self.assertTrue(resp.data['attachment_url'].startswith(MEDIA))
        self.assertTrue(resp.data['attachments'][0]['url'].endswith('.png'))
        msg.refresh_from_db()
        self.assertEqual(msg.attachment_url, resp.data['attachment_url'])

    def test_instagram_uses_page_token(self):
        page = self.create_fb_connection()
        ig_conn = self.create_ig_connection(fb_page=page)
        ig = self.create_ig_message(
            account_connection=ig_conn, timestamp=FIXED_TS, attachment_type='image',
            attachment_url=IG_CDN.format(9), attachments=[{'type': 'image', 'url': IG_CDN.format(9)}],
        )
        fresh = graph_response([{'image_data': {'url': CDN.format('ig')}}])
        with self.get_mock(cdn_response(status_code=403), fresh, cdn_response(b'i')) as get:
            resp = self.post({'platform': 'instagram', 'message_id': ig.message_id})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(get.call_args_list[1].kwargs['params']['access_token'], page.page_access_token)

    def test_failure_is_cached(self):
        msg = self.fb_msg()
        gone = graph_response([], status_code=400, error={'code': 100, 'message': 'nope'})
        with self.get_mock(cdn_response(status_code=403), gone):
            resp = self.post({'platform': 'facebook', 'message_id': msg.message_id})
        self.assertEqual(resp.status_code, 410)
        self.assertEqual(resp.data['error_code'], 'attachment_unavailable')

        with self.get_mock() as get:
            again = self.post({'platform': 'facebook', 'message_id': msg.message_id})
        self.assertEqual(again.status_code, 429)
        self.assertEqual(again.data['error_code'], 'refresh_cooldown')
        self.assertEqual(get.call_count, 0)

    def test_graph_without_matching_attachment(self):
        msg = self.fb_msg()
        with self.get_mock(cdn_response(status_code=403), graph_response([])):
            resp = self.post({'platform': 'facebook', 'message_id': msg.message_id})
        self.assertEqual(resp.status_code, 410)
        self.assertEqual(resp.data['reason'], 'not_in_graph')

    def test_requires_view_permission(self):
        msg = self.fb_msg()
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.post({'platform': 'facebook', 'message_id': msg.message_id})
        self.assertEqual(resp.status_code, 403)

    def test_requires_authentication(self):
        resp = self.api_post(self.URL, {'platform': 'facebook', 'message_id': 'x'})
        self.assertIn(resp.status_code, (401, 403))

    def test_hidden_assigned_chat_is_404(self):
        from social_integrations.models import ChatAssignment, SocialIntegrationSettings
        settings_obj = SocialIntegrationSettings.objects.first() or SocialIntegrationSettings.objects.create()
        settings_obj.hide_assigned_chats = True
        settings_obj.save()
        msg = self.fb_msg()
        other = self.create_user(email='other@test.com')
        ChatAssignment.objects.create(
            platform='facebook', conversation_id=msg.sender_id,
            account_id=msg.page_connection.page_id, assigned_user=other, status='active',
        )
        with self.get_mock() as get:
            resp = self.post({'platform': 'facebook', 'message_id': msg.message_id})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(get.call_count, 0)

    def test_deleted_or_unknown_message_is_404(self):
        msg = self.fb_msg(is_deleted=True)
        resp = self.post({'platform': 'facebook', 'message_id': msg.message_id})
        self.assertEqual(resp.status_code, 404)
        resp = self.post({'platform': 'facebook', 'message_id': 'nope'})
        self.assertEqual(resp.status_code, 404)

    def test_invalid_platform(self):
        resp = self.post({'platform': 'whatsapp', 'message_id': 'x'})
        self.assertEqual(resp.status_code, 400)


class TestBackfillCommand(MediaMirrorTestCase):
    def run_cmd(self, *args):
        out = StringIO()
        call_command('mirror_social_attachments', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_counts_only(self):
        self.fb_msg([{'type': 'image', 'url': CDN.format(1)}, {'type': 'video', 'url': CDN.format(2)}])
        self.fb_msg([{'type': 'image', 'url': MEDIA + 'social/done.jpg'}])  # already mirrored
        self.create_fb_message(timestamp=FIXED_TS)  # text only
        self.create_ig_message(timestamp=FIXED_TS, attachment_type='image', attachment_url=IG_CDN.format(3),
                               attachments=[{'type': 'image', 'url': IG_CDN.format(3)}])
        with self.get_mock() as get:
            out = self.run_cmd('--schema', self.tenant.schema_name, '--dry-run')
        self.assertEqual(get.call_count, 0)
        self.assertEqual(self.storage.saves, 0)
        self.assertIn('facebook: 1 message(s), 2 attachment(s) would be mirrored', out)
        self.assertIn('instagram: 1 message(s), 1 attachment(s) would be mirrored', out)
        self.assertIn('Dry run: 2 message(s), 3 attachment(s)', out)
        self.assertTrue(all(a['url'].startswith('https://scontent')
                            for a in FacebookMessage.objects.filter(attachments__icontains='fbcdn')
                            .values_list('attachments', flat=True)[0]))

    def test_since_and_limit(self):
        self.fb_msg(timestamp=datetime(2026, 5, 1, tzinfo=dt_timezone.utc))
        self.fb_msg([{'type': 'image', 'url': CDN.format('j1')}])
        self.fb_msg([{'type': 'image', 'url': CDN.format('j2')}])
        out = self.run_cmd('--schema', self.tenant.schema_name, '--dry-run', '--since', '2026-06-01')
        self.assertIn('facebook: 2 message(s)', out)
        out = self.run_cmd('--schema', self.tenant.schema_name, '--dry-run', '--limit', '1')
        self.assertIn('Dry run: 1 message(s)', out)

    def test_real_run_mirrors(self):
        msg = self.fb_msg()
        with self.get_mock(cdn_response(b'x')):
            out = self.run_cmd('--schema', self.tenant.schema_name, '--platform', 'facebook')
        self.assertIn('mirrored=1', out)
        msg.refresh_from_db()
        self.assertTrue(msg.attachment_url.startswith(MEDIA))

    def test_requires_target(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self.run_cmd('--dry-run')
