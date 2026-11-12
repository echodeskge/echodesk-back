"""
Calls step (key ``ip_calling``, order 70).

Answers schema (every key optional; only the keys present are applied)::

    {
      "hosting": "byo" | "hosted",
      "server": {"name": str, "fqdn": str, "public_ip": str | null, "ami_port": 5038} | null,
      "trunk": {"provider": "magti" | "silknet" | "other", "sip_server": str, "sip_port": 5060,
                "username": str, "password": str, "realm": str,
                "phone_numbers": [str], "caller_id_number": str} | null,
      "agent_user_ids": [int],
      "queue": {"strategy": "ringall" | "rrmemory" | "leastrecent",
                "timeout_seconds": 30, "max_wait_seconds": 300},
      "hours": {"timezone": str,
                "schedule": {"mon": {"from": "09:00", "to": "18:00",
                                     "break_from": "13:00" | null, "break_to": "14:00" | null} | null, ...},
                "after_hours_action": "announcement" | "voicemail" | "forward",
                "forward_number": str},
      "rating": {"review_method": "none" | "callback" | "sms" | "both", "sms_api_key": str | null}
    }

``apply``:

- ``hosting == "hosted"``: a Telegram notification to the EchoDesk team
  (:func:`tenants.telegram_notifications.notify_pbx_hosting_request`, the
  same channel as the subscription / payment events) with the tenant name,
  the admin's e-mail and the requested server; ``result["notes"]`` carries
  ``hosted_request_sent`` and no ``PbxServer`` is created. Everything else is
  still applied.
- ``server`` (byo): a ``PbxServer`` through the same path as
  ``POST /api/pbx-servers/`` (row + ``provision_and_bootstrap``: dedicated
  realtime DB, role, migrations). Matched by ``fqdn``; a tenant has one
  server, so an existing one with another fqdn is kept and noted. A failed
  provisioning marks the row ``error`` (like the view) without undoing the
  rest of the step.
- ``trunk`` → ``Trunk`` matched by ``sip_server`` + ``username``
  (``register=True``, model-default codecs, first trunk is the default).
- ``agent_user_ids`` → one ``SipConfiguration`` named "EchoDesk" (server =
  the PbxServer's fqdn, WSS port 8089 + ``/ws``; else the existing default
  configuration) and one ``UserPhoneAssignment`` per user with extensions
  100, 101, … skipping numbers already taken (matched by user).
- ``queue`` → ``TenantGroup`` "Calls" (members = the agents) + ``Queue``
  slug ``support`` + one ``InboundRoute`` per trunk DID → that queue
  (matched by DID).
- ``hours`` → ``PbxSettings`` of that SipConfiguration:
  ``working_hours_enabled``, ``working_hours_schedule`` in the model's
  ``{"monday": [9, 10, …], …}`` open-hours format, ``timezone``,
  ``after_hours_action``, ``forward_number``.
- ``rating`` → ``review_method`` + ``sms_api_key`` (blank keeps the stored key).

Idempotent throughout. ``context``: ``server`` {id, name, fqdn, status,
install_command (exactly what /settings/pbx/server shows)} | null,
``trunks``, ``users``, ``hosted_request_sent``, ``agents`` (users that already
have an extension), ``has_sip_configuration``, ``has_queue``,
``telegram_configured``. ``seed_defaults``: nothing — there is no sensible
PBX without a server. The greeting upload stays on the upload-sound action.
"""
import logging
import math
import re
import secrets

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers

from onboarding.base import OnboardingStep, empty_result
from onboarding.registry import register
from users.models import TenantGroup

from .models import (
    InboundRoute, PbxServer, PbxSettings, Queue, SipConfiguration, Trunk, UserPhoneAssignment,
)

logger = logging.getLogger(__name__)
User = get_user_model()

HOSTING_CHOICES = ['byo', 'hosted']
PROVIDERS = ['magti', 'silknet', 'other']
PROVIDER_LABELS = {'magti': 'Magti', 'silknet': 'Silknet', 'other': 'SIP trunk'}
STRATEGIES = ['ringall', 'rrmemory', 'leastrecent']
AFTER_HOURS_ACTIONS = [key for key, _ in PbxSettings.AFTER_HOURS_ACTIONS]
REVIEW_METHODS = ['none', 'callback', 'sms', 'both']
HOURS_DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
DAY_NAMES = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')

SIP_CONFIG_NAME = 'EchoDesk'
CALLS_GROUP_NAME = 'Calls'
QUEUE_SLUG = 'support'
QUEUE_NAME = 'Support'
FIRST_EXTENSION = 100
WSS_PORT = 8089
HOSTED_NOTE = 'hosted_request_sent'


def _clean(value):
    return ' '.join(str(value or '').split())


def _opt_char(max_length=255, **kwargs):
    return serializers.CharField(max_length=max_length, required=False, allow_blank=True, **kwargs)


class ServerSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    fqdn = serializers.CharField(max_length=255)
    public_ip = serializers.IPAddressField(required=False, allow_blank=True, allow_null=True)
    ami_port = serializers.IntegerField(min_value=1, max_value=65535, required=False, default=5038)

    def validate_fqdn(self, value):
        value = _clean(value).lower().rstrip('.')
        value = re.sub(r'^[a-z]+://', '', value).split('/')[0]
        if not value or ' ' in value:
            raise serializers.ValidationError('Enter the server domain, e.g. pbx.example.com.')
        return value

    def validate(self, attrs):
        if not _clean(attrs.get('name')):
            attrs['name'] = 'Main PBX'
        if attrs.get('public_ip') == '':
            attrs['public_ip'] = None
        return attrs


class TrunkSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(choices=PROVIDERS, required=False, default='other')
    sip_server = serializers.CharField(max_length=255)
    sip_port = serializers.IntegerField(min_value=1, max_value=65535, required=False, default=5060)
    username = serializers.CharField(max_length=100)
    password = _opt_char()
    realm = _opt_char()
    phone_numbers = serializers.ListField(child=serializers.CharField(max_length=30), required=False, default=list)
    caller_id_number = _opt_char(30)

    def validate_sip_server(self, value):
        return _clean(value).lower()

    def validate_username(self, value):
        return _clean(value)

    def validate_phone_numbers(self, value):
        seen, cleaned = set(), []
        for number in value:
            number = re.sub(r'[\s\-()]', '', number)
            if number and number not in seen:
                seen.add(number)
                cleaned.append(number)
        return cleaned


class QueueAnswerSerializer(serializers.Serializer):
    strategy = serializers.ChoiceField(choices=STRATEGIES, required=False)
    timeout_seconds = serializers.IntegerField(min_value=5, max_value=600, required=False)
    max_wait_seconds = serializers.IntegerField(min_value=10, max_value=3600, required=False)


class DayHoursSerializer(serializers.Serializer):
    """``{"from": "09:00", "to": "18:00", "break_from": ..., "break_to": ...}`` or null."""

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError('Expected an object with "from" and "to".')
        out = {}
        for key in ('from', 'to'):
            value = _clean(data.get(key))
            if not TIME_RE.match(value):
                raise serializers.ValidationError({key: 'Use HH:MM.'})
            out[key] = value
        if out['from'] >= out['to']:
            raise serializers.ValidationError({'to': 'Must be after "from".'})
        for key in ('break_from', 'break_to'):
            value = _clean(data.get(key)) or None
            if value is not None and not TIME_RE.match(value):
                raise serializers.ValidationError({key: 'Use HH:MM.'})
            out[key] = value
        if bool(out['break_from']) != bool(out['break_to']):
            raise serializers.ValidationError({'break_to': 'Set both break times or neither.'})
        return out


class HoursSerializer(serializers.Serializer):
    timezone = serializers.CharField(max_length=50, required=False)
    schedule = serializers.DictField(child=DayHoursSerializer(allow_null=True), required=False)
    after_hours_action = serializers.ChoiceField(choices=AFTER_HOURS_ACTIONS, required=False)
    forward_number = _opt_char(30)

    def validate_timezone(self, value):
        from zoneinfo import ZoneInfo
        value = _clean(value)
        try:
            ZoneInfo(value)
        except Exception:
            raise serializers.ValidationError('Unknown timezone.')
        return value

    def validate_schedule(self, value):
        unknown = sorted(set(value) - set(HOURS_DAYS))
        if unknown:
            raise serializers.ValidationError(f'Unknown day(s): {", ".join(unknown)}.')
        return value

    def validate(self, attrs):
        if attrs.get('after_hours_action') == 'forward' and not _clean(attrs.get('forward_number')):
            raise serializers.ValidationError({'forward_number': 'A number is required to forward after hours.'})
        return attrs


class RatingSerializer(serializers.Serializer):
    review_method = serializers.ChoiceField(choices=REVIEW_METHODS, required=False)
    sms_api_key = _opt_char(allow_null=True)


class CallsAnswersSerializer(serializers.Serializer):
    hosting = serializers.ChoiceField(choices=HOSTING_CHOICES, required=False, default='byo')
    server = ServerSerializer(required=False, allow_null=True)
    trunk = TrunkSerializer(required=False, allow_null=True)
    agent_user_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    queue = QueueAnswerSerializer(required=False)
    hours = HoursSerializer(required=False)
    rating = RatingSerializer(required=False)

    def validate_agent_user_ids(self, value):
        ids = list(dict.fromkeys(value))
        known = set(User.objects.filter(id__in=ids, is_active=True).values_list('id', flat=True))
        missing = [i for i in ids if i not in known]
        if missing:
            raise serializers.ValidationError(f'Unknown or inactive user id(s): {", ".join(map(str, missing))}.')
        return ids


def _set(instance, field, value, changed):
    if getattr(instance, field) != value:
        setattr(instance, field, value)
        changed.append(field)


def _hour(value):
    hours, minutes = value.split(':')
    return int(hours) + int(minutes) / 60


def hours_to_schedule(schedule):
    """``Hours`` answer → ``PbxSettings.working_hours_schedule`` (open hours per day)."""
    out = {}
    for short, long in zip(HOURS_DAYS, DAY_NAMES):
        day = schedule.get(short)
        if not day:
            out[long] = []
            continue
        start, end = math.floor(_hour(day['from'])), math.ceil(_hour(day['to']))
        hours = list(range(start, min(end, 24)))
        if day.get('break_from') and day.get('break_to'):
            b_start, b_end = math.ceil(_hour(day['break_from'])), math.floor(_hour(day['break_to']))
            hours = [h for h in hours if not (b_start <= h < b_end)]
        out[long] = hours
    return out


def schedule_to_hours(working_hours):
    """``PbxSettings.working_hours_schedule`` → ``Hours`` (for prefill)."""
    out = {}
    for short, long in zip(HOURS_DAYS, DAY_NAMES):
        hours = sorted(h for h in (working_hours or {}).get(long, []) if isinstance(h, int))
        if not hours:
            out[short] = None
            continue
        gap = next((hours[i] for i in range(len(hours) - 1) if hours[i + 1] - hours[i] > 1), None)
        out[short] = {
            'from': f'{hours[0]:02d}:00', 'to': f'{hours[-1] + 1:02d}:00',
            'break_from': f'{gap + 1:02d}:00' if gap is not None else None,
            'break_to': f'{next(h for h in hours if h > gap):02d}:00' if gap is not None else None,
        }
    return out


def _user_payload(u):
    return {'id': u.id, 'email': u.email, 'name': u.get_full_name(), 'role': u.role, 'is_active': u.is_active}


def _install_command(pbx, request):
    from .serializers import PbxServerSerializer
    return PbxServerSerializer(pbx, context={'request': request}).data['install_command']


@register
class CallsStep(OnboardingStep):
    key = 'ip_calling'
    feature_key = 'ip_calling'
    order = 70
    serializer_class = CallsAnswersSerializer

    # ── reads ──

    def context(self, tenant, request):
        from onboarding.models import TenantOnboarding

        pbx = PbxServer.objects.order_by('-created_at').first()
        answers = TenantOnboarding.get().answers_of(self.key) or {}
        agents = list(
            UserPhoneAssignment.objects.filter(is_active=True).values_list('user_id', flat=True).distinct()
        )
        return {
            'server': {
                'id': pbx.id, 'name': pbx.name, 'fqdn': pbx.fqdn, 'status': pbx.status,
                'public_ip': pbx.public_ip, 'ami_port': pbx.ami_port,
                'last_seen_at': pbx.last_seen_at.isoformat() if pbx.last_seen_at else None,
                'install_command': _install_command(pbx, request),
            } if pbx else None,
            'trunks': Trunk.objects.count(),
            'users': [_user_payload(u) for u in User.objects.filter(is_active=True).order_by('email')],
            'hosted_request_sent': answers.get('hosting') == 'hosted',
            'agents': agents,
            'has_sip_configuration': SipConfiguration.objects.exists(),
            'has_queue': Queue.objects.filter(slug=QUEUE_SLUG).exists(),
            'telegram_configured': bool(
                getattr(django_settings, 'TELEGRAM_BOT_TOKEN', '') and getattr(django_settings, 'TELEGRAM_CHAT_ID', '')
            ),
        }

    def prefill(self, tenant, state):
        answers = (state.answers_of(self.key) if state else {}) or {}
        pbx = PbxServer.objects.order_by('-created_at').first()
        trunk = Trunk.objects.order_by('-is_default', 'id').first()
        config = self._sip_configuration()
        pbx_settings = PbxSettings.objects.filter(sip_configuration=config).first() if config else None
        queue = Queue.objects.filter(slug=QUEUE_SLUG).first()
        return {
            'hosting': answers.get('hosting') or 'byo',
            'server': {
                'name': pbx.name, 'fqdn': pbx.fqdn, 'public_ip': pbx.public_ip, 'ami_port': pbx.ami_port,
            } if pbx else None,
            'trunk': {
                'provider': next((k for k, v in PROVIDER_LABELS.items() if v.lower() == (trunk.provider or '').lower()), 'other'),
                'sip_server': trunk.sip_server, 'sip_port': trunk.sip_port, 'username': trunk.username,
                'password': '', 'realm': trunk.realm, 'phone_numbers': list(trunk.phone_numbers or []),
                'caller_id_number': trunk.caller_id_number,
            } if trunk else None,
            'agent_user_ids': list(
                UserPhoneAssignment.objects.filter(is_active=True).order_by('extension')
                .values_list('user_id', flat=True).distinct()
            ),
            'queue': {
                'strategy': queue.strategy if queue and queue.strategy in STRATEGIES else 'ringall',
                'timeout_seconds': queue.timeout_seconds if queue else 30,
                'max_wait_seconds': queue.max_wait_seconds if queue else 300,
            },
            'hours': {
                'timezone': pbx_settings.timezone if pbx_settings else 'Asia/Tbilisi',
                'schedule': schedule_to_hours(pbx_settings.working_hours_schedule) if pbx_settings and pbx_settings.working_hours_schedule
                else {d: ({'from': '09:00', 'to': '18:00', 'break_from': None, 'break_to': None} if d not in ('sat', 'sun') else None) for d in HOURS_DAYS},
                'after_hours_action': pbx_settings.after_hours_action if pbx_settings else 'announcement',
                'forward_number': pbx_settings.forward_number if pbx_settings else '',
            },
            'rating': {
                'review_method': pbx_settings.review_method if pbx_settings else 'none',
                'sms_api_key': None,
            },
        }

    # ── writes ──

    def apply(self, tenant, answers, user):
        result = empty_result()
        notes = result['notes']
        changed = []
        hosted = answers.get('hosting') == 'hosted'

        if hosted:
            self._request_hosting(tenant, answers, user, result)
        elif answers.get('server'):
            self._apply_server(answers['server'], changed, result)

        trunk = self._apply_trunk(answers['trunk'], changed, result) if answers.get('trunk') else None

        config = None
        if 'agent_user_ids' in answers or 'hours' in answers or 'rating' in answers:
            config = self._ensure_sip_configuration(trunk, user, changed, result)

        agents = []
        if 'agent_user_ids' in answers:
            agents = self._apply_agents(config, answers['agent_user_ids'], trunk, changed, result)

        if 'queue' in answers:
            self._apply_queue(answers['queue'], agents if 'agent_user_ids' in answers else None, changed, result)

        if 'hours' in answers or 'rating' in answers:
            pbx_settings, created = PbxSettings.objects.get_or_create(sip_configuration=config)
            if created:
                result['created']['pbx_settings'] = 1
            self._apply_hours(pbx_settings, answers.get('hours', {}), changed)
            self._apply_rating(pbx_settings, answers.get('rating', {}), changed)

        result['updated'] = changed
        return result

    def seed_defaults(self, tenant):
        return None

    # ── helpers ──

    @staticmethod
    def _request_hosting(tenant, answers, user, result):
        from tenants.telegram_notifications import notify_pbx_hosting_request

        sent = False
        try:
            sent = bool(notify_pbx_hosting_request(
                tenant,
                admin_email=getattr(user, 'email', '') or '',
                requested_server=answers.get('server') or None,
                agent_count=len(answers.get('agent_user_ids') or []),
            ))
        except Exception:  # noqa: BLE001 - the request is recorded in the answers either way
            logger.exception('Hosted PBX request notification failed for %s', tenant.schema_name)
        result['created']['hosted_request'] = 1
        result['notes'].append(HOSTED_NOTE if sent else f'{HOSTED_NOTE}=false')
        result['notes'].append(
            'EchoDesk will host your PBX: the team has been notified and will contact '
            f'{getattr(user, "email", "") or tenant.admin_email}.'
            if sent else
            'EchoDesk will host your PBX: your request is recorded; the team will contact you.'
        )

    @staticmethod
    def _apply_server(server, changed, result):
        existing = PbxServer.objects.filter(fqdn__iexact=server['fqdn']).first()
        if existing is None:
            other = PbxServer.objects.exclude(status=PbxServer.STATUS_REVOKED).first()
            if other is not None:
                result['notes'].append(
                    f'A PBX server is already registered ({other.fqdn}); {server["fqdn"]} was not added. '
                    'Manage servers in Settings → PBX server.'
                )
                return other
            pbx = PbxServer(
                name=server['name'], fqdn=server['fqdn'], public_ip=server.get('public_ip') or None,
                ami_port=server.get('ami_port') or 5038,
                realtime_db_host='', realtime_db_name='', realtime_db_user='',
            )
            pbx.save()
            result['created']['pbx_servers'] = 1
            # Same as PbxServerViewSet.perform_create: provision the realtime DB;
            # on failure the row is marked error with the reason (own savepoint so
            # the rest of the step survives).
            from . import pbx_provisioning
            try:
                with transaction.atomic():
                    pbx_provisioning.provision_and_bootstrap(pbx)
            except Exception as exc:  # noqa: BLE001
                logger.exception('PBX provisioning failed for %s', pbx.fqdn)
                pbx.refresh_from_db()
                pbx.status = pbx.STATUS_ERROR
                pbx.notes = f'Provisioning failed: {exc}'[:1000]
                pbx.save(update_fields=['status', 'notes', 'updated_at'])
                result['notes'].append(f'PBX server {pbx.fqdn} was registered but provisioning failed: {exc}'[:300])
            return pbx

        updates = []
        _set(existing, 'name', server['name'], updates)
        if server.get('public_ip'):
            _set(existing, 'public_ip', server['public_ip'], updates)
        if server.get('ami_port'):
            _set(existing, 'ami_port', server['ami_port'], updates)
        if updates:
            existing.save(update_fields=updates + ['updated_at'])
            changed.append('pbx_server')
        return existing

    @staticmethod
    def _apply_trunk(data, changed, result):
        trunk = Trunk.objects.filter(sip_server__iexact=data['sip_server'], username=data['username']).first()
        label = PROVIDER_LABELS.get(data.get('provider') or 'other', 'SIP trunk')
        caller_id = data.get('caller_id_number') or (data['phone_numbers'][0] if data.get('phone_numbers') else '')
        if trunk is None:
            name = label
            if Trunk.objects.filter(name=name).exists():
                name = f'{label} ({data["username"]})'[:100]
            trunk = Trunk.objects.create(
                name=name,
                provider=label if data.get('provider') != 'other' else '',
                sip_server=data['sip_server'],
                sip_port=data.get('sip_port') or 5060,
                username=data['username'],
                password=data.get('password') or '',
                realm=data.get('realm') or '',
                register=True,
                caller_id_number=caller_id,
                phone_numbers=list(data.get('phone_numbers') or []),
                is_active=True,
                is_default=not Trunk.objects.filter(is_default=True).exists(),
            )
            result['created']['trunks'] = 1
            return trunk

        updates = []
        _set(trunk, 'sip_port', data.get('sip_port') or trunk.sip_port, updates)
        if data.get('password'):
            if trunk.password != data['password']:
                trunk.password = data['password']
                updates.append('password')
        if 'realm' in data:
            _set(trunk, 'realm', data['realm'] or '', updates)
        if data.get('provider') and data['provider'] != 'other':
            _set(trunk, 'provider', label, updates)
        if caller_id:
            _set(trunk, 'caller_id_number', caller_id, updates)
        numbers = list(trunk.phone_numbers or [])
        for number in data.get('phone_numbers') or []:
            if number not in numbers:
                numbers.append(number)
        _set(trunk, 'phone_numbers', numbers, updates)
        if not trunk.is_active:
            _set(trunk, 'is_active', True, updates)
        if updates:
            trunk.save(update_fields=updates + ['updated_at'])
            changed.append('trunk')
        return trunk

    @staticmethod
    def _sip_configuration():
        return (
            SipConfiguration.objects.filter(name=SIP_CONFIG_NAME).first()
            or SipConfiguration.objects.filter(is_default=True).first()
        )

    def _ensure_sip_configuration(self, trunk, user, changed, result):
        pbx = PbxServer.objects.exclude(status=PbxServer.STATUS_REVOKED).order_by('-created_at').first()
        config = SipConfiguration.objects.filter(name=SIP_CONFIG_NAME).first()
        trunk = trunk or Trunk.objects.order_by('-is_default', 'id').first()
        if config is None:
            default = SipConfiguration.objects.filter(is_default=True).first()
            if pbx is None and default is not None:
                return default
            config = SipConfiguration.objects.create(
                name=SIP_CONFIG_NAME,
                sip_server=pbx.fqdn if pbx else '',
                sip_port=WSS_PORT,
                websocket_path='/ws',
                username=trunk.username if trunk else '',
                password=trunk.password if trunk else '',
                realm=trunk.realm if trunk else '',
                phone_number=(trunk.phone_numbers[0] if trunk and trunk.phone_numbers else '') or (trunk.caller_id_number if trunk else ''),
                is_active=True,
                is_default=default is None,
                created_by=user,
            )
            result['created']['sip_configurations'] = 1
            if pbx is None:
                result['notes'].append(
                    'The softphone connection has no server yet; it is filled in once the PBX server is registered.'
                )
            return config

        updates = []
        if pbx is not None:
            _set(config, 'sip_server', pbx.fqdn, updates)
        if trunk is not None:
            if not config.username:
                _set(config, 'username', trunk.username, updates)
            if not config.password and trunk.password:
                config.password = trunk.password
                updates.append('password')
            if not config.phone_number and trunk.phone_numbers:
                _set(config, 'phone_number', trunk.phone_numbers[0], updates)
        if not config.is_active:
            _set(config, 'is_active', True, updates)
        if updates:
            config.save(update_fields=updates + ['updated_at'])
            changed.append('sip_configuration')
        return config

    @staticmethod
    def _apply_agents(config, user_ids, trunk, changed, result):
        users = {u.id: u for u in User.objects.filter(id__in=user_ids)}
        taken = set()
        for ext in UserPhoneAssignment.objects.values_list('extension', flat=True):
            if ext and ext.isdigit():
                taken.add(int(ext))
        trunk = trunk or Trunk.objects.order_by('-is_default', 'id').first()
        display_number = config.phone_number or (trunk.phone_numbers[0] if trunk and trunk.phone_numbers else '')
        assigned = []
        created_count = 0
        next_ext = FIRST_EXTENSION
        for user_id in user_ids:
            member = users.get(user_id)
            if member is None:
                continue
            assignment = UserPhoneAssignment.objects.filter(user=member).order_by('-is_primary', 'id').first()
            if assignment is not None:
                if not assignment.is_active:
                    assignment.is_active = True
                    assignment.save(update_fields=['is_active', 'updated_at'])
                    changed.append('user_phone_assignments')
                assigned.append(assignment)
                continue
            while next_ext in taken:
                next_ext += 1
            assignment = UserPhoneAssignment.objects.create(
                user=member,
                sip_configuration=config,
                extension=str(next_ext),
                extension_password=secrets.token_urlsafe(12),
                phone_number=display_number or str(next_ext),
                display_name=member.get_full_name() or member.email,
                is_primary=True,
                is_active=True,
            )
            taken.add(next_ext)
            created_count += 1
            assigned.append(assignment)
        if created_count:
            result['created']['user_phone_assignments'] = created_count
        return assigned

    @staticmethod
    def _apply_queue(data, agents, changed, result):
        group = TenantGroup.objects.filter(name=CALLS_GROUP_NAME).first()
        if group is None:
            group = TenantGroup.objects.create(name=CALLS_GROUP_NAME, description='Users who answer incoming calls')
            result['created']['groups'] = 1
        if agents is not None:
            wanted = {a.user_id for a in agents}
            current = set(group.members.values_list('id', flat=True))
            missing = wanted - current
            if missing:
                group.members.add(*missing)
                changed.append('queue_members')

        queue = Queue.objects.filter(slug=QUEUE_SLUG).first()
        if queue is None:
            queue = Queue.objects.create(
                name=QUEUE_NAME, slug=QUEUE_SLUG, group=group,
                strategy=data.get('strategy') or 'ringall',
                timeout_seconds=data.get('timeout_seconds') or 30,
                max_wait_seconds=data.get('max_wait_seconds') or 300,
                is_active=True,
                is_default=not Queue.objects.filter(is_default=True).exists(),
            )
            result['created']['queues'] = 1
        else:
            updates = []
            if queue.group_id != group.id and not queue.group.members.exists():
                _set(queue, 'group', group, updates)
            for field in ('strategy', 'timeout_seconds', 'max_wait_seconds'):
                if field in data:
                    _set(queue, field, data[field], updates)
            if not queue.is_active:
                _set(queue, 'is_active', True, updates)
            if updates:
                queue.save()
                changed.append('queue')

        created_routes = 0
        for trunk in Trunk.objects.filter(is_active=True):
            for did in trunk.phone_numbers or []:
                if InboundRoute.objects.filter(did=did).exists():
                    continue
                InboundRoute.objects.create(
                    did=did, trunk=trunk, destination_type='queue', destination_queue=queue, is_active=True,
                )
                created_routes += 1
        if created_routes:
            result['created']['inbound_routes'] = created_routes
        return queue

    @staticmethod
    def _apply_hours(pbx_settings, hours, changed):
        updates = []
        if 'timezone' in hours:
            _set(pbx_settings, 'timezone', hours['timezone'], updates)
        if 'schedule' in hours:
            schedule = hours_to_schedule(hours['schedule'])
            _set(pbx_settings, 'working_hours_schedule', schedule, updates)
            _set(pbx_settings, 'working_hours_enabled', any(schedule.values()), updates)
        if 'after_hours_action' in hours:
            _set(pbx_settings, 'after_hours_action', hours['after_hours_action'], updates)
            _set(pbx_settings, 'voicemail_enabled', hours['after_hours_action'] == 'voicemail', updates)
        if 'forward_number' in hours:
            _set(pbx_settings, 'forward_number', hours['forward_number'] or '', updates)
        if updates:
            pbx_settings.save(update_fields=updates + ['updated_at'])
            changed.append('working_hours')

    @staticmethod
    def _apply_rating(pbx_settings, rating, changed):
        updates = []
        if 'review_method' in rating:
            _set(pbx_settings, 'review_method', rating['review_method'], updates)
        if rating.get('sms_api_key'):
            if pbx_settings.sms_api_key != rating['sms_api_key']:
                pbx_settings.sms_api_key = rating['sms_api_key']
                updates.append('sms_api_key')
        if updates:
            pbx_settings.save(update_fields=updates + ['updated_at'])
            changed.append('rating')
