"""
Custom authentication for ecommerce clients using JWT tokens with client_id claim
"""
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from .models import EcommerceClient


class EcommerceClientJWTAuthentication(BaseAuthentication):
    """
    Custom JWT authentication for ecommerce clients.

    Validates JWT tokens that contain 'client_id' claim instead of 'user_id'.
    Returns the EcommerceClient instance as the user.
    """

    def authenticate(self, request):
        """
        Authenticate the request and return a two-tuple of (user, token).
        """
        # Extract token from Authorization header
        auth_header = request.headers.get('Authorization')

        if not auth_header:
            return None  # No authentication attempted

        if not auth_header.startswith('Bearer '):
            return None  # Wrong authentication scheme

        token_string = auth_header.split(' ')[1]

        try:
            # Decode and validate the JWT token
            token = AccessToken(token_string)

            # Extract client_id from token
            client_id = token.get('client_id')
            if not client_id:
                # No client_id means this is not an ecommerce client token
                # Return None to let other authentication classes try
                return None

            # Only tokens this shop issued: a token from another tenant's
            # shop or from a booking site carries a client_id that means a
            # different person here.
            from .tokens import is_shop_token_for_current_tenant
            if not is_shop_token_for_current_tenant(token):
                raise AuthenticationFailed('Token was not issued for this shop')

            # Get the client
            try:
                client = EcommerceClient.objects.get(id=client_id, is_active=True)
            except EcommerceClient.DoesNotExist:
                raise AuthenticationFailed('Client not found or inactive')

            # Return client as user and token
            return (client, token)

        except (InvalidToken, TokenError) as e:
            # Invalid token format - let other auth classes try
            return None

    def authenticate_header(self, request):
        """
        Return a string to be used as the value of the WWW-Authenticate
        header in a 401 Unauthenticated response.
        """
        return 'Bearer realm="api"'
