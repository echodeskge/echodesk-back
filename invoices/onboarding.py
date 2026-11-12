"""
Invoices step (key ``invoice_management``, order 50).

Answers schema (every key optional; only the keys present are applied)::

    {
      "company": {"name", "tax_id", "registration_number", "address", "phone", "email", "website"},
      "defaults": {"vat_payer": bool, "default_tax_rate": "18.00",
                   "default_currency": "GEL" | "USD" | "EUR",
                   "default_due_days": int, "invoice_prefix": str, "starting_number": int},
      "bank_accounts": [{"bank_name", "account_number", "iban", "swift", "is_default"}],
      "email": {"email_from", "email_from_name", "email_cc", "email_subject_template",
                "email_message_template", "footer_text", "default_terms"},
      "clients_source": "ecommerce" | "itemlist" | "none"
    }

``apply`` writes the single ``InvoiceSettings`` row (created when missing):
``company`` → ``company_name`` + the other company fields, ``defaults`` →
numbering / currency / due days (``vat_payer`` false forces
``default_tax_rate`` 0, true without a rate means 18), ``bank_accounts`` →
the JSON list (validated like the settings serializer: ``bank_name`` and
``account_number`` are required; exactly one account is the default),
``email`` → the sending fields, ``clients_source`` → ``client_itemlist``
(``itemlist``: get-or-create a ``tickets.ItemList`` titled "Clients" /
"კლიენტები"; ``ecommerce``: leave it empty — invoice clients come from the
shop; ``none``: untouched). Idempotent: a second apply with the same answers
creates nothing.

``context``: ``prefill_sources`` (tenant name, language, booking bank fields
when ``BookingSettings`` has them), ``language``, ``has_logo`` /
``tenant_has_logo``, ``next_number_preview`` (the model's own numbering),
``itemlists``, ``email_placeholders`` (the names the invoice mailer
substitutes). ``prefill.email`` carries ready-made subject / body / footer /
terms templates in the tenant language when none are stored; templates sent
with the web's ``{total_amount}`` / ``{due_days}`` placeholders are rewritten
to what ``invoices.tasks`` substitutes (``{total}``; ``{due_days}`` becomes
the due-days number).
``seed_defaults``: make sure the row exists with ``company_name`` = tenant
name. Logo / seal / signature uploads stay on the existing upload actions.
"""
from decimal import Decimal

from django.apps import apps
from rest_framework import serializers

from onboarding.base import OnboardingStep, empty_result
from onboarding.registry import register
from tickets.models import ItemList

from .models import InvoiceSettings

CURRENCIES = ['GEL', 'USD', 'EUR']
CLIENT_SOURCES = ['ecommerce', 'itemlist', 'none']
CLIENTS_LIST_TITLES = {'ka': 'კლიენტები', 'en': 'Clients'}
DEFAULT_VAT_RATE = Decimal('18.00')


def _clean(value):
    return ' '.join(str(value or '').split())


def _opt_char(max_length=255, **kwargs):
    return serializers.CharField(max_length=max_length, required=False, allow_blank=True, **kwargs)


class CompanySerializer(serializers.Serializer):
    name = _opt_char()
    tax_id = _opt_char(50)
    registration_number = _opt_char(50)
    address = serializers.CharField(required=False, allow_blank=True)
    phone = _opt_char(50)
    email = serializers.EmailField(required=False, allow_blank=True)
    website = _opt_char(200)

    def validate_website(self, value):
        value = _clean(value)
        if value and '://' not in value:
            value = f'https://{value}'
        return value


class DefaultsSerializer(serializers.Serializer):
    vat_payer = serializers.BooleanField(required=False)
    default_tax_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'), required=False,
    )
    default_currency = serializers.ChoiceField(choices=CURRENCIES, required=False)
    default_due_days = serializers.IntegerField(min_value=0, max_value=365, required=False)
    invoice_prefix = serializers.CharField(max_length=10, required=False)
    starting_number = serializers.IntegerField(min_value=1, required=False)

    def validate_invoice_prefix(self, value):
        value = _clean(value).upper()
        if not value:
            raise serializers.ValidationError('The invoice prefix cannot be blank.')
        return value

    def validate(self, attrs):
        # ``vat_payer`` wins over the rate: a non-payer always gets 0 %, a payer
        # without an explicit rate gets Georgia's 18 %.
        if 'vat_payer' in attrs:
            if not attrs['vat_payer']:
                attrs['default_tax_rate'] = Decimal('0.00')
            elif 'default_tax_rate' not in attrs or attrs['default_tax_rate'] == 0:
                attrs['default_tax_rate'] = DEFAULT_VAT_RATE
        return attrs


class BankAccountSerializer(serializers.Serializer):
    bank_name = serializers.CharField(max_length=255)
    account_number = serializers.CharField(max_length=100)
    iban = _opt_char(100)
    swift = _opt_char(50)
    is_default = serializers.BooleanField(required=False, default=False)

    def validate_bank_name(self, value):
        value = _clean(value)
        if not value:
            raise serializers.ValidationError('Each bank account needs a bank name.')
        return value

    def validate_account_number(self, value):
        value = _clean(value)
        if not value:
            raise serializers.ValidationError('Each bank account needs an account number.')
        return value


class EmailSerializer(serializers.Serializer):
    email_from = serializers.EmailField(required=False, allow_blank=True)
    email_from_name = _opt_char(100)
    email_cc = serializers.CharField(required=False, allow_blank=True)
    email_subject_template = _opt_char(255)
    email_message_template = serializers.CharField(required=False, allow_blank=True)
    footer_text = serializers.CharField(required=False, allow_blank=True)
    default_terms = serializers.CharField(required=False, allow_blank=True)


class InvoicesAnswersSerializer(serializers.Serializer):
    company = CompanySerializer(required=False)
    defaults = DefaultsSerializer(required=False)
    bank_accounts = BankAccountSerializer(many=True, required=False)
    email = EmailSerializer(required=False)
    clients_source = serializers.ChoiceField(choices=CLIENT_SOURCES, required=False)

    def validate_bank_accounts(self, value):
        defaults = [row for row in value if row.get('is_default')]
        if value and not defaults:
            value[0]['is_default'] = True
        elif len(defaults) > 1:
            for row in defaults[1:]:
                row['is_default'] = False
        return value


# Placeholders ``invoices.tasks.send_invoice_email_task`` substitutes.
EMAIL_PLACEHOLDERS = ['{invoice_number}', '{company_name}', '{client_name}', '{total}', '{due_date}']
# The web's drafts use two names the mailer does not know; rewrite them on apply.
PLACEHOLDER_ALIASES = {'{total_amount}': '{total}'}

EMAIL_TEMPLATES = {
    'ka': {
        'email_subject_template': 'ინვოისი {invoice_number} — {company_name}',
        'email_message_template': (
            'მოგესალმებით, {client_name},\n\n'
            'გიგზავნით ინვოისს {invoice_number} თანხით {total}.\n'
            'გადახდის ვადა: {due_date}.\n\n'
            'გმადლობთ,\n{company_name}'
        ),
        'footer_text': 'გმადლობთ თანამშრომლობისთვის.',
        'default_terms': 'გთხოვთ, გადაიხადოთ ინვოისი მითითებულ ვადაში — {due_days} დღე გამოწერიდან.',
    },
    'en': {
        'email_subject_template': 'Invoice {invoice_number} from {company_name}',
        'email_message_template': (
            'Dear {client_name},\n\n'
            'Please find attached invoice {invoice_number} for {total}.\n'
            'Due date: {due_date}.\n\n'
            'Best regards,\n{company_name}'
        ),
        'footer_text': 'Thank you for your business.',
        'default_terms': 'Payment is due within {due_days} days of the invoice date.',
    },
}


def _language(tenant):
    lang = (getattr(tenant, 'preferred_language', '') or 'ka').lower()
    return 'en' if lang == 'en' else 'ka'


def _email_templates(tenant, due_days):
    out = dict(EMAIL_TEMPLATES[_language(tenant)])
    out['default_terms'] = out['default_terms'].replace('{due_days}', str(due_days))
    return out


def normalize_template(text, due_days):
    """Rewrite the web's placeholder names to the ones the mailer substitutes."""
    if not text:
        return text
    for alias, real in PLACEHOLDER_ALIASES.items():
        text = text.replace(alias, real)
    return text.replace('{due_days}', str(due_days))


COMPANY_FIELDS = {
    'name': 'company_name', 'tax_id': 'tax_id', 'registration_number': 'registration_number',
    'address': 'address', 'phone': 'phone', 'email': 'email', 'website': 'website',
}
DEFAULT_FIELDS = ['default_tax_rate', 'default_currency', 'default_due_days', 'invoice_prefix', 'starting_number']
EMAIL_FIELDS = [
    'email_from', 'email_from_name', 'email_cc', 'email_subject_template',
    'email_message_template', 'footer_text', 'default_terms',
]


def _set(instance, field, value, changed):
    if getattr(instance, field) != value:
        setattr(instance, field, value)
        changed.append(field)


def _booking_bank():
    """Bank fields of ``BookingSettings`` (the bookings module), when filled."""
    if not apps.is_installed('booking_management'):
        return None
    try:
        BookingSettings = apps.get_model('booking_management', 'BookingSettings')
        row = BookingSettings.objects.only('bank_name', 'bank_iban', 'bank_account_holder').first()
    except Exception:  # noqa: BLE001 - optional source, never break the page
        return None
    if row is None or not (row.bank_name or row.bank_iban):
        return None
    return {
        'bank_name': row.bank_name or '',
        'bank_iban': row.bank_iban or '',
        'bank_account_holder': row.bank_account_holder or '',
    }


def _clients_list_title(tenant):
    lang = (getattr(tenant, 'preferred_language', '') or 'ka').lower()
    return CLIENTS_LIST_TITLES.get(lang, CLIENTS_LIST_TITLES['en'])


@register
class InvoicesStep(OnboardingStep):
    key = 'invoice_management'
    feature_key = 'invoice_management'
    order = 50
    serializer_class = InvoicesAnswersSerializer

    # ── reads ──

    def context(self, tenant, request):
        settings_obj = InvoiceSettings.objects.first()
        preview_source = settings_obj or InvoiceSettings()
        return {
            'prefill_sources': {
                'tenant_name': tenant.name if tenant else '',
                'tenant_email': getattr(tenant, 'admin_email', '') or '',
                'language': _language(tenant),
                'booking_bank': _booking_bank(),
            },
            'language': _language(tenant),
            'email_placeholders': list(EMAIL_PLACEHOLDERS),
            'has_logo': bool(settings_obj and settings_obj.logo),
            'tenant_has_logo': bool(tenant and getattr(tenant, 'logo', None)),
            'next_number_preview': preview_source.get_next_invoice_number(),
            'itemlists': [
                {'id': il.id, 'title': il.title}
                for il in ItemList.objects.filter(is_active=True).order_by('title').only('id', 'title')
            ],
            'has_settings': settings_obj is not None,
        }

    def prefill(self, tenant, state):
        s = InvoiceSettings.objects.first()
        booking_bank = _booking_bank()
        company = {
            'name': (s.company_name if s and s.company_name else '') or (tenant.name if tenant else ''),
            'tax_id': s.tax_id if s else '',
            'registration_number': s.registration_number if s else '',
            'address': s.address if s else '',
            'phone': s.phone if s else '',
            'email': (s.email if s and s.email else '') or (getattr(tenant, 'admin_email', '') or ''),
            'website': s.website if s else '',
        }
        tax_rate = s.default_tax_rate if s else Decimal('0.00')
        defaults = {
            'vat_payer': bool(tax_rate and tax_rate > 0),
            'default_tax_rate': str(tax_rate if tax_rate and tax_rate > 0 else DEFAULT_VAT_RATE),
            'default_currency': s.default_currency if s else 'GEL',
            'default_due_days': s.default_due_days if s else 30,
            'invoice_prefix': s.invoice_prefix if s else 'INV',
            'starting_number': s.starting_number if s else 1,
        }
        bank_accounts = list(s.bank_accounts) if s and s.bank_accounts else []
        if not bank_accounts and booking_bank:
            bank_accounts = [{
                'bank_name': booking_bank['bank_name'],
                'account_number': booking_bank['bank_iban'],
                'iban': booking_bank['bank_iban'],
                'swift': '',
                'is_default': True,
            }]
        email = {field: (getattr(s, field) if s else '') for field in EMAIL_FIELDS}
        # Ready-made templates in the tenant language where nothing is stored
        # (the model default subject counts as "nothing").
        model_subject = InvoiceSettings._meta.get_field('email_subject_template').default
        for field, template in _email_templates(tenant, defaults['default_due_days']).items():
            if not email[field] or (field == 'email_subject_template' and email[field] == model_subject):
                email[field] = template
        if not email['email_from_name']:
            email['email_from_name'] = company['name']
        if s is not None and s.client_itemlist_id:
            clients_source = 'itemlist'
        elif apps.is_installed('ecommerce_crm') and _ecommerce_enabled(tenant):
            clients_source = 'ecommerce'
        else:
            clients_source = 'none'
        return {
            'company': company,
            'defaults': defaults,
            'bank_accounts': bank_accounts,
            'email': email,
            'clients_source': clients_source,
        }

    # ── writes ──

    def apply(self, tenant, answers, user):
        result = empty_result()
        changed = []
        settings_obj, created = InvoiceSettings.objects.get_or_create()
        if created:
            result['created']['invoice_settings'] = 1
            if not settings_obj.company_name and tenant:
                settings_obj.company_name = tenant.name or ''

        for key, field in COMPANY_FIELDS.items():
            if key in answers.get('company', {}):
                _set(settings_obj, field, answers['company'][key] or '', changed)

        for field in DEFAULT_FIELDS:
            if field in answers.get('defaults', {}):
                _set(settings_obj, field, answers['defaults'][field], changed)

        if 'bank_accounts' in answers:
            accounts = [dict(row) for row in answers['bank_accounts']]
            _set(settings_obj, 'bank_accounts', accounts, changed)

        for field in EMAIL_FIELDS:
            if field in answers.get('email', {}):
                value = answers['email'][field] or ''
                if field in ('email_subject_template', 'email_message_template', 'footer_text', 'default_terms'):
                    value = normalize_template(value, settings_obj.default_due_days)
                _set(settings_obj, field, value, changed)

        source = answers.get('clients_source')
        if source == 'itemlist':
            itemlist, list_created = self._clients_itemlist(tenant, user)
            if list_created:
                result['created']['itemlists'] = 1
            if settings_obj.client_itemlist_id != itemlist.id:
                settings_obj.client_itemlist = itemlist
                changed.append('client_itemlist')
        elif source == 'ecommerce':
            if settings_obj.client_itemlist_id is not None:
                settings_obj.client_itemlist = None
                changed.append('client_itemlist')
            result['notes'].append('Invoice clients come from the online shop; no client list was created.')

        if created or changed:
            settings_obj.save()
        result['updated'] = changed
        return result

    def seed_defaults(self, tenant):
        settings_obj, created = InvoiceSettings.objects.get_or_create()
        if not settings_obj.company_name and tenant and tenant.name:
            settings_obj.company_name = tenant.name
            settings_obj.save(update_fields=['company_name', 'updated_at'])

    # ── helpers ──

    @staticmethod
    def _clients_itemlist(tenant, user):
        """The "Clients" list (either language), created when missing."""
        existing = (
            ItemList.objects.filter(title__in=list(CLIENTS_LIST_TITLES.values()), parent_list__isnull=True)
            .order_by('-is_active', 'id').first()
        )
        if existing is not None:
            return existing, False
        return ItemList.objects.create(
            title=_clients_list_title(tenant),
            description='Invoice clients',
            is_active=True,
            created_by=user,
        ), True


def _ecommerce_enabled(tenant):
    try:
        from onboarding.services import enabled_feature_keys
        return 'ecommerce_crm' in enabled_feature_keys(tenant)
    except Exception:  # noqa: BLE001
        return False
