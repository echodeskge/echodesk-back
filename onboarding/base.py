"""
The step interface every module's onboarding step implements.

A step lives in its own app as ``<app>/onboarding.py`` (the workspace step
is ``onboarding/steps/workspace.py``) and registers itself with
:func:`onboarding.registry.register`. It is stateless: the registry hands
out fresh instances, and all progress lives in ``TenantOnboarding``.
"""
from rest_framework import serializers


def empty_result():
    """The shape ``apply`` returns; copy and fill it."""
    return {'created': {}, 'updated': [], 'notes': []}


def normalize_result(result):
    """Coerce whatever ``apply`` returned into the contract's result shape."""
    out = empty_result()
    if isinstance(result, dict):
        created = result.get('created')
        if isinstance(created, dict):
            out['created'] = created
        updated = result.get('updated')
        if isinstance(updated, (list, tuple)):
            out['updated'] = list(updated)
        notes = result.get('notes')
        if isinstance(notes, (list, tuple)):
            out['notes'] = [str(n) for n in notes]
    return out


class OnboardingStep:
    """
    One wizard step.

    Class attributes:

    - ``key``: the step key (``"tickets"``), unique in the registry.
    - ``feature_key``: the subscription feature that must be enabled for the
      step to be shown; ``None`` means always shown (the workspace step).
    - ``order``: sort key in the wizard (workspace 0, tickets 10, ...).
    - ``serializer_class``: a DRF ``Serializer`` that validates ``answers``.
      It is instantiated with ``context={"request", "tenant", "state"}``.

    Methods receive ``tenant`` (the ``tenants.Tenant`` row) and run inside
    that tenant's schema.
    """

    key: str = ''
    feature_key = None
    order: int = 0
    serializer_class = serializers.Serializer

    def context(self, tenant, request):
        """Read-only data the wizard page needs (users, channels, flags...)."""
        return {}

    def prefill(self, tenant, state):
        """Suggested answers computed from existing data; ``state`` is the
        ``TenantOnboarding`` row (its ``industry`` and other steps' answers
        are available through it)."""
        return {}

    def apply(self, tenant, answers, user):
        """Write ``answers`` (already validated) into the real settings.

        Must be idempotent — match existing rows by natural keys and never
        delete. The view wraps the call in one transaction and marks the step
        ``done`` when it returns. Return ``{"created": {...}, "updated":
        [...], "notes": [...]}`` (see :func:`empty_result`).
        """
        return empty_result()

    def seed_defaults(self, tenant):
        """Sensible defaults written at provisioning for an enabled module,
        before any admin has answered anything (decision B). Must be safe to
        run more than once and must not depend on the wizard."""
        return None

    # ── helpers ──

    def validate(self, answers, **context):
        """Instantiate ``serializer_class`` on ``answers``; raises
        ``serializers.ValidationError`` on bad input."""
        serializer = self.serializer_class(data=answers, context=context)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data
