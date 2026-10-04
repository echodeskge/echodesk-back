"""
Shop customer tokens are bound to the tenant that issued them.
"""
from unittest.mock import patch

from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from users.tests.conftest import EchoDeskTenantTestCase
from ecommerce_crm import tokens as shop_tokens
from ecommerce_crm.models import EcommerceClient

PROFILE_URL = '/api/ecommerce/client/orders/'
REFRESH_URL = '/api/ecommerce/clients/refresh-token/'


class TestShopTokenBinding(EchoDeskTenantTestCase):

    def setUp(self):
        super().setUp()
        self.shopper = EcommerceClient.objects.create(
            email='shopper@test.com', first_name='S', last_name='H',
            phone_number='+995555000111', password='', is_verified=True,
        )

    def token(self, **claims):
        refresh = RefreshToken()
        refresh['client_id'] = self.shopper.id
        for key, value in claims.items():
            refresh[key] = value
        return refresh

    def get_profile(self, refresh):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        return api.get(PROFILE_URL, HTTP_HOST='tenant.test.com')

    def test_token_issued_here_is_accepted(self):
        refresh = shop_tokens.issue_shop_tokens(self.shopper)
        self.assertEqual(refresh['tenant'], self.tenant.schema_name)
        self.assertEqual(self.get_profile(refresh).status_code, status.HTTP_200_OK)

    def test_token_from_another_shop_or_a_booking_site_is_rejected(self):
        for claims in ({'kind': 'shop', 'tenant': 'othershop'},
                       {'kind': 'booking', 'tenant': self.tenant.schema_name}):
            refresh = self.token(**claims)
            self.assertEqual(self.get_profile(refresh).status_code, status.HTTP_401_UNAUTHORIZED, claims)
            refreshed = self.api_post(REFRESH_URL, {'refresh': str(refresh)})
            self.assertEqual(refreshed.status_code, status.HTTP_401_UNAUTHORIZED, claims)

    def test_unclaimed_tokens_only_work_if_issued_before_the_cutoff(self):
        legacy = self.token()
        with patch.object(shop_tokens, 'LEGACY_TOKEN_CUTOFF', legacy['iat'] + 60):
            self.assertEqual(self.get_profile(legacy).status_code, status.HTTP_200_OK)
            # refreshing a legacy token upgrades it to a claimed one
            refreshed = self.api_post(REFRESH_URL, {'refresh': str(legacy)})
            self.assertEqual(refreshed.status_code, status.HTTP_200_OK, refreshed.data)
            self.assertEqual(RefreshToken(refreshed.data['refresh'])['tenant'], self.tenant.schema_name)
        with patch.object(shop_tokens, 'LEGACY_TOKEN_CUTOFF', legacy['iat'] - 60):
            self.assertEqual(self.get_profile(legacy).status_code, status.HTTP_401_UNAUTHORIZED)
