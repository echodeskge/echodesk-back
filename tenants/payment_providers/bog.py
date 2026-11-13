"""
BOG (Bank of Georgia) payment provider adapter.

Wraps the existing BOGPaymentService from tenants/bog_payment.py
into the abstract PaymentProvider interface without rewriting BOG logic.
"""
import logging
from decimal import Decimal
from typing import Optional, Dict, Any, List

from .base import PaymentProvider, PaymentResult, PaymentStatus, ChargeResult

logger = logging.getLogger(__name__)


class BOGPaymentProvider(PaymentProvider):
    """
    Bank of Georgia payment provider.

    - Requires redirect to hosted payment page
    - Does NOT manage recurring billing (EchoDesk charges via cron)
    - Card saving for recurring charges via BOG /subscriptions endpoint
    """

    def __init__(self):
        from tenants.bog_payment import bog_service
        self._service = bog_service

    @property
    def name(self) -> str:
        return 'bog'

    @property
    def manages_recurring_billing(self) -> bool:
        return False

    @property
    def requires_redirect(self) -> bool:
        return True

    def create_payment(
        self,
        amount: Decimal,
        currency: str,
        external_order_id: str,
        description: str = '',
        customer_email: str = '',
        customer_name: str = '',
        return_url_success: str = '',
        return_url_fail: str = '',
        callback_url: str = '',
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentResult:
        result = self._service.create_payment(
            amount=float(amount),
            currency=currency,
            description=description,
            customer_email=customer_email,
            customer_name=customer_name,
            return_url_success=return_url_success,
            return_url_fail=return_url_fail,
            callback_url=callback_url,
            external_order_id=external_order_id,
            metadata=metadata,
        )

        return PaymentResult(
            provider='bog',
            provider_order_id=result['order_id'],
            external_order_id=external_order_id,
            amount=Decimal(str(result['amount'])),
            currency=currency,
            status='pending',
            payment_url=result['payment_url'],
            requires_redirect=True,
            metadata=metadata or {},
        )

    def create_subscription_payment(
        self,
        amount: Decimal,
        currency: str,
        external_order_id: str,
        customer_email: str = '',
        customer_name: str = '',
        company_name: str = '',
        return_url_success: str = '',
        return_url_fail: str = '',
        callback_url: str = '',
        feature_items: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentResult:
        result = self._service.create_subscription_payment_with_card_save(
            package=None,
            agent_count=1,
            customer_email=customer_email,
            customer_name=customer_name,
            company_name=company_name,
            return_url_success=return_url_success,
            return_url_fail=return_url_fail,
            callback_url=callback_url,
            external_order_id=external_order_id,
            subscription_amount=float(amount),
        )

        return PaymentResult(
            provider='bog',
            provider_order_id=result['order_id'],
            external_order_id=external_order_id,
            amount=Decimal(str(result['subscription_amount'])),
            currency=currency,
            status='pending',
            payment_url=result['payment_url'],
            requires_redirect=True,
            card_saving_enabled=result.get('card_saving_enabled', False),
            metadata=metadata or {},
        )

    def check_payment_status(self, provider_order_id: str) -> PaymentStatus:
        result = self._service.check_payment_status(provider_order_id)
        return PaymentStatus(
            provider='bog',
            provider_order_id=provider_order_id,
            status=result.get('status', 'unknown'),
            amount=Decimal(str(result['amount'])) if result.get('amount') else None,
            currency=result.get('currency'),
            raw_data=result.get('raw_data'),
        )

    def verify_webhook(self, headers: Dict[str, str], body: bytes) -> bool:
        signature = headers.get('Callback-Signature') or headers.get('callback-signature') or ''
        return self._service.verify_callback_signature(body, signature) is not False

    def charge_recurring(
        self,
        parent_order_id: str,
        amount: Optional[Decimal] = None,
        callback_url: str = '',
        external_order_id: str = '',
    ) -> ChargeResult:
        """Charge a saved card via BOG subscription endpoint."""
        result = self._service.charge_subscription(
            parent_order_id=parent_order_id,
            callback_url=callback_url,
            external_order_id=external_order_id,
        )

        return ChargeResult(
            provider='bog',
            provider_order_id=result['order_id'],
            status='processing',
            requires_authentication=result.get('requires_authentication', False),
            payment_url=result.get('payment_url'),
        )
