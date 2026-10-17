"""Shared definitions of "answered" and "missed" for call logs.

The web softphone closes every call with ``end_call``, so a ringing inbound
call that nobody picked up used to end as ``status='ended'`` with no
``answered_at`` and a zero duration. Counting every ``ended`` row as
answered therefore inflated the answered numbers. These helpers are the
single definition used by the stats endpoints, the legacy
``call-logs/statistics`` action and the ``missed`` list filter:

* answered: status ``answered``, or ended/transferred **with** evidence the
  call was picked up (``answered_at`` set or a positive duration).
* missed (inbound only): status missed/no_answer, or a legacy ``ended`` row
  that was never picked up (no ``answered_at``, zero/empty duration).

A ring-all leg that rang here while a colleague answered the call is saved
as ``cancelled`` (see :data:`ANSWERED_ELSEWHERE_STATUS`), which neither
definition counts, so one handled call does not show up as N-1 missed ones.
"""

from datetime import timedelta

from django.db.models import Q

MISSED_STATUSES = ('missed', 'no_answer')
# Status of an inbound leg that rang here but was answered by a colleague
# (ring-all queue). Not answered here and not missed: excluded from both.
ANSWERED_ELSEWHERE_STATUS = 'cancelled'
# Statuses that mean the call is over; end_call refuses these.
FINAL_STATUSES = ('ended', 'missed', 'failed', 'cancelled', 'no_answer', 'busy', 'transferred')


def picked_up_q():
    return Q(answered_at__isnull=False) | Q(duration__gt=timedelta(0))


def answered_q():
    # ``answered`` itself is proof of pick-up; ``ended``/``transferred``
    # need answered_at or a positive duration.
    return Q(status='answered') | (Q(status__in=('ended', 'transferred')) & picked_up_q())


def missed_q():
    never_picked_up = Q(answered_at__isnull=True) & (
        Q(duration__isnull=True) | Q(duration=timedelta(0))
    )
    return Q(direction='inbound') & (
        Q(status__in=MISSED_STATUSES) | (Q(status='ended') & never_picked_up)
    )


def last_digits(number, n=7):
    clean = ''.join(ch for ch in (number or '') if ch.isdigit())
    return clean[-n:] if len(clean) >= n else clean
