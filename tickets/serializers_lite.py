"""Compact read serializers for the redesigned tickets screens (BE-07).

Kept apart from ``tickets/serializers.py`` so the read API can grow without
touching the write serializers.
"""
from rest_framework import serializers

from .models import Board


class BoardLiteSerializer(serializers.ModelSerializer):
    """``GET boards/?lite=1`` row (A1): what the sidebar and the board switcher
    need, with ``open_count`` and ``columns_count`` from scalar subqueries."""
    open_count = serializers.IntegerField(read_only=True, default=0)
    columns_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Board
        fields = ['id', 'name', 'color', 'position', 'is_default', 'open_count', 'columns_count']
        read_only_fields = fields


class TicketViewCountsSerializer(serializers.Serializer):
    """``GET tickets/view_counts/`` (A5)."""
    mine = serializers.IntegerField()
    unassigned = serializers.IntegerField()
    urgent = serializers.IntegerField()
    due_this_week = serializers.IntegerField()


class TicketColumnCountsSerializer(serializers.Serializer):
    """``GET tickets/column_counts/`` (A5b): ``{"counts": {"<column id>": n}}``."""
    counts = serializers.DictField(child=serializers.IntegerField())


class TicketNavigationSerializer(serializers.Serializer):
    """``GET tickets/{id}/navigation/`` (A6)."""
    index = serializers.IntegerField(
        allow_null=True,
        help_text='1-based position of the ticket in the filtered order ("x of n" shows index of total); '
                  'null when the ticket does not match the filters.',
    )
    total = serializers.IntegerField(help_text='Number of tickets in the order the neighbours come from.')
    prev_id = serializers.IntegerField(allow_null=True)
    next_id = serializers.IntegerField(allow_null=True)
