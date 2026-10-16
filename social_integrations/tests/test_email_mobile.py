"""
Tests for the mobile-oriented email additions (Phase 3 package F3):

- ``?lite=1`` / ``/lite/`` list rows (snippet + attachments_count, no bodies)
- ``body_html_safe`` on the detail endpoint and the allow-list sanitiser
- EmailConnectionUserAssignment enforcement (connection-centric, same rule as
  /email/status/) on messages / threads / action / move / drafts / send / folders
- ``move`` and draft creation use the message's own connection
- send body contract (JSON and multipart) and the OpenAPI declarations
"""
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import path, include
from django.utils import timezone
from rest_framework import status

from social_integrations.html_sanitizer import sanitize_email_html
from social_integrations.models import EmailDraft, EmailMessage, EmailConnectionUserAssignment
from social_integrations.tests.conftest import SocialIntegrationTestCase


LIST_URL = '/api/social/email-messages/'
ACTION_URL = '/api/social/email/action/'


class TestHtmlSanitizer(SocialIntegrationTestCase):
    """Pure-function tests; the tenant base class is only used for consistency."""

    def test_strips_scripts_styles_and_handlers(self):
        html = (
            '<html><head><style>p{color:red}</style><title>t</title></head>'
            '<body><p style="color:red" onclick="evil()" class="x">Hi <b>there</b>'
            '<script>alert(1)</script></p></body></html>'
        )
        out = sanitize_email_html(html)
        self.assertEqual(out, '<p>Hi <b>there</b></p>')

    def test_links_only_http_https_mailto(self):
        out = sanitize_email_html(
            '<a href="javascript:alert(1)">bad</a>'
            '<a href="https://x.com/?a=1&b=2" title="t">ok</a>'
            '<a href="mailto:a@b.c">mail</a>'
        )
        self.assertIn('<a>bad</a>', out)
        self.assertIn('href="https://x.com/?a=1&amp;b=2"', out)
        self.assertIn('rel="noopener noreferrer"', out)
        self.assertIn('href="mailto:a@b.c"', out)
        self.assertNotIn('javascript:', out)

    def test_images_only_http_https(self):
        out = sanitize_email_html(
            '<img src="data:image/png;base64,AAAA" onerror="x()">'
            '<img src="http://cdn/x.png" alt="pic" width="10">'
        )
        self.assertEqual(out, '<img /><img src="http://cdn/x.png" alt="pic" width="10" />')

    def test_unknown_tags_dropped_but_text_kept(self):
        out = sanitize_email_html('<font color="red"><center>plain</center></font><iframe src="x">gone</iframe>')
        self.assertEqual(out, 'plain')

    def test_tables_and_structure_preserved(self):
        out = sanitize_email_html('<table><tr><td colspan="2">c</td><th>h</th></tr></table><ul><li>1</li></ul>')
        self.assertEqual(out, '<table><tr><td colspan="2">c</td><th>h</th></tr></table><ul><li>1</li></ul>')

    def test_unbalanced_input_closed_and_text_escaped(self):
        out = sanitize_email_html('<div><span>a < b & c')
        self.assertEqual(out, '<div><span>a &lt; b &amp; c</span></div>')

    def test_comments_and_doctype_dropped(self):
        out = sanitize_email_html('<!DOCTYPE html><!-- hidden --><p>x</p>')
        self.assertEqual(out, '<p>x</p>')

    def test_empty(self):
        self.assertEqual(sanitize_email_html(''), '')
        self.assertEqual(sanitize_email_html(None), '')


class TestEmailMessageLite(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='lite-agent@test.com')
        self.conn = self.create_email_connection()
        self.msg = self.create_email_message(
            connection=self.conn,
            body_text='  Hello\n\n   world,  ' + ('x' * 300),
            body_html='<p>Hello</p>',
            attachments=[
                {'filename': 'a.pdf', 'content_type': 'application/pdf', 'url': 'http://x/a.pdf', 'size': 1},
                {'filename': 'b.png', 'content_type': 'image/png', 'url': 'http://x/b.png', 'size': 2},
            ],
        )

    def test_default_list_unchanged(self):
        resp = self.api_get(LIST_URL, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        row = resp.json()['results'][0]
        self.assertIn('body_text', row)
        self.assertIn('body_html', row)
        self.assertIn('attachments', row)
        self.assertNotIn('snippet', row)
        self.assertNotIn('body_html_safe', row)

    def test_lite_rows(self):
        resp = self.api_get(f'{LIST_URL}?lite=1', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertIn('count', data)  # still paginated
        row = data['results'][0]
        self.assertNotIn('body_text', row)
        self.assertNotIn('body_html', row)
        self.assertNotIn('attachments', row)
        self.assertEqual(row['attachments_count'], 2)
        self.assertEqual(len(row['snippet']), 160)
        self.assertTrue(row['snippet'].startswith('Hello world, xxx'))
        # Everything a list screen needs is still there
        for key in ('id', 'thread_id', 'from_email', 'from_name', 'to_emails', 'subject',
                    'timestamp', 'folder', 'is_read', 'is_starred', 'labels',
                    'connection_id', 'has_business_reply'):
            self.assertIn(key, row)

    def test_lite_true_and_zero(self):
        self.assertIn('snippet', self.api_get(f'{LIST_URL}?lite=true', user=self.agent).json()['results'][0])
        self.assertIn('body_text', self.api_get(f'{LIST_URL}?lite=0', user=self.agent).json()['results'][0])

    def test_lite_route_matches_lite_param(self):
        via_param = self.api_get(f'{LIST_URL}?lite=1&folder=INBOX', user=self.agent).json()
        via_route = self.api_get(f'{LIST_URL}lite/?folder=INBOX', user=self.agent).json()
        self.assertEqual(via_param, via_route)
        self.assertEqual(via_route['results'][0]['attachments_count'], 2)

    def test_lite_snippet_no_attachments(self):
        msg = self.create_email_message(connection=self.conn, body_text='', attachments=[])
        resp = self.api_get(f'{LIST_URL}?lite=1&thread_id={msg.thread_id}', user=self.agent)
        row = resp.json()['results'][0]
        self.assertEqual(row['snippet'], '')
        self.assertEqual(row['attachments_count'], 0)

    def test_threads_lite(self):
        resp = self.api_get(f'{LIST_URL}threads/?lite=1', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = resp.json()
        self.assertIsInstance(rows, list)
        self.assertNotIn('body_text', rows[0])
        self.assertIn('snippet', rows[0])
        self.assertIn('message_count', rows[0])
        self.assertIn('customer_email', rows[0])

    def test_threads_default_still_full(self):
        rows = self.api_get(f'{LIST_URL}threads/', user=self.agent).json()
        self.assertIn('body_text', rows[0])


class TestEmailMessageDetailSafeHtml(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='detail-agent@test.com')
        self.conn = self.create_email_connection()

    def test_body_html_safe(self):
        raw = '<p onclick="x()">Hi</p><script>alert(1)</script><a href="javascript:void(0)">l</a>'
        msg = self.create_email_message(connection=self.conn, body_html=raw)
        resp = self.api_get(f'{LIST_URL}{msg.id}/', user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data['body_html'], raw)  # raw still returned
        self.assertEqual(data['body_html_safe'], '<p>Hi</p><a>l</a>')

    def test_body_html_safe_empty(self):
        msg = self.create_email_message(connection=self.conn, body_html='')
        data = self.api_get(f'{LIST_URL}{msg.id}/', user=self.agent).json()
        self.assertEqual(data['body_html_safe'], '')


class TestEmailAssignmentEnforcement(SocialIntegrationTestCase):
    """Mailbox visibility on the message endpoints.

    Connection-centric rule — the same one ``/email/status/``,
    ``/unread-count/`` and the unified conversations apply:

    - a connection with no assignments (``conn_shared``) is visible to everyone
    - a connection with assignments is visible only to its assignees
    - admins / staff / superusers / manage_social_connections → everything

    Fixture: ``conn_a`` assigned to ``assigned``, ``conn_b`` assigned to
    ``other``, ``conn_shared`` unassigned; ``unassigned`` has no assignment.
    """

    def setUp(self):
        super().setUp()
        self.conn_a = self.create_email_connection(email_address='a@example.com')
        self.conn_b = self.create_email_connection(email_address='b@example.com')
        self.conn_shared = self.create_email_connection(email_address='shared@example.com')
        self.msg_a = self.create_email_message(connection=self.conn_a, thread_id='thread-a', subject='A mail')
        self.msg_b = self.create_email_message(connection=self.conn_b, thread_id='thread-b', subject='B mail')
        self.msg_shared = self.create_email_message(
            connection=self.conn_shared, thread_id='thread-shared', subject='Shared mail',
        )

        self.assigned = self.create_user(email='assigned-a@test.com')
        EmailConnectionUserAssignment.objects.create(
            connection=self.conn_a, user=self.assigned, assigned_by=self.assigned,
        )
        self.other = self.create_user(email='assigned-b@test.com')
        EmailConnectionUserAssignment.objects.create(
            connection=self.conn_b, user=self.other, assigned_by=self.other,
        )
        self.unassigned = self.create_user(email='unassigned@test.com')
        self.admin = self.create_admin(email='email-admin@test.com')
        self.manager_perm = self.create_user(
            email='perm@test.com', can_manage_social_connections=True,
        )
        self.all_ids = {self.msg_a.id, self.msg_b.id, self.msg_shared.id}

    def _ids(self, resp):
        data = resp.json()
        rows = data['results'] if isinstance(data, dict) else data
        return {r['id'] for r in rows}

    def _status_connection_ids(self, user):
        return {c['id'] for c in self.api_get('/api/social/email/status/', user=user).json()['connections']}

    # -- list ---------------------------------------------------------------

    def test_assigned_user_sees_assigned_and_shared_connections(self):
        self.assertEqual(
            self._ids(self.api_get(LIST_URL, user=self.assigned)), {self.msg_a.id, self.msg_shared.id},
        )
        self.assertEqual(
            self._ids(self.api_get(LIST_URL, user=self.other)), {self.msg_b.id, self.msg_shared.id},
        )

    def test_unassigned_user_sees_only_shared_connections(self):
        self.assertEqual(self._ids(self.api_get(LIST_URL, user=self.unassigned)), {self.msg_shared.id})

    def test_unassigned_user_sees_everything_when_tenant_has_no_assignments(self):
        EmailConnectionUserAssignment.objects.all().delete()
        self.assertEqual(self._ids(self.api_get(LIST_URL, user=self.unassigned)), self.all_ids)

    def test_list_matches_status_connections(self):
        """The contract the mobile app relies on: messages only come from status.connections."""
        for user in (self.assigned, self.other, self.unassigned, self.admin):
            rows = self.api_get(LIST_URL, user=user).json()['results']
            self.assertEqual(
                {r['connection_id'] for r in rows}, self._status_connection_ids(user), user.email,
            )

    def test_connection_id_filter_cannot_bypass(self):
        resp = self.api_get(f'{LIST_URL}?connection_id={self.conn_b.id}', user=self.assigned)
        self.assertEqual(resp.json()['count'], 0)
        resp = self.api_get(f'{LIST_URL}?connection_id={self.conn_shared.id}', user=self.assigned)
        self.assertEqual(self._ids(resp), {self.msg_shared.id})
        resp = self.api_get(f'{LIST_URL}?connection_id={self.conn_a.id}', user=self.unassigned)
        self.assertEqual(resp.json()['count'], 0)

    def test_lite_list_respects_assignment(self):
        self.assertEqual(
            self._ids(self.api_get(f'{LIST_URL}?lite=1', user=self.unassigned)), {self.msg_shared.id},
        )

    # -- retrieve -----------------------------------------------------------

    def test_retrieve(self):
        self.assertEqual(self.api_get(f'{LIST_URL}{self.msg_a.id}/', user=self.assigned).status_code, 200)
        self.assertEqual(self.api_get(f'{LIST_URL}{self.msg_shared.id}/', user=self.assigned).status_code, 200)
        self.assertEqual(self.api_get(f'{LIST_URL}{self.msg_b.id}/', user=self.assigned).status_code, 404)
        self.assertEqual(self.api_get(f'{LIST_URL}{self.msg_shared.id}/', user=self.unassigned).status_code, 200)
        self.assertEqual(self.api_get(f'{LIST_URL}{self.msg_a.id}/', user=self.unassigned).status_code, 404)

    # -- threads ------------------------------------------------------------

    def test_threads_filtered(self):
        threads = self.api_get(f'{LIST_URL}threads/', user=self.assigned).json()
        self.assertEqual({t['thread_id'] for t in threads}, {'thread-a', 'thread-shared'})
        threads = self.api_get(f'{LIST_URL}threads/', user=self.unassigned).json()
        self.assertEqual({t['thread_id'] for t in threads}, {'thread-shared'})
        threads = self.api_get(f'{LIST_URL}threads/?lite=1', user=self.unassigned).json()
        self.assertEqual({t['thread_id'] for t in threads}, {'thread-shared'})

    def test_threads_shared_thread_id_across_connections(self):
        """A thread_id present on both mailboxes must only yield the accessible copy."""
        self.create_email_message(connection=self.conn_a, thread_id='shared', subject='A copy')
        other = self.create_email_message(
            connection=self.conn_b, thread_id='shared', subject='B copy',
            timestamp=timezone.now() + timezone.timedelta(hours=1),
        )
        threads = self.api_get(f'{LIST_URL}threads/', user=self.assigned).json()
        shared = next(t for t in threads if t['thread_id'] == 'shared')
        self.assertNotEqual(shared['id'], other.id)
        self.assertEqual(shared['connection_id'], self.conn_a.id)

    def test_access_predicate_is_cached_per_user(self):
        from social_integrations.views import _email_connection_access
        first = _email_connection_access(self.assigned)
        self.assertEqual(first, {self.conn_a.id, self.conn_shared.id})
        with self.assertNumQueries(0):
            self.assertEqual(_email_connection_access(self.assigned), first)

    # -- privileged users ---------------------------------------------------

    def test_admin_sees_everything(self):
        self.assertEqual(self._ids(self.api_get(LIST_URL, user=self.admin)), self.all_ids)

    def test_manage_social_connections_permission_sees_everything(self):
        self.assertEqual(self._ids(self.api_get(LIST_URL, user=self.manager_perm)), self.all_ids)

    def test_staff_flag_sees_everything(self):
        staff = self.create_user(email='staff@test.com', is_staff=True)
        EmailConnectionUserAssignment.objects.create(connection=self.conn_a, user=staff, assigned_by=staff)
        self.assertEqual(self._ids(self.api_get(LIST_URL, user=staff)), self.all_ids)

    # -- /email/action/ -----------------------------------------------------

    def test_action_inaccessible_message_404(self):
        resp = self.api_post(ACTION_URL, {'message_ids': [self.msg_b.id], 'action': 'star'}, user=self.assigned)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.msg_b.refresh_from_db()
        self.assertFalse(self.msg_b.is_starred)
        resp = self.api_post(ACTION_URL, {'message_ids': [self.msg_a.id], 'action': 'star'}, user=self.unassigned)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_action_shared_message_ok(self):
        for user in (self.assigned, self.unassigned):
            resp = self.api_post(
                ACTION_URL, {'message_ids': [self.msg_shared.id], 'action': 'mark_read'}, user=user,
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK, user.email)
            self.assertEqual(resp.json()['affected_count'], 1)

    def test_action_mixed_ids_only_affects_accessible(self):
        resp = self.api_post(
            ACTION_URL,
            {'message_ids': [self.msg_a.id, self.msg_b.id, self.msg_shared.id], 'action': 'mark_read'},
            user=self.assigned,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['affected_count'], 2)
        self.msg_a.refresh_from_db()
        self.msg_b.refresh_from_db()
        self.msg_shared.refresh_from_db()
        self.assertTrue(self.msg_a.is_read)
        self.assertFalse(self.msg_b.is_read)
        self.assertTrue(self.msg_shared.is_read)

    def test_action_by_thread_respects_assignment(self):
        resp = self.api_post(ACTION_URL, {'thread_id': 'thread-b', 'action': 'delete'}, user=self.assigned)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.msg_b.refresh_from_db()
        self.assertFalse(self.msg_b.is_deleted)

    def test_action_admin_unaffected(self):
        resp = self.api_post(ACTION_URL, {'message_ids': [self.msg_b.id], 'action': 'star'}, user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['affected_count'], 1)

    # -- move ---------------------------------------------------------------

    def _move(self, message, user):
        return self.api_post(
            f'{LIST_URL}move/',
            {'message_id': message.message_id, 'source_folder': 'INBOX', 'target_folder': 'Archive'},
            user=user,
        )

    @patch('social_integrations.email_utils.move_email_to_folder', return_value=True)
    def test_move_inaccessible_message_404(self, mocked_move):
        self.assertEqual(self._move(self.msg_b, self.assigned).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self._move(self.msg_a, self.unassigned).status_code, status.HTTP_404_NOT_FOUND)
        mocked_move.assert_not_called()

    @patch('social_integrations.email_utils.move_email_to_folder', return_value=True)
    def test_move_shared_and_assigned_ok(self, mocked_move):
        self.assertEqual(self._move(self.msg_shared, self.unassigned).status_code, status.HTTP_200_OK)
        self.assertEqual(self._move(self.msg_a, self.assigned).status_code, status.HTTP_200_OK)
        self.assertEqual(mocked_move.call_count, 2)

    # -- drafts -------------------------------------------------------------

    DRAFT_URL = '/api/social/email-drafts/'

    def _draft(self, user, **extra):
        body = {'to_emails': [], 'subject': 'New', 'body_text': 'hi'}
        body.update(extra)
        return self.api_post(self.DRAFT_URL, body, user=user)

    def test_draft_explicit_connection_access(self):
        self.assertEqual(self._draft(self.assigned, connection=self.conn_b.id).status_code, 400)
        self.assertEqual(self._draft(self.unassigned, connection=self.conn_a.id).status_code, 400)
        resp = self._draft(self.assigned, connection=self.conn_shared.id)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        resp = self._draft(self.unassigned, connection=self.conn_shared.id)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

    def test_draft_fallback_is_first_accessible_connection(self):
        """conn_a is the first active connection by pk but not accessible to ``unassigned``."""
        resp = self._draft(self.unassigned)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()['connection'], self.conn_shared.id)

    def test_reply_draft_on_inaccessible_message_400(self):
        self.assertEqual(self._draft(self.assigned, reply_to_message=self.msg_b.id).status_code, 400)
        resp = self._draft(self.unassigned, reply_to_message=self.msg_shared.id)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()['connection'], self.conn_shared.id)

    # -- send ---------------------------------------------------------------

    SEND_URL = '/api/social/email/send/'

    def _fake_send(self, **kwargs):
        message = self.create_email_message(
            connection=kwargs['connection'], folder='Sent', is_from_business=True,
        )
        return message.message_id, message

    @patch('social_integrations.email_utils.send_email_smtp')
    def test_send_from_inaccessible_connection_404(self, mocked_send):
        body = {'to_emails': ['to@example.com'], 'body_text': 'b', 'connection_id': self.conn_b.id}
        self.assertEqual(self.api_post(self.SEND_URL, body, user=self.assigned).status_code, 404)
        body['connection_id'] = self.conn_a.id
        self.assertEqual(self.api_post(self.SEND_URL, body, user=self.unassigned).status_code, 404)
        mocked_send.assert_not_called()

    @patch('social_integrations.email_utils.send_email_smtp')
    def test_send_defaults_to_first_accessible_connection(self, mocked_send):
        mocked_send.side_effect = lambda **kw: self._fake_send(**kw)
        resp = self.api_post(self.SEND_URL, {'to_emails': ['to@example.com'], 'body_text': 'b'}, user=self.unassigned)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(mocked_send.call_args.kwargs['connection'].id, self.conn_shared.id)
        resp = self.api_post(
            self.SEND_URL,
            {'to_emails': ['to@example.com'], 'body_text': 'b', 'connection_id': self.conn_shared.id},
            user=self.assigned,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

    # -- folders ------------------------------------------------------------

    FOLDERS_URL = '/api/social/email/folders/'

    @patch('social_integrations.email_utils.get_imap_folders', return_value={'success': True, 'folders': ['INBOX']})
    def test_folders_inaccessible_connection_404(self, mocked_folders):
        resp = self.api_get(f'{self.FOLDERS_URL}?connection_id={self.conn_b.id}', user=self.assigned)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        mocked_folders.assert_not_called()
        resp = self.api_get(f'{self.FOLDERS_URL}?connection_id={self.conn_shared.id}', user=self.unassigned)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['connection_id'], self.conn_shared.id)

    @patch('social_integrations.email_utils.get_imap_folders', return_value={'success': True, 'folders': ['INBOX']})
    def test_folders_all_lists_only_accessible(self, mocked_folders):
        resp = self.api_get(self.FOLDERS_URL, user=self.assigned)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {c['connection_id'] for c in resp.json()['connections']}, {self.conn_a.id, self.conn_shared.id},
        )
        resp = self.api_get(self.FOLDERS_URL, user=self.admin)
        self.assertEqual(len(resp.json()['connections']), 3)

    @patch('social_integrations.email_utils.get_available_folders', return_value=[{'name': 'INBOX', 'delimiter': '/', 'flags': []}])
    def test_legacy_folders_uses_first_accessible_connection(self, mocked_folders):
        resp = self.api_get(f'{LIST_URL}folders/', user=self.unassigned)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(mocked_folders.call_args[0][0].id, self.conn_shared.id)


class TestEmailMoveUsesMessageConnection(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='move-agent@test.com')
        self.conn_first = self.create_email_connection(email_address='first@example.com')
        self.conn_second = self.create_email_connection(email_address='second@example.com')
        self.msg = self.create_email_message(connection=self.conn_second, folder='INBOX')
        self.url = f'{LIST_URL}move/'

    @patch('social_integrations.email_utils.move_email_to_folder', return_value=True)
    def test_move_uses_owning_connection(self, mocked_move):
        resp = self.api_post(
            self.url,
            {'message_id': self.msg.message_id, 'source_folder': 'INBOX', 'target_folder': 'Archive'},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.json()['success'])
        called_connection = mocked_move.call_args[0][0]
        self.assertEqual(called_connection.id, self.conn_second.id)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.folder, 'Archive')

    @patch('social_integrations.email_utils.move_email_to_folder', return_value=True)
    def test_move_unknown_message_404(self, mocked_move):
        resp = self.api_post(
            self.url,
            {'message_id': '<nope@example.com>', 'source_folder': 'INBOX', 'target_folder': 'Archive'},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        mocked_move.assert_not_called()

    @patch('social_integrations.email_utils.move_email_to_folder', return_value=True)
    def test_move_inactive_connection_400(self, mocked_move):
        self.conn_second.is_active = False
        self.conn_second.save()
        resp = self.api_post(
            self.url,
            {'message_id': self.msg.message_id, 'source_folder': 'INBOX', 'target_folder': 'Archive'},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        mocked_move.assert_not_called()

    @patch('social_integrations.email_utils.move_email_to_folder', return_value=False)
    def test_move_imap_failure_500(self, mocked_move):
        resp = self.api_post(
            self.url,
            {'message_id': self.msg.message_id, 'source_folder': 'INBOX', 'target_folder': 'Archive'},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, 500)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.folder, 'INBOX')

    def test_move_missing_fields_400(self):
        resp = self.api_post(self.url, {'message_id': self.msg.message_id}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestEmailDraftConnection(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='draft-conn@test.com')
        self.conn_first = self.create_email_connection(email_address='dfirst@example.com')
        self.conn_second = self.create_email_connection(email_address='dsecond@example.com')
        self.url = '/api/social/email-drafts/'

    def test_reply_draft_uses_original_message_connection(self):
        original = self.create_email_message(connection=self.conn_second)
        resp = self.api_post(
            self.url,
            {'to_emails': [{'email': 'x@y.z'}], 'subject': 'Re', 'body_text': 'hi',
             'reply_to_message': original.id},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        draft = EmailDraft.objects.get(id=resp.json()['id'])
        self.assertEqual(draft.connection_id, self.conn_second.id)
        self.assertEqual(draft.created_by_id, self.agent.id)

    def test_explicit_connection_honoured(self):
        resp = self.api_post(
            self.url,
            {'to_emails': [], 'subject': 'New', 'body_text': 'hi', 'connection': self.conn_second.id},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()['connection'], self.conn_second.id)

    def test_no_connection_falls_back_to_first_active(self):
        resp = self.api_post(self.url, {'to_emails': [], 'subject': 'New', 'body_text': 'hi'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()['connection'], self.conn_first.id)

    def test_inactive_explicit_connection_400(self):
        self.conn_second.is_active = False
        self.conn_second.save()
        resp = self.api_post(
            self.url,
            {'to_emails': [], 'subject': 'New', 'body_text': 'hi', 'connection': self.conn_second.id},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_no_active_connection_400(self):
        self.conn_first.is_active = False
        self.conn_first.save()
        self.conn_second.is_active = False
        self.conn_second.save()
        resp = self.api_post(self.url, {'to_emails': [], 'subject': 'New', 'body_text': 'hi'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_assigned_user_cannot_draft_on_other_mailbox(self):
        """conn_second is assigned to someone else, so it is not a shared inbox for ``agent``."""
        EmailConnectionUserAssignment.objects.create(
            connection=self.conn_first, user=self.agent, assigned_by=self.agent,
        )
        other = self.create_user(email='draft-other@test.com')
        EmailConnectionUserAssignment.objects.create(
            connection=self.conn_second, user=other, assigned_by=other,
        )
        resp = self.api_post(
            self.url,
            {'to_emails': [], 'subject': 'New', 'body_text': 'hi', 'connection': self.conn_second.id},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestEmailSendBody(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='send-agent@test.com')
        self.conn_first = self.create_email_connection(email_address='sfirst@example.com')
        self.conn_second = self.create_email_connection(email_address='ssecond@example.com')
        self.url = '/api/social/email/send/'

    def _fake_send(self, **kwargs):
        connection = kwargs['connection']
        message = self.create_email_message(
            connection=connection, folder='Sent', is_from_business=True,
            subject=kwargs.get('subject', ''), body_text=kwargs.get('body_text', ''),
        )
        return message.message_id, message

    @patch('social_integrations.email_utils.send_email_smtp')
    def test_json_send_with_connection_id(self, mocked_send):
        mocked_send.side_effect = lambda **kw: self._fake_send(**kw)
        resp = self.api_post(
            self.url,
            {'to_emails': ['to@example.com'], 'cc_emails': ['cc@example.com'], 'subject': 'S',
             'body_text': 'B', 'connection_id': self.conn_second.id},
            user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        data = resp.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['data']['connection_id'], self.conn_second.id)
        self.assertEqual(data['data']['folder'], 'Sent')
        kwargs = mocked_send.call_args.kwargs
        self.assertEqual(kwargs['connection'].id, self.conn_second.id)
        self.assertEqual(kwargs['to_emails'], ['to@example.com'])
        self.assertEqual(kwargs['cc_emails'], ['cc@example.com'])
        self.assertEqual(kwargs['attachments'], [])

    @patch('social_integrations.email_utils.send_email_smtp')
    def test_multipart_send_repeated_keys_and_attachments(self, mocked_send):
        mocked_send.side_effect = lambda **kw: self._fake_send(**kw)
        client = self.authenticated_client(self.agent)
        resp = client.post(
            self.url,
            {
                'to_emails': ['a@example.com', 'b@example.com'],
                'subject': 'Multi',
                'body_text': 'hello',
                'attachments': [
                    SimpleUploadedFile('one.txt', b'one', content_type='text/plain'),
                    SimpleUploadedFile('two.bin', b'\x00\x01', content_type='application/octet-stream'),
                ],
            },
            format='multipart',
            HTTP_HOST='tenant.test.com',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        kwargs = mocked_send.call_args.kwargs
        self.assertEqual(kwargs['to_emails'], ['a@example.com', 'b@example.com'])
        self.assertEqual([a['filename'] for a in kwargs['attachments']], ['one.txt', 'two.bin'])
        self.assertEqual(kwargs['attachments'][0]['content'], b'one')
        self.assertEqual(kwargs['connection'].id, self.conn_first.id)  # default: first active

    def test_send_requires_body(self):
        resp = self.api_post(self.url, {'to_emails': ['to@example.com'], 'subject': 'S'}, user=self.agent)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_send_unknown_connection_404(self):
        resp = self.api_post(
            self.url, {'to_emails': ['to@example.com'], 'body_text': 'b', 'connection_id': 999999}, user=self.agent,
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class TestUnreadCountEmail(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.agent = self.create_user(email='unread-agent@test.com')
        self.url = '/api/social/unread-count/'

    def test_email_count_only_inbox_customer_unread(self):
        conn = self.create_email_connection()
        self.create_email_message(connection=conn, folder='INBOX', is_from_business=False, is_read_by_staff=False)
        self.create_email_message(connection=conn, folder='INBOX', is_from_business=False, is_read_by_staff=True)
        self.create_email_message(connection=conn, folder='Sent', is_from_business=True, is_read_by_staff=False)
        self.create_email_message(connection=conn, folder='Archive', is_from_business=False, is_read_by_staff=False)
        data = self.api_get(self.url, user=self.agent).json()
        self.assertEqual(data['email'], 1)
        self.assertIn('total', data)

    def test_email_count_respects_connection_assignment(self):
        conn = self.create_email_connection()
        other = self.create_user(email='other-unread@test.com')
        EmailConnectionUserAssignment.objects.create(connection=conn, user=other, assigned_by=other)
        self.create_email_message(connection=conn, folder='INBOX', is_from_business=False, is_read_by_staff=False)
        self.assertEqual(self.api_get(self.url, user=self.agent).json()['email'], 0)
        self.assertEqual(self.api_get(self.url, user=other).json()['email'], 1)


class TestEmailOpenApiSchema(SocialIntegrationTestCase):
    """The generated client is what the mobile app codes against — pin the declarations."""

    @classmethod
    def _schema(cls):
        from drf_spectacular.generators import SchemaGenerator
        patterns = [path('api/social/', include('social_integrations.urls'))]
        generator = SchemaGenerator(patterns=patterns)
        return generator.get_schema(request=None, public=True)

    def test_list_parameters_and_lite_component(self):
        schema = self._schema()
        op = schema['paths']['/api/social/email-messages/']['get']
        names = {p['name'] for p in op['parameters']}
        self.assertTrue(
            {'folder', 'starred', 'is_read', 'label', 'thread_id', 'connection_id', 'search', 'lite', 'page', 'page_size'}
            <= names
        )
        ref = op['responses']['200']['content']['application/json']['schema']['$ref']
        self.assertEqual(ref, '#/components/schemas/PaginatedEmailMessageList')  # unchanged for the web client

        lite = schema['paths']['/api/social/email-messages/lite/']['get']
        ref = lite['responses']['200']['content']['application/json']['schema']['$ref']
        self.assertEqual(ref, '#/components/schemas/PaginatedEmailMessageLiteList')
        props = schema['components']['schemas']['EmailMessageLite']['properties']
        self.assertIn('snippet', props)
        self.assertIn('attachments_count', props)
        self.assertNotIn('body_text', props)
        self.assertNotIn('body_html', props)

        detail = schema['components']['schemas']['EmailMessageDetail']['properties']
        self.assertIn('body_html_safe', detail)

    def test_threads_is_bare_array(self):
        schema = self._schema()
        op = schema['paths']['/api/social/email-messages/threads/']['get']
        self.assertEqual({p['name'] for p in op['parameters']}, {'folder', 'connection_id', 'lite'})
        body = op['responses']['200']['content']['application/json']['schema']
        self.assertEqual(body['type'], 'array')
        self.assertEqual(body['items']['$ref'], '#/components/schemas/EmailThread')

    def test_send_has_json_and_multipart_bodies(self):
        schema = self._schema()
        op = schema['paths']['/api/social/email/send/']['post']
        content = op['requestBody']['content']
        self.assertIn('application/json', content)
        self.assertIn('multipart/form-data', content)
        multipart = schema['components']['schemas']['EmailSendMultipartRequest']['properties']
        self.assertEqual(multipart['attachments']['type'], 'array')
        self.assertEqual(multipart['to_emails']['type'], 'array')
        self.assertIn('connection_id', multipart)
        self.assertIn('EmailSendResponse', op['responses']['200']['content']['application/json']['schema']['$ref'])

    def test_function_views_are_typed(self):
        schema = self._schema()
        paths = schema['paths']
        self.assertEqual(
            paths['/api/social/email/status/']['get']['responses']['200']['content']['application/json']['schema']['$ref'],
            '#/components/schemas/EmailConnectionStatusResponse',
        )
        self.assertIn('EmailMessageActionRequest', paths['/api/social/email/action/']['post']['requestBody']['content']['application/json']['schema']['$ref'])
        self.assertEqual({p['name'] for p in paths['/api/social/email/folders/']['get']['parameters']}, {'connection_id'})
        self.assertEqual(
            paths['/api/social/email-messages/move/']['post']['requestBody']['content']['application/json']['schema']['$ref'],
            '#/components/schemas/EmailMoveRequest',
        )
        self.assertIn('requestBody', paths['/api/social/email/sync/']['post'])
        self.assertIn('requestBody', paths['/api/social/email/signature/']['patch'])
        self.assertIn('requestBody', paths['/api/social/email/assignments/create/']['post'])
        self.assertEqual(
            paths['/api/social/unread-count/']['get']['responses']['200']['content']['application/json']['schema']['$ref'],
            '#/components/schemas/SocialUnreadCountResponse',
        )

    def test_request_component_names(self):
        """COMPONENT_SPLIT_REQUEST appends 'Request' itself — the names must not double it."""
        components = self._schema()['components']['schemas']
        expected = {
            '/api/social/email-messages/move/': ('post', 'EmailMoveRequest'),
            '/api/social/email/connect/': ('post', 'EmailConnectRequest'),
            '/api/social/email/update/': ('post', 'EmailConnectionUpdateRequest'),
            '/api/social/email/reactivate/': ('post', 'EmailReactivateRequest'),
            '/api/social/email/disconnect/': ('post', 'EmailConnectionIdRequest'),
            '/api/social/email/sync/': ('post', 'EmailSyncRequest'),
            '/api/social/email/sync/settings/': ('post', 'EmailSyncDaysRequest'),
            '/api/social/email/assignments/create/': ('post', 'EmailAssignmentCreateRequest'),
        }
        paths = self._schema()['paths']
        for route, (method, name) in expected.items():
            self.assertIn(name, components, name)
            self.assertNotIn(name + 'Request', components, name)
            self.assertEqual(
                paths[route][method]['requestBody']['content']['application/json']['schema']['$ref'],
                f'#/components/schemas/{name}', route,
            )
        self.assertIn('EmailAssignedUsersUpdateRequest', components)
        self.assertNotIn('EmailAssignedUsersUpdateRequestRequest', components)
