"""
Substitutes and their consent (INSERVICE "ჩემი შემცვლელები").

An employee keeps a list of (substitute, task) pairs. When they request a
leave type that requires substitutes (and the feature is on), the list is
copied into the request and every substitute has to accept before the
approval chain starts. One refusal rejects the request. Only staff can skip
pending consents, with a reason that goes into the audit log.
"""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .audit import log_event
from .models import LeaveRequest, LeaveRequestSubstitute, SubstituteAssignment
from .utils import get_leave_settings, update_leave_balance

User = get_user_model()

CONFIRMED_REQUEST_STATUSES = ('pending', 'manager_approved', 'hr_approved', 'approved')


def substitutes_on(tenant):
    settings_obj = get_leave_settings(tenant)
    return bool(settings_obj and settings_obj.substitutes_enabled)


def requires_substitutes(tenant, leave_type):
    return substitutes_on(tenant) and bool(leave_type.requires_substitutes)


def clean_rows(owner, rows):
    """
    Validate [{substitute: id|User, task: str}] for ``owner``; returns
    [(User, task)] or raises ValidationError. Same rules as INSERVICE: at
    most 50 rows, active users, not the owner, each listed once, task 1-300
    characters.
    """
    if not isinstance(rows, (list, tuple)):
        raise serializers.ValidationError({'substitutes': 'Send a list of {substitute, task}.'})
    if len(rows) > SubstituteAssignment.MAX_PER_OWNER:
        raise serializers.ValidationError(
            {'substitutes': f'At most {SubstituteAssignment.MAX_PER_OWNER} substitutes.'}
        )
    ids = []
    for row in rows:
        sub = row.get('substitute') if isinstance(row, dict) else None
        ids.append(getattr(sub, 'pk', sub))
    users = {u.pk: u for u in User.objects.filter(pk__in=[i for i in ids if i])}
    seen, cleaned = set(), []
    for row, sub_id in zip(rows, ids):
        task = (row.get('task') or '').strip() if isinstance(row, dict) else ''
        user = users.get(sub_id)
        if (
            user is None or not user.is_active or user.pk == owner.pk
            or user.pk in seen or not task or len(task) > 300
        ):
            raise serializers.ValidationError({
                'substitutes': 'Choose an active colleague (not yourself, each once) and their task (max 300 characters).'
            })
        seen.add(user.pk)
        cleaned.append((user, task))
    return cleaned


def resolve_for_request(owner, leave_type, tenant, body_rows=None):
    """
    The substitutes a new request needs, or [] when the type does not need
    any. Uses the owner's saved list; when it is empty the request body
    must carry at least one substitute.
    """
    if not requires_substitutes(tenant, leave_type):
        return []
    saved = list(
        SubstituteAssignment.objects.filter(tenant=tenant, owner=owner).select_related('substitute')
    )
    if saved:
        rows = [{'substitute': a.substitute, 'task': a.task} for a in saved]
        try:
            return clean_rows(owner, rows)
        except serializers.ValidationError:
            raise serializers.ValidationError({
                'substitutes': 'One of your saved substitutes is no longer active. Update "My substitutes" first.'
            })
    if not body_rows:
        raise serializers.ValidationError({
            'substitutes': 'This leave type needs at least one substitute. Add them in "My substitutes" or on the request.'
        })
    return clean_rows(owner, body_rows)


def check_availability(employee, substitutes, start_date, end_date, tenant, exclude_request_id=None):
    """
    - no substitute may have an approved absence overlapping the dates;
    - the employee may not be a confirmed substitute on someone else's
      overlapping request (checked whenever the feature is on).
    Raises ValidationError.
    """
    for user, _task in substitutes:
        clash = LeaveRequest.objects.filter(
            tenant=tenant, employee=user, status='approved',
            start_date__lte=end_date, end_date__gte=start_date,
        ).exists()
        if clash:
            name = user.get_full_name() or user.email
            raise serializers.ValidationError({
                'substitutes': f'{name} has approved leave on these dates.'
            })
    if substitutes_on(tenant):
        covering = LeaveRequestSubstitute.objects.filter(
            substitute=employee, status__in=('accepted', 'skipped'),
            leave_request__tenant=tenant,
            leave_request__status__in=CONFIRMED_REQUEST_STATUSES,
            leave_request__start_date__lte=end_date,
            leave_request__end_date__gte=start_date,
        )
        if exclude_request_id:
            covering = covering.exclude(leave_request_id=exclude_request_id)
        if covering.exists():
            raise serializers.ValidationError({
                'start_date': 'You are a confirmed substitute for a colleague on these dates.'
            })


def _rate_snapshot(tenant, owner, substitute, task):
    """Current substitution rate for the pair, when substitution pay is on."""
    settings_obj = get_leave_settings(tenant)
    if not (settings_obj and settings_obj.substitution_pay_enabled):
        return {}
    try:
        from .models import SubstitutionRate
    except ImportError:  # pragma: no cover - B4 not migrated yet
        return {}
    rate = SubstitutionRate.objects.filter(
        assignment__tenant=tenant, assignment__owner=owner,
        assignment__substitute=substitute, assignment__task=task,
    ).first()
    if rate is None:
        return {}
    return {'rate_mode': rate.mode, 'rate_amount': rate.amount, 'rate_currency': rate.currency}


def snapshot(leave_request, substitutes, actor=None, notify=True):
    """(Re)create the request's substitute rows, all pending."""
    leave_request.substitutes.all().delete()
    rows = []
    for index, (user, task) in enumerate(substitutes):
        rows.append(LeaveRequestSubstitute.objects.create(
            leave_request=leave_request, substitute=user, task=task, sort_order=index,
            **_rate_snapshot(leave_request.tenant, leave_request.employee, user, task),
        ))
    log_event(
        leave_request.tenant, 'substitute', 'snapshot', actor=actor or leave_request.employee,
        leave_request=leave_request, subject_user=leave_request.employee,
        object_id=leave_request.pk,
        data={'substitutes': [{'id': r.substitute_id, 'task': r.task} for r in rows]},
    )
    if notify:
        notify_requested(leave_request, rows)
    return rows


class SubstituteResponseError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def respond(leave_request, user, action, comment=''):
    """
    Record ``user``'s answer. Returns the (locked, saved) request. Raises
    SubstituteResponseError for an invalid answer.
    """
    if action not in ('accept', 'reject'):
        raise SubstituteResponseError('action must be "accept" or "reject"')
    with transaction.atomic():
        locked = LeaveRequest.objects.select_for_update().get(pk=leave_request.pk)
        row = locked.substitutes.filter(substitute=user).first()
        if row is None:
            raise SubstituteResponseError('You are not a substitute on this request', 403)
        if locked.status != 'awaiting_substitutes':
            raise SubstituteResponseError('This request is no longer waiting for substitutes')
        if row.status != 'pending':
            raise SubstituteResponseError('You have already answered')
        if action == 'reject' and not (comment or '').strip():
            raise SubstituteResponseError('Please give a reason when declining')

        now = timezone.now()
        row.status = 'accepted' if action == 'accept' else 'rejected'
        row.responded_at = now
        row.comment = comment or ''
        row.save(update_fields=['status', 'responded_at', 'comment', 'updated_at'])
        log_event(
            locked.tenant, 'substitute', 'accepted' if action == 'accept' else 'rejected',
            actor=user, leave_request=locked, subject_user=locked.employee, object_id=row.pk,
            from_status='pending', to_status=row.status, data={'comment': comment or ''},
        )

        locked._leave_actor = user
        if action == 'reject':
            locked.status = 'rejected'
            locked.rejected_by = user
            locked.rejected_at = now
            locked.rejection_reason = comment or 'Declined by substitute'
            locked.save()
            update_leave_balance(locked, action='reject')
        elif not locked.substitutes.exclude(status__in=('accepted', 'skipped')).exists():
            locked.status = 'pending'
            locked.save()
    return locked


def skip_pending(leave_request, admin, reason):
    """Staff override: mark open consents as skipped and start the approval chain."""
    reason = (reason or '').strip()
    if not reason:
        raise SubstituteResponseError('A reason is required')
    with transaction.atomic():
        locked = LeaveRequest.objects.select_for_update().get(pk=leave_request.pk)
        if locked.status != 'awaiting_substitutes':
            raise SubstituteResponseError('This request is not waiting for substitutes')
        now = timezone.now()
        skipped = list(locked.substitutes.filter(status='pending'))
        locked.substitutes.filter(status='pending').update(status='skipped', responded_at=now)
        locked.substitutes_skipped_by = admin
        locked.substitutes_skipped_at = now
        locked.substitutes_skip_reason = reason
        locked.status = 'pending'
        locked._leave_actor = admin
        locked._audit_comment = reason
        locked.save()
        log_event(
            locked.tenant, 'substitute', 'skipped', actor=admin, leave_request=locked,
            subject_user=locked.employee, object_id=locked.pk,
            data={'reason': reason, 'substitutes': [r.substitute_id for r in skipped]},
        )
    notify_skipped(locked, skipped, admin)
    return locked


# ── notifications ───────────────────────────────────────────────────────────

def _texts():
    from users.notification_texts import T
    return T


def _period(leave_request):
    return f'{leave_request.start_date} - {leave_request.end_date}'


def notify_requested(leave_request, rows):
    from users.notification_utils import create_notification
    T = _texts()
    employee = leave_request.employee
    name = employee.get_full_name() or employee.email
    for row in rows:
        create_notification(
            user=row.substitute,
            notification_type='leave_substitute_requested',
            title=T('leave.substitute_requested.title'),
            message=T('leave.substitute_requested.message', employee=name,
                      period=_period(leave_request), task=row.task),
            metadata={'leave_request_id': leave_request.id, 'employee_name': name, 'task': row.task},
            link_url=f'/leave/covering?id={leave_request.id}',
        )


def notify_skipped(leave_request, skipped_rows, admin):
    from users.notification_utils import create_notification
    T = _texts()
    recipients = {r.substitute_id: r.substitute for r in skipped_rows}
    if leave_request.employee_id != getattr(admin, 'pk', None):
        recipients[leave_request.employee_id] = leave_request.employee
    for user in recipients.values():
        is_employee = user.pk == leave_request.employee_id
        create_notification(
            user=user,
            notification_type='leave_substitutes_skipped',
            title=T('leave.substitutes_skipped.title'),
            message=T('leave.substitutes_skipped.message', period=_period(leave_request)),
            metadata={'leave_request_id': leave_request.id,
                      'reason': leave_request.substitutes_skip_reason},
            link_url=(f'/leave/my-requests?id={leave_request.id}' if is_employee
                      else f'/leave/covering?id={leave_request.id}'),
        )
