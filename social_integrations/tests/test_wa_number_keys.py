"""WhatsApp chats are keyed by the digits-only number everywhere.

Regression for the nitchiani inbox (2026-11-13): rating requests stored the
customer as ``+995…`` so the list showed the same chat twice, and an archive
keyed by the '+' form left the chat in the active tab.
"""
import importlib
from datetime import timedelta

from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from social_integrations.conversation_keys import (
    assignment_conversation_id, assignment_lookup_ids, session_conversation_id, wa_number,
)
from social_integrations.models import ChatAssignment, ConversationArchive, WhatsAppMessage
from social_integrations.tests.conftest import SocialIntegrationTestCase

LIST = '/api/social/conversations/'


class WaNumberHelperTests(SocialIntegrationTestCase):

    def test_wa_number_keeps_digits_only(self):
        self.assertEqual(wa_number('+995 579 37 03 74'), '995579370374')
        self.assertEqual(wa_number('995577603228'), '995577603228')
        self.assertEqual(wa_number(None), '')

    def test_key_helpers_normalise_whatsapp_only(self):
        self.assertEqual(session_conversation_id('whatsapp', '+995500'), '995500')
        self.assertEqual(assignment_conversation_id('whatsapp', '+995500'), '995500')
        self.assertEqual(assignment_lookup_ids('whatsapp', '+995500'), ['+995500', '995500'])
        self.assertEqual(session_conversation_id('facebook', '+abc'), '+abc')

    def test_save_normalises_both_numbers(self):
        waba = self.create_wa_account(waba_id='wabasave', phone_number='+995579370374')
        msg = self.create_wa_message(
            business_account=waba, from_number='+995 579 37 03 74', to_number='+995577603228',
            is_from_business=True,
        )
        msg.refresh_from_db()
        self.assertEqual((msg.from_number, msg.to_number), ('995579370374', '995577603228'))


class WaInboxKeyTests(SocialIntegrationTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.admin = self.create_admin(email='wa-admin@test.com')
        self.waba = self.create_wa_account(waba_id='waba9', phone_number='+995579370374')
        now = timezone.now()
        self.create_wa_message(business_account=self.waba, from_number='995500', contact_name='Cust',
                               timestamp=now - timedelta(minutes=5), is_from_business=False)
        # A rating request written the old way: '+' on both sides.
        self.create_wa_message(business_account=self.waba, from_number='+995 579 37 03 74',
                               to_number='+995500', message_text='rate us',
                               timestamp=now - timedelta(minutes=1), is_from_business=True)

    def _wa_rows(self, **params):
        resp = self.api_get(LIST, user=self.admin, data={'platforms': 'whatsapp', **params})
        self.assertEqual(resp.status_code, 200, resp.content)
        return [r for r in resp.data['results'] if r['platform'] == 'whatsapp']

    def test_list_has_one_row_per_customer(self):
        rows = self._wa_rows()
        self.assertEqual([r['conversation_id'] for r in rows], ['wa_waba9_995500'])
        self.assertEqual(rows[0]['last_message']['text'], 'rate us')
        self.assertEqual(rows[0]['sender_id'], '995500')

    def test_thread_view_includes_the_reply_whatever_the_client_sends(self):
        for number in ('995500', '+995500'):
            resp = self.api_get('/api/social/whatsapp-messages/', user=self.admin,
                                data={'waba_id': 'waba9', 'from_number': number})
            self.assertEqual(resp.status_code, 200, resp.content)
            texts = sorted(m['message_text'] for m in resp.data['results'])
            self.assertIn('rate us', texts, number)
            self.assertEqual(len(texts), 2, number)

    def test_archive_with_plus_hides_the_chat_and_unarchive_restores_it(self):
        payload = {'conversations': [
            {'platform': 'whatsapp', 'conversation_id': '+995500', 'account_id': 'waba9'},
        ]}
        resp = self.api_post('/api/social/conversations/archive/', payload, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            list(ConversationArchive.objects.filter(platform='whatsapp').values_list('conversation_id', flat=True)),
            ['995500'],
        )
        cache.clear()
        self.assertEqual(self._wa_rows(), [])
        self.assertEqual([r['conversation_id'] for r in self._wa_rows(archived='true')], ['wa_waba9_995500'])

        resp = self.api_post('/api/social/conversations/unarchive/', payload, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(ConversationArchive.objects.filter(platform='whatsapp').exists())
        cache.clear()
        self.assertEqual([r['conversation_id'] for r in self._wa_rows()], ['wa_waba9_995500'])

    def test_unarchive_removes_a_legacy_plus_row(self):
        ConversationArchive.objects.create(platform='whatsapp', conversation_id='+995500', account_id='waba9')
        resp = self.api_post('/api/social/conversations/unarchive/', {'conversations': [
            {'platform': 'whatsapp', 'conversation_id': '995500', 'account_id': 'waba9'},
        ]}, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(ConversationArchive.objects.filter(platform='whatsapp').exists())

    def test_mark_read_with_plus_marks_the_customer_messages(self):
        resp = self.api_post('/api/social/mark-read/', {
            'platform': 'whatsapp', 'conversation_id': '+995500', 'account_id': 'waba9',
        }, user=self.admin)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(WhatsAppMessage.objects.filter(
            from_number='995500', is_from_business=False, is_read_by_staff=False,
        ).exists())


class WaNumberMigrationTests(SocialIntegrationTestCase):

    def test_forwards_rewrites_rows_and_collapses_twins(self):
        waba = self.create_wa_account(waba_id='wabamig', phone_number='+995579370374')
        msg = self.create_wa_message(business_account=waba, from_number='995500', is_from_business=False)
        # Legacy shapes, written past save().
        WhatsAppMessage.objects.filter(pk=msg.pk).update(from_number='+995 579 37 03 74', to_number='+995500')
        ConversationArchive.objects.create(platform='whatsapp', conversation_id='995500', account_id='wabamig')
        ConversationArchive.objects.create(platform='whatsapp', conversation_id='+995500', account_id='wabamig')
        ChatAssignment.objects.create(platform='whatsapp', conversation_id='+995501', account_id='wabamig',
                                      assigned_user=None)
        ConversationArchive.objects.create(platform='facebook', conversation_id='+keep', account_id='pg')

        mod = importlib.import_module('social_integrations.migrations.0066_whatsapp_numbers_digits_only')
        from django.apps import apps
        with connection.schema_editor() as editor:
            mod.forwards(apps, editor)

        msg.refresh_from_db()
        self.assertEqual((msg.from_number, msg.to_number), ('995579370374', '995500'))
        self.assertEqual(
            sorted(ConversationArchive.objects.filter(platform='whatsapp').values_list('conversation_id', flat=True)),
            ['995500'],
        )
        self.assertEqual(ChatAssignment.objects.get(platform='whatsapp').conversation_id, '995501')
        self.assertTrue(ConversationArchive.objects.filter(platform='facebook', conversation_id='+keep').exists())
