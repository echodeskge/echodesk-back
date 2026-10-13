"""Forgot-password flow for dashboard users."""
from unittest.mock import patch

from django.core.cache import cache
from rest_framework import status

from users.tests.conftest import EchoDeskTenantTestCase

REQUEST_URL = '/api/users/password-reset/'
CONFIRM_URL = '/api/users/password-reset/confirm/'


class PasswordResetTests(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.user = self.create_admin(email='Reset.Me@test.com')
        self.user.set_password('OldPassw0rd!')
        self.user.save()

    def _request(self, email='reset.me@test.com'):
        with patch('tenants.email_service.email_service._send_email', return_value=True) as send:
            resp = self.api_post(REQUEST_URL, {'email': email})
        return resp, send

    def _code(self, send):
        return send.call_args[0][2].split('letter-spacing:4px">')[1][:6]

    def test_full_flow(self):
        resp, send = self._request()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(send.call_args[0][0], 'Reset.Me@test.com')
        code = self._code(send)
        resp = self.api_post(CONFIRM_URL, {'email': 'RESET.me@test.com', 'code': code, 'new_password': 'N3w-Strong-Pass'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('N3w-Strong-Pass'))
        # The code is single-use
        resp = self.api_post(CONFIRM_URL, {'email': 'reset.me@test.com', 'code': code, 'new_password': 'An0ther-Pass!'})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_email_looks_the_same(self):
        resp, send = self._request('nobody@test.com')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        send.assert_not_called()

    def test_wrong_code_and_attempt_limit(self):
        _, send = self._request()
        code = self._code(send)
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(5):
            resp = self.api_post(CONFIRM_URL, {'email': 'reset.me@test.com', 'code': wrong, 'new_password': 'N3w-Strong-Pass'})
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        # Five misses void the code, even the right one
        resp = self.api_post(CONFIRM_URL, {'email': 'reset.me@test.com', 'code': code, 'new_password': 'N3w-Strong-Pass'})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('OldPassw0rd!'))

    def test_weak_password_rejected_and_code_kept(self):
        _, send = self._request()
        code = self._code(send)
        resp = self.api_post(CONFIRM_URL, {'email': 'reset.me@test.com', 'code': code, 'new_password': '12345678'})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('new_password', resp.data)
        resp = self.api_post(CONFIRM_URL, {'email': 'reset.me@test.com', 'code': code, 'new_password': 'N3w-Strong-Pass'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
