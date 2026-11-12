"""
Workspace step (key ``workspace``, always shown, order 0).

Answers schema::

    {
      "company_name": str (1-100),
      "preferred_language": "ka" | "en" | "ru",
      "industry": "salon" | "clinic" | "real_estate" | "shop" | "call_centre"
                  | "logistics" | "services" | "other",
      "departments": [str],                      # names; existing ones matched case-insensitively
      "team": [
        {"email": str, "first_name": str, "last_name": str,
         "role": "admin" | "manager" | "agent",
         "department": str | null}               # a department name (created if missing)
      ]
    }

``apply`` writes ``Tenant.name`` / ``Tenant.preferred_language``, stores the
industry on ``TenantOnboarding.industry``, creates ``users.Department`` rows
and invites the team through the same code path as ``POST /api/users/``
(:func:`users.invitations.invite_user`: temporary password, forced password
change, invitation e-mail task). E-mails that already exist (or belong to the
acting admin) are skipped and listed in ``result["notes"]``. The logo is
uploaded by the web through ``/api/tenant-settings/upload-logo/``.

``prefill``: company name + language from the Tenant, the stored industry,
existing department names. ``context``: existing users (id, email, name,
role, is_active), departments (id, name), industry choices with en/ka
labels, language and role choices.
"""
from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from users.models import Department

from ..base import OnboardingStep, empty_result
from ..models import INDUSTRY_KEYS, TenantOnboarding, industry_choices
from ..registry import register

User = get_user_model()

LANGUAGES = [('ka', 'ქართული'), ('en', 'English'), ('ru', 'Русский')]
LANGUAGE_KEYS = [key for key, _ in LANGUAGES]

# Wizard role → ``User.role``. "admin" is the admin role (``User.is_admin``:
# manage_users / manage_settings / manage_groups); it does not grant Django
# ``is_superuser``. Granting it is reserved to superusers, like ``POST /api/users/``.
TEAM_ROLES = ['admin', 'manager', 'agent']
ROLE_MAP = {'admin': 'admin', 'manager': 'manager', 'agent': 'agent'}


def _clean_name(value):
    return ' '.join(str(value or '').split())


class TeamMemberSerializer(serializers.Serializer):
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default='')
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default='')
    role = serializers.ChoiceField(choices=TEAM_ROLES)
    department = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True, default=None)

    def validate_email(self, value):
        return User.objects.normalize_email(value).strip().lower()

    def validate_department(self, value):
        value = _clean_name(value)
        return value or None


class WorkspaceAnswersSerializer(serializers.Serializer):
    company_name = serializers.CharField(max_length=100)
    preferred_language = serializers.ChoiceField(choices=LANGUAGE_KEYS)
    industry = serializers.ChoiceField(choices=INDUSTRY_KEYS)
    departments = serializers.ListField(
        child=serializers.CharField(max_length=100, allow_blank=True), required=False, default=list,
    )
    team = TeamMemberSerializer(many=True, required=False, default=list)

    def validate_company_name(self, value):
        value = _clean_name(value)
        if not value:
            raise serializers.ValidationError('Company name cannot be blank.')
        return value

    def validate_departments(self, value):
        seen, cleaned = set(), []
        for name in value:
            name = _clean_name(name)
            if name and name.lower() not in seen:
                seen.add(name.lower())
                cleaned.append(name)
        return cleaned

    def validate_team(self, value):
        seen = set()
        for row in value:
            if row['email'] in seen:
                raise serializers.ValidationError(f'{row["email"]} is listed more than once.')
            seen.add(row['email'])
        request = self.context.get('request')
        actor = getattr(request, 'user', None)
        if actor is not None and value:
            # Same role guard as POST /api/users/ (admin role only from a superuser,
            # no role above the actor's own for non-staff).
            from users.views import _guard_role_change
            for row in value:
                try:
                    _guard_role_change(actor, None, ROLE_MAP[row['role']])
                except PermissionDenied as exc:
                    raise serializers.ValidationError(f'{row["email"]}: {exc.detail}')
        return value


@register
class WorkspaceStep(OnboardingStep):
    key = 'workspace'
    feature_key = None
    order = 0
    serializer_class = WorkspaceAnswersSerializer

    def context(self, tenant, request):
        users = [
            {
                'id': u.id,
                'email': u.email,
                'name': u.get_full_name(),
                'role': u.role,
                'is_active': u.is_active,
            }
            for u in User.objects.order_by('email').only('id', 'email', 'first_name', 'last_name', 'role', 'is_active')
        ]
        return {
            'users': users,
            'departments': [{'id': d.id, 'name': d.name} for d in Department.objects.order_by('name')],
            'industries': industry_choices(),
            'languages': [{'key': key, 'label': label} for key, label in LANGUAGES],
            'roles': list(TEAM_ROLES),
        }

    def prefill(self, tenant, state):
        return {
            'company_name': tenant.name if tenant else '',
            'preferred_language': (tenant.preferred_language if tenant else None) or 'ka',
            'industry': state.industry if state else None,
            'departments': list(Department.objects.order_by('name').values_list('name', flat=True)),
            'team': [],
        }

    def apply(self, tenant, answers, user):
        from users.invitations import invite_user

        result = empty_result()
        notes = result['notes']

        # Tenant name / language (public-schema row, reachable from the tenant schema).
        changed = []
        if tenant.name != answers['company_name']:
            tenant.name = answers['company_name']
            changed.append('name')
        if tenant.preferred_language != answers['preferred_language']:
            tenant.preferred_language = answers['preferred_language']
            changed.append('preferred_language')
        if changed:
            tenant.save(update_fields=changed)
            result['updated'].extend(changed)

        # Industry on the onboarding row.
        state = TenantOnboarding.get()
        if state.industry != answers['industry']:
            state.industry = answers['industry']
            state.save(update_fields=['industry', 'updated_at'])
            result['updated'].append('industry')

        # Departments: match existing names case-insensitively, create the rest.
        wanted = list(answers.get('departments') or [])
        for row in answers.get('team') or []:
            if row.get('department') and row['department'].lower() not in {w.lower() for w in wanted}:
                wanted.append(row['department'])
        departments = {}
        created_departments = 0
        for name in wanted:
            dept = Department.objects.filter(name__iexact=name).first()
            if dept is None:
                dept = Department.objects.create(name=name)
                created_departments += 1
            departments[name.lower()] = dept

        # Team: invite new e-mails exactly like POST /api/users/; skip the rest.
        created_users = []
        actor_email = (user.email or '').lower() if user else ''
        for row in answers.get('team') or []:
            email = row['email']
            if email == actor_email:
                notes.append(f'{email}: that is you, skipped.')
                continue
            if User.objects.filter(email__iexact=email).exists():
                notes.append(f'{email}: already a member, skipped.')
                continue
            dept = departments.get(row['department'].lower()) if row.get('department') else None
            member = User.objects.create(
                email=email,
                first_name=row.get('first_name') or '',
                last_name=row.get('last_name') or '',
                role=ROLE_MAP[row['role']],
                department=dept,
            )
            invite_user(member, invited_by=user, tenant=tenant)
            created_users.append(member.id)

        result['created'] = {
            'departments': created_departments,
            'users': len(created_users),
            'user_ids': created_users,
        }
        return result
