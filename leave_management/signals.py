"""Audit trail for leave request status changes."""
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from .models import LeaveRequest
from .audit import log_event


@receiver(pre_save, sender=LeaveRequest)
def remember_leave_status_for_audit(sender, instance, **kwargs):
    if instance.pk:
        instance._audit_old_status = (
            LeaveRequest.objects.filter(pk=instance.pk).values_list('status', flat=True).first()
        )
    else:
        instance._audit_old_status = None


@receiver(post_save, sender=LeaveRequest)
def audit_leave_status(sender, instance, created, **kwargs):
    actor = getattr(instance, '_leave_actor', None)
    if created:
        log_event(
            instance.tenant, 'request', 'created',
            actor=actor or instance.employee, leave_request=instance,
            subject_user=instance.employee, object_id=instance.pk,
            from_status='new', to_status=instance.status,
            data={
                'start_date': str(instance.start_date),
                'end_date': str(instance.end_date),
                'total_days': str(instance.total_days),
                'leave_type': instance.leave_type_id,
            },
        )
        return
    old = getattr(instance, '_audit_old_status', None)
    if old and old != instance.status:
        data = {}
        comment = getattr(instance, '_audit_comment', '')
        if comment:
            data['comment'] = comment
        log_event(
            instance.tenant, 'request', 'status_changed',
            actor=actor, leave_request=instance, subject_user=instance.employee,
            object_id=instance.pk, from_status=old, to_status=instance.status, data=data,
        )
