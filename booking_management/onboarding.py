"""
Bookings onboarding step (key ``booking_management``, feature
``booking_management``, order 30).

Answers schema (``~/Echodesk/.wt/onb/SCHEMAS.md``)::

    {
      "timezone": str,
      "hours": Hours,                                   # → StaffAvailability for every staff in staff_user_ids
      "services": [{"name": I18n, "duration_minutes": int, "price": "25.00",
                    "deposit_percentage": int, "category": I18n | null}],
      "staff_user_ids": [UserId],                       # → BookingStaff (+ availability, + assigned to the services)
      "payment": {"method": "manual_transfer"|"bog_gateway", "allow_cash": bool, "allow_card": bool,
                  "require_deposit": bool, "bank_name": str, "bank_iban": str, "bank_account_holder": str,
                  "bog_client_id": str|null, "bog_client_secret": str|null, "bog_use_production": bool},
      "rules": {"min_hours_before_booking": int, "max_days_advance_booking": int,
                "cancellation_hours_before": int, "refund_policy": "full"|"partial_50"|"partial_25"|"no_refund",
                "auto_confirm": bool},                   # auto_confirm → both auto_confirm_* flags
      "reminders": {"reminder_hours_before": int, "second_reminder_hours_before": int|null, "sms_enabled": bool,
                    "sms_api_key": str|null, "sms_on_created": bool, "sms_on_confirmed": bool,
                    "sms_on_rescheduled": bool, "sms_on_cancelled": bool, "sms_on_reminder": bool},
      "public_page": {"enabled": bool, "description": I18n, "address": str, "phone": str}
    }

Only the keys present are applied (inside each object too). ``apply``
writes ``BookingSettings`` (created when missing; secrets through the
model's encrypting setters, an empty/null key keeps the stored one),
``ServiceCategory`` / ``Service`` rows matched by English or Georgian name
(``status='active'``, every staff member of the answer assigned),
``BookingStaff`` for ``staff_user_ids`` and one ``StaffAvailability`` row per
weekday from ``hours`` (closed day → ``is_available=False``). Re-applying
updates the same rows and creates nothing new.

``seed_defaults``: the ``BookingSettings`` row exists.
"""
import datetime as dt
import logging
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import serializers

from onboarding.base import OnboardingStep, empty_result
from onboarding.common import (
    DAY_KEYS,
    DEFAULT_HOURS,
    HoursSerializer,
    I18nSerializer,
    UserIdsField,
    clock_str,
    i18n,
    parse_clock,
    users_context,
)
from onboarding.registry import register

from .emails import booking_site_url
from .models import BookingSettings, BookingStaff, Service, ServiceCategory, StaffAvailability

logger = logging.getLogger(__name__)
User = get_user_model()

PAYMENT_METHODS = [key for key, _ in BookingSettings.PAYMENT_METHOD_CHOICES]
REFUND_POLICIES = [key for key, _ in BookingSettings.REFUND_POLICY_CHOICES]

# Placeholder working window stored on closed days (the row is is_available=False).
CLOSED_START, CLOSED_END = dt.time(9, 0), dt.time(18, 0)


def _service(name_en, name_ka, minutes, price, deposit=0, category=None):
    return {
        'name': i18n(name_en, name_ka),
        'duration_minutes': minutes,
        'price': price,
        'deposit_percentage': deposit,
        'category': category,
    }


CAT_HAIR = i18n('Hair', 'თმა')
CAT_NAILS = i18n('Nails', 'ფრჩხილები')
CAT_FACE = i18n('Face & body', 'სახე და სხეული')
CAT_CONSULT = i18n('Consultations', 'კონსულტაციები')
CAT_DIAG = i18n('Diagnostics', 'დიაგნოსტიკა')
CAT_SERVICES = i18n('Services', 'მომსახურება')

SERVICE_PRESETS = {
    'salon': [
        _service('Haircut', 'თმის შეჭრა', 45, '40.00', 0, CAT_HAIR),
        _service('Colouring', 'თმის შეღებვა', 120, '150.00', 20, CAT_HAIR),
        _service('Blow-dry', 'თმის დავარცხნა', 30, '35.00', 0, CAT_HAIR),
        _service('Manicure', 'მანიკური', 60, '50.00', 0, CAT_NAILS),
        _service('Pedicure', 'პედიკური', 60, '60.00', 0, CAT_NAILS),
        _service('Facial', 'სახის წმენდა', 60, '90.00', 20, CAT_FACE),
    ],
    'clinic': [
        _service('Consultation', 'კონსულტაცია', 30, '80.00', 0, CAT_CONSULT),
        _service('Follow-up visit', 'განმეორებითი ვიზიტი', 20, '50.00', 0, CAT_CONSULT),
        _service('Ultrasound', 'ექოსკოპია', 30, '100.00', 0, CAT_DIAG),
        _service('Blood test', 'სისხლის ანალიზი', 15, '40.00', 0, CAT_DIAG),
    ],
    'services': [
        _service('Consultation', 'კონსულტაცია', 30, '50.00', 0, CAT_SERVICES),
        _service('On-site visit', 'ადგილზე ვიზიტი', 60, '100.00', 30, CAT_SERVICES),
        _service('Repair', 'შეკეთება', 90, '150.00', 30, CAT_SERVICES),
    ],
    'real_estate': [
        _service('Property viewing', 'ობიექტის დათვალიერება', 45, '0.00', 0, CAT_SERVICES),
        _service('Consultation', 'კონსულტაცია', 30, '0.00', 0, CAT_CONSULT),
    ],
    'shop': [
        _service('Consultation', 'კონსულტაცია', 30, '0.00', 0, CAT_CONSULT),
        _service('Fitting', 'მორგება', 30, '0.00', 0, CAT_SERVICES),
    ],
    'call_centre': [
        _service('Call back', 'უკან დარეკვა', 15, '0.00', 0, CAT_SERVICES),
        _service('Consultation', 'კონსულტაცია', 30, '0.00', 0, CAT_CONSULT),
    ],
    'logistics': [
        _service('Pickup', 'ტვირთის აღება', 30, '20.00', 0, CAT_SERVICES),
        _service('Delivery', 'მიწოდება', 60, '30.00', 0, CAT_SERVICES),
    ],
    'other': [
        _service('Consultation', 'კონსულტაცია', 30, '50.00', 0, CAT_CONSULT),
        _service('Appointment', 'ვიზიტი', 60, '80.00', 0, CAT_SERVICES),
    ],
}


def _validate_timezone(value):
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(value)
    except Exception:   # noqa: BLE001
        raise serializers.ValidationError(f'Invalid timezone: {value}')
    return value


def _name_matches(name):
    """Case-insensitive JSON name filter on the en / ka keys."""
    from django.db.models import Q
    query = Q()
    for lang in ('en', 'ka'):
        text = (name.get(lang) or '').strip()
        if text:
            query |= Q(**{f'name__{lang}__iexact': text})
    return query


# ── serializer ──

class ServiceAnswerSerializer(serializers.Serializer):
    name = I18nSerializer()
    duration_minutes = serializers.IntegerField(min_value=5, max_value=24 * 60)
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0'))
    deposit_percentage = serializers.IntegerField(min_value=0, max_value=100, required=False, default=0)
    category = I18nSerializer(required=False, allow_null=True, require_text=False)

    def validate_category(self, value):
        if value and not (value.get('en') or value.get('ka')):
            return None
        return value


class PaymentAnswerSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=PAYMENT_METHODS, required=False)
    allow_cash = serializers.BooleanField(required=False)
    allow_card = serializers.BooleanField(required=False)
    require_deposit = serializers.BooleanField(required=False)
    bank_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    bank_iban = serializers.CharField(max_length=50, required=False, allow_blank=True)
    bank_account_holder = serializers.CharField(max_length=255, required=False, allow_blank=True)
    bog_client_id = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    bog_client_secret = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    bog_use_production = serializers.BooleanField(required=False)


class RulesAnswerSerializer(serializers.Serializer):
    min_hours_before_booking = serializers.IntegerField(min_value=0, max_value=24 * 30, required=False)
    max_days_advance_booking = serializers.IntegerField(min_value=1, max_value=730, required=False)
    cancellation_hours_before = serializers.IntegerField(min_value=0, max_value=24 * 30, required=False)
    refund_policy = serializers.ChoiceField(choices=REFUND_POLICIES, required=False)
    auto_confirm = serializers.BooleanField(required=False)


class RemindersAnswerSerializer(serializers.Serializer):
    reminder_hours_before = serializers.IntegerField(min_value=1, max_value=24 * 30, required=False)
    second_reminder_hours_before = serializers.IntegerField(min_value=1, max_value=24 * 30, required=False, allow_null=True)
    sms_enabled = serializers.BooleanField(required=False)
    sms_api_key = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    sms_on_created = serializers.BooleanField(required=False)
    sms_on_confirmed = serializers.BooleanField(required=False)
    sms_on_rescheduled = serializers.BooleanField(required=False)
    sms_on_cancelled = serializers.BooleanField(required=False)
    sms_on_reminder = serializers.BooleanField(required=False)


class PublicPageAnswerSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
    description = I18nSerializer(required=False, require_text=False)
    address = serializers.CharField(max_length=255, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=50, required=False, allow_blank=True)


class BookingsAnswersSerializer(serializers.Serializer):
    timezone = serializers.CharField(max_length=64, required=False, validators=[_validate_timezone])
    hours = HoursSerializer(required=False, allow_null=True)
    services = ServiceAnswerSerializer(many=True, required=False)
    staff_user_ids = UserIdsField()
    payment = PaymentAnswerSerializer(required=False)
    rules = RulesAnswerSerializer(required=False)
    reminders = RemindersAnswerSerializer(required=False)
    public_page = PublicPageAnswerSerializer(required=False)

    def validate_services(self, value):
        seen = set()
        for row in value:
            for lang in ('en', 'ka'):
                text = (row['name'].get(lang) or '').strip().lower()
                if text and (lang, text) in seen:
                    raise serializers.ValidationError(f'Service "{row["name"][lang]}" is listed more than once.')
                if text:
                    seen.add((lang, text))
        return value


# ── step ──

@register
class BookingsStep(OnboardingStep):
    key = 'booking_management'
    feature_key = 'booking_management'
    order = 30
    serializer_class = BookingsAnswersSerializer

    # -- read --

    def context(self, tenant, request):
        return {
            'users': users_context(),
            'existing': {
                'services': Service.objects.count(),
                'staff': BookingStaff.objects.count(),
            },
            'service_presets': SERVICE_PRESETS,
            'public_url': self.public_url(tenant),
        }

    @staticmethod
    def public_url(tenant):
        # The settings page builds `https://book.<main domain>/<tenant subdomain>`;
        # the schema name is the subdomain.
        return f'{booking_site_url()}/{tenant.schema_name}'

    def prefill(self, tenant, state):
        settings = BookingSettings.objects.filter(tenant=tenant).first()
        industry = (state.industry if state else None) or 'other'

        staff_ids = list(BookingStaff.objects.order_by('id').values_list('user_id', flat=True))
        hours = None
        first_staff = BookingStaff.objects.order_by('id').first()
        if first_staff is not None:
            rows = {a.day_of_week: a for a in first_staff.availability.all()}
            if rows:
                hours = {}
                for index, day in enumerate(DAY_KEYS):
                    row = rows.get(index)
                    if row is None or not row.is_available:
                        hours[day] = None
                    else:
                        hours[day] = {
                            'from': clock_str(row.start_time), 'to': clock_str(row.end_time),
                            'break_from': clock_str(row.break_start), 'break_to': clock_str(row.break_end),
                        }
        if hours is None:
            social = state.answers_of('social_integrations') if state else {}
            hours = (social or {}).get('hours') or DEFAULT_HOURS

        services = [
            {
                'name': s.name if isinstance(s.name, dict) else i18n(str(s.name), ''),
                'duration_minutes': s.duration_minutes,
                'price': str(s.base_price),
                'deposit_percentage': s.deposit_percentage,
                'category': (s.category.name if s.category and isinstance(s.category.name, dict) else None),
            }
            for s in Service.objects.select_related('category').order_by('category', 'id')[:50]
        ] or [dict(p) for p in SERVICE_PRESETS.get(industry, SERVICE_PRESETS['other'])]

        s = settings
        return {
            'timezone': (s.timezone if s else None) or 'Asia/Tbilisi',
            'hours': hours,
            'services': services,
            'staff_user_ids': staff_ids,
            'payment': {
                'method': s.payment_method if s else 'manual_transfer',
                'allow_cash': s.allow_cash_payment if s else True,
                'allow_card': s.allow_card_payment if s else True,
                'require_deposit': s.require_deposit if s else False,
                'bank_name': s.bank_name if s else '',
                'bank_iban': s.bank_iban if s else '',
                'bank_account_holder': (s.bank_account_holder if s else '') or tenant.name,
                'bog_client_id': None,
                'bog_client_secret': None,
                'bog_use_production': s.bog_use_production if s else False,
                'has_bog_credentials': bool(s and s.bog_client_id and s.bog_client_secret),
            },
            'rules': {
                'min_hours_before_booking': s.min_hours_before_booking if s else 2,
                'max_days_advance_booking': s.max_days_advance_booking if s else 60,
                'cancellation_hours_before': s.cancellation_hours_before if s else 24,
                'refund_policy': s.refund_policy if s else 'full',
                'auto_confirm': (s.auto_confirm_on_deposit and s.auto_confirm_on_full_payment) if s else True,
            },
            'reminders': {
                'reminder_hours_before': s.reminder_hours_before if s else 24,
                'second_reminder_hours_before': s.second_reminder_hours_before if s else None,
                'sms_enabled': s.sms_enabled if s else False,
                'sms_api_key': None,
                'has_sms_api_key': bool(s and s.sms_api_key),
                'sms_on_created': s.sms_on_created if s else False,
                'sms_on_confirmed': s.sms_on_confirmed if s else True,
                'sms_on_rescheduled': s.sms_on_rescheduled if s else True,
                'sms_on_cancelled': s.sms_on_cancelled if s else True,
                'sms_on_reminder': s.sms_on_reminder if s else True,
            },
            'public_page': {
                'enabled': s.public_page_enabled if s else True,
                'description': {
                    'en': ((s.public_description or {}).get('en') if s else '') or '',
                    'ka': ((s.public_description or {}).get('ka') if s else '') or '',
                },
                'address': s.public_address if s else '',
                'phone': s.public_phone if s else '',
            },
        }

    # -- write --

    def apply(self, tenant, answers, user):
        result = empty_result()
        created, updated, notes = result['created'], result['updated'], result['notes']

        settings, settings_created = BookingSettings.objects.get_or_create(tenant=tenant)
        created['settings'] = int(settings_created)
        changed = self._apply_settings(settings, answers)
        if changed:
            settings.save()
            updated.extend(changed)

        staff_rows = []
        if 'staff_user_ids' in answers:
            staff_rows, staff_created = self._ensure_staff(answers['staff_user_ids'])
            created['staff'] = staff_created
        hours = answers.get('hours')   # null = not answered; a closed day is null inside
        if hours is not None and staff_rows:
            created['availability'] = self._ensure_availability(staff_rows, hours)
        elif hours is not None and not staff_rows:
            notes.append('Opening hours were not stored: pick at least one staff member.')

        if 'services' in answers:
            categories_created, services_created = self._ensure_services(answers['services'], staff_rows)
            created['categories'] = categories_created
            created['services'] = services_created

        return result

    def seed_defaults(self, tenant):
        BookingSettings.objects.get_or_create(tenant=tenant)

    # -- helpers --

    @staticmethod
    def _apply_settings(settings, answers):
        changed = []

        def put(field, value):
            if getattr(settings, field) != value:
                setattr(settings, field, value)
                changed.append(field)

        if 'timezone' in answers:
            put('timezone', answers['timezone'])

        payment = answers.get('payment')
        if payment:
            direct = {
                'method': 'payment_method', 'allow_cash': 'allow_cash_payment', 'allow_card': 'allow_card_payment',
                'require_deposit': 'require_deposit', 'bank_name': 'bank_name', 'bank_iban': 'bank_iban',
                'bank_account_holder': 'bank_account_holder', 'bog_use_production': 'bog_use_production',
            }
            for key, field in direct.items():
                if key in payment:
                    put(field, payment[key])
            # Secrets: the dashboard never gets them back, so empty/null = keep.
            if payment.get('bog_client_id'):
                settings.bog_client_id = payment['bog_client_id'].strip()
                changed.append('bog_client_id')
            if payment.get('bog_client_secret'):
                settings.bog_client_secret = payment['bog_client_secret'].strip()
                changed.append('bog_client_secret')

        rules = answers.get('rules')
        if rules:
            for key in ('min_hours_before_booking', 'max_days_advance_booking', 'cancellation_hours_before', 'refund_policy'):
                if key in rules:
                    put(key, rules[key])
            if 'auto_confirm' in rules:
                put('auto_confirm_on_deposit', rules['auto_confirm'])
                put('auto_confirm_on_full_payment', rules['auto_confirm'])

        reminders = answers.get('reminders')
        if reminders:
            for key in (
                'reminder_hours_before', 'second_reminder_hours_before', 'sms_enabled',
                'sms_on_created', 'sms_on_confirmed', 'sms_on_rescheduled', 'sms_on_cancelled', 'sms_on_reminder',
            ):
                if key in reminders:
                    put(key, reminders[key])
            if reminders.get('sms_api_key'):
                settings.sms_api_key = reminders['sms_api_key'].strip()
                changed.append('sms_api_key')

        page = answers.get('public_page')
        if page:
            if 'enabled' in page:
                put('public_page_enabled', page['enabled'])
            if 'description' in page:
                put('public_description', {'en': page['description'].get('en', ''), 'ka': page['description'].get('ka', '')})
            if 'address' in page:
                put('public_address', page['address'])
            if 'phone' in page:
                put('public_phone', page['phone'])
        return changed

    @staticmethod
    def _ensure_staff(user_ids):
        rows, made = [], 0
        for member in User.objects.filter(id__in=user_ids).order_by('id'):
            staff, was_created = BookingStaff.objects.get_or_create(user=member)
            rows.append(staff)
            made += int(was_created)
        return rows, made

    @staticmethod
    def _ensure_availability(staff_rows, hours):
        made = 0
        for staff in staff_rows:
            for index, day in enumerate(DAY_KEYS):
                entry = hours.get(day)
                if entry:
                    defaults = {
                        'start_time': parse_clock(entry['from']),
                        'end_time': parse_clock(entry['to']),
                        'break_start': parse_clock(entry.get('break_from')) if entry.get('break_from') else None,
                        'break_end': parse_clock(entry.get('break_to')) if entry.get('break_to') else None,
                        'is_available': True,
                    }
                else:
                    defaults = {
                        'start_time': CLOSED_START, 'end_time': CLOSED_END,
                        'break_start': None, 'break_end': None, 'is_available': False,
                    }
                _, was_created = StaffAvailability.objects.update_or_create(
                    staff=staff, day_of_week=index, defaults=defaults,
                )
                made += int(was_created)
        return made

    @staticmethod
    def _ensure_category(name):
        if not name:
            return None, False
        category = ServiceCategory.objects.filter(_name_matches(name)).order_by('id').first()
        if category is not None:
            return category, False
        display_order = (ServiceCategory.objects.order_by('-display_order').values_list('display_order', flat=True).first() or 0) + 1
        category = ServiceCategory.objects.create(
            name={'en': name.get('en', ''), 'ka': name.get('ka', '')},
            display_order=display_order,
        )
        return category, True

    def _ensure_services(self, rows, staff_rows):
        categories_created = services_created = 0
        for row in rows:
            category, cat_created = self._ensure_category(row.get('category'))
            categories_created += int(cat_created)
            service = Service.objects.filter(_name_matches(row['name'])).order_by('id').first()
            if service is None:
                service = Service.objects.create(
                    name={'en': row['name'].get('en', ''), 'ka': row['name'].get('ka', '')},
                    category=category,
                    base_price=row['price'],
                    deposit_percentage=row.get('deposit_percentage') or 0,
                    duration_minutes=row['duration_minutes'],
                    status='active',
                )
                services_created += 1
            elif service.category_id is None and category is not None:
                service.category = category
                service.save(update_fields=['category', 'updated_at'])
            if staff_rows:
                service.staff_members.add(*staff_rows)
        return categories_created, services_created
