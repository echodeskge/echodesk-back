"""
Inbox / channels onboarding step (key ``social_integrations``, feature
``social_integrations``, order 20).

Answers schema (``~/Echodesk/.wt/onb/SCHEMAS.md``)::

    {
      "channels": ["facebook", "whatsapp", "telegram", "email", "widget"],   # informational
      "timezone": "Asia/Tbilisi",
      "hours": Hours,                               # → away_hours_enabled + away_hours_schedule
      "welcome_message": I18n, "away_message": I18n,   # → auto_reply_settings (facebook/instagram/whatsapp)
      "assignment": "shared" | "claim",
      "collect_rating": bool,
      "quick_replies": [{"title": str, "message": I18n}],
      "ai": {"enabled": bool, "company_description": str, "language": "ka"|"en"} | null
    }

Only the keys present are applied. ``apply`` writes the singleton
``SocialIntegrationSettings`` (timezone, away hours = the hours OUTSIDE
``from``–``to`` per weekday in the model's ``{"monday": [0..23]}`` format,
per-platform welcome/away auto-replies in the tenant's language with a
fallback to the other one, the claim/shared assignment flags with the
60-minute stale reminder, the rating flags, ``widget_enabled`` when the
widget channel is picked), ``QuickReply`` rows matched by title, and — only
when the tenant has the ``ai_companion`` feature — ``AICompanionSettings``
plus one ``AICompanionChannel`` per connected replyable channel.

Connections themselves (OAuth, Telegram login, e-mail, widget) happen
through the existing endpoints; ``context.connections`` lists what exists
(ids, display names, ``is_active`` — never tokens).
"""
import logging

from rest_framework import serializers

from onboarding.base import OnboardingStep, empty_result
from onboarding.common import (
    DEFAULT_HOURS,
    HoursSerializer,
    I18nSerializer,
    away_schedule_to_hours,
    hours_to_away_schedule,
    i18n,
    pick_text,
    tenant_language,
    users_context,
)
from onboarding.registry import register

from .models import (
    AICompanionChannel,
    AICompanionSettings,
    EmailConnection,
    FacebookPageConnection,
    InstagramAccountConnection,
    QuickReply,
    SocialIntegrationSettings,
    TelegramAccount,
    WhatsAppBusinessAccount,
)

logger = logging.getLogger(__name__)

CHANNELS = ['facebook', 'whatsapp', 'telegram', 'email', 'widget']
AUTO_REPLY_PLATFORMS = ('facebook', 'instagram', 'whatsapp')
ASSIGNMENT_MODES = ['shared', 'claim']
STALE_REMINDER_MINUTES = 60
AI_LANGUAGES = ['ka', 'en']

OAUTH = {
    'facebook_start': '/api/social/facebook/oauth/start/',
    'whatsapp_start': '/api/social/whatsapp/oauth/start/',
}

DEFAULT_WELCOME = i18n(
    'Hello! Thank you for writing to us. We will reply as soon as possible.',
    'გამარჯობა! გმადლობთ, რომ მოგვწერეთ. მალე გიპასუხებთ.',
)
DEFAULT_AWAY = i18n(
    'Thank you for your message. We are offline right now and will reply during working hours.',
    'გმადლობთ შეტყობინებისთვის. ამჟამად არასამუშაო საათებია, გიპასუხებთ სამუშაო დროს.',
)


def _preset(title_en, title_ka, msg_en, msg_ka):
    return {'title': i18n(title_en, title_ka), 'message': i18n(msg_en, msg_ka)}


QUICK_REPLY_PRESETS = {
    'salon': [
        _preset('Book a visit', 'ჩაწერა',
                'Hello {{customer_name}}! To book a visit, tell us the service and the time that suits you.',
                'გამარჯობა {{customer_name}}! ვიზიტზე ჩასაწერად მოგვწერეთ სასურველი პროცედურა და დრო.'),
        _preset('Prices', 'ფასები',
                'Our current price list is on our page. Which service are you interested in?',
                'ჩვენი ფასები გვერდზეა განთავსებული. რომელი პროცედურა გაინტერესებთ?'),
        _preset('Address', 'მისამართი',
                'We are waiting for you at our salon. Let us know if you need directions.',
                'გელოდებით ჩვენს სალონში. თუ გზის ახსნა დაგჭირდებათ, მოგვწერეთ.'),
    ],
    'clinic': [
        _preset('Appointment', 'ვიზიტის დანიშვნა',
                'Hello {{customer_name}}! To book an appointment, tell us the doctor and a convenient time.',
                'გამარჯობა {{customer_name}}! ვიზიტის დასანიშნად მოგვწერეთ ექიმი და თქვენთვის მოსახერხებელი დრო.'),
        _preset('Preparation', 'მომზადება',
                'Please arrive 10 minutes early and bring your ID and previous test results, if any.',
                'გთხოვთ, მობრძანდეთ 10 წუთით ადრე, თან იქონიეთ პირადობა და წინა კვლევების პასუხები, თუ გაქვთ.'),
        _preset('Working hours', 'სამუშაო საათები',
                'We are open Monday to Friday, 09:00–18:00.',
                'ვმუშაობთ ორშაბათიდან პარასკევის ჩათვლით, 09:00–18:00.'),
    ],
    'real_estate': [
        _preset('Viewing', 'დათვალიერება',
                'Hello {{customer_name}}! When would you like to view the property? We will arrange it.',
                'გამარჯობა {{customer_name}}! როდის გსურთ ობიექტის ნახვა? მოვაწყობთ დათვალიერებას.'),
        _preset('Details', 'დეტალები',
                'Please send the listing you are interested in and we will share all the details.',
                'გთხოვთ, გამოგვიგზავნოთ განცხადება, რომელიც გაინტერესებთ, და ყველა დეტალს გაგიზიარებთ.'),
        _preset('Documents', 'დოკუმენტები',
                'For the deal we need an ID and, for the owner, the ownership certificate.',
                'გარიგებისთვის საჭიროა პირადობის მოწმობა, მესაკუთრისგან კი საკუთრების ამონაწერი.'),
    ],
    'shop': [
        _preset('Order status', 'შეკვეთის სტატუსი',
                'Hello {{customer_name}}! Please send your order number and we will check the status.',
                'გამარჯობა {{customer_name}}! მოგვწერეთ შეკვეთის ნომერი და სტატუსს შეგიმოწმებთ.'),
        _preset('Delivery', 'მიწოდება',
                'Delivery in Tbilisi takes 1–2 business days, in the regions 2–4.',
                'მიწოდება თბილისში 1–2 სამუშაო დღეა, რეგიონებში 2–4.'),
        _preset('Returns', 'დაბრუნება',
                'You can return an item within 14 days if it is unused and in its original packaging.',
                'ნივთის დაბრუნება შესაძლებელია 14 დღის განმავლობაში, თუ ის გამოუყენებელია და ორიგინალ შეფუთვაშია.'),
    ],
    'call_centre': [
        _preset('Greeting', 'მისალმება',
                'Hello {{customer_name}}! You are talking to {{agent_name}}. How can I help?',
                'გამარჯობა {{customer_name}}! თქვენ გესაუბრებათ {{agent_name}}. რით შემიძლია დაგეხმაროთ?'),
        _preset('Hold on', 'მოიცადეთ',
                'One moment please, I am checking this for you.',
                'ერთი წუთით, ვამოწმებ.'),
        _preset('Thanks', 'მადლობა',
                'Thank you for contacting us! Have a great day.',
                'გმადლობთ, რომ დაგვიკავშირდით! წარმატებულ დღეს გისურვებთ.'),
    ],
    'logistics': [
        _preset('Tracking', 'ამანათის სტატუსი',
                'Hello {{customer_name}}! Please send the tracking number and we will locate your parcel.',
                'გამარჯობა {{customer_name}}! მოგვწერეთ თრექინგ-ნომერი და ამანათის ადგილმდებარეობას შეგიმოწმებთ.'),
        _preset('Pickup', 'აღება',
                'We can pick up the shipment at your address. Please send the address and a convenient time.',
                'ტვირთს თქვენი მისამართიდან ავიღებთ. მოგვწერეთ მისამართი და მოსახერხებელი დრო.'),
        _preset('Delay', 'დაგვიანება',
                'We are sorry for the delay. Your shipment is on its way and we will update you as soon as it moves.',
                'ბოდიშს გიხდით დაგვიანებისთვის. ტვირთი გზაშია და შეტყობინებას მიიღებთ, როგორც კი გადაადგილდება.'),
    ],
    'services': [
        _preset('Quote', 'შეთავაზება',
                'Hello {{customer_name}}! Describe what you need and we will send you a quote.',
                'გამარჯობა {{customer_name}}! აღწერეთ, რა გჭირდებათ, და შეთავაზებას გამოგიგზავნით.'),
        _preset('Visit', 'ვიზიტი',
                'Our specialist can come to you. Which day suits you?',
                'ჩვენი სპეციალისტი ადგილზე მოვა. რომელი დღე გაწყობთ?'),
        _preset('Warranty', 'გარანტია',
                'All our work comes with a warranty. Tell us what happened and we will sort it out.',
                'ჩვენს ყველა სამუშაოს გარანტია აქვს. მოგვწერეთ, რა მოხდა, და მოვაგვარებთ.'),
    ],
    'other': [
        _preset('Greeting', 'მისალმება',
                'Hello {{customer_name}}! How can we help you?',
                'გამარჯობა {{customer_name}}! რით შეგვიძლია დაგეხმაროთ?'),
        _preset('Working hours', 'სამუშაო საათები',
                'We are open Monday to Friday, 09:00–18:00.',
                'ვმუშაობთ ორშაბათიდან პარასკევის ჩათვლით, 09:00–18:00.'),
        _preset('Thanks', 'მადლობა',
                'Thank you for contacting us! Have a great day.',
                'გმადლობთ, რომ დაგვიკავშირდით! წარმატებულ დღეს გისურვებთ.'),
    ],
}


def _validate_timezone(value):
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(value)
    except Exception:   # noqa: BLE001 — ZoneInfoNotFoundError / ValueError / KeyError
        raise serializers.ValidationError(f'Invalid timezone: {value}')
    return value


def _ai_feature_enabled(tenant):
    from onboarding.services import enabled_feature_keys
    return 'ai_companion' in enabled_feature_keys(tenant)


# ── serializer ──

class QuickReplyAnswerSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=100)
    message = I18nSerializer()

    def validate_title(self, value):
        value = ' '.join(value.split())
        if not value:
            raise serializers.ValidationError('Title cannot be blank.')
        return value


class AIAnswerSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(default=False)
    company_description = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    language = serializers.ChoiceField(choices=AI_LANGUAGES, required=False)


class SocialAnswersSerializer(serializers.Serializer):
    channels = serializers.ListField(child=serializers.ChoiceField(choices=CHANNELS), required=False)
    timezone = serializers.CharField(max_length=50, required=False, validators=[_validate_timezone])
    hours = HoursSerializer(required=False, allow_null=True)
    welcome_message = I18nSerializer(required=False, require_text=False)
    away_message = I18nSerializer(required=False, require_text=False)
    assignment = serializers.ChoiceField(choices=ASSIGNMENT_MODES, required=False)
    collect_rating = serializers.BooleanField(required=False)
    quick_replies = QuickReplyAnswerSerializer(many=True, required=False)
    ai = AIAnswerSerializer(required=False, allow_null=True)

    def validate_channels(self, value):
        return list(dict.fromkeys(value))

    def validate_quick_replies(self, value):
        seen = set()
        for row in value:
            key = row['title'].lower()
            if key in seen:
                raise serializers.ValidationError(f'"{row["title"]}" is listed more than once.')
            seen.add(key)
        return value


# ── step ──

@register
class SocialStep(OnboardingStep):
    key = 'social_integrations'
    feature_key = 'social_integrations'
    order = 20
    serializer_class = SocialAnswersSerializer

    # -- read --

    def context(self, tenant, request):
        language = tenant_language(tenant)
        return {
            'users': users_context(),
            'connections': self._connections(tenant),
            'oauth': dict(OAUTH),
            'has_ai_feature': _ai_feature_enabled(tenant),
            'quick_reply_presets': {
                industry: [
                    {
                        'title': preset['title'][language],
                        'title_i18n': preset['title'],
                        'message': preset['message'],
                    }
                    for preset in presets
                ]
                for industry, presets in QUICK_REPLY_PRESETS.items()
            },
        }

    def prefill(self, tenant, state):
        language = tenant_language(tenant)
        settings = SocialIntegrationSettings.objects.first()
        industry = (state.industry if state else None) or 'other'

        channels = []
        if FacebookPageConnection.objects.filter(is_active=True).exists():
            channels.append('facebook')
        if WhatsAppBusinessAccount.objects.filter(is_active=True).exists():
            channels.append('whatsapp')
        if TelegramAccount.objects.filter(is_active=True).exists():
            channels.append('telegram')
        if EmailConnection.objects.filter(is_active=True).exists():
            channels.append('email')
        if self._widget_connections(tenant):
            channels.append('widget')

        hours = None
        if settings and settings.away_hours_enabled and settings.away_hours_schedule:
            hours = away_schedule_to_hours(settings.away_hours_schedule)
        if hours is None:
            booking = state.answers_of('booking_management') if state else {}
            hours = (booking or {}).get('hours') or DEFAULT_HOURS

        auto = (settings.auto_reply_settings if settings else None) or {}
        fb = auto.get('facebook') or {}
        welcome = dict(DEFAULT_WELCOME)
        away = dict(DEFAULT_AWAY)
        if fb.get('welcome_message'):
            welcome[language] = fb['welcome_message']
        if fb.get('away_message'):
            away[language] = fb['away_message']

        quick_replies = [
            {'title': qr.title, 'message': {language: qr.message, ('en' if language == 'ka' else 'ka'): ''}}
            for qr in QuickReply.objects.order_by('position', 'id')[:20]
        ]
        if not quick_replies:
            quick_replies = [
                {'title': preset['title'][language], 'message': dict(preset['message'])}
                for preset in QUICK_REPLY_PRESETS.get(industry, QUICK_REPLY_PRESETS['other'])
            ]

        ai = None
        if _ai_feature_enabled(tenant):
            ai_settings = AICompanionSettings.objects.first()
            ai = {
                'enabled': bool(ai_settings and ai_settings.is_enabled),
                'company_description': (ai_settings.guidance_prompt if ai_settings else '') or '',
                'language': (ai_settings.language if ai_settings and ai_settings.language in AI_LANGUAGES else language),
            }

        tz = settings.timezone if settings and settings.timezone and settings.timezone != 'UTC' else 'Asia/Tbilisi'
        claim = bool(settings and settings.chat_assignment_enabled)
        return {
            'channels': channels,
            'timezone': tz,
            'hours': hours,
            'welcome_message': welcome,
            'away_message': away,
            'assignment': 'claim' if claim else 'shared',
            'collect_rating': bool(settings and (settings.collect_customer_rating or settings.link_based_rating_enabled)),
            'quick_replies': quick_replies,
            'ai': ai,
        }

    # -- write --

    def apply(self, tenant, answers, user):
        result = empty_result()
        created, updated, notes = result['created'], result['updated'], result['notes']
        language = tenant_language(tenant)

        settings = SocialIntegrationSettings.objects.first()
        if settings is None:
            settings = SocialIntegrationSettings.objects.create()
            created['settings'] = 1
        changed = set()

        if 'timezone' in answers:
            settings.timezone = answers['timezone']
            changed.add('timezone')

        if answers.get('hours') is not None:   # null = not answered; a closed day is null inside
            settings.away_hours_schedule = hours_to_away_schedule(answers['hours'])
            settings.away_hours_enabled = True
            changed.update({'away_hours_schedule', 'away_hours_enabled'})

        if 'welcome_message' in answers or 'away_message' in answers:
            auto = dict(settings.auto_reply_settings or {})
            welcome = pick_text(answers.get('welcome_message'), language) if 'welcome_message' in answers else None
            away = pick_text(answers.get('away_message'), language) if 'away_message' in answers else None
            for platform in AUTO_REPLY_PLATFORMS:
                entry = dict(auto.get(platform) or {})
                if welcome is not None:
                    entry['welcome_message'] = welcome
                    entry['welcome_enabled'] = bool(welcome)
                if away is not None:
                    entry['away_message'] = away
                    entry['away_enabled'] = bool(away)
                auto[platform] = entry
            settings.auto_reply_settings = auto
            changed.add('auto_reply_settings')

        if 'assignment' in answers:
            claim = answers['assignment'] == 'claim'
            settings.chat_assignment_enabled = claim
            settings.session_management_enabled = claim
            settings.hide_assigned_chats = claim
            settings.stale_assignment_reminder_enabled = claim
            if claim:
                settings.stale_assignment_reminder_minutes = STALE_REMINDER_MINUTES
            changed.update({
                'chat_assignment_enabled', 'session_management_enabled', 'hide_assigned_chats',
                'stale_assignment_reminder_enabled', 'stale_assignment_reminder_minutes',
            })

        if 'collect_rating' in answers:
            settings.collect_customer_rating = answers['collect_rating']
            settings.link_based_rating_enabled = answers['collect_rating']
            changed.update({'collect_customer_rating', 'link_based_rating_enabled'})

        if 'widget' in (answers.get('channels') or []) and not settings.widget_enabled:
            settings.widget_enabled = True
            changed.add('widget_enabled')

        if changed:
            settings.save(update_fields=sorted(changed) + ['updated_at'])
            updated.extend(sorted(changed))

        if 'quick_replies' in answers:
            created['quick_replies'] = self._ensure_quick_replies(answers['quick_replies'], language, user)

        if 'ai' in answers and answers['ai'] is not None:
            if _ai_feature_enabled(tenant):
                ai_created, ai_updated, channel_count = self._apply_ai(tenant, answers['ai'])
                created['ai_settings'] = ai_created
                created['ai_channels'] = channel_count
                updated.extend(ai_updated)
            else:
                notes.append('AI assistant answers ignored: the ai_companion feature is not enabled.')

        return result

    # -- helpers --

    @staticmethod
    def _ensure_quick_replies(rows, language, user):
        made = 0
        next_position = (QuickReply.objects.order_by('-position').values_list('position', flat=True).first() or 0) + 1
        for row in rows:
            if QuickReply.objects.filter(title__iexact=row['title']).exists():
                continue
            QuickReply.objects.create(
                title=row['title'],
                message=pick_text(row['message'], language),
                platforms=[],
                category='Onboarding',
                position=next_position,
                created_by=user,
            )
            next_position += 1
            made += 1
        return made

    def _apply_ai(self, tenant, ai):
        settings = AICompanionSettings.objects.first()
        ai_created = 0
        if settings is None:
            settings = AICompanionSettings()
            ai_created = 1
        settings.is_enabled = bool(ai.get('enabled'))
        if 'company_description' in ai:
            settings.guidance_prompt = ai['company_description'] or ''
        if ai.get('language'):
            settings.language = ai['language']
        settings.save()

        count = 0
        for platform, account_id in self._replyable_channels(tenant):
            AICompanionChannel.objects.update_or_create(
                platform=platform, account_id=account_id, defaults={'enabled': settings.is_enabled},
            )
            count += 1
        return ai_created, ['ai_is_enabled', 'ai_guidance_prompt', 'ai_language'], count

    # -- connections --

    @staticmethod
    def _widget_connections(tenant):
        try:
            from widget_registry.models import WidgetConnection
            return list(
                WidgetConnection.objects.filter(tenant_schema=tenant.schema_name)
                .order_by('created_at').only('id', 'label', 'is_active', 'created_at')
            )
        except Exception:   # noqa: BLE001 — the registry is a public-schema app; never break the wizard
            logger.exception('social onboarding: widget connections unavailable')
            return []

    def _replyable_channels(self, tenant):
        """``(platform, account_id)`` of every connected channel the AI can
        answer on, with the ids the webhooks pass to ``schedule_ai_reply``."""
        pairs = [
            ('facebook', p.page_id) for p in FacebookPageConnection.objects.filter(is_active=True)
        ] + [
            ('instagram', a.instagram_account_id) for a in InstagramAccountConnection.objects.filter(is_active=True)
        ] + [
            ('whatsapp', w.waba_id) for w in WhatsAppBusinessAccount.objects.filter(is_active=True)
        ] + [
            ('telegram', str(t.telegram_user_id)) for t in TelegramAccount.objects.filter(is_active=True)
        ] + [
            ('widget', str(w.id)) for w in self._widget_connections(tenant) if w.is_active
        ]
        return pairs

    def _connections(self, tenant):
        telegram = TelegramAccount.objects.filter(is_active=True).order_by('-created_at').first()
        return {
            'facebook': [
                {'id': p.id, 'page_id': p.page_id, 'name': p.page_name, 'is_active': p.is_active}
                for p in FacebookPageConnection.objects.order_by('page_name')
            ],
            'instagram': [
                {'id': a.id, 'account_id': a.instagram_account_id, 'name': a.username, 'is_active': a.is_active}
                for a in InstagramAccountConnection.objects.order_by('username')
            ],
            'whatsapp': [
                {
                    'id': w.id, 'waba_id': w.waba_id, 'name': w.business_name,
                    'phone_number': w.display_phone_number or w.phone_number, 'is_active': w.is_active,
                }
                for w in WhatsAppBusinessAccount.objects.order_by('business_name')
            ],
            'telegram': (
                {
                    'id': telegram.id,
                    'name': ' '.join(filter(None, [telegram.first_name, telegram.last_name])) or telegram.username,
                    'phone_number': telegram.phone_number,
                    'username': telegram.username,
                    'is_active': telegram.is_active,
                }
                if telegram else None
            ),
            'email': [
                {'id': e.id, 'email': e.email_address, 'name': e.display_name, 'is_active': e.is_active}
                for e in EmailConnection.objects.order_by('email_address')
            ],
            'widget': [
                {'id': w.id, 'name': w.label, 'is_active': w.is_active}
                for w in self._widget_connections(tenant)
            ],
        }
