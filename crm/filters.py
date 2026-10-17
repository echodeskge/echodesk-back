"""django-filter FilterSets for the calls API."""

import django_filters
from django.db.models import Q

from .call_status import missed_q
from .models import CallLog, UserPhoneAssignment


class CharInFilter(django_filters.BaseInFilter, django_filters.CharFilter):
    """Comma separated list of strings."""


class CallLogFilter(django_filters.FilterSet):
    """Filters for ``GET /api/call-logs/``.

    ``status``, ``direction`` and ``call_type`` behave as before (exact
    match). The rest are additive:

    * ``status__in`` - comma separated statuses (``missed,no_answer``)
    * ``handled_by`` - user id of the agent who handled the call
    * ``mine`` - ``true``: calls handled by, or transferred to, the caller
    * ``started_after`` / ``started_before`` - ISO datetime or date bounds on
      ``started_at`` (inclusive / exclusive)
    * ``missed`` - ``true``: inbound calls nobody picked up (status
      missed/no_answer, or a legacy ``ended`` row never answered);
      ``false``: everything else
    * ``client`` / ``social_client`` - CRM or social client id
    """

    status = django_filters.ChoiceFilter(choices=CallLog.STATUS_CHOICES)
    status__in = CharInFilter(
        field_name='status', lookup_expr='in',
        help_text='Comma separated statuses, e.g. missed,no_answer',
    )
    handled_by = django_filters.NumberFilter(field_name='handled_by_id')
    mine = django_filters.BooleanFilter(method='filter_mine')
    started_after = django_filters.DateTimeFilter(field_name='started_at', lookup_expr='gte')
    started_before = django_filters.DateTimeFilter(field_name='started_at', lookup_expr='lt')
    missed = django_filters.BooleanFilter(method='filter_missed')
    client = django_filters.NumberFilter(field_name='client_id')
    social_client = django_filters.NumberFilter(field_name='social_client_id')

    class Meta:
        model = CallLog
        fields = [
            'status', 'status__in', 'direction', 'call_type', 'handled_by', 'mine',
            'started_after', 'started_before', 'missed', 'client', 'social_client',
        ]

    def filter_mine(self, queryset, name, value):
        if value is None:
            return queryset
        user = getattr(self.request, 'user', None)
        if user is None or not user.is_authenticated:
            return queryset.none() if value else queryset
        mine = Q(handled_by=user) | Q(transferred_to_user=user)
        return queryset.filter(mine) if value else queryset.exclude(mine)

    def filter_missed(self, queryset, name, value):
        if value is None:
            return queryset
        return queryset.filter(missed_q()) if value else queryset.exclude(missed_q())


class UserPhoneAssignmentFilter(django_filters.FilterSet):
    """``?is_active=true`` (used by the web transfer picker), ``user``,
    ``sip_configuration``, ``is_primary``."""

    class Meta:
        model = UserPhoneAssignment
        fields = ['is_active', 'is_primary', 'user', 'sip_configuration']
