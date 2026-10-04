from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework import exceptions
from social_integrations.models import Client
import logging

logger = logging.getLogger(__name__)


class BookingClientJWTAuthentication(JWTAuthentication):
    """
    Custom JWT authentication for booking clients using unified Client model.
    Supports both new 'client_id' claim and legacy 'booking_client_id' claim.
    """

    def get_user(self, validated_token):
        """
        Attempts to find and return a Client using the given validated token.
        """
        try:
            # Only tokens issued by THIS tenant's booking site are accepted.
            # Client ids are per-tenant row numbers and every token is signed
            # with the same key, so a token from another tenant's booking
            # page — or a shop customer's token — must not pass as the client
            # here who happens to share the id.
            from .serializers import is_booking_token_for_current_tenant
            if not is_booking_token_for_current_tenant(validated_token):
                raise exceptions.AuthenticationFailed('Token was not issued for this booking site')

            client_id = validated_token.get('client_id')

            if not client_id:
                raise exceptions.AuthenticationFailed('Token does not contain client_id')

            try:
                client = Client.objects.get(id=client_id)
            except Client.DoesNotExist:
                raise exceptions.AuthenticationFailed('Client not found')

            # Check if client has booking enabled
            if not client.is_booking_enabled:
                raise exceptions.AuthenticationFailed('Booking not enabled for this client')

            # Check if client is verified
            if not client.is_verified:
                raise exceptions.AuthenticationFailed('Email not verified')

            return client

        except KeyError:
            logger.error('client_id not found in token payload')
            raise exceptions.AuthenticationFailed('Invalid token payload')
        except exceptions.AuthenticationFailed:
            raise
        except Exception as e:
            logger.error(f'Error in BookingClientJWTAuthentication: {str(e)}')
            raise exceptions.AuthenticationFailed('Authentication failed')
