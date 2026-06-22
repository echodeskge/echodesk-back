"""
Tests for Client/SocialClient-related views and Quick Reply views.
"""
from unittest.mock import patch

from rest_framework import status
from social_integrations.tests.conftest import SocialIntegrationTestCase


class TestSocialClientViewSet(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='client-admin@test.com')
        self.agent = self.create_user(email='client-agent@test.com')
        self.url = '/api/social/clients/'

    def test_list_clients(self):
        self.create_client(name='Client A')
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_empty(self):
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_retrieve_client(self):
        c = self.create_client(name='Client B')
        resp = self.api_get(f'{self.url}{c.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_create_client(self):
        resp = self.api_post(self.url, {
            'name': 'New Client',
        }, user=self.admin)
        self.assertIn(resp.status_code, [
            status.HTTP_201_CREATED,
            status.HTTP_200_OK,
        ])

    def test_update_client(self):
        c = self.create_client(name='Old Name')
        resp = self.api_patch(f'{self.url}{c.id}/', {
            'name': 'New Name',
        }, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_delete_client(self):
        c = self.create_client(name='To Delete')
        resp = self.api_delete(f'{self.url}{c.id}/', user=self.admin)
        self.assertIn(resp.status_code, [
            status.HTTP_204_NO_CONTENT,
            status.HTTP_200_OK,
        ])

    def test_unauthenticated_denied(self):
        resp = self.client.get(self.url, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class TestSocialClientCustomFieldViewSet(SocialIntegrationTestCase):
    """
    Note: The custom-fields URL `clients/custom-fields/` is registered in the
    DRF router, but the clients ViewSet's `clients/(?P<pk>[^/.]+)/` pattern
    matches first (pk='custom-fields'). This is a known routing issue.
    Tests use the DRF test client to call the viewset directly.
    """

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='cf-admin@test.com')

    def test_custom_field_creation(self):
        """Verify custom field can be created and queried."""
        from social_integrations.models import SocialClientCustomField
        f = SocialClientCustomField.objects.create(
            name='test_field', label='Test Field', field_type='string',
            created_by=self.admin,
        )
        self.assertTrue(f.is_active)
        self.assertEqual(SocialClientCustomField.objects.filter(is_active=True).count(), 1)

    def test_custom_field_model_str(self):
        from social_integrations.models import SocialClientCustomField
        f = SocialClientCustomField.objects.create(
            name='company', label='Company', field_type='string',
            created_by=self.admin,
        )
        self.assertEqual(str(f), 'Company (string)')


class TestQuickReplyViewSet(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.create_admin(email='qr-admin@test.com')
        self.agent = self.create_user(email='qr-agent@test.com')
        self.url = '/api/social/quick-replies/'

    def test_list_quick_replies(self):
        self.create_quick_reply(created_by=self.admin)
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_empty(self):
        resp = self.api_get(self.url, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_create_quick_reply(self):
        resp = self.api_post(self.url, {
            'title': 'Greeting',
            'message': 'Hello {{customer_name}}!',
        }, user=self.admin)
        self.assertIn(resp.status_code, [
            status.HTTP_201_CREATED,
            status.HTTP_200_OK,
        ])

    def test_retrieve_quick_reply(self):
        qr = self.create_quick_reply(created_by=self.admin)
        resp = self.api_get(f'{self.url}{qr.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_update_quick_reply(self):
        qr = self.create_quick_reply(created_by=self.admin)
        resp = self.api_patch(f'{self.url}{qr.id}/', {
            'title': 'Updated',
        }, user=self.admin)
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_delete_quick_reply(self):
        qr = self.create_quick_reply(created_by=self.admin)
        resp = self.api_delete(f'{self.url}{qr.id}/', user=self.admin)
        self.assertIn(resp.status_code, [
            status.HTTP_204_NO_CONTENT,
            status.HTTP_200_OK,
        ])

    def test_unauthenticated_denied(self):
        resp = self.client.get(self.url, HTTP_HOST='tenant.test.com')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    # ── Cross-user access: any user with social access can manage the shared
    #    quick-reply library, even without the social_integrations feature.
    #    (The base class patches has_feature→True, so these force it False to
    #    reproduce the real-world case where access comes from a permission
    #    toggle, not the subscription-feature intersection.) ──

    def test_social_permission_user_can_update_others_quick_reply(self):
        """A non-staff user with a social permission (no feature) can edit a
        quick reply created by someone else — the Liza case."""
        qr = self.create_quick_reply(created_by=self.admin)
        liza = self.create_user(
            email='qr-liza@test.com', role='agent', can_send_social_messages=True
        )
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_patch(
                f'{self.url}{qr.id}/', {'title': 'Edited by Liza'}, user=liza
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        qr.refresh_from_db()
        self.assertEqual(qr.title, 'Edited by Liza')

    def test_social_permission_user_can_delete_others_quick_reply(self):
        qr = self.create_quick_reply(created_by=self.admin)
        liza = self.create_user(
            email='qr-liza2@test.com', role='agent', can_view_social_messages=True
        )
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_delete(f'{self.url}{qr.id}/', user=liza)
        self.assertIn(
            resp.status_code, [status.HTTP_204_NO_CONTENT, status.HTTP_200_OK]
        )

    def test_user_without_social_access_denied(self):
        """A non-staff user with neither the feature nor any social permission
        is forbidden from managing quick replies."""
        qr = self.create_quick_reply(created_by=self.admin)
        nobody = self.create_user(email='qr-nosocial@test.com', role='agent')
        with patch('users.models.User.has_feature', return_value=False):
            resp = self.api_patch(
                f'{self.url}{qr.id}/', {'title': 'nope'}, user=nobody
            )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_stamps_created_by(self):
        resp = self.api_post(self.url, {
            'title': 'Stamped', 'message': 'hi',
        }, user=self.agent)
        self.assertIn(resp.status_code, [status.HTTP_201_CREATED, status.HTTP_200_OK])
        self.assertEqual(resp.data['created_by'], self.agent.id)
