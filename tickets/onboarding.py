"""
Tickets onboarding step (key ``tickets``, feature ``tickets``, order 10).

Answers schema (``~/Echodesk/.wt/onb/SCHEMAS.md``)::

    {
      "purpose": "support" | "tasks" | "jobs",      # required; picks the column preset
      "board_name": str,                            # default per purpose (Support / Tasks / Jobs)
      "team_user_ids": [UserId],                    # → TenantGroup "<board_name>" + Board.board_groups
      "form_fields": ["phone", "address", "site", "deadline"]
                     + [{"label": str, "type": "string"|"text"|"number"|"date"}],
      "tags": [str],
      "only_superadmin_can_delete_tickets": bool,
      "min_users_per_ticket": int (0-100),
      "telegram": {"bot_token": str, "chat_id": str} | null
    }

Only the keys present are applied. ``apply`` writes:

- ``Board`` matched by name (case-insensitive); ``is_default`` only when the
  tenant has no default board yet. Columns from the purpose preset, matched
  by name per board (either language), stored in the tenant's dashboard
  language (``ka`` → Georgian, otherwise English).
- ``users.TenantGroup`` named after the board (members = ``team_user_ids``,
  gets the ``tickets`` feature so members see the module) + ``Board.board_groups``.
- One default ``TicketForm`` titled after the board whose ``custom_fields``
  hold the chosen fields (the shape ``settings/ticket-forms`` edits; fields
  are merged by ``name``, never removed).
- ``Tag`` rows matched by name.
- ``Tenant.only_superadmin_can_delete_tickets`` / ``min_users_per_ticket``
  through the same guard + save path as ``PATCH /api/tenant-settings/``.
- ``BoardTelegramConnection`` for the board (token stored through
  ``set_bot_token``).

``seed_defaults``: Board "Main" (default) + To do / In progress / Review /
Done when the tenant has no board, so ``Ticket.save`` always finds a column.
"""
import logging
import re

from django.contrib.auth import get_user_model
from rest_framework import serializers

from onboarding.base import OnboardingStep, empty_result
from onboarding.common import UserIdsField, i18n, tenant_language, users_context
from onboarding.registry import register
from users.models import TenantGroup

from .models import Board, BoardTelegramConnection, Tag, Ticket, TicketColumn, TicketForm

logger = logging.getLogger(__name__)
User = get_user_model()

PURPOSES = ['support', 'tasks', 'jobs']

BOARD_NAMES = {
    'support': i18n('Support', 'მხარდაჭერა'),
    'tasks': i18n('Tasks', 'დავალებები'),
    'jobs': i18n('Jobs', 'შეკვეთები'),
}
SEED_BOARD_NAME = i18n('Main', 'მთავარი')

COLOR_DEFAULT, COLOR_ACTIVE, COLOR_WAITING, COLOR_CLOSED = '#6B7280', '#3B82F6', '#F59E0B', '#10B981'

# (en, ka, column attributes)
COLUMN_PRESETS = {
    'support': [
        ('New', 'ახალი', {'is_default': True, 'color': COLOR_DEFAULT}),
        ('In progress', 'მიმდინარე', {'track_time': True, 'color': COLOR_ACTIVE}),
        ('Waiting for customer', 'ველოდებით მომხმარებელს', {'color': COLOR_WAITING}),
        ('Resolved', 'გადაწყვეტილი', {'is_closed_status': True, 'color': COLOR_CLOSED}),
    ],
    'tasks': [
        ('To do', 'გასაკეთებელი', {'is_default': True, 'color': COLOR_DEFAULT}),
        ('In progress', 'მიმდინარე', {'track_time': True, 'color': COLOR_ACTIVE}),
        ('Review', 'განხილვა', {'color': COLOR_WAITING}),
        ('Done', 'დასრულებული', {'is_closed_status': True, 'color': COLOR_CLOSED}),
    ],
    'jobs': [
        ('New', 'ახალი', {'is_default': True, 'color': COLOR_DEFAULT}),
        ('Scheduled', 'დაგეგმილი', {'color': COLOR_WAITING}),
        ('In progress', 'მიმდინარე', {'track_time': True, 'color': COLOR_ACTIVE}),
        ('Done', 'დასრულებული', {'is_closed_status': True, 'color': COLOR_CLOSED}),
    ],
}
SEED_COLUMNS = COLUMN_PRESETS['tasks']

FIELD_TYPES = ['string', 'text', 'number', 'date']
FORM_FIELD_PRESETS = {
    'phone': {'label': i18n('Phone', 'ტელეფონი'), 'type': 'string'},
    'address': {'label': i18n('Address', 'მისამართი'), 'type': 'text'},
    'site': {'label': i18n('Object / site', 'ობიექტი'), 'type': 'string'},
    'deadline': {'label': i18n('Deadline', 'ვადა'), 'type': 'date'},
}

TAG_PRESETS = {
    'salon': [i18n('Complaint', 'საჩივარი'), i18n('Reschedule', 'გადატანა'), i18n('VIP', 'VIP')],
    'clinic': [i18n('Urgent', 'სასწრაფო'), i18n('Follow-up', 'განმეორებითი'), i18n('Insurance', 'დაზღვევა')],
    'real_estate': [i18n('Viewing', 'დათვალიერება'), i18n('Rent', 'ქირავდება'), i18n('Sale', 'იყიდება')],
    'shop': [i18n('Refund', 'დაბრუნება'), i18n('Delivery', 'მიწოდება'), i18n('Wholesale', 'საბითუმო')],
    'call_centre': [i18n('Billing', 'ანგარიშსწორება'), i18n('Technical', 'ტექნიკური'), i18n('Callback', 'უკუკავშირი')],
    'logistics': [i18n('Pickup', 'აღება'), i18n('Delayed', 'დაგვიანებული'), i18n('Damaged', 'დაზიანებული')],
    'services': [i18n('Quote', 'შეთავაზება'), i18n('Warranty', 'გარანტია'), i18n('Repeat client', 'მუდმივი კლიენტი')],
    'other': [i18n('Urgent', 'სასწრაფო'), i18n('Bug', 'ხარვეზი'), i18n('Question', 'კითხვა')],
}
TAG_COLORS = ['#EF4444', '#F59E0B', '#10B981', '#3B82F6', '#8B5CF6', '#EC4899', '#14B8A6', '#6366F1']

INDUSTRY_PURPOSE = {
    'shop': 'jobs', 'logistics': 'jobs', 'services': 'jobs',
    'salon': 'support', 'clinic': 'support', 'real_estate': 'support', 'call_centre': 'support',
}
INDUSTRY_FORM_FIELDS = {
    'real_estate': ['phone', 'address', 'site'],
    'logistics': ['phone', 'address', 'deadline'],
    'services': ['phone', 'address', 'deadline'],
    'shop': ['phone', 'address'],
}


def _clean(value):
    return ' '.join(str(value or '').split())


def _field_name(label, taken):
    base = re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_') or 'field'
    name, n = base, 2
    while name in taken:
        name, n = f'{base}_{n}', n + 1
    return name


# ── serializer ──

class CustomFieldSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=100)
    type = serializers.ChoiceField(choices=FIELD_TYPES, default='string')

    def validate_label(self, value):
        value = _clean(value)
        if not value:
            raise serializers.ValidationError('Label cannot be blank.')
        return value


class FormFieldsField(serializers.ListField):
    """Preset keys (strings) and custom ``{"label", "type"}`` objects, mixed."""

    def __init__(self, **kwargs):
        kwargs.setdefault('child', serializers.JSONField())
        kwargs.setdefault('required', False)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        out, errors, seen = [], {}, set()
        for index, item in enumerate(super().to_internal_value(data)):
            if isinstance(item, str):
                key = item.strip().lower()
                if key not in FORM_FIELD_PRESETS:
                    errors[index] = [f'Unknown preset field "{item}".']
                elif key not in seen:
                    seen.add(key)
                    out.append(key)
            elif isinstance(item, dict):
                nested = CustomFieldSerializer(data=item)
                if not nested.is_valid():
                    errors[index] = nested.errors
                else:
                    label_key = nested.validated_data['label'].lower()
                    if label_key not in seen:
                        seen.add(label_key)
                        out.append(dict(nested.validated_data))
            else:
                errors[index] = ['Expected a preset key or {"label", "type"}.']
        if errors:
            raise serializers.ValidationError(errors)
        return out


class TelegramSerializer(serializers.Serializer):
    bot_token = serializers.CharField(max_length=200)
    chat_id = serializers.CharField(max_length=100)


class TicketsAnswersSerializer(serializers.Serializer):
    purpose = serializers.ChoiceField(choices=PURPOSES)
    board_name = serializers.CharField(max_length=255, required=False)
    team_user_ids = UserIdsField()
    form_fields = FormFieldsField()
    tags = serializers.ListField(child=serializers.CharField(max_length=50, allow_blank=True), required=False)
    only_superadmin_can_delete_tickets = serializers.BooleanField(required=False)
    min_users_per_ticket = serializers.IntegerField(required=False, min_value=0, max_value=100)
    telegram = TelegramSerializer(required=False, allow_null=True)

    def validate_board_name(self, value):
        value = _clean(value)
        if not value:
            raise serializers.ValidationError('Board name cannot be blank.')
        return value

    def validate_tags(self, value):
        seen, cleaned = set(), []
        for name in value:
            name = _clean(name)
            if name and name.lower() not in seen:
                seen.add(name.lower())
                cleaned.append(name)
        return cleaned

    def validate_only_superadmin_can_delete_tickets(self, value):
        # Same guard as PATCH /api/tenant-settings/: only staff/superusers may
        # change the delete restriction; sending the current value is fine.
        tenant = self.context.get('tenant')
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if (
            tenant is not None and user is not None
            and value != tenant.only_superadmin_can_delete_tickets
            and not (user.is_staff or user.is_superuser)
        ):
            raise serializers.ValidationError('Only staff or superusers can change this setting.')
        return value


# ── step ──

@register
class TicketsStep(OnboardingStep):
    key = 'tickets'
    feature_key = 'ticket_management'
    order = 10
    serializer_class = TicketsAnswersSerializer

    # -- read --

    def context(self, tenant, request):
        return {
            'users': users_context(),
            'groups': [{'id': g.id, 'name': g.name} for g in TenantGroup.objects.order_by('name')],
            'has_boards': Board.objects.exists(),
            'presets': {
                purpose: [i18n(en, ka) for en, ka, _ in columns]
                for purpose, columns in COLUMN_PRESETS.items()
            },
            'board_names': BOARD_NAMES,
            'form_field_presets': {
                key: {'label': preset['label'], 'type': preset['type']}
                for key, preset in FORM_FIELD_PRESETS.items()
            },
            'tag_presets': TAG_PRESETS,
        }

    def prefill(self, tenant, state):
        language = tenant_language(tenant)
        industry = state.industry if state else None
        purpose = INDUSTRY_PURPOSE.get(industry, 'support')
        return {
            'purpose': purpose,
            'board_name': BOARD_NAMES[purpose][language],
            'team_user_ids': list(
                User.objects.filter(is_active=True).order_by('id').values_list('id', flat=True)
            ),
            'form_fields': list(INDUSTRY_FORM_FIELDS.get(industry, ['phone'])),
            'tags': [t[language] for t in TAG_PRESETS.get(industry or 'other', TAG_PRESETS['other'])],
            'only_superadmin_can_delete_tickets': bool(tenant.only_superadmin_can_delete_tickets),
            'min_users_per_ticket': int(tenant.min_users_per_ticket or 0),
            'telegram': None,
        }

    # -- write --

    def apply(self, tenant, answers, user):
        result = empty_result()
        created, updated, notes = result['created'], result['updated'], result['notes']
        language = tenant_language(tenant)
        purpose = answers['purpose']
        board_name = answers.get('board_name') or BOARD_NAMES[purpose][language]

        board, board_created = self._ensure_board(board_name, user)
        created['boards'] = int(board_created)
        created['board_id'] = board.id
        if not board_created:
            notes.append(f'Board "{board.name}" already existed; its columns were completed.')
        if not board.is_default:
            default = Board.objects.filter(is_default=True).exclude(pk=board.pk).first()
            if default is not None:
                notes.append(f'"{default.name}" stays the default board.')

        created['columns'] = self._ensure_columns(board, COLUMN_PRESETS[purpose], language, user)

        if answers.get('team_user_ids'):
            group, group_created = self._ensure_group(board, answers['team_user_ids'])
            created['groups'] = int(group_created)
            created['group_id'] = group.id
            created['group_members'] = len(answers['team_user_ids'])

        if 'form_fields' in answers and answers['form_fields']:
            form, form_created, fields_added = self._ensure_form(board_name, answers['form_fields'], language, user)
            created['forms'] = int(form_created)
            created['form_id'] = form.id
            created['form_fields'] = fields_added

        if 'tags' in answers:
            created['tags'] = self._ensure_tags(answers['tags'], user)

        changed = []
        for field in ('only_superadmin_can_delete_tickets', 'min_users_per_ticket'):
            if field in answers and getattr(tenant, field) != answers[field]:
                setattr(tenant, field, answers[field])
                changed.append(field)
        if changed:
            tenant.save(update_fields=changed)   # same save path as PATCH /api/tenant-settings/
            updated.extend(changed)

        if answers.get('telegram'):
            created['telegram_connections'] = int(self._ensure_telegram(board, answers['telegram'], user))

        return result

    def seed_defaults(self, tenant):
        if Board.objects.exists():
            return
        owner = self._seed_owner()
        if owner is None:
            logger.warning('tickets seed_defaults: no user on %s yet, no board seeded', tenant.schema_name)
            return
        language = tenant_language(tenant)
        board = Board.objects.create(name=SEED_BOARD_NAME[language], is_default=True, created_by=owner)
        self._ensure_columns(board, SEED_COLUMNS, language, owner)

    # -- helpers --

    @staticmethod
    def _seed_owner():
        for flt in ({'is_superuser': True}, {'is_staff': True}, {'role': 'admin'}, {}):
            owner = User.objects.filter(**flt).order_by('id').first()
            if owner is not None:
                return owner
        return None

    @classmethod
    def _ensure_board(cls, name, user):
        board = Board.objects.filter(name__iexact=name).order_by('id').first()
        if board is not None:
            return board, False
        seeded = cls._untouched_seed_board()
        if seeded is not None:
            # Adopt the board seeded at provisioning instead of adding a second
            # one: rename it and let the purpose preset replace its columns.
            seeded.name = name
            seeded.save()   # full save: Board.save() re-asserts is_default
            seeded.columns.all().delete()
            return seeded, True
        board = Board.objects.create(
            name=name,
            is_default=not Board.objects.filter(is_default=True).exists(),
            created_by=user,
        )
        return board, True

    @staticmethod
    def _untouched_seed_board():
        """The board ``seed_defaults`` created, if nothing has been put on it yet."""
        names = [n.lower() for n in SEED_BOARD_NAME.values()]
        for board in Board.objects.filter(is_default=True).order_by('id'):
            if board.name.lower() not in names:
                continue
            if Ticket.objects.filter(column__board=board).exists():
                continue
            return board
        return None

    @staticmethod
    def _ensure_columns(board, preset, language, user):
        """Create the preset columns missing on ``board`` (matched by either
        language's name); returns how many were created."""
        existing = {c.name.lower(): c for c in board.columns.all()}
        next_position = max([c.position for c in existing.values()] + [-1]) + 1
        made = 0
        for en, ka, attrs in preset:
            if en.lower() in existing or ka.lower() in existing:
                continue
            attrs = dict(attrs)
            if attrs.get('is_default') and any(c.is_default for c in existing.values()):
                attrs['is_default'] = False   # keep the board's current default
            column = TicketColumn.objects.create(
                board=board,
                name=ka if language == 'ka' else en,
                position=next_position,
                created_by=user,
                **attrs,
            )
            existing[column.name.lower()] = column
            next_position += 1
            made += 1
        if not any(c.is_default for c in existing.values()):
            first = board.columns.order_by('position', 'created_at').first()
            if first is not None:
                first.is_default = True
                first.save(update_fields=['is_default'])
        return made

    @staticmethod
    def _ensure_group(board, user_ids):
        group = TenantGroup.objects.filter(name__iexact=board.name).first()
        group_created = False
        if group is None:
            group = TenantGroup.objects.create(name=board.name, description=f'{board.name} board team')
            group_created = True
        if user_ids:
            group.members.add(*User.objects.filter(id__in=user_ids))
        try:
            from tenants.models import Feature
            feature = Feature.objects.filter(key='ticket_management', is_active=True).first()
            if feature is not None:
                group.features.add(feature)
        except Exception:   # noqa: BLE001 — the group is still usable without the feature link
            logger.exception('tickets onboarding: could not attach the tickets feature to %s', group.name)
        board.board_groups.add(group)
        return group, group_created

    @staticmethod
    def _ensure_form(title, form_fields, language, user):
        form = TicketForm.objects.filter(title__iexact=title).order_by('id').first()
        form_created = False
        if form is None:
            form = TicketForm(
                title=title,
                custom_fields=[],
                is_default=not TicketForm.objects.filter(is_default=True).exists(),
                is_active=True,
                created_by=user,
            )
            form_created = True
        fields = list(form.custom_fields) if isinstance(form.custom_fields, list) else []
        taken = {f.get('name') for f in fields if isinstance(f, dict)}
        labels = {str(f.get('label', '')).lower() for f in fields if isinstance(f, dict)}
        added = 0
        for item in form_fields:
            if isinstance(item, str):
                preset = FORM_FIELD_PRESETS[item]
                label, ftype, name = preset['label'][language], preset['type'], item
            else:
                label, ftype, name = item['label'], item.get('type', 'string'), _field_name(item['label'], taken)
            if name in taken or label.lower() in labels:
                continue
            fields.append({'name': name, 'label': label, 'type': ftype, 'required': False})
            taken.add(name)
            labels.add(label.lower())
            added += 1
        form.custom_fields = fields
        form.save()
        return form, form_created, added

    @staticmethod
    def _ensure_tags(names, user):
        made = 0
        for index, name in enumerate(names):
            if Tag.objects.filter(name__iexact=name).exists():
                continue
            Tag.objects.create(name=name, color=TAG_COLORS[index % len(TAG_COLORS)], created_by=user)
            made += 1
        return made

    @staticmethod
    def _ensure_telegram(board, telegram, user):
        connection = BoardTelegramConnection.objects.filter(board=board).first()
        created = connection is None
        if created:
            connection = BoardTelegramConnection(board=board, created_by=user)
        connection.set_bot_token(telegram['bot_token'].strip())
        connection.chat_id = telegram['chat_id'].strip()
        connection.is_active = True
        connection.save()
        return created
