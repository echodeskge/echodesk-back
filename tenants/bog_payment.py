"""
Bank of Georgia (BOG) Payment Gateway Integration for EchoDesk

Official BOG Payment API integration for processing subscription payments.
Documentation: https://api.bog.ge/docs/en/payments/introduction
"""

import requests
import hashlib
import base64
import logging
from django.conf import settings
from typing import Dict, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class BOGPaymentService:
    """
    Service for integrating with Bank of Georgia payment gateway

    Configuration in settings.py:
    BOG_CLIENT_ID = 'your-client-id'
    BOG_CLIENT_SECRET = 'your-client-secret'
    BOG_AUTH_URL = 'https://oauth2.bog.ge/auth/realms/bog/protocol/openid-connect/token'
    BOG_API_BASE_URL = 'https://api.bog.ge/payments/v1'
    """

    def __init__(self):
        self.client_id = getattr(settings, 'BOG_CLIENT_ID', '')
        self.client_secret = getattr(settings, 'BOG_CLIENT_SECRET', '')
        self.auth_url = getattr(settings, 'BOG_AUTH_URL',
                               'https://oauth2.bog.ge/auth/realms/bog/protocol/openid-connect/token')
        self.base_url = getattr(settings, 'BOG_API_BASE_URL',
                               'https://api.bog.ge/payments/v1')

        self._access_token = None
        self._token_expires_at = None

        if not all([self.client_id, self.client_secret]):
            logger.warning('BOG payment gateway not fully configured. Payment processing will be disabled.')

    def is_configured(self) -> bool:
        """Check if BOG is properly configured"""
        return bool(self.client_id and self.client_secret)

    def _get_access_token(self) -> str:
        """
        Get OAuth 2.0 access token using client credentials grant
        Caches token until it expires

        Returns:
            Access token string
        """
        # Return cached token if still valid
        if self._access_token and self._token_expires_at:
            if datetime.now() < self._token_expires_at:
                return self._access_token

        try:
            # Create Basic Auth header
            credentials = f"{self.client_id}:{self.client_secret}"
            b64_credentials = base64.b64encode(credentials.encode()).decode()

            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': f'Basic {b64_credentials}'
            }

            data = {
                'grant_type': 'client_credentials'
            }

            response = requests.post(
                self.auth_url,
                headers=headers,
                data=data,
                timeout=30
            )

            if response.status_code == 200:
                token_data = response.json()
                self._access_token = token_data['access_token']

                # Cache token with 5 minute buffer before expiration
                expires_in = token_data.get('expires_in', 3600)
                self._token_expires_at = datetime.now() + timedelta(seconds=expires_in - 300)

                logger.info('Successfully obtained BOG access token')
                return self._access_token
            else:
                logger.error(f'Failed to get BOG access token: {response.status_code} - {response.text}')
                raise ValueError(f'Authentication failed: {response.status_code}')

        except requests.RequestException as e:
            logger.error(f'Error getting BOG access token: {e}')
            raise ValueError(f'Authentication error: {str(e)}')

    def create_payment(
        self,
        amount: float,
        currency: str = 'GEL',
        description: str = '',
        customer_email: str = '',
        customer_name: str = '',
        customer_phone: str = '',
        return_url_success: str = '',
        return_url_fail: str = '',
        callback_url: str = '',
        external_order_id: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> Dict:
        """
        Create a payment order with BOG

        Args:
            amount: Payment amount
            currency: Currency code (GEL, USD, EUR, GBP)
            description: Payment description
            customer_email: Customer email (masked automatically by BOG)
            customer_name: Customer full name
            customer_phone: Customer phone (masked automatically by BOG)
            return_url_success: URL to redirect after successful payment
            return_url_fail: URL to redirect after failed payment
            callback_url: Webhook URL for payment status updates
            external_order_id: Optional custom order ID
            metadata: Additional metadata (stored separately, not sent to BOG)

        Returns:
            Dict containing order_id, payment_url, and other details
        """
        if not self.is_configured():
            raise ValueError('BOG payment gateway is not configured')

        # Get access token
        access_token = self._get_access_token()

        # Format amount to 2 decimal places
        amount = round(float(amount), 2)

        # Build request payload
        payload = {
            'callback_url': callback_url,
            'purchase_units': {
                'currency': currency,
                'total_amount': amount,
                'basket': [
                    {
                        'product_id': external_order_id or 'subscription',
                        'description': description[:255] if description else 'EchoDesk Subscription',
                        'quantity': 1,
                        'unit_price': amount
                    }
                ]
            },
            'redirect_urls': {
                'success': return_url_success,
                'fail': return_url_fail
            },
            'payment_method': ['card']  # Enable card payment method
        }

        # Add optional fields
        if external_order_id:
            payload['external_order_id'] = external_order_id

        if customer_name or customer_email or customer_phone:
            payload['buyer'] = {}
            if customer_name:
                payload['buyer']['full_name'] = customer_name
            if customer_email:
                payload['buyer']['masked_email'] = customer_email
            if customer_phone:
                payload['buyer']['masked_phone'] = customer_phone

        # Make API request
        try:
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {access_token}',
                'Accept-Language': 'en'
            }

            response = requests.post(
                f'{self.base_url}/ecommerce/orders',
                json=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code in [200, 201]:  # Accept both 200 OK and 201 Created
                data = response.json()
                order_id = data['id']
                payment_url = data['_links']['redirect']['href']

                logger.info(f'BOG payment created: order_id={order_id}, amount={amount}{currency}')

                return {
                    'order_id': order_id,
                    'payment_id': order_id,
                    'payment_url': payment_url,
                    'amount': amount,
                    'currency': currency,
                    'status': 'pending',
                    'metadata': metadata,
                    'details_url': data['_links']['details']['href']
                }
            else:
                error_msg = f'BOG payment creation failed: {response.status_code} - {response.text}'
                logger.error(error_msg)
                raise ValueError(error_msg)

        except requests.RequestException as e:
            logger.error(f'Error creating BOG payment: {e}')
            raise ValueError(f'Payment creation error: {str(e)}')

    def check_payment_status(self, order_id: str) -> Dict:
        """
        Check the status of a payment

        Args:
            order_id: BOG order ID

        Returns:
            Dict containing payment status and details
        """
        if not self.is_configured():
            raise ValueError('BOG payment gateway is not configured')

        try:
            access_token = self._get_access_token()

            headers = {
                'Authorization': f'Bearer {access_token}',
                'Accept-Language': 'en'
            }

            response = requests.get(
                f'{self.base_url}/receipt/{order_id}',
                headers=headers,
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()

                # Extract key status information
                order_status = data.get('order_status', {})
                status_key = order_status.get('key', 'unknown')

                # Map BOG status to our internal status
                status_mapping = {
                    'created': 'pending',
                    'processing': 'processing',
                    'completed': 'paid',
                    'rejected': 'failed',
                    'refund_requested': 'refund_requested',
                    'refunded': 'refunded',
                    'refunded_partially': 'refunded_partially',
                    'blocked': 'blocked'
                }

                internal_status = status_mapping.get(status_key, 'unknown')

                return {
                    'order_id': order_id,
                    'status': internal_status,
                    'bog_status': status_key,
                    'transaction_id': data.get('transaction_id'),
                    'amount': data.get('transfer_amount'),
                    'currency': data.get('currency'),
                    'transfer_method': data.get('transfer_method'),
                    'response_code': data.get('code'),
                    'raw_data': data
                }
            else:
                logger.warning(f'BOG status check failed: {response.status_code} - {response.text}')
                return {
                    'order_id': order_id,
                    'status': 'unknown',
                    'error': f'Status check failed: {response.status_code}'
                }

        except requests.RequestException as e:
            logger.error(f'Failed to check payment status: {e}')
            return {
                'order_id': order_id,
                'status': 'error',
                'error': str(e)
            }

    def refund_payment(self, order_id: str, amount=None) -> Dict:
        """
        Refund a completed payment.

        Args:
            order_id: BOG order ID
            amount: amount to refund for a partial refund; None refunds in full

        Returns:
            Dict with BOG's response (key, message, action_id)

        Raises:
            ValueError: BOG rejected the refund or could not be reached
        """
        if not self.is_configured():
            raise ValueError('BOG payment gateway is not configured')

        payload = {}
        if amount is not None:
            payload['amount'] = f'{float(amount):.2f}'

        try:
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self._get_access_token()}',
            }
            response = requests.post(
                f'{self.base_url}/payment/refund/{order_id}',
                json=payload,
                headers=headers,
                timeout=30
            )
        except requests.RequestException as e:
            logger.error(f'Error refunding BOG payment {order_id}: {e}')
            raise ValueError(f'Refund error: {str(e)}')

        if response.status_code in [200, 201, 202]:
            try:
                data = response.json()
            except ValueError:
                data = {}
            logger.info(f'BOG refund requested: order_id={order_id}, amount={amount or "full"}')
            return {
                'order_id': order_id,
                'key': data.get('key'),
                'message': data.get('message'),
                'action_id': data.get('action_id'),
            }

        error_msg = f'BOG refund failed: {response.status_code} - {response.text}'
        logger.error(error_msg)
        raise ValueError(error_msg)

    def verify_webhook_signature(self, payload: str, signature: str, public_key: str = None) -> bool:
        """
        Verify webhook signature from BOG callback

        Note: BOG uses SHA256withRSA signature verification with a public key.
        This is optional but recommended for production.

        Args:
            payload: Raw request body as string
            signature: Signature from Callback-Signature header
            public_key: Public key provided by BOG

        Returns:
            True if signature is valid or if signature verification is not configured
        """
        if not signature or not public_key:
            logger.warning('Webhook signature verification skipped (no signature or public key)')
            return True

        try:
            # TODO: Implement RSA signature verification when public key is available
            # This requires cryptography library and proper RSA verification
            logger.warning('Webhook signature verification not implemented yet')
            return True

        except Exception as e:
            logger.error(f'Webhook signature verification error: {e}')
            return False

    def enable_card_saving(self, order_id: str) -> bool:
        """
        Enable card saving for recurring payments on an order
        Must be called AFTER creating the order but BEFORE redirecting user to payment page

        Documentation: https://api.bog.ge/docs/payments/saved-card/recurrent

        Args:
            order_id: BOG order ID (their internal ID, not our external_order_id)

        Returns:
            True if successful, False otherwise
        """
        if not self.is_configured():
            raise ValueError('BOG payment gateway is not configured')

        try:
            access_token = self._get_access_token()

            headers = {
                'Authorization': f'Bearer {access_token}'
            }

            # Use the correct endpoint: PUT /orders/{order_id}/cards
            response = requests.put(
                f'{self.base_url}/orders/{order_id}/cards',
                headers=headers,
                timeout=30
            )

            if response.status_code == 202:  # 202 Accepted
                logger.info(f'Card saving enabled for order: {order_id}')
                return True
            else:
                logger.error(f'Failed to enable card saving: {response.status_code} - {response.text}')
                return False

        except requests.RequestException as e:
            logger.error(f'Error enabling card saving: {e}')
            return False

    def charge_saved_card(
        self,
        parent_order_id: str,
        amount: float,
        currency: str = 'GEL',
        callback_url: str = '',
        external_order_id: str = None
    ) -> Dict:
        """
        Charge a saved card using recurrent payment

        Documentation: https://api.bog.ge/docs/en/payments/saved-card/recurrent-payment

        Note: Uses the same endpoint as create_payment but with parent_order_id in the path.
        BOG automatically charges the saved card associated with the parent order.

        Args:
            parent_order_id: The original order ID where card was saved
            amount: Amount to charge
            currency: Currency code
            callback_url: Webhook URL for payment updates
            external_order_id: Optional custom order ID

        Returns:
            Dict with order_id and details_url
        """
        if not self.is_configured():
            raise ValueError('BOG payment gateway is not configured')

        try:
            access_token = self._get_access_token()

            # Format amount to 2 decimal places
            amount = round(float(amount), 2)

            # Build payload according to BOG API - same structure as create_payment
            payload = {
                'purchase_units': {
                    'currency': currency,
                    'total_amount': amount,
                    'basket': [
                        {
                            'product_id': external_order_id or 'recurring_payment',
                            'quantity': 1,
                            'unit_price': amount
                        }
                    ]
                }
            }

            if callback_url:
                payload['callback_url'] = callback_url
            if external_order_id:
                payload['external_order_id'] = external_order_id

            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {access_token}',
                'Accept-Language': 'en'
            }

            # POST to /ecommerce/orders/{parent_order_id} to charge saved card
            response = requests.post(
                f'{self.base_url}/ecommerce/orders/{parent_order_id}',
                json=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code in [200, 201]:
                data = response.json()
                order_id = data['id']
                details_url = data['_links']['details']['href']

                # Check if BOG requires user authentication (3D Secure, etc.)
                payment_url = data.get('_links', {}).get('redirect', {}).get('href')

                logger.info(f'Saved card charged: new_order_id={order_id}, parent={parent_order_id}, amount={amount}{currency}, payment_url={payment_url}')

                result = {
                    'order_id': order_id,
                    'parent_order_id': parent_order_id,
                    'details_url': details_url,
                    'amount': amount,
                    'currency': currency,
                    'status': 'processing'
                }

                # If payment URL is provided, user needs to authenticate
                if payment_url:
                    result['payment_url'] = payment_url
                    result['requires_authentication'] = True
                else:
                    result['requires_authentication'] = False

                return result
            else:
                error_msg = f'Saved card charge failed: {response.status_code} - {response.text}'
                logger.warning(error_msg)
                raise ValueError(error_msg)

        except requests.RequestException as e:
            logger.error(f'Error charging saved card: {e}')
            raise ValueError(f'Saved card charge error: {str(e)}')

    def delete_saved_card(self, parent_order_id: str) -> bool:
        """
        Delete a saved card

        Args:
            parent_order_id: The original order ID where card was saved

        Returns:
            True if successful, False otherwise
        """
        if not self.is_configured():
            raise ValueError('BOG payment gateway is not configured')

        try:
            access_token = self._get_access_token()

            headers = {
                'Authorization': f'Bearer {access_token}'
            }

            # Use the correct endpoint: DELETE /orders/{order_id}/cards
            response = requests.delete(
                f'{self.base_url}/orders/{parent_order_id}/cards',
                headers=headers,
                timeout=30
            )

            if response.status_code == 202:  # 202 Accepted
                logger.info(f'Saved card deleted for order: {parent_order_id}')
                return True
            else:
                logger.error(f'Failed to delete saved card: {response.status_code} - {response.text}')
                return False

        except requests.RequestException as e:
            logger.error(f'Error deleting saved card: {e}')
            return False

    def enable_subscription_card_saving(self, order_id: str) -> bool:
        """
        Enable card saving for subscription recurring payments (fixed amount)

        Uses BOG /subscriptions endpoint which allows charging the same amount repeatedly.
        Different from enable_card_saving() which uses /cards and allows variable amounts.

        Documentation: https://api.bog.ge/docs/en/payments/saved-card/offline-payment

        Args:
            order_id: The BOG order ID from create_payment()

        Returns:
            True if card saving enabled successfully, False otherwise
        """
        if not self.is_configured():
            raise ValueError('BOG payment gateway is not configured')

        try:
            access_token = self._get_access_token()

            headers = {
                'Authorization': f'Bearer {access_token}'
            }

            # Use subscription endpoint: PUT /orders/{order_id}/subscriptions
            response = requests.put(
                f'{self.base_url}/orders/{order_id}/subscriptions',
                headers=headers,
                timeout=30
            )

            if response.status_code == 202:  # 202 Accepted
                logger.info(f'Subscription card saving enabled for order: {order_id}')
                return True
            else:
                logger.error(f'Failed to enable subscription card saving: {response.status_code} - {response.text}')
                return False

        except requests.RequestException as e:
            logger.error(f'Error enabling subscription card saving: {e}')
            return False

    def charge_subscription(
        self,
        parent_order_id: str,
        callback_url: str = '',
        external_order_id: str = None
    ) -> Dict:
        """
        Charge a saved subscription card with the SAME amount as the original payment

        Uses BOG /subscribe endpoint which automatically charges the same amount.
        Different from charge_saved_card() which allows specifying different amounts.

        Documentation: https://api.bog.ge/docs/en/payments/saved-card/offline-payment

        Args:
            parent_order_id: The original order ID where card was saved with /subscriptions
            callback_url: Webhook URL for payment updates
            external_order_id: Optional custom order ID for tracking

        Returns:
            Dict with order_id and details_url
        """
        if not self.is_configured():
            raise ValueError('BOG payment gateway is not configured')

        try:
            access_token = self._get_access_token()

            # Build minimal payload - amount is not specified, BOG charges same amount as original
            payload = {}

            if callback_url:
                payload['callback_url'] = callback_url
            if external_order_id:
                payload['external_order_id'] = external_order_id

            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {access_token}',
                'Accept-Language': 'en'
            }

            # POST to /ecommerce/orders/{parent_order_id}/subscribe
            response = requests.post(
                f'{self.base_url}/ecommerce/orders/{parent_order_id}/subscribe',
                json=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code in [200, 201]:
                data = response.json()
                order_id = data['id']
                details_url = data['_links']['details']['href']

                # Check if payment URL is provided (requires authentication)
                payment_url = data.get('_links', {}).get('redirect', {}).get('href')

                logger.info(f'Subscription charged: new_order_id={order_id}, parent={parent_order_id}, payment_url={payment_url}')

                result = {
                    'order_id': order_id,
                    'parent_order_id': parent_order_id,
                    'details_url': details_url,
                    'status': 'processing'
                }

                # If payment URL is provided, user needs to authenticate
                if payment_url:
                    result['payment_url'] = payment_url
                    result['requires_authentication'] = True
                else:
                    result['requires_authentication'] = False

                return result
            else:
                error_msg = f'Subscription charge failed: {response.status_code} - {response.text}'
                logger.warning(error_msg)
                raise ValueError(error_msg)

        except requests.RequestException as e:
            logger.error(f'Error charging subscription: {e}')
            raise ValueError(f'Subscription charge error: {str(e)}')

    def create_subscription_payment_with_card_save(
        self,
        package=None,
        agent_count: int = 1,
        customer_email: str = '',
        customer_name: str = '',
        company_name: str = '',
        return_url_success: str = '',
        return_url_fail: str = '',
        callback_url: str = '',
        external_order_id: str = None,
        subscription_amount: float = None
    ) -> Dict:
        """
        Create a subscription payment that charges the first month and saves card for recurring charges

        Flow:
        1. Charge the actual subscription amount upfront
        2. Enable card saving for future recurring payments
        3. Use the saved BOG order_id to charge for subsequent months

        Args:
            package: Package instance (optional for feature-based pricing)
            agent_count: Number of agents (for agent-based pricing)
            customer_email: Customer email
            customer_name: Customer name
            company_name: Company name
            return_url_success: URL to redirect after successful payment
            return_url_fail: URL to redirect after failed payment
            callback_url: Webhook URL for payment updates
            external_order_id: Optional custom order ID
            subscription_amount: Pre-calculated subscription amount (for feature-based pricing)

        Returns:
            Dict with payment details including payment_url and order_id
        """
        from .models import PricingModel

        # Calculate subscription amount
        if subscription_amount is not None:
            # Use provided subscription amount (feature-based pricing)
            calculated_subscription_amount = subscription_amount
            package_name = "Custom Feature Package"
        elif package:
            # Calculate from package (legacy)
            if package.pricing_model == PricingModel.AGENT_BASED:
                calculated_subscription_amount = float(package.price_gel) * agent_count
            else:
                calculated_subscription_amount = float(package.price_gel)
            package_name = package.display_name
        else:
            raise ValueError("Either package or subscription_amount must be provided")

        # Format description
        description = f"EchoDesk Subscription - {package_name} - {company_name}"

        # Create payment with actual subscription amount
        payment_result = self.create_payment(
            amount=calculated_subscription_amount,
            currency='GEL',
            description=description,
            customer_email=customer_email,
            customer_name=customer_name,
            return_url_success=return_url_success,
            return_url_fail=return_url_fail,
            callback_url=callback_url,
            external_order_id=external_order_id,
            metadata={
                'package_id': package.id if package else None,
                'package_name': package.name if package else 'custom',
                'agent_count': agent_count,
                'subscription_amount': calculated_subscription_amount,
                'payment_type': 'subscription',
                'company_name': company_name
            }
        )

        # Enable card saving on this order using subscription endpoint (MUST be called before user pays)
        # Uses /subscriptions endpoint which allows recurring charges of the same amount
        bog_order_id = payment_result['order_id']
        card_saving_enabled = self.enable_subscription_card_saving(bog_order_id)

        if not card_saving_enabled:
            logger.warning(f'Failed to enable subscription card saving for payment: {bog_order_id}')

        payment_result['card_saving_enabled'] = card_saving_enabled
        payment_result['subscription_amount'] = calculated_subscription_amount

        logger.info(f'Subscription payment created: {bog_order_id}, amount={calculated_subscription_amount}, card_saving={card_saving_enabled}')

        return payment_result

    def create_trial_payment_with_card_save(
        self,
        package=None,
        agent_count: int = 1,
        customer_email: str = '',
        customer_name: str = '',
        company_name: str = '',
        return_url_success: str = '',
        return_url_fail: str = '',
        callback_url: str = '',
        external_order_id: str = None,
        subscription_amount: float = None
    ) -> Dict:
        """
        DEPRECATED: Use create_subscription_payment_with_card_save instead

        Create a 0 GEL trial payment that saves the card for future recurring charges

        For 14-day free trial:
        1. Create 0 GEL payment (card verification)
        2. Enable card saving for future recurring payments
        3. After trial ends, use charge_saved_card() to charge actual amount

        Args:
            package: Package instance (optional for feature-based pricing)
            agent_count: Number of agents (for agent-based pricing)
            customer_email: Customer email
            customer_name: Customer name
            company_name: Company name
            return_url_success: URL to redirect after successful payment
            return_url_fail: URL to redirect after failed payment
            callback_url: Webhook URL for payment updates
            external_order_id: Optional custom order ID
            subscription_amount: Pre-calculated subscription amount (for feature-based pricing)

        Returns:
            Dict with payment details including payment_url and order_id
        """
        from .models import PricingModel

        # Calculate full subscription amount (for metadata only, not charged yet)
        if subscription_amount is not None:
            # Use provided subscription amount (feature-based pricing)
            calculated_subscription_amount = subscription_amount
            package_name = "Custom Feature Package"
        elif package:
            # Calculate from package (legacy)
            if package.pricing_model == PricingModel.AGENT_BASED:
                calculated_subscription_amount = float(package.price_gel) * agent_count
            else:
                calculated_subscription_amount = float(package.price_gel)
            package_name = package.display_name
        else:
            raise ValueError("Either package or subscription_amount must be provided")

        # For trial, we charge 0 GEL to save the card
        # BOG requires at least 0.01 GEL, so we use that as card verification
        trial_amount = 0.0

        # Format description
        description = f"EchoDesk 14-Day Free Trial - {package_name} - {company_name}"

        # Create payment with 0 GEL
        payment_result = self.create_payment(
            amount=trial_amount,
            currency='GEL',
            description=description,
            customer_email=customer_email,
            customer_name=customer_name,
            return_url_success=return_url_success,
            return_url_fail=return_url_fail,
            callback_url=callback_url,
            external_order_id=external_order_id,
            metadata={
                'package_id': package.id if package else None,
                'package_name': package.name if package else 'custom',
                'agent_count': agent_count,
                'subscription_amount': calculated_subscription_amount,
                'payment_type': 'trial',
                'trial_days': 14,
                'company_name': company_name
            }
        )

        # Enable card saving on this order using subscription endpoint (MUST be called before user pays)
        # Uses /subscriptions endpoint which allows recurring charges of the same amount
        bog_order_id = payment_result['order_id']
        card_saving_enabled = self.enable_subscription_card_saving(bog_order_id)

        if not card_saving_enabled:
            logger.warning(f'Failed to enable subscription card saving for trial payment: {bog_order_id}')

        payment_result['card_saving_enabled'] = card_saving_enabled
        payment_result['subscription_amount'] = subscription_amount
        payment_result['trial_days'] = 14

        return payment_result

    def create_subscription_payment(
        self,
        tenant,
        package,
        agent_count: int = 1,
        return_url_success: str = '',
        return_url_fail: str = '',
        callback_url: str = '',
        external_order_id: str = None
    ) -> Dict:
        """
        Create a subscription payment for a tenant

        Args:
            tenant: Tenant instance
            package: Package instance
            agent_count: Number of agents (for agent-based pricing)
            return_url_success: URL to redirect after successful payment
            return_url_fail: URL to redirect after failed payment
            callback_url: Webhook URL for payment updates
            external_order_id: Optional custom order ID

        Returns:
            Dict with payment details including payment_url
        """
        from .models import PricingModel

        # Calculate amount
        if package.pricing_model == PricingModel.AGENT_BASED:
            amount = float(package.price_gel) * agent_count
        else:
            amount = float(package.price_gel)

        # Format description
        description = f"EchoDesk {package.display_name} - {tenant.name}"

        # Create payment
        return self.create_payment(
            amount=amount,
            currency='GEL',
            description=description,
            customer_email=tenant.admin_email,
            customer_name=tenant.admin_name,
            return_url_success=return_url_success,
            return_url_fail=return_url_fail,
            callback_url=callback_url,
            external_order_id=external_order_id,
            metadata={
                'tenant_id': tenant.id,
                'tenant_schema': tenant.schema_name,
                'package_id': package.id,
                'package_name': package.name,
                'agent_count': agent_count,
                'subscription_type': 'new' if not hasattr(tenant, 'subscription') else 'upgrade'
            }
        )


# Convenience instance
bog_service = BOGPaymentService()
