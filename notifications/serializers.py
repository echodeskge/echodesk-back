from rest_framework import serializers
from .expo import is_valid_expo_token
from .models import DevicePushToken, PushSubscription, NotificationLog


class PushSubscriptionSerializer(serializers.ModelSerializer):
    """Serializer for push subscriptions."""

    class Meta:
        model = PushSubscription
        fields = ['id', 'endpoint', 'p256dh', 'auth', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']


class SubscribeSerializer(serializers.Serializer):
    """Serializer for subscription data from frontend."""
    subscription = serializers.JSONField()

    def validate_subscription(self, value):
        """Validate subscription data structure."""
        if not isinstance(value, dict):
            raise serializers.ValidationError("Subscription must be an object")

        if 'endpoint' not in value:
            raise serializers.ValidationError("Subscription must have an endpoint")

        if 'keys' not in value or not isinstance(value['keys'], dict):
            raise serializers.ValidationError("Subscription must have keys")

        keys = value['keys']
        if 'p256dh' not in keys or 'auth' not in keys:
            raise serializers.ValidationError("Subscription keys must have p256dh and auth")

        return value


class UnsubscribeSerializer(serializers.Serializer):
    """Serializer for unsubscribe request."""
    endpoint = serializers.CharField()


class DevicePushTokenSerializer(serializers.ModelSerializer):
    """Expo device token row — used for register (POST) and list (GET)."""

    class Meta:
        model = DevicePushToken
        fields = [
            'id', 'token', 'platform', 'device_id', 'device_name',
            'app_version', 'is_active', 'created_at', 'last_seen_at',
        ]
        read_only_fields = ['id', 'is_active', 'created_at', 'last_seen_at']
        # The token is unique across users on purpose: re-registering an
        # existing token re-points it to the caller (handled in the view), so
        # the default UniqueValidator must not reject it here.
        extra_kwargs = {
            'token': {'validators': []},
            'device_id': {'required': False},
            'device_name': {'required': False},
            'app_version': {'required': False},
        }

    def validate_token(self, value):
        if not is_valid_expo_token(value):
            raise serializers.ValidationError(
                "Token must look like ExponentPushToken[...]"
            )
        return value


class DeviceTokenDeleteSerializer(serializers.Serializer):
    """Serializer for unregistering a device token."""
    token = serializers.CharField()


class NotificationLogSerializer(serializers.ModelSerializer):
    """Serializer for notification logs."""
    user_email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = NotificationLog
        fields = [
            'id', 'user_email', 'title', 'body', 'data',
            'status', 'error_message', 'created_at'
        ]
        read_only_fields = fields
