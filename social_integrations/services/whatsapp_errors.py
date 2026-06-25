"""
Translate WhatsApp Cloud API (Graph) send errors into stable, frontend-facing
error codes.

The Graph API returns errors whose English `message` text is unstable and leaks
internal detail. We map the numeric `code`/`error_subcode` to a small set of
stable codes the frontend can translate into clear agent-facing guidance. This
is the single chokepoint reused by the template-send view (and, later, the
normal send path).

Reference: https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes/
"""

# Stable error codes (kept in sync with the frontend i18n `social.whatsapp.sendErrors.*`)
OPT_IN_REQUIRED = 'OPT_IN_REQUIRED'
RECIPIENT_NOT_OPTED_IN = 'RECIPIENT_NOT_OPTED_IN'
INVALID_WHATSAPP_NUMBER = 'INVALID_WHATSAPP_NUMBER'
TEMPLATE_PAUSED_OR_DISABLED = 'TEMPLATE_PAUSED_OR_DISABLED'
TEMPLATE_NOT_APPROVED = 'TEMPLATE_NOT_APPROVED'
RATE_OR_TIER_LIMIT = 'RATE_OR_TIER_LIMIT'
US_MARKETING_PAUSED = 'US_MARKETING_PAUSED'
TEMPLATE_PARAM_MISMATCH = 'TEMPLATE_PARAM_MISMATCH'
SEND_FAILED_GENERIC = 'SEND_FAILED_GENERIC'

# Safe English fallbacks (the frontend localizes by code; this is the default).
STABLE_ERROR_MESSAGES = {
    OPT_IN_REQUIRED: "Confirm the recipient has opted in before starting a new conversation.",
    RECIPIENT_NOT_OPTED_IN: "The recipient hasn't opted in, or the messaging window is closed.",
    INVALID_WHATSAPP_NUMBER: "This number isn't a valid WhatsApp user.",
    TEMPLATE_PAUSED_OR_DISABLED: "This template is paused or disabled due to quality. Pick another template.",
    TEMPLATE_NOT_APPROVED: "This template hasn't been approved yet.",
    RATE_OR_TIER_LIMIT: "Messaging limit reached for this number. Try again later.",
    US_MARKETING_PAUSED: "WhatsApp has paused marketing templates to US numbers. Use a utility template.",
    TEMPLATE_PARAM_MISMATCH: "The template parameters don't match the template.",
    SEND_FAILED_GENERIC: "Couldn't send the message. Please try again or contact support.",
}

# Graph numeric code -> stable code. Anything not listed is treated as a generic
# upstream failure (502) so monitoring can distinguish it from client errors.
_GRAPH_CODE_MAP = {
    131026: INVALID_WHATSAPP_NUMBER,   # Message undeliverable / not a valid WA user
    133010: INVALID_WHATSAPP_NUMBER,   # Phone number not registered on WhatsApp
    131047: RECIPIENT_NOT_OPTED_IN,    # Re-engagement required (outside window)
    131048: RATE_OR_TIER_LIMIT,        # Spam/quality rate limit hit
    131049: RATE_OR_TIER_LIMIT,        # Healthy-ecosystem engagement throttle (marketing)
    130429: RATE_OR_TIER_LIMIT,        # Cloud API throughput rate limit
    132000: TEMPLATE_PARAM_MISMATCH,   # Param count mismatch
    132012: TEMPLATE_PARAM_MISMATCH,   # Param format mismatch
    132001: TEMPLATE_NOT_APPROVED,     # Template does not exist / not approved
    132015: TEMPLATE_PAUSED_OR_DISABLED,  # Template paused
    132016: TEMPLATE_PAUSED_OR_DISABLED,  # Template disabled
}

# Stable codes that represent an actionable client problem -> HTTP 400.
# Everything else is an upstream/unknown failure -> HTTP 502.
_CLIENT_ERROR_CODES = {
    RECIPIENT_NOT_OPTED_IN,
    INVALID_WHATSAPP_NUMBER,
    TEMPLATE_PAUSED_OR_DISABLED,
    TEMPLATE_NOT_APPROVED,
    RATE_OR_TIER_LIMIT,
    US_MARKETING_PAUSED,
    TEMPLATE_PARAM_MISMATCH,
}


def map_graph_error(meta_error):
    """
    Map a Graph API error object to (stable_error_code, http_status).

    `meta_error` is the dict at response.json()['error'] (may be empty/None).
    """
    meta_error = meta_error or {}
    code = meta_error.get('code')
    error_code = _GRAPH_CODE_MAP.get(code, SEND_FAILED_GENERIC)
    http_status = 400 if error_code in _CLIENT_ERROR_CODES else 502
    return error_code, http_status


def error_response_body(error_code, meta_code=None):
    """Build the stable JSON body returned to the frontend."""
    return {
        'error_code': error_code,
        'message': STABLE_ERROR_MESSAGES.get(error_code, STABLE_ERROR_MESSAGES[SEND_FAILED_GENERIC]),
        'meta_code': meta_code,
    }
