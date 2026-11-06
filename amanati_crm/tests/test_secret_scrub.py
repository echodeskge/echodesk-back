"""Credentials never reach logs, Sentry or API error bodies (ECHODESK-BACKEND-84)."""
import json
import logging
from unittest import TestCase
from unittest.mock import patch

import requests
from django.http import JsonResponse
from django.test import RequestFactory

from amanati_crm.secret_scrub import (
    ScrubErrorResponseMiddleware, install, scrub, sentry_before_send,
)

TOKEN = 'EAAJOZAfPAPmYBSySmaq4LoZAAGiSqRDqTtWQUvpxmIoRiZA4ngXZBW7I1b2Tu1ldqPg'
ERR = (
    "HTTPSConnectionPool(host='graph.facebook.com', port=443): Max retries exceeded "
    f"with url: /v23.0/me/messages?access_token={TOKEN} (Caused by NewConnectionError(...))"
)


class ScrubTextTest(TestCase):
    def test_access_token_query_param(self):
        out = scrub(ERR)
        self.assertNotIn(TOKEN, out)
        self.assertIn('access_token=[redacted]', out)
        self.assertIn('graph.facebook.com', out)

    def test_other_patterns(self):
        self.assertNotIn('s3cr3t', scrub('a?client_secret=s3cr3t&x=1'))
        self.assertEqual(scrub('x=1&client_secret=s3cr3t&y=2'), 'x=1&client_secret=[redacted]&y=2')
        self.assertNotIn('abcdefghijklmnopqrstuvwx', scrub('Authorization: Bearer abcdefghijklmnopqrstuvwx'))
        self.assertNotIn(TOKEN, scrub(f'token was {TOKEN}.'))

    def test_plain_text_untouched(self):
        for text in ('hello', 'a = b', '', None, 42):
            self.assertEqual(scrub(text), text)


class SentryHookTest(TestCase):
    def test_exception_and_message_scrubbed(self):
        event = {
            'message': ERR,
            'logentry': {'message': 'Exception in facebook_send_message: %s', 'params': [ERR]},
            'exception': {'values': [{'type': 'ConnectionError', 'value': ERR}]},
            'breadcrumbs': {'values': [{'message': ERR}]},
        }
        out = json.dumps(sentry_before_send(event))
        self.assertNotIn(TOKEN, out)


class LoggingTest(TestCase):
    def test_log_records_are_scrubbed(self):
        install()
        logger = logging.getLogger('secret-scrub-test')
        with self.assertLogs(logger, level='ERROR') as cm:
            logger.error('Exception in facebook_send_message: %s', ERR)
            logger.error(f'f-string too: {ERR}')
        joined = '\n'.join(cm.output)
        self.assertNotIn(TOKEN, joined)
        self.assertIn('access_token=[redacted]', joined)


class MiddlewareTest(TestCase):
    def _run(self, response):
        mw = ScrubErrorResponseMiddleware(lambda request: response)
        return mw(RequestFactory().get('/'))

    def test_error_json_body_scrubbed(self):
        resp = self._run(JsonResponse({'error': f'Failed to send message: {ERR}'}, status=500))
        self.assertNotIn(TOKEN, resp.content.decode())
        self.assertIn('Failed to send message', json.loads(resp.content)['error'])

    def test_success_body_untouched(self):
        body = {'note': f'access_token={TOKEN}'}
        resp = self._run(JsonResponse(body, status=200))
        self.assertEqual(json.loads(resp.content), body)


class ConnectRetryTest(TestCase):
    def _conn_err(self):
        return requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='graph.facebook.com'): Max retries exceeded "
            "(Caused by NewConnectionError('Failed to establish a new connection: [Errno 101]'))"
        )

    @patch('social_integrations.http_retry.time.sleep')
    def test_retries_connect_failure_then_succeeds(self, _sleep):
        from social_integrations import http_retry
        ok = object()
        with patch('requests.post', side_effect=[self._conn_err(), ok]) as post:
            self.assertIs(http_retry.post('https://graph.facebook.com/x', json={}), ok)
        self.assertEqual(post.call_count, 2)

    @patch('social_integrations.http_retry.time.sleep')
    def test_gives_up_after_retries(self, _sleep):
        from social_integrations import http_retry
        with patch('requests.post', side_effect=self._conn_err()) as post:
            with self.assertRaises(requests.exceptions.ConnectionError):
                http_retry.post('https://graph.facebook.com/x')
        self.assertEqual(post.call_count, 1 + http_retry.CONNECT_RETRIES)

    @patch('social_integrations.http_retry.time.sleep')
    def test_read_timeout_not_retried(self, _sleep):
        """A read timeout may mean Meta already delivered the message."""
        from social_integrations import http_retry
        with patch('requests.post', side_effect=requests.exceptions.ReadTimeout('slow')) as post:
            with self.assertRaises(requests.exceptions.ReadTimeout):
                http_retry.post('https://graph.facebook.com/x')
        self.assertEqual(post.call_count, 1)
