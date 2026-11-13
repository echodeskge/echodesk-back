"""
Pricing rules shared by every place that accepts an ``agent_count``
(paid/free registration, the in-app agent-count update).

Decision 2026-11-13 (supersedes pricing D2): the number of employees is
any whole number from 1, with no upper cap. It is chosen at signup
(default 1) and can be changed in Settings at any time; while on the free
trial a change is free. Keep this module the single source of truth so the
serializer, the views and the tests agree.
"""

MIN_AGENT_COUNT = 1
AGENT_COUNT_STEP = 1

# Pricing decision D1: POST /api/register/ is a 30-day free trial with no
# card; the card is added in-app before the trial ends.
TRIAL_DAYS = 30

AGENT_COUNT_ERROR = (
    f'Employee count must be a whole number of at least {MIN_AGENT_COUNT}'
)


def is_valid_agent_count(value) -> bool:
    """True iff ``value`` is an int (not a bool) >= MIN_AGENT_COUNT."""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= MIN_AGENT_COUNT
