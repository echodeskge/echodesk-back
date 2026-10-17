"""Tests for TagViewSet."""
from tickets.models import Tag
from tickets.tests.conftest import TicketTestCase


class TestTagCRUD(TicketTestCase):

    def test_create_tag(self):
        user = self.create_manager(email='tagger@test.com')  # manage_tags by role
        resp = self.api_post('/api/tags/', {'name': 'Bug', 'color': '#FF0000'}, user=user)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['name'], 'Bug')

    def test_list_tags(self):
        user = self.create_user(email='tagger@test.com')
        self.create_tag(name='Bug')
        self.create_tag(name='Feature')
        resp = self.api_get('/api/tags/', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 2)

    def test_update_tag(self):
        user = self.create_manager(email='tagger@test.com')  # manage_tags by role
        tag = self.create_tag(name='Bugg')
        resp = self.api_patch(f'/api/tags/{tag.id}/', {'name': 'Bug'}, user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['name'], 'Bug')

    def test_delete_tag(self):
        user = self.create_manager(email='tagger@test.com')  # manage_tags by role
        tag = self.create_tag(name='ToDelete')
        resp = self.api_delete(f'/api/tags/{tag.id}/', user=user)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Tag.objects.filter(id=tag.id).exists())

    def test_search_tags(self):
        user = self.create_user(email='tagger@test.com')
        self.create_tag(name='Bug')
        self.create_tag(name='Feature')
        resp = self.api_get('/api/tags/?search=bug', user=user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.get_results(resp)), 1)
        self.assertEqual(self.get_results(resp)[0]['name'], 'Bug')

    def test_unauthenticated_denied(self):
        resp = self.api_get('/api/tags/')
        self.assertIn(resp.status_code, [401, 403])
