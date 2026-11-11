"""Sentry ECHODESK-BACKEND-84: a send whose connection to Meta never opened.

``http_retry.post`` retries the connect; when the host's outbound network is
down for longer than that (``[Errno 101] Network is unreachable`` at 10:48 UTC
on 2026-11-06) the send views used to log an *error* with the full Graph URL
and answer 500. Nothing reached Meta, so this is a 503 the agent can retry,
logged at warning. Any other exception keeps the error log + 500.
"""
import logging
from unittest.mock import patch

import requests
from rest_framework import status

from social_integrations import http_retry
from social_integrations.tests.conftest import SocialIntegrationTestCase


def _unreachable():
    return requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='graph.facebook.com', port=443): Max retries "
        "exceeded with url: /v23.0/me/messages?access_token=EAAJsecret (Caused by "
        "NewConnectionError(\"HTTPSConnection(host='graph.facebook.com', port=443): "
        "Failed to establish a new connection: [Errno 101] Network is unreachable\"))"
    )


class _SendViewMixin:
    url = None
    service = None
    view_name = None

    def _payload(self):
        raise NotImplementedError

    def test_connect_failure_is_a_503_logged_at_warning(self):
        with patch('social_integrations.views.http_retry.post', side_effect=_unreachable()):
            with self.assertLogs('social_integrations.views', level='WARNING') as cm:
                resp = self.api_post(self.url, self._payload(), user=self.agent)

        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(resp.data['error_code'], 'upstream_unreachable')
        self.assertIn(self.service, resp.data['error'])
        self.assertEqual([r.levelno for r in cm.records], [logging.WARNING])
        self.assertIn(self.view_name, cm.records[0].getMessage())
        # No Graph URL (it carries the page token) in the log line.
        self.assertNotIn('access_token', cm.records[0].getMessage())
        self.assertNotIn('EAAJsecret', str(resp.data))

    def test_other_exception_still_an_error_and_500(self):
        with patch('social_integrations.views.http_retry.post',
                   side_effect=requests.exceptions.ReadTimeout('slow')):
            with self.assertLogs('social_integrations.views', level='ERROR') as cm:
                resp = self.api_post(self.url, self._payload(), user=self.agent)

        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(len(cm.records), 1)
        self.assertIn(self.view_name.split('_')[0], cm.records[0].getMessage().lower())


class TestFacebookSendUnreachable(_SendViewMixin, SocialIntegrationTestCase):
    url = '/api/social/facebook/send-message/'
    service = 'Facebook'
    view_name = 'facebook_send_message'

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='fb-unreach@test.com')
        self.conn = self.create_fb_connection()
        self.create_fb_message(page_connection=self.conn, sender_id='fb_customer', is_from_page=False)

    def _payload(self):
        return {'page_id': self.conn.page_id, 'recipient_id': 'fb_customer', 'message': 'Hello'}


class TestInstagramSendUnreachable(_SendViewMixin, SocialIntegrationTestCase):
    url = '/api/social/instagram/send-message/'
    service = 'Instagram'
    view_name = 'instagram_send_message'

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='ig-unreach@test.com')
        page = self.create_fb_connection()
        self.conn = self.create_ig_connection(fb_page=page)
        self.create_ig_message(account_connection=self.conn, sender_id='ig_customer', is_from_business=False)

    def _payload(self):
        return {
            'instagram_account_id': self.conn.instagram_account_id,
            'recipient_id': 'ig_customer',
            'message': 'Hello',
        }


class TestWhatsAppSendUnreachable(_SendViewMixin, SocialIntegrationTestCase):
    url = '/api/social/whatsapp/send-message/'
    service = 'WhatsApp'
    view_name = 'whatsapp_send_message'

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='wa-unreach@test.com')
        self.account = self.create_wa_account()

    def _payload(self):
        return {'waba_id': self.account.waba_id, 'to_number': '+15550001111', 'message': 'Hello'}


class TestIsConnectFailure(SocialIntegrationTestCase):
    def test_classification(self):
        self.assertTrue(http_retry.is_connect_failure(_unreachable()))
        self.assertTrue(http_retry.is_connect_failure(requests.exceptions.ConnectTimeout('t')))
        self.assertFalse(http_retry.is_connect_failure(requests.exceptions.ReadTimeout('t')))
        self.assertFalse(http_retry.is_connect_failure(requests.exceptions.ConnectionError('reset by peer')))
        self.assertFalse(http_retry.is_connect_failure(ValueError('x')))
