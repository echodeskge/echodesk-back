"""Call statistics endpoints for the PBX management panel.

All aggregation is performed DB-side via ``annotate``/``aggregate`` plus
``TruncDate``/``Extract`` — there are no Python loops over large
querysets. All endpoints are gated by the ``ip_calling`` subscription
feature and need a stats viewer (PBX admin or ``view_reports``, see
``crm.permissions``); a user may always read their own timeline.

"Answered" and "missed" come from ``crm.call_status`` so every stats
surface agrees (an ``ended`` call only counts as answered when it was
actually picked up).
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta
from typing import Tuple

from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import ExtractHour, ExtractIsoWeekDay, TruncDate
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from tenants.permissions import require_subscription_feature

from .call_status import answered_q, missed_q
from .models import CallLog, InboundRoute
from .permissions import CanViewCallStats, can_view_call_stats


# ---------------------------------------------------------------------------
# Response schemas (documentation only; the views build plain dicts)
# ---------------------------------------------------------------------------

_UserStatsRow = inline_serializer(
    name="CallStatsUserRow",
    many=True,
    fields={
        'user_id': serializers.IntegerField(),
        'user_name': serializers.CharField(),
        'user_email': serializers.CharField(),
        'answered_count': serializers.IntegerField(),
        'missed_count': serializers.IntegerField(),
        'outbound_count': serializers.IntegerField(),
        'total_talk_seconds': serializers.IntegerField(),
        'avg_talk_seconds': serializers.IntegerField(),
    },
)
UsersStatsResponse = inline_serializer(
    name='CallStatsUsersResponse',
    fields={
        'month': serializers.CharField(),
        'start': serializers.DateTimeField(),
        'end': serializers.DateTimeField(),
        'results': _UserStatsRow,
    },
)
UserTimelineResponse = inline_serializer(
    name='CallStatsUserTimelineResponse',
    fields={
        'user_id': serializers.IntegerField(),
        'month': serializers.CharField(),
        'start': serializers.DateTimeField(),
        'end': serializers.DateTimeField(),
        'results': inline_serializer(
            name='CallStatsTimelineDay',
            many=True,
            fields={
                'day': serializers.DateField(allow_null=True),
                'total_calls': serializers.IntegerField(),
                'answered_count': serializers.IntegerField(),
                'missed_count': serializers.IntegerField(),
                'outbound_count': serializers.IntegerField(),
                'total_talk_seconds': serializers.IntegerField(),
            },
        ),
    },
)
QueueStatsResponse = inline_serializer(
    name='CallStatsQueueResponse',
    fields={
        'queue_id': serializers.IntegerField(),
        'range': serializers.CharField(),
        'start': serializers.DateTimeField(),
        'end': serializers.DateTimeField(),
        'matched_dids': serializers.ListField(child=serializers.CharField()),
        'total_calls': serializers.IntegerField(),
        'answered_count': serializers.IntegerField(),
        'abandoned_count': serializers.IntegerField(),
        'avg_wait_seconds': serializers.IntegerField(),
        'peak_hour': serializers.IntegerField(allow_null=True),
    },
)
OverviewStatsResponse = inline_serializer(
    name='CallStatsOverviewResponse',
    fields={
        'range': serializers.CharField(),
        'start': serializers.DateTimeField(),
        'end': serializers.DateTimeField(),
        'total_calls': serializers.IntegerField(),
        'answered_calls': serializers.IntegerField(),
        'missed_calls': serializers.IntegerField(),
        'inbound_calls': serializers.IntegerField(),
        'outbound_calls': serializers.IntegerField(),
        'avg_talk_seconds': serializers.IntegerField(),
        'busiest_hour': serializers.IntegerField(allow_null=True, help_text='0-23'),
        'busiest_weekday': serializers.IntegerField(allow_null=True, help_text='0=Monday .. 6=Sunday'),
        'top_5_users': inline_serializer(
            name='CallStatsTopUser',
            many=True,
            fields={
                'user_id': serializers.IntegerField(),
                'user_name': serializers.CharField(),
                'user_email': serializers.CharField(),
                'answered_count': serializers.IntegerField(),
                'outbound_count': serializers.IntegerField(),
                'total_talk_seconds': serializers.IntegerField(),
            },
        ),
    },
)
_ERRORS = {400: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_month(month_str: str | None) -> Tuple[datetime, datetime] | None:
    """Parse a ``YYYY-MM`` string into an inclusive/exclusive datetime range.

    Returns ``(start_dt, end_dt)`` as aware datetimes in the current timezone,
    where ``start_dt`` is the first instant of the month and ``end_dt`` is the
    first instant of the following month. Returns ``None`` on malformed input.
    """
    if not month_str:
        return None
    try:
        year_str, mon_str = month_str.split('-', 1)
        year = int(year_str)
        month = int(mon_str)
        if not (1 <= month <= 12):
            return None
    except (ValueError, TypeError):
        return None

    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(date(year, month, 1), time.min), tz)
    last_day = calendar.monthrange(year, month)[1]
    end_of_month = date(year, month, last_day)
    # Exclusive upper bound — first instant of the next day.
    end_dt = timezone.make_aware(datetime.combine(end_of_month + timedelta(days=1), time.min), tz)
    return start_dt, end_dt


def _parse_range(range_str: str | None) -> Tuple[datetime, datetime]:
    """Parse ``range`` query param (today|week|month) into a datetime range.

    Mirrors the period parsing used by ``CallLogViewSet.statistics``.
    Defaults to ``month`` when unspecified or unrecognised.
    """
    now = timezone.now()
    if range_str == 'today':
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif range_str == 'week':
        start = now - timedelta(days=7)
    else:  # month or default
        start = now - timedelta(days=30)
    return start, now


def _answered_durations_filter() -> Q:
    """Filter for answered calls with a non-null duration — used for talk time."""
    return answered_q() & Q(duration__isnull=False)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@extend_schema(
    tags=['Call Statistics'],
    summary="Per-user call rollup for a month",
    description=(
        "Returns a per-user aggregate of answered, missed, and outbound call "
        "counts plus total and average talk-time for the specified month. "
        "Ordered by total talk-time descending."
    ),
    parameters=[
        OpenApiParameter(
            name='month',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description='Month in YYYY-MM format (defaults to the current month)',
            required=False,
        ),
    ],
    responses={200: UsersStatsResponse, **_ERRORS},
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewCallStats])
@require_subscription_feature('ip_calling')
def users_stats(request):
    """Per-user call rollup for a month."""
    month_str = request.query_params.get('month')
    if month_str:
        parsed = _parse_month(month_str)
        if parsed is None:
            return Response(
                {'error': "Invalid month format. Use YYYY-MM (e.g. 2026-04)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        start, end = parsed
    else:
        now = timezone.now()
        parsed = _parse_month(now.strftime('%Y-%m'))
        assert parsed is not None  # Guaranteed by strftime above.
        start, end = parsed

    queryset = (
        CallLog.objects
        .filter(started_at__gte=start, started_at__lt=end, handled_by__isnull=False)
        .values('handled_by')
        .annotate(
            answered_count=Count('id', filter=answered_q() & Q(direction='inbound')),
            missed_count=Count('id', filter=missed_q()),
            outbound_count=Count('id', filter=Q(direction='outbound')),
            total_talk_seconds=Sum('duration', filter=_answered_durations_filter()),
            avg_talk_seconds=Avg('duration', filter=_answered_durations_filter()),
            user_first_name=F('handled_by__first_name'),
            user_last_name=F('handled_by__last_name'),
            user_email=F('handled_by__email'),
        )
        .order_by('-total_talk_seconds')
    )

    def _to_seconds(d):
        if d is None:
            return 0
        # ``Sum``/``Avg`` over a DurationField returns a timedelta on Postgres.
        if isinstance(d, timedelta):
            return int(d.total_seconds())
        # Some backends return microseconds as an int.
        try:
            return int(d) // 1_000_000
        except (TypeError, ValueError):
            return 0

    data = []
    for row in queryset:
        full_name = f"{row['user_first_name'] or ''} {row['user_last_name'] or ''}".strip()
        data.append({
            'user_id': row['handled_by'],
            'user_name': full_name or row['user_email'],
            'user_email': row['user_email'],
            'answered_count': row['answered_count'],
            'missed_count': row['missed_count'],
            'outbound_count': row['outbound_count'],
            'total_talk_seconds': _to_seconds(row['total_talk_seconds']),
            'avg_talk_seconds': _to_seconds(row['avg_talk_seconds']),
        })

    return Response({
        'month': month_str or start.strftime('%Y-%m'),
        'start': start.isoformat(),
        'end': end.isoformat(),
        'results': data,
    })


@extend_schema(
    tags=['Call Statistics'],
    summary="Day-bucketed call timeline for a single user",
    description=(
        "Returns per-day counts and total talk-time for the specified user "
        "over the specified month. Useful for rendering a line chart on the "
        "per-user detail page."
    ),
    parameters=[
        OpenApiParameter(
            name='month',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description='Month in YYYY-MM format (defaults to the current month)',
            required=False,
        ),
    ],
    responses={200: UserTimelineResponse, **_ERRORS},
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
@require_subscription_feature('ip_calling')
def user_timeline(request, user_id: int):
    """Day-bucketed call counts + talk time for a single user."""
    if user_id != request.user.id and not can_view_call_stats(request.user):
        return Response(
            {'error': 'You can only view your own call timeline.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    month_str = request.query_params.get('month')
    if month_str:
        parsed = _parse_month(month_str)
        if parsed is None:
            return Response(
                {'error': "Invalid month format. Use YYYY-MM (e.g. 2026-04)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        start, end = parsed
    else:
        now = timezone.now()
        parsed = _parse_month(now.strftime('%Y-%m'))
        assert parsed is not None
        start, end = parsed

    queryset = (
        CallLog.objects
        .filter(handled_by_id=user_id, started_at__gte=start, started_at__lt=end)
        .annotate(day=TruncDate('started_at'))
        .values('day')
        .annotate(
            total_calls=Count('id'),
            answered_count=Count('id', filter=answered_q() & Q(direction='inbound')),
            missed_count=Count('id', filter=missed_q()),
            outbound_count=Count('id', filter=Q(direction='outbound')),
            total_talk_seconds=Sum('duration', filter=_answered_durations_filter()),
        )
        .order_by('day')
    )

    def _to_seconds(d):
        if d is None:
            return 0
        if isinstance(d, timedelta):
            return int(d.total_seconds())
        try:
            return int(d) // 1_000_000
        except (TypeError, ValueError):
            return 0

    buckets = [
        {
            'day': row['day'].isoformat() if row['day'] else None,
            'total_calls': row['total_calls'],
            'answered_count': row['answered_count'],
            'missed_count': row['missed_count'],
            'outbound_count': row['outbound_count'],
            'total_talk_seconds': _to_seconds(row['total_talk_seconds']),
        }
        for row in queryset
    ]

    return Response({
        'user_id': user_id,
        'month': month_str or start.strftime('%Y-%m'),
        'start': start.isoformat(),
        'end': end.isoformat(),
        'results': buckets,
    })


@extend_schema(
    tags=['Call Statistics'],
    summary="Queue SLA metrics",
    description=(
        "Returns answered/abandoned counts, average wait time, and peak hour "
        "for a single queue. Queue calls are identified by matching the "
        "``CallLog.recipient_number`` against the DIDs of ``InboundRoute`` "
        "rows whose ``destination_queue_id`` is the requested queue."
    ),
    parameters=[
        OpenApiParameter(
            name='queue_id',
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            description='Queue id',
            required=True,
        ),
        OpenApiParameter(
            name='range',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description='today | week | month (defaults to month)',
            enum=['today', 'week', 'month'],
            required=False,
        ),
    ],
    responses={200: QueueStatsResponse, **_ERRORS},
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewCallStats])
@require_subscription_feature('ip_calling')
def queue_stats(request):
    """Queue SLA metrics (answered/abandoned, avg wait, peak hour)."""
    queue_id = request.query_params.get('queue_id')
    if not queue_id:
        return Response(
            {'error': 'queue_id query parameter is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        queue_id = int(queue_id)
    except (ValueError, TypeError):
        return Response(
            {'error': 'queue_id must be an integer.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    range_str = request.query_params.get('range', 'month')
    start, end = _parse_range(range_str)

    dids = list(
        InboundRoute.objects
        .filter(destination_queue_id=queue_id, is_active=True)
        .values_list('did', flat=True)
    )

    base_qs = CallLog.objects.filter(
        started_at__gte=start, started_at__lt=end,
        direction='inbound',
    )
    if dids:
        base_qs = base_qs.filter(recipient_number__in=dids)
    else:
        base_qs = base_qs.none()

    # Wait time = answered_at - started_at (seconds)
    wait_seconds_expr = (F('answered_at') - F('started_at'))

    summary = base_qs.aggregate(
        total_calls=Count('id'),
        answered_count=Count('id', filter=answered_q()),
        abandoned_count=Count('id', filter=missed_q()),
        avg_wait_interval=Avg(
            wait_seconds_expr,
            filter=Q(answered_at__isnull=False),
        ),
    )

    # Peak hour — DB-side EXTRACT(HOUR FROM started_at).
    hour_rows = (
        base_qs
        .annotate(hour=ExtractHour('started_at'))
        .values('hour')
        .annotate(count=Count('id'))
        .order_by('-count', 'hour')
    )
    peak_hour = hour_rows[0]['hour'] if hour_rows else None

    avg_wait_interval = summary.get('avg_wait_interval')
    if isinstance(avg_wait_interval, timedelta):
        avg_wait_seconds = int(avg_wait_interval.total_seconds())
    elif avg_wait_interval is None:
        avg_wait_seconds = 0
    else:
        try:
            avg_wait_seconds = int(avg_wait_interval) // 1_000_000
        except (TypeError, ValueError):
            avg_wait_seconds = 0

    return Response({
        'queue_id': queue_id,
        'range': range_str,
        'start': start.isoformat(),
        'end': end.isoformat(),
        'matched_dids': dids,
        'total_calls': summary['total_calls'] or 0,
        'answered_count': summary['answered_count'] or 0,
        'abandoned_count': summary['abandoned_count'] or 0,
        'avg_wait_seconds': avg_wait_seconds,
        'peak_hour': peak_hour,
    })


@extend_schema(
    tags=['Call Statistics'],
    summary="Tenant-wide call overview",
    description=(
        "Returns tenant-wide call metrics for the requested range: total "
        "calls, answered/missed counts, direction split, average talk time, "
        "busiest hour/weekday, and the top 5 users by total talk time."
    ),
    parameters=[
        OpenApiParameter(
            name='range',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description='today | week | month (defaults to month)',
            enum=['today', 'week', 'month'],
            required=False,
        ),
    ],
    responses={200: OverviewStatsResponse, **_ERRORS},
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewCallStats])
@require_subscription_feature('ip_calling')
def overview_stats(request):
    """Tenant-wide call overview (numbers + top 5 users)."""
    range_str = request.query_params.get('range', 'month')
    start, end = _parse_range(range_str)

    base_qs = CallLog.objects.filter(started_at__gte=start, started_at__lt=end)

    summary = base_qs.aggregate(
        total_calls=Count('id'),
        answered_calls=Count('id', filter=answered_q()),
        missed_calls=Count('id', filter=missed_q()),
        inbound_calls=Count('id', filter=Q(direction='inbound')),
        outbound_calls=Count('id', filter=Q(direction='outbound')),
        avg_talk_seconds_raw=Avg('duration', filter=_answered_durations_filter()),
    )

    avg_talk_raw = summary.get('avg_talk_seconds_raw')
    if isinstance(avg_talk_raw, timedelta):
        avg_talk_seconds = int(avg_talk_raw.total_seconds())
    elif avg_talk_raw is None:
        avg_talk_seconds = 0
    else:
        try:
            avg_talk_seconds = int(avg_talk_raw) // 1_000_000
        except (TypeError, ValueError):
            avg_talk_seconds = 0

    # Busiest hour (0–23)
    hour_rows = list(
        base_qs.annotate(hour=ExtractHour('started_at'))
        .values('hour')
        .annotate(count=Count('id'))
        .order_by('-count', 'hour')
    )
    busiest_hour = hour_rows[0]['hour'] if hour_rows else None

    # Busiest weekday — ISO weekday: 1=Monday..7=Sunday. Convert to 0..6 (Mon..Sun).
    weekday_rows = list(
        base_qs.annotate(iso_weekday=ExtractIsoWeekDay('started_at'))
        .values('iso_weekday')
        .annotate(count=Count('id'))
        .order_by('-count', 'iso_weekday')
    )
    busiest_weekday = (weekday_rows[0]['iso_weekday'] - 1) if weekday_rows else None

    # Top 5 users by total talk time (reuse the per-user aggregate shape).
    top_rows = (
        base_qs
        .filter(handled_by__isnull=False)
        .values('handled_by')
        .annotate(
            total_talk_seconds=Sum('duration', filter=_answered_durations_filter()),
            answered_count=Count('id', filter=answered_q() & Q(direction='inbound')),
            outbound_count=Count('id', filter=Q(direction='outbound')),
            user_first_name=F('handled_by__first_name'),
            user_last_name=F('handled_by__last_name'),
            user_email=F('handled_by__email'),
        )
        .order_by(F('total_talk_seconds').desc(nulls_last=True))[:5]
    )

    def _to_seconds(d):
        if d is None:
            return 0
        if isinstance(d, timedelta):
            return int(d.total_seconds())
        try:
            return int(d) // 1_000_000
        except (TypeError, ValueError):
            return 0

    top_5_users = []
    for row in top_rows:
        full_name = f"{row['user_first_name'] or ''} {row['user_last_name'] or ''}".strip()
        top_5_users.append({
            'user_id': row['handled_by'],
            'user_name': full_name or row['user_email'],
            'user_email': row['user_email'],
            'answered_count': row['answered_count'],
            'outbound_count': row['outbound_count'],
            'total_talk_seconds': _to_seconds(row['total_talk_seconds']),
        })

    return Response({
        'range': range_str,
        'start': start.isoformat(),
        'end': end.isoformat(),
        'total_calls': summary['total_calls'] or 0,
        'answered_calls': summary['answered_calls'] or 0,
        'missed_calls': summary['missed_calls'] or 0,
        'inbound_calls': summary['inbound_calls'] or 0,
        'outbound_calls': summary['outbound_calls'] or 0,
        'avg_talk_seconds': avg_talk_seconds,
        'busiest_hour': busiest_hour,
        'busiest_weekday': busiest_weekday,
        'top_5_users': top_5_users,
    })
