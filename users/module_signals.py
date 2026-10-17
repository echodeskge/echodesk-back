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
                link_url=f'/leave/my-requests?id={instance.id}',
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
                link_url=f'/leave/my-requests?id={instance.id}',
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
# E-commerce orders
# ---------------------------------------------------------------------------
try:
    from ecommerce_crm.models import Order as EcommerceOrder

    @receiver(pre_save, sender=EcommerceOrder)
    def track_ecommerce_order_changes(sender, instance, **kwargs):
        """Remember the previous payment state so post_save sees transitions."""
        instance._old_payment_status = None
        instance._old_payment_method = None
        if instance.pk:
            old = EcommerceOrder.objects.filter(pk=instance.pk).values(
                'payment_status', 'payment_method'
            ).first()
            if old:
                instance._old_payment_status = old['payment_status']
                instance._old_payment_method = old['payment_method']

    @receiver(post_save, sender=EcommerceOrder)
    def notify_ecommerce_order_events(sender, instance, created, **kwargs):
        """ecommerce_order_created for cash-on-delivery orders when placed,
        ecommerce_order_paid when payment_status becomes paid. Unpaid card
        orders never notify (the sweeper cancels the abandoned ones)."""
        from django.db import transaction

        old_payment_status = getattr(instance, '_old_payment_status', None)
        old_payment_method = getattr(instance, '_old_payment_method', None)

        became_paid = instance.payment_status == 'paid' and old_payment_status != 'paid'
        became_cod = (
            instance.payment_method == 'cash_on_delivery'
            and (created or old_payment_method != 'cash_on_delivery')
            and instance.status != 'cancelled'
            and instance.payment_status != 'paid'
        )
        if not (became_paid or became_cod):
            return

        order_id = instance.pk

        def _send():
            import logging
            from ecommerce_crm import notifications
            try:
                order = EcommerceOrder.objects.select_related('client').filter(pk=order_id).first()
                if order is None:
                    return
                if became_cod:
                    notifications.notify_order_created(order)
                if became_paid:
                    notifications.notify_order_paid(order)
            except Exception:
                # Never fail a checkout or a payment webhook over a notification.
                logging.getLogger(__name__).exception(
                    'E-commerce order notification failed for order %s', order_id
                )

        # After commit: a rolled-back checkout must not announce an order.
        transaction.on_commit(_send)

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
