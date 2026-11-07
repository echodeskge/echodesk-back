"""Helpers for the HR audit trail (LeaveAuditEvent)."""
import logging

logger = logging.getLogger(__name__)


def log_event(tenant, kind, action, *, actor=None, leave_request=None, kpi=None,
              subject_user=None, object_id='', from_status='', to_status='', data=None):
    """Append one audit row. Never raises: auditing must not break the action."""
    from .models import LeaveAuditEvent

    try:
        kwargs = dict(
            tenant=tenant, kind=kind, action=action,
            actor=actor if getattr(actor, 'pk', None) else None,
            leave_request=leave_request, subject_user=subject_user,
            object_id=str(object_id or ''), from_status=from_status or '',
            to_status=to_status or '', data=data or {},
        )
        if kpi is not None:
            kwargs['kpi'] = kpi
        return LeaveAuditEvent.objects.create(**kwargs)
    except Exception:  # pragma: no cover - defensive
        logger.exception('Could not write leave audit event %s/%s', kind, action)
        return None


def serialize_events(queryset):
    """Compact dicts for API responses (request/KPI history)."""
    rows = []
    for event in queryset.select_related('actor'):
        actor = event.actor
        rows.append({
            'id': event.id,
            'kind': event.kind,
            'action': event.action,
            'from_status': event.from_status,
            'to_status': event.to_status,
            'actor': actor.pk if actor else None,
            'actor_name': (actor.get_full_name() or actor.email) if actor else None,
            'data': event.data,
            'created_at': event.created_at,
        })
    return rows
