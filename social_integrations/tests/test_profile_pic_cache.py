"""
Unit tests for the profile-picture cache service.

The service must spend Graph API budget only on cache misses/stale
entries, negative-cache senders with no picture, and never lose an
existing stored copy when a refresh fails.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests as requests_lib
from django.utils import timezone

from social_integrations.models import CachedProfilePicture
from social_integrations.services import profile_pic_cache
from social_integrations.services.profile_pic_cache import (
    get_facebook_profile_pic,
    get_instagram_profile,
)
from .conftest import SocialIntegrationTestCase


def graph_pic_response(url='https://cdn.fbsbx.test/pic.jpg', silhouette=False):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'data': {'url': url, 'is_silhouette': silhouette}}
    return resp


def image_response(content=b'imgbytes', content_type='image/jpeg'):
    resp = MagicMock(status_code=200)
    resp.content = content
    resp.headers = {'Content-Type': content_type}
    return resp


class ProfilePicCacheTestCase(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        storage = MagicMock()
        storage.save.side_effect = lambda path, content: path
        storage.url.side_effect = lambda path: f'https://spaces.test/media/{path}'
        self._storage_patcher = patch.object(profile_pic_cache, 'default_storage', storage)
        self._storage_patcher.start()
        self.storage = storage

    def tearDown(self):
        self._storage_patcher.stop()
        super().tearDown()


class TestFacebookProfilePic(ProfilePicCacheTestCase):
    def test_miss_fetches_and_stores_copy(self):
        with patch.object(profile_pic_cache.requests, 'get',
                          side_effect=[graph_pic_response(), image_response()]) as mock_get:
            url = get_facebook_profile_pic('psid_1', 'token')

        self.assertIsNotNone(url)
        self.assertTrue(url.endswith('/facebook/psid_1.jpg'))
        self.assertEqual(mock_get.call_count, 2)  # one Graph call + one CDN download
        entry = CachedProfilePicture.objects.get(platform='facebook', platform_id='psid_1')
        self.assertEqual(entry.image_url, url)
        self.assertIsNotNone(entry.fetched_at)

    def test_fresh_cache_hit_makes_no_network_calls(self):
        CachedProfilePicture.objects.create(
            platform='facebook', platform_id='psid_2',
            image_url='https://spaces.test/media/a.jpg',
            fetched_at=timezone.now(),
        )
        with patch.object(profile_pic_cache.requests, 'get') as mock_get:
            url = get_facebook_profile_pic('psid_2', 'token')

        self.assertEqual(url, 'https://spaces.test/media/a.jpg')
        mock_get.assert_not_called()

    def test_silhouette_is_negative_cached(self):
        with patch.object(profile_pic_cache.requests, 'get',
                          return_value=graph_pic_response(silhouette=True)) as mock_get:
            url = get_facebook_profile_pic('psid_3', 'token')
        self.assertIsNone(url)
        self.assertEqual(mock_get.call_count, 1)

        # Second message from the same sender: no network at all.
        with patch.object(profile_pic_cache.requests, 'get') as mock_get2:
            url2 = get_facebook_profile_pic('psid_3', 'token')
        self.assertIsNone(url2)
        mock_get2.assert_not_called()

    def test_refused_refresh_keeps_old_picture(self):
        stale = timezone.now() - timedelta(days=30)
        CachedProfilePicture.objects.create(
            platform='facebook', platform_id='psid_4',
            image_url='https://spaces.test/media/old.jpg',
            storage_path='social_avatars/t/facebook/psid_4.jpg',
            fetched_at=stale,
        )
        refused = MagicMock(status_code=400)
        refused.json.return_value = {'error': {'code': 100}}
        with patch.object(profile_pic_cache.requests, 'get', return_value=refused):
            url = get_facebook_profile_pic('psid_4', 'token')

        self.assertEqual(url, 'https://spaces.test/media/old.jpg')
        entry = CachedProfilePicture.objects.get(platform='facebook', platform_id='psid_4')
        self.assertEqual(entry.image_url, 'https://spaces.test/media/old.jpg')
        # Refusal is negative-cached so the next messages don't burn budget.
        self.assertGreater(entry.fetched_at, stale)

    def test_failed_download_keeps_old_picture(self):
        stale = timezone.now() - timedelta(days=30)
        CachedProfilePicture.objects.create(
            platform='facebook', platform_id='psid_5',
            image_url='https://spaces.test/media/old.jpg',
            storage_path='social_avatars/t/facebook/psid_5.jpg',
            fetched_at=stale,
        )
        dead_cdn = MagicMock(status_code=200)
        dead_cdn.raise_for_status.side_effect = requests_lib.exceptions.HTTPError('410')
        with patch.object(profile_pic_cache.requests, 'get',
                          side_effect=[graph_pic_response(), dead_cdn]):
            url = get_facebook_profile_pic('psid_5', 'token')

        self.assertEqual(url, 'https://spaces.test/media/old.jpg')

    def test_network_error_returns_stale_and_retries_later(self):
        stale = timezone.now() - timedelta(days=30)
        CachedProfilePicture.objects.create(
            platform='facebook', platform_id='psid_6',
            image_url='https://spaces.test/media/old.jpg',
            fetched_at=stale,
        )
        with patch.object(profile_pic_cache.requests, 'get',
                          side_effect=requests_lib.exceptions.ConnectionError('boom')):
            url = get_facebook_profile_pic('psid_6', 'token')

        self.assertEqual(url, 'https://spaces.test/media/old.jpg')
        entry = CachedProfilePicture.objects.get(platform='facebook', platform_id='psid_6')
        # fetched_at not advanced: a later message retries the refresh.
        self.assertEqual(entry.fetched_at, stale)


class TestInstagramProfile(ProfilePicCacheTestCase):
    def test_profile_and_picture_cached(self):
        profile_resp = MagicMock(status_code=200)
        profile_resp.json.return_value = {
            'name': 'Nino Testishvili', 'username': 'nino.ge',
            'profile_pic': 'https://cdn.ig.test/p.jpg',
        }
        with patch.object(profile_pic_cache.requests, 'get',
                          side_effect=[profile_resp, image_response()]) as mock_get:
            profile = get_instagram_profile('ig_1', 'token')

        self.assertEqual(profile['name'], 'Nino Testishvili')
        self.assertEqual(profile['username'], 'nino.ge')
        self.assertTrue(profile['profile_pic'].endswith('/instagram/ig_1.jpg'))
        self.assertEqual(mock_get.call_count, 2)  # profile + CDN download, no /picture fallback

        with patch.object(profile_pic_cache.requests, 'get') as mock_get2:
            profile2 = get_instagram_profile('ig_1', 'token')
        mock_get2.assert_not_called()
        self.assertEqual(profile2['username'], 'nino.ge')
        self.assertEqual(profile2['profile_pic'], profile['profile_pic'])

    def test_picture_fallback_endpoint_used_when_profile_has_no_pic(self):
        profile_resp = MagicMock(status_code=200)
        profile_resp.json.return_value = {'name': 'Nika', 'username': 'nika'}
        with patch.object(profile_pic_cache.requests, 'get',
                          side_effect=[profile_resp, graph_pic_response(), image_response()]):
            profile = get_instagram_profile('ig_2', 'token')

        self.assertEqual(profile['name'], 'Nika')
        self.assertTrue(profile['profile_pic'].endswith('/instagram/ig_2.jpg'))

    def test_refused_profile_keeps_cached_identity(self):
        stale = timezone.now() - timedelta(days=30)
        CachedProfilePicture.objects.create(
            platform='instagram', platform_id='ig_3',
            image_url='https://spaces.test/media/old.jpg',
            display_name='Old Name', username='old.username',
            fetched_at=stale,
        )
        refused = MagicMock(status_code=400)
        refused.json.return_value = {'error': {'code': 100}}
        with patch.object(profile_pic_cache.requests, 'get', return_value=refused):
            profile = get_instagram_profile('ig_3', 'token')

        self.assertEqual(profile['name'], 'Old Name')
        self.assertEqual(profile['username'], 'old.username')
        self.assertEqual(profile['profile_pic'], 'https://spaces.test/media/old.jpg')
