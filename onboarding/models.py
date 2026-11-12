"""
Onboarding state for one tenant.

``TenantOnboarding`` is a single row per tenant schema. ``state`` maps a step
key to ``{"status", "answers", "completed_at"}``; a step missing from
``state`` counts as ``pending``. The step list itself (what is shown, in
which order) comes from :mod:`onboarding.registry` filtered by the tenant's
enabled feature keys — this model only stores progress and answers.
"""
from django.db import models
from django.utils import timezone

STATUS_PENDING = 'pending'
STATUS_DONE = 'done'
STATUS_SKIPPED = 'skipped'
STATUSES = (STATUS_PENDING, STATUS_DONE, STATUS_SKIPPED)

# Industry choices with the labels the wizard shows. The key is what the
# workspace step stores in ``TenantOnboarding.industry`` and what later steps
# read to pick their presets.
INDUSTRIES = [
    ('salon', {'en': 'Beauty salon / spa', 'ka': 'სილამაზის სალონი / სპა'}),
    ('clinic', {'en': 'Clinic / medical', 'ka': 'კლინიკა / სამედიცინო'}),
    ('real_estate', {'en': 'Real estate / construction', 'ka': 'უძრავი ქონება / მშენებლობა'}),
    ('shop', {'en': 'Shop / retail', 'ka': 'მაღაზია / საცალო ვაჭრობა'}),
    ('call_centre', {'en': 'Call centre / customer support', 'ka': 'ქოლ-ცენტრი / მომხმარებელთა მხარდაჭერა'}),
    ('logistics', {'en': 'Logistics / delivery', 'ka': 'ლოგისტიკა / მიწოდება'}),
    ('services', {'en': 'Professional services', 'ka': 'პროფესიული მომსახურება'}),
    ('other', {'en': 'Other', 'ka': 'სხვა'}),
]
INDUSTRY_KEYS = [key for key, _ in INDUSTRIES]
INDUSTRY_CHOICES = [(key, labels['en']) for key, labels in INDUSTRIES]


def industry_choices():
    """``[{"key", "label": {"en", "ka"}}]`` for the wizard."""
    return [{'key': key, 'label': dict(labels)} for key, labels in INDUSTRIES]


def _empty_entry():
    return {'status': STATUS_PENDING, 'answers': {}, 'completed_at': None}


class TenantOnboarding(models.Model):
    """Single row per tenant schema; use :meth:`get` to fetch/create it."""

    state = models.JSONField(default=dict, blank=True)
    industry = models.CharField(max_length=32, choices=INDUSTRY_CHOICES, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'tenant onboarding'
        verbose_name_plural = 'tenant onboarding'

    def __str__(self):
        return f'Onboarding (industry={self.industry or "-"}, finished={self.finished_at is not None})'

    @classmethod
    def get(cls):
        """The tenant's row, created on first use (call inside the tenant schema)."""
        obj = cls.objects.order_by('pk').first()
        if obj is None:
            obj = cls.objects.create()
        return obj

    # ── read helpers ──

    def entry(self, key):
        """The stored entry for ``key`` (a pending placeholder if absent)."""
        stored = self.state.get(key) if isinstance(self.state, dict) else None
        if not isinstance(stored, dict):
            return _empty_entry()
        entry = _empty_entry()
        entry.update(stored)
        if entry['status'] not in STATUSES:
            entry['status'] = STATUS_PENDING
        if not isinstance(entry.get('answers'), dict):
            entry['answers'] = {}
        return entry

    def status_of(self, key):
        return self.entry(key)['status']

    def answers_of(self, key):
        return self.entry(key)['answers']

    def pending_keys(self, enabled_feature_keys):
        """Keys of the steps shown for ``enabled_feature_keys`` that are still pending."""
        from .registry import steps_for
        return [
            step.key for step in steps_for(enabled_feature_keys)
            if self.status_of(step.key) == STATUS_PENDING
        ]

    # ── write helpers (all save unless told otherwise) ──

    def mark_pending(self, keys, save=True):
        """Add a pending entry for every key that has none yet.

        Existing entries (done / skipped / pending) are left untouched, so the
        call is idempotent and never undoes an admin's progress. Returns the
        keys that were added. ``finished_at`` is cleared when something was
        added, so the "finish setup" chip reappears for a newly enabled module.
        """
        if not isinstance(self.state, dict):
            self.state = {}
        added = []
        for key in keys:
            if key not in self.state:
                self.state[key] = _empty_entry()
                added.append(key)
        if added:
            self.finished_at = None
        if save:
            self.save()
        return added

    def remove(self, keys, save=True):
        """Drop the entries of ``keys`` (used when a module is removed)."""
        if not isinstance(self.state, dict):
            self.state = {}
        removed = [key for key in keys if self.state.pop(key, None) is not None]
        if save:
            self.save()
        return removed

    def set_status(self, key, status, answers=None, save=True):
        """Set ``status`` for ``key``; ``answers`` replaces the stored answers when given.

        ``done`` / ``skipped`` stamp ``completed_at``; ``pending`` clears it and
        ``finished_at``. The caller recomputes ``finished_at`` afterwards with
        :meth:`refresh_finished` once it knows the enabled feature keys.
        """
        if status not in STATUSES:
            raise ValueError(f'Unknown onboarding status: {status!r}')
        if not isinstance(self.state, dict):
            self.state = {}
        entry = self.entry(key)
        entry['status'] = status
        if answers is not None:
            entry['answers'] = answers
        if status == STATUS_PENDING:
            entry['completed_at'] = None
            self.finished_at = None
        else:
            entry['completed_at'] = timezone.now().isoformat()
            if self.started_at is None:
                self.started_at = timezone.now()
        self.state[key] = entry
        if save:
            self.save()
        return entry

    def refresh_finished(self, enabled_feature_keys, save=True):
        """``finished_at`` = now when no shown step is pending, else ``None``."""
        if self.pending_keys(enabled_feature_keys):
            self.finished_at = None
        elif self.finished_at is None:
            self.finished_at = timezone.now()
        if save:
            self.save()
        return self.finished_at
