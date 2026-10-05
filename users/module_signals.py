"""
Signal handlers for optional modules (invoices, leave, bookings, calls).

Each block is guarded by try/except ImportError so the app works even if a
module is not installed.  Registered via UsersConfig.ready().
"""

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model

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
            # Notify managers / HR — use users with is_staff as a proxy
            managers = User.objects.filter(is_active=True, is_staff=True)[:10]
            employee_name = instance.employee.get_full_name() or instance.employee.email
            for manager in managers:
                if manager.id == instance.employee_id:
                    continue  # Don't notify the requester
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
                    link_url=f'/leave/requests/{instance.id}',
                )
            return

        old_status = getattr(instance, '_old_status', None)

        # Approved (any approved state)
        if old_status not in ('approved', 'manager_approved', 'hr_approved') and instance.status in ('approved', 'manager_approved', 'hr_approved'):
            create_notification(
                user=instance.employee,
                notification_type='leave_request_approved',
                title='Leave request approved',
                message=f'Your leave request for {instance.start_date} - {instance.end_date} has been approved',
                metadata={
                    'leave_request_id': instance.id,
                    'status': instance.status,
                },
                link_url=f'/leave/requests/{instance.id}',
            )

        # Rejected
        if old_status != 'rejected' and instance.status == 'rejected':
            create_notification(
                user=instance.employee,
                notification_type='leave_request_rejected',
                title='Leave request rejected',
                message=f'Your leave request for {instance.start_date} - {instance.end_date} has been rejected',
                metadata={
                    'leave_request_id': instance.id,
                    'rejection_reason': instance.rejection_reason or '',
                },
                link_url=f'/leave/requests/{instance.id}',
            )

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
