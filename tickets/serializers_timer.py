"""Serializers for the personal ticket timer (A14)."""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import TicketTimeEntry
from .timer import user_brief


class TimeEntryUserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    initials = serializers.CharField()


class TicketTimeEntrySerializer(serializers.ModelSerializer):
    """``Entry``: ``duration_seconds`` is 0 while running; clients tick from
    ``started_at`` against ``server_now``."""
    ticket_id = serializers.IntegerField(read_only=True)
    user = serializers.SerializerMethodField()
    is_running = serializers.BooleanField(read_only=True)

    class Meta:
        model = TicketTimeEntry
        fields = ['id', 'ticket_id', 'user', 'started_at', 'ended_at',
                  'duration_seconds', 'is_running']
        read_only_fields = fields

    @extend_schema_field(TimeEntryUserSerializer(allow_null=True))
    def get_user(self, obj):
        u = obj.user
        if u is None:
            return None
        return user_brief(u.pk, u.first_name, u.last_name, u.email)


class TimerStartRequestSerializer(serializers.Serializer):
    switch = serializers.BooleanField(
        required=False, default=False,
        help_text='Stop the timer running on another ticket and start this one.',
    )


class TimerUserTotalSerializer(serializers.Serializer):
    user = TimeEntryUserSerializer(allow_null=True)
    seconds = serializers.IntegerField()


class TicketTimerStateSerializer(serializers.Serializer):
    running = TicketTimeEntrySerializer(allow_null=True)
    my_total_seconds = serializers.IntegerField()
    total_seconds = serializers.IntegerField()
    server_now = serializers.DateTimeField()
    by_user = TimerUserTotalSerializer(many=True)
    entries = TicketTimeEntrySerializer(many=True)


class CurrentTimerSerializer(serializers.Serializer):
    running = TicketTimeEntrySerializer(allow_null=True)
    server_now = serializers.DateTimeField()


class TimerRunningBriefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    ticket_id = serializers.IntegerField()
    ticket_title = serializers.CharField(allow_null=True)
    started_at = serializers.DateTimeField()


class TimerConflictSerializer(serializers.Serializer):
    """409 body: ``code`` is ``timer_running`` (with ``running``),
    ``ticket_closed`` or ``timer_not_running``."""
    code = serializers.ChoiceField(choices=['timer_running', 'ticket_closed', 'timer_not_running'])
    running = TimerRunningBriefSerializer(required=False, allow_null=True)
