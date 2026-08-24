"""
Tests for the cross-platform transcript registry.

The registry must normalise each platform's outbound-flag polarity and
conversation scoping — especially widget's inverted is_from_visitor and
WhatsApp's from/to-number split.
"""
from datetime import timedelta

from django.utils import timezone

from social_integrations.models import (
    TelegramAccount,
    TelegramMessage,
    WidgetMessage,
    WidgetSession,
)
from social_integrations.services.conversation_transcript import (
    build_transcript,
    get_customer_display_name,
    get_last_inbound_at,
    get_latest_inbound_pk,
)
from .conftest import SocialIntegrationTestCase


class TestFacebookTranscript(SocialIntegrationTestCase):
    def test_roles_and_order(self):
        conn = self.create_fb_connection()
        now = timezone.now()
        self.create_fb_message(
            page_connection=conn, sender_id='psid_t1', sender_name='Nino',
            message_text='hi there', is_from_page=False,
            timestamp=now - timedelta(minutes=2),
        )
        self.create_fb_message(
            page_connection=conn, sender_id='psid_t1', sender_name='Page',
            message_text='hello!', is_from_page=True,
            timestamp=now - timedelta(minutes=1),
        )

        transcript = build_transcript('facebook', conn.page_id, 'psid_t1')
        self.assertEqual(len(transcript), 2)
        self.assertEqual(transcript[0]['role'], 'customer')
        self.assertEqual(transcript[0]['text'], 'hi there')
        self.assertEqual(transcript[1]['role'], 'business')

    def test_deleted_messages_excluded(self):
        conn = self.create_fb_connection()
        self.create_fb_message(
            page_connection=conn, sender_id='psid_t2',
            message_text='visible', is_from_page=False,
        )
        self.create_fb_message(
            page_connection=conn, sender_id='psid_t2',
            message_text='ghost', is_from_page=False, is_deleted=True,
        )
        transcript = build_transcript('facebook', conn.page_id, 'psid_t2')
        self.assertEqual([e['text'] for e in transcript], ['visible'])

    def test_inbound_helpers(self):
        conn = self.create_fb_connection()
        now = timezone.now()
        self.create_fb_message(
            page_connection=conn, sender_id='psid_t3',
            is_from_page=False, timestamp=now - timedelta(hours=2),
        )
        latest = self.create_fb_message(
            page_connection=conn, sender_id='psid_t3', sender_name='Gio',
            is_from_page=False, timestamp=now - timedelta(hours=1),
        )
        # Outbound must not count as inbound.
        self.create_fb_message(
            page_connection=conn, sender_id='psid_t3',
            is_from_page=True, timestamp=now,
        )

        self.assertEqual(
            get_latest_inbound_pk('facebook', conn.page_id, 'psid_t3'), latest.pk
        )
        self.assertEqual(
            get_last_inbound_at('facebook', conn.page_id, 'psid_t3'),
            latest.timestamp,
        )
        self.assertEqual(
            get_customer_display_name('facebook', conn.page_id, 'psid_t3'), 'Gio'
        )


class TestWhatsAppTranscript(SocialIntegrationTestCase):
    def test_from_to_number_scoping(self):
        account = self.create_wa_account()
        customer = '+995599000001'
        other = '+995599000002'
        now = timezone.now()
        self.create_wa_message(
            business_account=account, from_number=customer,
            message_text='inbound msg', is_from_business=False,
            timestamp=now - timedelta(minutes=3),
        )
        self.create_wa_message(
            business_account=account, from_number=account.phone_number,
            to_number=customer, message_text='outbound msg',
            is_from_business=True, timestamp=now - timedelta(minutes=2),
        )
        # A different customer's message must not leak in.
        self.create_wa_message(
            business_account=account, from_number=other,
            message_text='other convo', is_from_business=False,
            timestamp=now - timedelta(minutes=1),
        )

        transcript = build_transcript('whatsapp', account.waba_id, customer)
        self.assertEqual(
            [(e['role'], e['text']) for e in transcript],
            [('customer', 'inbound msg'), ('business', 'outbound msg')],
        )


class TestTelegramTranscript(SocialIntegrationTestCase):
    def _account(self):
        return TelegramAccount.objects.create(
            telegram_user_id=555001, phone_number='+995599111111',
            first_name='Biz', session_string='sess',
        )

    def _msg(self, account, peer_id, text, outbound, ts, name='Peer'):
        return TelegramMessage.objects.create(
            account=account,
            message_id=f'{account.telegram_user_id}_{self._next()}',
            telegram_msg_id=self._next(),
            peer_id=peer_id,
            peer_name=name,
            message_text=text,
            is_from_business=outbound,
            timestamp=ts,
        )

    def test_transcript_and_helpers(self):
        account = self._account()
        now = timezone.now()
        inbound = self._msg(account, 777, 'gamarjoba', False, now - timedelta(minutes=5))
        self._msg(account, 777, 'hello!', True, now - timedelta(minutes=4))
        # Different peer excluded.
        self._msg(account, 888, 'sxva', False, now)

        transcript = build_transcript('telegram', str(account.telegram_user_id), '777')
        self.assertEqual(
            [(e['role'], e['text']) for e in transcript],
            [('customer', 'gamarjoba'), ('business', 'hello!')],
        )
        self.assertEqual(
            get_latest_inbound_pk('telegram', str(account.telegram_user_id), '777'),
            inbound.pk,
        )


class TestWidgetTranscript(SocialIntegrationTestCase):
    def test_inverted_polarity(self):
        session = WidgetSession.objects.create(
            connection_id=42, session_id='sess_ai_1', visitor_id='v1',
            visitor_name='Visitor Vano',
        )
        now = timezone.now()
        WidgetMessage.objects.create(
            session=session, message_id='wm1', message_text='need help',
            is_from_visitor=True, timestamp=now - timedelta(minutes=2),
        )
        WidgetMessage.objects.create(
            session=session, message_id='wm2', message_text='sure!',
            is_from_visitor=False, timestamp=now - timedelta(minutes=1),
        )

        transcript = build_transcript('widget', '42', 'sess_ai_1')
        self.assertEqual(
            [(e['role'], e['text']) for e in transcript],
            [('customer', 'need help'), ('business', 'sure!')],
        )
        self.assertEqual(transcript[0]['sender_name'], 'Visitor Vano')
        self.assertEqual(
            get_customer_display_name('widget', '42', 'sess_ai_1'),
            'Visitor Vano',
        )


class TestTranscriptCaps(SocialIntegrationTestCase):
    def test_limit_and_char_cap_keep_newest(self):
        conn = self.create_fb_connection()
        now = timezone.now()
        for i in range(6):
            self.create_fb_message(
                page_connection=conn, sender_id='psid_cap',
                message_text=f'm{i}', is_from_page=False,
                timestamp=now - timedelta(minutes=10 - i),
            )
        transcript = build_transcript(
            'facebook', conn.page_id, 'psid_cap', limit=4
        )
        self.assertEqual([e['text'] for e in transcript], ['m2', 'm3', 'm4', 'm5'])

        capped = build_transcript(
            'facebook', conn.page_id, 'psid_cap', limit=6, max_chars=5
        )
        # Newest entry always survives the char cap.
        self.assertEqual(capped[-1]['text'], 'm5')
        self.assertLess(len(capped), 6)

    def test_unknown_platform_raises(self):
        with self.assertRaises(ValueError):
            build_transcript('carrierpigeon', '1', '2')
