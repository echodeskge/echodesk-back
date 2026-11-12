"""
Step registry.

``register(step_cls)`` (usable as a decorator) stores a step class by its
``key``; ``get`` / ``all_steps`` hand out fresh instances. ``autodiscover``
imports ``<app>.onboarding`` for every app in ``settings.TENANT_APPS`` that
ships such a module, plus the built-in workspace step. It runs from
``OnboardingConfig.ready()`` and is safe to call again.
"""
import importlib
import importlib.util
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

_registry = {}


def register(step_cls):
    """Register ``step_cls`` (an ``OnboardingStep`` subclass) under its ``key``."""
    key = getattr(step_cls, 'key', None)
    if not key or not isinstance(key, str):
        raise ValueError(f'{step_cls!r} needs a non-empty string `key`')
    existing = _registry.get(key)
    if existing is not None and existing is not step_cls:
        logger.warning('Onboarding step %r re-registered: %r replaces %r', key, step_cls, existing)
    _registry[key] = step_cls
    return step_cls


def unregister(key):
    """Remove a step (tests)."""
    return _registry.pop(key, None)


def get(key):
    """A fresh instance of the step registered under ``key`` or ``None``."""
    step_cls = _registry.get(key)
    return step_cls() if step_cls is not None else None


def all_steps():
    """Fresh instances of every registered step, ordered by ``order`` then ``key``."""
    steps = [cls() for cls in _registry.values()]
    steps.sort(key=lambda s: (s.order, s.key))
    return steps


def steps_for(enabled_feature_keys):
    """The steps shown to a tenant: feature-less steps plus those whose
    ``feature_key`` is in ``enabled_feature_keys``, in wizard order."""
    enabled = set(enabled_feature_keys or ())
    return [s for s in all_steps() if s.feature_key is None or s.feature_key in enabled]


def registered_keys():
    return list(_registry.keys())


def autodiscover():
    """Import every tenant app's ``onboarding`` module so its steps register."""
    importlib.import_module('onboarding.steps.workspace')
    for app in getattr(settings, 'TENANT_APPS', []):
        if app == 'onboarding':
            continue
        module_name = f'{app}.onboarding'
        try:
            spec = importlib.util.find_spec(module_name)
        except (ModuleNotFoundError, ValueError):
            continue
        if spec is None:
            continue
        importlib.import_module(module_name)
