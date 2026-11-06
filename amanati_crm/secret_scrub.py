"""Keep third-party credentials out of logs, Sentry and API error bodies.

Meta's Graph API takes the page token as an ``access_token`` query
parameter, so a ``requests`` connection error message contains the full
URL, token included (ECHODESK-BACKEND-84 showed a page token in Sentry and
in the JSON error returned to the browser). Everything that turns an
exception into text goes through :func:`scrub`.

``install()`` wires it into three places:
- a log-record factory, so every logger (Django, Celery, gunicorn) is covered
  regardless of which handlers are attached;
- ``SENTRY_BEFORE_SEND`` for the Sentry SDK (exception values, messages,
  breadcrumbs, request query strings);
- :class:`ScrubErrorResponseMiddleware` for 4xx/5xx JSON response bodies.
"""
from __future__ import annotations

import logging
import re

# key=value in URLs / query strings / form bodies. The value stops at a
# separator or quote so the rest of the message stays readable.
_PARAM_RE = re.compile(
    r'(?i)\b(access_token|appsecret_proof|client_secret|api_key|apikey|'
    r'fb_exchange_token|input_token|refresh_token|password)=([^&\s\'"<>)]+)'
)
# "Bearer <token>" in headers echoed into messages.
_BEARER_RE = re.compile(r'(?i)\b(bearer)\s+([A-Za-z0-9\-._~+/]{16,}=*)')
# Bare Meta user/page tokens (EAA…) that show up outside a key=value pair.
_META_TOKEN_RE = re.compile(r'\bEAA[A-Za-z0-9]{40,}\b')

REDACTED = '[redacted]'


def scrub(text):
    """Return ``text`` with known credential patterns replaced."""
    if not isinstance(text, str) or not text:
        return text
    if '=' not in text and 'EAA' not in text and 'earer' not in text:
        return text
    text = _PARAM_RE.sub(lambda m: f'{m.group(1)}={REDACTED}', text)
    text = _BEARER_RE.sub(lambda m: f'{m.group(1)} {REDACTED}', text)
    text = _META_TOKEN_RE.sub(REDACTED, text)
    return text


def _scrub_obj(value, depth=0):
    if depth > 8:
        return value
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _scrub_obj(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_obj(v, depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_obj(v, depth + 1) for v in value)
    return value


# ── logging ─────────────────────────────────────────────────────────────────

_installed = False


def _wrap_record_factory():
    original = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = original(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:
            return record
        cleaned = scrub(message)
        if cleaned is not message and cleaned != message:
            record.msg = cleaned
            record.args = ()
        return record

    logging.setLogRecordFactory(factory)


# ── Sentry ──────────────────────────────────────────────────────────────────

def sentry_before_send(event, hint=None):
    try:
        for key in ('message', 'logentry', 'exception', 'breadcrumbs', 'request', 'extra', 'threads'):
            if key in event:
                event[key] = _scrub_obj(event[key])
    except Exception:
        pass
    return event


def sentry_before_breadcrumb(crumb, hint=None):
    try:
        return _scrub_obj(crumb)
    except Exception:
        return crumb


# ── HTTP responses ──────────────────────────────────────────────────────────

class ScrubErrorResponseMiddleware:
    """Views often return ``{'error': str(e)}``; strip credentials from the
    body of JSON error responses before they leave the server."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            if (
                response.status_code >= 400
                and not getattr(response, 'streaming', False)
                and 'json' in (response.get('Content-Type') or '')
            ):
                body = response.content.decode('utf-8')
                cleaned = scrub(body)
                if cleaned != body:
                    response.content = cleaned.encode('utf-8')
                    if response.has_header('Content-Length'):
                        response['Content-Length'] = str(len(response.content))
        except Exception:
            pass
        return response


def install():
    global _installed
    if _installed:
        return
    _installed = True
    _wrap_record_factory()
