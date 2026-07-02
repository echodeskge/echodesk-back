"""
Payment views for subscription management
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema, OpenApiResponse, inline_serializer
from rest_framework import serializers
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from .models import (
    Tenant, TenantSubscription, UsageLog, PaymentOrder,
    PendingRegistration, SavedCard, Invoice, PaymentAttempt,
    SubscriptionEvent, PaymentRetrySchedule
)
from .bog_payment import bog_service
from .services import SingleFrontendDeploymentService
from .email_service import email_service
from .subscription_utils import schedule_payment_retries, cancel_pending_retries
from .telegram_notifications import (
    notify_subscription_created, notify_payment_success, notify_payment_failed,
    notify_retry_success, notify_retry_scheduled, notify_all_retries_exhausted
)
from tenant_schemas.utils import schema_context
from django.contrib.auth import get_user_model
from django.db import transaction
import logging

# Package model was removed (feature-based pricing now).
# Stub class to prevent NameError in deprecated code paths.
class _DeprecatedPackage:
    DoesNotExist = type('DoesNotExist', (Exception,), {})
    class objects:
        @staticmethod
        def get(**kwargs):
            raise _DeprecatedPackage.DoesNotExist('Package-based pricing is deprecated')
Package = _DeprecatedPackage

User = get_user_model()

logger = logging.getLogger(__name__)


def get_next_billing_date():
    """
    Calculate next billing date based on TEST_BILLING_INTERVAL setting

    Returns:
        datetime: Next billing date (2 minutes for testing, 30 days for production)
    """
    if getattr(settings, 'TEST_BILLING_INTERVAL', False):
        # Test mode: 2 minutes
        return timezone.now() + timedelta(minutes=2)
    else:
        # Production mode: 30 days
        return timezone.now() + timedelta(days=30)


def create_subscription_event(subscription, event_type, payment_order=None, payment_attempt=None, description='', metadata=None):
    """
    Helper function to create subscription events

    Args:
        subscription: TenantSubscription instance
        event_type: Event type (e.g., 'created', 'payment_success')
        payment_order: Optional PaymentOrder
        payment_attempt: Optional PaymentAttempt
        description: Event description
        metadata: Additional event metadata
    """
    return SubscriptionEvent.objects.create(
        subscription=subscription,
        tenant=subscription.tenant,
        event_type=event_type,
        payment_order=payment_order,
        payment_attempt=payment_attempt,
        description=description,
        metadata=metadata or {}
    )


def trigger_tenant_processing(schema_name):
    """
    Trigger the tenant processing command via Celery

    This dispatches the process_pending_tenants task asynchronously
    without blocking the webhook response.
    """
    try:
        from tenants.tasks import process_pending_tenant
        process_pending_tenant.delay(schema_name)
        logger.info(f'Dispatched process_pending_tenant task for: {schema_name}')
    except Exception as e:
        logger.error(f'Failed to dispatch tenant processing for {schema_name}: {e}')


@extend_schema(
    operation_id='create_subscription_payment',
    summary='Create Subscription Payment',
    description='Create a payment session for subscribing to or upgrading a package',
    request={
        'type': 'object',
        'properties': {
            'package_id': {'type': 'integer'},
            'agent_count': {'type': 'integer', 'default': 1},
        },
        'required': ['package_id']
    },
    responses={
        200: OpenApiResponse(
            description='Payment session created',
            response={
                'type': 'object',
                'properties': {
                    'payment_id': {'type': 'string'},
                    'payment_url': {'type': 'string'},
                    'amount': {'type': 'number'},
                    'currency': {'type': 'string'}
                }
            }
        ),
        400: OpenApiResponse(description='Invalid request'),
        404: OpenApiResponse(description='Package not found'),
        500: OpenApiResponse(description='Payment gateway error')
    },
    tags=['Payments']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_subscription_payment(request):
    """
    Create a payment session for subscription

    For agent-based pricing, provide agent_count
    For CRM-based pricing, agent_count is ignored
    """
    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    package_id = request.data.get('package_id')
    agent_count = request.data.get('agent_count', 1)

    if not package_id:
        return Response(
            {'error': 'package_id is required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        package = Package.objects.get(id=package_id, is_active=True)
    except Package.DoesNotExist:
        return Response(
            {'error': 'Package not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    # Validate agent_count
    if package.pricing_model == 'agent':
        if agent_count < 1:
            return Response(
                {'error': 'agent_count must be at least 1'},
                status=status.HTTP_400_BAD_REQUEST
            )
    else:
        agent_count = 1  # Not applicable for CRM-based

    # Check if BOG is configured
    if not bog_service.is_configured():
        return Response(
            {
                'error': 'Payment gateway not configured',
                'message': 'Please contact support to set up payment processing'
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    # Generate URLs
    # Get frontend URL by removing 'api.' from the request host
    api_host = request.get_host()
    frontend_host = api_host.replace('api.', '')
    frontend_url = f"https://{frontend_host}"
    return_url = f"{frontend_url}/settings/subscription/success"

    # Ensure callback_url starts with https://
    api_domain = settings.API_DOMAIN
    if not api_domain.startswith('http'):
        api_domain = f"https://{api_domain}"
    callback_url = f"{api_domain}/api/payments/webhook/"

    try:
        # Generate external order ID
        import uuid
        external_order_id = f"SUB-{uuid.uuid4().hex[:12].upper()}"

        # Create payment session
        payment_result = bog_service.create_subscription_payment(
            tenant=request.tenant,
            package=package,
            agent_count=agent_count,
            return_url_success=return_url,
            return_url_fail=f"{frontend_url}/settings/subscription/failed",
            callback_url=callback_url,
            external_order_id=external_order_id
        )

        # Store payment order in database
        payment_order = PaymentOrder.objects.create(
            order_id=external_order_id,  # Store our external_order_id for webhook lookup
            bog_order_id=payment_result['order_id'],  # Store BOG's internal order_id
            tenant=request.tenant,
            package=package,
            amount=payment_result['amount'],
            currency=payment_result['currency'],
            agent_count=agent_count,
            payment_url=payment_result['payment_url'],
            status='pending',
            metadata=payment_result.get('metadata', {})
        )

        logger.info(f'Payment order created: {payment_order.order_id} for tenant {request.tenant.schema_name}')

        return Response({
            'payment_id': payment_result.get('payment_id'),
            'payment_url': payment_result.get('payment_url'),
            'amount': payment_result.get('amount'),
            'currency': payment_result.get('currency', 'GEL'),
            'status': 'pending'
        })

    except Exception as e:
        logger.error(f'Failed to create payment for tenant {request.tenant.schema_name}: {e}')
        return Response(
            {
                'error': 'Payment creation failed',
                'message': str(e)
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    operation_id='check_payment_status',
    summary='Check Payment Status',
    description='Check the status of a payment transaction',
    responses={
        200: OpenApiResponse(
            description='Payment status',
            response={
                'type': 'object',
                'properties': {
                    'payment_id': {'type': 'string'},
                    'status': {'type': 'string'},
                    'amount': {'type': 'number'},
                    'paid': {'type': 'boolean'}
                }
            }
        ),
        404: OpenApiResponse(description='Payment not found')
    },
    tags=['Payments']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def check_payment_status(request, payment_id):
    """
    Check the status of a payment
    """
    if not bog_service.is_configured():
        return Response(
            {'error': 'Payment gateway not configured'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    try:
        payment_status = bog_service.check_payment_status(payment_id)
        return Response(payment_status)

    except Exception as e:
        logger.error(f'Failed to check payment status for {payment_id}: {e}')
        return Response(
            {'error': 'Failed to check payment status'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    operation_id='bog_webhook',
    summary='BOG Payment Webhook',
    description='Webhook endpoint for receiving payment status updates from Bank of Georgia',
    responses={
        200: OpenApiResponse(description='Webhook processed successfully'),
        400: OpenApiResponse(description='Invalid signature or payload')
    },
    tags=['Payments']
)
def generate_invoice_for_payment(payment_order, tenant, agent_count, description=None):
    """
    Generate an invoice for a successful payment
    Returns the created Invoice object
    """
    try:
        # Check if invoice already exists for this payment order
        if hasattr(payment_order, 'invoice'):
            logger.info(f'Invoice already exists for payment order {payment_order.order_id}')
            return payment_order.invoice

        # Don't generate invoice for 0 GEL payments (trial or card-only)
        if payment_order.amount <= 0:
            logger.info(f'Skipping invoice generation for 0 GEL payment: {payment_order.order_id}')
            return None

        # Generate invoice description if not provided
        if not description:
            description = "Subscription payment"
            if agent_count > 1:
                description += f" for {agent_count} agents"

        # Create invoice
        invoice = Invoice.objects.create(
            tenant=tenant,
            payment_order=payment_order,
            amount=payment_order.amount,
            currency=payment_order.currency,
            description=description,
            agent_count=agent_count,
            paid_date=payment_order.paid_at or timezone.now(),
            metadata={
                'order_id': payment_order.order_id,
                'bog_order_id': payment_order.bog_order_id,
            }
        )

        logger.info(f'Invoice {invoice.invoice_number} generated for payment order {payment_order.order_id}')

        # Dispatch async PDF generation + email delivery
        try:
            from tenants.tasks import generate_tenant_invoice_pdf
            generate_tenant_invoice_pdf.delay(invoice.id)
        except Exception as pdf_exc:
            logger.error(f'Failed to dispatch PDF task for invoice {invoice.invoice_number}: {pdf_exc}')

        return invoice

    except Exception as e:
        logger.error(f'Error generating invoice for payment order {payment_order.order_id}: {str(e)}')
        return None


@api_view(['POST'])
@permission_classes([AllowAny])  # Webhook from BOG, no auth
def bog_webhook(request):
    """
    Handle webhook notifications from Bank of Georgia payment gateway

    This endpoint processes payment status updates and creates/updates subscriptions

    BOG Callback format:
    {
        "event": "order_payment",
        "zoned_request_time": "2024-01-01T12:00:00.000000Z",
        "body": {
            "order_id": "...",
            "order_status": {"key": "completed", ...},
            ...
        }
    }
    """
    # Optional: Verify webhook signature
    signature = request.headers.get('Callback-Signature', '')
    if signature:
        # TODO: Implement signature verification when BOG provides public key
        # bog_service.verify_webhook_signature(request.body, signature)
        logger.info('Webhook signature received (verification not implemented)')

    # Process payment event
    event_type = request.data.get('event')
    body = request.data.get('body', {})

    if event_type != 'order_payment':
        logger.warning(f'Unexpected webhook event type: {event_type}')
        return Response({'error': 'Unexpected event type'}, status=status.HTTP_400_BAD_REQUEST)

    # BOG sends their internal order_id and our external_order_id
    bog_order_id = body.get('order_id') or body.get('id')
    external_order_id = body.get('external_order_id')
    order_status_obj = body.get('order_status', {})
    bog_status = order_status_obj.get('key', '')

    # Get response code from payment_detail
    payment_detail = body.get('payment_detail', {})
    response_code = payment_detail.get('code', '')
    transaction_id = payment_detail.get('transaction_id', '')

    logger.info(f'BOG webhook received: bog_order_id={bog_order_id}, external_order_id={external_order_id}, status={bog_status}, code={response_code}')

    # Handle successful payment (BOG status: 'completed' with response code '100')
    if bog_status == 'completed' and response_code == '100':
        try:
            # Get payment order from database using external_order_id (we store this as order_id)
            if not external_order_id:
                logger.error('Missing external_order_id in webhook payload')
                return Response({'error': 'Invalid payload'}, status=status.HTTP_400_BAD_REQUEST)

            if not bog_order_id:
                logger.error('Missing BOG order_id in webhook payload')
                return Response({'error': 'Invalid payload'}, status=status.HTTP_400_BAD_REQUEST)

            # SECURITY: don't trust the webhook body — confirm with BOG directly
            # before provisioning a paid subscription. A forged "completed" POST
            # will not survive this check.
            try:
                confirmation = bog_service.check_payment_status(bog_order_id)
            except Exception:
                logger.exception('BOG status re-query failed for %s', bog_order_id)
                return Response({'error': 'verification_unavailable'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

            if confirmation.get('status') in ('error', 'unknown'):
                logger.warning('BOG status inconclusive for %s: %s', bog_order_id, confirmation.get('error'))
                return Response({'error': 'verification_unavailable'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

            if not (confirmation.get('bog_status') == 'completed' and str(confirmation.get('response_code')) == '100'):
                logger.warning(
                    'BOG did NOT confirm payment for %s (bog_status=%s) — ignoring webhook',
                    bog_order_id, confirmation.get('bog_status')
                )
                return Response({'status': 'ignored', 'message': 'Payment not confirmed by BOG'}, status=status.HTTP_200_OK)

            # Lock the payment order and re-check idempotency under the lock so
            # concurrent duplicate callbacks can't both mark it paid.
            with transaction.atomic():
                try:
                    payment_order = PaymentOrder.objects.select_for_update().get(order_id=external_order_id)
                except PaymentOrder.DoesNotExist:
                    logger.error(f'Payment order not found: {external_order_id}')
                    return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)

                # Check if already processed (idempotency - handle duplicate webhooks)
                if payment_order.status == 'paid' and payment_order.tenant is not None:
                    logger.info(f'Webhook already processed for order: {external_order_id}, returning success')
                    return Response({
                        'status': 'success',
                        'message': 'Payment already processed'
                    }, status=status.HTTP_200_OK)

                # Update payment order status
                payment_order.status = 'paid'
                payment_order.paid_at = timezone.now()
                payment_order.bog_order_id = bog_order_id  # Save BOG's order ID
                payment_order.save()

            # Determine if this is a retry attempt
            is_retry_payment = external_order_id.startswith('RETRY-')
            attempt_num = 1

            if is_retry_payment:
                # Extract retry number from order ID if possible
                try:
                    # Format: RETRY-{retry_schedule_id}-{original_order_id}
                    retry_id = external_order_id.split('-')[1]
                    retry_schedule = PaymentRetrySchedule.objects.get(id=retry_id)
                    attempt_num = retry_schedule.retry_number + 1
                except:
                    attempt_num = 2  # Default to 2 for retries

            # Create successful PaymentAttempt record
            attempt = PaymentAttempt.objects.create(
                payment_order=payment_order,
                subscription=payment_order.tenant.subscription if payment_order.tenant else None,
                tenant=payment_order.tenant if payment_order.tenant else None,
                attempt_number=attempt_num,
                status='success',
                bog_order_id=bog_order_id,
                amount=payment_order.amount,
                attempted_at=timezone.now(),
                completed_at=timezone.now(),
                bog_response=body,
                is_retry=is_retry_payment,
            )

            # If this is a successful retry, update retry schedule and cancel remaining retries
            if is_retry_payment and payment_order.tenant and hasattr(payment_order.tenant, 'subscription'):
                subscription = payment_order.tenant.subscription

                # Mark payment as successful
                subscription.mark_payment_succeeded()

                # Cancel all pending retries
                cancelled_count = cancel_pending_retries(subscription, reason='Payment succeeded on retry')

                # Mark the retry schedule that triggered this as succeeded
                try:
                    retry_id = external_order_id.split('-')[1]
                    retry_schedule = PaymentRetrySchedule.objects.get(id=retry_id)
                    retry_schedule.status = 'succeeded'
                    retry_schedule.executed_at = timezone.now()
                    retry_schedule.retry_attempt = attempt
                    retry_schedule.save()

                    # Create retry success event
                    create_subscription_event(
                        subscription=subscription,
                        event_type='retry_success',
                        payment_order=payment_order,
                        payment_attempt=attempt,
                        description=f'Retry #{retry_schedule.retry_number} succeeded',
                        metadata={
                            'retry_number': retry_schedule.retry_number,
                            'cancelled_remaining_retries': cancelled_count,
                        }
                    )

                    # Send Telegram notification for successful retry
                    try:
                        notify_retry_success(subscription, attempt)
                    except Exception as e:
                        logger.error(f'Failed to send Telegram notification for retry success: {e}')

                except:
                    logger.warning(f'Could not find retry schedule for {external_order_id}')

                logger.info(f'Retry payment succeeded, cancelled {cancelled_count} pending retries')

            # Extract card save information from webhook
            saved_card_type = payment_detail.get('saved_card_type')
            parent_order_id_from_bog = payment_detail.get('parent_order_id')

            # For trial payments, BOG returns the parent order ID that should be used for future charges
            # This is the BOG order_id (not external_order_id) that we'll use in recurring payment API calls
            card_saved_for_recurring = saved_card_type == 'subscription'

            if card_saved_for_recurring:
                logger.info(f'Card saved for recurring payments. BOG order_id: {bog_order_id}')

            # Check if this is a registration payment (no tenant yet)
            if payment_order.tenant is None:
                # This is a registration payment - create tenant from pending registration
                try:
                    pending_registration = PendingRegistration.objects.get(
                        order_id=external_order_id,
                        is_processed=False
                    )
                except PendingRegistration.DoesNotExist:
                    # Check if it was already processed (duplicate webhook)
                    processed_registration = PendingRegistration.objects.filter(
                        order_id=external_order_id,
                        is_processed=True
                    ).first()

                    if processed_registration:
                        logger.info(f'Registration already processed for order: {external_order_id}')
                        return Response({
                            'status': 'success',
                            'message': 'Registration already processed'
                        }, status=status.HTTP_200_OK)

                    logger.error(f'Pending registration not found for order: {external_order_id}')
                    return Response({'error': 'Registration not found'}, status=status.HTTP_404_NOT_FOUND)

                # Check if expired
                if pending_registration.is_expired:
                    logger.error(f'Registration expired for order: {external_order_id}')
                    return Response({'error': 'Registration expired'}, status=status.HTTP_400_BAD_REQUEST)

                # Create tenant in a transaction
                with transaction.atomic():
                    # Determine max_users and max_storage based on package or feature-based pricing
                    if pending_registration.package:
                        # Legacy package-based
                        max_users = pending_registration.package.max_users or 1000
                        max_storage = pending_registration.package.max_storage_gb * 1024
                    else:
                        # Feature-based pricing - use agent_count or default limits
                        max_users = pending_registration.agent_count or 1000
                        max_storage = 100 * 1024  # Default 100 GB

                    # Create tenant
                    tenant = Tenant.objects.create(
                        schema_name=pending_registration.schema_name,
                        domain_url=f"{pending_registration.schema_name}.api.echodesk.ge",
                        name=pending_registration.name,
                        admin_email=pending_registration.admin_email,
                        admin_name=f"{pending_registration.admin_first_name} {pending_registration.admin_last_name}",
                        plan='paid',
                        max_users=max_users,
                        max_storage=max_storage,
                        deployment_status='deploying',
                        is_active=True
                    )

                    # Update payment order with tenant
                    payment_order.tenant = tenant
                    payment_order.save()

                    # Check if this is a trial payment
                    is_trial = payment_order.is_trial_payment
                    trial_days = payment_order.metadata.get('trial_days', 14)

                    # Create subscription with trial information
                    if is_trial:
                        trial_ends_at = timezone.now() + timedelta(days=trial_days)

                        # Use the BOG order_id from webhook as parent_order_id for future recurring charges
                        parent_order_id_for_subscription = bog_order_id if card_saved_for_recurring else None

                        subscription = TenantSubscription.objects.create(
                            tenant=tenant,
                            package=pending_registration.package,
                            is_active=True,
                            starts_at=timezone.now(),
                            expires_at=trial_ends_at,
                            agent_count=pending_registration.agent_count,
                            current_users=1,
                            whatsapp_messages_used=0,
                            storage_used_gb=0,
                            # Trial subscription fields
                            is_trial=True,
                            trial_ends_at=trial_ends_at,
                            trial_converted=False,
                            parent_order_id=parent_order_id_for_subscription,  # Save BOG order ID for future charges
                            # Billing will happen at end of trial
                            last_billed_at=None,
                            next_billing_date=trial_ends_at
                        )
                        logger.info(f'Trial subscription created for {tenant.schema_name}, ends at {trial_ends_at}, parent_order_id: {parent_order_id_for_subscription}')

                        # Generate invoice for non-trial payments
                        if payment_order.amount > 0:
                            generate_invoice_for_payment(
                                payment_order=payment_order,
                                tenant=tenant,
                                agent_count=pending_registration.agent_count,
                                description=f"EchoDesk Subscription - {tenant.name}"
                            )

                        # Save card details if card was saved for recurring payments
                        if card_saved_for_recurring and parent_order_id_for_subscription:
                            card_type = payment_detail.get('card_type', '')
                            masked_card = payment_detail.get('payer_identifier', '')
                            card_expiry = payment_detail.get('card_expiry_date', '')

                            SavedCard.objects.update_or_create(
                                tenant=tenant,
                                defaults={
                                    'parent_order_id': parent_order_id_for_subscription,
                                    'card_type': card_type,
                                    'masked_card_number': masked_card,
                                    'card_expiry': card_expiry,
                                    'transaction_id': transaction_id,
                                    'is_active': True,
                                    'card_save_type': 'subscription'  # Fixed amount recurring
                                }
                            )
                            logger.info(f'Saved card details for tenant {tenant.schema_name}: {card_type} {masked_card}')
                    else:
                        # Regular paid subscription (no trial)
                        parent_order_id_for_subscription = bog_order_id if card_saved_for_recurring else None

                        subscription = TenantSubscription.objects.create(
                            tenant=tenant,
                            package=pending_registration.package,  # Can be None for feature-based
                            is_active=True,
                            starts_at=timezone.now(),
                            expires_at=get_next_billing_date(),
                            agent_count=pending_registration.agent_count,
                            current_users=1,
                            whatsapp_messages_used=0,
                            storage_used_gb=0,
                            last_billed_at=timezone.now(),
                            next_billing_date=get_next_billing_date(),
                            parent_order_id=parent_order_id_for_subscription,  # Save BOG order ID for recurring charges
                            payment_status='current',  # Mark as current since payment successful
                        )

                        # Add selected features for feature-based subscriptions
                        if pending_registration.selected_features.exists():
                            subscription.selected_features.set(pending_registration.selected_features.all())
                            logger.info(f'Added {pending_registration.selected_features.count()} features to subscription for {tenant.schema_name}')

                        # Update PaymentAttempt with subscription reference
                        attempt.subscription = subscription
                        attempt.tenant = tenant
                        attempt.save()

                        # Create subscription events
                        create_subscription_event(
                            subscription=subscription,
                            event_type='created',
                            payment_order=payment_order,
                            payment_attempt=attempt,
                            description=f'Subscription created for {tenant.name}',
                            metadata={
                                'package': pending_registration.package.name if pending_registration.package else 'feature_based',
                                'agent_count': pending_registration.agent_count,
                                'is_trial': False,
                            }
                        )

                        create_subscription_event(
                            subscription=subscription,
                            event_type='payment_success',
                            payment_order=payment_order,
                            payment_attempt=attempt,
                            description=f'Initial payment successful: {payment_order.amount} GEL',
                            metadata={
                                'amount': str(payment_order.amount),
                                'transaction_id': transaction_id,
                            }
                        )

                        # Send Telegram notifications
                        try:
                            notify_subscription_created(subscription)
                            notify_payment_success(attempt, subscription)
                        except Exception as e:
                            logger.error(f'Failed to send Telegram notification: {e}')

                        if card_saved_for_recurring:
                            create_subscription_event(
                                subscription=subscription,
                                event_type='card_added',
                                payment_order=payment_order,
                                description='Card saved for recurring payments',
                                metadata={
                                    'card_type': payment_detail.get('card_type', ''),
                                    'masked_card': payment_detail.get('payer_identifier', ''),
                                }
                            )

                        # Generate invoice
                        if payment_order.amount > 0:
                            generate_invoice_for_payment(
                                payment_order=payment_order,
                                tenant=tenant,
                                agent_count=pending_registration.agent_count,
                                description=f"EchoDesk Subscription - {tenant.name}"
                            )

                        # Save card details if card was saved for recurring payments
                        if card_saved_for_recurring and parent_order_id_for_subscription:
                            card_type = payment_detail.get('card_type', '')
                            masked_card = payment_detail.get('payer_identifier', '')
                            card_expiry = payment_detail.get('card_expiry_date', '')

                            SavedCard.objects.update_or_create(
                                tenant=tenant,
                                defaults={
                                    'parent_order_id': parent_order_id_for_subscription,
                                    'card_type': card_type,
                                    'masked_card_number': masked_card,
                                    'card_expiry': card_expiry,
                                    'transaction_id': transaction_id,
                                    'is_active': True,
                                    'card_save_type': 'subscription'  # Fixed amount recurring
                                }
                            )
                            logger.info(f'Saved card details for tenant {tenant.schema_name}: {card_type} {masked_card}')

                    # Mark registration as processed
                    pending_registration.is_processed = True
                    pending_registration.save()

                    logger.info(
                        f'Tenant record created: {tenant.schema_name}. '
                        f'Triggering background processing for schema creation and migrations.'
                    )

                    # Trigger the tenant processing command in the background
                    trigger_tenant_processing(tenant.schema_name)

                    return Response({
                        'status': 'success',
                        'action': 'tenant_created',
                        'tenant_id': tenant.id,
                        'schema_name': tenant.schema_name
                    })

            # Check if this is a card-only payment (not a subscription payment)
            payment_type = payment_order.metadata.get('type')
            if payment_type == 'add_card':
                # This is a card addition payment (0 GEL)
                tenant = payment_order.tenant
                make_default = payment_order.metadata.get('make_default', False)

                # Extract card information from webhook
                card_type = payment_detail.get('card_type', '')
                masked_card_number = payment_detail.get('payer_identifier', '')
                card_expiry = payment_detail.get('card_expiry_date', '')

                logger.info(f'Processing card addition for tenant {tenant.schema_name}: {card_type} {masked_card_number}')

                # Save the card to SavedCard model
                with transaction.atomic():
                    saved_card = SavedCard.objects.create(
                        tenant=tenant,
                        parent_order_id=bog_order_id,  # BOG's order_id for future charges
                        card_type=card_type,
                        masked_card_number=masked_card_number,
                        card_expiry=card_expiry,
                        is_active=True,
                        is_default=make_default,
                        card_save_type='subscription'  # Fixed amount recurring
                    )

                    logger.info(f'Card saved for tenant {tenant.schema_name}: {saved_card.id}, default: {make_default}')

                    # Mark payment order as having saved the card
                    payment_order.card_saved = True
                    payment_order.save()

                return Response({
                    'status': 'success',
                    'action': 'card_added',
                    'card_id': saved_card.id
                })

            # Check if this is an ecommerce card addition payment
            if payment_type == 'add_ecommerce_card':
                tenant = payment_order.tenant

                # Extract card information from webhook
                card_type = payment_detail.get('card_type', '')
                masked_card_number = payment_detail.get('payer_identifier', '')
                card_expiry = payment_detail.get('card_expiry_date', '')

                logger.info(f'Processing ecommerce card addition for tenant {tenant.schema_name}: {card_type} {masked_card_number}')

                # Save the card to SavedCard model as ecommerce type
                with transaction.atomic():
                    # Check if ecommerce card already exists
                    existing_ecommerce_card = SavedCard.objects.filter(
                        tenant=tenant,
                        card_save_type='ecommerce',
                        is_active=True
                    ).first()

                    if existing_ecommerce_card:
                        # Update existing ecommerce card
                        existing_ecommerce_card.parent_order_id = bog_order_id
                        existing_ecommerce_card.card_type = card_type
                        existing_ecommerce_card.masked_card_number = masked_card_number
                        existing_ecommerce_card.card_expiry = card_expiry
                        existing_ecommerce_card.transaction_id = transaction_id
                        existing_ecommerce_card.save()
                        saved_card = existing_ecommerce_card
                        logger.info(f'Updated ecommerce card for tenant {tenant.schema_name}: {saved_card.id}')
                    else:
                        saved_card = SavedCard.objects.create(
                            tenant=tenant,
                            parent_order_id=bog_order_id,
                            card_type=card_type,
                            masked_card_number=masked_card_number,
                            card_expiry=card_expiry,
                            transaction_id=transaction_id,
                            is_active=True,
                            is_default=False,  # Ecommerce card is not default for recurring
                            card_save_type='ecommerce'  # Variable amount charges
                        )
                        logger.info(f'Ecommerce card saved for tenant {tenant.schema_name}: {saved_card.id}')

                    # Mark payment order as having saved the card
                    payment_order.card_saved = True
                    payment_order.save()

                return Response({
                    'status': 'success',
                    'action': 'ecommerce_card_added',
                    'card_id': saved_card.id
                })

            # Check if this is a feature addition payment
            if payment_type == 'feature_addition':
                logger.info(f'Processing feature addition payment for order {external_order_id}')

                tenant = payment_order.tenant
                feature_id = payment_order.metadata.get('feature_id')
                save_both_cards = payment_order.metadata.get('save_both_cards', False)

                with transaction.atomic():
                    # Add the feature to subscription
                    try:
                        from .models import Feature
                        feature = Feature.objects.get(id=feature_id)
                        subscription = TenantSubscription.objects.get(tenant=tenant)

                        # Add feature if not already added
                        if not subscription.selected_features.filter(id=feature_id).exists():
                            subscription.selected_features.add(feature)
                            logger.info(f'Feature {feature.key} added to subscription for {tenant.schema_name}')

                        # Recalculate monthly cost
                        total_feature_cost = sum(
                            f.price_per_user_gel for f in subscription.selected_features.all()
                        )
                        # Note: monthly_cost is a property, not a field, so we don't save it
                        subscription.save()

                    except Feature.DoesNotExist:
                        logger.error(f'Feature {feature_id} not found for feature addition')
                    except TenantSubscription.DoesNotExist:
                        logger.error(f'Subscription not found for tenant {tenant.schema_name}')

                    # Save BOTH card types if card was saved
                    if card_saved_for_recurring and save_both_cards:
                        card_type = payment_detail.get('card_type', '')
                        masked_card = payment_detail.get('payer_identifier', '')
                        card_expiry = payment_detail.get('card_expiry_date', '')

                        # 1. Save/Update ECOMMERCE card (for variable amount charges)
                        existing_ecommerce_card = SavedCard.objects.filter(
                            tenant=tenant,
                            card_save_type='ecommerce',
                            is_active=True
                        ).first()

                        if not existing_ecommerce_card:
                            SavedCard.objects.create(
                                tenant=tenant,
                                parent_order_id=bog_order_id,
                                card_type=card_type,
                                masked_card_number=masked_card,
                                card_expiry=card_expiry,
                                transaction_id=transaction_id,
                                is_active=True,
                                is_default=False,  # Ecommerce card is not default
                                card_save_type='ecommerce'
                            )
                            logger.info(f'Saved ecommerce card for tenant {tenant.schema_name}: {card_type} {masked_card}')
                        else:
                            # Update existing ecommerce card
                            existing_ecommerce_card.parent_order_id = bog_order_id
                            existing_ecommerce_card.card_type = card_type
                            existing_ecommerce_card.masked_card_number = masked_card
                            existing_ecommerce_card.card_expiry = card_expiry
                            existing_ecommerce_card.transaction_id = transaction_id
                            existing_ecommerce_card.save()
                            logger.info(f'Updated ecommerce card for tenant {tenant.schema_name}')

                        # 2. Save/Update SUBSCRIPTION card (for fixed recurring charges)
                        existing_subscription_card = SavedCard.objects.filter(
                            tenant=tenant,
                            card_save_type='subscription',
                            is_active=True
                        ).first()

                        if not existing_subscription_card:
                            SavedCard.objects.create(
                                tenant=tenant,
                                parent_order_id=bog_order_id,
                                card_type=card_type,
                                masked_card_number=masked_card,
                                card_expiry=card_expiry,
                                transaction_id=transaction_id,
                                is_active=True,
                                is_default=True,  # Subscription card is default for recurring
                                card_save_type='subscription'
                            )
                            logger.info(f'Saved subscription card for tenant {tenant.schema_name}: {card_type} {masked_card}')
                        else:
                            # Update existing subscription card
                            existing_subscription_card.parent_order_id = bog_order_id
                            existing_subscription_card.card_type = card_type
                            existing_subscription_card.masked_card_number = masked_card
                            existing_subscription_card.card_expiry = card_expiry
                            existing_subscription_card.transaction_id = transaction_id
                            existing_subscription_card.save()
                            logger.info(f'Updated subscription card for tenant {tenant.schema_name}')

                        # Also update subscription's parent_order_id for recurring payments
                        try:
                            subscription = TenantSubscription.objects.get(tenant=tenant)
                            subscription.parent_order_id = bog_order_id
                            subscription.save()
                            logger.info(f'Updated subscription parent_order_id for tenant {tenant.schema_name}')
                        except TenantSubscription.DoesNotExist:
                            pass

                    # Mark payment order as complete
                    payment_order.card_saved = card_saved_for_recurring
                    payment_order.save()

                    # Generate invoice
                    generate_invoice_for_payment(
                        payment_order=payment_order,
                        tenant=tenant,
                        agent_count=payment_order.agent_count,
                        description="Feature addition payment"
                    )

                return Response({
                    'status': 'success',
                    'action': 'feature_added',
                    'feature_id': feature_id
                })

            # Check if this is a recurring subscription payment
            if payment_type == 'recurring':
                logger.info(f'Processing recurring subscription payment for order {external_order_id}')

                tenant = payment_order.tenant
                subscription_id = payment_order.metadata.get('subscription_id')

                with transaction.atomic():
                    try:
                        subscription = TenantSubscription.objects.select_for_update().get(id=subscription_id)

                        # Update subscription dates for next billing cycle.
                        # Advance from previous next_billing_date to preserve the cycle
                        # rather than from now() which shifts the cycle on late payments.
                        #
                        # IMPORTANT: always advance by at least one interval on a
                        # successful charge. The prior logic only advanced if the
                        # anchor was in the past, which caused daily double-charges
                        # when the recurring cron (which runs 3 days before
                        # next_billing_date) fired more than once inside that
                        # 3-day window — next_billing_date never moved, so the
                        # same sub was selected again the next day. Reported for
                        # tenant `artlighthouse` on 2026-04-22 (charged 2026-04-21
                        # and again 2026-04-22 for a cycle whose next_billing_date
                        # was 2026-04-24).
                        billing_interval = timedelta(days=30)
                        if getattr(settings, 'TEST_BILLING_INTERVAL', False):
                            billing_interval = timedelta(minutes=2)
                        anchor = subscription.next_billing_date or timezone.now()
                        # Always advance at least one cycle on a successful charge.
                        anchor += billing_interval
                        # Keep advancing while still in the past (catching up after
                        # long outages / failed-retry runs).
                        while anchor <= timezone.now():
                            anchor += billing_interval
                        subscription.last_billed_at = timezone.now()
                        subscription.expires_at = anchor
                        subscription.next_billing_date = anchor
                        subscription.failed_payment_count = 0  # Reset failed count on successful payment
                        subscription.save()

                        # Mark payment order as complete
                        payment_order.status = 'paid'
                        payment_order.save()

                        # Generate invoice
                        generate_invoice_for_payment(
                            payment_order=payment_order,
                            tenant=tenant,
                            agent_count=subscription.agent_count,
                            description=f"EchoDesk Subscription Renewal - {tenant.name}"
                        )

                        logger.info(f'Recurring payment processed for tenant {tenant.schema_name}, next billing: {subscription.next_billing_date}')

                        return Response({
                            'status': 'success',
                            'action': 'recurring_renewed',
                            'subscription_id': subscription.id,
                            'next_billing_date': subscription.next_billing_date.isoformat()
                        })

                    except TenantSubscription.DoesNotExist:
                        logger.error(f'Subscription {subscription_id} not found for recurring payment')
                        return Response({'error': 'Subscription not found'}, status=status.HTTP_404_NOT_FOUND)

            # Check if this is a reactivation payment
            if payment_type == 'reactivation':
                logger.info(f'Processing reactivation payment for order {external_order_id}')

                tenant = payment_order.tenant
                subscription_id = payment_order.metadata.get('subscription_id')

                with transaction.atomic():
                    try:
                        subscription = TenantSubscription.objects.select_for_update().get(id=subscription_id)

                        # Reactivate the subscription
                        subscription.is_active = True
                        subscription.expires_at = get_next_billing_date()
                        subscription.last_billed_at = timezone.now()
                        subscription.next_billing_date = get_next_billing_date()
                        subscription.subscription_type = 'paid'

                        # Save the card for recurring payments if card was saved
                        if card_saved_for_recurring:
                            subscription.parent_order_id = bog_order_id
                            logger.info(f'Saved card reference for recurring payments: {bog_order_id}')

                        subscription.save()

                        # Save card details to SavedCard model
                        if card_saved_for_recurring:
                            card_type = payment_detail.get('card_type', '')
                            masked_card = payment_detail.get('payer_identifier', '')
                            card_expiry = payment_detail.get('card_expiry_date', '')

                            SavedCard.objects.update_or_create(
                                tenant=tenant,
                                defaults={
                                    'parent_order_id': bog_order_id,
                                    'card_type': card_type,
                                    'masked_card_number': masked_card,
                                    'card_expiry': card_expiry,
                                    'transaction_id': transaction_id,
                                    'is_active': True,
                                    'is_default': True,
                                    'card_save_type': 'subscription'
                                }
                            )
                            logger.info(f'Saved card for reactivation: {card_type} {masked_card}')

                        # Mark payment order as complete
                        payment_order.card_saved = card_saved_for_recurring
                        payment_order.save()

                        # Generate invoice
                        generate_invoice_for_payment(
                            payment_order=payment_order,
                            tenant=tenant,
                            agent_count=subscription.agent_count,
                            description=f"EchoDesk Subscription Reactivation - {tenant.name}"
                        )

                        logger.info(f'Subscription reactivated for tenant {tenant.schema_name}')

                        return Response({
                            'status': 'success',
                            'action': 'reactivated',
                            'subscription_id': subscription.id
                        })

                    except TenantSubscription.DoesNotExist:
                        logger.error(f'Subscription {subscription_id} not found for reactivation')
                        return Response({'error': 'Subscription not found'}, status=status.HTTP_404_NOT_FOUND)

            # Regular subscription payment for existing tenant
            tenant = payment_order.tenant
            package = payment_order.package
            agent_count = payment_order.agent_count

            # Check if this is an immediate upgrade
            if payment_order.is_immediate_upgrade:
                logger.info(f'Processing immediate upgrade for tenant {tenant.schema_name} to {package.display_name}')

                with transaction.atomic():
                    # Get existing subscription
                    try:
                        subscription = TenantSubscription.objects.select_for_update().get(
                            tenant=tenant,
                            is_active=True
                        )

                        # Deactivate old subscription (forfeiting remaining time)
                        subscription.is_active = False
                        subscription.save()

                        logger.info(f'Deactivated old subscription for {tenant.schema_name} (package: {subscription.package.display_name})')
                    except TenantSubscription.DoesNotExist:
                        logger.warning(f'No active subscription found for immediate upgrade, creating new one')

                    # Determine parent_order_id for future recurring charges
                    parent_order_id_for_subscription = bog_order_id if card_saved_for_recurring else None

                    # Create new subscription with upgraded package
                    new_subscription = TenantSubscription.objects.create(
                        tenant=tenant,
                        package=package,
                        is_active=True,
                        starts_at=timezone.now(),
                        expires_at=get_next_billing_date(),
                        agent_count=1,  # Flat CRM pricing (agent_count deprecated)
                        current_users=1,
                        whatsapp_messages_used=0,
                        storage_used_gb=0,
                        parent_order_id=parent_order_id_for_subscription,
                        last_billed_at=timezone.now(),
                        next_billing_date=get_next_billing_date(),
                        subscription_type='paid',
                        pending_package=None,  # Clear any pending upgrade
                        upgrade_scheduled_for=None
                    )

                    logger.info(f'Created new subscription for {tenant.schema_name}: {package.display_name}')

                    # Save card if card saving was enabled
                    if card_saved_for_recurring and parent_order_id_for_subscription:
                        card_type = payment_detail.get('card_type', '')
                        masked_card = payment_detail.get('payer_identifier', '')
                        card_expiry = payment_detail.get('card_expiry_date', '')

                        SavedCard.objects.update_or_create(
                            tenant=tenant,
                            defaults={
                                'parent_order_id': parent_order_id_for_subscription,
                                'card_type': card_type,
                                'masked_card_number': masked_card,
                                'card_expiry': card_expiry,
                                'transaction_id': transaction_id,
                                'is_active': True,
                                'is_default': True,
                                'card_save_type': 'subscription'
                            }
                        )
                        logger.info(f'Saved card for immediate upgrade: {card_type} {masked_card}')

                    # Generate invoice
                    package_name = package.display_name if package else "Subscription"
                    generate_invoice_for_payment(
                        payment_order=payment_order,
                        tenant=tenant,
                        agent_count=1,
                        description=f"EchoDesk Subscription Upgrade - {package_name}"
                    )

                    return Response({
                        'status': 'success',
                        'action': 'immediate_upgrade',
                        'new_package': package.display_name if package else "Subscription"
                    })

            # Check if this is a scheduled upgrade from recurring payment
            payment_metadata = payment_order.metadata or {}
            if payment_metadata.get('scheduled_upgrade'):
                logger.info(f'Processing scheduled upgrade completion for tenant {tenant.schema_name}')

                with transaction.atomic():
                    try:
                        subscription = TenantSubscription.objects.select_for_update().get(
                            tenant=tenant,
                            is_active=True
                        )

                        # Update to new package
                        previous_package = subscription.package
                        subscription.package = package
                        subscription.pending_package = None
                        subscription.upgrade_scheduled_for = None
                        subscription.subscription_type = 'paid'
                        subscription.last_billed_at = timezone.now()
                        subscription.next_billing_date = get_next_billing_date()
                        subscription.expires_at = get_next_billing_date()
                        subscription.save()

                        logger.info(f'Scheduled upgrade completed: {tenant.schema_name} upgraded from {previous_package.display_name} to {package.display_name}')

                        # Generate invoice
                        package_name = package.display_name if package else "Subscription"
                        generate_invoice_for_payment(
                            payment_order=payment_order,
                            tenant=tenant,
                            agent_count=1,
                            description=f"EchoDesk Subscription Upgrade - {package_name}"
                        )

                        return Response({
                            'status': 'success',
                            'action': 'scheduled_upgrade_completed',
                            'previous_package': previous_package.display_name,
                            'new_package': package.display_name
                        })

                    except TenantSubscription.DoesNotExist:
                        logger.error(f'No subscription found for scheduled upgrade: {tenant.schema_name}')
                        return Response({'error': 'Subscription not found'}, status=status.HTTP_404_NOT_FOUND)

            # Regular recurring payment (not an upgrade)
            try:
                subscription = TenantSubscription.objects.get(tenant=tenant)
                # Renewal: Update existing subscription
                subscription.package = package
                subscription.is_active = True
                subscription.expires_at = get_next_billing_date()
                subscription.agent_count = 1  # Flat CRM pricing (agent_count deprecated)
                subscription.last_billed_at = timezone.now()
                subscription.next_billing_date = get_next_billing_date()
                subscription.subscription_type = 'paid'
                subscription.save()
                created = False
                action = 'renewed'
            except TenantSubscription.DoesNotExist:
                # New subscription
                subscription = TenantSubscription.objects.create(
                    tenant=tenant,
                    package=package,
                    is_active=True,
                    starts_at=timezone.now(),
                    expires_at=get_next_billing_date(),
                    agent_count=1,  # Flat CRM pricing (agent_count deprecated)
                    current_users=1,
                    whatsapp_messages_used=0,
                    storage_used_gb=0,
                    last_billed_at=timezone.now(),
                    next_billing_date=get_next_billing_date(),
                    subscription_type='paid'
                )
                created = True
                action = 'created'

            # Log the event
            UsageLog.objects.create(
                subscription=subscription,
                event_type='feature_used',
                quantity=1,
                metadata={
                    'event': 'subscription_payment',
                    'order_id': external_order_id,
                    'bog_order_id': bog_order_id,
                    'transaction_id': transaction_id,
                    'amount': payment_detail.get('transfer_amount'),
                    'action': action
                }
            )

            logger.info(f'Subscription {action} for tenant {tenant.schema_name}')

            # Generate invoice for the payment
            package_name = package.display_name if package else "Subscription"
            generate_invoice_for_payment(
                payment_order=payment_order,
                tenant=tenant,
                agent_count=1,
                description=f"EchoDesk Subscription - {package_name}"
            )

            return Response({'status': 'success', 'action': action})

        except Tenant.DoesNotExist:
            logger.error('Tenant not found in webhook')
            return Response({'error': 'Tenant not found'}, status=status.HTTP_404_NOT_FOUND)

        except Package.DoesNotExist:
            logger.error('Package not found in webhook')
            return Response({'error': 'Package not found'}, status=status.HTTP_404_NOT_FOUND)

        except Exception as e:
            logger.error(f'Error processing webhook: {e}')
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Handle failed payment (BOG status: 'rejected')
    elif bog_status == 'rejected':
        logger.warning(f'Payment failed: external_order_id={external_order_id}, code={response_code}')

        # Update payment order status if exists
        try:
            payment_order = PaymentOrder.objects.get(order_id=external_order_id)
            payment_order.status = 'failed'
            payment_order.bog_order_id = bog_order_id
            payment_order.metadata['bog_status'] = bog_status
            payment_order.metadata['response_code'] = response_code
            payment_order.save()

            # Create failed PaymentAttempt record
            failed_attempt = PaymentAttempt.objects.create(
                payment_order=payment_order,
                subscription=payment_order.tenant.subscription if payment_order.tenant else None,
                tenant=payment_order.tenant if payment_order.tenant else None,
                attempt_number=1,  # Initial attempt
                status='failed',
                bog_order_id=bog_order_id,
                amount=payment_order.amount,
                attempted_at=timezone.now(),
                completed_at=timezone.now(),
                failed_reason=body.get('message', 'Payment rejected by BOG'),
                bog_error_code=response_code or '',
                bog_response=body,
            )

            # If this is for an existing subscription (recurring payment), schedule retries
            if payment_order.tenant and hasattr(payment_order.tenant, 'subscription'):
                subscription = payment_order.tenant.subscription

                # Create payment failure event
                create_subscription_event(
                    subscription=subscription,
                    event_type='payment_failed',
                    payment_order=payment_order,
                    payment_attempt=failed_attempt,
                    description=f'Payment failed: {failed_attempt.failed_reason}',
                    metadata={
                        'amount': str(payment_order.amount),
                        'error_code': response_code,
                    }
                )

                # Send Telegram notification for failed payment
                try:
                    notify_payment_failed(failed_attempt, subscription)
                except Exception as e:
                    logger.error(f'Failed to send Telegram notification for payment failure: {e}')

                # Schedule automatic retries if subscription has saved card
                if subscription.parent_order_id:
                    schedule_payment_retries(payment_order, failed_attempt)
                    subscription.mark_payment_failed()  # Update payment status to 'retrying'

                    logger.info(f'Scheduled retries for failed payment: {external_order_id}')
                else:
                    # No saved card - mark as failed
                    subscription.payment_status = 'failed'
                    subscription.mark_payment_failed()
                    logger.warning(f'Payment failed with no saved card for retry: {external_order_id}')

        except PaymentOrder.DoesNotExist:
            logger.error(f'Payment order not found for failed payment: {external_order_id}')

    # Handle other statuses
    else:
        logger.info(f'Webhook received with status: {bog_status}, code: {response_code}')

    return Response({'status': 'received'})


@extend_schema(
    operation_id='cancel_subscription',
    summary='Cancel Subscription',
    description='Cancel the current subscription',
    responses={
        200: OpenApiResponse(description='Subscription cancelled'),
        404: OpenApiResponse(description='No active subscription found')
    },
    tags=['Payments']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cancel_subscription(request):
    """
    Cancel the current subscription

    NOTE: This only deactivates the subscription, doesn't process refunds
    For refunds, contact admin
    """
    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Only staff/admin can cancel subscription
    if not request.user.is_staff:
        return Response(
            {'error': 'Only administrators can cancel subscriptions'},
            status=status.HTTP_403_FORBIDDEN
        )

    try:
        subscription = TenantSubscription.objects.get(tenant=request.tenant, is_active=True)
        subscription.is_active = False
        subscription.save()

        logger.info(f'Subscription cancelled for tenant {request.tenant.schema_name}')

        return Response({
            'status': 'cancelled',
            'message': 'Your subscription has been cancelled. You will retain access until the end of your billing period.'
        })

    except TenantSubscription.DoesNotExist:
        return Response(
            {'error': 'No active subscription found'},
            status=status.HTTP_404_NOT_FOUND
        )


@extend_schema(
    operation_id='get_saved_card_info',
    summary='Get Saved Card Information',
    description='Get information about the saved card for recurring payments',
    responses={
        200: OpenApiResponse(
            description='Saved card information',
            response={
                'type': 'object',
                'properties': {
                    'has_saved_card': {'type': 'boolean'},
                    'last_payment_date': {'type': 'string'},
                    'card_saved_date': {'type': 'string'},
                    'auto_renew_enabled': {'type': 'boolean'}
                }
            }
        ),
        404: OpenApiResponse(description='No subscription found')
    },
    tags=['Payments']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_saved_card_info(request):
    """
    Get information about saved card
    """
    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    try:
        # Find the payment order with saved card
        payment_order = PaymentOrder.objects.filter(
            tenant=request.tenant,
            card_saved=True,
            bog_order_id__isnull=False,
            status='paid'
        ).order_by('-paid_at').first()

        if payment_order:
            return Response({
                'has_saved_card': True,
                'last_payment_date': payment_order.paid_at.isoformat() if payment_order.paid_at else None,
                'card_saved_date': payment_order.paid_at.isoformat() if payment_order.paid_at else None,
                'auto_renew_enabled': True,
                'order_id': payment_order.order_id
            })
        else:
            return Response({
                'has_saved_card': False,
                'auto_renew_enabled': False
            })

    except Exception as e:
        logger.error(f'Error getting saved card info for {request.tenant.schema_name}: {e}')
        return Response(
            {'error': 'Failed to get saved card information'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    operation_id='delete_saved_card',
    summary='Delete Saved Card',
    description='Delete the saved card from BOG payment gateway',
    responses={
        200: OpenApiResponse(description='Card deleted successfully'),
        404: OpenApiResponse(description='No saved card found'),
        500: OpenApiResponse(description='Failed to delete card')
    },
    tags=['Payments']
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def delete_saved_card(request):
    """
    Delete the saved card from BOG
    """
    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    try:
        # Find the payment order with saved card
        payment_order = PaymentOrder.objects.filter(
            tenant=request.tenant,
            card_saved=True,
            bog_order_id__isnull=False,
            status='paid'
        ).order_by('-paid_at').first()

        if not payment_order:
            return Response(
                {'error': 'No saved card found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Delete card from BOG
        success = bog_service.delete_saved_card(payment_order.bog_order_id)

        if success:
            # Update payment order
            payment_order.card_saved = False
            payment_order.save()

            logger.info(f'Saved card deleted for tenant {request.tenant.schema_name}')

            return Response({
                'status': 'success',
                'message': 'Saved card has been deleted. You will need to manually pay for future renewals.'
            })
        else:
            return Response(
                {'error': 'Failed to delete card from payment gateway'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    except Exception as e:
        logger.error(f'Error deleting saved card for {request.tenant.schema_name}: {e}')
        return Response(
            {'error': 'Failed to delete saved card'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    operation_id='manual_payment',
    summary='Create Manual Payment',
    description='Create a manual payment for subscription renewal (when no saved card)',
    responses={
        200: OpenApiResponse(
            description='Payment created',
            response={
                'type': 'object',
                'properties': {
                    'payment_url': {'type': 'string'},
                    'order_id': {'type': 'string'},
                    'amount': {'type': 'number'}
                }
            }
        ),
        404: OpenApiResponse(description='No active subscription found')
    },
    tags=['Payments']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def manual_payment(request):
    """
    Create a manual payment for subscription renewal
    Used when user doesn't have saved card or wants to pay manually
    """
    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    try:
        subscription = TenantSubscription.objects.get(tenant=request.tenant, is_active=True)
        package = subscription.package

        # Calculate amount
        from .models import PricingModel
        if package.pricing_model == PricingModel.AGENT_BASED:
            amount = float(package.price_gel) * subscription.agent_count
        else:
            amount = float(package.price_gel)

        # Generate order ID
        import uuid
        external_order_id = f"MAN-{uuid.uuid4().hex[:12].upper()}"

        # Create payment
        payment_result = bog_service.create_payment(
            amount=amount,
            currency='GEL',
            external_order_id=external_order_id,
            description=f"EchoDesk Subscription Renewal - {request.tenant.name}",
            customer_email=request.tenant.admin_email,
            customer_name=request.tenant.admin_name,
            return_url_success=f"https://{request.get_host()}/settings/subscription/success",
            return_url_fail=f"https://{request.get_host()}/settings/subscription/failed",
            callback_url=f"https://api.echodesk.ge/api/payments/webhook/"
        )

        # Create payment order
        payment_order = PaymentOrder.objects.create(
            order_id=external_order_id,
            bog_order_id=payment_result.get('order_id'),
            tenant=request.tenant,
            package=package,
            amount=amount,
            currency='GEL',
            agent_count=subscription.agent_count,
            payment_url=payment_result['payment_url'],
            status='pending',
            card_saved=False,
            metadata={
                'type': 'manual_renewal',
                'subscription_id': subscription.id
            }
        )

        logger.info(f'Manual payment created for tenant {request.tenant.schema_name}: {external_order_id}')

        return Response({
            'payment_url': payment_result['payment_url'],
            'order_id': external_order_id,
            'amount': amount,
            'currency': 'GEL'
        })

    except TenantSubscription.DoesNotExist:
        return Response(
            {'error': 'No active subscription found'},
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        logger.error(f'Error creating manual payment for {request.tenant.schema_name}: {e}')
        return Response(
            {'error': f'Failed to create payment: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    operation_id='reactivate_subscription_payment',
    summary='Reactivate Subscription with Payment',
    description='Create a payment to reactivate an inactive subscription and save card for recurring payments',
    responses={
        200: OpenApiResponse(
            description='Payment created',
            response={
                'type': 'object',
                'properties': {
                    'payment_url': {'type': 'string'},
                    'order_id': {'type': 'string'},
                    'amount': {'type': 'number'},
                    'currency': {'type': 'string'}
                }
            }
        ),
        404: OpenApiResponse(description='No subscription found')
    },
    tags=['Payments']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reactivate_subscription_payment(request):
    """
    Create a payment to reactivate an inactive subscription

    This endpoint:
    - Works with inactive subscriptions
    - Charges the full subscription amount
    - Saves the card for future recurring payments
    - Activates the subscription when payment succeeds
    """
    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    try:
        # Get subscription (even if inactive)
        subscription = TenantSubscription.objects.get(tenant=request.tenant)

        # Calculate amount based on feature-based pricing
        if subscription.selected_features.exists():
            total_per_user = sum(f.price_per_user_gel for f in subscription.selected_features.all())
            amount = float(total_per_user * subscription.agent_count)
        else:
            # No features selected - use stored monthly_cost or return error
            if subscription.monthly_cost and subscription.monthly_cost > 0:
                amount = float(subscription.monthly_cost)
            else:
                return Response(
                    {'error': 'No features selected for subscription'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # Check if BOG is configured
        if not bog_service.is_configured():
            return Response(
                {
                    'error': 'Payment gateway not configured',
                    'message': 'Please contact support to set up payment processing'
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        # Generate URLs
        api_host = request.get_host()
        frontend_host = api_host.replace('api.', '')
        frontend_url = f"https://{frontend_host}"
        return_url_success = f"{frontend_url}/settings/subscription?payment=success"
        return_url_fail = f"{frontend_url}/settings/subscription?payment=failed"

        # Ensure callback_url starts with https://
        api_domain = settings.API_DOMAIN
        if not api_domain.startswith('http'):
            api_domain = f"https://{api_domain}"
        callback_url = f"{api_domain}/api/payments/webhook/"

        # Generate external order ID
        import uuid
        external_order_id = f"REACT-{uuid.uuid4().hex[:12].upper()}"

        # Create payment with card saving enabled
        payment_result = bog_service.create_payment(
            amount=amount,
            currency='GEL',
            external_order_id=external_order_id,
            description=f"EchoDesk Subscription Reactivation - {request.tenant.name}",
            customer_email=request.tenant.admin_email,
            customer_name=request.tenant.admin_name or request.tenant.name,
            return_url_success=return_url_success,
            return_url_fail=return_url_fail,
            callback_url=callback_url
        )

        bog_order_id = payment_result.get('order_id')

        # Enable subscription card saving on this order
        try:
            bog_service.enable_subscription_card_saving(bog_order_id)
            logger.info(f'Enabled subscription card saving for order {bog_order_id}')
        except Exception as e:
            logger.error(f'Failed to enable card saving for order {bog_order_id}: {e}')

        # Create payment order
        payment_order = PaymentOrder.objects.create(
            order_id=external_order_id,
            bog_order_id=bog_order_id,
            tenant=request.tenant,
            amount=amount,
            currency='GEL',
            agent_count=subscription.agent_count,
            payment_url=payment_result['payment_url'],
            status='pending',
            card_saved=False,  # Will be updated by webhook
            metadata={
                'type': 'reactivation',
                'subscription_id': subscription.id,
                'save_card': True
            }
        )

        logger.info(f'Reactivation payment created for tenant {request.tenant.schema_name}: {external_order_id}, amount: {amount}₾')

        return Response({
            'payment_url': payment_result['payment_url'],
            'order_id': external_order_id,
            'amount': amount,
            'currency': 'GEL'
        })

    except TenantSubscription.DoesNotExist:
        return Response(
            {'error': 'No subscription found'},
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        logger.error(f'Error creating reactivation payment for {request.tenant.schema_name}: {e}')
        return Response(
            {'error': f'Failed to create payment: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    operation_id='get_saved_card',
    summary='Get Saved Payment Card',
    description='Retrieve the saved payment card for the current tenant',
    responses={
        200: OpenApiResponse(description='Saved card details'),
        404: OpenApiResponse(description='No saved card found')
    },
    tags=['Payments']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_saved_card(request):
    """
    Get all saved payment cards for the current tenant

    Only returns masked card details (last 4 digits, expiry, card type)
    Actual card details are stored at Bank of Georgia
    """
    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Only staff/admin can view saved cards
    if not request.user.is_staff:
        return Response(
            {'error': 'Only administrators can view saved card details'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Get all active saved cards for this tenant, ordered by default first
    saved_cards = SavedCard.objects.filter(tenant=request.tenant, is_active=True)
    from .serializers import SavedCardSerializer
    serializer = SavedCardSerializer(saved_cards, many=True)
    return Response(serializer.data)


@extend_schema(
    operation_id='remove_saved_card',
    summary='Remove Saved Payment Card',
    description='Remove a specific saved payment card from the tenant account by card ID.',
    request=inline_serializer(
        'RemoveCardRequest',
        fields={
            'card_id': serializers.IntegerField(help_text='ID of the card to remove')
        }
    ),
    responses={
        200: OpenApiResponse(description='Card removed successfully'),
        404: OpenApiResponse(description='Card not found')
    },
    tags=['Payments']
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def remove_saved_card(request):
    """
    Remove a specific saved payment card

    This deactivates the saved card. If it's the default card and there are other cards,
    another card will be set as default automatically.
    """
    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Only staff/admin can remove saved card
    if not request.user.is_staff:
        return Response(
            {'error': 'Only administrators can remove saved card'},
            status=status.HTTP_403_FORBIDDEN
        )

    card_id = request.data.get('card_id')
    if not card_id:
        return Response(
            {'error': 'card_id is required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        with transaction.atomic():
            # Get and deactivate the specified card
            saved_card = SavedCard.objects.get(id=card_id, tenant=request.tenant, is_active=True)
            was_default = saved_card.is_default
            saved_card.is_active = False
            saved_card.is_default = False
            saved_card.save()

            # If this was the default card, set another active card as default
            if was_default:
                other_card = SavedCard.objects.filter(
                    tenant=request.tenant,
                    is_active=True
                ).first()
                if other_card:
                    other_card.is_default = True
                    other_card.save()
                else:
                    # No more cards - remove parent_order_id from subscription
                    try:
                        subscription = TenantSubscription.objects.get(tenant=request.tenant)
                        subscription.parent_order_id = None
                        subscription.save()
                    except TenantSubscription.DoesNotExist:
                        pass

            logger.info(f'Removed saved card {card_id} for tenant {request.tenant.schema_name}')

            return Response({
                'status': 'success',
                'message': 'Saved card removed successfully'
            })
    except SavedCard.DoesNotExist:
        return Response(
            {'error': 'Card not found'},
            status=status.HTTP_404_NOT_FOUND
        )


@extend_schema(
    operation_id='set_default_card',
    summary='Set Default Payment Card',
    description='Set a specific saved card as the default payment method for automatic renewals.',
    request=inline_serializer(
        'SetDefaultCardRequest',
        fields={
            'card_id': serializers.IntegerField(help_text='ID of the card to set as default')
        }
    ),
    responses={
        200: OpenApiResponse(description='Default card updated successfully'),
        404: OpenApiResponse(description='Card not found')
    },
    tags=['Payments']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def set_default_card(request):
    """
    Set a specific card as the default payment method

    This card will be used for automatic subscription renewals.
    Any previously default card will be unmarked.
    """
    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Only staff/admin can set default card
    if not request.user.is_staff:
        return Response(
            {'error': 'Only administrators can set default card'},
            status=status.HTTP_403_FORBIDDEN
        )

    card_id = request.data.get('card_id')
    if not card_id:
        return Response(
            {'error': 'card_id is required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        with transaction.atomic():
            # Get the card to set as default
            card = SavedCard.objects.get(id=card_id, tenant=request.tenant, is_active=True)

            # Unset all other default cards
            SavedCard.objects.filter(
                tenant=request.tenant,
                is_default=True
            ).exclude(id=card_id).update(is_default=False)

            # Set this card as default
            card.is_default = True
            card.save()

            # Update subscription parent_order_id to use this card
            try:
                subscription = TenantSubscription.objects.get(tenant=request.tenant)
                subscription.parent_order_id = card.parent_order_id
                subscription.save()
            except TenantSubscription.DoesNotExist:
                pass

            logger.info(f'Set card {card_id} as default for tenant {request.tenant.schema_name}')

            from .serializers import SavedCardSerializer
            serializer = SavedCardSerializer(card)
            return Response({
                'status': 'success',
                'message': 'Default card updated successfully',
                'card': serializer.data
            })
    except SavedCard.DoesNotExist:
        return Response(
            {'error': 'Card not found'},
            status=status.HTTP_404_NOT_FOUND
        )


@extend_schema(
    operation_id='add_new_card',
    summary='Add New Payment Card',
    description='Create a 0 GEL payment to add and save a new payment card without charging',
    request=inline_serializer(
        name='AddNewCardRequest',
        fields={
            'make_default': serializers.BooleanField(
                default=False,
                help_text='Set this card as default after adding'
            )
        }
    ),
    responses={
        200: OpenApiResponse(
            description='Payment URL created to add card',
            response=inline_serializer(
                name='AddNewCardResponse',
                fields={
                    'payment_id': serializers.CharField(),
                    'payment_url': serializers.CharField(),
                    'amount': serializers.FloatField(),
                    'currency': serializers.CharField()
                }
            )
        ),
        403: OpenApiResponse(description='Not accessible from this domain'),
        503: OpenApiResponse(description='Payment gateway not configured')
    },
    tags=['Payments']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def add_new_card(request):
    """
    Create a 0 GEL payment to add and save a new payment card

    This endpoint creates a payment session with 0 amount, allowing the user
    to go through the BOG payment flow and add their card details. The card
    will be saved for future automatic payments without any charge.
    """
    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    make_default = request.data.get('make_default', False)

    # Check if BOG is configured
    if not bog_service.is_configured():
        return Response(
            {
                'error': 'Payment gateway not configured',
                'message': 'Please contact support to set up payment processing'
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    # Generate URLs
    # Get frontend URL by removing 'api.' from the request host
    api_host = request.get_host()
    frontend_host = api_host.replace('api.', '')
    frontend_url = f"https://{frontend_host}"
    return_url = f"{frontend_url}/settings/subscription?card_added=success"

    # Ensure callback_url starts with https://
    api_domain = settings.API_DOMAIN
    if not api_domain.startswith('http'):
        api_domain = f"https://{api_domain}"
    callback_url = f"{api_domain}/api/payments/webhook/"

    try:
        # Generate external order ID for tracking
        import uuid
        external_order_id = f"CARD-{uuid.uuid4().hex[:12].upper()}"

        # Create 0 GEL payment session
        payment_result = bog_service.create_payment(
            amount=0.0,  # 0 GEL charge
            currency='GEL',
            description='Add new payment card',
            customer_email=request.user.email if hasattr(request.user, 'email') else '',
            customer_name=f"{request.user.first_name} {request.user.last_name}" if hasattr(request.user, 'first_name') else '',
            return_url_success=return_url,
            return_url_fail=f"{frontend_url}/settings/subscription?card_added=failed",
            callback_url=callback_url,
            external_order_id=external_order_id,
            metadata={
                'type': 'add_card',
                'tenant_id': request.tenant.id,
                'make_default': make_default
            }
        )

        # Store payment order in database
        payment_order = PaymentOrder.objects.create(
            order_id=external_order_id,  # Store our external_order_id for webhook lookup
            bog_order_id=payment_result['order_id'],  # Store BOG's internal order_id
            tenant=request.tenant,
            package=None,  # No package for card addition
            amount=0.0,
            currency='GEL',
            agent_count=0,
            payment_url=payment_result['payment_url'],
            status='pending',
            metadata={
                **payment_result.get('metadata', {}),
                'type': 'add_card',
                'make_default': make_default
            }
        )

        logger.info(f'Card addition payment order created: {payment_order.order_id} for tenant {request.tenant.schema_name}')

        return Response({
            'payment_id': payment_result.get('payment_id'),
            'payment_url': payment_result.get('payment_url'),
            'amount': 0.0,
            'currency': 'GEL',
            'status': 'pending',
            'message': 'Redirect user to payment_url to add card'
        })

    except Exception as e:
        logger.error(f'Failed to create card addition payment for tenant {request.tenant.schema_name}: {e}')
        return Response(
            {
                'error': 'Card addition failed',
                'message': str(e)
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    operation_id='add_ecommerce_card',
    summary='Add Ecommerce Payment Card',
    description='Create a 0 GEL payment to add and save a new ecommerce payment card for variable amount charges (like feature additions)',
    request=inline_serializer(
        name='AddEcommerceCardRequest',
        fields={}
    ),
    responses={
        200: OpenApiResponse(
            description='Payment URL created to add ecommerce card',
            response=inline_serializer(
                name='AddEcommerceCardResponse',
                fields={
                    'payment_id': serializers.CharField(),
                    'payment_url': serializers.CharField(),
                    'amount': serializers.FloatField(),
                    'currency': serializers.CharField()
                }
            )
        ),
        403: OpenApiResponse(description='Not accessible from this domain'),
        503: OpenApiResponse(description='Payment gateway not configured')
    },
    tags=['Payments']
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def add_ecommerce_card(request):
    """
    Create a 0 GEL payment to add and save an ecommerce payment card

    This endpoint creates a payment session with 0 amount, allowing the user
    to go through the BOG payment flow and add their card details. The card
    will be saved as an 'ecommerce' type card for future variable amount charges
    (like feature additions).

    Different from add_new_card which saves a 'subscription' type card for fixed recurring amounts.
    """
    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'This endpoint is only available from tenant subdomains'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Check if BOG is configured
    if not bog_service.is_configured():
        return Response(
            {
                'error': 'Payment gateway not configured',
                'message': 'Please contact support to set up payment processing'
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    # Generate URLs
    api_host = request.get_host()
    frontend_host = api_host.replace('api.', '')
    frontend_url = f"https://{frontend_host}"
    return_url = f"{frontend_url}/settings/subscription?ecommerce_card_added=success"

    # Ensure callback_url starts with https://
    api_domain = settings.API_DOMAIN
    if not api_domain.startswith('http'):
        api_domain = f"https://{api_domain}"
    callback_url = f"{api_domain}/api/payments/webhook/"

    try:
        # Generate external order ID for tracking
        import uuid
        external_order_id = f"ECARD-{uuid.uuid4().hex[:12].upper()}"

        # Create 0 GEL payment session
        payment_result = bog_service.create_payment(
            amount=0.0,  # 0 GEL charge
            currency='GEL',
            description='Add ecommerce payment card',
            customer_email=request.user.email if hasattr(request.user, 'email') else '',
            customer_name=f"{request.user.first_name} {request.user.last_name}" if hasattr(request.user, 'first_name') else '',
            return_url_success=return_url,
            return_url_fail=f"{frontend_url}/settings/subscription?ecommerce_card_added=failed",
            callback_url=callback_url,
            external_order_id=external_order_id,
            metadata={
                'type': 'add_ecommerce_card',
                'tenant_id': request.tenant.id
            }
        )

        # Enable ecommerce card saving (variable amounts)
        bog_order_id = payment_result['order_id']
        bog_service.enable_card_saving(bog_order_id)  # Uses /cards endpoint for ecommerce

        # Store payment order in database
        payment_order = PaymentOrder.objects.create(
            order_id=external_order_id,
            bog_order_id=bog_order_id,
            tenant=request.tenant,
            package=None,
            amount=0.0,
            currency='GEL',
            agent_count=0,
            payment_url=payment_result['payment_url'],
            status='pending',
            metadata={
                **payment_result.get('metadata', {}),
                'type': 'add_ecommerce_card'
            }
        )

        logger.info(f'Ecommerce card addition payment order created: {payment_order.order_id} for tenant {request.tenant.schema_name}')

        return Response({
            'payment_id': payment_result.get('payment_id'),
            'payment_url': payment_result.get('payment_url'),
            'amount': 0.0,
            'currency': 'GEL',
            'status': 'pending',
            'message': 'Redirect user to payment_url to add ecommerce card'
        })

    except Exception as e:
        logger.error(f'Failed to create ecommerce card addition payment for tenant {request.tenant.schema_name}: {e}')
        return Response(
            {
                'error': 'Ecommerce card addition failed',
                'message': str(e)
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    operation_id='list_invoices',
    summary='List Invoices',
    description='Get list of invoices for the current tenant',
    responses={
        200: inline_serializer(
            name='InvoiceListResponse',
            fields={
                'invoices': serializers.ListField(child=serializers.DictField())
            }
        ),
    },
    tags=['Payments']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_invoices(request):
    """
    Get list of invoices for the current tenant
    """
    try:
        # Get invoices for the current tenant
        invoices = Invoice.objects.filter(tenant=request.tenant).select_related('payment_order')

        invoices_data = []
        for invoice in invoices:
            invoices_data.append({
                'id': invoice.id,
                'invoice_number': invoice.invoice_number,
                'amount': float(invoice.amount),
                'currency': invoice.currency,
                'description': invoice.description,
                'package_name': None,  # Package system removed
                'agent_count': invoice.agent_count,
                'invoice_date': invoice.invoice_date.isoformat(),
                'paid_date': invoice.paid_date.isoformat() if invoice.paid_date else None,
                'due_date': invoice.due_date.isoformat() if invoice.due_date else None,
                'pdf_url': invoice.pdf_url,
                'pdf_generated': invoice.pdf_generated
            })

        return Response({'invoices': invoices_data})

    except Exception as e:
        logger.error(f'Error fetching invoices for tenant {request.tenant.schema_name}: {str(e)}')
        return Response(
            {'error': 'Failed to fetch invoices'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
