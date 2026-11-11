"""Outbound POST with a retry for connection *establishment* failures.

Only retries when the TCP/TLS connection could not be opened
(``ConnectionError`` caused by ``NewConnectionError`` / ``ConnectTimeout``):
in that case nothing reached Meta/WhatsApp, so retrying cannot send a
customer a duplicate. Read timeouts and HTTP errors are never retried.
"""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

CONNECT_RETRIES = 2
BACKOFF_SECONDS = (0.5, 1.5)


def is_connect_failure(exc: Exception) -> bool:
    """True when ``exc`` means the connection was never opened (so nothing
    reached the remote side): ``[Errno 101] Network is unreachable``, DNS
    failures, connect timeouts. Read timeouts / SSL / HTTP errors are not."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(exc, (requests.exceptions.ReadTimeout, requests.exceptions.SSLError)):
        return False
    if isinstance(exc, requests.exceptions.ConnectionError):
        text = repr(exc)
        return (
            'NewConnectionError' in text
            or 'Failed to establish a new connection' in text
            or 'NameResolutionError' in text
            or 'Temporary failure in name resolution' in text
        )
    return False


_is_connect_failure = is_connect_failure


def post(url, **kwargs):
    """``requests.post`` with up to ``CONNECT_RETRIES`` retries on
    connection-establishment failures."""
    attempt = 0
    while True:
        try:
            return requests.post(url, **kwargs)
        except requests.exceptions.RequestException as exc:
            if attempt >= CONNECT_RETRIES or not is_connect_failure(exc):
                raise
            delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            attempt += 1
            logger.warning(
                'Outbound POST could not connect (attempt %s/%s), retrying in %ss: %s',
                attempt, CONNECT_RETRIES, delay, type(exc).__name__,
            )
            time.sleep(delay)
