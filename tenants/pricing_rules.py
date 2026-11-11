"""
Pricing rules shared by every place that accepts an ``agent_count``
(paid/free registration, the in-app agent-count update).

Decision 2026-11-11 (pricing D2): the minimum number of employees is 5,
sold in increments of 5 (5, 10, 15, ...). Keep this module the single
source of truth so the serializer, the views and the tests agree.
"""

MIN_AGENT_COUNT = 5
AGENT_COUNT_STEP = 5

AGENT_COUNT_ERROR = (
    f'Agent count must be at least {MIN_AGENT_COUNT} and a multiple of '
    f'{AGENT_COUNT_STEP} ({MIN_AGENT_COUNT}, {MIN_AGENT_COUNT + AGENT_COUNT_STEP}, '
    f'{MIN_AGENT_COUNT + 2 * AGENT_COUNT_STEP}, ...)'
)


def is_valid_agent_count(value) -> bool:
    """True iff ``value`` is an int >= MIN_AGENT_COUNT in steps of AGENT_COUNT_STEP."""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= MIN_AGENT_COUNT and value % AGENT_COUNT_STEP == 0
