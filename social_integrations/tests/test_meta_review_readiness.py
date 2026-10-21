"""
Meta App Review readiness (Messenger Human Agent):

- 7-day bound / non-allowlisted tenants: send views refuse with a structured
  400 and never call Meta
- Meta outside-window errors are mapped to code=outside_messaging_window
- text+media: a rejected text half is reported and not stored as sent
- messaging_type / message_tag persisted on outgoing FB/IG rows + serialized
- rating requests are skipped once the 24h window has closed
- X-Hub-Signature-256 verification on FB/IG webhooks
- signed_request verification, data-deletion + deauthorize callbacks
"""
import base64
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from social_integrations import views as social_views
from social_integrations.meta_compliance import (
    SignedRequestError, SignedRequestUnverifiable, parse_signed_request,
)
from social_integrations.models import FacebookMessage, InstagramMessage
from social_integrations.serializers import FacebookMessageSerializer, InstagramMessageSerializer
from .conftest import SocialIntegrationTestCase

APP_SECRET = 'test-app-secret'
IG_SECRET = 'test-ig-secret'


def _social_settings(**overrides):
    from django.conf import settings
    cfg = dict(getattr(settings, 'SOCIAL_INTEGRATIONS', {}))
    cfg.update(overrides)
    return cfg


def make_signed_request(payload, secret=APP_SECRET):
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip('=')
    return f'{sig_b64}.{payload_b64}'


def _ok(message_id='<mid_ok>'):
    resp = MagicMock(status_code=200, content=b'{}')
    resp.json.return_value = {'message_id': message_id}
    return resp


def _meta_error(code, subcode=None, message='(#10) This message is sent outside of allowed window.'):
    body = {'error': {'code': code, 'message': message, 'type': 'OAuthException'}}
    if subcode is not None:
        body['error']['error_subcode'] = subcode
    resp = MagicMock(status_code=400, content=json.dumps(body).encode())
    resp.json.return_value = body
    return resp


class _SendBase(SocialIntegrationTestCase):
    allowlist = None  # None = HUMAN_AGENT tag enabled for the test schema

    def setUp(self):
        super().setUp()
        self._gate = patch.object(social_views, 'HUMAN_AGENT_TAG_SCHEMAS', self.allowlist)
        self._gate.start()
        self._ws = patch('social_integrations.consumers.send_new_message_notification')
        self._ws.start()
        self.agent = self.create_user(email=f'agent-{self._next()}@test.com')
        self.fb = self.create_fb_connection()
        self.ig = self.create_ig_connection(fb_page=self.fb)

    def tearDown(self):
        self._ws.stop()
        self._gate.stop()
        super().tearDown()

    def fb_send(self, age, post_side_effect=None, media=False, text='hello back'):
        self.create_fb_message(
            page_connection=self.fb, sender_id='psid_r', timestamp=timezone.now() - age,
        )
        mock_kwargs = {'side_effect': post_side_effect} if post_side_effect else {'return_value': _ok()}
        with patch('social_integrations.views.requests.post', **mock_kwargs) as mock_post:
            if media:
                client = self.authenticated_client(self.agent)
                resp = client.post('/api/social/facebook/send-message/', {
                    'recipient_id': 'psid_r', 'message': text, 'page_id': self.fb.page_id,
                    'media': SimpleUploadedFile('a.png', b'\x89PNG', content_type='image/png'),
                }, format='multipart', HTTP_HOST='tenant.test.com')
            else:
                resp = self.api_post('/api/social/facebook/send-message/', {
                    'recipient_id': 'psid_r', 'message': text, 'page_id': self.fb.page_id,
                }, user=self.agent)
        return resp, mock_post

    def ig_send(self, age, post_side_effect=None):
        self.create_ig_message(
            account_connection=self.ig, sender_id='igsid_r', timestamp=timezone.now() - age,
        )
        mock_kwargs = {'side_effect': post_side_effect} if post_side_effect else {'return_value': _ok('<ig_ok>')}
        with patch('social_integrations.views.requests.post', **mock_kwargs) as mock_post:
            resp = self.api_post('/api/social/instagram/send-message/', {
                'recipient_id': 'igsid_r', 'message': 'hi', 'instagram_account_id': self.ig.instagram_account_id,
            }, user=self.agent)
        return resp, mock_post


class TestSevenDayBound(_SendBase):
    def test_fb_older_than_7_days_refused_without_calling_meta(self):
        resp, mock_post = self.fb_send(timedelta(days=8))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'outside_messaging_window')
        self.assertEqual(resp.data['window'], 'closed')
        self.assertIn('7 days', resp.data['error'])
        mock_post.assert_not_called()

    def test_ig_older_than_7_days_refused_without_calling_meta(self):
        resp, mock_post = self.ig_send(timedelta(days=7, hours=1))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'outside_messaging_window')
        mock_post.assert_not_called()

    def test_fb_between_24h_and_7d_uses_human_agent_and_persists_it(self):
        resp, mock_post = self.fb_send(timedelta(days=3))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        payload = mock_post.call_args.kwargs['json']
        self.assertEqual(payload['messaging_type'], 'MESSAGE_TAG')
        self.assertEqual(payload['tag'], 'HUMAN_AGENT')
        self.assertEqual(resp.data['messaging_type'], 'MESSAGE_TAG')
        self.assertEqual(resp.data['message_tag'], 'HUMAN_AGENT')
        row = FacebookMessage.objects.get(message_id='<mid_ok>')
        self.assertEqual(row.messaging_type, 'MESSAGE_TAG')
        self.assertEqual(row.message_tag, 'HUMAN_AGENT')
        data = FacebookMessageSerializer(row).data
        self.assertEqual(data['messaging_type'], 'MESSAGE_TAG')
        self.assertEqual(data['message_tag'], 'HUMAN_AGENT')

    def test_fb_inside_24h_persists_response(self):
        resp, _ = self.fb_send(timedelta(hours=1))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        row = FacebookMessage.objects.get(message_id='<mid_ok>')
        self.assertEqual(row.messaging_type, 'RESPONSE')
        self.assertFalse(row.message_tag)

    def test_ig_human_agent_persisted(self):
        resp, mock_post = self.ig_send(timedelta(days=2))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(mock_post.call_args.kwargs['json']['tag'], 'HUMAN_AGENT')
        row = InstagramMessage.objects.get(message_id='<ig_ok>')
        self.assertEqual((row.messaging_type, row.message_tag), ('MESSAGE_TAG', 'HUMAN_AGENT'))
        data = InstagramMessageSerializer(row).data
        self.assertEqual(data['messaging_type'], 'MESSAGE_TAG')
        self.assertEqual(data['message_tag'], 'HUMAN_AGENT')

    def test_incoming_rows_serialize_null_mode(self):
        msg = self.create_fb_message(page_connection=self.fb)
        data = FacebookMessageSerializer(msg).data
        self.assertIsNone(data['messaging_type'])
        self.assertIsNone(data['message_tag'])


class TestNonAllowlistedTenant(_SendBase):
    allowlist = {'groot'}  # test schema is 'test'

    def test_fb_after_24h_refused_without_calling_meta(self):
        resp, mock_post = self.fb_send(timedelta(hours=30))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'outside_messaging_window')
        self.assertIn('24 hours', resp.data['error'])
        mock_post.assert_not_called()

    def test_ig_after_24h_refused_without_calling_meta(self):
        resp, mock_post = self.ig_send(timedelta(hours=30))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'outside_messaging_window')
        mock_post.assert_not_called()

    def test_fb_inside_24h_still_sends(self):
        resp, mock_post = self.fb_send(timedelta(hours=2))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(mock_post.call_args.kwargs['json']['messaging_type'], 'RESPONSE')

    def test_messaging_window_reports_reply_mode(self):
        self.create_fb_message(page_connection=self.fb, sender_id='psid_w',
                               timestamp=timezone.now() - timedelta(hours=30))
        resp = self.api_get(
            f'/api/social/messaging-window/?platform=facebook&conversation_id=psid_w&account_id={self.fb.page_id}',
            user=self.agent,
        )
        self.assertEqual(resp.data['reply_mode'], 'closed')
        self.assertFalse(resp.data['human_agent_tag_enabled'])


class TestStructuredMetaErrors(_SendBase):
    def _assert_mapped(self, resp, code, subcode):
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'outside_messaging_window')
        self.assertEqual(resp.data['window'], 'closed')
        self.assertEqual(resp.data['meta_error']['code'], code)
        self.assertEqual(resp.data['meta_error']['error_subcode'], subcode)
        self.assertTrue(resp.data['meta_error']['message'])

    def test_fb_code_10_subcode_2018278(self):
        resp, _ = self.fb_send(timedelta(hours=2), post_side_effect=[_meta_error(10, 2018278)])
        self._assert_mapped(resp, 10, 2018278)

    def test_fb_subcode_2018108(self):
        resp, _ = self.fb_send(timedelta(hours=2), post_side_effect=[_meta_error(200, 2018108)])
        self._assert_mapped(resp, 200, 2018108)

    def test_fb_code_551(self):
        resp, _ = self.fb_send(timedelta(hours=2), post_side_effect=[_meta_error(551, 1545041, 'unavailable')])
        self._assert_mapped(resp, 551, 1545041)

    def test_ig_subcode_2018109(self):
        resp, _ = self.ig_send(timedelta(hours=2), post_side_effect=[_meta_error(100, 2018109)])
        self._assert_mapped(resp, 100, 2018109)

    def test_other_meta_error_not_mapped(self):
        resp, _ = self.fb_send(timedelta(hours=2), post_side_effect=[_meta_error(190, None, 'token expired')])
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn('code', resp.data)
        self.assertEqual(resp.data['meta_error']['code'], 190)
        self.assertIn('facebook_error', resp.data)


class TestTextMediaTextPart(_SendBase):
    def test_failed_text_part_is_reported_and_not_saved_as_sent(self):
        resp, mock_post = self.fb_send(
            timedelta(hours=1), media=True,
            post_side_effect=[_meta_error(100, None, 'bad text'), _ok('<mid_media>')],
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(mock_post.call_count, 2)
        self.assertTrue(resp.data['text_part_failed'])
        self.assertEqual(resp.data['text_error']['message'], 'bad text')
        rows = FacebookMessage.objects.filter(page_connection=self.fb, is_from_page=True)
        self.assertEqual(rows.count(), 1)  # only the media row
        self.assertEqual(rows.get().message_id, '<mid_media>')
        self.assertEqual(rows.get().message_text, '')

    def test_successful_text_part_gets_own_row(self):
        resp, _ = self.fb_send(
            timedelta(hours=1), media=True,
            post_side_effect=[_ok('<mid_text>'), _ok('<mid_media>')],
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertNotIn('text_part_failed', resp.data)
        text_row = FacebookMessage.objects.get(message_id='<mid_text>')
        self.assertEqual(text_row.message_text, 'hello back')
        self.assertEqual(text_row.messaging_type, 'RESPONSE')

    def test_text_part_outside_window_stops_before_media(self):
        resp, mock_post = self.fb_send(
            timedelta(hours=1), media=True,
            post_side_effect=[_meta_error(10, 2018278), _ok('<mid_media>')],
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'outside_messaging_window')
        self.assertEqual(mock_post.call_count, 1)


class TestRatingRequestWindow(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self._ws = patch('social_integrations.consumers.send_new_message_notification')
        self._ws.start()
        self.fb = self.create_fb_connection()
        self.ig = self.create_ig_connection(fb_page=self.fb)

    def tearDown(self):
        self._ws.stop()
        super().tearDown()

    def test_fb_rating_skipped_after_24h(self):
        self.create_fb_message(page_connection=self.fb, sender_id='psid_rt',
                               timestamp=timezone.now() - timedelta(hours=25))
        with patch('social_integrations.views.requests.post') as mock_post:
            result = social_views.send_rating_request_facebook('psid_rt', self.fb.page_id, 'rate us')
        self.assertIsNone(result)
        mock_post.assert_not_called()

    def test_fb_rating_sent_inside_24h_as_response_without_tag(self):
        self.create_fb_message(page_connection=self.fb, sender_id='psid_rt2',
                               timestamp=timezone.now() - timedelta(hours=1))
        with patch('social_integrations.views.requests.post', return_value=_ok('<rate_mid>')) as mock_post:
            result = social_views.send_rating_request_facebook('psid_rt2', self.fb.page_id, 'rate us')
        self.assertEqual(result, '<rate_mid>')
        payload = mock_post.call_args.kwargs['json']
        self.assertEqual(payload['messaging_type'], 'RESPONSE')
        self.assertNotIn('tag', payload)

    def test_ig_rating_skipped_after_24h(self):
        self.create_ig_message(account_connection=self.ig, sender_id='igsid_rt',
                               timestamp=timezone.now() - timedelta(days=2))
        with patch('social_integrations.views.requests.post') as mock_post:
            result = social_views.send_rating_request_instagram('igsid_rt', self.ig.instagram_account_id, 'rate')
        self.assertIsNone(result)
        mock_post.assert_not_called()

    def test_ig_rating_sent_inside_24h(self):
        self.create_ig_message(account_connection=self.ig, sender_id='igsid_rt2',
                               timestamp=timezone.now() - timedelta(hours=2))
        with patch('social_integrations.views.requests.post', return_value=_ok('<ig_rate>')) as mock_post:
            result = social_views.send_rating_request_instagram('igsid_rt2', self.ig.instagram_account_id, 'rate')
        self.assertEqual(result, '<ig_rate>')
        self.assertNotIn('tag', mock_post.call_args.kwargs['json'])


class TestWebhookSignatures(SocialIntegrationTestCase):
    body = json.dumps({'object': 'page', 'entry': []}).encode()

    def _post(self, url, signature=None):
        headers = {'HTTP_HOST': 'tenant.test.com'}
        if signature is not None:
            headers['HTTP_X_HUB_SIGNATURE_256'] = signature
        return self.client.generic('POST', url, self.body, content_type='application/json', **headers)

    def _sig(self, secret):
        return 'sha256=' + hmac.new(secret.encode(), self.body, hashlib.sha256).hexdigest()

    def test_fb_valid_signature_accepted(self):
        with override_settings(SOCIAL_INTEGRATIONS=_social_settings(FACEBOOK_APP_SECRET=APP_SECRET)):
            resp = self._post('/api/social/facebook/webhook/', self._sig(APP_SECRET))
        # Past the signature gate (the empty payload itself is a 400).
        self.assertNotEqual(resp.status_code, 403)

    def test_fb_bad_signature_rejected(self):
        with override_settings(SOCIAL_INTEGRATIONS=_social_settings(FACEBOOK_APP_SECRET=APP_SECRET)):
            resp = self._post('/api/social/facebook/webhook/', self._sig('wrong'))
        self.assertEqual(resp.status_code, 403)

    def test_fb_missing_signature_rejected(self):
        with override_settings(SOCIAL_INTEGRATIONS=_social_settings(FACEBOOK_APP_SECRET=APP_SECRET)):
            resp = self._post('/api/social/facebook/webhook/')
        self.assertEqual(resp.status_code, 403)

    def test_ig_signed_with_fb_secret_accepted(self):
        cfg = _social_settings(FACEBOOK_APP_SECRET=APP_SECRET, INSTAGRAM_APP_SECRET=IG_SECRET)
        with override_settings(SOCIAL_INTEGRATIONS=cfg):
            resp = self._post('/api/social/instagram/webhook/', self._sig(APP_SECRET))
        self.assertEqual(resp.status_code, 200)

    def test_ig_signed_with_ig_secret_accepted(self):
        cfg = _social_settings(FACEBOOK_APP_SECRET=APP_SECRET, INSTAGRAM_APP_SECRET=IG_SECRET)
        with override_settings(SOCIAL_INTEGRATIONS=cfg):
            resp = self._post('/api/social/instagram/webhook/', self._sig(IG_SECRET))
        self.assertEqual(resp.status_code, 200)

    def test_ig_bad_signature_rejected(self):
        with override_settings(SOCIAL_INTEGRATIONS=_social_settings(FACEBOOK_APP_SECRET=APP_SECRET)):
            resp = self._post('/api/social/instagram/webhook/', self._sig('nope'))
        self.assertEqual(resp.status_code, 403)

    def test_no_secret_configured_accepts(self):
        cfg = _social_settings(FACEBOOK_APP_SECRET='', INSTAGRAM_APP_SECRET='')
        with override_settings(SOCIAL_INTEGRATIONS=cfg, FACEBOOK_APP_SECRET='', INSTAGRAM_APP_SECRET=''):
            resp = self._post('/api/social/facebook/webhook/')
        self.assertNotEqual(resp.status_code, 403)

    def test_get_verification_unchanged(self):
        cfg = _social_settings(FACEBOOK_APP_SECRET=APP_SECRET, FACEBOOK_VERIFY_TOKEN='vt')
        with override_settings(SOCIAL_INTEGRATIONS=cfg):
            resp = self.client.get(
                '/api/social/facebook/webhook/?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=42',
                HTTP_HOST='tenant.test.com',
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b'42')


class TestSignedRequest(SocialIntegrationTestCase):
    def test_valid(self):
        sr = make_signed_request({'algorithm': 'HMAC-SHA256', 'user_id': '123', 'issued_at': 1})
        self.assertEqual(parse_signed_request(sr, [APP_SECRET])['user_id'], '123')

    def test_valid_with_second_secret(self):
        sr = make_signed_request({'algorithm': 'HMAC-SHA256', 'user_id': '9'}, secret=IG_SECRET)
        self.assertEqual(parse_signed_request(sr, [APP_SECRET, IG_SECRET])['user_id'], '9')

    def test_wrong_secret(self):
        sr = make_signed_request({'algorithm': 'HMAC-SHA256', 'user_id': '123'}, secret='other')
        with self.assertRaises(SignedRequestError):
            parse_signed_request(sr, [APP_SECRET])

    def test_tampered_payload(self):
        sr = make_signed_request({'algorithm': 'HMAC-SHA256', 'user_id': '123'})
        sig, _ = sr.split('.', 1)
        forged = base64.urlsafe_b64encode(json.dumps(
            {'algorithm': 'HMAC-SHA256', 'user_id': '999'}).encode()).decode().rstrip('=')
        with self.assertRaises(SignedRequestError):
            parse_signed_request(f'{sig}.{forged}', [APP_SECRET])

    def test_wrong_algorithm(self):
        sr = make_signed_request({'algorithm': 'none', 'user_id': '123'})
        with self.assertRaises(SignedRequestError):
            parse_signed_request(sr, [APP_SECRET])

    def test_malformed(self):
        for bad in ('', 'nodot', '!!!.@@@'):
            with self.assertRaises(SignedRequestError):
                parse_signed_request(bad, [APP_SECRET])

    def test_no_secret(self):
        with self.assertRaises(SignedRequestUnverifiable):
            parse_signed_request('a.b', [])


@override_settings(API_DOMAIN='api.echodesk.ge')
class TestDataDeletionAndDeauthorize(SocialIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self._cfg = override_settings(SOCIAL_INTEGRATIONS=_social_settings(FACEBOOK_APP_SECRET=APP_SECRET))
        self._cfg.enable()
        self.mine = self.create_fb_connection(connected_by_fb_user_id='asid_1')
        self.my_ig = self.create_ig_connection(fb_page=self.mine)
        self.other = self.create_fb_connection(connected_by_fb_user_id='asid_2')

    def tearDown(self):
        self._cfg.disable()
        super().tearDown()

    def _post(self, url, signed_request):
        return self.client.post(url, {'signed_request': signed_request}, HTTP_HOST='tenant.test.com')

    def test_deletion_valid_request(self):
        from tenants.models import FacebookDataDeletionRequest
        sr = make_signed_request({'algorithm': 'HMAC-SHA256', 'user_id': 'asid_1'})
        resp = self._post('/api/social/facebook/data-deletion/', sr)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        code = body['confirmation_code']
        self.assertEqual(
            body['url'], f'https://api.echodesk.ge/api/social/facebook/data-deletion/status/{code}/',
        )
        record = FacebookDataDeletionRequest.objects.get(confirmation_code=code)
        self.assertEqual(record.user_id, 'asid_1')
        self.assertEqual(record.status, 'completed')
        self.assertIsNotNone(record.completed_at)

        self.mine.refresh_from_db(); self.my_ig.refresh_from_db(); self.other.refresh_from_db()
        self.assertFalse(self.mine.is_active)
        self.assertEqual(self.mine.page_access_token, '')
        self.assertEqual(self.mine.connected_by_fb_user_id, '')
        self.assertFalse(self.my_ig.is_active)
        self.assertEqual(self.my_ig.access_token, '')
        self.assertTrue(self.other.is_active)
        self.assertTrue(self.other.page_access_token)

        status_resp = self.client.get(
            f'/api/social/facebook/data-deletion/status/{code}/?format=json', HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(status_resp.json()['status'], 'completed')
        html = self.client.get(
            f'/api/social/facebook/data-deletion/status/{code}/', HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(html.status_code, 200)
        self.assertIn(code.encode(), html.content)

    def test_deletion_invalid_signature(self):
        from tenants.models import FacebookDataDeletionRequest
        sr = make_signed_request({'algorithm': 'HMAC-SHA256', 'user_id': 'asid_1'}, secret='forged')
        resp = self._post('/api/social/facebook/data-deletion/', sr)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(FacebookDataDeletionRequest.objects.exists())
        self.mine.refresh_from_db()
        self.assertTrue(self.mine.is_active)

    def test_deletion_missing_signed_request(self):
        resp = self.client.post('/api/social/facebook/data-deletion/', {}, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, 400)

    def test_unknown_status_code(self):
        resp = self.client.get(
            '/api/social/facebook/data-deletion/status/nope/?format=json', HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, 404)

    def test_deauthorize_valid(self):
        sr = make_signed_request({'algorithm': 'HMAC-SHA256', 'user_id': 'asid_1'})
        resp = self._post('/api/social/facebook/deauthorize/', sr)
        self.assertEqual(resp.status_code, 200)
        self.mine.refresh_from_db(); self.other.refresh_from_db(); self.my_ig.refresh_from_db()
        self.assertFalse(self.mine.is_active)
        self.assertEqual(self.mine.page_access_token, '')
        self.assertEqual(self.mine.deactivation_reason, 'permission_revoked')
        self.assertFalse(self.my_ig.is_active)
        self.assertTrue(self.other.is_active)

    def test_deauthorize_invalid_signature(self):
        sr = make_signed_request({'algorithm': 'HMAC-SHA256', 'user_id': 'asid_1'}, secret='forged')
        resp = self._post('/api/social/facebook/deauthorize/', sr)
        self.assertEqual(resp.status_code, 400)
        self.mine.refresh_from_db()
        self.assertTrue(self.mine.is_active)


class TestDisconnectDeletesTokens(SocialIntegrationTestCase):
    """The privacy policy promises tokens are deleted on disconnect."""

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='tok-wipe-admin@test.com')

    def test_facebook_disconnect_all_wipes_page_tokens_keeps_messages(self):
        conn = self.create_fb_connection(page_id='wipe_all_page', page_access_token='secret-page-token')
        self.create_fb_message(page_connection=conn)
        resp = self.api_post('/api/social/facebook/disconnect/', {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        conn.refresh_from_db()
        self.assertFalse(conn.is_active)
        self.assertEqual(conn.page_access_token, '')
        self.assertEqual(conn.deactivation_reason, 'manual')
        self.assertEqual(FacebookMessage.objects.filter(page_connection=conn).count(), 1)

    def test_facebook_page_disconnect_wipes_that_page_token_only(self):
        target = self.create_fb_connection(page_id='wipe_one_page', page_access_token='tok-a')
        other = self.create_fb_connection(page_id='keep_page', page_access_token='tok-b')
        resp = self.api_post(f'/api/social/facebook/pages/{target.page_id}/disconnect/', {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        target.refresh_from_db()
        other.refresh_from_db()
        self.assertFalse(target.is_active)
        self.assertEqual(target.page_access_token, '')
        self.assertTrue(other.is_active)
        self.assertEqual(other.page_access_token, 'tok-b')

    def test_instagram_disconnect_wipes_tokens_keeps_messages(self):
        conn = self.create_ig_connection(username='wipe_ig', access_token='secret-ig-token')
        self.create_ig_message(account_connection=conn)
        resp = self.api_post('/api/social/instagram/disconnect/', {}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        conn.refresh_from_db()
        self.assertFalse(conn.is_active)
        self.assertEqual(conn.access_token, '')
        self.assertEqual(InstagramMessage.objects.filter(account_connection=conn).count(), 1)
