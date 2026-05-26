"""Shared HTTP client for outbound social-platform API calls.

A single ``requests.Session`` with a **send-safe** retry policy and a default
timeout, so outbound calls don't hang a request/worker thread forever and
recover from transient *connection* failures.

Why the retry policy is conservative
-------------------------------------
Retrying a POST that *sends a message* is dangerous: if the server already
processed the request but the response was lost (a read timeout or a 5xx after
commit), retrying produces a **duplicate message**. So this client retries only
``connect`` failures — where we're certain the request never reached the
server — and never retries on read timeouts or HTTP status codes. That makes it
safe to use for message sends as well as idempotent GETs.

Use ``post``/``get`` here for new code (e.g. channel adapters). Existing inline
``requests.*`` call sites are intentionally left as-is — a blanket swap to an
aggressive retry would risk double-sends.
"""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_TIMEOUT = 30  # seconds

# connect-only retries: never retry on read timeout or HTTP status, so a send
# that may have been processed server-side is never repeated.
_retry = Retry(
    total=2,
    connect=2,
    read=0,
    status=0,
    backoff_factor=0.5,
)

_session = requests.Session()
_adapter = HTTPAdapter(max_retries=_retry)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


def post(url, **kwargs):
    """POST via the shared session with a default timeout."""
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return _session.post(url, **kwargs)


def get(url, **kwargs):
    """GET via the shared session with a default timeout."""
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return _session.get(url, **kwargs)


def session() -> requests.Session:
    """Return the shared session (for callers that need full control)."""
    return _session
