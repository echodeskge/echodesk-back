"""
Online shop step (key ``ecommerce_crm``, order 40).

Answers schema (every key optional; only the keys present are applied)::

    {
      "store": {"name": str, "email": str, "phone": str},
      "categories": [{"ka": str, "en": str}],
      "payment": {"cash_on_delivery": bool, "card": bool, "provider": "bog"|"tbc"|"flitt"|null,
                  "bog_client_id", "bog_client_secret",
                  "tbc_client_id", "tbc_client_secret", "tbc_api_key",
                  "flitt_merchant_id", "flitt_password"},
      "delivery": {"allow_pickup": bool, "pickup_address": str,
                   "methods": [{"name": I18n, "price": "5.00",
                                "free_shipping_threshold": "100.00"|null, "estimated_days": int}],
                   "quickshipper": {"enabled": bool, "api_key": str|null, "use_production": bool,
                                    "pickup": {"contact_name", "phone", "address", "city",
                                               "latitude", "longitude", "extra_instructions"}} | null},
      "tax": {"rate": "18.00", "inclusive": bool, "label": "VAT"},
      "look": {"template": "classic"|"voltage", "preset": str, "color_mode": "light"|"dark",
               "primary_color": str|null},
      "analytics": {"google_analytics_id", "google_ads_conversion_id",
                    "google_ads_purchase_label", "clarity_project_id"},
      "deploy_now": bool
    }

``apply`` writes the tenant's ``EcommerceSettings`` row (created when
missing): ``store`` → ``store_*``; ``payment`` → ``active_payment_providers``
plus the legacy ``enable_cash_on_delivery`` / ``enable_card_payment`` flags
computed the way the settings page does (card = any card provider active,
cash = "cash" active), ``ecommerce_payment_provider`` and the provider
credentials (secrets go through the model's encrypted setters, blank keeps
the stored one); ``delivery`` → ``allow_pickup`` (pickup address lands in
the Quickshipper pickup address, which is what the storefront shows),
``ShippingMethod`` rows matched by their en/ka name, Quickshipper fields;
``tax`` → ``tax_rate`` / ``tax_inclusive`` / ``tax_label``; ``look`` →
``storefront_template`` + the Voltage axes or the classic ``theme_*``;
``analytics`` → the tracking ids; ``categories`` → ``tickets.ItemList``
"Categories" (public) + one ``ListItem`` per category (matched by label in
either language) and, when the storefront has no homepage sections yet, the
classic hero banner + a category grid pointing at that list; ``deploy_now``
→ the same ``deploy_tenant_frontend`` service the ``deploy-frontend`` action
uses (skipped with a note when already deployed or when Vercel is not
configured). Idempotent: nothing is created twice.

``context``: ``industry`` (``TenantOnboarding.industry``), ``deployment``
{status, url}, ``languages``, ``existing``
{products, shipping_methods, categories, homepage_sections}, ``presets``
(category presets per industry, ka + en), ``templates`` / ``voltage_presets``
/ ``classic_presets``, ``vercel_configured``. ``seed_defaults``: make sure the
settings row exists with the tenant's name / admin e-mail.
"""
import logging
from decimal import Decimal

from django.conf import settings as django_settings
from rest_framework import serializers

from onboarding.base import OnboardingStep, empty_result
from onboarding.registry import register
from tickets.models import ItemList, ListItem

from .models import EcommerceSettings, HomepageSection, Product, ShippingMethod

logger = logging.getLogger(__name__)

CARD_PROVIDERS = ['bog', 'tbc', 'flitt', 'paddle']   # mirrors settings/ecommerce/_lib/settings.ts
WIZARD_PROVIDERS = ['bog', 'tbc', 'flitt']
LANGUAGES = ['ka', 'en']
CATEGORIES_LIST_TITLES = {'ka': 'კატეგორიები', 'en': 'Categories'}

TEMPLATES = [key for key, _ in EcommerceSettings.STOREFRONT_TEMPLATE_CHOICES]
VOLTAGE_PRESETS = [key for key, _ in EcommerceSettings.VOLTAGE_THEME_CHOICES]
VOLTAGE_MODES = [key for key, _ in EcommerceSettings.VOLTAGE_MODE_CHOICES]
CLASSIC_PRESETS = [key for key, _ in EcommerceSettings._meta.get_field('theme_preset').choices]

# Category presets per industry (``onboarding.models.INDUSTRY_KEYS``), ka + en.
CATEGORY_PRESETS = {
    'shop': [
        {'ka': 'ელექტრონიკა', 'en': 'Electronics'},
        {'ka': 'ტანსაცმელი', 'en': 'Clothing'},
        {'ka': 'ფეხსაცმელი', 'en': 'Shoes'},
        {'ka': 'სახლი და ბაღი', 'en': 'Home & Garden'},
        {'ka': 'სილამაზე', 'en': 'Beauty'},
        {'ka': 'ბავშვები', 'en': 'Kids'},
        {'ka': 'სპორტი', 'en': 'Sport'},
        {'ka': 'ფასდაკლება', 'en': 'Sale'},
    ],
    'salon': [
        {'ka': 'თმის მოვლა', 'en': 'Hair care'},
        {'ka': 'კანის მოვლა', 'en': 'Skin care'},
        {'ka': 'ფრჩხილები', 'en': 'Nails'},
        {'ka': 'კოსმეტიკა', 'en': 'Cosmetics'},
        {'ka': 'სუნამოები', 'en': 'Perfume'},
        {'ka': 'სასაჩუქრე ნაკრებები', 'en': 'Gift sets'},
    ],
    'clinic': [
        {'ka': 'ვიტამინები', 'en': 'Vitamins'},
        {'ka': 'სამედიცინო მოწყობილობები', 'en': 'Medical devices'},
        {'ka': 'პირადი ჰიგიენა', 'en': 'Personal care'},
        {'ka': 'საკვები დანამატები', 'en': 'Supplements'},
        {'ka': 'დედა და ბავშვი', 'en': 'Mother & baby'},
    ],
    'real_estate': [
        {'ka': 'ბინები', 'en': 'Apartments'},
        {'ka': 'სახლები', 'en': 'Houses'},
        {'ka': 'კომერციული ფართი', 'en': 'Commercial'},
        {'ka': 'მიწის ნაკვეთები', 'en': 'Land'},
        {'ka': 'ქირავდება', 'en': 'For rent'},
    ],
    'call_centre': [
        {'ka': 'სერვისები', 'en': 'Services'},
        {'ka': 'პაკეტები', 'en': 'Packages'},
        {'ka': 'აღჭურვილობა', 'en': 'Equipment'},
    ],
    'logistics': [
        {'ka': 'მიწოდება', 'en': 'Delivery'},
        {'ka': 'შეფუთვა', 'en': 'Packaging'},
        {'ka': 'საწყობი', 'en': 'Storage'},
        {'ka': 'ექსპრეს მიწოდება', 'en': 'Express'},
    ],
    'services': [
        {'ka': 'სერვისები', 'en': 'Services'},
        {'ka': 'პაკეტები', 'en': 'Packages'},
        {'ka': 'კონსულტაციები', 'en': 'Consultations'},
        {'ka': 'სასაჩუქრე ბარათები', 'en': 'Gift cards'},
    ],
    'other': [
        {'ka': 'ახალი', 'en': 'New'},
        {'ka': 'პოპულარული', 'en': 'Popular'},
        {'ka': 'ფასდაკლება', 'en': 'Sale'},
        {'ka': 'აქსესუარები', 'en': 'Accessories'},
    ],
}


def _clean(value):
    return ' '.join(str(value or '').split())


def _opt_char(max_length=255, **kwargs):
    return serializers.CharField(max_length=max_length, required=False, allow_blank=True, **kwargs)


class I18nField(serializers.DictField):
    """``{"ka": str, "en": str}`` — at least one language non-empty."""

    child = serializers.CharField(allow_blank=True, max_length=255)

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        value = {lang: _clean(data.get(lang)) for lang in LANGUAGES}
        if not any(value.values()):
            raise serializers.ValidationError('A name in at least one language is required.')
        return value


class StoreSerializer(serializers.Serializer):
    name = _opt_char()
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = _opt_char(50)


class PaymentSerializer(serializers.Serializer):
    cash_on_delivery = serializers.BooleanField(required=False)
    card = serializers.BooleanField(required=False)
    provider = serializers.ChoiceField(choices=WIZARD_PROVIDERS, required=False, allow_null=True)
    bog_client_id = _opt_char()
    bog_client_secret = _opt_char(500)
    tbc_client_id = _opt_char()
    tbc_client_secret = _opt_char(500)
    tbc_api_key = _opt_char(500)
    flitt_merchant_id = _opt_char()
    flitt_password = _opt_char(500)

    def validate(self, attrs):
        if attrs.get('card') and not attrs.get('provider'):
            raise serializers.ValidationError({'provider': 'Pick a card payment provider.'})
        return attrs


class ShippingMethodSerializer(serializers.Serializer):
    name = I18nField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0'), required=False, default=Decimal('0'))
    free_shipping_threshold = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal('0'), required=False, allow_null=True, default=None,
    )
    estimated_days = serializers.IntegerField(min_value=0, max_value=365, required=False, default=3)


class QuickshipperPickupSerializer(serializers.Serializer):
    contact_name = _opt_char(200)
    phone = _opt_char(50)
    address = _opt_char(500)
    city = _opt_char(100)
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True)
    extra_instructions = serializers.CharField(required=False, allow_blank=True)


class QuickshipperSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
    api_key = _opt_char(500, allow_null=True)
    use_production = serializers.BooleanField(required=False)
    pickup = QuickshipperPickupSerializer(required=False)


class DeliverySerializer(serializers.Serializer):
    allow_pickup = serializers.BooleanField(required=False)
    pickup_address = _opt_char(500)
    methods = ShippingMethodSerializer(many=True, required=False)
    quickshipper = QuickshipperSerializer(required=False, allow_null=True)


class TaxSerializer(serializers.Serializer):
    rate = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'), required=False)
    inclusive = serializers.BooleanField(required=False)
    label = _opt_char(50)


class LookSerializer(serializers.Serializer):
    template = serializers.ChoiceField(choices=TEMPLATES, required=False)
    preset = _opt_char(50)
    color_mode = serializers.ChoiceField(choices=VOLTAGE_MODES, required=False)
    primary_color = _opt_char(50, allow_null=True)

    def validate(self, attrs):
        template = attrs.get('template')
        preset = attrs.get('preset')
        if preset:
            allowed = VOLTAGE_PRESETS if template == 'voltage' else CLASSIC_PRESETS
            if template and preset not in allowed:
                raise serializers.ValidationError({'preset': f'Unknown preset for the {template} template.'})
        return attrs


class AnalyticsSerializer(serializers.Serializer):
    google_analytics_id = _opt_char(50)
    google_ads_conversion_id = _opt_char(50)
    google_ads_purchase_label = _opt_char(100)
    clarity_project_id = _opt_char(50)


class ShopAnswersSerializer(serializers.Serializer):
    store = StoreSerializer(required=False)
    categories = serializers.ListField(child=I18nField(), required=False)
    payment = PaymentSerializer(required=False)
    delivery = DeliverySerializer(required=False)
    tax = TaxSerializer(required=False)
    look = LookSerializer(required=False)
    analytics = AnalyticsSerializer(required=False)
    deploy_now = serializers.BooleanField(required=False)

    def validate_categories(self, value):
        seen, cleaned = set(), []
        for row in value:
            key = (row.get('ka', '').lower(), row.get('en', '').lower())
            if key not in seen:
                seen.add(key)
                cleaned.append(row)
        return cleaned


def _set(instance, field, value, changed):
    if getattr(instance, field) != value:
        setattr(instance, field, value)
        changed.append(field)


def _get_settings(tenant):
    return EcommerceSettings.objects.get_or_create(
        tenant=tenant,
        defaults={'store_name': tenant.name or '', 'store_email': getattr(tenant, 'admin_email', '') or ''},
    )


def vercel_configured():
    return bool(
        getattr(django_settings, 'VERCEL_TOKEN', '')
        and getattr(django_settings, 'VERCEL_SHARED_ECOMMERCE_PROJECT_ID', '')
    )


def _names_match(stored, wanted):
    """``ShippingMethod.name`` / category names: equal in any shared language."""
    stored = stored if isinstance(stored, dict) else {}
    for lang in LANGUAGES:
        a, b = _clean(stored.get(lang)).lower(), _clean(wanted.get(lang)).lower()
        if a and b and a == b:
            return True
    return False


def _label_for(tenant, name):
    lang = (getattr(tenant, 'preferred_language', '') or 'ka').lower()
    return name.get(lang) or name.get('ka') or name.get('en')


@register
class ShopStep(OnboardingStep):
    key = 'ecommerce_crm'
    feature_key = 'ecommerce_crm'
    order = 40
    serializer_class = ShopAnswersSerializer

    # ── reads ──

    def context(self, tenant, request):
        from onboarding.models import TenantOnboarding

        s = EcommerceSettings.objects.filter(tenant=tenant).first()
        categories = self._categories_list()
        return {
            'industry': TenantOnboarding.get().industry,
            'deployment': {
                'status': s.deployment_status if s else 'pending',
                'url': (s.ecommerce_frontend_url or None) if s else None,
            },
            'languages': list(LANGUAGES),
            'existing': {
                'products': Product.objects.count(),
                'shipping_methods': ShippingMethod.objects.count(),
                'categories': categories.items.count() if categories else 0,
                'homepage_sections': HomepageSection.objects.count(),
            },
            'presets': CATEGORY_PRESETS,
            'templates': list(TEMPLATES),
            'voltage_presets': list(VOLTAGE_PRESETS),
            'classic_presets': list(CLASSIC_PRESETS),
            'vercel_configured': vercel_configured(),
        }

    def prefill(self, tenant, state):
        s = EcommerceSettings.objects.filter(tenant=tenant).first()
        active = list(s.active_payment_providers or []) if s else []
        provider = next((p for p in active if p in WIZARD_PROVIDERS), None)
        categories_list = self._categories_list()
        if categories_list is not None and categories_list.items.exists():
            categories = [
                item.custom_data.get('name') if isinstance(item.custom_data, dict) and item.custom_data.get('name')
                else {'ka': item.label, 'en': item.label}
                for item in categories_list.items.filter(is_active=True).order_by('position', 'id')
            ]
        else:
            categories = CATEGORY_PRESETS.get(state.industry or 'other', CATEGORY_PRESETS['other'])
        methods = [
            {
                'name': {lang: (m.name or {}).get(lang, '') for lang in LANGUAGES} if isinstance(m.name, dict) else {'ka': '', 'en': str(m.name)},
                'price': str(m.price),
                'free_shipping_threshold': str(m.free_shipping_threshold) if m.free_shipping_threshold is not None else None,
                'estimated_days': m.estimated_days,
            }
            for m in ShippingMethod.objects.filter(is_active=True).order_by('position', 'id')
        ]
        return {
            'store': {
                'name': (s.store_name if s and s.store_name else '') or (tenant.name if tenant else ''),
                'email': (s.store_email if s and s.store_email else '') or (getattr(tenant, 'admin_email', '') or ''),
                'phone': s.store_phone if s else '',
            },
            'categories': categories,
            'payment': {
                'cash_on_delivery': ('cash' in active) if s else True,
                'card': any(p in CARD_PROVIDERS for p in active) if s else False,
                'provider': provider or (s.ecommerce_payment_provider if s and s.ecommerce_payment_provider in WIZARD_PROVIDERS else None),
                'bog_client_id': s.bog_client_id if s else '',
                'tbc_client_id': s.tbc_client_id if s else '',
                'tbc_api_key': s.tbc_api_key if s else '',
                'flitt_merchant_id': s.flitt_merchant_id if s else '',
            },
            'delivery': {
                'allow_pickup': s.allow_pickup if s else False,
                'pickup_address': s.quickshipper_pickup_address if s else '',
                'methods': methods,
                'quickshipper': {
                    'enabled': s.quickshipper_enabled,
                    'api_key': None,
                    'use_production': s.quickshipper_use_production,
                    'pickup': {
                        'contact_name': s.quickshipper_pickup_contact_name,
                        'phone': s.quickshipper_pickup_phone,
                        'address': s.quickshipper_pickup_address,
                        'city': s.quickshipper_pickup_city,
                        'latitude': str(s.quickshipper_pickup_latitude) if s.quickshipper_pickup_latitude is not None else None,
                        'longitude': str(s.quickshipper_pickup_longitude) if s.quickshipper_pickup_longitude is not None else None,
                        'extra_instructions': s.quickshipper_pickup_extra_instructions,
                    },
                } if s else None,
            },
            'tax': {
                'rate': str(s.tax_rate) if s else '18.00',
                'inclusive': s.tax_inclusive if s else True,
                'label': (s.tax_label if s else '') or 'VAT',
            },
            'look': {
                'template': s.storefront_template if s else 'classic',
                'preset': (s.voltage_theme_preset if s.storefront_template == 'voltage' else s.theme_preset) if s else 'default',
                'color_mode': s.voltage_color_mode if s else 'light',
                'primary_color': (s.theme_primary_color or None) if s else None,
            },
            'analytics': {
                'google_analytics_id': s.google_analytics_id if s else '',
                'google_ads_conversion_id': s.google_ads_conversion_id if s else '',
                'google_ads_purchase_label': s.google_ads_purchase_label if s else '',
                'clarity_project_id': s.clarity_project_id if s else '',
            },
            'deploy_now': not (s and s.deployment_status == 'deployed'),
        }

    # ── writes ──

    def apply(self, tenant, answers, user):
        result = empty_result()
        notes = result['notes']
        changed = []
        s, created = _get_settings(tenant)
        if created:
            result['created']['ecommerce_settings'] = 1

        store = answers.get('store', {})
        for key in ('name', 'email', 'phone'):
            if key in store:
                _set(s, f'store_{key}', store[key] or '', changed)

        if 'payment' in answers:
            self._apply_payment(s, answers['payment'], changed)

        if 'delivery' in answers:
            self._apply_delivery(s, answers['delivery'], changed, result)

        tax = answers.get('tax', {})
        if 'rate' in tax:
            _set(s, 'tax_rate', tax['rate'], changed)
        if 'inclusive' in tax:
            _set(s, 'tax_inclusive', tax['inclusive'], changed)
        if 'label' in tax and tax['label']:
            _set(s, 'tax_label', tax['label'], changed)

        if 'look' in answers:
            self._apply_look(s, answers['look'], changed)

        analytics = answers.get('analytics', {})
        for key in ('google_analytics_id', 'google_ads_conversion_id', 'google_ads_purchase_label', 'clarity_project_id'):
            if key in analytics:
                _set(s, key, analytics[key] or '', changed)

        if 'categories' in answers:
            self._apply_categories(tenant, answers['categories'], user, s, changed, result)

        if created or changed:
            s.save()

        if answers.get('deploy_now'):
            self._deploy(tenant, s, result)

        result['updated'] = changed
        return result

    def seed_defaults(self, tenant):
        s, _ = _get_settings(tenant)
        updates = []
        if not s.store_name and tenant.name:
            s.store_name = tenant.name
            updates.append('store_name')
        if not s.store_email and getattr(tenant, 'admin_email', ''):
            s.store_email = tenant.admin_email
            updates.append('store_email')
        if updates:
            s.save(update_fields=updates + ['updated_at'])

    # ── helpers ──

    @staticmethod
    def _apply_payment(s, payment, changed):
        active = [p for p in (s.active_payment_providers or []) if isinstance(p, str)]
        if 'cash_on_delivery' in payment:
            if payment['cash_on_delivery'] and 'cash' not in active:
                active.append('cash')
            elif not payment['cash_on_delivery']:
                active = [p for p in active if p != 'cash']
        if 'card' in payment:
            provider = payment.get('provider')
            if payment['card'] and provider:
                # One wizard provider at a time; other card providers stay as they were
                # only if they were not chosen here (the page toggles them one by one).
                active = [p for p in active if p not in WIZARD_PROVIDERS or p == provider]
                if provider not in active:
                    active.append(provider)
                _set(s, 'ecommerce_payment_provider', provider, changed)
            elif not payment['card']:
                active = [p for p in active if p not in CARD_PROVIDERS]
        elif payment.get('provider'):
            _set(s, 'ecommerce_payment_provider', payment['provider'], changed)
        _set(s, 'active_payment_providers', active, changed)
        # Legacy flags, computed like settings/ecommerce/_lib/settings.ts legacyPaymentFlags().
        _set(s, 'enable_card_payment', any(p in CARD_PROVIDERS for p in active), changed)
        _set(s, 'enable_cash_on_delivery', 'cash' in active, changed)

        for field in ('bog_client_id', 'tbc_client_id', 'tbc_api_key', 'flitt_merchant_id'):
            if field in payment and payment[field]:
                _set(s, field, payment[field], changed)
        if payment.get('bog_client_secret'):
            s.set_bog_secret(payment['bog_client_secret'])
            changed.append('bog_client_secret')
        if payment.get('tbc_client_secret'):
            s.set_tbc_secret(payment['tbc_client_secret'])
            changed.append('tbc_client_secret')
        if payment.get('flitt_password'):
            s.set_flitt_password(payment['flitt_password'])
            changed.append('flitt_password')

    @staticmethod
    def _apply_delivery(s, delivery, changed, result):
        if 'allow_pickup' in delivery:
            _set(s, 'allow_pickup', delivery['allow_pickup'], changed)
        qs_pickup_address = ((delivery.get('quickshipper') or {}).get('pickup') or {}).get('address')
        if delivery.get('pickup_address') and not qs_pickup_address:
            # The storefront reads the pickup address from the Quickshipper pickup fields.
            _set(s, 'quickshipper_pickup_address', delivery['pickup_address'], changed)

        created_methods = 0
        existing = list(ShippingMethod.objects.all())
        position = max((m.position for m in existing), default=-1) + 1
        for row in delivery.get('methods') or []:
            match = next((m for m in existing if _names_match(m.name, row['name'])), None)
            if match is not None:
                updates = []
                if match.price != row['price']:
                    match.price = row['price']
                    updates.append('price')
                if match.free_shipping_threshold != row['free_shipping_threshold']:
                    match.free_shipping_threshold = row['free_shipping_threshold']
                    updates.append('free_shipping_threshold')
                if match.estimated_days != row['estimated_days']:
                    match.estimated_days = row['estimated_days']
                    updates.append('estimated_days')
                if not match.is_active:
                    match.is_active = True
                    updates.append('is_active')
                if updates:
                    match.save(update_fields=updates)
                    changed.append('shipping_methods')
                continue
            method = ShippingMethod.objects.create(
                name=dict(row['name']),
                description={},
                price=row['price'],
                free_shipping_threshold=row['free_shipping_threshold'],
                estimated_days=row['estimated_days'],
                is_active=True,
                position=position,
            )
            existing.append(method)
            position += 1
            created_methods += 1
        if created_methods:
            result['created']['shipping_methods'] = created_methods

        qs = delivery.get('quickshipper')
        if qs:
            if 'enabled' in qs:
                _set(s, 'quickshipper_enabled', qs['enabled'], changed)
            if 'use_production' in qs:
                _set(s, 'quickshipper_use_production', qs['use_production'], changed)
            if qs.get('api_key'):
                s.set_quickshipper_api_key(qs['api_key'])
                changed.append('quickshipper_api_key')
            pickup = qs.get('pickup') or {}
            for key in ('contact_name', 'phone', 'address', 'city', 'latitude', 'longitude', 'extra_instructions'):
                if key in pickup:
                    _set(s, f'quickshipper_pickup_{key}', pickup[key] if key in ('latitude', 'longitude') else (pickup[key] or ''), changed)

    @staticmethod
    def _apply_look(s, look, changed):
        template = look.get('template')
        if template:
            _set(s, 'storefront_template', template, changed)
        template = template or s.storefront_template
        preset = look.get('preset')
        if preset:
            if template == 'voltage' and preset in VOLTAGE_PRESETS:
                _set(s, 'voltage_theme_preset', preset, changed)
            elif template == 'classic' and preset in CLASSIC_PRESETS:
                _set(s, 'theme_preset', preset, changed)
        if 'color_mode' in look:
            _set(s, 'voltage_color_mode', look['color_mode'], changed)
        if look.get('primary_color'):
            _set(s, 'theme_primary_color', look['primary_color'], changed)
            if template == 'classic' and s.theme_preset != 'custom' and not preset:
                _set(s, 'theme_preset', 'custom', changed)

    @staticmethod
    def _categories_list():
        return (
            ItemList.objects.filter(title__in=list(CATEGORIES_LIST_TITLES.values()), parent_list__isnull=True)
            .order_by('-is_active', 'id').first()
        )

    def _apply_categories(self, tenant, categories, user, s, changed, result):
        if not categories:
            return
        itemlist = self._categories_list()
        if itemlist is None:
            lang = (getattr(tenant, 'preferred_language', '') or 'ka').lower()
            itemlist = ItemList.objects.create(
                title=CATEGORIES_LIST_TITLES.get(lang, CATEGORIES_LIST_TITLES['en']),
                description='Product categories',
                is_active=True,
                is_public=True,
                created_by=user,
            )
            result['created']['itemlists'] = 1
        elif not itemlist.is_public or not itemlist.is_active:
            itemlist.is_public = True
            itemlist.is_active = True
            itemlist.save(update_fields=['is_public', 'is_active', 'updated_at'])

        existing = list(itemlist.items.filter(parent__isnull=True))

        def matches(item, name):
            names = {}
            if isinstance(item.custom_data, dict) and isinstance(item.custom_data.get('name'), dict):
                names = item.custom_data['name']
            if _names_match(names, name):
                return True
            label = _clean(item.label).lower()
            return bool(label) and label in {name.get('ka', '').lower(), name.get('en', '').lower()}

        created_items = 0
        position = max((i.position for i in existing), default=0) + 1
        for name in categories:
            if any(matches(item, name) for item in existing):
                continue
            item = ListItem.objects.create(
                item_list=itemlist,
                label=_label_for(tenant, name),
                custom_data={'name': dict(name)},
                position=position,
                is_active=True,
                created_by=user,
            )
            existing.append(item)
            position += 1
            created_items += 1
        if created_items:
            result['created']['categories'] = created_items

        if not HomepageSection.objects.exists():
            from .homepage_variants import HOMEPAGE_VARIANTS
            created_sections = 0
            for section in HOMEPAGE_VARIANTS['classic']['sections']:
                if section['section_type'] not in ('hero_banner', 'category_grid'):
                    continue
                config = {k: (dict(v) if isinstance(v, dict) else v) for k, v in section.items()}
                if config['section_type'] == 'category_grid':
                    config['item_list'] = itemlist
                HomepageSection.objects.create(**config)
                created_sections += 1
            if created_sections:
                result['created']['homepage_sections'] = created_sections
                if not s.homepage_variant:
                    _set(s, 'homepage_variant', 'classic', changed)
        else:
            grid = HomepageSection.objects.filter(section_type='category_grid', item_list__isnull=True).first()
            if grid is not None:
                grid.item_list = itemlist
                grid.save(update_fields=['item_list', 'updated_at'])
                changed.append('homepage_sections')

    @staticmethod
    def _deploy(tenant, s, result):
        """The ``deploy-frontend`` action's flow, minus the HTTP layer."""
        if s.deployment_status == 'deployed' and s.ecommerce_frontend_url:
            result['notes'].append(f'Storefront already deployed at {s.ecommerce_frontend_url}.')
            return
        if s.deployment_status == 'deploying':
            result['notes'].append('A storefront deployment is already in progress.')
            return
        if not vercel_configured():
            result['notes'].append('Storefront deployment skipped: Vercel is not configured on this server.')
            return

        from tenant_schemas.utils import get_public_schema_name, schema_context
        from tenants.models import Tenant
        from .services.vercel_deployment import deploy_tenant_frontend

        s.deployment_status = 'deploying'
        s.save(update_fields=['deployment_status'])
        try:
            with schema_context(get_public_schema_name()):
                tenant_obj = Tenant.objects.get(id=tenant.id)
                outcome = deploy_tenant_frontend(tenant_obj)
        except Exception as exc:  # noqa: BLE001 - a deploy failure must not undo the rest of the step
            logger.exception('Onboarding storefront deployment failed for %s', tenant.schema_name)
            outcome = {'success': False, 'error': str(exc)}

        if outcome.get('success'):
            s.vercel_project_id = outcome.get('project_id')
            s.ecommerce_frontend_url = outcome.get('url')
            s.deployment_status = 'deployed'
            s.save(update_fields=['vercel_project_id', 'ecommerce_frontend_url', 'deployment_status'])
            result['created']['deployment'] = 1
            result['notes'].append(f'Storefront deployed at {s.ecommerce_frontend_url}.')
        else:
            s.deployment_status = 'failed'
            s.save(update_fields=['deployment_status'])
            result['notes'].append(f"Storefront deployment failed: {outcome.get('error') or outcome.get('message') or 'unknown error'}")

