"""Lazy refresh of expired Facebook / Instagram attachments.

``POST /api/social/attachments/refresh/`` — body
``{"platform": "facebook" | "instagram", "message_id": "<Meta mid>"}``.

``message_id`` is Meta's message id (``FacebookMessage.message_id`` /
``InstagramMessage.message_id``, the ``platformMessageId`` the web client
already holds for every loaded or live message) — not the DB primary key.

The web chat calls this when an attachment image fails to load. We mirror
the attachment into our storage (asking the Graph API for a fresh link when
the stored one has expired), save, and return the updated attachments.
Failures are negative-cached per message for 10 minutes so a broken image
re-rendering in a loop can't hammer Meta.
"""
import logging

from django.core.cache import cache
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .permissions import CanViewSocialMessages
from .services import media_mirror

logger = logging.getLogger(__name__)

FAILURE_TTL = 600  # seconds a failed refresh is remembered per message
LOCK_TTL = 120


class AttachmentRefreshRequestSerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=list(media_mirror.PLATFORMS))
    message_id = serializers.CharField(max_length=255, help_text="Meta message id (mid), not the DB id")


AttachmentRefreshResponseSerializer = inline_serializer(
    name='AttachmentRefreshResponse',
    fields={
        'platform': serializers.CharField(),
        'message_id': serializers.CharField(),
        'id': serializers.IntegerField(),
        'mirrored': serializers.IntegerField(),
        'already_mirrored': serializers.IntegerField(),
        'failures': serializers.ListField(child=serializers.DictField()),
        'attachments': serializers.ListField(child=serializers.DictField()),
        'attachment_url': serializers.CharField(allow_null=True),
        'attachment_type': serializers.CharField(allow_blank=True),
    },
)

AttachmentRefreshErrorSerializer = inline_serializer(
    name='AttachmentRefreshError',
    fields={
        'error': serializers.CharField(),
        'error_code': serializers.CharField(),
        'reason': serializers.CharField(required=False),
    },
)


def _visible_messages(request, platform):
    """The messages this user may see — the same queryset the
    facebook-messages / instagram-messages list endpoints serve (tenant
    connections, not soft-deleted, honouring ``hide_assigned_chats``)."""
    from . import views

    viewset_cls = views.FacebookMessageViewSet if platform == 'facebook' else views.InstagramMessageViewSet
    viewset = viewset_cls()
    viewset.request = request
    viewset.format_kwarg = None
    viewset.kwargs = {}
    viewset.args = ()
    return viewset.get_queryset()


def _cache_keys(platform, pk):
    base = f'social_att_refresh:{media_mirror.current_schema()}:{platform}:{pk}'
    return f'{base}:failed', f'{base}:lock'


@extend_schema(
    summary='Re-mirror an expired Facebook/Instagram attachment',
    description=(
        "Copies the message's attachments from Meta's CDN into EchoDesk storage, "
        "fetching fresh URLs from the Graph API when the stored links have expired, "
        "and returns the updated attachments. `message_id` is the Meta message id (mid). "
        "A failed refresh is remembered for 10 minutes (429 `refresh_cooldown`)."
    ),
    request=AttachmentRefreshRequestSerializer,
    responses={
        200: AttachmentRefreshResponseSerializer,
        400: AttachmentRefreshErrorSerializer,
        404: AttachmentRefreshErrorSerializer,
        409: AttachmentRefreshErrorSerializer,
        410: AttachmentRefreshErrorSerializer,
        429: AttachmentRefreshErrorSerializer,
        503: AttachmentRefreshErrorSerializer,
    },
    tags=['Social - Messages'],
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def refresh_social_attachments(request):
    ser = AttachmentRefreshRequestSerializer(data=request.data)
    if not ser.is_valid():
        return Response(
            {'error': 'Invalid request', 'error_code': 'invalid', 'details': ser.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )
    platform = ser.validated_data['platform']
    mid = ser.validated_data['message_id']

    related = 'page_connection' if platform == 'facebook' else 'account_connection__facebook_page'
    msg = _visible_messages(request, platform).filter(message_id=mid).select_related(related).first()
    if msg is None:
        return Response({'error': 'Message not found', 'error_code': 'not_found'},
                        status=status.HTTP_404_NOT_FOUND)

    def payload(result):
        data = result.as_dict()
        data.update({'platform': platform, 'message_id': msg.message_id, 'id': msg.pk})
        return data

    failed_key, lock_key = _cache_keys(platform, msg.pk)
    recent_failure = cache.get(failed_key)
    if recent_failure:
        return Response(
            {'error': 'Attachment refresh failed recently; try again later',
             'error_code': 'refresh_cooldown', 'reason': str(recent_failure)},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    if not cache.add(lock_key, 1, LOCK_TTL):
        return Response({'error': 'Refresh already in progress', 'error_code': 'refresh_in_progress'},
                        status=status.HTTP_409_CONFLICT)
    try:
        result = media_mirror.refresh_message(msg, platform)
    except Exception:  # noqa: BLE001
        logger.exception('Attachment refresh crashed for %s message %s', platform, msg.pk)
        cache.set(failed_key, 'error', FAILURE_TTL)
        return Response({'error': 'Attachment refresh failed', 'error_code': 'refresh_failed'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)
    finally:
        cache.delete(lock_key)

    if result.failures:
        reason = result.failures[0]['reason']
        cache.set(failed_key, reason, FAILURE_TTL)
        if result.mirrored:
            # Partial success — hand back what we have.
            return Response(payload(result))
        if result.transient:
            return Response({'error': 'Attachment temporarily unavailable',
                             'error_code': 'refresh_failed', 'reason': reason},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'error': 'Attachment is no longer available',
                         'error_code': 'attachment_unavailable', 'reason': reason},
                        status=status.HTTP_410_GONE)
    return Response(payload(result))
