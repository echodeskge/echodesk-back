from decimal import Decimal
import logging

from django.db import connection, transaction

from tenants.bog_payment import BOGPaymentService
from tenants.models import Tenant
from .models import Booking, BookingSettings

logger = logging.getLogger(__name__)


class BookingPaymentNotConfigured(RuntimeError):
    """Card payment was requested but the tenant has no usable BOG credentials."""


class BookingPaymentService:
    """
    Payment service for booking management
    Wrapper around BOGPaymentService with booking-specific logic
    """

    def __init__(self, tenant=None):
        """
        Initialize payment service

        Args:
            tenant: Tenant instance (optional, will auto-detect from schema)
        """
        if tenant is None:
            try:
                tenant = Tenant.objects.get(schema_name=connection.schema_name)
            except Tenant.DoesNotExist:
                raise ValueError("Could not determine tenant")

        self.tenant = tenant

        # Get or create booking settings
        self.settings, created = BookingSettings.objects.get_or_create(tenant=tenant)

        self._bog_service = None
        # What BOG said the last time process_webhook asked ('paid', 'failed',
        # 'pending', 'processing', 'unknown', 'error', or None if not asked).
        self.last_state = None

    @property
    def bog_service(self):
        """BOG client using the tenant's own merchant credentials.

        Built lazily, and never with fallback credentials: charging a salon's
        customer into some other merchant account would be worse than failing.
        """
        if self._bog_service is None:
            client_id = self.settings.bog_client_id
            client_secret = self.settings.bog_client_secret
            if not client_id or not client_secret:
                raise BookingPaymentNotConfigured(
                    f"Tenant {self.tenant.schema_name} has no BOG credentials for bookings"
                )
            service = BOGPaymentService()
            service.client_id = client_id
            service.client_secret = client_secret
            self._bog_service = service
        return self._bog_service

    def create_booking_payment(self, booking, callback_url, return_url_success='', return_url_fail=''):
        """
        Create BOG payment for a booking

        Args:
            booking: Booking instance
            callback_url: Webhook URL for payment notifications
            return_url_success / return_url_fail: where BOG sends the customer afterwards

        Returns:
            dict: Payment result with order_id and payment_url
        """
        # Determine amount to charge (deposit or full)
        amount = booking.deposit_amount if booking.deposit_amount > 0 else booking.total_amount

        # Create payment description
        from .utils_text import localized_text
        service_name = localized_text(booking.service.name, 'en') or 'Service'
        description = f"Booking {booking.booking_number} - {service_name}"

        # Create payment
        try:
            result = self.bog_service.create_payment(
                amount=float(amount),
                currency='GEL',
                description=description,
                customer_email=booking.client.email or '',
                customer_name=booking.client.full_name,
                customer_phone=booking.client.phone or '',
                return_url_success=return_url_success,
                return_url_fail=return_url_fail,
                callback_url=callback_url,
                external_order_id=booking.booking_number
            )

            # Update booking with payment details
            booking.bog_order_id = result['order_id']
            booking.payment_url = result['payment_url']
            booking.payment_metadata = result
            booking.save(update_fields=['bog_order_id', 'payment_url', 'payment_metadata'])

            return result

        except Exception as e:
            logger.error(f"Failed to create booking payment for {booking.booking_number}: {str(e)}")
            raise

    def process_webhook(self, webhook_data):
        """
        Process BOG webhook notification.

        The callback body is not trusted: it only tells us which booking to
        look at. Whether money actually moved is decided by asking BOG for the
        order's status with the tenant's own credentials.

        Returns:
            Booking: Updated booking instance, or None when the callback doesn't
            match a booking / can't be confirmed yet.
        """
        body = webhook_data.get('body') or {}
        external_order_id = body.get('external_order_id') or ''
        callback_order_id = body.get('order_id') or body.get('id') or ''

        if not external_order_id and not callback_order_id:
            logger.warning("Booking payment webhook without order identifiers")
            return None

        with transaction.atomic():
            lookup = Booking.objects.select_for_update()
            booking = None
            if external_order_id:
                booking = lookup.filter(booking_number=external_order_id).first()
            if booking is None and callback_order_id:
                booking = lookup.filter(bog_order_id=callback_order_id).first()
            if booking is None:
                logger.error("Booking not found for webhook: %s / %s", external_order_id, callback_order_id)
                return None

            if not booking.bog_order_id:
                logger.warning("Webhook for booking %s which has no BOG order", booking.booking_number)
                return None

            # Idempotent: a paid booking stays as it is on repeated callbacks.
            if booking.payment_status in ('deposit_paid', 'fully_paid', 'refunded'):
                return booking

            status = self.bog_service.check_payment_status(booking.bog_order_id)
            state = status.get('status')
            self.last_state = state

            if state == 'paid':
                paid = Decimal(str(status.get('amount') or 0))
                if paid <= 0:
                    # Receipt didn't carry an amount; we charged exactly this.
                    paid = booking.deposit_amount if booking.deposit_amount > 0 else booking.total_amount

                booking.paid_amount = paid
                booking.payment_status = 'fully_paid' if paid >= booking.total_amount else 'deposit_paid'
                booking.payment_metadata = {
                    'bog_status': status.get('bog_status'),
                    'transaction_id': status.get('transaction_id'),
                    'response_code': status.get('response_code'),
                }
                booking.save(update_fields=['paid_amount', 'payment_status', 'payment_metadata', 'updated_at'])

                if booking.status == 'cancelled':
                    # Money arrived for a booking that was already cancelled (the
                    # customer cancelled, or the unpaid sweeper ran first). The
                    # slot may be gone, so don't resurrect it: give the money back.
                    metadata = dict(booking.payment_metadata or {})
                    try:
                        result = self.bog_service.refund_payment(order_id=booking.bog_order_id, amount=None)
                        metadata['refund'] = {'status': 'requested', 'amount': str(paid), 'response': result,
                                              'reason': 'paid after cancellation'}
                        booking.payment_status = 'refunded'
                        booking.paid_amount = Decimal('0')
                        logger.warning("Refunded late payment for cancelled booking %s", booking.booking_number)
                    except Exception as exc:
                        metadata['refund'] = {'status': 'manual_required', 'amount': str(paid), 'error': str(exc),
                                              'reason': 'paid after cancellation'}
                        logger.error(
                            "BOG CONFIRMED payment for CANCELLED booking %s — manual refund required: %s",
                            booking.booking_number, exc,
                        )
                    booking.payment_metadata = metadata
                    booking.save(update_fields=['payment_status', 'paid_amount', 'payment_metadata', 'updated_at'])
                elif booking.status == 'pending':
                    if booking.payment_status == 'fully_paid' and self.settings.auto_confirm_on_full_payment:
                        booking.confirm()
                    elif booking.payment_status == 'deposit_paid' and self.settings.auto_confirm_on_deposit:
                        booking.confirm()

                logger.info("Booking %s payment successful. Status: %s", booking.booking_number, booking.payment_status)

            elif state == 'failed':
                booking.payment_status = 'failed'
                booking.save(update_fields=['payment_status', 'updated_at'])
                logger.warning("Booking %s payment failed (%s)", booking.booking_number, status.get('bog_status'))
                # The order can't be paid any more: free the slot right away
                # so the customer (or anyone) can book the time again.
                if booking.status == 'pending':
                    booking.cancel(cancelled_by='admin', reason='Card payment was declined', notify=False)

            else:
                # pending / processing / unknown / error: leave the booking alone;
                # BOG calls back again when the order settles.
                logger.info("Booking %s payment not final yet: %s", booking.booking_number, state)

            return booking

    def initiate_refund(self, booking):
        """
        Refund a cancelled booking according to the tenant's refund policy.

        Returns:
            dict: {'status': 'no_refund' | 'refunded' | 'manual_required', 'amount': Decimal}
        Never raises: a refund that can't be made automatically is recorded on
        the booking for staff to handle, and the cancellation stands.
        """
        if not booking.bog_order_id or not booking.paid_amount:
            return {'status': 'no_refund', 'amount': Decimal('0')}

        # Never refund the same booking twice.
        if ((booking.payment_metadata or {}).get('refund') or {}).get('status') == 'requested':
            return {'status': 'no_refund', 'amount': Decimal('0')}

        # Calculate refund amount based on policy
        from .utils import calculate_refund_amount
        refund_amount = calculate_refund_amount(booking, self.settings)

        if refund_amount <= 0:
            logger.info(f"No refund for booking {booking.booking_number} due to policy")
            return {'status': 'no_refund', 'amount': Decimal('0')}

        metadata = dict(booking.payment_metadata or {})
        try:
            full = refund_amount >= booking.paid_amount
            result = self.bog_service.refund_payment(
                order_id=booking.bog_order_id,
                amount=None if full else refund_amount,
            )
            metadata['refund'] = {'status': 'requested', 'amount': str(refund_amount), 'response': result}
            booking.payment_status = 'refunded'
            booking.paid_amount = max(Decimal('0'), booking.paid_amount - refund_amount)
            booking.payment_metadata = metadata
            booking.save(update_fields=['payment_status', 'paid_amount', 'payment_metadata', 'updated_at'])
            logger.info(f"Refund initiated for booking {booking.booking_number}: {refund_amount} GEL")
            return {'status': 'refunded', 'amount': refund_amount}

        except Exception as e:
            logger.error(
                "Refund for booking %s (%s GEL) needs manual handling: %s",
                booking.booking_number, refund_amount, e,
            )
            metadata['refund'] = {'status': 'manual_required', 'amount': str(refund_amount), 'error': str(e)}
            booking.payment_metadata = metadata
            booking.save(update_fields=['payment_metadata', 'updated_at'])
            return {'status': 'manual_required', 'amount': refund_amount}

    def check_payment_status(self, booking):
        """
        Check payment status from BOG

        Args:
            booking: Booking instance

        Returns:
            dict: Payment status info
        """
        if not booking.bog_order_id:
            return {'status': 'no_payment'}

        try:
            status = self.bog_service.check_payment_status(booking.bog_order_id)
            return status
        except Exception as e:
            logger.error(f"Failed to check payment status for {booking.booking_number}: {str(e)}")
            raise


# Convenience function to get payment service
def get_booking_payment_service(tenant=None):
    """Get or create booking payment service instance"""
    return BookingPaymentService(tenant=tenant)
