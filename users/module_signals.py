"""
Signal handlers for optional modules (invoices, leave, bookings, calls).

Each block is guarded by try/except ImportError so the app works even if a
module is not installed.  Registered via UsersConfig.ready().
"""

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()

# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------
try:
    from invoices.models import Invoice

    @receiver(pre_save, sender=Invoice)
    def track_invoice_status_change(sender, instance, **kwargs):
        """Remember old status so post_save can detect transitions."""
        if instance.pk:
            try:
                old = Invoice.objects.get(pk=instance.pk)
                instance._old_status = old.status
            except Invoice.DoesNotExist:
                instance._old_status = None
        else:
            instance._old_status = None

    @receiver(post_save, sender=Invoice)
    def notify_invoice_events(sender, instance, created, **kwargs):
        from users.notification_utils import create_notification

        if created:
            # Notify all active users about a new invoice
            users = User.objects.filter(is_active=True)[:10]
            for user in users:
                create_notification(
                    user=user,
                    notification_type='invoice_created',
                    title=f'Invoice {instance.invoice_number} created',
                    message=f'New invoice for {instance.client_name or "a client"} totalling {instance.total} {instance.currency}',
                    metadata={
                        'invoice_id': instance.id,
                        'invoice_number': instance.invoice_number,
                        'status': instance.status,
                    },
                    link_url=f'/invoices/{instance.id}',
                )
            return

        old_status = getattr(instance, '_old_status', None)

        # Invoice paid
        if old_status != 'paid' and instance.status == 'paid':
            if instance.created_by:
                create_notification(
                    user=instance.created_by,
                    notification_type='invoice_paid',
                    title=f'Invoice {instance.invoice_number} paid',
                    message=f'{instance.client_name or "Client"} has paid invoice {instance.invoice_number}',
                    metadata={
                        'invoice_id': instance.id,
                        'invoice_number': instance.invoice_number,
                    },
                    link_url=f'/invoices/{instance.id}',
                )

        # Invoice overdue
        if old_status != 'overdue' and instance.status == 'overdue':
            if instance.created_by:
                create_notification(
                    user=instance.created_by,
                    notification_type='invoice_overdue',
                    title=f'Invoice {instance.invoice_number} overdue',
                    message=f'Invoice for {instance.client_name or "a client"} is now overdue',
                    metadata={
                        'invoice_id': instance.id,
                        'invoice_number': instance.invoice_number,
                    },
                    link_url=f'/invoices/{instance.id}',
                )

except ImportError:
    pass

# ---------------------------------------------------------------------------
# Leave Management
# ---------------------------------------------------------------------------
try:
    from leave_management.models import LeaveRequest

    @receiver(pre_save, sender=LeaveRequest)
    def track_leave_status_change(sender, instance, **kwargs):
        """Remember old status so post_save can detect transitions."""
        if instance.pk:
            try:
                old = LeaveRequest.objects.get(pk=instance.pk)
                instance._old_status = old.status
            except LeaveRequest.DoesNotExist:
                instance._old_status = None
        else:
            instance._old_status = None

    @receiver(post_save, sender=LeaveRequest)
    def notify_leave_events(sender, instance, created, **kwargs):
        from users.notification_utils import create_notification

        if created:
            # Notify the employee's direct manager when one is assigned and
            # active; otherwise fall back to staff users as a proxy for HR.
            manager = instance.employee.manager
            if manager and manager.is_active and manager.id != instance.employee_id:
                managers = [manager]
            else:
                managers = User.objects.filter(
                    is_active=True, is_staff=True
                ).exclude(id=instance.employee_id)[:10]
            employee_name = instance.employee.get_full_name() or instance.employee.email
            for manager in managers:
                create_notification(
                    user=manager,
                    notification_type='leave_request_submitted',
                    title='New leave request',
                    message=f'{employee_name} submitted a leave request ({instance.leave_type})',
                    metadata={
                        'leave_request_id': instance.id,
                        'employee_name': employee_name,
                        'leave_type': str(instance.leave_type),
                        'start_date': str(instance.start_date),
                        'end_date': str(instance.end_date),
                    },
                    link_url=f'/leave/team-requests?id={instance.id}',
                )
            return

        old_status = getattr(instance, '_old_status', None)
        if old_status == instance.status:
            return

        # Set by the views on the request object before saving; used to keep
        # the person who acted out of their own notifications.
        actor = getattr(instance, '_leave_actor', None)
        actor_id = getattr(actor, 'pk', None)
        period = f'{instance.start_date} - {instance.end_date}'
        employee_name = instance.employee.get_full_name() or instance.employee.email

        # Partial approval (pending -> manager_approved -> hr_approved): tell
        # the employee it moved on, and the next approver it is their turn.
        if instance.status in ('manager_approved', 'hr_approved'):
            from leave_management.utils import get_next_approver_role

            stage = 'manager' if instance.status == 'manager_approved' else 'HR'
            next_role = get_next_approver_role(instance)
            next_label = {'manager': 'manager', 'hr': 'HR', 'admin': 'admin'}.get(next_role, 'final')
            if instance.employee_id != actor_id:
                create_notification(
                    user=instance.employee,
                    notification_type='leave_request_progress',
                    title='Leave request partially approved',
                    message=(
                        f'Your leave request for {period} was approved by {stage}; '
                        f'it now awaits {next_label} approval'
                    ),
                    metadata={
                        'leave_request_id': instance.id,
                        'status': instance.status,
                        'next_approver_role': next_role,
                    },
                    link_url=f'/leave/my-requests?id={instance.id}',
                )

            if next_role:
                for approver, link in _leave_next_approvers(instance, next_role, actor_id):
                    create_notification(
                        user=approver,
                        notification_type='leave_request_awaiting_approval',
                        title=f'Leave request awaiting {next_label} approval',
                        message=f'{employee_name} has a leave request ({instance.leave_type}) for {period} waiting for your approval',
                        metadata={
                            'leave_request_id': instance.id,
                            'employee_name': employee_name,
                            'leave_type': str(instance.leave_type),
                            'start_date': str(instance.start_date),
                            'end_date': str(instance.end_date),
                            'status': instance.status,
                            'next_approver_role': next_role,
                        },
                        link_url=link,
                    )

        # Final approval
        if instance.status == 'approved':
            create_notification(
                user=instance.employee,
                notification_type='leave_request_approved',
                title='Leave request approved',
                message=f'Your leave request for {period} has been approved',
                metadata={
                    'leave_request_id': instance.id,
                    'status': instance.status,
                },
                link_url=f'/leave/my-requests?id={instance.id}',
            )

        # Rejected
        if instance.status == 'rejected':
            create_notification(
                user=instance.employee,
                notification_type='leave_request_rejected',
                title='Leave request rejected',
                message=f'Your leave request for {period} has been rejected',
                metadata={
                    'leave_request_id': instance.id,
                    'rejection_reason': instance.rejection_reason or '',
                },
                link_url=f'/leave/my-requests?id={instance.id}',
            )

        # Cancelled: the employee (when someone else cancelled it) and the
        # people who already approved it or were due to approve it.
        if instance.status == 'cancelled':
            metadata = {
                'leave_request_id': instance.id,
                'previous_status': old_status,
                'cancellation_reason': instance.cancellation_reason or '',
            }
            if instance.employee_id != actor_id:
                create_notification(
                    user=instance.employee,
                    notification_type='leave_request_cancelled',
                    title='Leave request cancelled',
                    message=f'Your leave request for {period} has been cancelled',
                    metadata=metadata,
                    link_url=f'/leave/my-requests?id={instance.id}',
                )
            recipients = {}
            for approver in (instance.manager_approver, instance.hr_approver, instance.final_approver):
                if approver is not None:
                    recipients[approver.pk] = approver
            if old_status == 'pending' and not recipients:
                manager = instance.employee.manager
                if manager is not None:
                    recipients[manager.pk] = manager
            for approver in recipients.values():
                if approver.pk in (actor_id, instance.employee_id) or not approver.is_active:
                    continue
                create_notification(
                    user=approver,
                    notification_type='leave_request_cancelled',
                    title='Leave request cancelled',
                    message=f"{employee_name}'s leave request for {period} has been cancelled",
                    metadata={**metadata, 'employee_name': employee_name},
                    link_url=_leave_approver_link(approver, instance.id),
                )

    def _leave_approver_link(user, leave_request_id):
        """Staff open the admin list, managers their team list."""
        if user.is_staff or user.is_superuser:
            return f'/leave/all-requests?id={leave_request_id}'
        return f'/leave/team-requests?id={leave_request_id}'

    def _leave_next_approvers(instance, next_role, actor_id):
        """
        (user, link_url) pairs for whoever acts on the next approval step.

        - manager: the employee's active manager, else up to 10 staff users
          (same rule as for a new request), linking to the team list;
        - hr / admin: up to 10 active staff or superusers (HR means staff
          until tenant groups can grant the leave permissions), linking to
          the admin list.
        """
        exclude = {instance.employee_id}
        if actor_id:
            exclude.add(actor_id)

        if next_role == 'manager':
            manager = instance.employee.manager
            if manager and manager.is_active and manager.pk not in exclude:
                return [(manager, f'/leave/team-requests?id={instance.id}')]

        approvers = User.objects.filter(
            Q(is_staff=True) | Q(is_superuser=True), is_active=True
        ).exclude(id__in=exclude).order_by('id')[:10]
        return [(u, _leave_approver_link(u, instance.id)) for u in approvers]

except ImportError:
    pass

# ---------------------------------------------------------------------------
# Booking Management
# ---------------------------------------------------------------------------
try:
    from booking_management.models import Booking

    @receiver(pre_save, sender=Booking)
    def track_booking_status_change(sender, instance, **kwargs):
        """Remember old status so post_save can detect transitions."""
        if instance.pk:
            try:
                old = Booking.objects.get(pk=instance.pk)
                instance._old_status = old.status
            except Booking.DoesNotExist:
                instance._old_status = None
        else:
            instance._old_status = None

    @receiver(post_save, sender=Booking)
    def notify_booking_events(sender, instance, created, **kwargs):
        from users.notification_utils import create_notification

        old_status = getattr(instance, '_old_status', None)

        # Booking confirmed
        if old_status != 'confirmed' and instance.status == 'confirmed':
            # Notify assigned staff
            if instance.staff and instance.staff.user:
                create_notification(
                    user=instance.staff.user,
                    notification_type='booking_confirmed',
                    title=f'Booking {instance.booking_number} confirmed',
                    message=f'Booking on {instance.date} at {instance.start_time} has been confirmed',
                    metadata={
                        'booking_id': instance.id,
                        'booking_number': instance.booking_number,
                        'date': str(instance.date),
                        'start_time': str(instance.start_time),
                    },
                    link_url=f'/bookings/bookings/{instance.id}',
                )

        # Booking cancelled
        if old_status != 'cancelled' and instance.status == 'cancelled':
            if instance.staff and instance.staff.user:
                create_notification(
                    user=instance.staff.user,
                    notification_type='booking_cancelled',
                    title=f'Booking {instance.booking_number} cancelled',
                    message=f'Booking on {instance.date} at {instance.start_time} has been cancelled',
                    metadata={
                        'booking_id': instance.id,
                        'booking_number': instance.booking_number,
                        'cancelled_by': instance.cancelled_by or '',
                    },
                    link_url=f'/bookings/bookings/{instance.id}',
                )

except ImportError:
    pass

# ---------------------------------------------------------------------------
# Calls (CRM CallLog)
# ---------------------------------------------------------------------------
try:
    from crm.models import CallLog

    @receiver(post_save, sender=CallLog)
    def notify_call_events(sender, instance, created, **kwargs):
        from users.notification_utils import create_notification

        # Only notify on missed inbound calls
        if instance.status != 'missed' or instance.direction != 'inbound':
            return

        if instance.handled_by:
            # Notify the specific user who was supposed to handle the call
            create_notification(
                user=instance.handled_by,
                notification_type='call_missed',
                title='Missed call',
                message=f'Missed inbound call from {instance.caller_number}',
                metadata={
                    'call_id': str(instance.call_id),
                    'caller_number': instance.caller_number,
                    'direction': instance.direction,
                },
                link_url='/calls',
            )
        else:
            # Notify up to 10 active users
            users = User.objects.filter(is_active=True)[:10]
            for user in users:
                create_notification(
                    user=user,
                    notification_type='call_missed',
                    title='Missed call',
                    message=f'Missed inbound call from {instance.caller_number}',
                    metadata={
                        'call_id': str(instance.call_id),
                        'caller_number': instance.caller_number,
                        'direction': instance.direction,
                    },
                    link_url='/calls',
                )

except ImportError:
    pass
