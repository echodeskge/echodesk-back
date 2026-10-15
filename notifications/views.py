from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import DevicePushToken, PushSubscription, NotificationLog
from .serializers import (
    DevicePushTokenSerializer,
    DeviceTokenDeleteSerializer,
    PushSubscriptionSerializer,
    SubscribeSerializer,
    UnsubscribeSerializer,
    NotificationLogSerializer
)
from .utils import get_vapid_keys, send_notification_to_user


class NotificationViewSet(viewsets.ViewSet):
    """ViewSet for managing push notifications."""
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get'], url_path='vapid-public-key')
    def vapid_public_key(self, request):
        """Get VAPID public key for client-side subscription."""
        vapid_keys = get_vapid_keys()
        if not vapid_keys:
            return Response(
                {'error': 'Push notifications are not configured. VAPID keys not set.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return Response({
            'public_key': vapid_keys['public_key']
        })

    @action(detail=False, methods=['post'])
    def subscribe(self, request):
        """Subscribe user to push notifications."""
        serializer = SubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        subscription_data = serializer.validated_data['subscription']

        # Extract subscription details
        endpoint = subscription_data['endpoint']
        p256dh = subscription_data['keys']['p256dh']
        auth = subscription_data['keys']['auth']

        # Get or create subscription
        subscription, created = PushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={
                'user': request.user,
                'p256dh': p256dh,
                'auth': auth,
                'user_agent': request.META.get('HTTP_USER_AGENT', ''),
                'is_active': True
            }
        )

        return Response({
            'message': 'Subscribed successfully',
            'subscription_id': subscription.id,
            'created': created
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @action(detail=False, methods=['post'])
    def unsubscribe(self, request):
        """Unsubscribe user from push notifications."""
        serializer = UnsubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        endpoint = serializer.validated_data['endpoint']

        # Find and deactivate subscription
        try:
            subscription = PushSubscription.objects.get(
                user=request.user,
                endpoint=endpoint
            )
            subscription.is_active = False
            subscription.save()

            return Response({
                'message': 'Unsubscribed successfully'
            })
        except PushSubscription.DoesNotExist:
            return Response({
                'error': 'Subscription not found'
            }, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['post'])
    def test(self, request):
        """Send a test notification to the user."""
        sent_count = send_notification_to_user(
            user=request.user,
            title='Test Notification',
            body='This is a test notification from EchoDesk!',
            data={'test': True},
            tag='test-notification'
        )

        if sent_count > 0:
            return Response({
                'message': f'Test notification sent to {sent_count} device(s)'
            })
        else:
            return Response({
                'error': 'No active subscriptions found or notification failed'
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'])
    def subscriptions(self, request):
        """Get user's active subscriptions."""
        subscriptions = PushSubscription.objects.filter(
            user=request.user,
            is_active=True
        )
        serializer = PushSubscriptionSerializer(subscriptions, many=True)
        return Response(serializer.data)

    @extend_schema(
        methods=['GET'],
        summary='List my registered mobile devices',
        description='Active Expo push tokens registered by the calling user.',
        responses={200: DevicePushTokenSerializer(many=True)},
    )
    @extend_schema(
        methods=['POST'],
        summary='Register a mobile device for push',
        description=(
            'Register (or refresh) an Expo push token for the calling user. '
            'A token already known to the server is re-pointed to the caller '
            'and re-activated. Returns 201 when a new row was created, 200 '
            'when an existing one was updated.'
        ),
        request=DevicePushTokenSerializer,
        responses={
            200: DevicePushTokenSerializer,
            201: DevicePushTokenSerializer,
            400: OpenApiResponse(description='Invalid token format or platform'),
        },
    )
    @extend_schema(
        methods=['DELETE'],
        summary='Unregister a mobile device',
        description=(
            'Deactivate one of the caller\'s Expo push tokens. Pass the token '
            'in the JSON body or as the `token` query parameter. Only the '
            'caller\'s own tokens can be removed; anything else is 404.'
        ),
        request=DeviceTokenDeleteSerializer,
        parameters=[
            OpenApiParameter(
                'token', str, OpenApiParameter.QUERY, required=False,
                description='Expo push token (alternative to the body field)',
            ),
        ],
        responses={
            200: OpenApiResponse(description='Device unregistered'),
            404: OpenApiResponse(description='Token not registered for this user'),
        },
    )
    @action(detail=False, methods=['get', 'post', 'delete'], url_path='devices')
    def devices(self, request):
        """Register / list / unregister Expo push tokens (native mobile app)."""
        if request.method == 'GET':
            devices = DevicePushToken.objects.filter(
                user=request.user,
                is_active=True
            )
            return Response(DevicePushTokenSerializer(devices, many=True).data)

        if request.method == 'POST':
            serializer = DevicePushTokenSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            fields = serializer.validated_data
            token = fields.pop('token')

            # update_or_create keyed on the token: a phone that logs in as a
            # different user moves the token to the new owner.
            device, created = DevicePushToken.objects.update_or_create(
                token=token,
                defaults={
                    'user': request.user,
                    'platform': fields['platform'],
                    'device_id': fields.get('device_id', ''),
                    'device_name': fields.get('device_name', ''),
                    'app_version': fields.get('app_version', ''),
                    'is_active': True,
                },
            )
            return Response(
                DevicePushTokenSerializer(device).data,
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )

        # DELETE — token from body (preferred) or query string
        token = None
        if isinstance(request.data, dict):
            token = request.data.get('token')
        token = token or request.query_params.get('token')
        serializer = DeviceTokenDeleteSerializer(data={'token': token})
        serializer.is_valid(raise_exception=True)

        updated = DevicePushToken.objects.filter(
            user=request.user,
            token=serializer.validated_data['token'],
        ).update(is_active=False)
        if not updated:
            return Response({
                'error': 'Device not found'
            }, status=status.HTTP_404_NOT_FOUND)
        return Response({
            'message': 'Device unregistered successfully'
        })

    @action(detail=False, methods=['get'])
    def logs(self, request):
        """Get user's notification logs."""
        logs = NotificationLog.objects.filter(user=request.user)[:50]
        serializer = NotificationLogSerializer(logs, many=True)
        return Response(serializer.data)
