import os
import requests
import logging
from datetime import datetime
from urllib.parse import urlencode, quote_plus
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth.decorators import login_required
from tenant_schemas.utils import schema_context
from django.db import IntegrityError, ProgrammingError
from django.db.models import F, Q, Max, Count, Subquery, OuterRef, Case, When, Value, CharField
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from rest_framework import viewsets, status, serializers as drf_serializers, filters
from rest_framework.decorators import api_view, permission_classes, action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
import secrets
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes
from .models import (
    FacebookPageConnection, FacebookMessage, OrphanedFacebookMessage,
    InstagramAccountConnection, InstagramMessage,
    WhatsAppBusinessAccount, WhatsAppMessage, WhatsAppMessageTemplate,
    WhatsAppContact, WhatsAppOutboundConsentLog, SocialIntegrationSettings, ConversationAutoReply,
    ChatAssignment, ChatRating, ConversationArchive,
    EmailConnection, EmailMessage, EmailDraft, EmailConnectionUserAssignment,
    TikTokShopAccount, TikTokMessage,
    EmailSignature, QuickReply,
    SocialClient, SocialClientCustomField, SocialClientCustomFieldValue, SocialAccount,
    AutoPostSettings, AutoPostContent,
    WidgetSession, WidgetMessage,
)
from .serializers import (
    FacebookPageConnectionSerializer, FacebookMessageSerializer, FacebookSendMessageSerializer,
    InstagramAccountConnectionSerializer, InstagramMessageSerializer, InstagramSendMessageSerializer,
    WhatsAppBusinessAccountSerializer, WhatsAppMessageSerializer, WhatsAppSendMessageSerializer,
    WhatsAppMessageTemplateSerializer, WhatsAppTemplateCreateSerializer, WhatsAppTemplateSendSerializer,
    WhatsAppContactSerializer, SocialIntegrationSettingsSerializer,
    ChatAssignmentSerializer, ChatAssignmentCreateSerializer, ChatRatingSerializer,
    AssignmentStatusResponseSerializer,
    PublicRatingInfoSerializer, PublicRatingSubmitSerializer,
    EmailConnectionSerializer, EmailConnectionCreateSerializer, EmailMessageSerializer,
    EmailSendSerializer, EmailDraftSerializer, EmailMessageActionSerializer, EmailFolderSerializer,
    EmailConnectionUserAssignmentSerializer, EmailConnectionUserAssignmentCreateSerializer,
    EmailConnectionWithAssignmentsSerializer,
    TikTokShopAccountSerializer, TikTokMessageSerializer, TikTokSendMessageSerializer,
    EmailSignatureSerializer, QuickReplySerializer, QuickReplyUseSerializer, QuickReplyVariablesSerializer,
    SocialClientSerializer, SocialClientListSerializer, SocialClientCreateSerializer,
    SocialClientCustomFieldSerializer, SocialAccountSerializer, SocialAccountLinkSerializer,
    UnifiedConversationSerializer, PaginatedUnifiedConversationSerializer,
    AutoPostSettingsSerializer, AutoPostContentSerializer,
)
from .pagination import SocialMessagePagination
from .permissions import (
    CanManageSocialConnections, CanViewSocialMessages,
    CanSendSocialMessages, CanManageSocialSettings, IsSuperAdmin, IsStaffUser,
    CanAccessSocial
)

# Initialize logger
logger = logging.getLogger(__name__)


# Sentinel that marks "we already looked and the row doesn't exist".
# Distinct from a falsy `None` so a single per-request lookup can
# memoise both hits and misses.
_SOCIAL_SETTINGS_MISS = object()


def get_social_settings(request):
    """Return the singleton ``SocialIntegrationSettings`` row for this tenant,
    cached for the lifetime of the request.

    The hot social endpoints all peek at this same row (12+ callsites — see
    plan ``Speed up Social Messages``). Without caching, every request
    issues an identical ``SELECT … LIMIT 1`` per touchpoint. Memoising
    against the request object collapses those into one query while keeping
    invalidation trivial: a new request gets a fresh fetch automatically.
    """
    cached = getattr(request, '_social_settings_cache', _SOCIAL_SETTINGS_MISS)
    if cached is _SOCIAL_SETTINGS_MISS:
        cached = SocialIntegrationSettings.objects.first()
        request._social_settings_cache = cached
    return cached


def convert_facebook_timestamp(timestamp):
    """Convert Facebook timestamp (Unix timestamp in milliseconds or seconds) to datetime object"""
    try:
        if timestamp == 0:
            return timezone.now()

        # Facebook timestamps can be in seconds or milliseconds
        # If timestamp is very large, it's probably in milliseconds
        if timestamp > 10000000000:  # If timestamp is greater than year 2286 in seconds, it's milliseconds
            timestamp = timestamp / 1000

        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (ValueError, TypeError):
        return timezone.now()


def extract_customer_information(message_event):
    """
    Extract customer information from messaging_customer_information field in webhook.

    Facebook Messenger can include customer information collected through instant forms
    or customer information features. This extracts that data when available.

    Args:
        message_event: The messaging event dict from Facebook webhook

    Returns:
        dict with keys like: name, email, phone, address_line_1, address_line_2,
        locality, administrative_area, country, zipcode, etc.
    """
    customer_info = {}

    messaging_customer_info = message_event.get('messaging_customer_information', {})
    if not messaging_customer_info:
        return customer_info

    # Extract responses from all screens
    screens = messaging_customer_info.get('screens', [])
    for screen in screens:
        responses = screen.get('responses', [])
        for response in responses:
            key = response.get('key')
            value = response.get('value')
            if key and value:
                customer_info[key] = value

    return customer_info


def get_assignment_for_conversation(platform, conversation_id, account_id):
    """
    Look up if a conversation is assigned to a user.
    Returns the assigned_user_id or None if not assigned.
    """
    try:
        assignment = ChatAssignment.objects.filter(
            platform=platform,
            conversation_id=conversation_id,
            account_id=account_id,
            status__in=['active', 'in_session']  # Only active assignments
        ).first()
        if assignment and assignment.assigned_user:
            return assignment.assigned_user.id
        return None
    except Exception as e:
        logger.error(f"Failed to get assignment for conversation: {e}")
        return None


def send_websocket_notification(tenant_schema, message_data, conversation_id, assigned_user_id=None):
    """Send WebSocket notification for new message

    Args:
        tenant_schema: The tenant schema name
        message_data: The message data dict
        conversation_id: The conversation ID
        assigned_user_id: The ID of the user assigned to this chat, or None if unassigned
    """
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            logger.warning("WebSocket channel layer not configured")
            return

        # Send to general messages group for this tenant
        group_name = f'messages_{tenant_schema}'

        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                'type': 'new_message',
                'message': message_data,
                'conversation_id': conversation_id,
                'timestamp': message_data.get('timestamp'),
                'assigned_user_id': assigned_user_id,  # None if unassigned, user_id if assigned
            }
        )

    except (ConnectionError, OSError) as e:
        # Connection reset by peer (errno 104) and similar transient errors
        # are expected when clients disconnect before receiving the message
        logger.warning(f"WebSocket notification could not be delivered (client may have disconnected): {e}")
    except Exception as e:
        logger.error(f"Failed to send WebSocket notification: {e}")


def find_tenant_by_page_id(page_id):
    """Find which tenant schema owns the given Facebook page ID.

    O(1) lookup against the public-schema SocialPlatformRoute table, with a
    self-healing fallback to the legacy full scan. See
    ``social_integrations.platform_routing.resolve_tenant``.
    """
    from social_integrations.platform_routing import resolve_tenant
    return resolve_tenant("facebook", page_id)


def find_tenant_by_whatsapp_phone_number_id(phone_number_id):
    """Find which tenant schema owns the given WhatsApp phone number ID (O(1) + fallback)."""
    from social_integrations.platform_routing import resolve_tenant
    return resolve_tenant("whatsapp", phone_number_id)


# ============================================================================
# AUTO-REPLY HELPER FUNCTIONS
# ============================================================================

def process_auto_reply(platform, account_id, conversation_id, sender_name, connection):
    """
    Process auto-reply for incoming message.
    Returns True if auto-reply was sent, False otherwise.
    Does NOT mark messages as read.

    Args:
        platform: 'facebook', 'instagram', or 'whatsapp'
        account_id: page_id, instagram_account_id, or waba_id
        conversation_id: sender_id or from_number
        sender_name: Customer's display name for personalization
        connection: FacebookPageConnection, InstagramAccountConnection, or WhatsAppBusinessAccount
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from pytz import timezone as _pytz_tz
        ZoneInfo = _pytz_tz

    try:
        settings = SocialIntegrationSettings.objects.first()
        if not settings or not settings.auto_reply_settings:
            return False

        # Get platform-specific settings
        platform_settings = settings.auto_reply_settings.get(platform, {})
        if not platform_settings:
            return False

        # Get or create conversation tracking
        tracking, created = ConversationAutoReply.objects.get_or_create(
            platform=platform,
            account_id=account_id,
            conversation_id=conversation_id,
        )

        now = timezone.now()

        # Check if within away hours using schedule grid
        is_away = False
        if settings.away_hours_enabled and settings.away_hours_schedule:
            try:
                business_tz = ZoneInfo(settings.timezone)
                local_dt = now.astimezone(business_tz)
                current_hour = local_dt.hour
                current_day = local_dt.strftime('%A').lower()  # monday, tuesday, etc.

                day_schedule = settings.away_hours_schedule.get(current_day, [])
                is_away = current_hour in day_schedule
                logger.info(f"🕐 Auto-reply check: {current_day} {current_hour}:00 in {settings.timezone}, is_away={is_away}")
            except Exception as e:
                logger.error(f"Error checking away hours: {e}")

        # Determine which message to send
        message_to_send = None
        is_welcome = False

        # Check welcome message - only once per conversation (session), only during working hours
        welcome_enabled = platform_settings.get('welcome_enabled', False)
        welcome_message = platform_settings.get('welcome_message', '')
        if welcome_enabled and welcome_message and not is_away:
            # Only send welcome message if never sent before for this conversation
            if tracking.last_welcome_sent is None:
                message_to_send = welcome_message
                is_welcome = True
                logger.info(f"📬 Will send welcome message (first time for this conversation, during working hours)")
            else:
                logger.info(f"⏭️ Skipping welcome message (already sent: {tracking.last_welcome_sent})")
        elif welcome_enabled and welcome_message and is_away:
            logger.info(f"⏭️ Skipping welcome message (outside working hours)")

        # Away message takes precedence if within away hours - platform specific
        # Only send one away message per 12 hours per conversation
        away_enabled = platform_settings.get('away_enabled', False)
        away_message = platform_settings.get('away_message', '')
        if is_away and away_enabled and away_message:
            should_send_away = (
                tracking.last_away_sent is None or
                (now - tracking.last_away_sent).total_seconds() >= 12 * 3600
            )
            if should_send_away:
                message_to_send = away_message
                is_welcome = False
                logger.info(f"📬 Will send away message (last sent: {tracking.last_away_sent})")
            else:
                logger.info(f"⏭️ Skipping away message (sent {tracking.last_away_sent}, cooldown active)")

        if not message_to_send:
            return False

        # Replace placeholders
        message_to_send = message_to_send.replace('{{customer_name}}', sender_name or 'there')

        # Send the auto-reply (platform-specific)
        success = send_auto_reply(platform, conversation_id, message_to_send, connection)

        if success:
            if is_welcome:
                tracking.last_welcome_sent = now
            else:
                tracking.last_away_sent = now
            tracking.save()
            logger.info(f"✅ Auto-reply sent successfully to {conversation_id}")

        return success

    except Exception as e:
        logger.error(f"❌ Error processing auto-reply: {e}")
        return False


def send_auto_reply(platform, recipient_id, message_text, connection):
    """Queue an auto-reply for asynchronous delivery.

    Returns ``True`` when the reply was successfully *enqueued* — the inbound
    webhook no longer blocks on the outbound Graph/WhatsApp API call. Actual
    delivery + DB persistence happen in ``send_auto_reply_task`` (with retry on
    transient connection failures). Tracking timestamps in ``process_auto_reply``
    are therefore set optimistically, which is the desired behaviour for
    welcome/away messages (better to under-send than to spam on a retry storm).

    Falls back to a synchronous send if the task can't be enqueued (e.g. broker
    unavailable) so auto-replies still work in degraded conditions.
    """
    from django.db import connection as db_connection

    if platform not in ('facebook', 'instagram', 'whatsapp'):
        return False

    schema = getattr(db_connection, 'schema_name', None)
    if not schema or schema == 'public':
        return False

    try:
        from social_integrations.tasks import send_auto_reply_task
        send_auto_reply_task.delay(
            schema, platform, recipient_id, message_text, connection.pk
        )
        return True
    except Exception as e:  # noqa: BLE001 — broker down / eager error → degrade to sync
        logger.warning(f"Auto-reply enqueue failed ({e}); sending synchronously")
        if platform == 'facebook':
            return send_facebook_auto_reply(recipient_id, message_text, connection)
        elif platform == 'instagram':
            return send_instagram_auto_reply(recipient_id, message_text, connection)
        elif platform == 'whatsapp':
            return send_whatsapp_auto_reply(recipient_id, message_text, connection)
        return False


def send_facebook_auto_reply(recipient_id, message_text, page_connection):
    """Send Facebook auto-reply and save to DB"""
    try:
        send_url = "https://graph.facebook.com/v23.0/me/messages"
        message_data = {
            'recipient': {'id': recipient_id},
            'message': {'text': message_text},
            'messaging_type': 'RESPONSE'
        }
        response = requests.post(
            send_url,
            json=message_data,
            params={'access_token': page_connection.page_access_token},
            timeout=30
        )

        if response.status_code == 200:
            response_data = response.json()
            message_id = response_data.get('message_id', f"auto_{datetime.now().timestamp()}")

            # Save to database - use recipient_id for conversation grouping
            FacebookMessage.objects.create(
                page_connection=page_connection,
                message_id=message_id,
                sender_id=recipient_id,  # Use recipient_id for conversation grouping
                sender_name=page_connection.page_name,
                message_text=message_text,
                timestamp=timezone.now(),
                is_from_page=True,
                source='echodesk',
                is_echo=False,
                sent_by=None,  # Auto-reply, no user
            )
            logger.info(f"✅ Facebook auto-reply sent to {recipient_id}: {message_id}")
            return True
        else:
            logger.error(f"❌ Facebook auto-reply failed: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        logger.error(f"❌ Facebook auto-reply exception: {e}")
        return False


def send_instagram_auto_reply(recipient_id, message_text, account_connection):
    """Send Instagram auto-reply and save to DB"""
    try:
        # Instagram messages are sent through the Facebook Page, not the Instagram account directly
        # We use /me/messages with platform=instagram parameter
        if not account_connection.facebook_page:
            logger.error("❌ Instagram auto-reply failed: No linked Facebook Page")
            return False

        send_url = "https://graph.facebook.com/v23.0/me/messages"
        message_data = {
            'recipient': {'id': recipient_id},
            'message': {'text': message_text},
            'messaging_type': 'RESPONSE'  # Required for Instagram within 24hr window
        }

        # Use the Facebook Page's access token with platform=instagram
        access_token = account_connection.facebook_page.page_access_token

        response = requests.post(
            send_url,
            json=message_data,
            params={
                'access_token': access_token,
                'platform': 'instagram'
            },
            timeout=30
        )

        if response.status_code == 200:
            response_data = response.json()
            message_id = response_data.get('message_id', f"auto_{datetime.now().timestamp()}")

            # Save to database - use recipient_id for conversation grouping
            InstagramMessage.objects.create(
                account_connection=account_connection,
                message_id=message_id,
                sender_id=recipient_id,  # Use recipient_id for conversation grouping
                sender_name=account_connection.username,
                sender_username=account_connection.username,
                message_text=message_text,
                timestamp=timezone.now(),
                is_from_business=True,
                source='echodesk',
                is_echo=False,
                sent_by=None,  # Auto-reply, no user
            )
            logger.info(f"✅ Instagram auto-reply sent to {recipient_id}: {message_id}")
            return True
        else:
            logger.error(f"❌ Instagram auto-reply failed: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        logger.error(f"❌ Instagram auto-reply exception: {e}")
        return False


def send_whatsapp_auto_reply(recipient_number, message_text, account):
    """Send WhatsApp auto-reply and save to DB"""
    try:
        # Format phone number (remove + if present for API)
        to_number = recipient_number.lstrip('+')

        send_url = f"https://graph.facebook.com/v23.0/{account.phone_number_id}/messages"
        message_data = {
            'messaging_product': 'whatsapp',
            'to': to_number,
            'type': 'text',
            'text': {'body': message_text}
        }

        response = requests.post(
            send_url,
            json=message_data,
            headers={'Authorization': f'Bearer {account.access_token}'},
            timeout=30
        )

        if response.status_code == 200:
            response_data = response.json()
            messages = response_data.get('messages', [])
            message_id = messages[0].get('id') if messages else f"auto_{datetime.now().timestamp()}"

            # Save to database
            WhatsAppMessage.objects.create(
                business_account=account,
                message_id=message_id,
                from_number=account.phone_number,
                to_number=recipient_number,
                contact_name='',
                message_text=message_text,
                message_type='text',
                timestamp=timezone.now(),
                is_from_business=True,
                status='sent',
                source='cloud_api',
                is_echo=False,
                sent_by=None,  # Auto-reply, no user
            )
            logger.info(f"✅ WhatsApp auto-reply sent: {message_id}")
            return True
        else:
            logger.error(f"❌ WhatsApp auto-reply failed: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        logger.error(f"❌ WhatsApp auto-reply exception: {e}")
        return False


# ============================================================================
# AUTO-UNARCHIVE HELPER FUNCTION
# ============================================================================

def auto_unarchive_conversation(platform, conversation_id, account_id):
    """
    Automatically unarchive a conversation when a new message is received.
    This ensures conversations that receive new messages pop back into the main inbox.

    Args:
        platform: 'facebook', 'instagram', 'whatsapp', or 'email'
        conversation_id: sender_id, from_number, or thread_id
        account_id: page_id, instagram_account_id, waba_id, or email connection id
    """
    try:
        deleted_count, _ = ConversationArchive.objects.filter(
            platform=platform,
            conversation_id=conversation_id,
            account_id=account_id
        ).delete()

        if deleted_count > 0:
            logger.info(f"✅ Auto-unarchived {platform} conversation {conversation_id} (was archived)")

    except ProgrammingError:
        # Table doesn't exist yet (migration not applied on this tenant) — safe to ignore
        logger.debug("ConversationArchive table not yet created, skipping unarchive check")
    except Exception as e:
        logger.error(f"❌ Failed to auto-unarchive conversation: {e}")


class FacebookPageConnectionViewSet(viewsets.ModelViewSet):
    serializer_class = FacebookPageConnectionSerializer
    permission_classes = [IsAuthenticated, CanManageSocialConnections]

    def get_queryset(self):
        return FacebookPageConnection.objects.all()  # Tenant schema provides isolation

    def perform_create(self, serializer):
        serializer.save()  # No user assignment needed in multi-tenant setup


class FacebookMessageViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = FacebookMessageSerializer
    permission_classes = [IsAuthenticated, CanViewSocialMessages]
    pagination_class = SocialMessagePagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # Shared mutable cache to avoid N+1 queries in get_recipient_name
        if '_recipient_names_cache' not in context:
            context['_recipient_names_cache'] = {}
        return context
    filterset_fields = {
        'page_connection__page_id': ['exact'],
        'sender_id': ['exact'],
        'is_from_page': ['exact'],
        'is_read_by_staff': ['exact'],
    }
    search_fields = ['sender_name', 'message_text']
    ordering_fields = ['timestamp', 'created_at']
    ordering = ['-timestamp']

    def get_queryset(self):
        tenant_pages = FacebookPageConnection.objects.all()  # All pages for this tenant
        # Filter out soft-deleted messages
        base_queryset = FacebookMessage.objects.filter(
            page_connection__in=tenant_pages,
            is_deleted=False
        ).select_related('page_connection', 'sent_by', 'reply_to')

        # Apply page_id filter from query params (support both formats)
        page_id = self.request.query_params.get('page_id')
        if page_id:
            base_queryset = base_queryset.filter(page_connection__page_id=page_id)

        # Check if hiding assigned chats is enabled
        settings_obj = get_social_settings(self.request)
        if settings_obj and settings_obj.hide_assigned_chats:
            # Admin/superuser sees all messages
            if not (self.request.user.is_superuser or self.request.user.is_staff):
                # Get page IDs for this tenant
                page_ids = list(tenant_pages.values_list('page_id', flat=True))

                # Only active assignments count — completed sessions have
                # ``assigned_user=None`` (see end_session view) so without the
                # status filter, completed chats land in ``all_assigned`` but
                # not ``my_assigned`` and get excluded for every non-staff
                # user. This bit teonagolodze.16@gmail.com on amanati,
                # 2026-04-21: after ending a chat she couldn't see its
                # history on return. Mirrors the email-viewset pattern below.
                active_assignments = ChatAssignment.objects.filter(
                    platform='facebook',
                    account_id__in=page_ids,
                    status__in=['active', 'in_session'],
                )
                my_assigned = active_assignments.filter(
                    assigned_user=self.request.user
                ).values_list('conversation_id', flat=True)
                all_assigned = active_assignments.values_list('conversation_id', flat=True)

                # Return: assigned to me OR not assigned to anyone active
                base_queryset = base_queryset.filter(
                    Q(sender_id__in=my_assigned) | ~Q(sender_id__in=all_assigned)
                )

        # Check for initial_load parameter - returns unread messages + 10 most recent
        initial_load = self.request.query_params.get('initial_load', '').lower() == 'true'
        sender_id = self.request.query_params.get('sender_id')

        if initial_load and sender_id:
            # Filter to this conversation
            conv_queryset = base_queryset.filter(sender_id=sender_id)

            # Count unread incoming messages
            unread_count = conv_queryset.filter(
                is_from_page=False,
                is_read_by_staff=False
            ).count()

            # Get unread + 10 messages (minimum 10)
            limit = max(unread_count + 10, 10)

            # Return the most recent `limit` messages
            recent_ids = list(conv_queryset.order_by('-timestamp').values_list('id', flat=True)[:limit])
            return base_queryset.filter(id__in=recent_ids)

        return base_queryset


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def facebook_oauth_start(request):
    """Generate Facebook OAuth URL for business pages access"""
    logger = logging.getLogger(__name__)
    
    try:
        fb_app_id = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_APP_ID')
        if not fb_app_id:
            return Response({
                'error': 'Facebook App ID not configured'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Use public callback URL since Facebook needs a consistent redirect URI
        redirect_uri = f'https://{settings.API_DOMAIN}/api/social/facebook/oauth/callback/'
        
        # Include tenant info in state parameter - no user_id needed since tenant is unique
        from urllib.parse import quote
        tenant_obj = getattr(request, "tenant", None)
        
        # Extract tenant schema name from Tenant object or use default
        if tenant_obj and hasattr(tenant_obj, 'schema_name'):
            tenant_name = tenant_obj.schema_name
        elif tenant_obj and hasattr(tenant_obj, 'name'):
            tenant_name = tenant_obj.name
        else:
            tenant_name = "amanati"  # Default fallback
        
        # Simplified state parameter with just tenant schema
        state_raw = f'tenant={tenant_name}'
        state = quote(state_raw)  # URL encode the state
        logger.info(f"Tenant object: {tenant_obj}")
        logger.info(f"Extracted tenant schema: {tenant_name}")
        logger.info(f"Generated raw state parameter: {state_raw}")
        logger.info(f"URL encoded state parameter: {state}")
        
        # Facebook OAuth URL for business pages with Messenger Platform for Instagram
        # Using pages_messaging for both Facebook and Instagram messaging
        # instagram_basic for accessing connected Instagram accounts
        # instagram_manage_messages for reading and responding to Instagram DMs (requires app review)
        # https://developers.facebook.com/docs/messenger-platform/instagram/get-started
        oauth_url = (
            f"https://www.facebook.com/v23.0/dialog/oauth?"
            f"client_id={fb_app_id}&"
            f"redirect_uri={quote(redirect_uri)}&"
            f"scope=business_management,pages_show_list,pages_manage_metadata,pages_messaging,pages_read_engagement,instagram_basic,instagram_manage_messages,public_profile,email&"
            f"state={state}&"
            f"response_type=code&"
            f"display=popup"
        )
        
        return Response({
            'oauth_url': oauth_url,
            'redirect_uri': redirect_uri,
            'instructions': 'Visit the OAuth URL to connect your Facebook pages'
        })
    except Exception as e:
        return Response({
            'error': f'Failed to generate OAuth URL: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([])  # No authentication required for Facebook callbacks
def facebook_oauth_callback(request):
    """Handle Facebook OAuth callback, exchange code for access token, and save page connections"""
    logger = logging.getLogger(__name__)
    
    try:
        # Get all parameters from Facebook callback
        code = request.GET.get('code')
        error = request.GET.get('error')
        error_description = request.GET.get('error_description')
        error_reason = request.GET.get('error_reason')
        state = request.GET.get('state')
        
        # Parse state to get tenant info - no user_id needed
        tenant_name = None
        is_publishing_upgrade = False
        if state:
            from urllib.parse import unquote
            # URL decode the state parameter in case it's encoded
            decoded_state = unquote(state)
            logger.info(f"Raw state parameter: {state}")
            logger.info(f"Decoded state parameter: {decoded_state}")

            # State format: "tenant=amanati" or "tenant=amanati&publishing=true"
            try:
                for param in decoded_state.split('&'):
                    if param.startswith('tenant='):
                        tenant_name = param.split('=', 1)[1]
                        logger.info(f"Parsed tenant_name: {tenant_name}")
                    elif param.startswith('publishing='):
                        is_publishing_upgrade = param.split('=', 1)[1].lower() == 'true'
                        logger.info(f"Publishing upgrade: {is_publishing_upgrade}")
            except (ValueError, IndexError) as e:
                logger.error(f"Error parsing state parameter: {e}")
                return JsonResponse({
                    'status': 'error',
                    'message': f'Invalid state parameter format: {state}',
                    'error': str(e),
                    'expected_format': 'tenant=TenantName'
                })
        
        # Validate that we have required parameters
        if not tenant_name:
            logger.error(f"No tenant_name found in state: {state}")
            frontend_url = f"https://amanati.{settings.MAIN_DOMAIN}"  # Default fallback
            error_msg = "Tenant name not found in state parameter"
            from urllib.parse import quote_plus
            return redirect(f"{frontend_url}/social/connections?facebook_status=error&message={quote_plus(error_msg)}")
        
        # Extract tenant schema name from tenant_name - use tenant_name directly as schema
        tenant_schema = tenant_name if tenant_name else 'amanati'
        
        # Frontend dashboard URL using tenant name
        frontend_url = f"https://{tenant_schema}.{settings.MAIN_DOMAIN}"
        
        # Find a suitable user for this tenant (superuser or admin)
        from tenant_schemas.utils import schema_context
        from django.contrib.auth import get_user_model
        User = get_user_model()
        
        user_id = None
        with schema_context(tenant_schema):
            # Try to find a superuser first
            superuser = User.objects.filter(is_superuser=True, is_active=True).first()
            if superuser:
                user_id = superuser.id
                logger.info(f"Found superuser for tenant {tenant_schema}: {superuser.email}")
            else:
                # If no superuser, try to find an admin
                admin_user = User.objects.filter(role='admin', is_active=True).first()
                if admin_user:
                    user_id = admin_user.id
                    logger.info(f"Found admin user for tenant {tenant_schema}: {admin_user.email}")
                else:
                    # If no admin, use any active user
                    any_user = User.objects.filter(is_active=True).first()
                    if any_user:
                        user_id = any_user.id
                        logger.info(f"Found active user for tenant {tenant_schema}: {any_user.email}")
        
        if not user_id:
            logger.error(f"No active users found in tenant {tenant_schema}")
            frontend_url = f"https://{tenant_schema}.{settings.MAIN_DOMAIN}"
            error_msg = f"No active users found in tenant {tenant_schema}"
            from urllib.parse import quote_plus
            return redirect(f"{frontend_url}/social/connections?facebook_status=error&message={quote_plus(error_msg)}")
        
        # Frontend dashboard URL
        frontend_url = f"https://{tenant_schema}.{settings.MAIN_DOMAIN}"
        
        # Handle Facebook errors (user denied, etc.)
        if error:
            error_msg = f"Facebook OAuth failed: {error_description or error}"
            logger.error(f"Facebook OAuth error: {error} - {error_description}")
            from urllib.parse import quote_plus
            return redirect(f"{frontend_url}/social/connections?facebook_status=error&message={quote_plus(error_msg)}")
        
        # Handle missing code
        if not code:
            error_msg = "Authorization code not provided by Facebook"
            logger.error("Facebook OAuth callback missing authorization code")
            from urllib.parse import quote_plus
            return redirect(f"{frontend_url}/social/connections?facebook_status=error&message={quote_plus(error_msg)}")
        
        # Exchange authorization code for access token
        token_url = f"https://graph.facebook.com/{getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_API_VERSION', 'v23.0')}/oauth/access_token"
        token_params = {
            'client_id': getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_APP_ID'),
            'client_secret': getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_APP_SECRET'),
            'redirect_uri': f'https://{settings.API_DOMAIN}/api/social/facebook/oauth/callback/',  # Must match OAuth URL exactly
            'code': code
        }
        
        logger.info(f"Exchanging Facebook code for access token using URL: {token_url}")
        logger.info(f"Token exchange parameters: {dict(token_params, client_secret='[HIDDEN]')}")
        token_response = requests.get(token_url, params=token_params, timeout=30)
        token_data = token_response.json()
        
        if 'error' in token_data:
            error_msg = f"Token exchange failed: {token_data.get('error', {}).get('message', 'Unknown error')}"
            logger.error(f"Facebook token exchange error: {token_data}")
            from urllib.parse import quote_plus
            return redirect(f"{frontend_url}/social/connections?facebook_status=error&message={quote_plus(error_msg)}")
        
        user_access_token = token_data.get('access_token')
        if not user_access_token:
            error_msg = "No access token received from Facebook"
            logger.error("Facebook token exchange did not return access token")
            from urllib.parse import quote_plus
            return redirect(f"{frontend_url}/social/connections?facebook_status=error&message={quote_plus(error_msg)}")
        
        # Get user's Facebook pages - simplified approach
        pages_url = f"https://graph.facebook.com/{getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_API_VERSION', 'v23.0')}/me/accounts"
        pages_params = {
            'access_token': user_access_token,
            'fields': 'id,name,access_token,category'
        }
        
        logger.info(f"Fetching Facebook pages for tenant {tenant_name}...")
        logger.info(f"Pages API URL: {pages_url}")
        logger.info(f"Pages API params: {dict(pages_params, access_token='[HIDDEN]')}")
        
        pages_response = requests.get(pages_url, params=pages_params, timeout=30)
        pages_data = pages_response.json()
        
        logger.info(f"Pages API response status: {pages_response.status_code}")
        logger.info(f"Pages API response data: {pages_data}")
        
        if 'error' in pages_data:
            error_msg = f"Failed to fetch pages: {pages_data.get('error', {}).get('message', 'Unknown error')}"
            logger.error(f"Facebook pages fetch error: {pages_data}")
            from urllib.parse import quote_plus
            return redirect(f"{frontend_url}/social/connections?facebook_status=error&message={quote_plus(error_msg)}")
        
        pages = pages_data.get('data', [])
        
        # Enhanced debugging for no pages scenario
        if not pages:
            logger.warning("User has no Facebook pages to connect")
            
            # Also try to get user info to see what we can access
            user_info_url = f"https://graph.facebook.com/{getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_API_VERSION', 'v23.0')}/me"
            user_info_params = {
                'access_token': user_access_token,
                'fields': 'id,name,email'
            }
            user_info_response = requests.get(user_info_url, params=user_info_params, timeout=30)
            user_info_data = user_info_response.json()
            
            return JsonResponse({
                'status': 'error', 
                'message': 'No Facebook pages found for this account.',
                'help': 'This could be because your app is in Development Mode. Only pages you admin can be accessed.',
                'debug_info': {
                    'pages_response': pages_data,
                    'user_info': user_info_data,
                    'api_url': pages_url,
                    'app_mode': 'Development Mode - limited access',
                    'solutions': [
                        'Make sure you are an admin of the Facebook page',
                        'Create a Facebook business page at facebook.com/pages/create',
                        'Switch app to Live Mode (requires app review)',
                        'Add your Facebook account as a developer/tester in the app'
                    ]
                }
            })
        
        # Import tenant schema context for multi-tenant database operations
        from tenant_schemas.utils import schema_context
        
        # Save page connections to database with proper tenant context.
        # IMPORTANT: do NOT wipe existing connections first. FacebookMessage has
        # on_delete=CASCADE to FacebookPageConnection, so a tenant-wide
        # FacebookPageConnection.objects.all().delete() cascades through every
        # historical message and routinely takes longer than Daphne's request
        # shutdown window for large tenants — the OAuth callback gets killed
        # mid-flight and the popup never reaches the success redirect.
        # update_or_create below already upserts each page returned by the
        # callback; pages that disappear from /me/accounts are deactivated via
        # the post-loop sweep instead, preserving their messages.
        saved_pages = 0
        with schema_context(tenant_schema):
            # Create new connections for all pages from callback
            saved_instagram_accounts = 0
            returned_page_ids: set[str] = set()
            for page in pages:
                page_id = page.get('id')
                page_name = page.get('name')
                page_access_token = page.get('access_token')

                if page_id and page_access_token and page_name:
                    # Create or reactivate page connection for this tenant
                    # Use try/except to handle race conditions with concurrent OAuth callbacks
                    page_defaults = {
                                'page_name': page_name,
                                'page_access_token': page_access_token,
                                'is_active': True,
                            }
                    if is_publishing_upgrade:
                        page_defaults['has_publishing_permission'] = True
                    try:
                        page_connection, created = FacebookPageConnection.objects.update_or_create(
                            page_id=page_id,
                            defaults=page_defaults,
                        )
                    except IntegrityError:
                        # Race condition: another request already created this page connection
                        # Fall back to updating the existing record
                        page_connection = FacebookPageConnection.objects.filter(page_id=page_id).first()
                        if page_connection:
                            page_connection.page_name = page_name
                            page_connection.page_access_token = page_access_token
                            page_connection.is_active = True
                            if is_publishing_upgrade:
                                page_connection.has_publishing_permission = True
                            page_connection.save()
                            created = False
                        else:
                            logger.error(f"❌ Could not create or find FacebookPageConnection for page {page_id}")
                            continue

                    returned_page_ids.add(page_id)
                    action = "Created" if created else "Reactivated"
                    logger.info(f"✅ {action} Facebook page connection: {page_name} ({page_id}) in schema {tenant_schema}")
                    saved_pages += 1

                    # Subscribe page to webhooks
                    try:
                        subscribe_url = f"https://graph.facebook.com/v23.0/{page_id}/subscribed_apps"
                        subscribe_params = {
                            'subscribed_fields': 'messages,messaging_postbacks,message_reads,message_deliveries,message_reactions,message_echoes',
                            'access_token': page_access_token
                        }
                        logger.info(f"📡 Subscribing page {page_name} ({page_id}) to webhooks...")
                        subscribe_response = requests.post(subscribe_url, params=subscribe_params, timeout=30)
                        subscribe_data = subscribe_response.json()

                        if subscribe_response.status_code == 200 and subscribe_data.get('success'):
                            logger.info(f"✅ Subscribed page {page_name} ({page_id}) to webhooks")
                        else:
                            logger.error(f"❌ Failed to subscribe page {page_name} to webhooks: {subscribe_data}")
                    except Exception as subscribe_error:
                        logger.error(f"❌ Error subscribing page {page_name} to webhooks: {subscribe_error}")

                    # Try to fetch Instagram Business Account connected to this page
                    try:
                        instagram_url = f"https://graph.facebook.com/v23.0/{page_id}"
                        instagram_params = {
                            'fields': 'instagram_business_account',
                            'access_token': page_access_token
                        }
                        instagram_response = requests.get(instagram_url, params=instagram_params, timeout=30)

                        if instagram_response.status_code == 200:
                            instagram_data = instagram_response.json()
                            instagram_account = instagram_data.get('instagram_business_account')

                            if instagram_account:
                                instagram_account_id = instagram_account.get('id')

                                # Fetch Instagram account details
                                ig_details_url = f"https://graph.facebook.com/v23.0/{instagram_account_id}"
                                ig_details_params = {
                                    'fields': 'id,username,profile_picture_url',
                                    'access_token': page_access_token
                                }
                                ig_details_response = requests.get(ig_details_url, params=ig_details_params, timeout=30)

                                if ig_details_response.status_code == 200:
                                    ig_details = ig_details_response.json()

                                    # Create or update Instagram account connection
                                    InstagramAccountConnection.objects.update_or_create(
                                        instagram_account_id=ig_details.get('id'),
                                        defaults={
                                            'username': ig_details.get('username', ''),
                                            'profile_picture_url': ig_details.get('profile_picture_url', ''),
                                            'access_token': page_access_token,
                                            'facebook_page': page_connection,
                                            'is_active': True
                                        }
                                    )

                                    logger.info(f"✅ Connected Instagram account: @{ig_details.get('username')} to page {page_name}")
                                    saved_instagram_accounts += 1
                    except Exception as e:
                        logger.warning(f"⚠️ Could not fetch Instagram account for page {page_name}: {e}")
                        # Continue processing other pages even if Instagram fetch fails
                else:
                    logger.warning(f"⚠️ Skipped page with missing data: {page}")

            # Pages the tenant previously connected but Meta no longer returns
            # for this user (revoked, removed from Business Portfolio, etc.):
            # deactivate them so webhooks/send-message stop, but keep messages.
            stale_pages = FacebookPageConnection.objects.filter(is_active=True).exclude(page_id__in=returned_page_ids)
            stale_count = stale_pages.update(is_active=False) if returned_page_ids else 0
            if stale_count:
                logger.info(
                    "Deactivated %d Facebook page connection(s) not returned by /me/accounts in schema %s",
                    stale_count, tenant_schema,
                )

        # Return success response with redirect to tenant frontend
        if saved_instagram_accounts > 0:
            success_msg = f"Successfully connected {saved_pages} Facebook page(s) and {saved_instagram_accounts} Instagram account(s)"
        else:
            success_msg = f"Successfully connected {saved_pages} Facebook page(s)"
        logger.info(f"Facebook OAuth completed successfully: {saved_pages} pages and {saved_instagram_accounts} Instagram accounts saved")
        
        # Redirect to tenant frontend with success parameters
        from urllib.parse import quote_plus
        return redirect(f"{frontend_url}/social/connections?facebook_status=connected&pages={saved_pages}&message={quote_plus(success_msg)}")
        
    except Exception as e:
        logger.error(f"Facebook OAuth callback processing failed: {e}")
        # Redirect to frontend with error message
        frontend_url = f"https://amanati.{settings.MAIN_DOMAIN}"  # Default fallback
        if 'tenant_schema' in locals() and tenant_schema:
            frontend_url = f"https://{tenant_schema}.{settings.MAIN_DOMAIN}"
        
        error_msg = f"OAuth processing failed: {str(e)}"
        from urllib.parse import quote_plus
        return redirect(f"{frontend_url}/social/connections?facebook_status=error&message={quote_plus(error_msg)}")


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def facebook_connection_status(request):
    """Check Facebook connection status for current tenant"""
    try:
        # Only count ACTIVE pages for connection status
        active_pages = FacebookPageConnection.objects.filter(is_active=True)
        all_pages = FacebookPageConnection.objects.all()

        pages_data = []
        for page in all_pages:
            pages_data.append({
                'id': page.id,
                'page_id': page.page_id,
                'page_name': page.page_name,
                'is_active': page.is_active,
                'connected_at': page.created_at.isoformat()
            })

        return Response({
            'connected': active_pages.exists(),  # Only active pages count as connected
            'pages_count': active_pages.count(),  # Only count active pages
            'pages': pages_data  # But show all pages with their is_active status
        })
    except Exception as e:
        return Response({
            'error': f'Failed to get connection status: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def facebook_disconnect(request):
    """
    Disconnect Facebook integration for current tenant

    Always performs soft delete (sets is_active=False) to preserve messages.
    Use the clear-history endpoint to permanently delete messages.
    """
    try:
        logger = logging.getLogger(__name__)

        # Get all pages for this tenant
        pages_to_disconnect = FacebookPageConnection.objects.all()
        page_count = pages_to_disconnect.count()
        page_names = list(pages_to_disconnect.values_list('page_name', flat=True))

        if page_count == 0:
            return Response({
                'status': 'no_pages',
                'message': 'No Facebook pages found to disconnect'
            })

        # SOFT DELETE: Deactivate pages, keep data for audit trail
        now = timezone.now()
        deactivated_count = pages_to_disconnect.update(
            is_active=False,
            deactivated_at=now,
            deactivation_reason='manual',
            updated_at=now
        )

        logger.info(f"✅ Facebook soft disconnect completed:")
        logger.info(f"   - Facebook pages deactivated: {deactivated_count}")
        logger.info(f"   - Deactivation reason: manual")
        logger.info(f"   - Messages preserved for audit trail")
        logger.info(f"   - Webhooks will now return 404 for these pages")

        return Response({
            'status': 'disconnected',
            'facebook_pages_deactivated': deactivated_count,
            'deactivated_pages': page_names,
            'message': f'Deactivated {deactivated_count} Facebook page(s). Messages preserved. Webhooks will be rejected.'
        })

    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to disconnect Facebook: {e}")
        return Response({
            'error': f'Failed to disconnect: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def facebook_page_disconnect(request, page_id):
    """
    Disconnect a specific Facebook page by page_id

    Always performs soft delete (sets is_active=False) to preserve messages.
    Use the clear-history endpoint to permanently delete messages.
    """
    try:
        logger = logging.getLogger(__name__)

        # Get the specific page for this tenant
        try:
            page = FacebookPageConnection.objects.get(page_id=page_id)
        except FacebookPageConnection.DoesNotExist:
            return Response({
                'error': f'Facebook page {page_id} not found for this tenant'
            }, status=status.HTTP_404_NOT_FOUND)

        page_name = page.page_name

        # SOFT DELETE: Deactivate page, keep data
        now = timezone.now()
        page.is_active = False
        page.deactivated_at = now
        page.deactivation_reason = 'manual'
        page.updated_at = now
        page.save()

        logger.info(f"✅ Soft disconnected Facebook page: {page_name} (ID: {page_id})")
        logger.info(f"   - Messages preserved for audit trail")
        logger.info(f"   - Webhooks will now return 404 for this page")

        return Response({
            'status': 'disconnected',
            'page_id': page_id,
            'page_name': page_name,
            'message': f'Deactivated Facebook page "{page_name}". Messages preserved. Webhooks will be rejected.'
        })

    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to disconnect Facebook page {page_id}: {e}")
        return Response({
            'error': f'Failed to disconnect page: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def clear_platform_history(request):
    """
    Permanently delete all messages for a given platform.
    Accepts { platform: 'facebook' | 'instagram' | 'whatsapp' }
    """
    logger = logging.getLogger(__name__)
    platform = request.data.get('platform', '').lower()

    if platform not in ('facebook', 'instagram', 'whatsapp'):
        return Response({
            'error': 'platform must be one of: facebook, instagram, whatsapp'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        deleted_count = 0

        if platform == 'facebook':
            pages = FacebookPageConnection.objects.all()
            for page in pages:
                count, _ = FacebookMessage.objects.filter(page_connection=page).delete()
                deleted_count += count

        elif platform == 'instagram':
            accounts = InstagramAccountConnection.objects.all()
            for account in accounts:
                count, _ = InstagramMessage.objects.filter(account_connection=account).delete()
                deleted_count += count

        elif platform == 'whatsapp':
            accounts = WhatsAppBusinessAccount.objects.all()
            for account in accounts:
                count, _ = WhatsAppMessage.objects.filter(business_account=account).delete()
                deleted_count += count

        # Also remove archive records for this platform
        archive_count, _ = ConversationArchive.objects.filter(platform=platform).delete()

        logger.info(f"✅ Cleared {platform} history: {deleted_count} messages deleted, {archive_count} archives removed")

        # PR F — bulk-variant `conversation_deleted` broadcast.
        # conversation_id=None tells beta clients "every row on this
        # platform is gone" so they can do one pass instead of N.
        try:
            from django.db import connection
            from .consumers import send_conversation_deleted
            async_to_sync(send_conversation_deleted)(
                connection.schema_name,
                platform=platform,
                conversation_id=None,
                account_id=None,
                by_user_id=request.user.id,
            )
        except Exception as exc:  # noqa: BLE001 — broadcast is best-effort.
            logger.warning('conversation_deleted (bulk) broadcast failed: %s', exc)

        return Response({
            'status': 'cleared',
            'platform': platform,
            'messages_deleted': deleted_count,
            'archives_removed': archive_count,
            'message': f'Permanently deleted {deleted_count} {platform} messages'
        })

    except Exception as e:
        logger.error(f"Failed to clear {platform} history: {e}")
        return Response({
            'error': f'Failed to clear history: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def check_messaging_window(request):
    """
    Check if the 24-hour messaging window is open for a conversation.
    Query params: platform, conversation_id, account_id
    Returns: { window_open: bool, hours_remaining: float | null }
    """
    platform = request.query_params.get('platform', '').lower()
    conversation_id = request.query_params.get('conversation_id', '')
    account_id = request.query_params.get('account_id', '')

    if not all([platform, conversation_id, account_id]):
        return Response({
            'error': 'platform, conversation_id, and account_id are required'
        }, status=status.HTTP_400_BAD_REQUEST)

    if platform not in ('facebook', 'instagram', 'whatsapp'):
        return Response({
            'error': 'platform must be one of: facebook, instagram, whatsapp'
        }, status=status.HTTP_400_BAD_REQUEST)

    last_user_message = None

    if platform == 'facebook':
        last_user_message = FacebookMessage.objects.filter(
            page_connection__page_id=account_id,
            sender_id=conversation_id,
            is_from_page=False
        ).order_by('-timestamp').first()

    elif platform == 'instagram':
        last_user_message = InstagramMessage.objects.filter(
            account_connection__instagram_account_id=account_id,
            sender_id=conversation_id,
            is_from_business=False
        ).order_by('-timestamp').first()

    elif platform == 'whatsapp':
        last_user_message = WhatsAppMessage.objects.filter(
            business_account__waba_id=account_id,
            from_number=conversation_id,
            is_from_business=False
        ).order_by('-timestamp').first()

    if not last_user_message:
        return Response({
            'window_open': False,
            'hours_remaining': None,
            'message': 'No messages from user found'
        })

    now = timezone.now()
    time_since = now - last_user_message.timestamp
    hours_passed = time_since.total_seconds() / 3600
    window_open = hours_passed <= 24
    hours_remaining = max(0, round(24 - hours_passed, 1)) if window_open else 0

    return Response({
        'window_open': window_open,
        'hours_remaining': hours_remaining,
        'last_user_message_at': last_user_message.timestamp.isoformat(),
        'hours_passed': round(hours_passed, 1)
    })


@extend_schema(
    request=FacebookSendMessageSerializer,
    responses={
        200: {
            'type': 'object',
            'properties': {
                'success': {'type': 'boolean'},
                'message': {'type': 'string'},
                'facebook_message_id': {'type': 'string'}
            }
        },
        400: {
            'type': 'object',
            'properties': {
                'error': {'type': 'string'},
                'details': {'type': 'object'}
            }
        },
        404: {
            'type': 'object',
            'properties': {
                'error': {'type': 'string'}
            }
        }
    },
    description="Send a message to a Facebook user",
    summary="Send Facebook Message"
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanSendSocialMessages])
def facebook_send_message(request):
    """Send a message (text and/or media) to a Facebook user"""
    from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
    request.parsers = [MultiPartParser(), FormParser(), JSONParser()]

    try:
        # Validate input data using serializer
        serializer = FacebookSendMessageSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'error': 'Invalid data',
                'details': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        validated_data = serializer.validated_data
        recipient_id = validated_data['recipient_id']
        message_text = validated_data.get('message', '')
        page_id = validated_data['page_id']
        reply_to_message_id = validated_data.get('reply_to_message_id', '').strip()
        media_file = request.FILES.get('media')

        # Validate: must have text or media
        if not message_text and not media_file:
            return Response({'error': 'Message text or media file is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Get the page connection for this tenant
        try:
            page_connection = FacebookPageConnection.objects.get(
                page_id=page_id,
                is_active=True
            )
        except FacebookPageConnection.DoesNotExist:
            return Response({
                'error': 'Page not found or not connected to this tenant'
            }, status=status.HTTP_404_NOT_FOUND)

        # Verify a conversation exists (user must have messaged first)
        last_user_message = FacebookMessage.objects.filter(
            page_connection=page_connection,
            sender_id=recipient_id,
            is_from_page=False
        ).order_by('-timestamp').first()

        if not last_user_message:
            return Response({
                'error': 'Cannot send message: No conversation found with this user. The user must message you first.',
                'error_code': 'no_conversation',
                'details': 'Facebook requires the user to initiate the conversation before you can send messages.'
            }, status=status.HTTP_400_BAD_REQUEST)
        # Note: 24-hour window enforcement is handled by Facebook's API.
        # If the window is expired, Facebook will return an error which we surface to the user.

        send_url = f"https://graph.facebook.com/v23.0/me/messages"
        params = {'access_token': page_connection.page_access_token}
        attachments_meta = []
        # When the agent sends text + attachment in one composer submit,
        # Facebook's Send API requires two separate calls — one for the text,
        # one for the attachment — and each gets its own `mid`. We need to
        # remember the TEXT call's mid here so the local save below can store
        # the text as its own row keyed by that mid. Without it we'd save a
        # single row carrying both text + attachment, and the text echo
        # webhook (with a different mid) would then create a *second* row
        # with just the text — the duplicate the user is seeing in the UI.
        text_message_id: str | None = None
        text_sent_separately = False

        if media_file:
            # Upload media as attachment via Facebook Send API
            media_mime_type = media_file.content_type or 'application/octet-stream'
            media_filename = media_file.name or 'file'

            # Determine attachment type
            if media_mime_type.startswith('image/'):
                attachment_type = 'image'
            elif media_mime_type.startswith('video/'):
                attachment_type = 'video'
            elif media_mime_type.startswith('audio/'):
                attachment_type = 'audio'
            else:
                attachment_type = 'file'

            # If there's text AND media, send text first then media. Capture
            # the text-message id so we can store text + media as two distinct
            # rows — matching how Facebook itself models them — and so the
            # echo webhook dedupes against an existing row instead of inserting
            # a third one.
            if message_text:
                text_payload = {
                    'recipient': {'id': recipient_id},
                    'message': {'text': message_text},
                    'messaging_type': 'RESPONSE'
                }
                if reply_to_message_id:
                    text_payload['reply_to'] = {'mid': reply_to_message_id}
                text_response = requests.post(
                    send_url,
                    json=text_payload,
                    headers={'Content-Type': 'application/json'},
                    params=params, timeout=30,
                )
                text_sent_separately = True
                if text_response.status_code == 200:
                    try:
                        text_message_id = text_response.json().get('message_id')
                    except ValueError:
                        text_message_id = None

            # Send media attachment using form data upload
            message_payload = {
                'recipient': '{"id":"' + recipient_id + '"}',
                'message': '{"attachment":{"type":"' + attachment_type + '","payload":{"is_reusable":true}}}',
                'messaging_type': 'RESPONSE',
                'filedata': (media_filename, media_file, media_mime_type),
            }

            response = requests.post(
                send_url,
                files={'filedata': (media_filename, media_file, media_mime_type)},
                data={
                    'recipient': '{"id":"' + recipient_id + '"}',
                    'message': '{"attachment":{"type":"' + attachment_type + '","payload":{"is_reusable":true}}}',
                    'messaging_type': 'RESPONSE',
                },
                params=params
            , timeout=30)

            attachments_meta = [{
                'mime_type': media_mime_type,
                'filename': media_filename,
                'type': attachment_type,
            }]
        else:
            # Text-only message
            message_data = {
                'recipient': {'id': recipient_id},
                'message': {'text': message_text},
                'messaging_type': 'RESPONSE'
            }
            if reply_to_message_id:
                message_data['reply_to'] = {'mid': reply_to_message_id}
                logger.info(f"Replying to message: {reply_to_message_id}")

            response = requests.post(
                send_url,
                json=message_data,
                headers={'Content-Type': 'application/json'},
                params=params
            , timeout=30)

        logger.info(f"Sending message to {recipient_id} via {page_connection.page_name}")

        if response.status_code == 200:
            response_data = response.json()
            message_id = response_data.get('message_id')

            # Save the sent message to our database
            try:
                from .consumers import send_new_message_notification
                from django.db import connection

                # Look up the original message if this is a reply
                reply_to_obj = None
                if reply_to_message_id:
                    reply_to_obj = FacebookMessage.objects.filter(
                        message_id=reply_to_message_id
                    ).first()

                timestamp = datetime.now()
                tenant_schema = connection.schema_name

                # Look up recipient's name from previous incoming messages in this conversation
                recipient_name = None
                previous_msg = FacebookMessage.objects.filter(
                    page_connection=page_connection,
                    sender_id=recipient_id,
                    is_from_page=False
                ).order_by('-timestamp').first()
                if previous_msg and previous_msg.sender_name:
                    recipient_name = previous_msg.sender_name

                # Facebook stores text + attachment as TWO distinct messages
                # when sent in one composer submit. Mirror that locally:
                #   • If text was sent separately, persist a text-only row.
                #   • Persist a row for the attachment that holds ONLY the
                #     attachment (no message_text) so the UI doesn't render
                #     the text twice when the text-echo webhook lands.
                # If only one of them was sent, persist a single row carrying
                # whatever was sent.
                sent_rows: list[FacebookMessage] = []

                if text_sent_separately:
                    text_row_kwargs = dict(
                        page_connection=page_connection,
                        message_id=text_message_id or f"sent_text_{timestamp.timestamp()}",
                        sender_id=recipient_id,
                        sender_name=page_connection.page_name,
                        message_text=message_text,
                        timestamp=timestamp,
                        is_from_page=True,
                        reply_to_message_id=reply_to_message_id if reply_to_message_id else None,
                        reply_to=reply_to_obj,
                        source='echodesk',
                        is_echo=False,
                        sent_by=request.user,
                    )
                    try:
                        sent_rows.append(FacebookMessage.objects.create(**text_row_kwargs))
                    except IntegrityError:
                        # Echo beat us to it — pick up the existing row so we
                        # still broadcast a WS frame for it.
                        existing = FacebookMessage.objects.filter(
                            page_connection=page_connection,
                            message_id=text_row_kwargs['message_id'],
                        ).first()
                        if existing:
                            sent_rows.append(existing)

                # The "primary" row from this request's perspective. For
                # media+text, this carries the attachment only. For text-only
                # / media-only, it carries whatever was sent.
                primary_kwargs = dict(
                    page_connection=page_connection,
                    message_id=message_id or f"sent_{timestamp.timestamp()}",
                    sender_id=recipient_id,
                    sender_name=page_connection.page_name,
                    message_text='' if text_sent_separately else message_text,
                    timestamp=timestamp,
                    is_from_page=True,
                    reply_to_message_id=reply_to_message_id if reply_to_message_id else None,
                    reply_to=reply_to_obj,
                    source='echodesk',
                    is_echo=False,
                    sent_by=request.user,
                )
                if attachments_meta:
                    primary_kwargs['attachments'] = attachments_meta

                try:
                    fb_message = FacebookMessage.objects.create(**primary_kwargs)
                    sent_rows.append(fb_message)
                except IntegrityError:
                    existing = FacebookMessage.objects.filter(
                        page_connection=page_connection,
                        message_id=primary_kwargs['message_id'],
                    ).first()
                    if existing:
                        fb_message = existing
                        sent_rows.append(existing)
                    else:
                        raise

                # Unarchive conversation if it was archived (move back to active when sending a message)
                unarchived = ConversationArchive.objects.filter(
                    platform='facebook',
                    conversation_id=recipient_id,
                    account_id=page_id
                ).delete()[0]
                if unarchived:
                    logger.info(f"📤 Unarchived conversation due to sent message: facebook/{page_id}/{recipient_id}")

                # Delete completed assignments to allow new sessions
                deleted_assignment = ChatAssignment.objects.filter(
                    platform='facebook',
                    conversation_id=recipient_id,
                    account_id=page_id,
                    status='completed'
                ).delete()[0]
                if deleted_assignment:
                    logger.info(f"🔄 Deleted completed assignment for new conversation: facebook/{page_id}/{recipient_id}")

                # Broadcast one WS frame per row so the UI renders the same
                # bubbles the customer's Messenger app shows.
                for row in sent_rows:
                    ws_message_data = {
                        'id': row.id,
                        'message_id': row.message_id,
                        'platform': 'facebook',
                        'sender_id': page_id,
                        'sender_name': page_connection.page_name,
                        'recipient_id': recipient_id,
                        'recipient_name': recipient_name,
                        'message_text': row.message_text or '',
                        'attachments': row.attachments or [],
                        'timestamp': row.timestamp.isoformat() if row.timestamp else timestamp.isoformat(),
                        'is_from_page': True,
                        'page_id': page_id,
                        'sent_by': request.user.email if request.user else None,
                        'reply_to_message_id': row.reply_to_message_id,
                        'reply_to_id': row.reply_to_id if row.reply_to_id else None,
                    }
                    async_to_sync(send_new_message_notification)(tenant_schema, recipient_id, ws_message_data)
            except Exception as e:
                logger.warning(f"Failed to save/broadcast sent message: {e}")

            return Response({
                'status': 'sent',
                'message_id': message_id,
                'recipient_id': recipient_id
            })
        else:
            error_data = response.json() if response.content else {}
            error_info = error_data.get('error', {})
            error_message = error_info.get('message', 'Unknown error')
            error_code = error_info.get('code')
            error_type = error_info.get('type', '')

            logger.warning(
                f"⚠️ Facebook API error for page '{page_connection.page_name}': "
                f"code={error_code}, type={error_type}, message={error_message}"
            )

            return Response({
                'error': f'Failed to send message: {error_message}',
                'facebook_error': error_data
            }, status=status.HTTP_400_BAD_REQUEST)

    except Exception as e:
        logger.error(f"Exception in facebook_send_message: {e}")
        return Response({
            'error': f'Failed to send message: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def facebook_webhook(request):
    """Handle Facebook webhook events for page messages"""
    if request.method == 'GET':
        # Webhook verification - Facebook sends these parameters
        mode = request.GET.get('hub.mode')
        token = request.GET.get('hub.verify_token')
        challenge = request.GET.get('hub.challenge')
        
        # Verify token from settings
        verify_token = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_VERIFY_TOKEN', '')
        
        # Verify the mode and token
        if mode == 'subscribe' and verify_token and token == verify_token:
            # Return the challenge as plain text (not JSON)
            return HttpResponse(challenge, content_type='text/plain')
        else:
            return JsonResponse({
                'error': 'Invalid verify token or mode'
            }, status=403)
    
    elif request.method == 'POST':
        # Handle webhook events
        try:
            import json
            import logging
            from datetime import datetime
            from tenant_schemas.utils import schema_context
            logger = logging.getLogger(__name__)
            
            # Parse webhook data
            data = json.loads(request.body)

            # Log webhook data for debugging
            logger.info(f"📩 FACEBOOK WEBHOOK RECEIVED:")
            logger.info(f"   Raw data: {json.dumps(data, indent=2)}")
            logger.info(f"   Headers: User-Agent={request.headers.get('User-Agent')}, X-Hub-Signature={request.headers.get('X-Hub-Signature')}")
            
            # Extract page_id to determine which tenant to use
            page_id = None

            # Handle Facebook Developer Console test format
            if 'field' in data and 'value' in data and data['field'] == 'messages':
                test_value = data['value']
                page_id = (test_value.get('metadata', {}).get('page_id') or
                          test_value.get('page_id') or
                          test_value.get('recipient', {}).get('id'))

            # Handle standard webhook format (real messages)
            elif 'entry' in data and len(data['entry']) > 0:
                page_id = data['entry'][0].get('id')

            if not page_id:
                logger.error("No page_id found in webhook data")
                return JsonResponse({'error': 'No page_id found'}, status=400)

            # Find which tenant this page belongs to
            tenant_schema = find_tenant_by_page_id(page_id)

            if not tenant_schema:
                logger.debug(f"Ignoring webhook for unlinked Facebook page: {page_id}")
                return JsonResponse({'status': 'received'})

            logger.info(f"Processing webhook for page_id {page_id} in tenant: {tenant_schema}")
            
            # Process webhook within the correct tenant context
            with schema_context(tenant_schema):
                # Handle Facebook Developer Console test format
                if 'field' in data and 'value' in data and data['field'] == 'messages':
                    # This is a test message from Facebook Developer Console
                    test_value = data['value']
                    
                    # Extract data from test format
                    sender_id = test_value.get('sender', {}).get('id', 'test_sender')
                    message_data = test_value.get('message', {})
                    timestamp = test_value.get('timestamp', 0)
                    
                    logger.info(f"Processing test message from sender {sender_id}")
                    
                    if page_id and sender_id and message_data:
                        # Find the page connection
                        try:
                            page_connection = FacebookPageConnection.objects.get(
                                page_id=page_id, 
                                is_active=True
                            )
                            logger.info(f"Found page connection: {page_connection}")
                            
                            # Process the test message
                            message_id = message_data.get('mid', f'test_mid_{timestamp}')
                            message_text = message_data.get('text', 'Test message from Facebook')

                            # Skip if this is an echo (message sent by the page)
                            if message_data.get('is_echo'):
                                return JsonResponse({'status': 'received'})

                            # For test messages, use simple sender info
                            sender_name = f"Test User {sender_id}"
                            profile_pic_url = None

                            # Save the message (avoid duplicates)
                            if message_id and not FacebookMessage.objects.filter(message_id=message_id).exists():
                                try:
                                    message_obj = FacebookMessage.objects.create(
                                        page_connection=page_connection,
                                        message_id=message_id,
                                        sender_id=sender_id,
                                        sender_name=sender_name,
                                        message_text=message_text,
                                        timestamp=convert_facebook_timestamp(int(timestamp) if timestamp else 0),
                                        is_from_page=(sender_id == page_id),
                                        profile_pic_url=profile_pic_url,
                                        is_echo=False,
                                    )
                                    logger.info(f"✅ Saved test message: {message_text}")

                                    # Send WebSocket notification for real-time updates
                                    from django.db import connection
                                    current_schema = getattr(connection, 'schema_name', None)
                                    if current_schema:
                                        ws_message_data = {
                                            'id': message_obj.id,
                                            'message_id': message_obj.message_id,
                                            'sender_id': message_obj.sender_id,
                                            'sender_name': message_obj.sender_name,
                                            'message_text': message_obj.message_text,
                                            'timestamp': message_obj.timestamp.isoformat() if message_obj.timestamp else None,
                                            'is_from_page': message_obj.is_from_page,
                                        }
                                        # Conversation ID is the sender_id (the customer)
                                        ws_conversation_id = sender_id
                                        send_websocket_notification(current_schema, ws_message_data, ws_conversation_id)

                                except Exception as e:
                                    logger.error(f"Failed to save test message: {e}")

                        except FacebookPageConnection.DoesNotExist:
                            logger.warning(f"No active page connection found for page_id: {page_id}")
                            return JsonResponse({'error': f'No page connection found for page_id: {page_id}'}, status=404)
                    
                    return JsonResponse({'status': 'received'})
                
                # Handle standard webhook format (real messages)
                elif 'entry' in data:
                    logger.info(f"🔄 Processing STANDARD webhook format with {len(data['entry'])} entries")
                    
                    for entry in data['entry']:
                        page_id = entry.get('id')
                        logger.info(f"🔍 Processing entry for page_id: {page_id}")
                        
                        # Find the page connection
                        try:
                            page_connection = FacebookPageConnection.objects.get(
                                page_id=page_id, 
                                is_active=True
                            )
                            logger.info(f"✅ Found page connection: {page_connection.page_name}")
                        except FacebookPageConnection.DoesNotExist:
                            # Log for debugging but continue processing other entries
                            logger.warning(f"❌ No active page connection found for page_id: {page_id}")
                            # List available connections for debugging
                            all_connections = FacebookPageConnection.objects.filter(is_active=True)
                            logger.warning(f"Available connections: {[(conn.page_id, conn.page_name) for conn in all_connections]}")
                            continue
                        
                        # Process messaging events
                        if 'messaging' in entry:
                            logger.info(f"📨 Found {len(entry['messaging'])} messaging events")
                            for message_event in entry['messaging']:
                                
                                if 'message' in message_event:
                                    message_data = message_event['message']
                                    sender_id = message_event['sender']['id']
                                    
                                    # Handle echo messages (messages sent by the page)
                                    if message_data.get('is_echo'):
                                        logger.info("📤 Processing echo message (outgoing from page)")
                                        message_id = message_data.get('mid')
                                        recipient_id = message_event.get('recipient', {}).get('id', '')

                                        if message_id:
                                            try:
                                                fb_timestamp = message_event.get('timestamp')
                                                timestamp_dt = convert_facebook_timestamp(fb_timestamp) if fb_timestamp else timezone.now()

                                                # Check if message already exists
                                                existing = FacebookMessage.objects.filter(
                                                    page_connection=page_connection,
                                                    message_id=message_id
                                                ).first()

                                                if existing:
                                                    # Update timestamp and attachment info from echo
                                                    update_fields = ['timestamp']
                                                    existing.timestamp = timestamp_dt

                                                    # Echo includes attachment URLs that we don't have from send API
                                                    raw_attachments = message_data.get('attachments', [])
                                                    if raw_attachments:
                                                        echo_attachments = []
                                                        echo_att_type = ''
                                                        echo_att_url = None
                                                        for att in raw_attachments:
                                                            att_type = att.get('type', 'file')
                                                            att_url = att.get('payload', {}).get('url')
                                                            echo_attachments.append({
                                                                'type': att_type,
                                                                'url': att_url,
                                                            })
                                                            if not echo_att_type:
                                                                echo_att_type = att_type
                                                                echo_att_url = att_url
                                                        if echo_attachments:
                                                            existing.attachments = echo_attachments
                                                            update_fields.append('attachments')
                                                        if echo_att_type:
                                                            existing.attachment_type = echo_att_type
                                                            update_fields.append('attachment_type')
                                                        if echo_att_url:
                                                            existing.attachment_url = echo_att_url
                                                            update_fields.append('attachment_url')

                                                    existing.save(update_fields=update_fields)
                                                    logger.info(f"✅ Updated echo message: timestamp={timestamp_dt}, attachments={'yes' if 'attachments' in update_fields else 'no'}")
                                                else:
                                                    # Create new message for echo (sent from Facebook directly)
                                                    message_text = message_data.get('text', '')

                                                    # Handle attachments
                                                    attachments = []
                                                    attachment_type = ''
                                                    attachment_url = None
                                                    raw_attachments = message_data.get('attachments', [])

                                                    for att in raw_attachments:
                                                        att_type = att.get('type', 'file')
                                                        att_url = att.get('payload', {}).get('url')
                                                        attachments.append({
                                                            'type': att_type,
                                                            'url': att_url,
                                                        })
                                                        if not attachment_type:
                                                            attachment_type = att_type
                                                            attachment_url = att_url

                                                    try:
                                                        echo_message = FacebookMessage.objects.create(
                                                            page_connection=page_connection,
                                                            message_id=message_id,
                                                            sender_id=recipient_id,  # Use recipient_id for conversation grouping (same as direct send API)
                                                            sender_name=page_connection.page_name,
                                                            message_text=message_text,
                                                            attachment_type=attachment_type,
                                                            attachment_url=attachment_url,
                                                            attachments=attachments,
                                                            timestamp=timestamp_dt,
                                                            is_from_page=True,
                                                            is_delivered=True,
                                                            source='facebook_app',  # Message sent from Facebook/Messenger app
                                                            is_echo=True,
                                                            sent_by=None,  # Not sent via EchoDesk
                                                        )
                                                        logger.info(f"✅ Created echo message from Facebook: {message_id} (recipient: {recipient_id})")
                                                    except IntegrityError:
                                                        # Race condition: duplicate webhook delivery — another request created
                                                        # the same message_id between our filter check and this create.
                                                        # Silently skip — the other request will handle downstream logic.
                                                        echo_message = FacebookMessage.objects.filter(
                                                            page_connection=page_connection,
                                                            message_id=message_id
                                                        ).first()
                                                        logger.warning(f"⚠️ Echo message already exists (race condition), skipping: {message_id}")
                                                        continue

                                                    # Unarchive conversation if it was archived (move back to active)
                                                    unarchived = ConversationArchive.objects.filter(
                                                        platform='facebook',
                                                        conversation_id=recipient_id,
                                                        account_id=page_id
                                                    ).delete()[0]
                                                    if unarchived:
                                                        logger.info(f"📤 Unarchived conversation due to echo message: facebook/{page_id}/{recipient_id}")

                                                    # Delete completed assignments to allow new sessions
                                                    deleted_assignment = ChatAssignment.objects.filter(
                                                        platform='facebook',
                                                        conversation_id=recipient_id,
                                                        account_id=page_id,
                                                        status='completed'
                                                    ).delete()[0]
                                                    if deleted_assignment:
                                                        logger.info(f"🔄 Deleted completed assignment for new conversation: facebook/{page_id}/{recipient_id}")

                                                    # Look up recipient's name from previous incoming messages
                                                    recipient_name = None
                                                    recipient_profile_pic = None
                                                    previous_msg = FacebookMessage.objects.filter(
                                                        page_connection=page_connection,
                                                        sender_id=recipient_id,
                                                        is_from_page=False
                                                    ).order_by('-timestamp').first()
                                                    if previous_msg:
                                                        if previous_msg.sender_name:
                                                            recipient_name = previous_msg.sender_name
                                                        if previous_msg.profile_pic_url:
                                                            recipient_profile_pic = previous_msg.profile_pic_url

                                                    # If no previous message, try to fetch profile from Facebook API
                                                    if not recipient_name:
                                                        try:
                                                            profile_url = f"https://graph.facebook.com/{recipient_id}"
                                                            profile_params = {
                                                                'fields': 'name,first_name,last_name,profile_pic',
                                                                'access_token': page_connection.page_access_token
                                                            }
                                                            profile_response = requests.get(profile_url, params=profile_params, timeout=10)
                                                            if profile_response.status_code == 200:
                                                                profile_data = profile_response.json()
                                                                recipient_name = profile_data.get('name', '')
                                                                if not recipient_name:
                                                                    first = profile_data.get('first_name', '')
                                                                    last = profile_data.get('last_name', '')
                                                                    recipient_name = f"{first} {last}".strip()
                                                                recipient_profile_pic = profile_data.get('profile_pic')
                                                                logger.info(f"👤 Fetched profile for echo recipient {recipient_id}: {recipient_name}")
                                                        except Exception as profile_err:
                                                            logger.warning(f"⚠️ Could not fetch profile for echo recipient {recipient_id}: {profile_err}")

                                                    # Create or update SocialAccount for the recipient (auto-create client if needed)
                                                    try:
                                                        social_account = SocialAccount.objects.filter(
                                                            platform='facebook',
                                                            platform_id=recipient_id,
                                                            account_connection_id=page_id
                                                        ).first()

                                                        if not social_account:
                                                            # Create a new client and social account for this Facebook user
                                                            display_name = recipient_name or f'Facebook User {recipient_id}'
                                                            client = SocialClient.objects.create(
                                                                name=display_name,
                                                                profile_picture=recipient_profile_pic,
                                                            )
                                                            social_account = SocialAccount.objects.create(
                                                                client=client,
                                                                platform='facebook',
                                                                platform_id=recipient_id,
                                                                account_connection_id=page_id,
                                                                display_name=display_name,
                                                                profile_pic_url=recipient_profile_pic,
                                                                is_auto_created=True,
                                                            )
                                                            logger.info(f"✅ Auto-created client and social account for echo message recipient: {recipient_id} ({display_name})")
                                                        else:
                                                            # Update last_message_at and name if we have better info
                                                            update_fields = ['last_message_at']
                                                            social_account.last_message_at = timestamp_dt
                                                            if recipient_name and (not social_account.display_name or social_account.display_name.startswith('Facebook User')):
                                                                social_account.display_name = recipient_name
                                                                update_fields.append('display_name')
                                                                # Also update client name
                                                                if social_account.client:
                                                                    social_account.client.name = recipient_name
                                                                    social_account.client.save(update_fields=['name'])
                                                            if recipient_profile_pic and not social_account.profile_pic_url:
                                                                social_account.profile_pic_url = recipient_profile_pic
                                                                update_fields.append('profile_pic_url')
                                                            social_account.save(update_fields=update_fields)
                                                    except Exception as client_err:
                                                        logger.warning(f"⚠️ Could not create/update social account for echo recipient: {client_err}")

                                                    # Send WebSocket notification
                                                    ws_data = {
                                                        'id': echo_message.id,
                                                        'message_id': echo_message.message_id,
                                                        'platform': 'facebook',
                                                        'sender_id': page_id,  # The page is the sender when is_from_page=True
                                                        'sender_name': echo_message.sender_name,
                                                        'recipient_id': recipient_id,  # The user we're messaging
                                                        'recipient_name': recipient_name,  # The user's name (for frontend sidebar)
                                                        'message_text': echo_message.message_text,
                                                        'attachment_type': echo_message.attachment_type,
                                                        'attachment_url': echo_message.attachment_url,
                                                        'timestamp': echo_message.timestamp.isoformat(),
                                                        'is_from_page': True,
                                                        'page_id': page_id,
                                                    }
                                                    send_websocket_notification(tenant_schema, ws_data, recipient_id)

                                            except Exception as e:
                                                logger.error(f"❌ Failed to process echo message: {e}")

                                        continue
                                    
                                    # Get sender information
                                    sender_name = 'Messenger User'  # Fallback name
                                    profile_pic_url = None
                                    customer_email = None
                                    customer_phone = None

                                    # Get message_id for API calls
                                    message_id = message_data.get('mid', '')

                                    if sender_id != page_id:  # Don't fetch profile for page itself
                                        # First try: Extract customer information from webhook
                                        customer_info = extract_customer_information(message_event)

                                        if customer_info:
                                            # Use customer information from webhook (most reliable)
                                            sender_name = customer_info.get('name', '').strip() or 'Messenger User'
                                            customer_email = customer_info.get('email')
                                            customer_phone = customer_info.get('phone')
                                            logger.info(f"👤 Using customer info from webhook: {sender_name}")
                                            if customer_email:
                                                logger.info(f"   Email: {customer_email}")
                                            if customer_phone:
                                                logger.info(f"   Phone: {customer_phone}")
                                        elif message_id:
                                            # Second try: Fetch sender info from message object (works in Live Mode!)
                                            try:
                                                message_url = f"https://graph.facebook.com/v23.0/{message_id}"
                                                message_params = {
                                                    'fields': 'from',
                                                    'access_token': page_connection.page_access_token
                                                }
                                                message_response = requests.get(message_url, params=message_params, timeout=10)

                                                if message_response.status_code == 200:
                                                    message_api_data = message_response.json()
                                                    from_data = message_api_data.get('from', {})

                                                    sender_name = from_data.get('name', '').strip() or 'Messenger User'
                                                    # Note: from_data.get('email') is usually a fake email like "psid@facebook.com"

                                                    logger.info(f"👤 Fetched sender name from message object: {sender_name}")
                                                else:
                                                    logger.warning(f"Could not fetch message info: status={message_response.status_code}")
                                            except Exception as e:
                                                logger.warning(f"Exception fetching message info: {e}")

                                        # Fetch profile picture for the sender
                                        try:
                                            profile_pic_url_api = f"https://graph.facebook.com/v23.0/{sender_id}/picture"
                                            profile_pic_params = {
                                                'type': 'large',
                                                'redirect': 'false',
                                                'access_token': page_connection.page_access_token
                                            }
                                            profile_pic_response = requests.get(profile_pic_url_api, params=profile_pic_params, timeout=10)
                                            if profile_pic_response.status_code == 200:
                                                pic_data = profile_pic_response.json().get('data', {})
                                                if not pic_data.get('is_silhouette', True):
                                                    profile_pic_url = pic_data.get('url')
                                                    logger.info(f"👤 Fetched profile picture for {sender_name}")
                                        except Exception as e:
                                            logger.warning(f"Exception fetching profile picture: {e}")

                                    # Save the message (avoid duplicates)
                                    message_id = message_data.get('mid', '')
                                    message_text = message_data.get('text', '')

                                    # Extract attachment information
                                    attachment_type = ''
                                    attachment_url = None
                                    attachments = []

                                    if 'attachments' in message_data:
                                        raw_attachments = message_data.get('attachments', [])
                                        for att in raw_attachments:
                                            att_type = att.get('type', '')
                                            payload = att.get('payload', {})
                                            att_url = payload.get('url', '')

                                            # Build attachment object for storage
                                            attachment_obj = {
                                                'type': att_type,
                                                'url': att_url,
                                            }

                                            # Add sticker_id if present (for stickers)
                                            if payload.get('sticker_id'):
                                                attachment_obj['sticker_id'] = payload.get('sticker_id')

                                            attachments.append(attachment_obj)

                                            # Set primary attachment type and URL (first attachment)
                                            if not attachment_type:
                                                attachment_type = att_type
                                                attachment_url = att_url

                                        logger.info(f"📎 Found {len(attachments)} attachment(s): {[a['type'] for a in attachments]}")

                                    # Extract reply_to information (for message replies)
                                    reply_to_message_id = None
                                    reply_to_obj = None
                                    if 'reply_to' in message_data:
                                        reply_to_data = message_data.get('reply_to', {})
                                        reply_to_message_id = reply_to_data.get('mid')
                                        if reply_to_message_id:
                                            # Try to find the original message
                                            reply_to_obj = FacebookMessage.objects.filter(
                                                message_id=reply_to_message_id
                                            ).first()
                                            logger.info(f"↩️ Message is a reply to: {reply_to_message_id}")

                                    if message_id and not FacebookMessage.objects.filter(message_id=message_id).exists():
                                        try:
                                            message_obj = FacebookMessage.objects.create(
                                                page_connection=page_connection,
                                                message_id=message_id,
                                                sender_id=sender_id,
                                                sender_name=sender_name,
                                                message_text=message_text,
                                                attachment_type=attachment_type,
                                                attachment_url=attachment_url,
                                                attachments=attachments,
                                                timestamp=convert_facebook_timestamp(message_event.get('timestamp', 0)),
                                                is_from_page=(sender_id == page_id),
                                                profile_pic_url=profile_pic_url,
                                                reply_to_message_id=reply_to_message_id,
                                                reply_to=reply_to_obj,
                                                is_echo=False,
                                            )
                                            logger.info(f"✅ Saved message from {sender_name}: {message_text[:50] if message_text else f'[{attachment_type}]'}")

                                            # Check if this is a rating response (only for customer messages)
                                            if sender_id != page_id:
                                                process_potential_rating_response(
                                                    message_text=message_text,
                                                    platform='facebook',
                                                    conversation_id=sender_id,
                                                    account_id=page_id,
                                                    message_id=message_id
                                                )

                                                # Auto-unarchive if conversation was in history
                                                auto_unarchive_conversation(
                                                    platform='facebook',
                                                    conversation_id=sender_id,
                                                    account_id=page_id
                                                )

                                                # Process auto-reply (doesn't mark as read)
                                                process_auto_reply(
                                                    platform='facebook',
                                                    account_id=page_id,
                                                    conversation_id=sender_id,
                                                    sender_name=sender_name,
                                                    connection=page_connection
                                                )

                                            # Send WebSocket notification for real-time updates
                                            from django.db import connection
                                            current_schema = getattr(connection, 'schema_name', None)
                                            if current_schema:
                                                ws_message_data = {
                                                    'id': message_obj.id,
                                                    'message_id': message_obj.message_id,
                                                    'sender_id': message_obj.sender_id,
                                                    'sender_name': message_obj.sender_name,
                                                    'message_text': message_obj.message_text,
                                                    'attachment_type': message_obj.attachment_type,
                                                    'attachment_url': message_obj.attachment_url,
                                                    'attachments': message_obj.attachments,
                                                    'timestamp': message_obj.timestamp.isoformat() if message_obj.timestamp else None,
                                                    'is_from_page': message_obj.is_from_page,
                                                    'reply_to_message_id': message_obj.reply_to_message_id,
                                                    'reply_to_id': message_obj.reply_to_id if message_obj.reply_to else None,
                                                    'platform': 'facebook',
                                                    'account_id': page_id,
                                                    'chat_id': f'fb_{page_id}_{sender_id}',
                                                }
                                                # Conversation ID is the sender_id (the customer)
                                                ws_conversation_id = sender_id
                                                # Get assignment for notification filtering
                                                assigned_user_id = get_assignment_for_conversation(
                                                    platform='facebook',
                                                    conversation_id=sender_id,
                                                    account_id=page_id
                                                )
                                                send_websocket_notification(current_schema, ws_message_data, ws_conversation_id, assigned_user_id)

                                                # Create in-app notification for incoming customer messages
                                                if sender_id != page_id:
                                                    try:
                                                        from users.notification_utils import create_social_message_notification
                                                        create_social_message_notification(
                                                            platform='Facebook',
                                                            sender_name=sender_name,
                                                            message_text=message_text,
                                                            conversation_id=sender_id,
                                                            sender_id=sender_id,
                                                            account_id=page_id,
                                                            assigned_user_id=assigned_user_id,
                                                        )
                                                    except Exception as notif_err:
                                                        logger.error(f"Failed to create Facebook message notification: {notif_err}")

                                        except Exception as e:
                                            logger.error(f"❌ Failed to save message: {e}")
                                    else:
                                        if not message_id:
                                            logger.warning(f"⚠️ No message_id provided, skipping save")
                                        else:
                                            logger.warning(f"⚠️ Message {message_id} already exists, skipping save")

                                # Handle read receipts
                                elif 'read' in message_event:
                                    read_data = message_event['read']
                                    sender_id = message_event['sender']['id']
                                    watermark = int(read_data.get('watermark', 0))

                                    logger.info(f"📖 Read receipt from {sender_id}, watermark: {watermark}")

                                    # Mark all messages from this sender before the watermark as read
                                    try:
                                        from django.utils import timezone
                                        watermark_datetime = convert_facebook_timestamp(watermark)

                                        # Find messages sent by the page to this user before the watermark
                                        messages_to_mark = FacebookMessage.objects.filter(
                                            page_connection=page_connection,
                                            sender_id=page_id,  # Messages sent BY the page
                                            timestamp__lte=watermark_datetime,
                                            is_read=False
                                        )

                                        updated_count = messages_to_mark.update(
                                            is_read=True,
                                            read_at=timezone.now()
                                        )

                                        logger.info(f"✅ Marked {updated_count} messages as read for conversation with {sender_id}")

                                        # Send WebSocket notification for read receipts
                                        if updated_count > 0:
                                            from django.db import connection
                                            current_schema = getattr(connection, 'schema_name', None)
                                            if current_schema:
                                                # Get the IDs of updated messages
                                                updated_message_ids = list(FacebookMessage.objects.filter(
                                                    page_connection=page_connection,
                                                    sender_id=page_id,
                                                    timestamp__lte=watermark_datetime,
                                                    is_read=True
                                                ).values_list('id', flat=True))

                                                # Typed status frame so /messages-beta flips the
                                                # READ pips on the agent's outgoing bubbles. (Was
                                                # previously wrapped as new_message and dropped by
                                                # the platform guard.)
                                                from .consumers import send_message_status_update
                                                async_to_sync(send_message_status_update)(
                                                    current_schema,
                                                    platform='facebook',
                                                    conversation_id=sender_id,
                                                    account_id=page_id,
                                                    message_ids=updated_message_ids,
                                                    status='read',
                                                )

                                    except Exception as e:
                                        logger.error(f"❌ Failed to process read receipt: {e}")

                                # Handle delivery receipts
                                elif 'delivery' in message_event:
                                    delivery_data = message_event['delivery']
                                    sender_id = message_event['sender']['id']
                                    watermark = int(delivery_data.get('watermark', 0))
                                    message_ids = delivery_data.get('mids', [])

                                    logger.info(f"📬 Delivery receipt from {sender_id}, watermark: {watermark}")

                                    # Mark all messages from this sender before the watermark as delivered
                                    try:
                                        from django.utils import timezone
                                        watermark_datetime = convert_facebook_timestamp(watermark)

                                        # Find messages sent by the page to this user before the watermark
                                        messages_to_mark = FacebookMessage.objects.filter(
                                            page_connection=page_connection,
                                            sender_id=page_id,  # Messages sent BY the page
                                            timestamp__lte=watermark_datetime,
                                            is_delivered=False
                                        )

                                        updated_count = messages_to_mark.update(
                                            is_delivered=True,
                                            delivered_at=timezone.now()
                                        )

                                        logger.info(f"✅ Marked {updated_count} messages as delivered for conversation with {sender_id}")

                                        # Send WebSocket notification for delivery receipts
                                        if updated_count > 0:
                                            from django.db import connection
                                            current_schema = getattr(connection, 'schema_name', None)
                                            if current_schema:
                                                # Get the IDs of updated messages
                                                updated_message_ids = list(FacebookMessage.objects.filter(
                                                    page_connection=page_connection,
                                                    sender_id=page_id,
                                                    timestamp__lte=watermark_datetime,
                                                    is_delivered=True
                                                ).values_list('id', flat=True))

                                                # Typed status frame → DELIVERED pips on beta.
                                                from .consumers import send_message_status_update
                                                async_to_sync(send_message_status_update)(
                                                    current_schema,
                                                    platform='facebook',
                                                    conversation_id=sender_id,
                                                    account_id=page_id,
                                                    message_ids=updated_message_ids,
                                                    status='delivered',
                                                )

                                    except Exception as e:
                                        logger.error(f"❌ Failed to process delivery receipt: {e}")

                                # Handle message reactions
                                elif 'reaction' in message_event:
                                    reaction_data = message_event['reaction']
                                    sender_id = message_event['sender']['id']
                                    message_id = reaction_data.get('mid')
                                    action = reaction_data.get('action')  # 'react' or 'unreact'
                                    reaction = reaction_data.get('reaction')  # 'love', 'smile', 'angry', 'sad', 'wow', 'like'
                                    emoji = reaction_data.get('emoji')

                                    logger.info(f"😀 Reaction from {sender_id}: {action} - {reaction} ({emoji}) on message {message_id}")

                                    try:
                                        from django.utils import timezone

                                        # Find the message that was reacted to
                                        fb_message = FacebookMessage.objects.filter(
                                            page_connection=page_connection,
                                            message_id=message_id
                                        ).first()

                                        if fb_message:
                                            if action == 'react':
                                                # Add or update reaction
                                                fb_message.reaction = reaction
                                                fb_message.reaction_emoji = emoji
                                                fb_message.reacted_by = sender_id
                                                fb_message.reacted_at = timezone.now()
                                                fb_message.save()
                                                logger.info(f"✅ Added reaction {reaction} to message {message_id}")
                                            elif action == 'unreact':
                                                # Remove reaction
                                                fb_message.reaction = None
                                                fb_message.reaction_emoji = None
                                                fb_message.reacted_by = None
                                                fb_message.reacted_at = None
                                                fb_message.save()
                                                logger.info(f"✅ Removed reaction from message {message_id}")

                                            # Send WebSocket notification for reaction
                                            from django.db import connection
                                            current_schema = getattr(connection, 'schema_name', None)
                                            if current_schema:
                                                # Typed reaction frame so beta patches the bubble's
                                                # reactionEmoji live. null emoji = reaction removed.
                                                ws_conversation_id = fb_message.sender_id if fb_message.sender_id != page_id else fb_message.recipient_id
                                                from .consumers import send_reaction_update
                                                async_to_sync(send_reaction_update)(
                                                    current_schema,
                                                    platform='facebook',
                                                    conversation_id=ws_conversation_id,
                                                    account_id=page_id,
                                                    message_id=fb_message.id,
                                                    reaction_emoji=(emoji if action == 'react' else None),
                                                )
                                        else:
                                            logger.warning(f"⚠️ Message not found for reaction: {message_id}")

                                    except Exception as e:
                                        logger.error(f"❌ Failed to process reaction: {e}")

                                else:
                                    logger.info(f"ℹ️ Message event has no 'message', 'read', 'delivery', or 'reaction' field: {message_event}")
                        else:
                            logger.info(f"ℹ️ Entry has no 'messaging' field: {entry}")
                else:
                    logger.warning(f"⚠️ Unknown webhook format - no 'entry' or 'field' found in data: {data}")
            
            return JsonResponse({'status': 'received'})
            
        except Exception as e:
            logger.error(f"Webhook processing failed: {e}")
            return JsonResponse({
                'error': f'Webhook processing failed: {str(e)}'
            }, status=500)
    
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@api_view(['GET'])
@permission_classes([])  # No authentication required for debugging
def facebook_debug_callback(request):
    """Debug endpoint to see what Facebook is actually sending"""
    # Also log to file for debugging
    try:
        from datetime import datetime
        with open('/tmp/facebook_debug_log.txt', 'a') as f:
            f.write(f"\n=== DEBUG ENDPOINT HIT ===\n")
            f.write(f"Time: {datetime.now()}\n")
            f.write(f"Method: {request.method}\n")
            f.write("=" * 50 + "\n")
    except:
        pass
    
    return Response({
        'method': request.method,
        'path': request.path,
        'full_url': request.build_absolute_uri(),
        'get_params': dict(request.GET.items()),
        'post_params': dict(request.POST.items()) if hasattr(request, 'POST') else {},
        'headers': {k: v for k, v in request.META.items() if k.startswith('HTTP_')},
        'message': 'This endpoint shows exactly what Facebook sends to the callback',
        'timestamp': str(datetime.now())
    })


@csrf_exempt
@api_view(['GET', 'POST'])
@permission_classes([])
def webhook_test_endpoint(request):
    """Simple test endpoint to verify webhook connectivity"""
    from datetime import datetime
    import os
    
    # Log everything to file
    try:
        log_file = os.path.join(os.getcwd(), 'webhook_test_log.txt')
        with open(log_file, 'a') as f:
            f.write(f"\n=== WEBHOOK TEST ===\n")
            f.write(f"Time: {datetime.now()}\n")
            f.write(f"Method: {request.method}\n")
            f.write(f"Body: {request.body.decode('utf-8') if request.body else 'No body'}\n")
            f.write("=" * 50 + "\n")
        print(f"TEST: Logged to {log_file}")
    except Exception as e:
        print(f"TEST: Failed to write to log file: {e}")
        print(f"TEST WEBHOOK at {datetime.now()}: {request.method} - {request.body.decode('utf-8') if request.body else 'No body'}")
    
    return Response({
        'status': 'success',
        'message': 'Webhook test endpoint working',
        'method': request.method,
        'timestamp': str(datetime.now()),
        'body_received': request.body.decode('utf-8') if request.body else None,
        'log_file': os.path.join(os.getcwd(), 'webhook_test_log.txt')
    })


@csrf_exempt
@api_view(['GET'])
@permission_classes([])
def debug_facebook_pages(request):
    """Debug endpoint to show available Facebook page connections"""
    try:
        from tenant_schemas.utils import schema_context
        from social_integrations.models import FacebookPageConnection, FacebookMessage
        
        result = {}
        
        with schema_context('amanati'):
            # Get all page connections
            page_connections = FacebookPageConnection.objects.all()
            result['page_connections'] = []
            
            for pc in page_connections:
                page_info = {
                    'page_id': pc.page_id,
                    'page_name': pc.page_name,
                    'is_active': pc.is_active,
                    'created_at': pc.created_at.isoformat() if pc.created_at else None,
                    'message_count': FacebookMessage.objects.filter(page_connection=pc).count()
                }
                result['page_connections'].append(page_info)
            
            # Get recent messages
            recent_messages = FacebookMessage.objects.all().order_by('-created_at')[:5]
            result['recent_messages'] = []
            
            for msg in recent_messages:
                msg_info = {
                    'message_id': msg.message_id,
                    'sender_name': msg.sender_name,
                    'message_text': msg.message_text[:100],
                    'page_name': msg.page_connection.page_name,
                    'timestamp': msg.timestamp.isoformat() if msg.timestamp else None,
                    'created_at': msg.created_at.isoformat() if msg.created_at else None
                }
                result['recent_messages'].append(msg_info)
            
            result['total_pages'] = len(result['page_connections'])
            result['total_messages'] = FacebookMessage.objects.count()
            
        return JsonResponse(result)
        
    except Exception as e:
        return JsonResponse({
            'error': f'Debug failed: {str(e)}'
        }, status=500)


@csrf_exempt
@api_view(['GET'])
@permission_classes([])
def debug_database_status(request):
    """Debug endpoint to check database table status"""
    from django.db import connection
    
    try:
        # Check if tables exist
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name LIKE '%social_integrations%'
            """)
            tables = [row[0] for row in cursor.fetchall()]
        
        # Try to count records
        try:
            page_count = FacebookPageConnection.objects.count()
            message_count = FacebookMessage.objects.count()
        except Exception as e:
            page_count = f"Error: {e}"
            message_count = f"Error: {e}"
        
        # Check migrations
        from django.db.migrations.recorder import MigrationRecorder
        try:
            applied_migrations = MigrationRecorder.Migration.objects.filter(app='social_integrations')
            migrations_list = [f"{m.app}.{m.name}" for m in applied_migrations]
        except Exception as e:
            migrations_list = f"Error: {e}"
        
        return Response({
            'database_tables': tables,
            'page_connections_count': page_count,
            'messages_count': message_count,
            'applied_migrations': migrations_list,
            'database_url': getattr(settings, 'DATABASE_URL', 'Not found')[:50] + '...' if hasattr(settings, 'DATABASE_URL') else 'Not configured'
        })
        
    except Exception as e:
        return Response({
            'error': f'Database check failed: {str(e)}'
        }, status=500)


@csrf_exempt
@api_view(['GET', 'POST'])
@permission_classes([])
def test_facebook_api_access(request):
    """Test Facebook API access with current permissions"""
    try:
        access_token = request.GET.get('access_token') or request.POST.get('access_token')
        
        if not access_token:
            return JsonResponse({
                'error': 'access_token parameter required',
                'usage': 'GET /api/social/facebook/api/test/?access_token=YOUR_TOKEN',
                'note': 'Use this to test what Facebook APIs return with your current access token'
            })
        
        api_version = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_API_VERSION', 'v23.0')
        results = {}
        
        # Test 1: Get user info
        try:
            user_url = f"https://graph.facebook.com/{api_version}/me"
            user_params = {
                'access_token': access_token,
                'fields': 'id,name,email'
            }
            user_response = requests.get(user_url, params=user_params, timeout=30)
            results['user_info'] = {
                'status_code': user_response.status_code,
                'data': user_response.json()
            }
        except Exception as e:
            results['user_info'] = {'error': str(e)}
        
        # Test 2: Get user permissions
        try:
            permissions_url = f"https://graph.facebook.com/{api_version}/me/permissions"
            permissions_params = {'access_token': access_token}
            permissions_response = requests.get(permissions_url, params=permissions_params, timeout=30)
            results['permissions'] = {
                'status_code': permissions_response.status_code,
                'data': permissions_response.json()
            }
        except Exception as e:
            results['permissions'] = {'error': str(e)}
        
        # Test 3: Get pages using /me/accounts (traditional way)
        try:
            accounts_url = f"https://graph.facebook.com/{api_version}/me/accounts"
            accounts_params = {
                'access_token': access_token,
                'fields': 'id,name,access_token,category,about,is_published,tasks'
            }
            accounts_response = requests.get(accounts_url, params=accounts_params, timeout=30)
            results['me_accounts'] = {
                'status_code': accounts_response.status_code,
                'data': accounts_response.json()
            }
        except Exception as e:
            results['me_accounts'] = {'error': str(e)}
        
        # Test 4: Get businesses (with business_management permission)
        try:
            businesses_url = f"https://graph.facebook.com/{api_version}/me/businesses"
            businesses_params = {
                'access_token': access_token,
                'fields': 'id,name,verification_status'
            }
            businesses_response = requests.get(businesses_url, params=businesses_params, timeout=30)
            results['businesses'] = {
                'status_code': businesses_response.status_code,
                'data': businesses_response.json()
            }
        except Exception as e:
            results['businesses'] = {'error': str(e)}
        
        # Test 5: Try pages via business management (if we have businesses)
        try:
            if 'businesses' in results and 'data' in results['businesses'] and results['businesses']['data'].get('data'):
                business_id = results['businesses']['data']['data'][0]['id']
                business_pages_url = f"https://graph.facebook.com/{api_version}/{business_id}/owned_pages"
                business_pages_params = {
                    'access_token': access_token,
                    'fields': 'id,name,category,about'
                }
                business_pages_response = requests.get(business_pages_url, params=business_pages_params, timeout=30)
                results['business_pages'] = {
                    'status_code': business_pages_response.status_code,
                    'data': business_pages_response.json()
                }
            else:
                results['business_pages'] = {'skipped': 'No businesses found'}
        except Exception as e:
            results['business_pages'] = {'error': str(e)}
        
        return JsonResponse({
            'status': 'success',
            'api_version': api_version,
            'tests_performed': len(results),
            'results': results,
            'recommendations': [
                'Check permissions data to see what permissions are granted',
                'If no pages in me/accounts, user may not have admin role on any pages',
                'If business_management permission is granted, check businesses and business_pages',
                'Ensure your Facebook app has business_management permission configured'
            ]
        })
        
    except Exception as e:
        return JsonResponse({
            'error': f'API test failed: {str(e)}'
        }, status=500)


@csrf_exempt
@api_view(['GET', 'POST'])
@permission_classes([])
def test_database_save(request):
    """Test endpoint to manually create a Facebook message in database"""
    try:
        # Find the first active page connection
        page_connection = FacebookPageConnection.objects.filter(is_active=True).first()

        if not page_connection:
            return Response({
                'error': 'No active Facebook page connections found',
                'available_connections': list(FacebookPageConnection.objects.values('page_id', 'page_name', 'is_active'))
            }, status=400)

        # Create a test message
        from datetime import datetime
        import random

        test_message = FacebookMessage.objects.create(
            page_connection=page_connection,
            message_id=f"test_message_{random.randint(1000, 9999)}",
            sender_id="test_sender_123",
            sender_name="Test User",
            message_text="This is a test message created manually",
            timestamp=timezone.now(),
            is_from_page=False,
            profile_pic_url=None,
            is_echo=False,
        )

        return Response({
            'success': True,
            'message_id': test_message.id,
            'message_text': test_message.message_text,
            'page_connection': {
                'page_id': page_connection.page_id,
                'page_name': page_connection.page_name
            },
            'admin_url': f'https://amanati.api.echodesk.ge/admin/social_integrations/facebookmessage/{test_message.id}/change/'
        })

    except Exception as e:
        return Response({
            'error': f'Failed to create test message: {str(e)}'
        }, status=500)


# ============================================================================
# INSTAGRAM VIEWS
# ============================================================================

class InstagramAccountConnectionViewSet(viewsets.ModelViewSet):
    serializer_class = InstagramAccountConnectionSerializer
    permission_classes = [IsAuthenticated, CanManageSocialConnections]

    def get_queryset(self):
        return InstagramAccountConnection.objects.select_related('facebook_page').all()

    def perform_create(self, serializer):
        serializer.save()  # No user assignment needed in multi-tenant setup


class InstagramMessageViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = InstagramMessageSerializer
    permission_classes = [IsAuthenticated, CanViewSocialMessages]
    pagination_class = SocialMessagePagination

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # Shared mutable cache to avoid N+1 queries in get_recipient_name
        if '_ig_recipient_names_cache' not in context:
            context['_ig_recipient_names_cache'] = {}
        return context
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = {
        'account_connection__instagram_account_id': ['exact'],
        'sender_id': ['exact'],
        'is_from_business': ['exact'],
        'is_read_by_staff': ['exact'],
    }
    search_fields = ['sender_name', 'sender_username', 'message_text']
    ordering_fields = ['timestamp', 'created_at']
    ordering = ['-timestamp']

    def get_queryset(self):
        tenant_accounts = InstagramAccountConnection.objects.all()  # All accounts for this tenant
        # Filter out soft-deleted messages
        base_queryset = InstagramMessage.objects.filter(
            account_connection__in=tenant_accounts,
            is_deleted=False
        ).select_related('account_connection', 'sent_by', 'reply_to')

        # Apply account_id filter from query params (support both formats)
        account_id = self.request.query_params.get('account_id')
        if account_id:
            base_queryset = base_queryset.filter(account_connection__instagram_account_id=account_id)

        # Check if hiding assigned chats is enabled
        settings_obj = get_social_settings(self.request)
        if settings_obj and settings_obj.hide_assigned_chats:
            # Admin/superuser sees all messages
            if not (self.request.user.is_superuser or self.request.user.is_staff):
                # Get account IDs for this tenant
                account_ids = list(tenant_accounts.values_list('instagram_account_id', flat=True))

                # Only active assignments — see the Facebook viewset for the
                # full rationale. Completed sessions have assigned_user=None
                # and would otherwise be hidden from everyone.
                active_assignments = ChatAssignment.objects.filter(
                    platform='instagram',
                    account_id__in=account_ids,
                    status__in=['active', 'in_session'],
                )
                my_assigned = active_assignments.filter(
                    assigned_user=self.request.user
                ).values_list('conversation_id', flat=True)
                all_assigned = active_assignments.values_list('conversation_id', flat=True)

                base_queryset = base_queryset.filter(
                    Q(sender_id__in=my_assigned) | ~Q(sender_id__in=all_assigned)
                )

        # Check for initial_load parameter - returns unread messages + 10 most recent
        initial_load = self.request.query_params.get('initial_load', '').lower() == 'true'
        sender_id = self.request.query_params.get('sender_id')

        if initial_load and sender_id:
            # Filter to this conversation
            conv_queryset = base_queryset.filter(sender_id=sender_id)

            # Count unread incoming messages
            unread_count = conv_queryset.filter(
                is_from_business=False,
                is_read_by_staff=False
            ).count()

            # Get unread + 10 messages (minimum 10)
            limit = max(unread_count + 10, 10)

            # Return the most recent `limit` messages
            recent_ids = list(conv_queryset.order_by('-timestamp').values_list('id', flat=True)[:limit])
            return base_queryset.filter(id__in=recent_ids)

        return base_queryset


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def instagram_connection_status(request):
    """Check Instagram connection status for current tenant"""
    try:
        accounts = InstagramAccountConnection.objects.all()  # All accounts for this tenant
        accounts_data = []

        for account in accounts:
            accounts_data.append({
                'id': account.id,
                'instagram_account_id': account.instagram_account_id,
                'username': account.username,
                'profile_picture_url': account.profile_picture_url,
                'is_active': account.is_active,
                'connected_at': account.created_at.isoformat()
            })

        return Response({
            'connected': accounts.exists(),
            'accounts_count': accounts.count(),
            'accounts': accounts_data
        })
    except Exception as e:
        return Response({
            'error': f'Failed to get connection status: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def instagram_disconnect(request):
    """
    Disconnect Instagram integration for current tenant.

    Always performs soft delete (is_active=False) to preserve messages — same
    pattern as facebook_disconnect. Hard CASCADE deletion of every related
    InstagramMessage was timing out behind Daphne for tenants with large
    histories ("Application instance ... took too long to shut down and was
    killed"). Use the clear-history endpoint to permanently delete messages.
    """
    try:
        logger = logging.getLogger(__name__)

        accounts = InstagramAccountConnection.objects.all()
        account_count = accounts.count()
        usernames = list(accounts.values_list('username', flat=True))

        if account_count == 0:
            return Response({
                'status': 'no_accounts',
                'message': 'No Instagram accounts found to disconnect'
            })

        # Single UPDATE — fast, no cascade traversal, no message loss.
        deactivated_count = accounts.update(is_active=False)

        logger.info(
            "✅ Instagram soft disconnect completed: %d account(s) deactivated, messages preserved",
            deactivated_count,
        )

        return Response({
            'status': 'disconnected',
            'instagram_accounts_deactivated': deactivated_count,
            'deactivated_accounts': usernames,
            'message': (
                f'Deactivated {deactivated_count} Instagram account(s). '
                'Messages preserved. Webhooks will be rejected.'
            ),
        })

    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to disconnect Instagram: {e}")
        return Response({
            'error': f'Failed to disconnect: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(
    request=InstagramSendMessageSerializer,
    responses={
        200: {
            'type': 'object',
            'properties': {
                'success': {'type': 'boolean'},
                'message': {'type': 'string'},
                'instagram_message_id': {'type': 'string'}
            }
        },
        400: {
            'type': 'object',
            'properties': {
                'error': {'type': 'string'},
                'details': {'type': 'object'}
            }
        },
        404: {
            'type': 'object',
            'properties': {
                'error': {'type': 'string'}
            }
        }
    },
    description="Send a message to an Instagram user",
    summary="Send Instagram Message"
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanSendSocialMessages])
def instagram_send_message(request):
    """Send a message (text and/or media) to an Instagram user"""
    from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
    request.parsers = [MultiPartParser(), FormParser(), JSONParser()]

    try:
        # Validate input data using serializer
        serializer = InstagramSendMessageSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'error': 'Invalid data',
                'details': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        validated_data = serializer.validated_data
        recipient_id = validated_data['recipient_id']
        message_text = validated_data.get('message', '')
        instagram_account_id = validated_data['instagram_account_id']
        media_file = request.FILES.get('media')
        reply_to_message_id = (request.data.get('reply_to_message_id') or '').strip()

        # Validate: must have text or media
        if not message_text and not media_file:
            return Response({'error': 'Message text or media file is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Get the Instagram account connection for this tenant
        try:
            account_connection = InstagramAccountConnection.objects.get(
                instagram_account_id=instagram_account_id,
                is_active=True
            )
        except InstagramAccountConnection.DoesNotExist:
            return Response({
                'error': 'Instagram account not found or not connected to this tenant'
            }, status=status.HTTP_404_NOT_FOUND)

        # Send message using Facebook Pages Messaging API
        # Instagram messages are sent through the Facebook Page, not the Instagram account directly
        # We use the Page ID in the URL, not the Instagram account ID
        if not account_connection.facebook_page:
            return Response({
                'error': 'Instagram account is not linked to a Facebook Page'
            }, status=status.HTTP_400_BAD_REQUEST)

        page_id = account_connection.facebook_page.page_id

        # Check if we have any messages from this recipient (to verify they messaged us first)
        recent_messages = InstagramMessage.objects.filter(
            account_connection=account_connection,
            sender_id=recipient_id,
            is_from_business=False
        ).order_by('-timestamp')

        if not recent_messages.exists():
            return Response({
                'error': 'Cannot send message: No conversation found with this user. The user must message you first on Instagram.',
                'details': 'Instagram requires the user to initiate the conversation before you can send messages.'
            }, status=status.HTTP_400_BAD_REQUEST)
        # Note: 24-hour window enforcement is handled by Instagram's API.
        # If the window is expired, the API will return an error which we surface to the user.

        # Instagram messaging through Messenger Platform API
        # Use Page ID in URL and specify platform=instagram in params
        # https://developers.facebook.com/docs/messenger-platform/instagram/get-started
        send_url = f"https://graph.facebook.com/v23.0/me/messages"

        # Use the Facebook Page's access token
        access_token = account_connection.facebook_page.page_access_token

        params = {
            'access_token': access_token,
            'platform': 'instagram'  # Specify Instagram platform
        }

        attachments_meta = []

        logger.info(f"Sending Instagram message to {recipient_id} via @{account_connection.username}")

        if media_file:
            # Upload media as attachment via Messenger Platform API (same as Facebook)
            media_mime_type = media_file.content_type or 'application/octet-stream'
            media_filename = media_file.name or 'file'

            # Determine attachment type
            if media_mime_type.startswith('image/'):
                attachment_type = 'image'
            elif media_mime_type.startswith('video/'):
                attachment_type = 'video'
            elif media_mime_type.startswith('audio/'):
                attachment_type = 'audio'
            else:
                attachment_type = 'file'

            # If there's text AND media, send text first then media
            if message_text:
                text_payload = {
                    'recipient': {'id': recipient_id},
                    'message': {'text': message_text},
                    'messaging_type': 'RESPONSE'
                }
                if reply_to_message_id:
                    text_payload['reply_to'] = {'mid': reply_to_message_id}
                text_resp = requests.post(send_url, json=text_payload,
                              headers={'Content-Type': 'application/json'}, params=params, timeout=30)
                # Don't silently drop the text half of a text+media reply.
                if text_resp.status_code >= 400:
                    logger.error(
                        'Failed to send text part of text+media reply: %s - %s',
                        text_resp.status_code, text_resp.text[:300]
                    )

            # Send media attachment using form data upload
            response = requests.post(
                send_url,
                files={'filedata': (media_filename, media_file, media_mime_type)},
                data={
                    'recipient': '{"id":"' + recipient_id + '"}',
                    'message': '{"attachment":{"type":"' + attachment_type + '","payload":{"is_reusable":true}}}',
                    'messaging_type': 'RESPONSE',
                },
                params=params
            , timeout=30)

            attachments_meta = [{
                'mime_type': media_mime_type,
                'filename': media_filename,
                'type': attachment_type,
            }]
        else:
            # Text-only message
            message_data = {
                'recipient': {'id': recipient_id},
                'message': {'text': message_text},
                'messaging_type': 'RESPONSE'
            }
            if reply_to_message_id:
                message_data['reply_to'] = {'mid': reply_to_message_id}

            response = requests.post(
                send_url,
                json=message_data,
                headers={'Content-Type': 'application/json'},
                params=params
            , timeout=30)

        print(f"📤 Instagram API Response: {response.status_code} - {response.text}")

        if response.status_code == 200:
            response_data = response.json()
            message_id = response_data.get('message_id')

            # Optionally save the sent message to our database
            try:
                from .consumers import send_new_message_notification
                from django.db import connection

                timestamp = datetime.now()
                reply_to_obj = (
                    InstagramMessage.objects.filter(message_id=reply_to_message_id).first()
                    if reply_to_message_id else None
                )
                create_kwargs = dict(
                    account_connection=account_connection,
                    message_id=message_id or f"sent_{timestamp.timestamp()}",
                    sender_id=recipient_id,  # Use recipient_id for conversation grouping (same as incoming messages)
                    sender_username=account_connection.username,
                    message_text=message_text,
                    timestamp=timestamp,
                    is_from_business=True,
                    source='echodesk',
                    is_echo=False,
                    sent_by=request.user,
                    reply_to_message_id=reply_to_message_id or None,
                    reply_to=reply_to_obj,
                )
                if attachments_meta:
                    create_kwargs['attachments'] = attachments_meta
                    create_kwargs['attachment_type'] = attachments_meta[0].get('type', '')

                ig_message = InstagramMessage.objects.create(**create_kwargs)
                print(f"✅ Saved sent message to database")

                # Unarchive conversation if it was archived (move back to active when sending a message)
                unarchived = ConversationArchive.objects.filter(
                    platform='instagram',
                    conversation_id=recipient_id,
                    account_id=instagram_account_id
                ).delete()[0]
                if unarchived:
                    print(f"📤 Unarchived conversation due to sent message: instagram/{instagram_account_id}/{recipient_id}")

                # Delete completed assignments to allow new sessions
                deleted_assignment = ChatAssignment.objects.filter(
                    platform='instagram',
                    conversation_id=recipient_id,
                    account_id=instagram_account_id,
                    status='completed'
                ).delete()[0]
                if deleted_assignment:
                    print(f"🔄 Deleted completed assignment for new conversation: instagram/{instagram_account_id}/{recipient_id}")

                # Broadcast via WebSocket so the message appears in real-time
                tenant_schema = connection.schema_name

                # Look up recipient's name from previous incoming messages in this conversation
                recipient_name = None
                previous_msg = InstagramMessage.objects.filter(
                    account_connection=account_connection,
                    sender_id=recipient_id,
                    is_from_business=False
                ).order_by('-timestamp').first()
                if previous_msg:
                    recipient_name = previous_msg.sender_name or previous_msg.sender_username

                message_data = {
                    'id': ig_message.id,
                    'message_id': ig_message.message_id,
                    'platform': 'instagram',
                    'sender_id': instagram_account_id,  # The account is the sender when is_from_business=True
                    'sender_username': account_connection.username,
                    'recipient_id': recipient_id,  # The user we're messaging
                    'recipient_name': recipient_name,  # The user's name (for new conversations in frontend)
                    'message_text': message_text,
                    'attachments': attachments_meta,
                    'timestamp': timestamp.isoformat(),
                    'is_from_business': True,
                    'account_id': instagram_account_id,
                    'sent_by': request.user.email if request.user else None,
                    'reply_to_message_id': ig_message.reply_to_message_id,
                    'reply_to_id': ig_message.reply_to_id if ig_message.reply_to_id else None,
                }
                async_to_sync(send_new_message_notification)(tenant_schema, recipient_id, message_data)
            except Exception as e:
                print(f"⚠️ Failed to save/broadcast sent message to database: {e}")

            return Response({
                'status': 'sent',
                'message_id': message_id,
                'recipient_id': recipient_id
            })
        else:
            error_data = response.json() if response.content else {}
            error_message = error_data.get('error', {}).get('message', 'Unknown error')

            return Response({
                'error': f'Failed to send message: {error_message}',
                'instagram_error': error_data
            }, status=status.HTTP_400_BAD_REQUEST)

    except Exception as e:
        print(f"❌ Exception in instagram_send_message: {e}")
        return Response({
            'error': f'Failed to send message: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def find_tenant_by_instagram_account_id(instagram_account_id):
    """Find which tenant schema owns the given Instagram account ID (O(1) + fallback)."""
    from social_integrations.platform_routing import resolve_tenant
    return resolve_tenant("instagram", instagram_account_id)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def instagram_webhook(request):
    """Handle Instagram webhook events for messages"""
    logger = logging.getLogger(__name__)

    if request.method == 'GET':
        # Webhook verification - Instagram/Facebook sends these parameters
        mode = request.GET.get('hub.mode')
        token = request.GET.get('hub.verify_token')
        challenge = request.GET.get('hub.challenge')

        # Verify token from settings (same as Facebook)
        verify_token = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_VERIFY_TOKEN', '')

        # Verify the mode and token
        if mode == 'subscribe' and verify_token and token == verify_token:
            # Return the challenge as plain text
            return HttpResponse(challenge, content_type='text/plain')
        else:
            return JsonResponse({
                'error': 'Invalid verify token or mode'
            }, status=403)

    elif request.method == 'POST':
        # Handle webhook events
        try:
            import json
            from tenant_schemas.utils import schema_context

            data = json.loads(request.body)
            logger.info(f"📸 Instagram webhook received: {data}")

            # Instagram webhooks have similar structure to Facebook
            if 'entry' not in data:
                logger.warning("No 'entry' field in Instagram webhook data")
                return JsonResponse({'status': 'received'})

            for entry in data['entry']:
                # Get Instagram account ID from entry
                instagram_account_id = entry.get('id')

                if not instagram_account_id:
                    logger.warning("No Instagram account ID in entry")
                    continue

                # Find which tenant this Instagram account belongs to
                tenant_schema = find_tenant_by_instagram_account_id(instagram_account_id)

                if not tenant_schema:
                    logger.debug(f"Ignoring webhook for unlinked Instagram account: {instagram_account_id}")
                    continue

                logger.info(f"Processing Instagram webhook for account {instagram_account_id} in tenant: {tenant_schema}")

                # Process webhook within the correct tenant context
                with schema_context(tenant_schema):
                    # Get the Instagram account connection
                    try:
                        account_connection = InstagramAccountConnection.objects.get(
                            instagram_account_id=instagram_account_id,
                            is_active=True
                        )
                    except InstagramAccountConnection.DoesNotExist:
                        logger.error(f"Instagram account connection not found: {instagram_account_id}")
                        continue

                    # Process messaging events
                    if 'messaging' in entry:
                        for message_event in entry['messaging']:
                            if 'message' in message_event:
                                message_data = message_event['message']
                                sender_id = message_event['sender']['id']

                                logger.info(f"📨 Instagram webhook message_event: {message_event}")

                                # Handle echo messages (messages sent by the business)
                                if message_data.get('is_echo'):
                                    logger.info("📤 Processing Instagram echo message (outgoing from business)")
                                    message_id = message_data.get('mid')
                                    recipient_id = message_event.get('recipient', {}).get('id', '')

                                    if message_id:
                                        try:
                                            ig_timestamp = message_event.get('timestamp')
                                            timestamp_dt = convert_facebook_timestamp(ig_timestamp) if ig_timestamp else timezone.now()

                                            # Check if message already exists
                                            existing = InstagramMessage.objects.filter(
                                                account_connection=account_connection,
                                                message_id=message_id
                                            ).first()

                                            if existing:
                                                # Update timestamp and attachment URLs from echo webhook
                                                update_fields = ['timestamp']
                                                existing.timestamp = timestamp_dt

                                                # Echo webhook contains CDN URLs for attachments — update DB
                                                raw_attachments = message_data.get('attachments', [])
                                                if raw_attachments:
                                                    attachments = []
                                                    for att in raw_attachments:
                                                        att_type = att.get('type', 'file')
                                                        att_url = att.get('payload', {}).get('url')
                                                        attachments.append({
                                                            'type': att_type,
                                                            'url': att_url,
                                                        })
                                                    existing.attachments = attachments
                                                    update_fields.append('attachments')
                                                    if not existing.attachment_url and attachments:
                                                        existing.attachment_url = attachments[0].get('url')
                                                        existing.attachment_type = attachments[0].get('type', '')
                                                        update_fields.extend(['attachment_url', 'attachment_type'])

                                                existing.save(update_fields=update_fields)
                                                logger.info(f"✅ Updated Instagram echo message (timestamp + {len(raw_attachments)} attachments) for {message_id}")
                                            else:
                                                # Create new message for echo (sent from Instagram directly)
                                                message_text = message_data.get('text', '')

                                                # Handle attachments
                                                attachments = []
                                                attachment_type = ''
                                                attachment_url = None
                                                raw_attachments = message_data.get('attachments', [])

                                                for att in raw_attachments:
                                                    att_type = att.get('type', 'file')
                                                    att_url = att.get('payload', {}).get('url')
                                                    attachments.append({
                                                        'type': att_type,
                                                        'url': att_url,
                                                    })
                                                    if not attachment_type:
                                                        attachment_type = att_type
                                                        attachment_url = att_url

                                                try:
                                                    echo_message = InstagramMessage.objects.create(
                                                        account_connection=account_connection,
                                                        message_id=message_id,
                                                        sender_id=recipient_id,  # Use recipient_id for conversation grouping (same as direct send API)
                                                        sender_name=account_connection.username,
                                                        sender_username=account_connection.username,
                                                        message_text=message_text,
                                                        attachment_type=attachment_type,
                                                        attachment_url=attachment_url,
                                                        attachments=attachments,
                                                        timestamp=timestamp_dt,
                                                        is_from_business=True,
                                                        is_delivered=True,
                                                        source='instagram_app',  # Message sent from Instagram app
                                                        is_echo=True,
                                                        sent_by=None,  # Not sent via EchoDesk
                                                    )
                                                    logger.info(f"✅ Created Instagram echo message: {message_id} (recipient: {recipient_id})")
                                                except IntegrityError:
                                                    # Race condition: duplicate webhook delivery — another request created
                                                    # the same message_id between our filter check and this create.
                                                    # Silently skip — the other request will handle downstream logic.
                                                    echo_message = InstagramMessage.objects.filter(
                                                        account_connection=account_connection,
                                                        message_id=message_id
                                                    ).first()
                                                    logger.warning(f"⚠️ Instagram echo message already exists (race condition), skipping: {message_id}")
                                                    continue

                                                # Look up recipient's name from previous incoming messages
                                                recipient_name = None
                                                recipient_username = None
                                                recipient_profile_pic = None
                                                previous_msg = InstagramMessage.objects.filter(
                                                    account_connection=account_connection,
                                                    sender_id=recipient_id,
                                                    is_from_business=False
                                                ).order_by('-timestamp').first()
                                                if previous_msg:
                                                    if previous_msg.sender_name:
                                                        recipient_name = previous_msg.sender_name
                                                    if previous_msg.sender_username:
                                                        recipient_username = previous_msg.sender_username
                                                    if previous_msg.sender_profile_pic:
                                                        recipient_profile_pic = previous_msg.sender_profile_pic

                                                # If no previous message, try to fetch profile from Instagram API
                                                if not recipient_name and not recipient_username:
                                                    try:
                                                        profile_url = f"https://graph.facebook.com/v23.0/{recipient_id}"
                                                        profile_params = {
                                                            'fields': 'name,username,profile_pic',
                                                            'access_token': account_connection.access_token
                                                        }
                                                        profile_response = requests.get(profile_url, params=profile_params, timeout=10)
                                                        if profile_response.status_code == 200:
                                                            profile_data = profile_response.json()
                                                            recipient_name = profile_data.get('name', '')
                                                            recipient_username = profile_data.get('username', '')
                                                            recipient_profile_pic = profile_data.get('profile_pic')
                                                            logger.info(f"👤 Fetched profile for Instagram echo recipient {recipient_id}: {recipient_name} (@{recipient_username})")
                                                    except Exception as profile_err:
                                                        logger.warning(f"⚠️ Could not fetch profile for Instagram echo recipient {recipient_id}: {profile_err}")

                                                # Create or update SocialAccount for the recipient (auto-create client if needed)
                                                try:
                                                    social_account = SocialAccount.objects.filter(
                                                        platform='instagram',
                                                        platform_id=recipient_id,
                                                        account_connection_id=instagram_account_id
                                                    ).first()

                                                    display_name = recipient_name or recipient_username or f'Instagram User {recipient_id}'

                                                    if not social_account:
                                                        # Create a new client and social account for this Instagram user
                                                        client = SocialClient.objects.create(
                                                            name=display_name,
                                                            profile_picture=recipient_profile_pic,
                                                        )
                                                        social_account = SocialAccount.objects.create(
                                                            client=client,
                                                            platform='instagram',
                                                            platform_id=recipient_id,
                                                            account_connection_id=instagram_account_id,
                                                            display_name=display_name,
                                                            username=recipient_username,
                                                            profile_pic_url=recipient_profile_pic,
                                                            is_auto_created=True,
                                                        )
                                                        logger.info(f"✅ Auto-created client and social account for Instagram echo recipient: {recipient_id} ({display_name})")
                                                    else:
                                                        # Update last_message_at and name if we have better info
                                                        update_fields = ['last_message_at']
                                                        social_account.last_message_at = timestamp_dt
                                                        if display_name and (not social_account.display_name or social_account.display_name.startswith('Instagram User')):
                                                            social_account.display_name = display_name
                                                            update_fields.append('display_name')
                                                            # Also update client name
                                                            if social_account.client:
                                                                social_account.client.name = display_name
                                                                social_account.client.save(update_fields=['name'])
                                                        if recipient_username and not social_account.username:
                                                            social_account.username = recipient_username
                                                            update_fields.append('username')
                                                        if recipient_profile_pic and not social_account.profile_pic_url:
                                                            social_account.profile_pic_url = recipient_profile_pic
                                                            update_fields.append('profile_pic_url')
                                                        social_account.save(update_fields=update_fields)
                                                except Exception as client_err:
                                                    logger.warning(f"⚠️ Could not create/update social account for Instagram echo recipient: {client_err}")

                                                # Send WebSocket notification
                                                ws_data = {
                                                    'id': echo_message.id,
                                                    'message_id': echo_message.message_id,
                                                    'platform': 'instagram',
                                                    'sender_id': instagram_account_id,  # The account is the sender when is_from_business=True
                                                    'sender_name': echo_message.sender_name,
                                                    'sender_username': echo_message.sender_username,
                                                    'recipient_id': recipient_id,  # The user we're messaging
                                                    'recipient_name': recipient_name,  # The user's name (for frontend sidebar)
                                                    'message_text': echo_message.message_text,
                                                    'attachment_type': echo_message.attachment_type,
                                                    'attachment_url': echo_message.attachment_url,
                                                    'timestamp': echo_message.timestamp.isoformat(),
                                                    'is_from_business': True,
                                                    'account_id': instagram_account_id,
                                                }
                                                send_websocket_notification(tenant_schema, ws_data, recipient_id)

                                        except Exception as e:
                                            logger.error(f"❌ Failed to process Instagram echo message: {e}")

                                    continue

                                # Get sender info - fetch from Instagram Graph API
                                sender_name = ''  # Display name
                                sender_username = sender_id  # Use the ID as username by default
                                sender_profile_pic = None

                                # Try to fetch the sender's Instagram username, name, and profile picture
                                if sender_id != instagram_account_id:  # Don't fetch profile for business account itself
                                    try:
                                        # Use Instagram Graph API to get user info
                                        profile_url = f"https://graph.facebook.com/v23.0/{sender_id}"
                                        profile_params = {
                                            'fields': 'name,username,profile_pic',
                                            'access_token': account_connection.access_token
                                        }
                                        logger.info(f"👤 Fetching Instagram profile for sender {sender_id}")
                                        profile_response = requests.get(profile_url, params=profile_params, timeout=10)

                                        logger.info(f"👤 Instagram profile fetch response: status={profile_response.status_code}")
                                        if profile_response.status_code == 200:
                                            profile_data = profile_response.json()
                                            logger.info(f"👤 Instagram profile data received: {profile_data}")
                                            sender_name = profile_data.get('name', '')
                                            sender_username = profile_data.get('username', sender_id)
                                            sender_profile_pic = profile_data.get('profile_pic')
                                            logger.info(f"👤 Set sender_name to: {sender_name}, sender_username to: {sender_username}, profile_pic: {sender_profile_pic is not None}")

                                            # Validate URL length to prevent database errors
                                            if sender_profile_pic and len(sender_profile_pic) > 500:
                                                logger.warning(f"Instagram profile pic URL too long ({len(sender_profile_pic)} chars), truncating")
                                                sender_profile_pic = None
                                        else:
                                            error_data = profile_response.json() if profile_response.content else {}
                                            logger.warning(f"⚠️ Failed to fetch Instagram profile for {sender_id}: status={profile_response.status_code}, error={error_data}")

                                    except Exception as e:
                                        logger.error(f"❌ Exception fetching Instagram profile for {sender_id}: {type(e).__name__}: {e}")

                                    # Try fetching profile picture separately (like Facebook) if not available from profile
                                    if not sender_profile_pic:
                                        try:
                                            # Try the Facebook-style picture endpoint
                                            pic_url = f"https://graph.facebook.com/v23.0/{sender_id}/picture"
                                            pic_params = {
                                                'type': 'large',
                                                'redirect': 'false',
                                                'access_token': account_connection.access_token
                                            }
                                            pic_response = requests.get(pic_url, params=pic_params, timeout=10)
                                            if pic_response.status_code == 200:
                                                pic_data = pic_response.json().get('data', {})
                                                if not pic_data.get('is_silhouette', True):
                                                    sender_profile_pic = pic_data.get('url')
                                                    logger.info(f"👤 Fetched Instagram profile picture via /picture endpoint")
                                        except Exception as e:
                                            logger.warning(f"Could not fetch Instagram profile picture via /picture endpoint: {e}")

                                # Save the message
                                message_id = message_data.get('mid', '')
                                message_text = message_data.get('text', '')

                                # Extract attachment information for Instagram
                                attachment_type = ''
                                attachment_url = None
                                attachments = []

                                if 'attachments' in message_data:
                                    raw_attachments = message_data.get('attachments', [])
                                    for att in raw_attachments:
                                        att_type = att.get('type', '')
                                        payload = att.get('payload', {})
                                        att_url = payload.get('url', '')

                                        attachment_obj = {
                                            'type': att_type,
                                            'url': att_url,
                                        }

                                        # Add sticker_id if present
                                        if payload.get('sticker_id'):
                                            attachment_obj['sticker_id'] = payload.get('sticker_id')

                                        attachments.append(attachment_obj)

                                        # Set primary attachment type and URL (first attachment)
                                        if not attachment_type:
                                            attachment_type = att_type
                                            attachment_url = att_url

                                    logger.info(f"📎 Found {len(attachments)} Instagram attachment(s): {[a['type'] for a in attachments]}")

                                logger.info(f"💾 Saving Instagram message from sender_id: {sender_id}, username: {sender_username}")

                                # Extract reply_to (message replies) — Instagram
                                # sends reply_to.mid just like Facebook.
                                ig_reply = message_data.get('reply_to') or {}
                                ig_reply_mid = ig_reply.get('mid')
                                ig_reply_obj = (
                                    InstagramMessage.objects.filter(message_id=ig_reply_mid).first()
                                    if ig_reply_mid else None
                                )

                                if message_id and not InstagramMessage.objects.filter(message_id=message_id).exists():
                                    try:
                                        message_obj = InstagramMessage.objects.create(
                                            account_connection=account_connection,
                                            message_id=message_id,
                                            sender_id=sender_id,
                                            sender_name=sender_name,
                                            sender_username=sender_username,
                                            sender_profile_pic=sender_profile_pic,
                                            message_text=message_text,
                                            attachment_type=attachment_type,
                                            attachment_url=attachment_url,
                                            attachments=attachments,
                                            timestamp=convert_facebook_timestamp(message_event.get('timestamp', 0)),
                                            is_from_business=False,
                                            reply_to_message_id=ig_reply_mid,
                                            reply_to=ig_reply_obj,
                                        )
                                        logger.info(f"✅ Saved Instagram message from {sender_username}: {message_text[:50] if message_text else f'[{attachment_type}]'}")

                                        # Check if this is a rating response
                                        process_potential_rating_response(
                                            message_text=message_text,
                                            platform='instagram',
                                            conversation_id=sender_id,
                                            account_id=account_connection.instagram_account_id,
                                            message_id=message_id
                                        )

                                        # Auto-unarchive if conversation was in history
                                        auto_unarchive_conversation(
                                            platform='instagram',
                                            conversation_id=sender_id,
                                            account_id=account_connection.instagram_account_id
                                        )

                                        # Process auto-reply (doesn't mark as read)
                                        process_auto_reply(
                                            platform='instagram',
                                            account_id=account_connection.instagram_account_id,
                                            conversation_id=sender_id,
                                            sender_name=sender_name or sender_username,
                                            connection=account_connection
                                        )

                                        # Send WebSocket notification for real-time updates
                                        from django.db import connection
                                        current_schema = getattr(connection, 'schema_name', None)
                                        if current_schema:
                                            ws_message_data = {
                                                'id': message_obj.id,
                                                'message_id': message_obj.message_id,
                                                'sender_id': message_obj.sender_id,
                                                'sender_name': message_obj.sender_name,
                                                'sender_username': message_obj.sender_username,
                                                'message_text': message_obj.message_text,
                                                'attachment_type': message_obj.attachment_type,
                                                'attachment_url': message_obj.attachment_url,
                                                'attachments': message_obj.attachments,
                                                'timestamp': message_obj.timestamp.isoformat() if message_obj.timestamp else None,
                                                'is_from_business': message_obj.is_from_business,
                                                'platform': 'instagram',
                                                'account_id': account_connection.instagram_account_id,
                                                'chat_id': f'ig_{account_connection.instagram_account_id}_{sender_id}',
                                                'reply_to_message_id': message_obj.reply_to_message_id,
                                                'reply_to_id': message_obj.reply_to_id if message_obj.reply_to_id else None,
                                            }
                                            # Conversation ID is the sender_id (the customer)
                                            ws_conversation_id = sender_id
                                            # Get assignment for notification filtering
                                            assigned_user_id = get_assignment_for_conversation(
                                                platform='instagram',
                                                conversation_id=sender_id,
                                                account_id=account_connection.instagram_account_id
                                            )
                                            send_websocket_notification(current_schema, ws_message_data, ws_conversation_id, assigned_user_id)

                                            # Create in-app notification for incoming customer messages
                                            try:
                                                from users.notification_utils import create_social_message_notification
                                                create_social_message_notification(
                                                    platform='Instagram',
                                                    sender_name=sender_name or sender_username or str(sender_id),
                                                    message_text=message_text,
                                                    conversation_id=sender_id,
                                                    sender_id=sender_id,
                                                    account_id=account_connection.instagram_account_id,
                                                    assigned_user_id=assigned_user_id,
                                                )
                                            except Exception as notif_err:
                                                logger.error(f"Failed to create Instagram message notification: {notif_err}")
                                        else:
                                            logger.warning(f"⚠️ WebSocket: Could not determine tenant schema - skipping notification")

                                    except Exception as e:
                                        logger.error(f"❌ Failed to save Instagram message: {e}")

            return JsonResponse({'status': 'received'})

        except Exception as e:
            logger.error(f"Instagram webhook processing failed: {e}")
            return JsonResponse({
                'error': f'Webhook processing failed: {str(e)}'
            }, status=500)

    return JsonResponse({'error': 'Method not allowed'}, status=405)


# ============================================================================
# WEBHOOK DEBUGGING ENDPOINTS
# ============================================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated, IsStaffUser])
def webhook_debug_logs(request):
    """
    Get recent webhook activity logs for debugging.
    Shows last 50 webhook events from the log file.
    """
    import os
    from datetime import datetime

    log_file = os.path.join(os.getcwd(), 'facebook_webhook_log.txt')

    if not os.path.exists(log_file):
        return Response({
            'status': 'no_logs',
            'message': 'No webhook logs found yet. Send a test webhook to create logs.',
            'log_file_path': log_file
        })

    try:
        # Read the last 100 lines of the log file
        with open(log_file, 'r') as f:
            lines = f.readlines()

        # Get last 100 lines
        recent_lines = lines[-100:] if len(lines) > 100 else lines

        # Parse into events
        events = []
        current_event = {}

        for line in recent_lines:
            if '=== WEBHOOK RECEIVED ===' in line:
                if current_event:
                    events.append(current_event)
                current_event = {'raw': ''}

            current_event['raw'] = current_event.get('raw', '') + line

            if line.startswith('Time:'):
                current_event['timestamp'] = line.replace('Time:', '').strip()
            elif line.startswith('Method:'):
                current_event['method'] = line.replace('Method:', '').strip()
            elif line.startswith('Body:'):
                current_event['body'] = line.replace('Body:', '').strip()

        if current_event:
            events.append(current_event)

        return Response({
            'status': 'success',
            'total_events': len(events),
            'events': events[-20:],  # Last 20 events
            'log_file_path': log_file,
            'file_size_bytes': os.path.getsize(log_file)
        })

    except Exception as e:
        return Response({
            'error': f'Failed to read webhook logs: {str(e)}',
            'log_file_path': log_file
        }, status=500)


@api_view(['POST'])
@permission_classes([])  # Public endpoint for testing
def webhook_test_receiver(request):
    """
    Test endpoint to receive and log any webhook data.
    Use this to test if webhooks are reaching your server.

    Usage:
    POST https://api.echodesk.ge/api/social/webhook-test/

    Send any JSON data and it will be logged and returned.
    """
    import json
    from datetime import datetime
    import os

    # Log everything we receive
    log_data = {
        'timestamp': str(datetime.now()),
        'method': request.method,
        'headers': dict(request.headers),
        'query_params': dict(request.GET),
        'body': request.body.decode('utf-8') if request.body else None,
    }

    try:
        log_data['parsed_body'] = json.loads(request.body) if request.body else None
    except:
        log_data['parsed_body'] = 'Could not parse as JSON'

    # Write to test log file
    test_log_file = os.path.join(os.getcwd(), 'webhook_test_log.txt')
    with open(test_log_file, 'a') as f:
        f.write(f"\n{'='*80}\n")
        f.write(f"WEBHOOK TEST RECEIVED at {datetime.now()}\n")
        f.write(json.dumps(log_data, indent=2))
        f.write(f"\n{'='*80}\n")

    print(f"✅ Webhook test received and logged to {test_log_file}")
    print(json.dumps(log_data, indent=2))

    return Response({
        'status': 'received',
        'message': 'Webhook test successful! Your data has been logged.',
        'received_data': log_data,
        'logged_to': test_log_file
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsStaffUser])
def webhook_status(request):
    """
    Check webhook configuration and recent activity.
    Shows webhook URLs, verify tokens, and recent activity.
    """
    import os
    from datetime import datetime

    # Check log files
    fb_log = os.path.join(os.getcwd(), 'facebook_webhook_log.txt')
    test_log = os.path.join(os.getcwd(), 'webhook_test_log.txt')

    fb_log_exists = os.path.exists(fb_log)
    test_log_exists = os.path.exists(test_log)

    fb_log_size = os.path.getsize(fb_log) if fb_log_exists else 0
    test_log_size = os.path.getsize(test_log) if test_log_exists else 0

    # Count webhook events
    fb_event_count = 0
    if fb_log_exists:
        with open(fb_log, 'r') as f:
            fb_event_count = f.read().count('=== WEBHOOK RECEIVED ===')

    test_event_count = 0
    if test_log_exists:
        with open(test_log, 'r') as f:
            test_event_count = f.read().count('WEBHOOK TEST RECEIVED')

    verify_token = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_VERIFY_TOKEN', '')

    return Response({
        'status': 'configured',
        'webhook_urls': {
            'facebook': request.build_absolute_uri('/api/social/facebook/webhook/'),
            'instagram': request.build_absolute_uri('/api/social/instagram/webhook/'),
            'test_endpoint': request.build_absolute_uri('/api/social/webhook-test/')
        },
        'verify_token': verify_token,
        'activity': {
            'facebook_webhooks': {
                'total_received': fb_event_count,
                'log_file_exists': fb_log_exists,
                'log_file_size_bytes': fb_log_size,
                'log_file_path': fb_log
            },
            'test_webhooks': {
                'total_received': test_event_count,
                'log_file_exists': test_log_exists,
                'log_file_size_bytes': test_log_size,
                'log_file_path': test_log
            }
        },
        'instructions': {
            'test_webhook': 'Send POST request to /api/social/webhook-test/ with any JSON data',
            'view_logs': 'GET /api/social/webhook-logs/ to see recent webhook activity',
            'facebook_setup': 'Configure webhook in Facebook App → Messenger → Settings',
            'instagram_setup': 'Instagram uses same webhook as Facebook (Pages Messaging API)'
        }
    })


@extend_schema(
    tags=['Social Settings'],
    summary='Get or update social integration settings',
    description='Get or update social integration settings for the current tenant including auto-reply configuration.',
    request=SocialIntegrationSettingsSerializer,
    responses={200: SocialIntegrationSettingsSerializer}
)
@api_view(['GET', 'PUT', 'PATCH'])
@permission_classes([IsAuthenticated, CanManageSocialSettings])
def social_settings(request):
    """Get or update social integration settings for the current tenant"""
    logger = logging.getLogger(__name__)

    try:
        # Get or create settings for this tenant (singleton pattern)
        settings_obj, created = SocialIntegrationSettings.objects.get_or_create(
            defaults={'refresh_interval': 5000}
        )

        if request.method == 'GET':
            serializer = SocialIntegrationSettingsSerializer(settings_obj)
            logger.info(f"GET social settings: {serializer.data}")
            return Response(serializer.data)

        elif request.method in ['PUT', 'PATCH']:
            partial = request.method == 'PATCH'
            logger.info(f"Received settings update request: {request.data}")
            serializer = SocialIntegrationSettingsSerializer(
                settings_obj,
                data=request.data,
                partial=partial
            )

            if serializer.is_valid():
                serializer.save()
                logger.info(f"Updated social integration settings: {serializer.data}")
                return Response(serializer.data)
            else:
                logger.error(f"Settings validation failed: {serializer.errors}")
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    except Exception as e:
        logger.error(f"Error managing social settings: {e}")
        return Response({
            'error': f'Failed to manage settings: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# =====================
# CHAT ASSIGNMENT ENDPOINTS
# =====================

@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def my_assignments(request):
    """Get all chat assignments for the current user"""
    assignments = ChatAssignment.objects.filter(
        assigned_user=request.user
    ).select_related('assigned_user').order_by('-updated_at')

    serializer = ChatAssignmentSerializer(assignments, many=True)
    return Response(serializer.data)


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def bulk_assignment_status(request):
    """Return every active / in-session / completed chat assignment plus the
    tenant settings flags in a single response.

    The frontend used to fire one ``/assignments/status/`` request *per
    visible chat* on every inbox load. The bulk endpoint replaces that fan-out
    with one query so opening the inbox with 20 chats no longer triggers 20
    round-trips.

    Query parameters:
    - ``platforms``: comma-separated list of platforms to include
      (default: facebook,instagram,whatsapp,email,widget). The result is
      bounded to these so we don't load assignments for platforms the user
      isn't viewing.
    """
    platforms_param = request.query_params.get(
        'platforms',
        'facebook,instagram,whatsapp,email,widget',
    )
    enabled_platforms = [p.strip().lower() for p in platforms_param.split(',') if p.strip()]

    assignments_qs = ChatAssignment.objects.filter(
        platform__in=enabled_platforms,
        status__in=['active', 'in_session', 'completed'],
    ).select_related('assigned_user').order_by('-updated_at')

    settings_obj = get_social_settings(request)
    settings_data = {
        'chat_assignment_enabled': settings_obj.chat_assignment_enabled if settings_obj else False,
        'session_management_enabled': settings_obj.session_management_enabled if settings_obj else False,
        'hide_assigned_chats': settings_obj.hide_assigned_chats if settings_obj else False,
        'collect_customer_rating': settings_obj.collect_customer_rating if settings_obj else False,
    }

    return Response({
        'assignments': ChatAssignmentSerializer(assignments_qs, many=True).data,
        'settings': settings_data,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def all_assignments(request):
    """Get all chat assignments (admin only)"""
    if not request.user.is_superuser and not request.user.is_staff:
        return Response(
            {'error': 'Only admins can view all assignments'},
            status=status.HTTP_403_FORBIDDEN
        )

    assignments = ChatAssignment.objects.all().select_related('assigned_user').order_by('-updated_at')
    serializer = ChatAssignmentSerializer(assignments, many=True)
    return Response(serializer.data)


# ---------------------------------------------------------------------------
# Cross-user reactivity broadcast helpers for /messages-beta.
#
# Each `_broadcast_*` is best-effort: if the channel layer or Redis is down
# we log and move on; the REST response still succeeds. The legacy /messages
# frontend ignores these event types, so we can ship these emits ahead of
# /messages-beta turning on, without any risk to the live UI.
# ---------------------------------------------------------------------------
def _broadcast_assignment_change(request, assignment, archived_change_to_unarchived=False):
    """Emit an `assignment_update` WS frame after an assign/unassign/transfer/
    start-session/end-session mutation has committed. `assignment` can be a
    ChatAssignment instance (still in the DB) or a dict with the platform /
    conversation_id / account_id keys when the row was just deleted.

    Best-effort — wraps any broadcast failure so it never breaks the REST
    request. Also emits an `archive_update(archived=false)` companion when
    the mutation unarchived the conversation, so beta clients reflect the
    move out of history in the same call.
    """
    from django.db import connection
    from .consumers import send_assignment_update, send_archive_update

    if isinstance(assignment, ChatAssignment):
        payload = {
            'platform': assignment.platform,
            'conversation_id': assignment.conversation_id,
            'account_id': assignment.account_id,
            'assigned_user_id': assignment.assigned_user_id,
            'assigned_user_name': (
                assignment.assigned_user.get_full_name() or assignment.assigned_user.email
            ) if assignment.assigned_user else None,
            'status': assignment.status,
            'session_started_at': assignment.session_started_at.isoformat()
                if assignment.session_started_at else None,
            'session_ended_at': assignment.session_ended_at.isoformat()
                if assignment.session_ended_at else None,
        }
    else:
        # Post-delete: caller passes the lookup keys plus the desired final
        # state (typically unassigned + null status).
        payload = {
            'platform': assignment.get('platform'),
            'conversation_id': assignment.get('conversation_id'),
            'account_id': assignment.get('account_id'),
            'assigned_user_id': None,
            'assigned_user_name': None,
            'status': assignment.get('status'),
            'session_started_at': assignment.get('session_started_at'),
            'session_ended_at': assignment.get('session_ended_at'),
        }

    payload['by_user_id'] = request.user.id if getattr(request, 'user', None) and request.user.is_authenticated else None
    tenant_schema = connection.schema_name

    try:
        async_to_sync(send_assignment_update)(tenant_schema, **payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning('assignment_update broadcast failed: %s', exc)

    if archived_change_to_unarchived:
        try:
            async_to_sync(send_archive_update)(
                tenant_schema,
                platform=payload['platform'],
                conversation_id=payload['conversation_id'],
                account_id=payload['account_id'],
                archived=False,
                archived_at=None,
                by_user_id=payload['by_user_id'],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning('archive_update broadcast failed: %s', exc)


def _broadcast_read_state(request, *, platform, conversation_id, account_id, unread_count, last_read_at=None):
    """Emit a `read_state_update` WS frame after a mark-read/unread mutation."""
    from django.db import connection
    from .consumers import send_read_state_update

    tenant_schema = connection.schema_name
    by_user_id = request.user.id if getattr(request, 'user', None) and request.user.is_authenticated else None
    try:
        async_to_sync(send_read_state_update)(
            tenant_schema,
            platform=platform,
            conversation_id=conversation_id,
            account_id=account_id,
            unread_count=unread_count,
            last_read_at=last_read_at.isoformat() if hasattr(last_read_at, 'isoformat') else last_read_at,
            by_user_id=by_user_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('read_state_update broadcast failed: %s', exc)


def _broadcast_archive_change(request, *, platform, conversation_id, account_id, archived, archived_at=None):
    """Emit an `archive_update` WS frame after archive/unarchive."""
    from django.db import connection
    from .consumers import send_archive_update

    tenant_schema = connection.schema_name
    by_user_id = request.user.id if getattr(request, 'user', None) and request.user.is_authenticated else None
    try:
        async_to_sync(send_archive_update)(
            tenant_schema,
            platform=platform,
            conversation_id=conversation_id,
            account_id=account_id,
            archived=archived,
            archived_at=archived_at.isoformat() if hasattr(archived_at, 'isoformat') else archived_at,
            by_user_id=by_user_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('archive_update broadcast failed: %s', exc)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def assign_chat(request):
    """Assign a chat to the current user"""
    serializer = ChatAssignmentCreateSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    # Check if assignment mode is enabled
    settings_obj = get_social_settings(request)
    if not settings_obj or not settings_obj.chat_assignment_enabled:
        return Response(
            {'error': 'Assignment mode is not enabled'},
            status=status.HTTP_400_BAD_REQUEST
        )

    platform = serializer.validated_data['platform']
    conversation_id = serializer.validated_data['conversation_id']
    account_id = serializer.validated_data['account_id']

    # Check if already assigned
    existing = ChatAssignment.objects.filter(
        platform=platform,
        conversation_id=conversation_id,
        account_id=account_id
    ).first()

    if existing:
        # If assignment is 'completed' (waiting for rating), delete it and allow new assignment
        # Rating is optional - users can start new sessions without waiting for rating
        if existing.status == 'completed':
            existing.delete()
            logger.info(f"Deleted completed assignment for new session: {platform}/{conversation_id}")
        elif existing.assigned_user == request.user:
            return Response({
                'message': 'Already assigned to you',
                'assignment': ChatAssignmentSerializer(existing).data
            })
        else:
            return Response(
                {'error': 'Chat is already assigned to another user'},
                status=status.HTTP_400_BAD_REQUEST
            )

    # Create assignment and automatically start session
    assignment = ChatAssignment.objects.create(
        platform=platform,
        conversation_id=conversation_id,
        account_id=account_id,
        assigned_user=request.user,
        status='in_session',
        session_started_at=timezone.now()
    )

    # Unarchive conversation if it was archived (assigning from history should reactivate)
    unarchived = ConversationArchive.objects.filter(
        platform=platform,
        conversation_id=conversation_id,
        account_id=account_id
    ).delete()[0]
    if unarchived:
        logger.info(f"📤 Unarchived conversation due to assignment: {platform}/{account_id}/{conversation_id}")

    logger.info(f"Chat assigned and session started: {assignment.full_conversation_id} -> {request.user.email}")
    _broadcast_assignment_change(request, assignment, archived_change_to_unarchived=bool(unarchived))
    return Response(ChatAssignmentSerializer(assignment).data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def unassign_chat(request):
    """Unassign a chat (only the assigned user or admin can do this)"""
    platform = request.data.get('platform')
    conversation_id = request.data.get('conversation_id')
    account_id = request.data.get('account_id')

    if not all([platform, conversation_id, account_id]):
        return Response(
            {'error': 'platform, conversation_id, and account_id are required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        assignment = ChatAssignment.objects.get(
            platform=platform,
            conversation_id=conversation_id,
            account_id=account_id
        )
    except ChatAssignment.DoesNotExist:
        return Response({'error': 'Assignment not found'}, status=status.HTTP_404_NOT_FOUND)

    # Only assigned user or admin can unassign
    if assignment.assigned_user != request.user and not request.user.is_staff and not request.user.is_superuser:
        return Response(
            {'error': 'You cannot unassign this chat'},
            status=status.HTTP_403_FORBIDDEN
        )

    logger.info(f"Chat unassigned: {assignment.full_conversation_id} by {request.user.email}")
    # Snapshot lookup keys before delete so the post-delete broadcast can
    # carry them to clients (the ORM row is gone after .delete()).
    snapshot = {
        'platform': assignment.platform,
        'conversation_id': assignment.conversation_id,
        'account_id': assignment.account_id,
        'status': None,
        'session_started_at': None,
        'session_ended_at': None,
    }
    assignment.delete()
    _broadcast_assignment_change(request, snapshot)
    return Response({'message': 'Chat unassigned successfully'})


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def transfer_chat(request):
    """Transfer a chat assignment to another user"""
    platform = request.data.get('platform')
    conversation_id = request.data.get('conversation_id')
    account_id = request.data.get('account_id')
    target_user_id = request.data.get('target_user_id')

    if not all([platform, conversation_id, account_id, target_user_id]):
        return Response(
            {'error': 'platform, conversation_id, account_id, and target_user_id are required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Validate target user exists and is active
    from users.models import User
    try:
        target_user = User.objects.get(id=target_user_id, is_active=True)
    except User.DoesNotExist:
        return Response({'error': 'Target user not found or inactive'}, status=status.HTTP_404_NOT_FOUND)

    # Cannot transfer to yourself
    if target_user == request.user:
        return Response({'error': 'Cannot transfer chat to yourself'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        assignment = ChatAssignment.objects.get(
            platform=platform,
            conversation_id=conversation_id,
            account_id=account_id
        )
    except ChatAssignment.DoesNotExist:
        # If no assignment exists, create one for the target user
        assignment = ChatAssignment.objects.create(
            platform=platform,
            conversation_id=conversation_id,
            account_id=account_id,
            assigned_user=target_user,
            status='in_session',
            session_started_at=timezone.now(),
        )
        logger.info(f"Chat transferred (new assignment): {assignment.full_conversation_id} to {target_user.email} by {request.user.email}")
        _broadcast_assignment_change(request, assignment)
        return Response({
            'message': f'Chat transferred to {target_user.get_full_name() or target_user.email}',
            'assignment': ChatAssignmentSerializer(assignment).data,
        })

    # Transfer the assignment
    previous_user = assignment.assigned_user
    assignment.assigned_user = target_user
    assignment.status = 'in_session'
    if not assignment.session_started_at:
        assignment.session_started_at = timezone.now()
    assignment.save()

    logger.info(
        f"Chat transferred: {assignment.full_conversation_id} "
        f"from {previous_user.email if previous_user else 'unassigned'} "
        f"to {target_user.email} by {request.user.email}"
    )
    _broadcast_assignment_change(request, assignment)

    return Response({
        'message': f'Chat transferred to {target_user.get_full_name() or target_user.email}',
        'assignment': ChatAssignmentSerializer(assignment).data,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def start_session(request):
    """Start a session for an assigned chat"""
    # Check if session management is enabled
    settings_obj = get_social_settings(request)
    if not settings_obj or not settings_obj.session_management_enabled:
        return Response(
            {'error': 'Session management is not enabled'},
            status=status.HTTP_400_BAD_REQUEST
        )

    platform = request.data.get('platform')
    conversation_id = request.data.get('conversation_id')
    account_id = request.data.get('account_id')

    if not all([platform, conversation_id, account_id]):
        return Response(
            {'error': 'platform, conversation_id, and account_id are required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        assignment = ChatAssignment.objects.get(
            platform=platform,
            conversation_id=conversation_id,
            account_id=account_id,
            assigned_user=request.user
        )
    except ChatAssignment.DoesNotExist:
        return Response(
            {'error': 'Assignment not found or not assigned to you'},
            status=status.HTTP_404_NOT_FOUND
        )

    if assignment.status == 'in_session':
        return Response({
            'message': 'Session already active',
            'assignment': ChatAssignmentSerializer(assignment).data
        })

    assignment.status = 'in_session'
    assignment.session_started_at = timezone.now()
    assignment.save()

    logger.info(f"Session started: {assignment.full_conversation_id} by {request.user.email}")
    _broadcast_assignment_change(request, assignment)
    return Response(ChatAssignmentSerializer(assignment).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def end_session(request):
    """End a session and optionally send rating request"""
    # Check if session management is enabled
    settings_obj = get_social_settings(request)
    if not settings_obj or not settings_obj.session_management_enabled:
        return Response(
            {'error': 'Session management is not enabled'},
            status=status.HTTP_400_BAD_REQUEST
        )

    platform = request.data.get('platform')
    conversation_id = request.data.get('conversation_id')
    account_id = request.data.get('account_id')

    if not all([platform, conversation_id, account_id]):
        return Response(
            {'error': 'platform, conversation_id, and account_id are required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        assignment = ChatAssignment.objects.get(
            platform=platform,
            conversation_id=conversation_id,
            account_id=account_id,
            status='in_session'
        )
    except ChatAssignment.DoesNotExist:
        return Response(
            {'error': 'Active session not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    # Only assigned user or admin/staff can end session
    if assignment.assigned_user != request.user and not request.user.is_staff and not request.user.is_superuser:
        return Response(
            {'error': 'You cannot end this session'},
            status=status.HTTP_403_FORBIDDEN
        )

    # Mark assignment as completed and unassign user
    # Setting status to 'completed' makes the chat available to everyone again
    assignment.status = 'completed'
    assignment.session_ended_at = timezone.now()
    assignment.assigned_user = None  # Explicitly unassign the user
    assignment.save()
    # Broadcast the unassign so every connected agent's beta sidebar moves
    # the conversation out of their Assigned tab back to All.
    _broadcast_assignment_change(request, assignment)

    # Check if customer rating collection is enabled
    collect_rating = settings_obj.collect_customer_rating if settings_obj else False
    link_based_rating = settings_obj.link_based_rating_enabled if settings_obj else False

    message_id = None
    rating_link = None

    if collect_rating:
        if link_based_rating:
            # Link-based rating: Generate token and send link
            from datetime import timedelta

            # Generate secure token
            rating_token = secrets.token_urlsafe(32)
            token_expires_at = timezone.now() + timedelta(days=7)

            # Build rating link using tenant subdomain
            host = request.get_host().split(':')[0]  # Remove port
            subdomain = host.split('.')[0] if '.' in host else 'app'
            # Use echodesk.ge domain for production, or current host for dev
            if 'localhost' in host or '127.0.0.1' in host:
                rating_link = f"http://{host}/review?token={rating_token}"
            else:
                rating_link = f"https://{subdomain}.{settings.MAIN_DOMAIN}/review?token={rating_token}"

            # Get message template (use Georgian by default)
            message_template = settings_obj.rating_request_message_template_ka
            rating_message = message_template.replace('{link}', rating_link)

            try:
                if platform == 'facebook':
                    message_id = send_rating_request_facebook(conversation_id, account_id, rating_message)
                elif platform == 'instagram':
                    message_id = send_rating_request_instagram(conversation_id, account_id, rating_message)
                elif platform == 'whatsapp':
                    message_id = send_rating_request_whatsapp(conversation_id, account_id, rating_message)
            except Exception as e:
                logger.error(f"Failed to send link-based rating request: {e}")

            # Create pending rating with token
            ChatRating.objects.create(
                assignment=assignment,
                rated_user=request.user,
                platform=platform,
                conversation_id=conversation_id,
                account_id=account_id,
                session_started_at=assignment.session_started_at,
                session_ended_at=assignment.session_ended_at,
                rating=0,  # Pending - waiting for customer response
                rating_request_message_id=message_id or '',
                rating_token=rating_token,
                token_expires_at=token_expires_at,
            )
            logger.info(f"Link-based rating request created for {platform} conversation {conversation_id}")

        else:
            # Traditional rating: Send message asking for 1-5 reply
            rating_message = "გმადლობთ ჩვენთან საუბრისთვის! გთხოვთ შეაფასოთ თქვენი გამოცდილება 1-დან 5-მდე (უპასუხეთ ციფრით)."

            try:
                if platform == 'facebook':
                    message_id = send_rating_request_facebook(conversation_id, account_id, rating_message)
                elif platform == 'instagram':
                    message_id = send_rating_request_instagram(conversation_id, account_id, rating_message)
                elif platform == 'whatsapp':
                    message_id = send_rating_request_whatsapp(conversation_id, account_id, rating_message)
            except Exception as e:
                logger.error(f"Failed to send rating request: {e}")

            # Create pending rating to track the response
            if message_id:
                ChatRating.objects.create(
                    assignment=assignment,
                    rated_user=request.user,
                    platform=platform,
                    conversation_id=conversation_id,
                    account_id=account_id,
                    session_started_at=assignment.session_started_at,
                    session_ended_at=assignment.session_ended_at,
                    rating=0,  # Pending - waiting for customer response
                    rating_request_message_id=message_id
                )

    # Archive the conversation (move to history)
    archive_obj, _archive_created = ConversationArchive.objects.get_or_create(
        platform=platform,
        conversation_id=conversation_id,
        account_id=account_id,
        defaults={'archived_by': request.user}
    )
    # Broadcast archive_update so /messages-beta clients move the chat into
    # History. Without this the only frame end_session emitted was the
    # unassign (assignment_update), which sent the chat back to the All tab
    # instead of History. Mirrors the archive view's broadcast.
    _broadcast_archive_change(
        request,
        platform=platform,
        conversation_id=conversation_id,
        account_id=account_id,
        archived=True,
        archived_at=getattr(archive_obj, 'archived_at', None) or timezone.now(),
    )

    # Widget-specific: close the underlying WidgetSession and broadcast
    # `session_ended` so the visitor's iframe flips to the post-chat
    # review form. The other platforms have no session-end concept on the
    # visitor side — agents just send messages and the user's app stays
    # the same — so this branch only matters for widget.
    if platform == 'widget':
        try:
            with schema_context(request.tenant.schema_name):
                widget_session = WidgetSession.objects.filter(
                    session_id=conversation_id
                ).first()
                if widget_session and widget_session.ended_at is None:
                    widget_session.ended_at = timezone.now()
                    widget_session.ended_by = 'agent'
                    widget_session.save(update_fields=['ended_at', 'ended_by'])
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            tenant_schema = getattr(request.tenant, 'schema_name', None)
            if channel_layer and tenant_schema and widget_session:
                async_to_sync(channel_layer.group_send)(
                    f'messages_{tenant_schema}',
                    {
                        'type': 'session_ended',
                        'session_id': conversation_id,
                        'connection_id': widget_session.connection_id,
                        'conversation_id': f'widget_{widget_session.connection_id}_{conversation_id}',
                        'ended_by': 'agent',
                        'ended_at': (widget_session.ended_at or timezone.now()).isoformat(),
                        'message': 'The agent has ended this conversation.',
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "end_session: failed to close widget session %s: %s",
                conversation_id, exc,
            )

    logger.info(f"Session ended: {assignment.full_conversation_id} by {request.user.email}")

    response_message = 'Session ended'
    if collect_rating:
        if link_based_rating:
            response_message = 'Session ended, rating link sent'
        elif message_id:
            response_message = 'Session ended, rating request sent'

    return Response({
        'message': response_message,
        'assignment': ChatAssignmentSerializer(assignment).data,
        'rating_link': rating_link if link_based_rating else None
    })


@extend_schema(
    summary="Get assignment and archive status for one conversation",
    description="""
    Returns the current chat assignment (or null), the tenant's assignment
    settings flags, and whether the conversation is archived (in History).
    Used by the messages UI to resolve the true state of a deep-linked chat.
    """,
    parameters=[
        OpenApiParameter(
            name='platform',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description='Platform (facebook, instagram, whatsapp, email, widget)',
            required=True,
        ),
        OpenApiParameter(
            name='conversation_id',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description='Conversation key within the account (e.g. sender PSID)',
            required=True,
        ),
        OpenApiParameter(
            name='account_id',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description='Account id (page id / WABA phone id / connection id)',
            required=True,
        ),
    ],
    responses={200: AssignmentStatusResponseSerializer},
    tags=['Social - Chat Assignment'],
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def get_assignment_status(request):
    """Get assignment status for a specific conversation"""
    platform = request.query_params.get('platform')
    conversation_id = request.query_params.get('conversation_id')
    account_id = request.query_params.get('account_id')

    if not all([platform, conversation_id, account_id]):
        return Response(
            {'error': 'platform, conversation_id, and account_id are required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Get settings to include in response
    settings_obj = get_social_settings(request)
    settings_data = {
        'chat_assignment_enabled': settings_obj.chat_assignment_enabled if settings_obj else False,
        'session_management_enabled': settings_obj.session_management_enabled if settings_obj else False,
        'hide_assigned_chats': settings_obj.hide_assigned_chats if settings_obj else False,
        'collect_customer_rating': settings_obj.collect_customer_rating if settings_obj else False,
    }

    archive_row = ConversationArchive.objects.filter(
        platform=platform,
        conversation_id=conversation_id,
        account_id=account_id,
    ).first()
    archive_data = {
        'is_archived': archive_row is not None,
        'archived_at': archive_row.archived_at if archive_row else None,
        'archived_by_id': archive_row.archived_by_id if archive_row else None,
    }

    try:
        assignment = ChatAssignment.objects.select_related('assigned_user').get(
            platform=platform,
            conversation_id=conversation_id,
            account_id=account_id
        )
        return Response({
            'assignment': ChatAssignmentSerializer(assignment).data,
            'settings': settings_data,
            **archive_data,
        })
    except ChatAssignment.DoesNotExist:
        return Response({
            'assignment': None,
            'settings': settings_data,
            **archive_data,
        })


def send_rating_request_facebook(conversation_id, page_id, message):
    """Send a rating request via Facebook Messenger and store locally"""
    from .consumers import send_new_message_notification
    from django.db import connection

    try:
        # Try active connection first, then fall back to any connection with this page_id
        page = FacebookPageConnection.objects.filter(page_id=page_id, is_active=True).first()
        if not page:
            page = FacebookPageConnection.objects.filter(page_id=page_id).first()
        if not page:
            logger.warning(f"No FacebookPageConnection found for page_id={page_id}, skipping rating request")
            return None
        url = f"https://graph.facebook.com/v21.0/{page_id}/messages"
        payload = {
            "recipient": {"id": conversation_id},
            "message": {"text": message}
        }
        headers = {"Authorization": f"Bearer {page.page_access_token}"}
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.ok:
            message_id = response.json().get('message_id')
            timestamp = datetime.now()
            # Store the message locally so it appears in the chat
            try:
                fb_message = FacebookMessage.objects.create(
                    page_connection=page,
                    message_id=message_id or f"rating_{timestamp.timestamp()}",
                    sender_id=conversation_id,
                    sender_name=page.page_name,
                    message_text=message,
                    timestamp=timestamp,
                    is_from_page=True,
                    is_echo=False,
                )
                # Broadcast via WebSocket
                tenant_schema = connection.schema_name
                message_data = {
                    'id': fb_message.id,
                    'message_id': fb_message.message_id,
                    'platform': 'facebook',
                    'sender_id': conversation_id,
                    'sender_name': page.page_name,
                    'message_text': message,
                    'timestamp': timestamp.isoformat(),
                    'is_from_page': True,
                    'page_id': page_id,
                }
                async_to_sync(send_new_message_notification)(tenant_schema, conversation_id, message_data)
            except Exception as e:
                logger.warning(f"Failed to save/broadcast rating message: {e}")
            return message_id
    except Exception as e:
        logger.error(f"Failed to send FB rating request: {e}")
    return None


def send_rating_request_instagram(conversation_id, account_id, message):
    """Send a rating request via Instagram DM and store locally"""
    from .consumers import send_new_message_notification
    from django.db import connection

    try:
        account = InstagramAccountConnection.objects.get(instagram_account_id=account_id, is_active=True)
        if not account.facebook_page:
            logger.error(f"Instagram account {account_id} is not linked to a Facebook Page")
            return None
        # Instagram messages must be sent through the Facebook Page's Messenger Platform API
        url = f"https://graph.facebook.com/v23.0/me/messages"
        payload = {
            "recipient": {"id": conversation_id},
            "message": {"text": message},
            "messaging_type": "RESPONSE"
        }
        headers = {"Content-Type": "application/json"}
        params = {
            "access_token": account.facebook_page.page_access_token,
            "platform": "instagram"
        }
        response = requests.post(url, json=payload, headers=headers, params=params, timeout=30)
        if response.ok:
            message_id = response.json().get('message_id')
            timestamp = datetime.now()
            # Store the message locally so it appears in the chat
            try:
                ig_message = InstagramMessage.objects.create(
                    account_connection=account,
                    message_id=message_id or f"rating_{timestamp.timestamp()}",
                    sender_id=conversation_id,
                    sender_username=account.username or account.instagram_account_id,
                    message_text=message,
                    timestamp=timestamp,
                    is_from_business=True
                )
                # Broadcast via WebSocket
                tenant_schema = connection.schema_name
                message_data = {
                    'id': ig_message.id,
                    'message_id': ig_message.message_id,
                    'platform': 'instagram',
                    'sender_id': conversation_id,
                    'sender_username': account.username or account.instagram_account_id,
                    'message_text': message,
                    'timestamp': timestamp.isoformat(),
                    'is_from_business': True,
                    'account_id': account_id,
                }
                async_to_sync(send_new_message_notification)(tenant_schema, conversation_id, message_data)
            except Exception as e:
                logger.warning(f"Failed to save/broadcast rating message: {e}")
            return message_id
    except Exception as e:
        logger.error(f"Failed to send IG rating request: {e}")
    return None


def send_rating_request_whatsapp(conversation_id, waba_id, message):
    """Send a rating request via WhatsApp and store locally"""
    from .consumers import send_new_message_notification
    from django.db import connection

    try:
        account = WhatsAppBusinessAccount.objects.get(waba_id=waba_id, is_active=True)
        # Format phone number (add + if not present)
        to_number = conversation_id if conversation_id.startswith('+') else f"+{conversation_id}"
        url = f"https://graph.facebook.com/v21.0/{account.phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "text",
            "text": {"body": message}
        }
        headers = {
            "Authorization": f"Bearer {account.access_token}",
            "Content-Type": "application/json"
        }
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.ok:
            data = response.json()
            message_id = data.get('messages', [{}])[0].get('id')
            timestamp = datetime.now()
            # Store the message locally so it appears in the chat
            try:
                wa_message = WhatsAppMessage.objects.create(
                    business_account=account,
                    message_id=message_id or f"rating_{timestamp.timestamp()}",
                    from_number=account.display_phone_number or account.phone_number_id,
                    to_number=to_number,
                    message_text=message,
                    message_type='text',
                    timestamp=timestamp,
                    is_from_business=True
                )
                # Broadcast via WebSocket
                tenant_schema = connection.schema_name
                message_data = {
                    'id': wa_message.id,
                    'message_id': wa_message.message_id,
                    'platform': 'whatsapp',
                    'from_number': account.display_phone_number or account.phone_number_id,
                    'to_number': to_number,
                    'message_text': message,
                    'timestamp': timestamp.isoformat(),
                    'is_from_business': True,
                    'waba_id': waba_id,
                }
                async_to_sync(send_new_message_notification)(tenant_schema, to_number, message_data)
            except Exception as e:
                logger.warning(f"Failed to save/broadcast rating message: {e}")
            return message_id
    except Exception as e:
        logger.error(f"Failed to send WA rating request: {e}")
    return None


def process_potential_rating_response(message_text, platform, conversation_id, account_id, message_id=None):
    """
    Check if an incoming message is a rating response (1-5).
    If a pending rating exists for this conversation, store the rating.
    Returns True if the message was a rating, False otherwise.
    """
    # Only process if it's a single digit 1-5
    text = (message_text or '').strip()
    if text not in ['1', '2', '3', '4', '5']:
        return False

    rating_value = int(text)

    try:
        # Find pending rating directly by conversation info (rating=0 means waiting for response)
        # This works even after assignment is deleted because we store conversation info directly
        rating = ChatRating.objects.filter(
            platform=platform,
            conversation_id=conversation_id,
            account_id=account_id,
            rating=0
        ).first()

        if not rating:
            return False

        # Get the assignment if it still exists (for deletion later)
        assignment = rating.assignment

        # Update the rating with the customer's response
        rating.rating = rating_value
        if message_id:
            rating.rating_response_message_id = message_id
        rating.save()

        logger.info(f"Rating received: {rating_value}/5 for {platform} conversation {conversation_id} (user: {rating.rated_user})")

        # Send thank you message in Georgian
        thank_you_message = f"გმადლობთ თქვენი გამოხმაურებისთვის! თქვენ შეაფასეთ ჩვენი სერვისი {rating_value}/5."
        try:
            if platform == 'facebook':
                send_rating_request_facebook(conversation_id, account_id, thank_you_message)
            elif platform == 'instagram':
                send_rating_request_instagram(conversation_id, account_id, thank_you_message)
            elif platform == 'whatsapp':
                send_rating_request_whatsapp(conversation_id, account_id, thank_you_message)
        except Exception as e:
            logger.warning(f"Failed to send thank you message: {e}")

        # Delete the assignment after rating is received (chat becomes available again)
        if assignment:
            assignment.delete()
            logger.info(f"Assignment deleted after rating received for {platform} conversation {conversation_id}")

        return True

    except Exception as e:
        logger.error(f"Error processing rating response: {e}")

    return False


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsStaffUser])
def rating_statistics(request):
    """
    Get rating statistics for all users.
    Superadmin only - returns aggregated stats per user.
    Supports date filtering via start_date and end_date query params.
    Default: current month (1st to today).
    Supports source filtering via source query param: all (default), social, calls.
    """
    from crm.models import CallRating

    # Parse date filters (default: this month)
    from datetime import date
    today = date.today()
    first_of_month = today.replace(day=1)

    start_date_str = request.query_params.get('start_date')
    end_date_str = request.query_params.get('end_date')
    source = request.query_params.get('source', 'all')  # all, social, calls

    try:
        if start_date_str:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        else:
            start_date = first_of_month

        if end_date_str:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        else:
            end_date = today
    except ValueError:
        return Response(
            {'error': 'Invalid date format. Use YYYY-MM-DD'},
            status=status.HTTP_400_BAD_REQUEST
        )

    if source not in ('all', 'social', 'calls'):
        return Response(
            {'error': "Invalid source. Use 'all', 'social', or 'calls'"},
            status=status.HTTP_400_BAD_REQUEST
        )

    from django.db.models import Avg, Count, Sum, Q
    from django.db.models.functions import Coalesce

    # --- Query ChatRating (social) ---
    include_social = source in ('all', 'social')
    include_calls = source in ('all', 'calls')

    social_ratings = None
    if include_social:
        social_ratings = ChatRating.objects.filter(
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
            rating__gt=0,
        ).select_related('rated_user')

    # --- Query CallRating (calls) ---
    call_ratings = None
    if include_calls:
        call_ratings = CallRating.objects.filter(
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
            rating__gt=0,
        ).select_related('rated_user')

    # --- Overall stats with by_source breakdown ---
    social_overall = {'total': 0, 'average': 0}
    calls_callback_overall = {'total': 0, 'average': 0}
    calls_sms_overall = {'total': 0, 'average': 0}

    if social_ratings is not None:
        agg = social_ratings.aggregate(total=Count('id'), average=Avg('rating'))
        social_overall = {
            'total': agg['total'] or 0,
            'average': round(agg['average'], 2) if agg['average'] else 0,
        }

    if call_ratings is not None:
        cb_agg = call_ratings.filter(review_method='callback').aggregate(total=Count('id'), average=Avg('rating'))
        calls_callback_overall = {
            'total': cb_agg['total'] or 0,
            'average': round(cb_agg['average'], 2) if cb_agg['average'] else 0,
        }
        sms_agg = call_ratings.filter(review_method='sms').aggregate(total=Count('id'), average=Avg('rating'))
        calls_sms_overall = {
            'total': sms_agg['total'] or 0,
            'average': round(sms_agg['average'], 2) if sms_agg['average'] else 0,
        }

    combined_total = social_overall['total'] + calls_callback_overall['total'] + calls_sms_overall['total']
    if combined_total > 0:
        combined_sum = (
            social_overall['average'] * social_overall['total']
            + calls_callback_overall['average'] * calls_callback_overall['total']
            + calls_sms_overall['average'] * calls_sms_overall['total']
        )
        combined_average = round(combined_sum / combined_total, 2)
    else:
        combined_average = 0

    # --- Per-user stats ---
    # Build a dict keyed by user_id with merged data
    user_map = {}  # user_id -> { ...stats }

    if social_ratings is not None:
        social_user_stats = social_ratings.values(
            'rated_user__id',
            'rated_user__email',
            'rated_user__first_name',
            'rated_user__last_name',
        ).annotate(
            total_ratings=Count('id'),
            average_rating=Avg('rating'),
            rating_1=Count('id', filter=Q(rating=1)),
            rating_2=Count('id', filter=Q(rating=2)),
            rating_3=Count('id', filter=Q(rating=3)),
            rating_4=Count('id', filter=Q(rating=4)),
            rating_5=Count('id', filter=Q(rating=5)),
        )
        for stat in social_user_stats:
            uid = stat['rated_user__id']
            if not uid:
                continue
            user_map[uid] = {
                'user_id': uid,
                'email': stat['rated_user__email'],
                'name': f"{stat['rated_user__first_name'] or ''} {stat['rated_user__last_name'] or ''}".strip() or stat['rated_user__email'],
                'total_ratings': stat['total_ratings'],
                'rating_sum': (stat['average_rating'] or 0) * stat['total_ratings'],
                'rating_breakdown': {
                    '1': stat['rating_1'],
                    '2': stat['rating_2'],
                    '3': stat['rating_3'],
                    '4': stat['rating_4'],
                    '5': stat['rating_5'],
                },
                'social_ratings': stat['total_ratings'],
                'call_ratings': 0,
            }

    if call_ratings is not None:
        call_user_stats = call_ratings.values(
            'rated_user__id',
            'rated_user__email',
            'rated_user__first_name',
            'rated_user__last_name',
        ).annotate(
            total_ratings=Count('id'),
            average_rating=Avg('rating'),
            rating_1=Count('id', filter=Q(rating=1)),
            rating_2=Count('id', filter=Q(rating=2)),
            rating_3=Count('id', filter=Q(rating=3)),
            rating_4=Count('id', filter=Q(rating=4)),
            rating_5=Count('id', filter=Q(rating=5)),
        )
        for stat in call_user_stats:
            uid = stat['rated_user__id']
            if not uid:
                continue
            if uid in user_map:
                existing = user_map[uid]
                existing['total_ratings'] += stat['total_ratings']
                existing['rating_sum'] += (stat['average_rating'] or 0) * stat['total_ratings']
                existing['rating_breakdown']['1'] += stat['rating_1']
                existing['rating_breakdown']['2'] += stat['rating_2']
                existing['rating_breakdown']['3'] += stat['rating_3']
                existing['rating_breakdown']['4'] += stat['rating_4']
                existing['rating_breakdown']['5'] += stat['rating_5']
                existing['call_ratings'] += stat['total_ratings']
            else:
                # Need user info — fetch email/name from annotated values
                user_map[uid] = {
                    'user_id': uid,
                    'email': stat['rated_user__email'],
                    'name': f"{stat['rated_user__first_name'] or ''} {stat['rated_user__last_name'] or ''}".strip() or stat['rated_user__email'],
                    'total_ratings': stat['total_ratings'],
                    'rating_sum': (stat['average_rating'] or 0) * stat['total_ratings'],
                    'rating_breakdown': {
                        '1': stat['rating_1'],
                        '2': stat['rating_2'],
                        '3': stat['rating_3'],
                        '4': stat['rating_4'],
                        '5': stat['rating_5'],
                    },
                    'social_ratings': 0,
                    'call_ratings': stat['total_ratings'],
                }

    # Finalize: compute average from sum/total and sort
    stats_list = []
    for entry in user_map.values():
        total = entry['total_ratings']
        avg = round(entry['rating_sum'] / total, 2) if total > 0 else 0
        stats_list.append({
            'user_id': entry['user_id'],
            'email': entry['email'],
            'name': entry['name'],
            'total_ratings': total,
            'average_rating': avg,
            'rating_breakdown': entry['rating_breakdown'],
            'social_ratings': entry['social_ratings'],
            'call_ratings': entry['call_ratings'],
        })

    stats_list.sort(key=lambda x: (-x['average_rating'], -x['total_ratings']))

    return Response({
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'overall': {
            'total_ratings': combined_total,
            'average_rating': combined_average,
            'by_source': {
                'social': social_overall,
                'calls_callback': calls_callback_overall,
                'calls_sms': calls_sms_overall,
            },
        },
        'users': stats_list
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsStaffUser])
def user_rating_sessions(request, user_id):
    """
    Get detailed rating sessions for a specific user.
    Includes both ChatRating (social) and CallRating (phone) sessions.
    Superadmin only - allows investigating chat/call history.
    Supports source filtering via source query param: all (default), social, calls.
    """
    from crm.models import CallRating

    # Get date range from query params
    start_date_str = request.query_params.get('start_date')
    end_date_str = request.query_params.get('end_date')
    source = request.query_params.get('source', 'all')  # all, social, calls

    # Default to current month
    today = timezone.now().date()
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        except ValueError:
            start_date = today.replace(day=1)
    else:
        start_date = today.replace(day=1)

    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except ValueError:
            end_date = today
    else:
        end_date = today

    sessions = []

    # --- Social (ChatRating) sessions ---
    if source in ('all', 'social'):
        chat_ratings = ChatRating.objects.filter(
            rated_user_id=user_id,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        ).order_by('-created_at')

        for rating in chat_ratings:
            # Try to get customer info from the conversation
            customer_name = None
            customer_identifier = rating.conversation_id

            if rating.platform == 'facebook':
                msg = FacebookMessage.objects.filter(
                    sender_id=rating.conversation_id,
                    is_from_page=False
                ).first()
                if msg and msg.sender_name:
                    customer_name = msg.sender_name
            elif rating.platform == 'instagram':
                msg = InstagramMessage.objects.filter(
                    sender_id=rating.conversation_id,
                    is_from_business=False
                ).first()
                if msg and msg.sender_username:
                    customer_name = msg.sender_username
            elif rating.platform == 'whatsapp':
                msg = WhatsAppMessage.objects.filter(
                    from_number=rating.conversation_id,
                    is_from_business=False
                ).first()
                if msg and msg.contact_name:
                    customer_name = msg.contact_name
            elif rating.platform == 'widget':
                # Widget conversation_id is `widget_<conn>_<session_id>`.
                # Pull the visitor name off the WidgetSession so the table
                # shows "Lika" instead of the raw composite identifier.
                # Mirrors the conventions in the conversations list
                # (views.py around line 5759).
                parts = (rating.conversation_id or '').split('_', 2)
                widget_session_id = parts[2] if len(parts) >= 3 else None
                if widget_session_id:
                    ws = WidgetSession.objects.filter(
                        session_id=widget_session_id
                    ).first()
                    if ws:
                        customer_name = (
                            ws.visitor_name
                            or f"Website visitor {ws.visitor_id[:6]}"
                        )

            # Check if conversation is archived
            is_archived = ConversationArchive.objects.filter(
                platform=rating.platform,
                conversation_id=rating.conversation_id,
                account_id=rating.account_id,
            ).exists()

            sessions.append({
                'id': rating.id,
                'platform': rating.platform,
                'conversation_id': rating.conversation_id,
                'account_id': rating.account_id,
                'customer_name': customer_name or customer_identifier,
                'rating': rating.rating if rating.rating > 0 else None,
                'comment': rating.comment or '',
                'session_started_at': rating.session_started_at.isoformat() if rating.session_started_at else None,
                'session_ended_at': rating.session_ended_at.isoformat() if rating.session_ended_at else None,
                'created_at': rating.created_at.isoformat(),
                'is_archived': is_archived,
            })

    # --- Call (CallRating) sessions ---
    if source in ('all', 'calls'):
        call_ratings = CallRating.objects.filter(
            rated_user_id=user_id,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
            rating__gt=0,
        ).select_related('call_log').order_by('-created_at')

        for rating in call_ratings:
            sessions.append({
                'id': rating.id,
                'platform': 'phone_' + rating.review_method,  # phone_sms or phone_callback
                'conversation_id': rating.caller_number,
                'account_id': None,
                'customer_name': rating.caller_number,
                'rating': rating.rating,
                'comment': rating.comment or '',
                'session_started_at': rating.call_log.started_at.isoformat() if rating.call_log and rating.call_log.started_at else None,
                'session_ended_at': rating.call_log.ended_at.isoformat() if rating.call_log and rating.call_log.ended_at else None,
                'created_at': rating.created_at.isoformat(),
                'is_archived': False,
            })

    # Sort combined sessions by created_at descending
    sessions.sort(key=lambda s: s['created_at'], reverse=True)

    # Get user info
    from users.models import User
    try:
        user = User.objects.get(id=user_id)
        user_info = {
            'id': user.id,
            'email': user.email,
            'name': f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email,
        }
    except User.DoesNotExist:
        user_info = {'id': user_id, 'email': 'Unknown', 'name': 'Unknown User'}

    return Response({
        'user': user_info,
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'sessions': sessions,
        'total_sessions': len(sessions),
    })


# Backward-compatible alias
user_chat_sessions = user_rating_sessions


# =============================================================================
# PUBLIC RATING ENDPOINTS (No authentication required)
# =============================================================================

@api_view(['GET'])
@permission_classes([AllowAny])
def get_rating_info(request, token):
    """
    Validate rating token and return tenant info.
    Public endpoint - no authentication required.
    """
    from django.db import connection
    from datetime import timedelta

    # Get tenant from subdomain
    host = request.get_host().split(':')[0]  # Remove port
    subdomain = host.split('.')[0] if '.' in host else None

    if not subdomain:
        return Response({
            'valid': False,
            'error': 'Invalid request'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Look up the rating by token in the tenant's schema
    try:
        from tenant_schemas.utils import schema_context
        from tenants.models import Tenant as TenantClient

        # Find the tenant by subdomain
        try:
            tenant = TenantClient.objects.get(schema_name=subdomain)
        except TenantClient.DoesNotExist:
            return Response({
                'valid': False,
                'error': 'Tenant not found'
            }, status=status.HTTP_404_NOT_FOUND)

        # Switch to tenant schema and look up the rating
        with schema_context(tenant.schema_name):
            rating = ChatRating.objects.filter(rating_token=token).first()
            if not rating:
                # Fallback: try CallRating for phone-based reviews
                from crm.models import CallRating
                rating = CallRating.objects.filter(rating_token=token).first()

            if not rating:
                return Response({
                    'valid': False,
                    'error': 'Invalid token'
                }, status=status.HTTP_404_NOT_FOUND)

            # Check if token is expired
            if rating.token_expires_at and rating.token_expires_at < timezone.now():
                return Response({
                    'valid': False,
                    'expired': True,
                    'error': 'Token has expired'
                }, status=status.HTTP_410_GONE)

            # Check if already rated
            if rating.rating > 0:
                return Response({
                    'valid': False,
                    'already_rated': True,
                    'error': 'Already rated'
                }, status=status.HTTP_409_CONFLICT)

            # Build logo URL if tenant has a logo
            logo_url = None
            if hasattr(tenant, 'logo') and tenant.logo:
                try:
                    logo_url = request.build_absolute_uri(tenant.logo.url)
                except Exception:
                    pass

            return Response({
                'valid': True,
                'expired': False,
                'already_rated': False,
                'tenant_name': tenant.name if hasattr(tenant, 'name') else subdomain,
                'tenant_logo_url': logo_url,
            })

    except Exception as e:
        logger.error(f"Error validating rating token: {e}")
        return Response({
            'valid': False,
            'error': 'Server error'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([AllowAny])
def submit_public_rating(request, token):
    """
    Submit a public rating.
    Public endpoint - no authentication required.
    Accepts rating (1, 3, or 5) and optional comment.
    """
    # Validate input
    serializer = PublicRatingSubmitSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    rating_value = serializer.validated_data['rating']
    comment = serializer.validated_data.get('comment', '')

    # Limit comment length
    if len(comment) > 1000:
        return Response({
            'error': 'Comment too long (max 1000 characters)'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Get tenant from subdomain
    host = request.get_host().split(':')[0]  # Remove port
    subdomain = host.split('.')[0] if '.' in host else None

    if not subdomain:
        return Response({
            'success': False,
            'error': 'Invalid request'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        from tenant_schemas.utils import schema_context
        from tenants.models import Tenant as TenantClient

        # Find the tenant by subdomain
        try:
            tenant = TenantClient.objects.get(schema_name=subdomain)
        except TenantClient.DoesNotExist:
            return Response({
                'success': False,
                'error': 'Tenant not found'
            }, status=status.HTTP_404_NOT_FOUND)

        # Switch to tenant schema and submit the rating
        with schema_context(tenant.schema_name):
            is_call_rating = False
            rating = ChatRating.objects.filter(rating_token=token).first()
            if not rating:
                # Fallback: try CallRating for phone-based reviews
                from crm.models import CallRating
                rating = CallRating.objects.filter(rating_token=token).first()
                if rating:
                    is_call_rating = True

            if not rating:
                return Response({
                    'success': False,
                    'error': 'Invalid token'
                }, status=status.HTTP_404_NOT_FOUND)

            # Check if token is expired
            if rating.token_expires_at and rating.token_expires_at < timezone.now():
                return Response({
                    'success': False,
                    'error': 'Token has expired'
                }, status=status.HTTP_410_GONE)

            # Check if already rated
            if rating.rating > 0:
                return Response({
                    'success': False,
                    'error': 'Already rated'
                }, status=status.HTTP_409_CONFLICT)

            # Update the rating
            rating.rating = rating_value
            rating.comment = comment
            # Clear the token after use (single-use)
            rating.rating_token = None
            if is_call_rating:
                rating.rated_at = timezone.now()
                rating.save()
                # Also update call_quality_score on the related call log
                if rating.call_log:
                    rating.call_log.call_quality_score = float(rating_value)
                    rating.call_log.save(update_fields=['call_quality_score'])
            else:
                rating.save()

            # Delete the assignment if it exists (chat becomes available again)
            if not is_call_rating and rating.assignment:
                rating.assignment.delete()
                logger.info(f"Assignment deleted after link-based rating for {rating.platform} conversation {rating.conversation_id}")

            # Get redirect URL from settings
            redirect_url = ''
            try:
                settings_obj = get_social_settings(request)
                if settings_obj and settings_obj.post_review_redirect_url:
                    redirect_url = settings_obj.post_review_redirect_url
            except Exception:
                pass

            return Response({
                'success': True,
                'message': 'Thank you for your feedback!',
                'redirect_url': redirect_url,
            })

    except Exception as e:
        logger.error(f"Error submitting public rating: {e}")
        return Response({
            'success': False,
            'error': 'Server error'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(
    summary="Get unified conversations from all social platforms",
    description="""
    Returns paginated list of conversations from Facebook, Instagram, WhatsApp, and Email.
    Each conversation includes sender info, last message, unread count, and account details.
    Supports search by sender name and message text, and filtering by platform.
    """,
    parameters=[
        OpenApiParameter(
            name='platforms',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description='Comma-separated list of platforms to include (facebook,instagram,whatsapp,email)',
            required=False,
        ),
        OpenApiParameter(
            name='search',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description='Search query (searches sender_name and message_text)',
            required=False,
        ),
        OpenApiParameter(
            name='page',
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            description='Page number (default: 1)',
            required=False,
        ),
        OpenApiParameter(
            name='page_size',
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            description='Results per page (default: 50, max: 200)',
            required=False,
        ),
        OpenApiParameter(
            name='folder',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description='Email folder filter (default: INBOX)',
            required=False,
        ),
    ],
    responses={200: PaginatedUnifiedConversationSerializer},
    tags=['Social - Conversations'],
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def unified_conversations(request):
    """
    Get unified conversations from all social platforms (Facebook, Instagram, WhatsApp, Email).

    Returns paginated list of conversations with:
    - conversation_id: Unique identifier (e.g., fb_pageId_senderId, ig_accountId_senderId)
    - platform: facebook, instagram, whatsapp, or email
    - sender_id: Customer identifier (sender_id, from_number, email address)
    - sender_name: Customer name
    - profile_pic_url: Customer avatar URL
    - last_message: Most recent message details
    - message_count: Total messages in conversation
    - unread_count: Unread messages from customer
    - account_name: Business account name
    - account_id: Business account identifier

    Query parameters:
    - platforms: Comma-separated list of platforms (default: all)
    - search: Search query (searches sender_name and message_text)
    - page: Page number (default: 1)
    - page_size: Results per page (default: 50, max: 200)
    - folder: Email folder filter (default: INBOX)
    - assigned: If true, only return conversations assigned to the current user
    - archived: If true, only return archived conversations (history)
    - connection_id: Filter emails by specific email connection ID
    """
    # Parse query parameters
    platforms_param = request.query_params.get('platforms', 'facebook,instagram,whatsapp,email,widget')
    enabled_platforms = [p.strip().lower() for p in platforms_param.split(',')]
    search_query = request.query_params.get('search', '').strip().lower()
    page = int(request.query_params.get('page', 1))
    page_size = min(int(request.query_params.get('page_size', 50)), 200)
    email_folder = request.query_params.get('folder', 'INBOX')
    assigned_only = request.query_params.get('assigned', '').lower() == 'true'
    archived_only = request.query_params.get('archived', '').lower() == 'true'
    # Email connection filter (for switching between email accounts)
    connection_id_param = request.query_params.get('connection_id')
    email_connection_id = int(connection_id_param) if connection_id_param else None

    all_conversations = []

    # Check assignment settings
    settings_obj = get_social_settings(request)
    hide_assigned = settings_obj and settings_obj.hide_assigned_chats
    is_admin = request.user.is_superuser or request.user.is_staff

    # Load archived conversation keys for filtering — bound to the platforms
    # actually being aggregated so we don't pull every archive row in the
    # tenant on each request.
    archived_conversations = set(
        ConversationArchive.objects
        .filter(platform__in=enabled_platforms)
        .values_list('platform', 'conversation_id', 'account_id')
    )

    # Helper to check if conversation is archived
    def is_conversation_archived(platform, account_id, conversation_id):
        return (platform, conversation_id, account_id) in archived_conversations

    # Get all assignments for current user if filtering by assigned
    user_assignments = set()
    if assigned_only:
        assignments = ChatAssignment.objects.filter(
            assigned_user=request.user,
            status__in=['active', 'in_session']
        )
        for a in assignments:
            # Create a tuple key: (platform, account_id, conversation_id)
            user_assignments.add((a.platform, a.account_id, a.conversation_id))

    # Helper to check if conversation is assigned to current user
    def is_assigned_to_user(platform, account_id, conversation_id):
        return (platform, account_id, conversation_id) in user_assignments

    # Pre-load all active assignments for hide_assigned visibility check
    # Maps (platform, account_id, conversation_id) -> assigned_user_id.
    # Narrowed to visible platforms so the scan is bounded.
    _active_assignment_map = {}
    if hide_assigned and not is_admin:
        for a in ChatAssignment.objects.filter(
            status__in=['active', 'in_session'],
            platform__in=enabled_platforms,
        ).values_list('platform', 'account_id', 'conversation_id', 'assigned_user_id'):
            _active_assignment_map[(a[0], a[1], a[2])] = a[3]

    # Helper to check if conversation should be visible based on assignment and archive status
    def is_conversation_visible(platform, account_id, conversation_id):
        # Check archive status first
        is_archived = is_conversation_archived(platform, account_id, conversation_id)
        if archived_only:
            # Only show archived conversations
            if not is_archived:
                return False
        else:
            # Hide archived conversations from main list
            if is_archived:
                return False
        # If filtering by assigned, only show assigned conversations
        if assigned_only:
            return is_assigned_to_user(platform, account_id, conversation_id)

        if not hide_assigned or is_admin:
            return True

        # Check pre-loaded assignment map
        key = (platform, account_id, conversation_id)
        assigned_user_id = _active_assignment_map.get(key)

        if assigned_user_id is None:
            return True  # Not assigned, visible to all

        return assigned_user_id == request.user.id  # Only visible if assigned to current user

    # Load Facebook conversations (optimized - single DISTINCT ON query)
    if 'facebook' in enabled_platforms:
        try:
            pages = FacebookPageConnection.objects.all()
            for page_conn in pages:
                base_filter = {
                    'page_connection': page_conn,
                    'is_deleted': False,
                }

                # Get latest message per sender using DISTINCT ON (single efficient query)
                latest_messages_qs = FacebookMessage.objects.filter(
                    **base_filter
                ).order_by('sender_id', '-timestamp').distinct('sender_id')

                # Apply search filter if provided
                if search_query:
                    # Get sender_ids that match search
                    matching_sender_ids = FacebookMessage.objects.filter(
                        **base_filter
                    ).filter(
                        Q(sender_name__icontains=search_query) |
                        Q(message_text__icontains=search_query)
                    ).values_list('sender_id', flat=True).distinct()
                    latest_messages_qs = latest_messages_qs.filter(sender_id__in=matching_sender_ids)

                # Get all latest messages (these are already unique per sender)
                latest_messages = list(latest_messages_qs)
                if not latest_messages:
                    continue

                sender_ids = [msg.sender_id for msg in latest_messages]

                # Batch fetch unread counts only (skip msg_count for speed)
                unread_counts = {
                    item['sender_id']: item['unread_count']
                    for item in FacebookMessage.objects.filter(
                        **base_filter,
                        sender_id__in=sender_ids,
                        is_from_page=False,
                        is_read_by_staff=False
                    ).values('sender_id').annotate(unread_count=Count('id'))
                }

                # Batch fetch customer info (latest incoming message for name/avatar)
                customer_messages = FacebookMessage.objects.filter(
                    **base_filter,
                    sender_id__in=sender_ids,
                    is_from_page=False
                ).order_by('sender_id', '-timestamp').distinct('sender_id')
                customer_msg_map = {msg.sender_id: msg for msg in customer_messages}

                # Batch fetch SocialAccount info for customers without incoming messages
                social_accounts = SocialAccount.objects.filter(
                    platform='facebook',
                    platform_id__in=sender_ids,
                    account_connection_id=page_conn.page_id
                )
                social_account_map = {sa.platform_id: sa for sa in social_accounts}

                # Build conversations
                for msg in latest_messages:
                    sender_id = msg.sender_id
                    if not is_conversation_visible('facebook', page_conn.page_id, sender_id):
                        continue

                    customer_msg = customer_msg_map.get(sender_id)
                    social_account = social_account_map.get(sender_id)

                    # Get customer name: prefer incoming message, then social account, then fallback
                    if customer_msg and customer_msg.sender_name:
                        customer_name = customer_msg.sender_name
                        customer_pic = customer_msg.profile_pic_url
                    elif social_account and social_account.display_name:
                        customer_name = social_account.display_name
                        customer_pic = social_account.profile_pic_url
                    else:
                        customer_name = f"Facebook User {sender_id}"
                        customer_pic = None

                    conversation = {
                        'conversation_id': f"fb_{page_conn.page_id}_{sender_id}",
                        'platform': 'facebook',
                        'sender_id': sender_id,
                        'sender_name': customer_name,
                        'profile_pic_url': customer_pic,
                        'last_message': {
                            'id': str(msg.id),
                            'text': msg.message_text,
                            'timestamp': msg.timestamp,
                            'is_from_business': msg.is_from_page,
                            'attachment_type': msg.attachment_type,
                            'platform_message_id': msg.message_id,
                        },
                        'message_count': 0,  # Skip for performance (not needed for list view)
                        'unread_count': unread_counts.get(sender_id, 0),
                        'account_name': page_conn.page_name,
                        'account_id': page_conn.page_id,
                    }
                    all_conversations.append(conversation)
        except Exception as e:
            logger.error(f"Error loading Facebook conversations: {e}")

    # Load Instagram conversations (optimized - single DISTINCT ON query)
    if 'instagram' in enabled_platforms:
        try:
            accounts = InstagramAccountConnection.objects.all()
            for account in accounts:
                base_filter = {
                    'account_connection': account,
                    'is_deleted': False,
                }

                # Get latest message per sender using DISTINCT ON
                latest_messages_qs = InstagramMessage.objects.filter(
                    **base_filter
                ).order_by('sender_id', '-timestamp').distinct('sender_id')

                # Apply search filter if provided
                if search_query:
                    matching_sender_ids = InstagramMessage.objects.filter(
                        **base_filter
                    ).filter(
                        Q(sender_name__icontains=search_query) |
                        Q(sender_username__icontains=search_query) |
                        Q(message_text__icontains=search_query)
                    ).values_list('sender_id', flat=True).distinct()
                    latest_messages_qs = latest_messages_qs.filter(sender_id__in=matching_sender_ids)

                latest_messages = list(latest_messages_qs)
                if not latest_messages:
                    continue

                sender_ids = [msg.sender_id for msg in latest_messages]

                # Batch fetch unread counts only
                unread_counts = {
                    item['sender_id']: item['unread_count']
                    for item in InstagramMessage.objects.filter(
                        **base_filter,
                        sender_id__in=sender_ids,
                        is_from_business=False,
                        is_read_by_staff=False
                    ).values('sender_id').annotate(unread_count=Count('id'))
                }

                # Batch fetch customer info
                customer_messages = InstagramMessage.objects.filter(
                    **base_filter,
                    sender_id__in=sender_ids,
                    is_from_business=False
                ).order_by('sender_id', '-timestamp').distinct('sender_id')
                customer_msg_map = {msg.sender_id: msg for msg in customer_messages}

                # Batch fetch SocialAccount info for customers without incoming messages
                social_accounts = SocialAccount.objects.filter(
                    platform='instagram',
                    platform_id__in=sender_ids,
                    account_connection_id=account.instagram_account_id
                )
                social_account_map = {sa.platform_id: sa for sa in social_accounts}

                # Build conversations
                for msg in latest_messages:
                    sender_id = msg.sender_id
                    if not is_conversation_visible('instagram', account.instagram_account_id, sender_id):
                        continue

                    customer_msg = customer_msg_map.get(sender_id)
                    social_account = social_account_map.get(sender_id)

                    # Get customer name: prefer incoming message, then social account, then fallback
                    if customer_msg and (customer_msg.sender_name or customer_msg.sender_username):
                        customer_name = customer_msg.sender_name or customer_msg.sender_username
                        customer_pic = customer_msg.sender_profile_pic
                    elif social_account and social_account.display_name:
                        customer_name = social_account.display_name
                        customer_pic = social_account.profile_pic_url
                    else:
                        customer_name = f"Instagram User {sender_id}"
                        customer_pic = None

                    conversation = {
                        'conversation_id': f"ig_{account.instagram_account_id}_{sender_id}",
                        'platform': 'instagram',
                        'sender_id': sender_id,
                        'sender_name': customer_name,
                        'profile_pic_url': customer_pic,
                        'last_message': {
                            'id': str(msg.id),
                            'text': msg.message_text,
                            'timestamp': msg.timestamp,
                            'is_from_business': msg.is_from_business,
                            'attachment_type': msg.attachment_type,
                            'platform_message_id': msg.message_id,
                        },
                        'message_count': 0,
                        'unread_count': unread_counts.get(sender_id, 0),
                        'account_name': f"@{account.username}",
                        'account_id': account.instagram_account_id,
                    }
                    all_conversations.append(conversation)
        except Exception as e:
            logger.error(f"Error loading Instagram conversations: {e}")

    # Load WhatsApp conversations (optimized - single DISTINCT ON query)
    if 'whatsapp' in enabled_platforms:
        try:
            accounts = WhatsAppBusinessAccount.objects.all()
            for account in accounts:
                base_filter = {
                    'business_account': account,
                    'is_deleted': False,
                }

                # Split into two index-backed DISTINCT ON queries instead of using
                # a Case/When annotation (which can't use an index). Customer-sent
                # messages group by from_number; business-sent messages group by
                # to_number. The indexes (business_account, from_number, -timestamp)
                # and (business_account, to_number, -timestamp) make both scans
                # cheap — Postgres uses an Index Scan and emits one row per key.
                customer_sent = WhatsAppMessage.objects.filter(
                    **base_filter, is_from_business=False
                ).order_by('from_number', '-timestamp').distinct('from_number')

                business_sent = WhatsAppMessage.objects.filter(
                    **base_filter, is_from_business=True
                ).order_by('to_number', '-timestamp').distinct('to_number')

                # Merge in Python: pick whichever side has the newer message per
                # customer_number. Each side already has at most one row per key.
                latest_by_customer = {}
                for m in customer_sent:
                    latest_by_customer[m.from_number] = m
                for m in business_sent:
                    existing = latest_by_customer.get(m.to_number)
                    if not existing or m.timestamp > existing.timestamp:
                        latest_by_customer[m.to_number] = m

                # Apply search filter if provided. We still use the annotated
                # queryset here only to collect matching customer_numbers — this
                # path is fine because it's only hit when search_query is set.
                if search_query:
                    from django.db.models import Case, When, CharField
                    matching_numbers = set(
                        WhatsAppMessage.objects.filter(
                            **base_filter
                        ).annotate(
                            customer_number=Case(
                                When(is_from_business=True, then='to_number'),
                                default='from_number',
                                output_field=CharField()
                            )
                        ).filter(
                            Q(contact_name__icontains=search_query) |
                            Q(message_text__icontains=search_query) |
                            Q(from_number__icontains=search_query) |
                            Q(to_number__icontains=search_query)
                        ).values_list('customer_number', flat=True).distinct()
                    )
                    latest_by_customer = {
                        k: v for k, v in latest_by_customer.items() if k in matching_numbers
                    }

                if not latest_by_customer:
                    continue

                # Synthesize customer_number on each message object so the rest of
                # the loop (which reads msg.customer_number) keeps working without
                # changes. This is pure in-memory assignment — no query cost.
                latest_messages = []
                for customer_number, msg in latest_by_customer.items():
                    msg.customer_number = customer_number
                    latest_messages.append(msg)

                customer_numbers = list(latest_by_customer.keys())

                # Batch fetch unread counts only (messages FROM customer, not from business)
                unread_counts = {}
                for item in WhatsAppMessage.objects.filter(
                    **base_filter,
                    from_number__in=customer_numbers,
                    is_from_business=False,
                    is_read_by_staff=False
                ).values('from_number').annotate(unread_count=Count('id')):
                    unread_counts[item['from_number']] = item['unread_count']

                # Batch fetch customer info (messages FROM customer for name)
                customer_messages = WhatsAppMessage.objects.filter(
                    **base_filter,
                    from_number__in=customer_numbers,
                    is_from_business=False
                ).order_by('from_number', '-timestamp').distinct('from_number')
                customer_msg_map = {msg.from_number: msg for msg in customer_messages}

                # Build conversations
                for msg in latest_messages:
                    customer_number = msg.customer_number
                    normalized_phone = customer_number.lstrip('+')

                    if not is_conversation_visible('whatsapp', account.waba_id, normalized_phone):
                        continue

                    customer_msg = customer_msg_map.get(customer_number)
                    msg_type = msg.message_type
                    attachment_type = msg_type if msg_type and msg_type != 'text' else None

                    conversation = {
                        'conversation_id': f"wa_{account.waba_id}_{normalized_phone}",
                        'platform': 'whatsapp',
                        'sender_id': customer_number,
                        'sender_name': customer_msg.contact_name if customer_msg and customer_msg.contact_name else customer_number,
                        'profile_pic_url': None,
                        'last_message': {
                            'id': str(msg.id),
                            'text': msg.message_text,
                            'timestamp': msg.timestamp,
                            'is_from_business': msg.is_from_business,
                            'attachment_type': attachment_type,
                            'platform_message_id': msg.message_id,
                        },
                        'message_count': 0,
                        'unread_count': unread_counts.get(customer_number, 0),
                        'account_name': account.business_name or account.display_phone_number,
                        'account_id': account.waba_id,
                    }
                    all_conversations.append(conversation)
        except Exception as e:
            logger.error(f"Error loading WhatsApp conversations: {e}")

    # Load Email conversations (optimized - single DISTINCT ON query)
    if 'email' in enabled_platforms:
        try:
            connections = EmailConnection.objects.filter(is_active=True).prefetch_related('user_assignments')
            # Filter by specific email connection if provided
            if email_connection_id:
                connections = connections.filter(id=email_connection_id)

            # Helper to check if user can access an email connection. Uses the
            # already-prefetched user_assignments to avoid N+1 queries — calling
            # .exists() or .filter() on the related manager bypasses Django's
            # prefetch cache and fires new SQL, so we iterate in Python instead.
            def can_access_email_connection(conn):
                assignments = list(conn.user_assignments.all())
                if not assignments:
                    return True
                if is_admin:
                    return True
                user_id = request.user.id
                return any(a.user_id == user_id for a in assignments)

            for conn in connections:
                # Skip connections the user cannot access
                if not can_access_email_connection(conn):
                    continue
                # Build base filter
                base_filter = {'connection': conn}
                if email_folder and email_folder.lower() not in ('all', 'all folders'):
                    base_filter['folder'] = email_folder

                # Get latest message per thread using DISTINCT ON
                latest_messages_qs = EmailMessage.objects.filter(
                    **base_filter
                ).order_by('thread_id', '-timestamp').distinct('thread_id')

                # Apply search filter if provided
                if search_query:
                    matching_threads = EmailMessage.objects.filter(
                        **base_filter
                    ).filter(
                        Q(from_name__icontains=search_query) |
                        Q(from_email__icontains=search_query) |
                        Q(subject__icontains=search_query) |
                        Q(body_text__icontains=search_query)
                    ).values_list('thread_id', flat=True).distinct()
                    latest_messages_qs = latest_messages_qs.filter(thread_id__in=matching_threads)

                latest_messages = list(latest_messages_qs)
                if not latest_messages:
                    continue

                thread_ids = [msg.thread_id for msg in latest_messages]

                # Batch fetch unread counts only
                unread_counts = {
                    item['thread_id']: item['unread_count']
                    for item in EmailMessage.objects.filter(
                        **base_filter,
                        thread_id__in=thread_ids,
                        is_from_business=False,
                        is_read=False
                    ).values('thread_id').annotate(unread_count=Count('id'))
                }

                # Batch fetch customer info
                customer_messages = EmailMessage.objects.filter(
                    **base_filter,
                    thread_id__in=thread_ids,
                    is_from_business=False
                ).order_by('thread_id', '-timestamp').distinct('thread_id')
                customer_msg_map = {msg.thread_id: msg for msg in customer_messages}

                # Build conversations
                is_inbox = email_folder and email_folder.upper() == 'INBOX'
                for msg in latest_messages:
                    thread_id = msg.thread_id
                    # Only apply archive/assignment filtering for INBOX
                    if is_inbox or email_folder.lower() in ('all', 'all folders'):
                        if not is_conversation_visible('email', str(conn.id), thread_id):
                            continue

                    customer_msg = customer_msg_map.get(thread_id)
                    customer_email = customer_msg.from_email if customer_msg else (msg.from_email if not msg.is_from_business else '')
                    customer_name = (customer_msg.from_name or customer_msg.from_email) if customer_msg else (msg.from_name or msg.from_email if not msg.is_from_business else '')

                    # Truncate body text
                    body_text = msg.body_text or ''
                    truncated_text = body_text[:200] if body_text else ''

                    conversation = {
                        'conversation_id': f"email_{conn.id}_{thread_id}",
                        'platform': 'email',
                        'sender_id': customer_email,
                        'sender_name': customer_name or customer_email,
                        'profile_pic_url': None,
                        'last_message': {
                            'id': str(msg.id),
                            'text': truncated_text,
                            'timestamp': msg.timestamp,
                            'is_from_business': msg.is_from_business,
                            'attachment_type': 'attachment' if msg.attachments else None,
                            'platform_message_id': msg.message_id,
                        },
                        'message_count': 0,
                        'unread_count': unread_counts.get(thread_id, 0),
                        'account_name': conn.email_address,
                        'account_id': str(conn.id),
                        'subject': msg.subject,
                    }
                    all_conversations.append(conversation)
        except Exception as e:
            logger.error(f"Error loading Email conversations: {e}")

    # Load Widget (embeddable chat) conversations
    if 'widget' in enabled_platforms:
        try:
            from widget_registry.models import WidgetConnection
            from .models import WidgetSession, WidgetMessage

            tenant_schema = request.tenant.schema_name
            with schema_context('public'):
                connections = list(
                    WidgetConnection.objects.filter(tenant_schema=tenant_schema)
                )

            for wconn in connections:
                sessions = list(
                    WidgetSession.objects.filter(connection_id=wconn.id)
                    .order_by('-last_seen_at')
                )
                if not sessions:
                    continue
                session_pks = [s.pk for s in sessions]

                # Latest message per session via DISTINCT ON.
                latest_by_session = {}
                for m in (
                    WidgetMessage.objects
                    .filter(session_id__in=session_pks, is_deleted=False)
                    .order_by('session_id', '-timestamp')
                    .distinct('session_id')
                ):
                    latest_by_session[m.session_id] = m

                # Unread counts (visitor messages not yet read by staff).
                unread_counts = {
                    item['session_id']: item['cnt']
                    for item in (
                        WidgetMessage.objects
                        .filter(
                            session_id__in=session_pks,
                            is_from_visitor=True,
                            is_read_by_staff=False,
                            is_deleted=False,
                        )
                        .values('session_id')
                        .annotate(cnt=Count('id'))
                    )
                }

                for session in sessions:
                    last = latest_by_session.get(session.pk)
                    if not last:
                        continue
                    account_id = str(wconn.id)
                    conversation_id = session.session_id
                    if not is_conversation_visible('widget', account_id, conversation_id):
                        continue

                    sender_name = (
                        session.visitor_name
                        or f"Website visitor {session.visitor_id[:6]}"
                    )

                    # Best-effort search filter: match visitor name or last
                    # message text. Cheap enough to do in Python here.
                    if search_query:
                        haystack = (
                            (sender_name or '') + ' ' + (last.message_text or '')
                        ).lower()
                        if search_query not in haystack:
                            continue

                    all_conversations.append({
                        'conversation_id': f"widget_{wconn.id}_{session.session_id}",
                        'platform': 'widget',
                        'sender_id': session.visitor_id,
                        'sender_name': sender_name,
                        'profile_pic_url': None,
                        'last_message': {
                            'id': str(last.id),
                            'text': last.message_text,
                            'timestamp': last.timestamp,
                            'is_from_business': not last.is_from_visitor,
                            'attachment_type': 'attachment' if last.attachments else None,
                            'platform_message_id': last.message_id,
                        },
                        'message_count': 0,
                        'unread_count': unread_counts.get(session.pk, 0),
                        'account_name': wconn.label,
                        'account_id': account_id,
                        # Widget-only: surface the session's close state so the
                        # agent UI can disable the composer (and show a banner)
                        # when the visitor or another agent has ended the chat.
                        'session_ended_at': session.ended_at.isoformat() if session.ended_at else None,
                        'session_ended_by': session.ended_by,
                    })
        except Exception as e:
            logger.error(f"Error loading Widget conversations: {e}")

    # Sort all conversations by last message timestamp (most recent first)
    all_conversations.sort(
        key=lambda x: x['last_message']['timestamp'],
        reverse=True
    )

    # Paginate
    total_count = len(all_conversations)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paginated_conversations = all_conversations[start_idx:end_idx]

    # Per-page assignment + archive enrichment.
    #
    # Added for /messages-beta: the beta page can't make a second round-trip
    # for these slices, and the legacy page doesn't depend on them being on
    # this payload — so we attach them here, bounded to the visible page
    # only (two extra queries scoped by the 50-row tuple set, not the whole
    # tenant). Keys are (platform, account_id, conversation_id) so the
    # platform-prefixed dict slugs we built above match the lookup.
    if paginated_conversations:
        page_keys = [
            (c['platform'], c['account_id'], c['sender_id'])
            for c in paginated_conversations
        ]
        # Assignment lookup — only fetches rows for chats on this page.
        from django.db.models import Q as _Q
        _assignment_q = _Q()
        for plat, acct, sid in page_keys:
            _assignment_q |= _Q(platform=plat, account_id=acct, conversation_id=sid)
        assignment_lookup = {}
        if page_keys:
            for a in ChatAssignment.objects.filter(_assignment_q).select_related('assigned_user'):
                assignment_lookup[(a.platform, a.account_id, a.conversation_id)] = a

        # Archive lookup — same tuple key. ConversationArchive doesn't have
        # a composite index, so we pre-filter by the platform set.
        archive_lookup = {}
        if page_keys:
            page_platforms = {plat for plat, _, _ in page_keys}
            for ar in ConversationArchive.objects.filter(platform__in=page_platforms):
                archive_lookup[(ar.platform, ar.account_id, ar.conversation_id)] = ar

        for c in paginated_conversations:
            key = (c['platform'], c['account_id'], c['sender_id'])
            assignment = assignment_lookup.get(key)
            if assignment and assignment.assigned_user:
                full_name = (
                    assignment.assigned_user.get_full_name()
                    or assignment.assigned_user.email
                )
                c['assigned_user_id'] = assignment.assigned_user_id
                c['assigned_user_name'] = full_name
                c['assignment_status'] = assignment.status
                c['session_started_at'] = assignment.session_started_at
                c['session_ended_at'] = assignment.session_ended_at
            else:
                # Always emit the keys (even when null) so the frontend's
                # serializer-defined shape matches its TypeScript interface
                # without `?` everywhere.
                c['assigned_user_id'] = None
                c['assigned_user_name'] = None
                c['assignment_status'] = None
                c['session_started_at'] = None
                c['session_ended_at'] = None

            archive = archive_lookup.get(key)
            if archive:
                c['is_archived'] = True
                c['archived_at'] = archive.archived_at
            else:
                c['is_archived'] = False
                c['archived_at'] = None

    # Build pagination URLs
    base_url = request.build_absolute_uri().split('?')[0]
    params = request.query_params.copy()

    next_url = None
    if end_idx < total_count:
        params['page'] = page + 1
        next_url = f"{base_url}?{params.urlencode()}"

    previous_url = None
    if page > 1:
        params['page'] = page - 1
        previous_url = f"{base_url}?{params.urlencode()}"

    # Serialize and return
    serializer = UnifiedConversationSerializer(paginated_conversations, many=True)

    return Response({
        'count': total_count,
        'next': next_url,
        'previous': previous_url,
        'results': serializer.data
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def unread_messages_count(request):
    """
    Get the total count of unread messages across all social platforms.
    Returns counts for Facebook, Instagram, WhatsApp, Email, and total.
    Unread = incoming messages from customers that staff hasn't read yet.

    Archived (history) conversations are excluded — the sidebar badge only
    reflects chats visible in the active list.

    When chat_assignment_enabled is True:
    - Only counts messages in chats assigned to the current user
    - Also counts messages in chats not assigned to anyone
    - Chats with status='completed' are treated as unassigned (notifications return to all)
    """
    logger = logging.getLogger(__name__)

    try:
        # Get accessible email connection IDs for this user.
        # Fetch connections + their user_assignments in two queries, then
        # iterate in Python. .exists() / .filter().exists() inside the loop
        # bypass prefetch_related and trigger N+1; reading .all() hits the
        # prefetch cache.
        is_admin = request.user.is_superuser or request.user.is_staff
        all_email_connections = EmailConnection.objects.filter(
            is_active=True
        ).prefetch_related('user_assignments')
        accessible_connection_ids = []
        user_id = request.user.id
        for conn in all_email_connections:
            assignments = list(conn.user_assignments.all())  # prefetched
            if not assignments:
                accessible_connection_ids.append(conn.id)
            elif is_admin or any(a.user_id == user_id for a in assignments):
                accessible_connection_ids.append(conn.id)

        # Archived conversation IDs per platform — excluded from unread counts
        # so the sidebar badge doesn't count messages in chats that are in history.
        # Platform IDs (sender_id / from_number / thread_id) are effectively unique
        # within a platform, so scoping by conversation_id alone is safe in practice.
        #
        # One query, bucketed in Python. Previously this was five separate
        # SELECTs (one per platform) which Sentry flagged as the top backend
        # performance issue at ~6k events/14d. We also drop the default
        # `-archived_at` ordering — we only need the ids, the sort was
        # unused and expensive.
        archived_by_platform = {
            'facebook': [], 'instagram': [], 'whatsapp': [],
            'email': [], 'widget': [],
        }
        for platform, conv_id in (
            ConversationArchive.objects
            .filter(platform__in=archived_by_platform.keys())
            .order_by()
            .values_list('platform', 'conversation_id')
        ):
            archived_by_platform[platform].append(conv_id)
        archived_fb_ids = archived_by_platform['facebook']
        archived_ig_ids = archived_by_platform['instagram']
        archived_wa_numbers = archived_by_platform['whatsapp']
        archived_email_threads = archived_by_platform['email']
        archived_widget_sessions = archived_by_platform['widget']

        # Check if chat assignment filtering is enabled
        settings_obj = get_social_settings(request)
        assignment_enabled = settings_obj and settings_obj.chat_assignment_enabled

        if assignment_enabled:
            # Get active assignments (not 'completed' - those return to everyone)
            active_assignments = ChatAssignment.objects.filter(
                status__in=['active', 'in_session']
            )

            # Get my active assignments by platform
            my_fb_assignments = active_assignments.filter(
                assigned_user=request.user,
                platform='facebook'
            ).values_list('conversation_id', flat=True)

            my_ig_assignments = active_assignments.filter(
                assigned_user=request.user,
                platform='instagram'
            ).values_list('conversation_id', flat=True)

            my_wa_assignments = active_assignments.filter(
                assigned_user=request.user,
                platform='whatsapp'
            ).values_list('conversation_id', flat=True)

            my_email_assignments = active_assignments.filter(
                assigned_user=request.user,
                platform='email'
            ).values_list('conversation_id', flat=True)

            my_widget_assignments = active_assignments.filter(
                assigned_user=request.user,
                platform='widget'
            ).values_list('conversation_id', flat=True)

            # Get all active assignments by platform (to exclude others' chats)
            all_fb_assignments = active_assignments.filter(
                platform='facebook'
            ).values_list('conversation_id', flat=True)

            all_ig_assignments = active_assignments.filter(
                platform='instagram'
            ).values_list('conversation_id', flat=True)

            all_wa_assignments = active_assignments.filter(
                platform='whatsapp'
            ).values_list('conversation_id', flat=True)

            all_email_assignments = active_assignments.filter(
                platform='email'
            ).values_list('conversation_id', flat=True)

            all_widget_assignments = active_assignments.filter(
                platform='widget'
            ).values_list('conversation_id', flat=True)

            # Count unread Facebook messages - only mine or unassigned
            facebook_unread = FacebookMessage.objects.filter(
                is_from_page=False,
                is_read_by_staff=False,
                is_deleted=False
            ).exclude(
                sender_id__in=archived_fb_ids  # Skip chats in history
            ).filter(
                Q(sender_id__in=my_fb_assignments) |  # Assigned to me
                ~Q(sender_id__in=all_fb_assignments)   # Not assigned to anyone
            ).count()

            # Count unread Instagram messages - only mine or unassigned
            instagram_unread = InstagramMessage.objects.filter(
                is_from_business=False,
                is_read_by_staff=False,
                is_deleted=False
            ).exclude(
                sender_id__in=archived_ig_ids  # Skip chats in history
            ).filter(
                Q(sender_id__in=my_ig_assignments) |  # Assigned to me
                ~Q(sender_id__in=all_ig_assignments)  # Not assigned to anyone
            ).count()

            # Count unread WhatsApp messages - only mine or unassigned
            whatsapp_unread = WhatsAppMessage.objects.filter(
                is_from_business=False,
                is_read_by_staff=False,
                is_deleted=False
            ).exclude(
                from_number__in=archived_wa_numbers  # Skip chats in history
            ).filter(
                Q(from_number__in=my_wa_assignments) |  # Assigned to me
                ~Q(from_number__in=all_wa_assignments)  # Not assigned to anyone
            ).count()

            # Count unread Email messages - only mine or unassigned, only INBOX
            # Also filter by accessible email connections
            email_unread = EmailMessage.objects.filter(
                is_from_business=False,
                is_read_by_staff=False,
                is_deleted=False,
                folder='INBOX',  # Only count INBOX messages for notifications
                connection_id__in=accessible_connection_ids,
            ).exclude(
                thread_id__in=archived_email_threads  # Skip chats in history
            ).filter(
                Q(thread_id__in=my_email_assignments) |  # Assigned to me
                ~Q(thread_id__in=all_email_assignments)  # Not assigned to anyone
            ).count()

            widget_unread = WidgetMessage.objects.filter(
                is_from_visitor=True,
                is_read_by_staff=False,
                is_deleted=False,
            ).exclude(
                session__session_id__in=archived_widget_sessions
            ).filter(
                Q(session__session_id__in=my_widget_assignments)
                | ~Q(session__session_id__in=all_widget_assignments)
            ).count()
        else:
            # Assignment not enabled - count all unread messages (original behavior)
            facebook_unread = FacebookMessage.objects.filter(
                is_from_page=False,
                is_read_by_staff=False,
                is_deleted=False
            ).exclude(sender_id__in=archived_fb_ids).count()

            instagram_unread = InstagramMessage.objects.filter(
                is_from_business=False,
                is_read_by_staff=False,
                is_deleted=False
            ).exclude(sender_id__in=archived_ig_ids).count()

            whatsapp_unread = WhatsAppMessage.objects.filter(
                is_from_business=False,
                is_read_by_staff=False,
                is_deleted=False
            ).exclude(from_number__in=archived_wa_numbers).count()

            email_unread = EmailMessage.objects.filter(
                is_from_business=False,
                is_read_by_staff=False,
                is_deleted=False,
                folder='INBOX',  # Only count INBOX messages for notifications
                connection_id__in=accessible_connection_ids,
            ).exclude(thread_id__in=archived_email_threads).count()

            widget_unread = WidgetMessage.objects.filter(
                is_from_visitor=True,
                is_read_by_staff=False,
                is_deleted=False,
            ).exclude(session__session_id__in=archived_widget_sessions).count()

        total_unread = (
            facebook_unread + instagram_unread + whatsapp_unread
            + email_unread + widget_unread
        )

        return Response({
            'total': total_unread,
            'facebook': facebook_unread,
            'instagram': instagram_unread,
            'whatsapp': whatsapp_unread,
            'email': email_unread,
            'widget': widget_unread,
        })

    except Exception as e:
        logger.error(f"Error getting unread message count: {e}")
        return Response({
            'error': f'Failed to get unread count: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def mark_conversation_read(request):
    """
    Mark all messages in a conversation as read by staff.

    Request body:
    {
        "platform": "facebook" | "instagram" | "whatsapp",
        "conversation_id": "sender_id for fb/ig, or from_number for whatsapp"
    }
    """
    logger = logging.getLogger(__name__)

    try:
        platform = request.data.get('platform')
        conversation_id = request.data.get('conversation_id')

        if not platform or not conversation_id:
            return Response({
                'error': 'platform and conversation_id are required'
            }, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        updated_count = 0

        if platform == 'facebook':
            # Mark all incoming messages from this sender as read by staff
            updated_count = FacebookMessage.objects.filter(
                sender_id=conversation_id,
                is_from_page=False,
                is_read_by_staff=False
            ).update(
                is_read_by_staff=True,
                read_by_staff_at=now
            )

        elif platform == 'instagram':
            # Mark all incoming messages from this sender as read by staff
            updated_count = InstagramMessage.objects.filter(
                sender_id=conversation_id,
                is_from_business=False,
                is_read_by_staff=False
            ).update(
                is_read_by_staff=True,
                read_by_staff_at=now
            )

        elif platform == 'whatsapp':
            # Mark all incoming messages from this phone number as read by staff
            updated_count = WhatsAppMessage.objects.filter(
                from_number=conversation_id,
                is_from_business=False,
                is_read_by_staff=False
            ).update(
                is_read_by_staff=True,
                read_by_staff_at=now
            )

        elif platform == 'email':
            # Mark all incoming messages in this thread as read by staff
            updated_count = EmailMessage.objects.filter(
                thread_id=conversation_id,
                is_from_business=False,
                is_read_by_staff=False,
                is_deleted=False
            ).update(
                is_read_by_staff=True,
                read_by_staff_at=now,
                is_read=True
            )
        elif platform == 'widget':
            updated_count = WidgetMessage.objects.filter(
                session__session_id=conversation_id,
                is_from_visitor=True,
                is_read_by_staff=False,
            ).update(
                is_read_by_staff=True,
                read_by_staff_at=now,
            )
        else:
            return Response({
                'error': f'Invalid platform: {platform}'
            }, status=status.HTTP_400_BAD_REQUEST)

        logger.info(f"Marked {updated_count} {platform} messages as read for conversation {conversation_id}")

        # --- Auto-clear message_received notifications for this conversation ---
        try:
            from users.models import Notification
            from django.db import connection as db_connection

            tenant_schema = getattr(db_connection, 'schema_name', 'public')

            cleared_count = Notification.objects.filter(
                user=request.user,
                notification_type='message_received',
                is_read=False,
                metadata__conversation_id=str(conversation_id),
            ).update(is_read=True, read_at=timezone.now())

            if cleared_count > 0:
                # Invalidate cached unread count so next check recounts from DB
                from users.notification_utils import get_unread_count
                from django.core.cache import cache as notif_cache
                cache_key = f'notif_unread:{tenant_schema}:{request.user.id}'
                notif_cache.delete(cache_key)

                # Broadcast updated unread count via WebSocket
                from asgiref.sync import async_to_sync
                from users.consumers import send_unread_count_update
                try:
                    count = get_unread_count(request.user, tenant_schema)
                    async_to_sync(send_unread_count_update)(tenant_schema, request.user.id, count)
                except Exception:
                    pass

                logger.info(
                    f"Cleared {cleared_count} message_received notifications for user {request.user.id} "
                    f"conversation {conversation_id}"
                )
        except Exception as e:
            logger.warning(f"Failed to clear message notifications on conversation read: {e}")

        # Cross-user reactivity: tell every connected agent that this chat's
        # unread count just dropped to 0 so their sidebar badge clears live.
        # account_id is not available on this endpoint (the request body
        # carries only platform + conversation_id); beta clients match by
        # conversation_id alone when account_id is null.
        _broadcast_read_state(
            request,
            platform=platform,
            conversation_id=conversation_id,
            account_id=None,
            unread_count=0,
            last_read_at=now,
        )

        return Response({
            'success': True,
            'messages_marked_read': updated_count
        })

    except Exception as e:
        logger.error(f"Error marking conversation as read: {e}")
        return Response({
            'error': f'Failed to mark conversation as read: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def mark_conversation_unread(request):
    """
    Mark a conversation as unread by staff.
    This marks only the last incoming message as unread (so it appears in unread list).

    Request body:
    {
        "platform": "facebook" | "instagram" | "whatsapp" | "email",
        "conversation_id": "sender_id for fb/ig, thread_id for email, or from_number for whatsapp"
    }
    """
    logger = logging.getLogger(__name__)

    try:
        platform = request.data.get('platform')
        conversation_id = request.data.get('conversation_id')

        if not platform or not conversation_id:
            return Response({
                'error': 'platform and conversation_id are required'
            }, status=status.HTTP_400_BAD_REQUEST)

        updated_count = 0

        if platform == 'facebook':
            # Mark the last incoming message as unread
            last_message = FacebookMessage.objects.filter(
                sender_id=conversation_id,
                is_from_page=False,
                is_read_by_staff=True
            ).order_by('-timestamp').first()

            if last_message:
                last_message.is_read_by_staff = False
                last_message.read_by_staff_at = None
                last_message.save()
                updated_count = 1

        elif platform == 'instagram':
            # Mark the last incoming message as unread
            last_message = InstagramMessage.objects.filter(
                sender_id=conversation_id,
                is_from_business=False,
                is_read_by_staff=True
            ).order_by('-timestamp').first()

            if last_message:
                last_message.is_read_by_staff = False
                last_message.read_by_staff_at = None
                last_message.save()
                updated_count = 1

        elif platform == 'whatsapp':
            # Mark the last incoming message as unread
            last_message = WhatsAppMessage.objects.filter(
                from_number=conversation_id,
                is_from_business=False,
                is_read_by_staff=True
            ).order_by('-timestamp').first()

            if last_message:
                last_message.is_read_by_staff = False
                last_message.read_by_staff_at = None
                last_message.save()
                updated_count = 1

        elif platform == 'email':
            # Mark the last incoming message in this thread as unread
            last_message = EmailMessage.objects.filter(
                thread_id=conversation_id,
                is_from_business=False,
                is_read_by_staff=True,
                is_deleted=False
            ).order_by('-timestamp').first()

            if last_message:
                last_message.is_read_by_staff = False
                last_message.read_by_staff_at = None
                last_message.is_read = False
                last_message.save()
                updated_count = 1
        elif platform == 'widget':
            last_message = WidgetMessage.objects.filter(
                session__session_id=conversation_id,
                is_from_visitor=True,
                is_read_by_staff=True,
            ).order_by('-timestamp').first()
            if last_message:
                last_message.is_read_by_staff = False
                last_message.read_by_staff_at = None
                last_message.save()
                updated_count = 1
        else:
            return Response({
                'error': f'Invalid platform: {platform}'
            }, status=status.HTTP_400_BAD_REQUEST)

        logger.info(f"Marked {updated_count} {platform} messages as unread for conversation {conversation_id}")

        # We bumped the unread count by exactly 1 (the endpoint flips only the
        # last incoming message). Beta clients use this to put the badge back
        # on the sidebar without polling. Passing unread_count=1 is a hint —
        # the precise count can be re-derived from the conversation list on
        # the next refresh if needed.
        _broadcast_read_state(
            request,
            platform=platform,
            conversation_id=conversation_id,
            account_id=None,
            unread_count=updated_count,
            last_read_at=None,
        )

        return Response({
            'success': True,
            'messages_marked_unread': updated_count
        })

    except Exception as e:
        logger.error(f"Error marking conversation as unread: {e}")
        return Response({
            'error': f'Failed to mark conversation as unread: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated, IsStaffUser])
def delete_conversation(request):
    """
    Soft delete all messages in a conversation. STAFF USERS ONLY.
    Messages are marked as deleted but kept for admin investigation.

    Request body:
    {
        "platform": "facebook" | "instagram" | "whatsapp",
        "conversation_id": "sender_id for fb/ig, or from_number for whatsapp"
    }
    """
    logger = logging.getLogger(__name__)

    try:
        platform = request.data.get('platform')
        conversation_id = request.data.get('conversation_id')

        if not platform or not conversation_id:
            return Response({
                'error': 'platform and conversation_id are required'
            }, status=status.HTTP_400_BAD_REQUEST)

        deleted_count = 0
        now = timezone.now()

        if platform == 'facebook':
            # Soft delete all messages from this conversation
            deleted_count = FacebookMessage.objects.filter(
                sender_id=conversation_id,
                is_deleted=False
            ).update(
                is_deleted=True,
                deleted_at=now,
                deleted_by=request.user
            )

        elif platform == 'instagram':
            # Soft delete all messages from this conversation
            deleted_count = InstagramMessage.objects.filter(
                sender_id=conversation_id,
                is_deleted=False
            ).update(
                is_deleted=True,
                deleted_at=now,
                deleted_by=request.user
            )

        elif platform == 'whatsapp':
            # Soft delete all messages from/to this phone number
            deleted_from = WhatsAppMessage.objects.filter(
                from_number=conversation_id,
                is_deleted=False
            ).update(
                is_deleted=True,
                deleted_at=now,
                deleted_by=request.user
            )
            deleted_to = WhatsAppMessage.objects.filter(
                to_number=conversation_id,
                is_deleted=False
            ).update(
                is_deleted=True,
                deleted_at=now,
                deleted_by=request.user
            )
            deleted_count = deleted_from + deleted_to

        elif platform == 'email':
            # Get messages to delete (need message_ids for IMAP deletion)
            messages_to_delete = EmailMessage.objects.filter(
                thread_id=conversation_id,
                is_deleted=False
            )
            message_ids = list(messages_to_delete.values_list('message_id', flat=True))

            # Try to delete from IMAP server first
            if message_ids:
                try:
                    # Get the connection for these emails
                    first_message = messages_to_delete.first()
                    if first_message and first_message.connection:
                        from .email_utils import delete_emails_from_imap
                        delete_emails_from_imap(first_message.connection, message_ids)
                except Exception as e:
                    logger.warning(f"Failed to delete emails from IMAP server: {e}")
                    # Continue with soft delete even if IMAP delete fails

            # Soft delete all messages in this thread
            deleted_count = messages_to_delete.update(
                is_deleted=True,
                deleted_at=now,
                deleted_by=request.user
            )

        elif platform == 'widget':
            deleted_count = WidgetMessage.objects.filter(
                session__session_id=conversation_id,
                is_deleted=False,
            ).update(is_deleted=True)

        else:
            return Response({
                'error': f'Invalid platform: {platform}'
            }, status=status.HTTP_400_BAD_REQUEST)

        logger.info(f"Staff user {request.user.email} soft-deleted {deleted_count} messages for {platform} conversation {conversation_id}")

        # PR F — emit `conversation_deleted` so every connected agent's
        # /messages-beta sidebar drops the row live. Legacy /messages
        # ignores the unknown event via switch-default; safe-by-default.
        # account_id is unknown at this layer (the view doesn't take one);
        # the beta store scans rows by (platform, conversationKey) and
        # removes all matches, mirroring the soft-delete's loose scope.
        try:
            from django.db import connection
            from .consumers import send_conversation_deleted
            async_to_sync(send_conversation_deleted)(
                connection.schema_name,
                platform=platform,
                conversation_id=conversation_id,
                account_id=None,
                by_user_id=request.user.id,
            )
        except Exception as exc:  # noqa: BLE001 — broadcast is best-effort.
            logger.warning('conversation_deleted broadcast failed: %s', exc)

        return Response({
            'success': True,
            'messages_deleted': deleted_count
        })

    except Exception as e:
        logger.error(f"Error deleting conversation: {e}")
        return Response({
            'error': f'Failed to delete conversation: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ===========================
# WHATSAPP BUSINESS API VIEWS
# ===========================

class WhatsAppBusinessAccountViewSet(viewsets.ModelViewSet):
    serializer_class = WhatsAppBusinessAccountSerializer
    permission_classes = [IsAuthenticated, CanManageSocialConnections]

    def get_queryset(self):
        return WhatsAppBusinessAccount.objects.all()  # Tenant schema provides isolation

    def perform_create(self, serializer):
        serializer.save()  # No user assignment needed in multi-tenant setup

    @action(detail=True, methods=['post'], url_path='sync-contacts')
    def sync_contacts(self, request, pk=None):
        """
        Sync contacts from WhatsApp Business App (Coexistence feature).
        Must be called within 24 hours of onboarding.
        Calls Meta API: POST /{PHONE_NUMBER_ID}/smb_app_data with {"types": ["contacts"]}
        """
        account = self.get_object()

        if not account.coex_enabled:
            return Response({
                'error': 'Coexistence is not enabled for this account'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Check if within 24-hour window
        if account.onboarded_at:
            time_since_onboarding = timezone.now() - account.onboarded_at
            if time_since_onboarding.total_seconds() > 24 * 60 * 60:
                return Response({
                    'error': 'Sync window expired. Contacts must be synced within 24 hours of onboarding.'
                }, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Update sync status
            account.sync_status = 'syncing'
            account.save(update_fields=['sync_status'])

            # Call Meta API to request contacts sync
            url = f"https://graph.facebook.com/v23.0/{account.phone_number_id}/smb_app_data"
            response = requests.post(
                url,
                headers={
                    'Authorization': f'Bearer {account.access_token}',
                    'Content-Type': 'application/json'
                },
                json={'types': ['contacts']},
                timeout=30
            )

            if response.status_code == 200:
                account.sync_status = 'completed'
                account.contacts_synced_at = timezone.now()
                account.save(update_fields=['sync_status', 'contacts_synced_at'])

                return Response({
                    'status': 'success',
                    'message': 'Contacts sync initiated. Contacts will be delivered via webhook.'
                })
            else:
                account.sync_status = 'failed'
                account.save(update_fields=['sync_status'])
                error_data = response.json() if response.content else {}
                return Response({
                    'error': 'Failed to initiate contacts sync',
                    'details': error_data
                }, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            account.sync_status = 'failed'
            account.save(update_fields=['sync_status'])
            logger.error(f"Failed to sync contacts for account {account.id}: {e}")
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='sync-history')
    def sync_history(self, request, pk=None):
        """
        Sync message history from WhatsApp Business App (Coexistence feature).
        Must be called within 24 hours of onboarding.
        Supports phases: 0-1 days, 1-90 days, 90-180 days.
        """
        account = self.get_object()

        if not account.coex_enabled:
            return Response({
                'error': 'Coexistence is not enabled for this account'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Check if within 24-hour window
        if account.onboarded_at:
            time_since_onboarding = timezone.now() - account.onboarded_at
            if time_since_onboarding.total_seconds() > 24 * 60 * 60:
                return Response({
                    'error': 'Sync window expired. History must be synced within 24 hours of onboarding.'
                }, status=status.HTTP_400_BAD_REQUEST)

        # Get phase from request (default: 0-1 days)
        phase = request.data.get('phase', '0-1')
        valid_phases = ['0-1', '1-90', '90-180']
        if phase not in valid_phases:
            return Response({
                'error': f'Invalid phase. Must be one of: {valid_phases}'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Update sync status
            account.sync_status = 'syncing'
            account.save(update_fields=['sync_status'])

            # Call Meta API to request history sync
            url = f"https://graph.facebook.com/v23.0/{account.phone_number_id}/smb_app_data"

            # Build request payload based on phase
            payload = {'types': ['history']}
            if phase == '0-1':
                payload['history'] = {'start_days_ago': 0, 'end_days_ago': 1}
            elif phase == '1-90':
                payload['history'] = {'start_days_ago': 1, 'end_days_ago': 90}
            elif phase == '90-180':
                payload['history'] = {'start_days_ago': 90, 'end_days_ago': 180}

            response = requests.post(
                url,
                headers={
                    'Authorization': f'Bearer {account.access_token}',
                    'Content-Type': 'application/json'
                },
                json=payload,
                timeout=30
            )

            if response.status_code == 200:
                account.sync_status = 'completed'
                account.history_synced_at = timezone.now()
                account.save(update_fields=['sync_status', 'history_synced_at'])

                return Response({
                    'status': 'success',
                    'message': f'History sync initiated for phase {phase}. Messages will be delivered via webhook.',
                    'phase': phase
                })
            else:
                account.sync_status = 'failed'
                account.save(update_fields=['sync_status'])
                error_data = response.json() if response.content else {}
                return Response({
                    'error': 'Failed to initiate history sync',
                    'details': error_data
                }, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            account.sync_status = 'failed'
            account.save(update_fields=['sync_status'])
            logger.error(f"Failed to sync history for account {account.id}: {e}")
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'], url_path='coex-status')
    def coex_status(self, request, pk=None):
        """
        Get coexistence status for a WhatsApp Business Account.
        Fetches current status from Meta API and returns local state.
        """
        account = self.get_object()

        # Prepare local status data
        local_status = {
            'coex_enabled': account.coex_enabled,
            'is_on_biz_app': account.is_on_biz_app,
            'platform_type': account.platform_type,
            'sync_status': account.sync_status,
            'onboarded_at': account.onboarded_at.isoformat() if account.onboarded_at else None,
            'contacts_synced_at': account.contacts_synced_at.isoformat() if account.contacts_synced_at else None,
            'history_synced_at': account.history_synced_at.isoformat() if account.history_synced_at else None,
            'throughput_limit': account.throughput_limit,
        }

        # Check if within 24-hour sync window
        sync_window_open = False
        sync_window_remaining = None
        if account.onboarded_at:
            time_since_onboarding = timezone.now() - account.onboarded_at
            if time_since_onboarding.total_seconds() < 24 * 60 * 60:
                sync_window_open = True
                sync_window_remaining = int(24 * 60 * 60 - time_since_onboarding.total_seconds())

        local_status['sync_window_open'] = sync_window_open
        local_status['sync_window_remaining_seconds'] = sync_window_remaining

        # Optionally fetch from Meta API for fresh data
        fetch_from_api = request.query_params.get('refresh', 'false').lower() == 'true'
        api_status = None

        if fetch_from_api and account.access_token:
            try:
                url = f"https://graph.facebook.com/v23.0/{account.phone_number_id}"
                response = requests.get(
                    url,
                    params={'fields': 'id,is_on_biz_app,platform_type,verified_name,quality_rating'},
                    headers={'Authorization': f'Bearer {account.access_token}'},
                    timeout=10
                )

                if response.status_code == 200:
                    api_data = response.json()
                    api_status = {
                        'is_on_biz_app': api_data.get('is_on_biz_app', False),
                        'platform_type': api_data.get('platform_type', ''),
                        'verified_name': api_data.get('verified_name', ''),
                        'quality_rating': api_data.get('quality_rating', ''),
                    }

                    # Update local fields if different
                    if api_data.get('is_on_biz_app') != account.is_on_biz_app:
                        account.is_on_biz_app = api_data.get('is_on_biz_app', False)
                        account.save(update_fields=['is_on_biz_app'])
                    if api_data.get('platform_type') != account.platform_type:
                        account.platform_type = api_data.get('platform_type', '')
                        account.save(update_fields=['platform_type'])

            except Exception as e:
                logger.warning(f"Failed to fetch coex status from API: {e}")

        return Response({
            'local_status': local_status,
            'api_status': api_status,
        })


class WhatsAppMessageViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = WhatsAppMessageSerializer
    permission_classes = [IsAuthenticated, CanViewSocialMessages]
    pagination_class = SocialMessagePagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = {
        'business_account__waba_id': ['exact'],
        'business_account__phone_number_id': ['exact'],
        'is_from_business': ['exact'],
        'is_read_by_staff': ['exact'],
    }
    search_fields = ['contact_name', 'message_text', 'from_number', 'to_number']
    ordering_fields = ['timestamp', 'created_at']
    ordering = ['-timestamp']

    def get_queryset(self):
        tenant_accounts = WhatsAppBusinessAccount.objects.all()  # All accounts for this tenant
        # Filter out soft-deleted messages
        base_queryset = WhatsAppMessage.objects.filter(
            business_account__in=tenant_accounts,
            is_deleted=False
        ).select_related('business_account', 'template', 'sent_by', 'reply_to')

        # Apply waba_id or phone_number_id filter from query params
        waba_id = self.request.query_params.get('waba_id')
        phone_number_id = self.request.query_params.get('phone_number_id')
        if waba_id:
            base_queryset = base_queryset.filter(business_account__waba_id=waba_id)
        if phone_number_id:
            base_queryset = base_queryset.filter(business_account__phone_number_id=phone_number_id)

        # Check if hiding assigned chats is enabled
        settings_obj = get_social_settings(self.request)
        if settings_obj and settings_obj.hide_assigned_chats:
            # Admin/superuser sees all messages
            if not (self.request.user.is_superuser or self.request.user.is_staff):
                # Get WABA IDs for this tenant
                waba_ids = list(tenant_accounts.values_list('waba_id', flat=True))

                # Only active assignments — see the Facebook viewset for the
                # full rationale. Completed sessions have assigned_user=None
                # and would otherwise be hidden from everyone.
                active_assignments = ChatAssignment.objects.filter(
                    platform='whatsapp',
                    account_id__in=waba_ids,
                    status__in=['active', 'in_session'],
                )
                my_assigned = active_assignments.filter(
                    assigned_user=self.request.user
                ).values_list('conversation_id', flat=True)
                all_assigned = active_assignments.values_list('conversation_id', flat=True)

                # For WhatsApp, we need to check both from_number and to_number
                # conversation_id is the customer's phone number (from_number for incoming, to_number for outgoing)
                # Return: messages where customer is in my_assigned OR customer is not in all_assigned
                base_queryset = base_queryset.filter(
                    Q(from_number__in=my_assigned) | Q(to_number__in=my_assigned) |
                    (~Q(from_number__in=all_assigned) & ~Q(to_number__in=all_assigned))
                )

        # Filter by from_number (bi-directional: both incoming and outgoing messages)
        from_number = self.request.query_params.get('from_number')
        if from_number:
            base_queryset = base_queryset.filter(
                Q(from_number=from_number) | Q(to_number=from_number)
            )

        # Check for initial_load parameter - returns unread messages + 10 most recent
        initial_load = self.request.query_params.get('initial_load', '').lower() == 'true'

        if initial_load and from_number:
            # Count unread incoming messages
            unread_count = base_queryset.filter(
                is_from_business=False,
                is_read_by_staff=False
            ).count()

            # Get unread + 10 messages (minimum 10)
            limit = max(unread_count + 10, 10)

            # Return the most recent `limit` messages
            recent_ids = list(base_queryset.order_by('-timestamp').values_list('id', flat=True)[:limit])
            return base_queryset.filter(id__in=recent_ids)

        return base_queryset


class WhatsAppContactViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing WhatsApp contacts synced from Business App (Coexistence feature)"""
    serializer_class = WhatsAppContactSerializer
    permission_classes = [IsAuthenticated, CanViewSocialMessages]

    def get_queryset(self):
        tenant_accounts = WhatsAppBusinessAccount.objects.all()
        queryset = WhatsAppContact.objects.filter(account__in=tenant_accounts).select_related('account')

        # Filter by account if provided
        account_id = self.request.query_params.get('account')
        if account_id:
            queryset = queryset.filter(account_id=account_id)

        # Filter by business contacts
        is_business = self.request.query_params.get('is_business')
        if is_business is not None:
            queryset = queryset.filter(is_business=is_business.lower() == 'true')

        return queryset.order_by('-last_message_at', '-synced_at')


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def whatsapp_oauth_start(request):
    """Generate WhatsApp Business OAuth URL"""
    logger = logging.getLogger(__name__)

    try:
        fb_app_id = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_APP_ID')
        if not fb_app_id:
            return Response({
                'error': 'Facebook App ID not configured'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # WhatsApp Embedded Signup config ID
        config_id = '4254308474803749'

        # Use public callback URL since Facebook needs a consistent redirect URI
        redirect_uri = f'https://{settings.API_DOMAIN}/api/social/whatsapp/embedded-signup/callback/'

        # Include tenant info in state parameter
        from urllib.parse import quote
        tenant_obj = getattr(request, "tenant", None)

        # Extract tenant schema name from Tenant object or use default
        if tenant_obj and hasattr(tenant_obj, 'schema_name'):
            tenant_name = tenant_obj.schema_name
        elif tenant_obj and hasattr(tenant_obj, 'name'):
            tenant_name = tenant_obj.name
        else:
            tenant_name = "amanati"  # Default fallback

        # Simplified state parameter with just tenant schema
        state_raw = f'tenant={tenant_name}'
        state = quote(state_raw)  # URL encode the state
        logger.info(f"WhatsApp OAuth - Tenant: {tenant_name}")
        logger.info(f"WhatsApp OAuth - State parameter: {state}")

        # WhatsApp Business App Coexistence extras parameter
        # This enables linking both Business App and Cloud API on the same number
        import json
        extras = {
            "featureType": "whatsapp_business_app_onboarding",  # Enable coexistence
            "sessionInfoVersion": 3  # Required for coexistence
        }
        extras_encoded = quote(json.dumps(extras))

        # WhatsApp Embedded Signup OAuth URL with coexistence support
        oauth_url = (
            f"https://www.facebook.com/v23.0/dialog/oauth?"
            f"client_id={fb_app_id}&"
            f"redirect_uri={quote(redirect_uri)}&"
            f"config_id={config_id}&"
            f"response_type=code&"
            f"scope=whatsapp_business_management,whatsapp_business_messaging&"
            f"state={state}&"
            f"extras={extras_encoded}"
        )

        logger.info(f"WhatsApp OAuth URL: {oauth_url}")

        # Return OAuth URL to frontend (similar to Facebook pattern)
        return Response({
            'oauth_url': oauth_url,
            'redirect_uri': redirect_uri,
            'instructions': 'Visit the OAuth URL to connect your WhatsApp Business Account'
        })

    except Exception as e:
        logger.error(f"Failed to generate WhatsApp OAuth URL: {str(e)}")
        return Response({
            'error': f'Failed to generate WhatsApp OAuth URL: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET', 'POST'])
@permission_classes([])  # No authentication required for embedded signup callback
def whatsapp_embedded_signup_callback(request):
    """
    Handle WhatsApp Embedded Signup callback from Facebook OAuth redirect.
    This receives the authorization code and exchanges it for access tokens,
    then retrieves WhatsApp Business Account details and saves them.
    """
    logger.info("📱 WhatsApp Embedded Signup callback received")

    try:
        # Handle both GET (OAuth redirect) and POST (API call)
        if request.method == 'GET':
            # OAuth redirect from Facebook
            code = request.GET.get('code')
            state = request.GET.get('state', '')
            error = request.GET.get('error')
            error_description = request.GET.get('error_description')

            # Parse tenant from state
            tenant_name = None
            if state:
                from urllib.parse import unquote
                decoded_state = unquote(state)
                # State format: "tenant=amanati"
                try:
                    for param in decoded_state.split('&'):
                        if param.startswith('tenant='):
                            tenant_name = param.split('=', 1)[1]
                except (ValueError, IndexError):
                    pass
        else:
            # POST request from API
            code = request.data.get('code')
            tenant_name = request.data.get('tenant')

        # Handle Facebook errors
        if request.method == 'GET' and error:
            error_msg = error_description or error
            logger.error(f"Facebook OAuth error: {error_msg}")
            # Redirect to frontend with error
            frontend_url = f"https://{tenant_name}.{settings.MAIN_DOMAIN}" if tenant_name else f"https://amanati.{settings.MAIN_DOMAIN}"
            from urllib.parse import quote_plus
            return redirect(f"{frontend_url}/social/connections?whatsapp_status=error&message={quote_plus(error_msg)}")

        if not code:
            error_msg = 'Authorization code is required'
            if request.method == 'GET':
                frontend_url = f"https://{tenant_name}.{settings.MAIN_DOMAIN}" if tenant_name else f"https://amanati.{settings.MAIN_DOMAIN}"
                from urllib.parse import quote_plus
                return redirect(f"{frontend_url}/social/connections?whatsapp_status=error&message={quote_plus(error_msg)}")
            return Response({'error': error_msg}, status=status.HTTP_400_BAD_REQUEST)

        if not tenant_name:
            error_msg = 'Tenant name is required'
            if request.method == 'GET':
                frontend_url = f"https://amanati.{settings.MAIN_DOMAIN}"
                from urllib.parse import quote_plus
                return redirect(f"{frontend_url}/social/connections?whatsapp_status=error&message={quote_plus(error_msg)}")
            return Response({'error': error_msg}, status=status.HTTP_400_BAD_REQUEST)

        logger.info(f"Processing WhatsApp signup for tenant: {tenant_name}")

        # Exchange code for access token
        fb_app_id = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_APP_ID')
        fb_app_secret = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_APP_SECRET')
        fb_api_version = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_API_VERSION', 'v23.0')

        token_url = f"https://graph.facebook.com/{fb_api_version}/oauth/access_token"
        redirect_uri = f'https://{settings.API_DOMAIN}/api/social/whatsapp/embedded-signup/callback/'
        token_params = {
            'client_id': fb_app_id,
            'client_secret': fb_app_secret,
            'redirect_uri': redirect_uri,
            'code': code
        }

        logger.info(f"Exchanging code for access token...")
        logger.info(f"Token URL: {token_url}")
        logger.info(f"Token params: {token_params}")

        # Make the request and log the exact URL being called
        token_response = requests.get(token_url, params=token_params, timeout=30)
        logger.info(f"Actual request URL: {token_response.url}")

        token_data = token_response.json()
        logger.info(f"Token response: {token_data}")

        if 'error' in token_data:
            error_msg = token_data.get('error', {}).get('message', 'Unknown error')
            logger.error(f"Token exchange failed: {error_msg}")
            return Response({
                'error': f'Token exchange failed: {error_msg}'
            }, status=status.HTTP_400_BAD_REQUEST)

        access_token = token_data.get('access_token')
        if not access_token:
            return Response({
                'error': 'No access token received from Facebook'
            }, status=status.HTTP_400_BAD_REQUEST)

        logger.info("✅ Successfully obtained access token")

        # Debug: Check token permissions and extract WABA IDs from granular scopes
        debug_url = f"https://graph.facebook.com/{fb_api_version}/debug_token"
        debug_params = {
            'input_token': access_token,
            'access_token': f"{fb_app_id}|{fb_app_secret}"
        }

        waba_ids = []
        try:
            debug_response = requests.get(debug_url, params=debug_params, timeout=30)
            debug_data = debug_response.json()
            logger.info(f"🔍 Token debug info: {debug_data}")

            if 'data' in debug_data:
                scopes = debug_data['data'].get('scopes', [])
                logger.info(f"📋 Granted permissions: {scopes}")

                # Extract WABA IDs from granular_scopes
                granular_scopes = debug_data['data'].get('granular_scopes', [])
                for scope in granular_scopes:
                    if scope.get('scope') == 'whatsapp_business_management':
                        waba_ids = scope.get('target_ids', [])
                        logger.info(f"✅ Found WABA IDs in granular scopes: {waba_ids}")
                        break

                # Check if required WhatsApp permissions are present
                required_perms = ['whatsapp_business_management', 'whatsapp_business_messaging']
                missing_perms = [p for p in required_perms if p not in scopes]
                if missing_perms:
                    logger.warning(f"⚠️ Missing required permissions: {missing_perms}")
        except Exception as e:
            logger.error(f"Failed to debug token: {str(e)}")

        if not waba_ids:
            return Response({
                'error': 'No WhatsApp Business Account IDs found in token'
            }, status=status.HTTP_404_NOT_FOUND)

        # Fetch details for each WABA directly
        whatsapp_accounts = []
        for waba_id in waba_ids:
            logger.info(f"Fetching details for WABA: {waba_id}")
            waba_url = f"https://graph.facebook.com/{fb_api_version}/{waba_id}"
            waba_params = {
                'access_token': access_token,
                'fields': 'id,name,timezone_id,message_template_namespace'
            }

            waba_response = requests.get(waba_url, params=waba_params, timeout=30)
            waba_data = waba_response.json()

            if 'error' in waba_data:
                error_msg = waba_data.get('error', {}).get('message', 'Unknown error')
                logger.error(f"Failed to fetch WABA {waba_id}: {error_msg}")
                continue

            whatsapp_accounts.append(waba_data)
            logger.info(f"✅ Successfully fetched WABA: {waba_data.get('name', waba_id)}")

        if not whatsapp_accounts:
            return Response({
                'error': 'No WhatsApp Business Accounts found'
            }, status=status.HTTP_404_NOT_FOUND)

        # Process WhatsApp accounts and save to tenant database
        from tenant_schemas.utils import schema_context

        saved_accounts = []
        with schema_context(tenant_name):
            for wa_account in whatsapp_accounts:
                waba_id = wa_account.get('id')

                # Get phone numbers for this WABA (including coexistence fields)
                phone_url = f"https://graph.facebook.com/{fb_api_version}/{waba_id}/phone_numbers"
                phone_params = {
                    'access_token': access_token,
                    'fields': 'id,verified_name,display_phone_number,quality_rating,is_on_biz_app,platform_type'
                }

                phone_response = requests.get(phone_url, params=phone_params, timeout=30)
                phone_data = phone_response.json()

                phones = phone_data.get('data', [])
                if not phones:
                    logger.warning(f"No phone numbers found for WABA {waba_id}")
                    continue

                # Save each phone number as a separate business account
                for phone in phones:
                    phone_number_id = phone.get('id')
                    display_phone_number = phone.get('display_phone_number', '')
                    verified_name = phone.get('verified_name', wa_account.get('name', 'WhatsApp Business'))
                    quality_rating = phone.get('quality_rating', '')

                    # WhatsApp Business App Coexistence fields
                    is_on_biz_app = phone.get('is_on_biz_app', False)
                    platform_type = phone.get('platform_type', '')

                    # Determine if coexistence is enabled based on platform type
                    coex_enabled = is_on_biz_app and platform_type == 'SMB'
                    throughput_limit = 20 if coex_enabled else 80  # Coex accounts have 20 mps limit

                    if coex_enabled:
                        logger.info(f"📱 Coexistence enabled for {display_phone_number} (Business App linked)")

                    # Create or update WhatsApp Business Account
                    account, created = WhatsAppBusinessAccount.objects.update_or_create(
                        waba_id=waba_id,
                        phone_number_id=phone_number_id,
                        defaults={
                            'business_name': verified_name,
                            'phone_number': display_phone_number.replace(' ', ''),
                            'display_phone_number': display_phone_number,
                            'access_token': access_token,
                            'quality_rating': quality_rating,
                            'is_active': True,
                            # Coexistence fields
                            'is_on_biz_app': is_on_biz_app,
                            'platform_type': platform_type,
                            'coex_enabled': coex_enabled,
                            'throughput_limit': throughput_limit,
                        }
                    )

                    # Set onboarded_at if coexistence just enabled (for 24-hour sync window)
                    if created and coex_enabled:
                        from django.utils import timezone
                        account.onboarded_at = timezone.now()
                        account.save(update_fields=['onboarded_at'])

                    action = "Created" if created else "Updated"
                    logger.info(f"✅ {action} WhatsApp Business Account: {verified_name} ({display_phone_number})")

                    # Register the phone number (required to activate it)
                    try:
                        register_url = f"https://graph.facebook.com/{fb_api_version}/{phone_number_id}/register"
                        register_data = {
                            'messaging_product': 'whatsapp',
                            'pin': '123456'  # Default PIN for 2FA recovery
                        }
                        register_headers = {
                            'Authorization': f'Bearer {access_token}',
                            'Content-Type': 'application/json'
                        }
                        register_response = requests.post(register_url, headers=register_headers, json=register_data, timeout=30)
                        if register_response.status_code == 200:
                            logger.info(f"✅ Registered phone number {phone_number_id}")
                        else:
                            # 500 error usually means already registered, which is fine
                            register_error = register_response.json()
                            if register_response.status_code == 500:
                                logger.info(f"ℹ️ Phone number {phone_number_id} likely already registered")
                            else:
                                logger.warning(f"⚠️ Phone registration response: {register_error}")
                    except Exception as e:
                        logger.error(f"❌ Error registering phone number: {e}")

                    saved_accounts.append({
                        'id': account.id,
                        'waba_id': account.waba_id,
                        'business_name': account.business_name,
                        'phone_number': account.display_phone_number,
                        'quality_rating': account.quality_rating
                    })

                    # Subscribe to webhooks (including coexistence webhook fields)
                    try:
                        webhook_url = f"https://graph.facebook.com/{fb_api_version}/{waba_id}/subscribed_apps"

                        # Standard fields plus coexistence fields
                        # - history: Synced message history from Business App
                        # - smb_app_state_sync: Business App state changes
                        # - smb_message_echoes: Messages sent from Business App
                        # - account_update: Account status changes (including PARTNER_REMOVED)
                        webhook_fields = [
                            'messages',
                            'message_template_status_update',
                            'history',              # Coexistence: synced history
                            'smb_app_state_sync',   # Coexistence: app state
                            'smb_message_echoes',   # Coexistence: echoed messages
                            'account_update',       # Coexistence: account changes
                        ]
                        webhook_params = {
                            'access_token': access_token,
                            'subscribed_fields': ','.join(webhook_fields)
                        }
                        webhook_response = requests.post(webhook_url, params=webhook_params, timeout=30)
                        if webhook_response.status_code == 200:
                            logger.info(f"✅ Subscribed WABA {waba_id} to webhooks with fields: {', '.join(webhook_fields)}")
                        else:
                            logger.warning(f"⚠️ Failed to subscribe WABA {waba_id} to webhooks: {webhook_response.json()}")
                    except Exception as e:
                        logger.error(f"❌ Error subscribing to webhooks: {e}")

        if not saved_accounts:
            error_msg = 'No WhatsApp phone numbers could be configured'
            if request.method == 'GET':
                frontend_url = f"https://{tenant_name}.{settings.MAIN_DOMAIN}"
                from urllib.parse import quote_plus
                return redirect(f"{frontend_url}/social/connections?whatsapp_status=error&message={quote_plus(error_msg)}")
            return Response({'error': error_msg}, status=status.HTTP_400_BAD_REQUEST)

        # Success! Redirect back to frontend
        if request.method == 'GET':
            frontend_url = f"https://{tenant_name}.{settings.MAIN_DOMAIN}"
            from urllib.parse import quote_plus
            success_msg = f'Successfully connected {len(saved_accounts)} WhatsApp Business Account(s)'
            return redirect(f"{frontend_url}/social/connections?whatsapp_status=connected&accounts={len(saved_accounts)}&message={quote_plus(success_msg)}")

        return Response({
            'status': 'success',
            'message': f'Successfully connected {len(saved_accounts)} WhatsApp Business Account(s)',
            'accounts': saved_accounts
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"WhatsApp embedded signup callback failed: {e}")
        error_msg = f'Failed to process WhatsApp signup: {str(e)}'
        if request.method == 'GET':
            frontend_url = f"https://{tenant_name if 'tenant_name' in locals() else 'amanati'}.{settings.MAIN_DOMAIN}"
            from urllib.parse import quote_plus
            return redirect(f"{frontend_url}/social/connections?whatsapp_status=error&message={quote_plus(error_msg)}")
        return Response({
            'error': error_msg
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def whatsapp_connection_status(request):
    """Check WhatsApp connection status for current tenant"""
    try:
        accounts = WhatsAppBusinessAccount.objects.filter(is_active=True)
        accounts_data = []

        for account in accounts:
            accounts_data.append({
                'id': account.id,
                'waba_id': account.waba_id,
                'business_name': account.business_name,
                'phone_number': account.display_phone_number or account.phone_number,
                'quality_rating': account.quality_rating,
                'is_active': account.is_active,
                'connected_at': account.created_at.isoformat()
            })

        return Response({
            'connected': accounts.exists(),
            'accounts_count': accounts.count(),
            'accounts': accounts_data
        })
    except Exception as e:
        logger.error(f"Failed to get WhatsApp connection status: {e}")
        return Response({
            'error': f'Failed to get connection status: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def whatsapp_disconnect(request):
    """
    Disconnect WhatsApp Business Account(s).

    Always performs soft delete (is_active=False) to preserve messages and
    templates — same pattern as facebook_disconnect / instagram_disconnect.
    Hard CASCADE deletion was timing out behind Daphne for tenants with large
    histories. Use the clear-history endpoint to permanently delete messages.
    """
    try:
        waba_id = request.data.get('waba_id')

        if waba_id:
            account = WhatsAppBusinessAccount.objects.filter(waba_id=waba_id).first()
            if not account:
                return Response({
                    'error': 'WhatsApp Business Account not found'
                }, status=status.HTTP_404_NOT_FOUND)

            business_name = account.business_name
            account.is_active = False
            account.save(update_fields=['is_active'])

            logger.info(
                "✅ WhatsApp soft disconnect: %s deactivated, messages preserved",
                business_name,
            )
            return Response({
                'status': 'success',
                'message': (
                    f'Deactivated WhatsApp Business Account: {business_name}. '
                    'Messages and templates preserved.'
                ),
                'deactivated': {'business_name': business_name},
            })

        accounts = WhatsAppBusinessAccount.objects.all()
        count = accounts.count()
        # Single UPDATE — fast, no cascade traversal.
        deactivated_count = accounts.update(is_active=False)

        logger.info(
            "✅ WhatsApp soft disconnect: %d account(s) deactivated, messages preserved",
            deactivated_count,
        )
        return Response({
            'status': 'success',
            'message': (
                f'Deactivated {count} WhatsApp Business Account(s). '
                'Messages and templates preserved.'
            ),
            'deactivated': {'accounts': deactivated_count},
        })

    except Exception as e:
        logger.error(f"Failed to disconnect WhatsApp: {e}")
        return Response({
            'error': f'Failed to disconnect: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(
    request=WhatsAppSendMessageSerializer,
    responses={
        200: {
            'type': 'object',
            'properties': {
                'success': {'type': 'boolean'},
                'message': {'type': 'string'},
                'whatsapp_message_id': {'type': 'string'}
            }
        },
        400: {
            'type': 'object',
            'properties': {
                'error': {'type': 'string'},
                'details': {'type': 'object'}
            }
        },
        404: {
            'type': 'object',
            'properties': {
                'error': {'type': 'string'}
            }
        }
    },
    description="Send a WhatsApp message",
    summary="Send WhatsApp Message"
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def whatsapp_media_proxy(request, media_id):
    """Proxy WhatsApp media through the backend to avoid browser ORB/CORS blocking.

    WhatsApp Cloud API media URLs (lookaside.fbsbx.com) are temporary and blocked
    by browsers. This endpoint fetches fresh URLs using the media_id and streams
    the content back to the frontend.
    """
    logger = logging.getLogger(__name__)
    waba_id = request.query_params.get('waba_id')
    if not waba_id:
        return Response({'error': 'waba_id required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        account = WhatsAppBusinessAccount.objects.get(waba_id=waba_id)
    except WhatsAppBusinessAccount.DoesNotExist:
        return Response({'error': 'Account not found'}, status=status.HTTP_404_NOT_FOUND)

    if not account.access_token:
        return Response({'error': 'No access token configured'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Step 1: Get fresh download URL from Meta Graph API
        media_info_url = f"https://graph.facebook.com/v23.0/{media_id}"
        media_response = requests.get(
            media_info_url,
            headers={'Authorization': f'Bearer {account.access_token}'},
            timeout=10
        )

        if media_response.status_code != 200:
            # Distinguish "Meta says it's gone" from transient upstream failures
            # so the frontend can show a friendly "expired" message instead of
            # DigitalOcean's generic 502 HTML page (DO replaces any backend 5xx
            # with its branded error page). Meta returns 4xx + error code 100
            # when the media_id has expired — Cloud API attachments only live
            # for a limited window (days for documents, up to 30 days for
            # other types). Anything 5xx from Meta is worth retrying → 502.
            try:
                err_body = media_response.json()
                meta_code = (err_body.get('error') or {}).get('code')
            except ValueError:
                err_body = {}
                meta_code = None
            logger.warning(
                "WhatsApp media info fetch failed: media_id=%s http=%s meta_code=%s body=%s",
                media_id, media_response.status_code, meta_code, str(err_body)[:500],
            )
            if 400 <= media_response.status_code < 500:
                return Response(
                    {
                        'error': 'Media no longer available',
                        'detail': (
                            'WhatsApp has expired this attachment. Ask the sender '
                            'to resend it, or contact support to retrieve from backup.'
                        ),
                        'media_id': media_id,
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(
                {'error': 'Upstream error from WhatsApp Cloud API'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        media_data = media_response.json()
        download_url = media_data.get('url')
        mime_type = media_data.get('mime_type', 'application/octet-stream')

        if not download_url:
            return Response({'error': 'No download URL returned by Meta'}, status=status.HTTP_404_NOT_FOUND)

        # Step 2: Download the actual media binary
        download_response = requests.get(
            download_url,
            headers={'Authorization': f'Bearer {account.access_token}'},
            timeout=30
        )

        if download_response.status_code != 200:
            logger.error(f"Failed to download WhatsApp media {media_id}: {download_response.status_code}")
            return Response({'error': 'Failed to download media'}, status=status.HTTP_502_BAD_GATEWAY)

        # Step 3: Stream back to the client with caching headers
        response = HttpResponse(download_response.content, content_type=mime_type)
        response['Cache-Control'] = 'public, max-age=86400'  # Cache for 24 hours
        response['Access-Control-Allow-Origin'] = '*'
        return response

    except requests.Timeout:
        return Response({'error': 'Media download timed out'}, status=status.HTTP_504_GATEWAY_TIMEOUT)
    except Exception as e:
        logger.error(f"WhatsApp media proxy error for {media_id}: {e}")
        return Response({'error': 'Internal error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanSendSocialMessages])
def whatsapp_send_message(request):
    """Send a WhatsApp message (text and/or media)"""
    from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
    request.parsers = [MultiPartParser(), FormParser(), JSONParser()]

    try:
        serializer = WhatsAppSendMessageSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        to_number = serializer.validated_data['to_number'].lstrip('+')
        message_text = serializer.validated_data.get('message', '')
        waba_id = serializer.validated_data['waba_id']
        reply_to_message_id = serializer.validated_data.get('reply_to_message_id', '').strip()
        media_file = request.FILES.get('media')

        # Validate: must have text or media
        if not message_text and not media_file:
            return Response({'error': 'Message text or media file is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Get WhatsApp Business Account
        try:
            account = WhatsAppBusinessAccount.objects.get(waba_id=waba_id, is_active=True)
        except WhatsAppBusinessAccount.DoesNotExist:
            return Response({
                'error': 'WhatsApp Business Account not found or inactive'
            }, status=status.HTTP_404_NOT_FOUND)

        fb_api_version = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('WHATSAPP_API_VERSION', 'v23.0')
        auth_headers = {'Authorization': f'Bearer {account.access_token}'}

        # Determine message type and payload
        message_type = 'text'
        media_id = None
        media_mime_type = None
        media_filename = None
        attachments_meta = []

        if media_file:
            # Step 1: Upload media to Meta Cloud API
            media_mime_type = media_file.content_type or 'application/octet-stream'
            media_filename = media_file.name or 'file'

            upload_url = f"https://graph.facebook.com/{fb_api_version}/{account.phone_number_id}/media"
            upload_response = requests.post(
                upload_url,
                headers=auth_headers,
                files={'file': (media_filename, media_file, media_mime_type)},
                data={'messaging_product': 'whatsapp', 'type': media_mime_type},
                timeout=60
            )
            upload_data = upload_response.json()

            if upload_response.status_code != 200 or 'id' not in upload_data:
                error_msg = upload_data.get('error', {}).get('message', 'Media upload failed')
                logger.error(f"WhatsApp media upload failed: {error_msg}")
                return Response({'error': f'Media upload failed: {error_msg}'}, status=status.HTTP_400_BAD_REQUEST)

            media_id = upload_data['id']
            logger.info(f"✅ Uploaded WhatsApp media: {media_id} ({media_mime_type})")

            # Step 2: Determine message type from MIME
            if media_mime_type.startswith('image/'):
                message_type = 'image'
            elif media_mime_type.startswith('video/'):
                message_type = 'video'
            elif media_mime_type.startswith('audio/'):
                message_type = 'audio'
            else:
                message_type = 'document'

            # Step 3: Build media message payload
            media_object = {'id': media_id}
            if message_text:
                if message_type in ('image', 'video'):
                    media_object['caption'] = message_text
                elif message_type == 'document':
                    media_object['caption'] = message_text
                    media_object['filename'] = media_filename

            if message_type == 'document' and 'filename' not in media_object:
                media_object['filename'] = media_filename

            message_payload = {
                'messaging_product': 'whatsapp',
                'to': to_number,
                'type': message_type,
                message_type: media_object
            }
            if reply_to_message_id:
                message_payload['context'] = {'message_id': reply_to_message_id}

            attachments_meta = [{
                'media_id': media_id,
                'mime_type': media_mime_type,
                'filename': media_filename,
            }]
        else:
            # Text-only message
            message_payload = {
                'messaging_product': 'whatsapp',
                'to': to_number,
                'type': 'text',
                'text': {'body': message_text}
            }
            if reply_to_message_id:
                message_payload['context'] = {'message_id': reply_to_message_id}

        # Send message via WhatsApp Cloud API
        send_url = f"https://graph.facebook.com/{fb_api_version}/{account.phone_number_id}/messages"
        headers = {**auth_headers, 'Content-Type': 'application/json'}

        logger.info(f"Sending WhatsApp {message_type} message to {to_number} from {account.display_phone_number}")
        response = requests.post(send_url, json=message_payload, headers=headers, timeout=30)
        response_data = response.json()

        if response.status_code != 200 or 'error' in response_data:
            error_msg = response_data.get('error', {}).get('message', 'Unknown error')
            logger.error(f"Failed to send WhatsApp message: {error_msg}")
            return Response({
                'error': f'Failed to send message: {error_msg}'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Save sent message to database
        message_id = response_data.get('messages', [{}])[0].get('id', '')
        if message_id:
            from .consumers import send_new_message_notification
            from django.db import connection

            timestamp = timezone.now()
            create_kwargs = dict(
                business_account=account,
                message_id=message_id,
                from_number=account.phone_number,
                to_number=to_number,
                message_text=message_text,
                message_type=message_type,
                timestamp=timestamp,
                is_from_business=True,
                status='sent',
                sent_by=request.user,
            )
            if media_mime_type:
                create_kwargs['media_mime_type'] = media_mime_type
            if attachments_meta:
                create_kwargs['attachments'] = attachments_meta
            if reply_to_message_id:
                create_kwargs['reply_to_message_id'] = reply_to_message_id
                reply_to_obj = WhatsAppMessage.objects.filter(message_id=reply_to_message_id).first()
                if reply_to_obj:
                    create_kwargs['reply_to'] = reply_to_obj

            wa_message = WhatsAppMessage.objects.create(**create_kwargs)
            logger.info(f"✅ Saved sent WhatsApp message: {message_id} (type={message_type})")

            # Unarchive conversation if it was archived (move back to active when sending a message)
            unarchived = ConversationArchive.objects.filter(
                platform='whatsapp',
                conversation_id=to_number,
                account_id=waba_id
            ).delete()[0]
            if unarchived:
                logger.info(f"📤 Unarchived conversation due to sent message: whatsapp/{waba_id}/{to_number}")

            # Delete completed assignments to allow new sessions
            deleted_assignment = ChatAssignment.objects.filter(
                platform='whatsapp',
                conversation_id=to_number,
                account_id=waba_id,
                status='completed'
            ).delete()[0]
            if deleted_assignment:
                logger.info(f"🔄 Deleted completed assignment for new conversation: whatsapp/{waba_id}/{to_number}")

            # Broadcast via WebSocket so the message appears in real-time
            try:
                tenant_schema = connection.schema_name

                # Look up recipient's name (contact_name) from previous incoming messages
                recipient_name = None
                previous_msg = WhatsAppMessage.objects.filter(
                    business_account=account,
                    from_number=to_number,
                    is_from_business=False
                ).order_by('-timestamp').first()
                if previous_msg and previous_msg.contact_name:
                    recipient_name = previous_msg.contact_name

                message_data = {
                    'id': wa_message.id,
                    'message_id': wa_message.message_id,
                    'platform': 'whatsapp',
                    'from_number': account.phone_number,
                    'to_number': to_number,
                    'contact_name': recipient_name,
                    'recipient_name': recipient_name,
                    'message_text': message_text,
                    'message_type': message_type,
                    'media_mime_type': media_mime_type,
                    'attachments': attachments_meta,
                    'timestamp': timestamp.isoformat(),
                    'is_from_business': True,
                    'waba_id': waba_id,
                    'sent_by': request.user.email if request.user else None,
                    'reply_to_message_id': wa_message.reply_to_message_id,
                    'reply_to_id': wa_message.reply_to_id,
                }
                async_to_sync(send_new_message_notification)(tenant_schema, to_number, message_data)
            except Exception as e:
                logger.warning(f"Failed to broadcast sent message: {e}")

        return Response({
            'status': 'success',
            'message': 'Message sent successfully',
            'message_id': message_id
        })

    except Exception as e:
        logger.error(f"Failed to send WhatsApp message: {e}")
        return Response({
            'error': f'Failed to send message: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request):
    """Handle WhatsApp webhook events for messages"""
    if request.method == 'GET':
        # Webhook verification
        mode = request.GET.get('hub.mode')
        token = request.GET.get('hub.verify_token')
        challenge = request.GET.get('hub.challenge')

        verify_token = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('WHATSAPP_VERIFY_TOKEN', '')

        if mode == 'subscribe' and verify_token and token == verify_token:
            logger.info("✅ WhatsApp webhook verified successfully")
            return HttpResponse(challenge, content_type='text/plain')
        else:
            logger.warning(f"❌ WhatsApp webhook verification failed - invalid token")
            return JsonResponse({
                'error': 'Invalid verify token or mode'
            }, status=403)

    elif request.method == 'POST':
        # Handle webhook events
        try:
            import json
            from tenant_schemas.utils import schema_context

            data = json.loads(request.body)
            logger.info(f"📱 WhatsApp webhook received: {json.dumps(data, indent=2)}")

            # Extract phone number ID to determine which tenant
            entry = data.get('entry', [{}])[0]
            changes = entry.get('changes', [{}])[0]
            value = changes.get('value', {})
            metadata = value.get('metadata', {})
            phone_number_id = metadata.get('phone_number_id')

            if not phone_number_id:
                logger.error("No phone_number_id found in webhook data")
                return JsonResponse({'status': 'error', 'message': 'No phone_number_id found'}, status=400)

            # Find tenant for this phone number
            tenant_schema = find_tenant_by_whatsapp_phone_number_id(phone_number_id)
            if not tenant_schema:
                logger.warning(f"No tenant found for phone_number_id: {phone_number_id} — ignoring unregistered webhook")
                return JsonResponse({'status': 'ignored', 'message': 'No tenant found for this phone_number_id'}, status=200)

            logger.info(f"Processing WhatsApp webhook for tenant: {tenant_schema}")

            # Process webhook within tenant context
            with schema_context(tenant_schema):
                # Get business account
                try:
                    account = WhatsAppBusinessAccount.objects.get(
                        phone_number_id=phone_number_id,
                        is_active=True
                    )
                except WhatsAppBusinessAccount.DoesNotExist:
                    logger.error(f"No active WhatsApp Business Account found for phone_number_id: {phone_number_id}")
                    return JsonResponse({'status': 'error', 'message': 'Account not found'}, status=404)

                # Get the webhook field to determine the type of event
                webhook_field = changes.get('field', 'messages')

                # Handle messages
                messages = value.get('messages', [])
                for message in messages:
                    message_id = message.get('id')
                    from_number = message.get('from')
                    timestamp = message.get('timestamp', '')
                    message_type = message.get('type', 'text')

                    # Handle edit message type (coexistence feature)
                    if message_type == 'edit':
                        edit_data = message.get('edit', {})
                        original_msg_id = edit_data.get('message_id')
                        new_text = edit_data.get('text', {}).get('body', '')

                        try:
                            existing_msg = WhatsAppMessage.objects.get(message_id=original_msg_id)
                            # Store original text before updating
                            if not existing_msg.original_text:
                                existing_msg.original_text = existing_msg.message_text
                            existing_msg.message_text = new_text
                            existing_msg.is_edited = True
                            existing_msg.edited_at = timezone.now()
                            existing_msg.save()
                            logger.info(f"✏️ Updated edited WhatsApp message: {original_msg_id}")
                        except WhatsAppMessage.DoesNotExist:
                            logger.warning(f"Cannot find original message for edit: {original_msg_id}")
                        continue

                    # Handle revoke message type (coexistence feature)
                    if message_type == 'revoke':
                        revoke_data = message.get('revoke', {})
                        revoked_msg_id = revoke_data.get('message_id')

                        try:
                            existing_msg = WhatsAppMessage.objects.get(message_id=revoked_msg_id)
                            existing_msg.is_revoked = True
                            existing_msg.revoked_at = timezone.now()
                            existing_msg.save()
                            logger.info(f"🗑️ Marked WhatsApp message as revoked: {revoked_msg_id}")
                        except WhatsAppMessage.DoesNotExist:
                            logger.warning(f"Cannot find message to revoke: {revoked_msg_id}")
                        continue

                    # Handle reaction message type
                    if message_type == 'reaction':
                        reaction_data = message.get('reaction', {})
                        reacted_msg_id = reaction_data.get('message_id')
                        emoji = reaction_data.get('emoji', '')

                        try:
                            existing_msg = WhatsAppMessage.objects.get(message_id=reacted_msg_id)
                            if emoji:
                                # Add reaction
                                existing_msg.reaction_emoji = emoji
                                existing_msg.reacted_by = from_number
                                existing_msg.reacted_at = timezone.now()
                                logger.info(f"😀 Added reaction {emoji} to WhatsApp message: {reacted_msg_id}")
                            else:
                                # Empty emoji means reaction removed
                                existing_msg.reaction_emoji = None
                                existing_msg.reacted_by = None
                                existing_msg.reacted_at = None
                                logger.info(f"🚫 Removed reaction from WhatsApp message: {reacted_msg_id}")
                            existing_msg.save()

                            # Typed reaction frame so beta patches the bubble live.
                            # WA matches by platform message id (reacted_msg_id).
                            from .consumers import send_reaction_update
                            _waba_id = getattr(getattr(existing_msg, 'business_account', None), 'waba_id', None) or account.waba_id
                            async_to_sync(send_reaction_update)(
                                tenant_schema,
                                platform='whatsapp',
                                conversation_id=from_number,
                                account_id=_waba_id,
                                message_id=reacted_msg_id,
                                reaction_emoji=existing_msg.reaction_emoji,
                            )
                        except WhatsAppMessage.DoesNotExist:
                            logger.warning(f"Cannot find message for reaction: {reacted_msg_id}")
                        continue

                    # Skip if message already exists
                    if WhatsAppMessage.objects.filter(message_id=message_id).exists():
                        logger.info(f"Skipping duplicate message: {message_id}")
                        continue

                    # Extract reply context if this message is a reply
                    context_data = message.get('context', {})
                    reply_to_wa_msg_id = context_data.get('id')
                    reply_to_obj = None
                    if reply_to_wa_msg_id:
                        reply_to_obj = WhatsAppMessage.objects.filter(
                            message_id=reply_to_wa_msg_id
                        ).first()
                        logger.info(f"Reply to message: {reply_to_wa_msg_id}")

                    # Extract message content based on type
                    message_text = ''
                    media_url = ''
                    media_mime_type = ''
                    attachments = []

                    if message_type == 'text':
                        message_text = message.get('text', {}).get('body', '')
                    elif message_type == 'image':
                        image_data = message.get('image', {})
                        message_text = image_data.get('caption', '')
                        media_id = image_data.get('id', '')
                        media_mime_type = image_data.get('mime_type', '')
                        # Fetch media URL using WhatsApp API
                        if media_id and account.access_token:
                            try:
                                media_info_url = f"https://graph.facebook.com/v23.0/{media_id}"
                                media_response = requests.get(
                                    media_info_url,
                                    headers={'Authorization': f'Bearer {account.access_token}'},
                                    timeout=10
                                )
                                if media_response.status_code == 200:
                                    media_url = media_response.json().get('url', '')
                                    logger.info(f"📎 Fetched WhatsApp image URL: {media_url[:50]}...")
                            except Exception as e:
                                logger.error(f"Failed to fetch WhatsApp media URL: {e}")
                        attachments.append({
                            'type': 'image',
                            'media_id': media_id,
                            'url': media_url,
                            'mime_type': media_mime_type,
                        })
                    elif message_type == 'video':
                        video_data = message.get('video', {})
                        message_text = video_data.get('caption', '')
                        media_id = video_data.get('id', '')
                        media_mime_type = video_data.get('mime_type', '')
                        if media_id and account.access_token:
                            try:
                                media_info_url = f"https://graph.facebook.com/v23.0/{media_id}"
                                media_response = requests.get(
                                    media_info_url,
                                    headers={'Authorization': f'Bearer {account.access_token}'},
                                    timeout=10
                                )
                                if media_response.status_code == 200:
                                    media_url = media_response.json().get('url', '')
                                    logger.info(f"📎 Fetched WhatsApp video URL")
                            except Exception as e:
                                logger.error(f"Failed to fetch WhatsApp media URL: {e}")
                        attachments.append({
                            'type': 'video',
                            'media_id': media_id,
                            'url': media_url,
                            'mime_type': media_mime_type,
                        })
                    elif message_type == 'document':
                        doc_data = message.get('document', {})
                        message_text = doc_data.get('filename', '')
                        media_id = doc_data.get('id', '')
                        media_mime_type = doc_data.get('mime_type', '')
                        if media_id and account.access_token:
                            try:
                                media_info_url = f"https://graph.facebook.com/v23.0/{media_id}"
                                media_response = requests.get(
                                    media_info_url,
                                    headers={'Authorization': f'Bearer {account.access_token}'},
                                    timeout=10
                                )
                                if media_response.status_code == 200:
                                    media_url = media_response.json().get('url', '')
                            except Exception as e:
                                logger.error(f"Failed to fetch WhatsApp media URL: {e}")
                        attachments.append({
                            'type': 'document',
                            'media_id': media_id,
                            'url': media_url,
                            'mime_type': media_mime_type,
                            'filename': doc_data.get('filename', ''),
                        })
                    elif message_type == 'audio':
                        audio_data = message.get('audio', {})
                        media_id = audio_data.get('id', '')
                        media_mime_type = audio_data.get('mime_type', '')
                        if media_id and account.access_token:
                            try:
                                media_info_url = f"https://graph.facebook.com/v23.0/{media_id}"
                                media_response = requests.get(
                                    media_info_url,
                                    headers={'Authorization': f'Bearer {account.access_token}'},
                                    timeout=10
                                )
                                if media_response.status_code == 200:
                                    media_url = media_response.json().get('url', '')
                                    logger.info(f"📎 Fetched WhatsApp audio URL")
                            except Exception as e:
                                logger.error(f"Failed to fetch WhatsApp media URL: {e}")
                        attachments.append({
                            'type': 'audio',
                            'media_id': media_id,
                            'url': media_url,
                            'mime_type': media_mime_type,
                        })
                    elif message_type == 'sticker':
                        sticker_data = message.get('sticker', {})
                        media_id = sticker_data.get('id', '')
                        media_mime_type = sticker_data.get('mime_type', 'image/webp')
                        if media_id and account.access_token:
                            try:
                                media_info_url = f"https://graph.facebook.com/v23.0/{media_id}"
                                media_response = requests.get(
                                    media_info_url,
                                    headers={'Authorization': f'Bearer {account.access_token}'},
                                    timeout=10
                                )
                                if media_response.status_code == 200:
                                    media_url = media_response.json().get('url', '')
                                    logger.info(f"📎 Fetched WhatsApp sticker URL")
                            except Exception as e:
                                logger.error(f"Failed to fetch WhatsApp media URL: {e}")
                        attachments.append({
                            'type': 'sticker',
                            'media_id': media_id,
                            'url': media_url,
                            'mime_type': media_mime_type,
                        })
                    elif message_type == 'location':
                        # Shared location: lat/long (+ optional name/address).
                        # No media to fetch — stored as a structured attachment
                        # the frontend renders as an "Open in Maps" card. We
                        # also drop a Google Maps URL into message_text so the
                        # sidebar preview + any non-location-aware surface still
                        # shows something useful.
                        loc = message.get('location', {})
                        lat = loc.get('latitude')
                        lng = loc.get('longitude')
                        loc_name = loc.get('name', '') or ''
                        loc_address = loc.get('address', '') or ''
                        maps_url = (
                            f"https://www.google.com/maps?q={lat},{lng}"
                            if lat is not None and lng is not None else ''
                        )
                        message_text = loc_name or loc_address or maps_url
                        attachments.append({
                            'type': 'location',
                            'latitude': lat,
                            'longitude': lng,
                            'name': loc_name,
                            'address': loc_address,
                            'url': maps_url,
                        })

                    # Get contact name
                    contacts = value.get('contacts', [])
                    contact_name = ''
                    if contacts:
                        profile = contacts[0].get('profile', {})
                        contact_name = profile.get('name', '')

                    # Save message
                    message_timestamp = datetime.fromtimestamp(int(timestamp), tz=timezone.utc) if timestamp else timezone.now()

                    message_obj = WhatsAppMessage.objects.create(
                        business_account=account,
                        message_id=message_id,
                        from_number=from_number,
                        to_number=account.phone_number,
                        contact_name=contact_name,
                        message_text=message_text,
                        message_type=message_type,
                        media_url=media_url,
                        media_mime_type=media_mime_type,
                        attachments=attachments,
                        timestamp=message_timestamp,
                        is_from_business=False,
                        status='delivered',
                        is_delivered=True,
                        delivered_at=timezone.now(),
                        reply_to_message_id=reply_to_wa_msg_id,
                        reply_to=reply_to_obj,
                    )

                    logger.info(f"✅ Saved WhatsApp message from {contact_name or from_number}: {message_text[:50] if message_text else f'[{message_type}]'}")

                    # Check if this is a rating response
                    process_potential_rating_response(
                        message_text=message_text,
                        platform='whatsapp',
                        conversation_id=from_number.lstrip('+'),  # Remove + for matching
                        account_id=account.waba_id,
                        message_id=message_id
                    )

                    # Auto-unarchive if conversation was in history
                    auto_unarchive_conversation(
                        platform='whatsapp',
                        conversation_id=from_number,
                        account_id=account.waba_id
                    )

                    # Process auto-reply (doesn't mark as read)
                    process_auto_reply(
                        platform='whatsapp',
                        account_id=account.waba_id,
                        conversation_id=from_number,
                        sender_name=contact_name,
                        connection=account
                    )

                    # Send WebSocket notification
                    ws_message_data = {
                        'id': message_obj.id,
                        'message_id': message_obj.message_id,
                        'from_number': message_obj.from_number,
                        'contact_name': message_obj.contact_name,
                        'message_text': message_obj.message_text,
                        'message_type': message_obj.message_type,
                        'media_url': message_obj.media_url,
                        'attachments': message_obj.attachments,
                        'timestamp': message_obj.timestamp.isoformat(),
                        'is_from_business': message_obj.is_from_business,
                        'platform': 'whatsapp',
                        'account_id': account.waba_id,
                        'waba_id': account.waba_id,
                        'chat_id': f'wa_{account.waba_id}_{from_number}',
                        'reply_to_message_id': message_obj.reply_to_message_id,
                        'reply_to_id': message_obj.reply_to_id,
                    }
                    # Get assignment for notification filtering
                    assigned_user_id = get_assignment_for_conversation(
                        platform='whatsapp',
                        conversation_id=from_number,
                        account_id=account.waba_id
                    )
                    send_websocket_notification(tenant_schema, ws_message_data, from_number, assigned_user_id)

                    # Create in-app notification for incoming customer messages
                    try:
                        from users.notification_utils import create_social_message_notification
                        create_social_message_notification(
                            platform='WhatsApp',
                            sender_name=contact_name or from_number,
                            message_text=message_text,
                            conversation_id=from_number,
                            sender_id=from_number,
                            account_id=account.waba_id,
                            assigned_user_id=assigned_user_id,
                        )
                    except Exception as notif_err:
                        logger.error(f"Failed to create WhatsApp message notification: {notif_err}")

                # Handle message status updates
                statuses = value.get('statuses', [])
                for status_update in statuses:
                    message_id = status_update.get('id')
                    status_value = status_update.get('status')  # sent, delivered, read, failed
                    timestamp_value = status_update.get('timestamp', '')

                    # Update message status
                    try:
                        message_obj = WhatsAppMessage.objects.get(message_id=message_id)
                        message_obj.status = status_value

                        if status_value == 'delivered':
                            message_obj.is_delivered = True
                            message_obj.delivered_at = datetime.fromtimestamp(int(timestamp_value), tz=timezone.utc) if timestamp_value else timezone.now()
                        elif status_value == 'read':
                            message_obj.is_read = True
                            message_obj.read_at = datetime.fromtimestamp(int(timestamp_value), tz=timezone.utc) if timestamp_value else timezone.now()
                        elif status_value == 'failed':
                            error_info = status_update.get('errors', [{}])[0]
                            message_obj.error_message = error_info.get('message', 'Failed to deliver')

                        message_obj.save()
                        logger.info(f"✅ Updated WhatsApp message status: {message_id} -> {status_value}")
                    except WhatsAppMessage.DoesNotExist:
                        logger.warning(f"Message not found for status update: {message_id}")

                # ==================== COEXISTENCE WEBHOOK HANDLERS ====================

                # Handle history webhook (synced messages from WhatsApp Business App)
                if webhook_field == 'history':
                    history_entries = value.get('history', [])
                    for entry in history_entries:
                        history_messages = entry.get('messages', [])
                        for hist_msg in history_messages:
                            hist_msg_id = hist_msg.get('id')

                            # Skip if message already exists
                            if WhatsAppMessage.objects.filter(message_id=hist_msg_id).exists():
                                logger.debug(f"Skipping duplicate history message: {hist_msg_id}")
                                continue

                            hist_from = hist_msg.get('from', '')
                            hist_to = hist_msg.get('to', '')
                            hist_timestamp = hist_msg.get('timestamp', '')
                            hist_type = hist_msg.get('type', 'text')
                            hist_text = ''

                            if hist_type == 'text':
                                hist_text = hist_msg.get('text', {}).get('body', '')
                            elif hist_type == 'image':
                                hist_text = hist_msg.get('image', {}).get('caption', '')
                            elif hist_type == 'video':
                                hist_text = hist_msg.get('video', {}).get('caption', '')
                            elif hist_type == 'document':
                                hist_text = hist_msg.get('document', {}).get('filename', '')

                            # Determine direction: if 'from' is our phone number, it's outbound
                            is_outbound = hist_from == account.phone_number or hist_from == account.phone_number_id

                            message_timestamp = datetime.fromtimestamp(int(hist_timestamp), tz=timezone.utc) if hist_timestamp else timezone.now()

                            WhatsAppMessage.objects.create(
                                business_account=account,
                                message_id=hist_msg_id,
                                from_number=hist_from if not is_outbound else account.phone_number,
                                to_number=hist_to if is_outbound else account.phone_number,
                                message_text=hist_text,
                                message_type=hist_type,
                                timestamp=message_timestamp,
                                is_from_business=is_outbound,
                                source='synced',
                                status='delivered',
                                is_delivered=True,
                            )
                            logger.info(f"📥 Synced history message: {hist_msg_id} (source: synced)")

                    # Update sync timestamp
                    account.history_synced_at = timezone.now()
                    account.save(update_fields=['history_synced_at'])

                # Handle smb_message_echoes webhook (messages sent from WhatsApp Business App)
                if webhook_field == 'smb_message_echoes':
                    # The data is in 'message_echoes' array, not 'smb_message_echoes'
                    echo_messages = value.get('message_echoes', [])
                    for echo_msg in echo_messages:
                        echo_msg_id = echo_msg.get('id')

                        # Skip if message already exists
                        if WhatsAppMessage.objects.filter(message_id=echo_msg_id).exists():
                            logger.debug(f"Skipping duplicate echo message: {echo_msg_id}")
                            continue

                        echo_from = echo_msg.get('from', '')
                        echo_to = echo_msg.get('to', '')
                        echo_timestamp = echo_msg.get('timestamp', '')
                        echo_type = echo_msg.get('type', 'text')
                        echo_text = ''

                        if echo_type == 'text':
                            echo_text = echo_msg.get('text', {}).get('body', '')
                        elif echo_type == 'image':
                            echo_text = echo_msg.get('image', {}).get('caption', '')
                        elif echo_type == 'video':
                            echo_text = echo_msg.get('video', {}).get('caption', '')
                        elif echo_type == 'document':
                            echo_text = echo_msg.get('document', {}).get('filename', '')
                        elif echo_type == 'audio':
                            echo_text = '[Audio]'
                        elif echo_type == 'sticker':
                            echo_text = '[Sticker]'

                        message_timestamp = datetime.fromtimestamp(int(echo_timestamp), tz=timezone.utc) if echo_timestamp else timezone.now()

                        echo_message_obj = WhatsAppMessage.objects.create(
                            business_account=account,
                            message_id=echo_msg_id,
                            from_number=echo_from or account.phone_number,
                            to_number=echo_to,
                            message_text=echo_text,
                            message_type=echo_type,
                            timestamp=message_timestamp,
                            is_from_business=True,
                            source='business_app',
                            is_echo=True,
                            status='sent',
                        )
                        logger.info(f"📤 Received SMB echo message: {echo_msg_id} to {echo_to}: {echo_text[:50] if echo_text else f'[{echo_type}]'}")

                        # Send WebSocket notification for echo
                        ws_echo_data = {
                            'id': echo_message_obj.id,
                            'message_id': echo_message_obj.message_id,
                            'from_number': echo_message_obj.from_number,
                            'to_number': echo_message_obj.to_number,
                            'message_text': echo_message_obj.message_text,
                            'message_type': echo_message_obj.message_type,
                            'timestamp': echo_message_obj.timestamp.isoformat(),
                            'is_from_business': True,
                            'is_echo': True,
                            'source': 'business_app',
                        }
                        send_websocket_notification(tenant_schema, ws_echo_data, echo_to)

                # Handle account_update webhook (coexistence account status changes)
                if webhook_field == 'account_update':
                    event = value.get('event')
                    logger.info(f"📱 WhatsApp account_update event: {event}")

                    if event == 'PARTNER_REMOVED':
                        # User removed Cloud API partner - disable coexistence
                        account.is_on_biz_app = False
                        account.coex_enabled = False
                        account.save(update_fields=['is_on_biz_app', 'coex_enabled'])
                        logger.warning(f"⚠️ WhatsApp partner removed for account {account.id}, coexistence disabled")

                    elif event == 'VERIFIED_ACCOUNT':
                        # Account verified - may need to refresh status
                        logger.info(f"✅ WhatsApp account verified: {account.id}")

                    elif event == 'PHONE_NUMBER_NAME_UPDATE':
                        # Phone number display name updated
                        new_name = value.get('new_name_info', {}).get('new_name', '')
                        if new_name:
                            account.verified_name = new_name
                            account.save(update_fields=['verified_name'])
                            logger.info(f"📝 WhatsApp phone name updated: {new_name}")

            return JsonResponse({'status': 'success'})

        except Exception as e:
            logger.error(f"WhatsApp webhook processing failed: {e}")
            return JsonResponse({
                'status': 'error',
                'message': str(e)
            }, status=500)


# ==================== WHATSAPP TEMPLATE MANAGEMENT ====================

@extend_schema(
    summary="List WhatsApp message templates",
    description="Fetch all message templates for a WhatsApp Business Account from the database and optionally sync from Meta",
    responses={200: WhatsAppMessageTemplateSerializer(many=True)}
)
@api_view(['GET'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def whatsapp_list_templates(request, waba_id):
    """List all WhatsApp message templates for a given WABA"""
    tenant_schema = request.tenant.schema_name

    with schema_context(tenant_schema):
        try:
            # Get WABA
            waba = WhatsAppBusinessAccount.objects.get(waba_id=waba_id)

            # Get templates from database
            templates = WhatsAppMessageTemplate.objects.filter(business_account=waba)

            serializer = WhatsAppMessageTemplateSerializer(templates, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)

        except WhatsAppBusinessAccount.DoesNotExist:
            return Response({
                'error': 'WhatsApp Business Account not found'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Error listing WhatsApp templates: {e}")
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(
    summary="Sync WhatsApp templates from Meta",
    description="Fetch all templates from Meta's API and sync them to the database",
    responses={200: WhatsAppMessageTemplateSerializer(many=True)}
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def whatsapp_sync_templates(request, waba_id):
    """Sync templates from Meta API to database"""
    tenant_schema = request.tenant.schema_name

    with schema_context(tenant_schema):
        try:
            # Get WABA
            waba = WhatsAppBusinessAccount.objects.get(waba_id=waba_id)

            # Fetch templates from Meta
            fb_api_version = settings.FACEBOOK_APP_VERSION
            templates_url = f"https://graph.facebook.com/{fb_api_version}/{waba_id}/message_templates"
            params = {
                'access_token': waba.access_token,
                'fields': 'id,name,language,status,category,components',
                'limit': 100
            }

            response = requests.get(templates_url, params=params, timeout=30)

            if response.status_code != 200:
                return Response({
                    'error': 'Failed to fetch templates from Meta',
                    'details': response.json()
                }, status=response.status_code)

            meta_templates = response.json().get('data', [])

            # Sync to database
            synced_count = 0
            for meta_template in meta_templates:
                template_obj, created = WhatsAppMessageTemplate.objects.update_or_create(
                    business_account=waba,
                    name=meta_template['name'],
                    language=meta_template['language'],
                    defaults={
                        'template_id': meta_template['id'],
                        'status': meta_template['status'],
                        'category': meta_template.get('category', 'UTILITY'),
                        'components': meta_template.get('components', []),
                        'created_by': request.user
                    }
                )
                synced_count += 1

            # Get all templates after sync
            templates = WhatsAppMessageTemplate.objects.filter(business_account=waba)
            serializer = WhatsAppMessageTemplateSerializer(templates, many=True)

            return Response({
                'synced': synced_count,
                'templates': serializer.data
            }, status=status.HTTP_200_OK)

        except WhatsAppBusinessAccount.DoesNotExist:
            return Response({
                'error': 'WhatsApp Business Account not found'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Error syncing WhatsApp templates: {e}")
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(
    summary="Create WhatsApp message template",
    description="Create a new message template via Meta's API",
    request=WhatsAppTemplateCreateSerializer,
    responses={201: WhatsAppMessageTemplateSerializer}
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def whatsapp_create_template(request):
    """Create a new WhatsApp message template"""
    tenant_schema = request.tenant.schema_name

    with schema_context(tenant_schema):
        serializer = WhatsAppTemplateCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            waba_id = serializer.validated_data['waba_id']
            waba = WhatsAppBusinessAccount.objects.get(waba_id=waba_id)

            # Create template via Meta API
            fb_api_version = settings.FACEBOOK_APP_VERSION
            create_url = f"https://graph.facebook.com/{fb_api_version}/{waba_id}/message_templates"

            payload = {
                'name': serializer.validated_data['name'],
                'language': serializer.validated_data['language'],
                'category': serializer.validated_data['category'],
                'components': serializer.validated_data['components']
            }

            headers = {
                'Authorization': f'Bearer {waba.access_token}',
                'Content-Type': 'application/json'
            }

            response = requests.post(create_url, json=payload, headers=headers, timeout=30)

            if response.status_code not in [200, 201]:
                return Response({
                    'error': 'Failed to create template in Meta',
                    'details': response.json()
                }, status=response.status_code)

            meta_response = response.json()

            # Save to database
            template = WhatsAppMessageTemplate.objects.create(
                business_account=waba,
                template_id=meta_response.get('id', ''),
                name=serializer.validated_data['name'],
                language=serializer.validated_data['language'],
                status='PENDING',  # New templates start as pending
                category=serializer.validated_data['category'],
                components=serializer.validated_data['components'],
                created_by=request.user
            )

            response_serializer = WhatsAppMessageTemplateSerializer(template)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)

        except WhatsAppBusinessAccount.DoesNotExist:
            return Response({
                'error': 'WhatsApp Business Account not found'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Error creating WhatsApp template: {e}")
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(
    summary="Delete WhatsApp message template",
    description="Delete a message template from both Meta and database",
    responses={200: {'description': 'Template deleted successfully'}}
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def whatsapp_delete_template(request, template_id):
    """Delete a WhatsApp message template"""
    tenant_schema = request.tenant.schema_name

    with schema_context(tenant_schema):
        try:
            template = WhatsAppMessageTemplate.objects.get(id=template_id)
            waba = template.business_account

            # Delete from Meta if template_id exists
            if template.template_id:
                fb_api_version = settings.FACEBOOK_APP_VERSION
                delete_url = f"https://graph.facebook.com/{fb_api_version}/{waba.waba_id}/message_templates"
                params = {
                    'access_token': waba.access_token,
                    'name': template.name
                }

                response = requests.delete(delete_url, params=params, timeout=30)

                if response.status_code not in [200, 204]:
                    logger.warning(f"Failed to delete template from Meta: {response.json()}")
                    # Continue with database deletion even if Meta deletion fails

            # Delete from database
            template.delete()

            return Response({
                'message': 'Template deleted successfully'
            }, status=status.HTTP_200_OK)

        except WhatsAppMessageTemplate.DoesNotExist:
            return Response({
                'error': 'Template not found'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Error deleting WhatsApp template: {e}")
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _render_template_body(template, parameters):
    """Render a template's BODY component with {{n}} placeholders substituted,
    using the same param ordering Meta receives. Falls back to the template name
    when the template has no BODY text."""
    import re
    parameters = parameters or {}
    body_text = ''
    for component in (template.components or []):
        if component.get('type') == 'BODY':
            body_text = component.get('text', '') or ''
            break
    if not body_text:
        return f"Template: {template.name}"

    keys = list(parameters.keys())

    def _sub(match):
        idx = int(match.group(1))  # 1-based placeholder index
        key = f'param{idx}' if f'param{idx}' in parameters else (keys[idx - 1] if len(keys) >= idx else None)
        if key is not None and key in parameters:
            return str(parameters[key])
        return match.group(0)

    return re.sub(r'\{\{(\d+)\}\}', _sub, body_text)


@extend_schema(
    summary="Send WhatsApp template message",
    description="Send a message using a WhatsApp message template with parameters. "
                "Also used to initiate a brand-new conversation with a number that "
                "has not messaged the business.",
    request=WhatsAppTemplateSendSerializer,
    responses={200: WhatsAppMessageSerializer}
)
@api_view(['POST'])
@permission_classes([IsAuthenticated, CanSendSocialMessages])
def whatsapp_send_template_message(request):
    """Send a WhatsApp template message (also used to initiate new conversations)."""
    from .services.whatsapp_errors import (
        map_graph_error, error_response_body,
        OPT_IN_REQUIRED, TEMPLATE_NOT_APPROVED, US_MARKETING_PAUSED, SEND_FAILED_GENERIC,
    )

    tenant_schema = request.tenant.schema_name

    with schema_context(tenant_schema):
        serializer = WhatsAppTemplateSendSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        waba_id = serializer.validated_data['waba_id']
        template_id = serializer.validated_data['template_id']
        to_number = serializer.validated_data['to_number']
        parameters = serializer.validated_data.get('parameters') or {}
        opt_in_confirmed = serializer.validated_data.get('opt_in_confirmed', False)
        # The rest of the pipeline (conversation key, archive/assignment, broadcast)
        # keys on the BARE number, matching the normal send path and the wa_ chatId.
        normalized_to = to_number.lstrip('+')

        try:
            waba = WhatsAppBusinessAccount.objects.get(waba_id=waba_id)
        except WhatsAppBusinessAccount.DoesNotExist:
            return Response({'error': 'WhatsApp Business Account not found'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            template = WhatsAppMessageTemplate.objects.get(id=template_id)
        except WhatsAppMessageTemplate.DoesNotExist:
            return Response({'error': 'Template not found'}, status=status.HTTP_404_NOT_FOUND)

        def _audit(outcome, error_code=''):
            """Append one compliance row per send attempt (never breaks the send)."""
            try:
                WhatsAppOutboundConsentLog.objects.create(
                    business_account=waba,
                    agent=request.user if request.user.is_authenticated else None,
                    to_number=to_number,
                    template=template,
                    opt_in_confirmed=opt_in_confirmed,
                    outcome=outcome,
                    error_code=error_code or '',
                )
            except Exception as e:
                logger.warning(f"Failed to write WhatsApp consent log: {e}")

        # --- Opt-in guard: a business-initiated send (24h window closed) needs an ack ---
        last_inbound = WhatsAppMessage.objects.filter(
            business_account=waba,
            from_number=normalized_to,
            is_from_business=False,
        ).order_by('-timestamp').first()
        window_open = bool(
            last_inbound and (timezone.now() - last_inbound.timestamp).total_seconds() <= 24 * 3600
        )
        if not window_open and not opt_in_confirmed:
            _audit('blocked', OPT_IN_REQUIRED)
            return Response(error_response_body(OPT_IN_REQUIRED), status=status.HTTP_400_BAD_REQUEST)

        # --- Pre-Graph guards (instant feedback, no API round-trip) ---
        if template.status != 'APPROVED':
            _audit('blocked', TEMPLATE_NOT_APPROVED)
            return Response(error_response_body(TEMPLATE_NOT_APPROVED), status=status.HTTP_400_BAD_REQUEST)

        # Meta paused MARKETING template delivery to US numbers (Apr 2025). +1 is the
        # NANP code (also Canada) — we block conservatively to avoid a guaranteed-fail send.
        if (template.category or '').upper() == 'MARKETING' and normalized_to.startswith('1'):
            _audit('blocked', US_MARKETING_PAUSED)
            return Response(error_response_body(US_MARKETING_PAUSED), status=status.HTTP_400_BAD_REQUEST)

        try:
            # Build template components with parameters
            components = []
            for component in template.components:
                if component.get('type') == 'BODY' and parameters:
                    import re
                    body_text = component.get('text', '')
                    param_placeholders = re.findall(r'\{\{(\d+)\}\}', body_text)
                    body_parameters = []
                    for i, placeholder in enumerate(param_placeholders, 1):
                        param_key = f'param{i}' if f'param{i}' in parameters else (list(parameters.keys())[i-1] if len(parameters) >= i else None)
                        if param_key and param_key in parameters:
                            body_parameters.append({'type': 'text', 'text': str(parameters[param_key])})
                    if body_parameters:
                        components.append({'type': 'body', 'parameters': body_parameters})

            fb_api_version = settings.FACEBOOK_APP_VERSION
            send_url = f"https://graph.facebook.com/{fb_api_version}/{waba.phone_number_id}/messages"
            message_payload = {
                'messaging_product': 'whatsapp',
                'to': normalized_to,
                'type': 'template',
                'template': {'name': template.name, 'language': {'code': template.language}},
            }
            if components:
                message_payload['template']['components'] = components
            headers = {'Authorization': f'Bearer {waba.access_token}', 'Content-Type': 'application/json'}

            response = requests.post(send_url, json=message_payload, headers=headers, timeout=30)
            try:
                response_data = response.json()
            except ValueError:
                response_data = {}

            if response.status_code != 200 or (isinstance(response_data, dict) and 'error' in response_data):
                meta_error = response_data.get('error', {}) if isinstance(response_data, dict) else {}
                error_code, http_status = map_graph_error(meta_error)
                logger.error(f"Failed to send WhatsApp template: {meta_error.get('message', response.text[:200])}")
                _audit('failed', error_code)
                return Response(error_response_body(error_code, meta_error.get('code')), status=http_status)

            message_id = response_data.get('messages', [{}])[0].get('id', '')
            rendered_text = _render_template_body(template, parameters)

            message = WhatsAppMessage.objects.create(
                business_account=waba,
                message_id=message_id,
                from_number=waba.phone_number,
                to_number=normalized_to,
                message_text=rendered_text,
                message_type='template',
                template=template,
                template_parameters=parameters,
                timestamp=timezone.now(),
                is_from_business=True,
                status='sent',
                sent_by=request.user,
            )
            _audit('sent')

            # Surface the (possibly brand-new) conversation in the inbox, exactly like
            # the normal send path: unarchive, clear a completed assignment, broadcast.
            try:
                ConversationArchive.objects.filter(
                    platform='whatsapp', conversation_id=normalized_to, account_id=waba_id,
                ).delete()
                ChatAssignment.objects.filter(
                    platform='whatsapp', conversation_id=normalized_to, account_id=waba_id, status='completed',
                ).delete()
            except Exception as e:
                logger.warning(f"Failed to unarchive/clear assignment for template send: {e}")

            try:
                from .consumers import send_new_message_notification
                recipient_name = None
                previous_msg = WhatsAppMessage.objects.filter(
                    business_account=waba, from_number=normalized_to, is_from_business=False,
                ).order_by('-timestamp').first()
                if previous_msg and previous_msg.contact_name:
                    recipient_name = previous_msg.contact_name
                message_data = {
                    'id': message.id,
                    'message_id': message.message_id,
                    'platform': 'whatsapp',
                    'from_number': waba.phone_number,
                    'to_number': normalized_to,
                    'contact_name': recipient_name,
                    'recipient_name': recipient_name,
                    'message_text': rendered_text,
                    'message_type': 'template',
                    'timestamp': message.timestamp.isoformat(),
                    'is_from_business': True,
                    'waba_id': waba_id,
                    'sent_by': request.user.email if request.user else None,
                }
                async_to_sync(send_new_message_notification)(tenant_schema, normalized_to, message_data)
            except Exception as e:
                logger.warning(f"Failed to broadcast sent template message: {e}")

            return Response(WhatsAppMessageSerializer(message).data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error sending WhatsApp template message: {e}")
            _audit('failed', SEND_FAILED_GENERIC)
            return Response(error_response_body(SEND_FAILED_GENERIC), status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# =============================================================================
# Email Integration Views
# =============================================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def email_connection_status(request):
    """Check Email connection status for current tenant - returns connections user can access"""
    try:
        connections = EmailConnection.objects.all().prefetch_related(
            'user_assignments__user', 'user_assignments__assigned_by'
        )

        is_admin = request.user.is_superuser or request.user.is_staff

        # Helper to check if user can access an email connection
        def can_access_connection(conn):
            # If no user assignments, everyone can access
            if not conn.user_assignments.exists():
                return True
            # Admins can always access
            if is_admin:
                return True
            # Check if user is assigned to this connection
            return conn.user_assignments.filter(user=request.user).exists()

        connections_data = []
        for conn in connections:
            # Skip connections the user cannot access
            if not can_access_connection(conn):
                continue

            # Get assigned users for this connection (only show to admins)
            assigned_users = []
            if is_admin:
                assigned_users = [{
                    'id': assignment.id,
                    'user_id': assignment.user.id,
                    'user_email': assignment.user.email,
                    'user_name': assignment.user.get_full_name() or assignment.user.email,
                    'assigned_at': assignment.assigned_at.isoformat(),
                } for assignment in conn.user_assignments.all()]

            connections_data.append({
                'id': conn.id,
                'email_address': conn.email_address,
                'display_name': conn.display_name,
                'imap_server': conn.imap_server,
                'smtp_server': conn.smtp_server,
                'is_active': conn.is_active,
                'last_sync_at': conn.last_sync_at.isoformat() if conn.last_sync_at else None,
                'last_sync_error': conn.last_sync_error,
                'auto_disabled_at': conn.auto_disabled_at.isoformat() if conn.auto_disabled_at else None,
                'sync_failure_count': conn.sync_failure_count,
                'sync_folder': conn.sync_folder,
                'connected_at': conn.created_at.isoformat(),
                'signature_enabled': conn.signature_enabled,
                'signature_html': conn.signature_html,
                'signature_text': conn.signature_text,
                'assigned_users': assigned_users,
            })

        return Response({
            'connected': len(connections_data) > 0,
            'connections': connections_data,
            # Keep backwards compatibility - return first connection as 'connection'
            'connection': connections_data[0] if connections_data else None
        })
    except Exception as e:
        logger.error(f"Failed to get Email connection status: {e}")
        return Response({
            'error': f'Failed to get connection status: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def email_connect(request):
    """
    Connect email account with IMAP/SMTP credentials.
    Tests both IMAP and SMTP connections before saving.
    """
    from .email_utils import test_imap_connection, test_smtp_connection

    serializer = EmailConnectionCreateSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data

    try:
        # Test IMAP connection (returns tuple: success, error_message)
        imap_success, imap_error = test_imap_connection(
            server=data['imap_server'],
            port=data['imap_port'],
            use_ssl=data['imap_use_ssl'],
            username=data['username'],
            password=data['password']
        )

        if not imap_success:
            return Response({
                'error': 'IMAP connection failed',
                'details': imap_error or 'Unknown error'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Test SMTP connection (returns tuple: success, error_message)
        smtp_success, smtp_error = test_smtp_connection(
            server=data['smtp_server'],
            port=data['smtp_port'],
            use_tls=data['smtp_use_tls'],
            use_ssl=data['smtp_use_ssl'],
            username=data['username'],
            password=data['password']
        )

        if not smtp_success:
            return Response({
                'error': 'SMTP connection failed',
                'details': smtp_error or 'Unknown error'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Check if connection already exists for this email
        existing = EmailConnection.objects.filter(email_address=data['email_address']).first()
        if existing:
            # Update existing connection
            existing.display_name = data.get('display_name', '')
            existing.imap_server = data['imap_server']
            existing.imap_port = data['imap_port']
            existing.imap_use_ssl = data['imap_use_ssl']
            existing.smtp_server = data['smtp_server']
            existing.smtp_port = data['smtp_port']
            existing.smtp_use_tls = data['smtp_use_tls']
            existing.smtp_use_ssl = data['smtp_use_ssl']
            existing.username = data['username']
            existing.set_password(data['password'])
            existing.sync_folder = data.get('sync_folder', 'INBOX')
            existing.sync_days_back = data.get('sync_days_back', 30)
            existing.is_active = True
            existing.last_sync_error = ''
            existing.save()
            connection = existing
            created = False
        else:
            # Create new connection (multiple connections per tenant supported)
            connection = EmailConnection.objects.create(
                email_address=data['email_address'],
                display_name=data.get('display_name', ''),
                imap_server=data['imap_server'],
                imap_port=data['imap_port'],
                imap_use_ssl=data['imap_use_ssl'],
                smtp_server=data['smtp_server'],
                smtp_port=data['smtp_port'],
                smtp_use_tls=data['smtp_use_tls'],
                smtp_use_ssl=data['smtp_use_ssl'],
                username=data['username'],
                sync_folder=data.get('sync_folder', 'INBOX'),
                sync_days_back=data.get('sync_days_back', 30),
                is_active=True
            )
            connection.set_password(data['password'])
            connection.save()
            created = True

        logger.info(f"Email connection {'created' if created else 'updated'}: {connection.email_address}")

        return Response({
            'status': 'success',
            'message': f"Email connected successfully: {connection.email_address}",
            'created': created,
            'connection': EmailConnectionSerializer(connection).data
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Failed to connect email: {e}")
        return Response({
            'error': f'Failed to connect email: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def email_update_connection(request):
    """Update an email connection's settings (display name, signature, etc.)"""
    try:
        connection_id = request.data.get('connection_id')
        if not connection_id:
            return Response({
                'error': 'connection_id is required'
            }, status=status.HTTP_400_BAD_REQUEST)

        connection = EmailConnection.objects.filter(id=connection_id).first()

        if not connection:
            return Response({
                'error': 'Email connection not found'
            }, status=status.HTTP_404_NOT_FOUND)

        # Update allowed fields
        if 'display_name' in request.data:
            connection.display_name = request.data['display_name']
        if 'signature_enabled' in request.data:
            connection.signature_enabled = request.data['signature_enabled']
        if 'signature_html' in request.data:
            connection.signature_html = request.data['signature_html']
        if 'signature_text' in request.data:
            connection.signature_text = request.data['signature_text']

        # IMAP/SMTP connection settings
        if 'imap_server' in request.data:
            connection.imap_server = request.data['imap_server']
        if 'imap_port' in request.data:
            connection.imap_port = int(request.data['imap_port'])
        if 'imap_use_ssl' in request.data:
            connection.imap_use_ssl = request.data['imap_use_ssl']
        if 'smtp_server' in request.data:
            connection.smtp_server = request.data['smtp_server']
        if 'smtp_port' in request.data:
            connection.smtp_port = int(request.data['smtp_port'])
        if 'smtp_use_tls' in request.data:
            connection.smtp_use_tls = request.data['smtp_use_tls']
        if 'smtp_use_ssl' in request.data:
            connection.smtp_use_ssl = request.data['smtp_use_ssl']
        if 'username' in request.data:
            connection.username = request.data['username']
        if 'password' in request.data and request.data['password']:
            connection.password = request.data['password']
        if 'sync_folder' in request.data:
            connection.sync_folder = request.data['sync_folder']
        if 'sync_days_back' in request.data:
            connection.sync_days_back = int(request.data['sync_days_back'])

        connection.save()

        logger.info(f"Email connection updated: {connection.email_address}")
        return Response({
            'message': 'Connection updated successfully',
            'connection': EmailConnectionSerializer(connection).data
        })

    except Exception as e:
        logger.error(f"Failed to update email connection: {e}")
        return Response({
            'error': f'Failed to update connection: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def email_reactivate(request):
    """Reactivate an auto-disabled email connection after the user has fixed
    credentials or server config.

    Body (all optional except connection_id):
    - connection_id (int, required)
    - password (str) — if provided, updated on the connection before testing
    - username, imap_server, imap_port, imap_use_ssl,
      smtp_server, smtp_port, smtp_use_tls, smtp_use_ssl — any of these
      overwrite the stored value before the test

    Runs a live IMAP (and, if creds changed, SMTP) check; only flips
    ``is_active`` back to True and clears ``auto_disabled_at`` if the test
    passes. On failure returns 400 with the reason so the UI can surface it.
    """
    from .email_utils import test_imap_connection, test_smtp_connection

    try:
        connection_id = request.data.get('connection_id')
        if not connection_id:
            return Response(
                {'error': 'connection_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        connection = EmailConnection.objects.filter(id=connection_id).first()
        if not connection:
            return Response(
                {'error': 'Email connection not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Apply any field overrides from the payload (lets the user fix
        # credentials inside the reactivate flow without a separate update).
        overrideable_fields = [
            'username', 'imap_server', 'imap_port', 'imap_use_ssl',
            'smtp_server', 'smtp_port', 'smtp_use_tls', 'smtp_use_ssl',
        ]
        for field in overrideable_fields:
            if field in request.data:
                value = request.data[field]
                if field in {'imap_port', 'smtp_port'}:
                    value = int(value)
                setattr(connection, field, value)

        new_password = request.data.get('password')
        if new_password:
            connection.set_password(new_password)
        password_for_test = new_password or connection.get_password()

        # IMAP test is mandatory — sync needs it.
        imap_ok, imap_error = test_imap_connection(
            server=connection.imap_server,
            port=connection.imap_port,
            use_ssl=connection.imap_use_ssl,
            username=connection.username,
            password=password_for_test,
        )
        if not imap_ok:
            return Response(
                {'error': f'IMAP test failed: {imap_error}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # SMTP test only when the user is providing new creds (outgoing is
        # optional on reactivation — incoming sync is the blocker we care
        # about; if SMTP breaks later, sending will surface its own error).
        if new_password:
            smtp_ok, smtp_error = test_smtp_connection(
                server=connection.smtp_server,
                port=connection.smtp_port,
                username=connection.username,
                password=password_for_test,
                use_tls=connection.smtp_use_tls,
                use_ssl=connection.smtp_use_ssl,
            )
            if not smtp_ok:
                return Response(
                    {'error': f'SMTP test failed: {smtp_error}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        connection.is_active = True
        connection.auto_disabled_at = None
        connection.last_sync_error = ''
        connection.sync_failure_count = 0
        connection.save()

        logger.info(
            f"Email connection reactivated: {connection.email_address} "
            f"by {request.user.email}"
        )
        return Response({
            'message': 'Connection reactivated. Sync will resume on the next cycle.',
            'connection': EmailConnectionSerializer(connection).data,
        })

    except Exception as e:
        logger.error(f"Failed to reactivate email connection: {e}")
        return Response(
            {'error': f'Failed to reactivate connection: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def email_disconnect(request):
    """Disconnect a specific email connection and delete all related data"""
    try:
        connection_id = request.data.get('connection_id')
        if not connection_id:
            return Response({
                'error': 'connection_id is required'
            }, status=status.HTTP_400_BAD_REQUEST)

        connection = EmailConnection.objects.filter(id=connection_id).first()

        if not connection:
            return Response({
                'error': 'Email connection not found'
            }, status=status.HTTP_404_NOT_FOUND)

        email_address = connection.email_address

        # Count related data before deletion
        messages_count = EmailMessage.objects.filter(connection=connection).count()
        drafts_count = EmailDraft.objects.filter(connection=connection).count()

        # Delete the connection (will cascade delete messages and drafts)
        connection.delete()

        logger.info(
            f"Deleted Email connection: {email_address} "
            f"({messages_count} messages, {drafts_count} drafts)"
        )

        return Response({
            'status': 'success',
            'message': f'Disconnected and deleted email: {email_address}',
            'deleted': {
                'messages': messages_count,
                'drafts': drafts_count
            }
        })

    except Exception as e:
        logger.error(f"Failed to disconnect email: {e}")
        return Response({
            'error': f'Failed to disconnect email: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanSendSocialMessages])
def email_send(request):
    """Send an email message (with optional file attachments)"""
    from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
    from .email_utils import send_email_smtp
    request.parsers = [MultiPartParser(), FormParser(), JSONParser()]

    serializer = EmailSendSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Support sending from a specific connection
        connection_id = request.data.get('connection_id')
        if connection_id:
            connection = EmailConnection.objects.filter(id=connection_id, is_active=True).first()
        else:
            connection = EmailConnection.objects.filter(is_active=True).first()
        if not connection:
            return Response({
                'error': 'No active email connection found'
            }, status=status.HTTP_404_NOT_FOUND)

        data = serializer.validated_data

        # Get reply context if replying to a message
        reply_to_message = None
        if data.get('reply_to_message_id'):
            reply_to_message = EmailMessage.objects.filter(
                id=data['reply_to_message_id'],
                connection=connection
            ).first()

        # Build attachment list from uploaded files
        attachment_files = request.FILES.getlist('attachments')
        attachments = []
        for f in attachment_files:
            attachments.append({
                'filename': f.name,
                'content': f.read(),
                'content_type': f.content_type or 'application/octet-stream',
            })

        # Send the email
        result = send_email_smtp(
            connection=connection,
            to_emails=data['to_emails'],
            subject=data.get('subject', ''),
            body_text=data.get('body_text', ''),
            body_html=data.get('body_html', ''),
            cc_emails=data.get('cc_emails', []),
            bcc_emails=data.get('bcc_emails', []),
            reply_to_message_id=reply_to_message.message_id if reply_to_message else None,
            attachments=attachments,
        )

        # send_email_smtp returns (message_id, sent_message) tuple
        message_id, message = result
        return Response({
            'status': 'success',
            'message_id': message.id if message else None,
            'data': EmailMessageSerializer(message).data if message else None
        })

    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return Response({
            'error': f'Failed to send email: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def email_action(request):
    """Perform actions on email messages (star, read, label, move, delete)"""
    serializer = EmailMessageActionSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    try:
        data = serializer.validated_data
        action = data['action']

        # Get messages either by message_ids or thread_id
        if data.get('thread_id'):
            messages = EmailMessage.objects.filter(thread_id=data['thread_id'])
        else:
            messages = EmailMessage.objects.filter(id__in=data['message_ids'])

        affected_count = messages.count()

        if affected_count == 0:
            return Response({
                'error': 'No messages found with the provided IDs'
            }, status=status.HTTP_404_NOT_FOUND)

        if action == 'mark_read':
            messages.update(is_read=True, is_read_by_staff=True, read_by_staff_at=timezone.now())
        elif action == 'mark_unread':
            messages.update(is_read=False, is_read_by_staff=False, read_by_staff_at=None)
        elif action == 'star':
            messages.update(is_starred=True)
        elif action == 'unstar':
            messages.update(is_starred=False)
        elif action == 'delete':
            messages.update(
                is_deleted=True,
                deleted_at=timezone.now(),
                deleted_by=request.user
            )
        elif action == 'restore':
            messages.update(
                is_deleted=False,
                deleted_at=None,
                deleted_by=None
            )
        elif action == 'label':
            label = data['label']
            for msg in messages:
                if label not in msg.labels:
                    msg.labels.append(label)
                    msg.save()
        elif action == 'unlabel':
            label = data['label']
            for msg in messages:
                if label in msg.labels:
                    msg.labels.remove(label)
                    msg.save()
        elif action == 'move':
            folder = data['folder']
            messages.update(folder=folder)

        return Response({
            'status': 'success',
            'action': action,
            'affected_count': affected_count
        })

    except Exception as e:
        logger.error(f"Failed to perform email action: {e}")
        return Response({
            'error': f'Failed to perform action: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def email_folders(request):
    """List available IMAP folders for the connected email(s)

    Query params:
        connection_id: Optional - get folders for specific connection
                       If not provided, returns folders for all active connections
    """
    from .email_utils import get_imap_folders

    try:
        connection_id = request.query_params.get('connection_id')

        if connection_id:
            # Get folders for specific connection
            try:
                connection = EmailConnection.objects.get(id=connection_id, is_active=True)
            except EmailConnection.DoesNotExist:
                return Response({
                    'error': 'Email connection not found or inactive'
                }, status=status.HTTP_404_NOT_FOUND)

            result = get_imap_folders(connection)

            if result['success']:
                return Response({
                    'connection_id': connection.id,
                    'email': connection.email_address,
                    'folders': result['folders']
                })
            else:
                return Response({
                    'error': 'Failed to get folders',
                    'details': result.get('error', 'Unknown error')
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        else:
            # Get folders for all active connections
            connections = EmailConnection.objects.filter(is_active=True)

            if not connections.exists():
                return Response({
                    'error': 'No active email connections found'
                }, status=status.HTTP_404_NOT_FOUND)

            all_connections_folders = []

            for connection in connections:
                result = get_imap_folders(connection)
                connection_data = {
                    'connection_id': connection.id,
                    'email': connection.email_address,
                    'folders': result['folders'] if result['success'] else [],
                    'success': result['success'],
                }
                if not result['success']:
                    connection_data['error'] = result.get('error', 'Unknown error')

                all_connections_folders.append(connection_data)

            return Response({
                'connections': all_connections_folders
            })

    except Exception as e:
        logger.error(f"Failed to get email folders: {e}")
        return Response({
            'error': f'Failed to get folders: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def email_sync(request):
    """Trigger manual email sync - supports syncing specific or all connections"""
    from .email_utils import sync_imap_messages

    try:
        # Support syncing a specific connection or all connections
        connection_id = request.data.get('connection_id')
        if connection_id:
            connections = EmailConnection.objects.filter(id=connection_id, is_active=True)
        else:
            connections = EmailConnection.objects.filter(is_active=True)

        if not connections.exists():
            return Response({
                'error': 'No active email connection found'
            }, status=status.HTTP_404_NOT_FOUND)

        # Perform sync for each connection
        total_new_messages = 0
        sync_results = []
        for connection in connections:
            new_messages = sync_imap_messages(connection)
            total_new_messages += new_messages
            sync_results.append({
                'connection_id': connection.id,
                'email_address': connection.email_address,
                'new_messages': new_messages
            })

        return Response({
            'status': 'success',
            'new_messages': total_new_messages,
            'sync_results': sync_results
        })

    except Exception as e:
        logger.error(f"Failed to sync email: {e}")
        return Response({
            'error': f'Failed to sync email: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def email_sync_debug(request):
    """
    Debug email sync - returns detailed folder info without syncing.
    Shows all folders, message counts, and why some folders might be empty.
    """
    from .email_utils import debug_email_sync

    try:
        connection_id = request.query_params.get('connection_id')
        if connection_id:
            connection = EmailConnection.objects.filter(id=connection_id, is_active=True).first()
        else:
            connection = EmailConnection.objects.filter(is_active=True).first()
        if not connection:
            return Response({
                'error': 'No active email connection found'
            }, status=status.HTTP_404_NOT_FOUND)

        debug_info = debug_email_sync(connection)
        return Response(debug_info)

    except Exception as e:
        logger.error(f"Failed to debug email sync: {e}")
        return Response({
            'error': f'Failed to debug email sync: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def email_update_sync_days(request):
    """
    Update sync_days_back setting for email connection.
    Use 0 to sync ALL history.
    """
    try:
        connection_id = request.data.get('connection_id')
        if connection_id:
            connection = EmailConnection.objects.filter(id=connection_id, is_active=True).first()
        else:
            connection = EmailConnection.objects.filter(is_active=True).first()
        if not connection:
            return Response({
                'error': 'No active email connection found'
            }, status=status.HTTP_404_NOT_FOUND)

        sync_days_back = request.data.get('sync_days_back')
        if sync_days_back is None:
            return Response({
                'error': 'sync_days_back is required'
            }, status=status.HTTP_400_BAD_REQUEST)

        sync_days_back = int(sync_days_back)
        if sync_days_back < 0:
            return Response({
                'error': 'sync_days_back must be 0 or positive'
            }, status=status.HTTP_400_BAD_REQUEST)

        connection.sync_days_back = sync_days_back
        connection.save(update_fields=['sync_days_back'])

        return Response({
            'status': 'success',
            'sync_days_back': sync_days_back,
            'meaning': 'all history' if sync_days_back == 0 else f'{sync_days_back} days'
        })

    except ValueError:
        return Response({
            'error': 'sync_days_back must be an integer'
        }, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error(f"Failed to update sync_days_back: {e}")
        return Response({
            'error': f'Failed to update: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class EmailMessageViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing email messages"""
    serializer_class = EmailMessageSerializer
    permission_classes = [IsAuthenticated, CanViewSocialMessages]

    def get_queryset(self):
        queryset = EmailMessage.objects.filter(
            is_deleted=False
        ).select_related('connection', 'deleted_by').order_by('-timestamp')

        # Filter by connection_id (specific email account)
        connection_id = self.request.query_params.get('connection_id')
        if connection_id:
            queryset = queryset.filter(connection_id=connection_id)

        # Filter by thread_id for conversation view
        thread_id = self.request.query_params.get('thread_id')
        if thread_id:
            queryset = queryset.filter(thread_id=thread_id)

        # Filter by folder
        folder = self.request.query_params.get('folder')
        if folder:
            queryset = queryset.filter(folder=folder)

        # Filter by starred
        starred = self.request.query_params.get('starred')
        if starred and starred.lower() == 'true':
            queryset = queryset.filter(is_starred=True)

        # Filter by read status
        is_read = self.request.query_params.get('is_read')
        if is_read is not None:
            queryset = queryset.filter(is_read=is_read.lower() == 'true')

        # Filter by label
        label = self.request.query_params.get('label')
        if label:
            queryset = queryset.filter(labels__contains=[label])

        # Search in subject, from_email, body_text
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(subject__icontains=search) |
                Q(from_email__icontains=search) |
                Q(from_name__icontains=search) |
                Q(body_text__icontains=search)
            )

        return queryset

    @action(detail=False, methods=['get'])
    def threads(self, request):
        """
        Get email threads (grouped conversations).
        Returns the latest message from each unique thread with customer info.

        When chat_assignment_enabled is True:
        - Only shows threads assigned to the current user or unassigned threads
        - Threads with status='completed' are treated as unassigned
        """
        # Check if chat assignment filtering is enabled
        settings_obj = get_social_settings(self.request)
        assignment_enabled = settings_obj and settings_obj.chat_assignment_enabled

        # Build base queryset
        base_queryset = EmailMessage.objects.filter(is_deleted=False)

        # Filter by connection_id (specific email account)
        connection_id = request.query_params.get('connection_id')
        if connection_id:
            base_queryset = base_queryset.filter(connection_id=connection_id)

        # Filter by folder if specified
        folder = request.query_params.get('folder')
        if folder and folder != 'All':
            base_queryset = base_queryset.filter(folder=folder)

        if assignment_enabled:
            # Get active assignments (not 'completed' - those return to everyone)
            active_assignments = ChatAssignment.objects.filter(
                platform='email',
                status__in=['active', 'in_session']
            )

            my_assignments = active_assignments.filter(
                assigned_user=request.user
            ).values_list('conversation_id', flat=True)

            all_assignments = active_assignments.values_list('conversation_id', flat=True)

            # Filter threads: only mine or unassigned
            base_queryset = base_queryset.filter(
                Q(thread_id__in=my_assignments) |  # Assigned to me
                ~Q(thread_id__in=all_assignments)  # Not assigned to anyone
            )

        # Get distinct thread_ids with their latest message
        threads = base_queryset.values('thread_id').annotate(
            latest_timestamp=Max('timestamp'),
            message_count=Count('id'),
            unread_count=Count('id', filter=Q(is_read_by_staff=False, is_from_business=False))
        ).order_by('-latest_timestamp')

        # Limit to 50 threads and collect thread IDs
        threads_page = list(threads[:50])
        thread_ids = [t['thread_id'] for t in threads_page]

        # Build lookup dicts: {thread_id: timestamp} and {thread_id: counts}
        thread_meta = {
            t['thread_id']: {
                'latest_timestamp': t['latest_timestamp'],
                'message_count': t['message_count'],
                'unread_count': t['unread_count'],
            }
            for t in threads_page
        }

        # Batch query: fetch latest message per thread (by timestamp match)
        latest_msgs = EmailMessage.objects.filter(
            thread_id__in=thread_ids,
            is_deleted=False,
        ).select_related('connection').order_by('thread_id', '-timestamp')

        # Build {thread_id: latest_msg} — pick first per thread_id
        latest_by_thread = {}
        for msg in latest_msgs:
            if msg.thread_id not in latest_by_thread:
                latest_by_thread[msg.thread_id] = msg

        # Batch query: fetch first customer message per thread
        customer_msgs = EmailMessage.objects.filter(
            thread_id__in=thread_ids,
            is_from_business=False,
            is_deleted=False,
        ).order_by('thread_id', 'timestamp')

        customer_by_thread = {}
        for msg in customer_msgs:
            if msg.thread_id not in customer_by_thread:
                customer_by_thread[msg.thread_id] = msg

        # Batch query: fetch first business message per thread (fallback for customer info)
        threads_needing_business = [tid for tid in thread_ids if tid not in customer_by_thread]
        business_by_thread = {}
        if threads_needing_business:
            business_msgs = EmailMessage.objects.filter(
                thread_id__in=threads_needing_business,
                is_from_business=True,
                is_deleted=False,
            ).order_by('thread_id', 'timestamp')

            for msg in business_msgs:
                if msg.thread_id not in business_by_thread:
                    business_by_thread[msg.thread_id] = msg

        # Build result using the pre-fetched data
        result = []
        for thread_id in thread_ids:
            latest_msg = latest_by_thread.get(thread_id)
            if not latest_msg:
                continue

            meta = thread_meta[thread_id]
            msg_data = EmailMessageSerializer(latest_msg).data
            msg_data['message_count'] = meta['message_count']
            msg_data['unread_count'] = meta['unread_count']

            # Find customer email (the external party, not the business)
            customer_msg = customer_by_thread.get(thread_id)
            if customer_msg:
                msg_data['customer_email'] = customer_msg.from_email
                msg_data['customer_name'] = customer_msg.from_name or customer_msg.from_email
            else:
                business_msg = business_by_thread.get(thread_id)
                if business_msg and business_msg.to_emails:
                    first_recipient = business_msg.to_emails[0]
                    if isinstance(first_recipient, dict):
                        msg_data['customer_email'] = first_recipient.get('email', '')
                        msg_data['customer_name'] = first_recipient.get('name') or first_recipient.get('email', '')
                    else:
                        msg_data['customer_email'] = first_recipient
                        msg_data['customer_name'] = first_recipient
                else:
                    msg_data['customer_email'] = ''
                    msg_data['customer_name'] = 'Unknown'

            result.append(msg_data)

        return Response(result)

    @action(detail=False, methods=['get'])
    def folders(self, request):
        """
        Get list of available IMAP folders.
        """
        from .email_utils import get_available_folders

        connection = EmailConnection.objects.filter(is_active=True).first()
        if not connection:
            return Response({'error': 'No active email connection'}, status=400)

        folders = get_available_folders(connection)
        return Response(folders)

    @action(detail=False, methods=['post'])
    def move(self, request):
        """
        Move an email to a different folder.
        Expects: message_id, source_folder, target_folder
        """
        from .email_utils import move_email_to_folder

        message_id = request.data.get('message_id')
        source_folder = request.data.get('source_folder')
        target_folder = request.data.get('target_folder')

        if not all([message_id, source_folder, target_folder]):
            return Response({'error': 'message_id, source_folder, and target_folder are required'}, status=400)

        connection = EmailConnection.objects.filter(is_active=True).first()
        if not connection:
            return Response({'error': 'No active email connection'}, status=400)

        success = move_email_to_folder(connection, message_id, source_folder, target_folder)

        if success:
            # Update the folder in our database
            EmailMessage.objects.filter(message_id=message_id).update(folder=target_folder)
            return Response({'success': True, 'message': f'Email moved to {target_folder}'})
        else:
            return Response({'error': 'Failed to move email'}, status=500)


class EmailDraftViewSet(viewsets.ModelViewSet):
    """ViewSet for managing email drafts"""
    serializer_class = EmailDraftSerializer
    permission_classes = [IsAuthenticated, CanSendSocialMessages]

    def get_queryset(self):
        return EmailDraft.objects.filter(
            created_by=self.request.user
        ).select_related('connection', 'reply_to_message', 'created_by').order_by('-updated_at')

    def perform_create(self, serializer):
        # Get the active email connection
        connection = EmailConnection.objects.filter(is_active=True).first()
        if not connection:
            raise drf_serializers.ValidationError("No active email connection found")

        serializer.save(
            created_by=self.request.user,
            connection=connection
        )


# =============================================================================
# Email User Assignment Views
# =============================================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def email_assignments_list(request):
    """
    List all email connection assignments.
    Returns email connections with their assigned users.
    """
    try:
        connections = EmailConnection.objects.all().prefetch_related('user_assignments__user', 'user_assignments__assigned_by')
        serializer = EmailConnectionWithAssignmentsSerializer(connections, many=True)
        return Response(serializer.data)
    except Exception as e:
        logger.error(f"Failed to list email assignments: {e}")
        return Response({
            'error': f'Failed to list assignments: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def email_connection_assigned_users(request, connection_id):
    """
    GET: Get users assigned to a specific email connection.
    PUT: Update (replace) the assigned users for an email connection.
    """
    try:
        connection = EmailConnection.objects.filter(id=connection_id).first()
        if not connection:
            return Response({
                'error': 'Email connection not found'
            }, status=status.HTTP_404_NOT_FOUND)

        if request.method == 'GET':
            assignments = connection.user_assignments.select_related('user', 'assigned_by').all()
            serializer = EmailConnectionUserAssignmentSerializer(assignments, many=True)
            return Response({
                'connection_id': connection.id,
                'connection_email': connection.email_address,
                'assigned_users': serializer.data
            })

        elif request.method == 'PUT':
            from django.contrib.auth import get_user_model
            User = get_user_model()

            user_ids = request.data.get('user_ids', [])

            # Validate that all user IDs exist
            existing_users = User.objects.filter(id__in=user_ids)
            if len(existing_users) != len(user_ids):
                return Response({
                    'error': 'One or more user IDs are invalid'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Remove existing assignments
            connection.user_assignments.all().delete()

            # Create new assignments
            new_assignments = []
            for user_id in user_ids:
                assignment = EmailConnectionUserAssignment.objects.create(
                    connection=connection,
                    user_id=user_id,
                    assigned_by=request.user
                )
                new_assignments.append(assignment)

            serializer = EmailConnectionUserAssignmentSerializer(new_assignments, many=True)
            return Response({
                'message': f'Assigned {len(new_assignments)} users to {connection.email_address}',
                'connection_id': connection.id,
                'assigned_users': serializer.data
            })

    except Exception as e:
        logger.error(f"Failed to manage email connection assignments: {e}")
        return Response({
            'error': f'Failed to manage assignments: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def email_assignment_create(request):
    """
    Create a new email connection assignment.
    Expects: { connection_id: int, user_id: int }
    """
    try:
        from django.contrib.auth import get_user_model
        User = get_user_model()

        connection_id = request.data.get('connection_id')
        user_id = request.data.get('user_id')

        if not connection_id or not user_id:
            return Response({
                'error': 'connection_id and user_id are required'
            }, status=status.HTTP_400_BAD_REQUEST)

        connection = EmailConnection.objects.filter(id=connection_id).first()
        if not connection:
            return Response({
                'error': 'Email connection not found'
            }, status=status.HTTP_404_NOT_FOUND)

        user = User.objects.filter(id=user_id).first()
        if not user:
            return Response({
                'error': 'User not found'
            }, status=status.HTTP_404_NOT_FOUND)

        # Check if assignment already exists
        existing = EmailConnectionUserAssignment.objects.filter(
            connection=connection, user=user
        ).first()
        if existing:
            return Response({
                'error': f'{user.email} is already assigned to {connection.email_address}'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Create the assignment
        assignment = EmailConnectionUserAssignment.objects.create(
            connection=connection,
            user=user,
            assigned_by=request.user
        )

        serializer = EmailConnectionUserAssignmentSerializer(assignment)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"Failed to create email assignment: {e}")
        return Response({
            'error': f'Failed to create assignment: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def email_assignment_delete(request, assignment_id):
    """
    Delete an email connection assignment.
    """
    try:
        assignment = EmailConnectionUserAssignment.objects.filter(id=assignment_id).first()
        if not assignment:
            return Response({
                'error': 'Assignment not found'
            }, status=status.HTTP_404_NOT_FOUND)

        user_email = assignment.user.email
        connection_email = assignment.connection.email_address
        assignment.delete()

        return Response({
            'message': f'Removed {user_email} from {connection_email}'
        })

    except Exception as e:
        logger.error(f"Failed to delete email assignment: {e}")
        return Response({
            'error': f'Failed to delete assignment: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# =============================================================================
# TikTok Integration Views
# =============================================================================

def find_tenant_by_tiktok_shop_id(shop_id):
    """Find the tenant schema that owns a TikTok Shop account by shop_id (O(1) + fallback)."""
    from social_integrations.platform_routing import resolve_tenant
    return resolve_tenant("tiktok", shop_id)


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def tiktok_oauth_start(request):
    """
    Start TikTok OAuth flow.
    Returns the OAuth authorization URL for TikTok login.
    """
    try:
        from .tiktok_utils import get_oauth_url

        # Get current tenant for state parameter
        current_schema = request.tenant.schema_name

        # Build state with tenant info
        state = f"tenant={current_schema}"

        oauth_url = get_oauth_url(state)

        return Response({
            'oauth_url': oauth_url
        })

    except Exception as e:
        logger.error(f"Failed to start TikTok OAuth: {e}")
        return Response({
            'error': f'Failed to start TikTok OAuth: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def tiktok_oauth_callback(request):
    """
    Handle TikTok Shop OAuth callback.
    Exchanges the authorization code for tokens, fetches authorized shops,
    and creates/updates the TikTokShopAccount connection.
    """
    import json
    from .tiktok_utils import exchange_code_for_token, get_authorized_shops

    # Handle POST requests (TikTok sends ping events to validate URL)
    if request.method == 'POST':
        try:
            data = json.loads(request.body) if request.body else {}
            event = data.get('event', '')
            if event == 'tiktok.ping':
                logger.info(f"TikTok ping received: {data}")
                return JsonResponse({'status': 'ok', 'message': 'Ping received'})
            return JsonResponse({'status': 'ok'})
        except Exception as e:
            logger.error(f"TikTok callback POST error: {e}")
            return JsonResponse({'status': 'ok'})

    code = request.GET.get('code')
    state = request.GET.get('state', '')
    error = request.GET.get('error')
    error_description = request.GET.get('error_description', '')

    # Parse tenant from state
    tenant_schema = None
    for param in state.split('&'):
        if param.startswith('tenant='):
            tenant_schema = param.split('=', 1)[1]
            break

    # Build frontend URL from tenant schema
    if tenant_schema:
        frontend_url = f"https://{tenant_schema}.{settings.MAIN_DOMAIN}"
    else:
        frontend_url = f"https://{settings.MAIN_DOMAIN}"

    # Handle OAuth errors
    if error:
        logger.error(f"TikTok Shop OAuth error: {error} - {error_description}")
        return redirect(f'{frontend_url}/social/connections?tiktok_status=error&message={quote_plus(error_description)}')

    if not code:
        return redirect(f'{frontend_url}/social/connections?tiktok_status=error&message=No+authorization+code+received')

    if not tenant_schema:
        return redirect(f'{frontend_url}/social/connections?tiktok_status=error&message=Invalid+state+parameter')

    try:
        # Exchange code for tokens (TikTok Shop uses auth_code param name)
        tokens = exchange_code_for_token(code)

        # Get authorized shops to obtain shop_cipher and shop_id
        shop_cipher = ''
        shop_id = ''
        try:
            shops = get_authorized_shops(tokens['access_token'])
            if shops:
                # Use the first shop
                shop = shops[0]
                shop_cipher = shop.get('cipher', '')
                shop_id = shop.get('id', '')
        except Exception as e:
            logger.warning(f"Could not fetch authorized shops: {e}")

        # Save connection in the correct tenant schema
        with schema_context(tenant_schema):
            account, created = TikTokShopAccount.objects.update_or_create(
                open_id=tokens.get('open_id'),
                defaults={
                    'seller_name': tokens.get('seller_name', ''),
                    'seller_base_region': tokens.get('seller_base_region', ''),
                    'shop_id': shop_id,
                    'shop_cipher': shop_cipher,
                    'user_type': tokens.get('user_type', 0),
                    'token_expires_at': tokens['access_expires_at'],
                    'refresh_token_expires_at': tokens['refresh_expires_at'],
                    'scope': tokens.get('granted_scopes', ''),
                    'is_active': True,
                    'deactivated_at': None,
                    'deactivation_reason': '',
                    'deactivation_error': '',
                }
            )
            # Set encrypted tokens
            account.set_tokens(tokens['access_token'], tokens['refresh_token'])
            account.save(update_fields=['access_token', 'refresh_token'])

            logger.info(f"TikTok Shop account {'created' if created else 'updated'}: {account.seller_name}")

        return redirect(f'{frontend_url}/social/connections?tiktok_status=connected')

    except Exception as e:
        logger.error(f"TikTok Shop OAuth callback error: {e}")
        return redirect(f'{frontend_url}/social/connections?tiktok_status=error&message={quote_plus(str(e))}')


@csrf_exempt
@require_http_methods(["GET", "POST"])
def tiktok_webhook(request):
    """
    Handle TikTok Shop webhook events.

    GET: Webhook verification challenge
    POST: Incoming customer service events (type 13=new conversation, type 14=new message)
    """
    import json
    from .tiktok_utils import verify_webhook_signature, get_tiktok_shop_config

    # GET: Webhook verification
    if request.method == 'GET':
        challenge = request.GET.get('challenge', '')
        return HttpResponse(challenge, content_type='text/plain')

    # POST: Incoming events
    elif request.method == 'POST':
        config = get_tiktok_shop_config()

        # Verify webhook signature (TTSPC uses Authorization header)
        signature = request.headers.get('Authorization', '')
        payload_str = request.body.decode('utf-8') if request.body else ''
        if signature and not verify_webhook_signature(
            config['app_key'], config['app_secret'], payload_str, signature
        ):
            logger.warning("TikTok Shop webhook signature verification failed")
            return JsonResponse({'error': 'Invalid signature'}, status=403)

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            logger.error("TikTok Shop webhook: Invalid JSON payload")
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        # TikTok Shop webhook format: {type, shop_id, timestamp, data: {...}}
        event_type = data.get('type')
        shop_id = str(data.get('shop_id', ''))

        if event_type == 14:
            # New message
            process_tiktok_message_event(data, shop_id)
        elif event_type == 13:
            # New conversation
            logger.info(f"TikTok Shop new conversation event for shop {shop_id}")
        else:
            logger.debug(f"Unhandled TikTok Shop event type: {event_type}")

        return JsonResponse({'status': 'received'})


def process_tiktok_message_event(webhook_data, shop_id):
    """
    Process incoming TikTok Shop message event (type 14).

    Webhook data format:
    {type: 14, shop_id, timestamp, tts_notification_id, data: {
        message_id, index, conversation_id, type, content (JSON string),
        create_time, is_visible, sender: {im_user_id, role}
    }}
    """
    if not shop_id:
        logger.warning("TikTok Shop message event missing shop_id")
        return

    # Find tenant by shop_id
    tenant_schema = find_tenant_by_tiktok_shop_id(shop_id)
    if not tenant_schema:
        logger.warning(f"TikTok Shop message for unknown shop: {shop_id}")
        return

    with schema_context(tenant_schema):
        try:
            account = TikTokShopAccount.objects.get(shop_id=shop_id, is_active=True)
        except TikTokShopAccount.DoesNotExist:
            logger.error(f"TikTok Shop account not found for shop_id: {shop_id}")
            return

        msg_data = webhook_data.get('data', {})
        message_id = msg_data.get('message_id')

        if not message_id:
            logger.warning("TikTok Shop message event missing message_id")
            return

        # Check for duplicate
        if TikTokMessage.objects.filter(message_id=message_id).exists():
            return

        # Extract sender info
        sender = msg_data.get('sender', {})
        sender_role = sender.get('role', '')
        sender_im_user_id = sender.get('im_user_id', '')

        # Parse content (JSON string)
        import json
        content_str = msg_data.get('content', '{}')
        try:
            content = json.loads(content_str) if isinstance(content_str, str) else content_str
        except (json.JSONDecodeError, TypeError):
            content = {}

        message_text = content.get('content', '') or content.get('text', '')
        media_url = content.get('url', '') or content.get('media_url', '')

        # Map sender role to is_from_creator
        is_from_creator = sender_role in ('CUSTOMER_SERVICE', 'SHOP')

        # Parse timestamp
        create_time = msg_data.get('create_time', webhook_data.get('timestamp', 0))
        try:
            ts = int(create_time)
            if ts > 10000000000:
                ts = ts / 1000
            timestamp = datetime.fromtimestamp(ts)
        except (ValueError, TypeError, OSError):
            timestamp = timezone.now()

        conversation_id = msg_data.get('conversation_id', '')

        # Create message record
        msg_obj = TikTokMessage.objects.create(
            shop_account=account,
            message_id=message_id,
            conversation_id=conversation_id,
            index=msg_data.get('index', ''),
            sender_id=sender_im_user_id,
            sender_role=sender_role,
            sender_im_user_id=sender_im_user_id,
            buyer_user_id=msg_data.get('buyer_user_id', ''),
            message_type=msg_data.get('type', 'TEXT'),
            message_text=message_text,
            media_url=media_url or None,
            attachments=content.get('attachments', []),
            timestamp=timestamp,
            is_from_creator=is_from_creator,
        )

        # Send WebSocket notification
        ws_message_data = {
            'id': msg_obj.id,
            'message_id': msg_obj.message_id,
            'platform': 'tiktok',
            'conversation_id': msg_obj.conversation_id,
            'sender_id': msg_obj.sender_id,
            'sender_name': msg_obj.sender_role,
            'message_text': msg_obj.message_text,
            'message_type': msg_obj.message_type,
            'media_url': msg_obj.media_url,
            'attachments': msg_obj.attachments,
            'timestamp': msg_obj.timestamp.isoformat(),
            'is_from_creator': msg_obj.is_from_creator,
            'account_id': account.open_id,
            'chat_id': f'tt_{account.shop_id}_{msg_obj.conversation_id}',
        }
        assigned_user_id = get_assignment_for_conversation(
            platform='tiktok',
            conversation_id=msg_obj.conversation_id,
            account_id=account.open_id
        )
        send_websocket_notification(tenant_schema, ws_message_data, msg_obj.conversation_id, assigned_user_id)

        logger.info(f"TikTok Shop message saved: {message_id}")


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def tiktok_status(request):
    """
    Get TikTok Shop connection status.
    Returns whether connected, account details, and token expiration info.
    """
    try:
        account = TikTokShopAccount.objects.filter(is_active=True).first()

        if not account:
            return Response({
                'connected': False,
                'account': None,
                'token_expires_at': None,
                'is_token_expired': True
            })

        is_expired = account.token_expires_at < timezone.now()

        return Response({
            'connected': True,
            'account': TikTokShopAccountSerializer(account).data,
            'token_expires_at': account.token_expires_at,
            'is_token_expired': is_expired
        })

    except Exception as e:
        logger.error(f"Failed to get TikTok Shop status: {e}")
        return Response({
            'error': f'Failed to get TikTok Shop status: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def tiktok_disconnect(request):
    """
    Disconnect TikTok Shop account.
    Deactivates the account.
    """
    try:
        account = TikTokShopAccount.objects.filter(is_active=True).first()

        if not account:
            return Response({
                'error': 'No active TikTok Shop connection found'
            }, status=status.HTTP_404_NOT_FOUND)

        # Deactivate account
        account.is_active = False
        account.deactivated_at = timezone.now()
        account.deactivation_reason = 'manual'
        account.save(update_fields=['is_active', 'deactivated_at', 'deactivation_reason'])

        logger.info(f"TikTok Shop account disconnected: {account.seller_name}")

        return Response({
            'status': 'disconnected',
            'message': 'TikTok Shop account has been disconnected'
        })

    except Exception as e:
        logger.error(f"Failed to disconnect TikTok Shop: {e}")
        return Response({
            'error': f'Failed to disconnect TikTok Shop: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanSendSocialMessages])
def tiktok_send_message(request):
    """
    Send a TikTok Shop message via Customer Service API.
    """
    from .tiktok_utils import send_message, ensure_valid_token
    import json as json_module

    serializer = TikTokSendMessageSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    conversation_id = data['conversation_id']
    message_text = data['message']
    message_type = data.get('message_type', 'TEXT')

    try:
        account = TikTokShopAccount.objects.filter(is_active=True).first()
        if not account:
            return Response({
                'error': 'No active TikTok Shop connection found'
            }, status=status.HTTP_404_NOT_FOUND)

        if not account.shop_cipher:
            return Response({
                'error': 'Shop cipher not available. Please reconnect your account.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Ensure we have a valid token
        access_token = ensure_valid_token(account)
        if not access_token:
            return Response({
                'error': 'TikTok Shop access token expired. Please reconnect your account.'
            }, status=status.HTTP_401_UNAUTHORIZED)

        # Build content JSON string
        if message_type == 'TEXT':
            content = json_module.dumps({"content": message_text})
        else:
            content = json_module.dumps({"content": message_text, "url": data.get('media_url', '')})

        # Send message via TikTok Shop API
        result = send_message(
            access_token=access_token,
            shop_cipher=account.shop_cipher,
            conversation_id=conversation_id,
            msg_type=message_type,
            content=content
        )

        # Create message record
        message_id = result.get('message_id', f'sent_{timezone.now().timestamp()}')
        msg_obj = TikTokMessage.objects.create(
            shop_account=account,
            message_id=message_id,
            conversation_id=conversation_id,
            sender_id=account.open_id,
            sender_role='CUSTOMER_SERVICE',
            message_type=message_type,
            message_text=message_text,
            media_url=data.get('media_url'),
            timestamp=timezone.now(),
            is_from_creator=True,
        )

        return Response({
            'status': 'sent',
            'message': TikTokMessageSerializer(msg_obj).data
        })

    except Exception as e:
        logger.error(f"Failed to send TikTok Shop message: {e}")
        return Response({
            'error': f'Failed to send message: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class TikTokMessageViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing TikTok Shop messages"""
    serializer_class = TikTokMessageSerializer
    permission_classes = [IsAuthenticated, CanViewSocialMessages]

    def get_queryset(self):
        queryset = TikTokMessage.objects.filter(
            is_deleted=False
        ).select_related('shop_account').order_by('-timestamp')

        # Filter by conversation_id for conversation view
        conversation_id = self.request.query_params.get('conversation_id')
        if conversation_id:
            queryset = queryset.filter(conversation_id=conversation_id)

        # Filter by account
        account_id = self.request.query_params.get('account_id')
        if account_id:
            queryset = queryset.filter(shop_account__open_id=account_id)

        # Search in message text
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(message_text__icontains=search)

        return queryset

    @action(detail=False, methods=['get'])
    def conversations(self, request):
        """
        Get TikTok Shop conversations (grouped by conversation_id).
        Returns the latest message from each unique conversation.
        """
        conversations = TikTokMessage.objects.filter(
            is_deleted=False
        ).values('conversation_id').annotate(
            latest_timestamp=Max('timestamp'),
            message_count=Count('id'),
            unread_count=Count('id', filter=Q(is_read_by_staff=False, is_from_creator=False))
        ).order_by('-latest_timestamp')

        # Limit to 50 conversations and batch-fetch latest messages
        conversations_page = list(conversations[:50])
        conv_ids = [c['conversation_id'] for c in conversations_page]

        # Batch query: fetch all messages for these conversations, pick latest per conv
        all_msgs = TikTokMessage.objects.filter(
            conversation_id__in=conv_ids,
            is_deleted=False,
        ).select_related('shop_account').order_by('conversation_id', '-timestamp')

        latest_by_conv = {}
        for msg in all_msgs:
            if msg.conversation_id not in latest_by_conv:
                latest_by_conv[msg.conversation_id] = msg

        result = []
        for conv in conversations_page:
            latest_msg = latest_by_conv.get(conv['conversation_id'])
            if latest_msg:
                msg_data = TikTokMessageSerializer(latest_msg).data
                msg_data['message_count'] = conv['message_count']
                msg_data['unread_count'] = conv['unread_count']
                result.append(msg_data)

        return Response(result)


# =============================================================================
# Email Signature Views
# =============================================================================

@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated, CanManageSocialSettings])
def email_signature_view(request):
    """
    GET: Retrieve the email signature settings (singleton per tenant)
    PATCH: Update the email signature settings
    """
    # Get or create the singleton signature
    signature, created = EmailSignature.objects.get_or_create(
        defaults={'created_by': request.user}
    )

    if request.method == 'GET':
        serializer = EmailSignatureSerializer(signature)
        return Response(serializer.data)

    elif request.method == 'PATCH':
        serializer = EmailSignatureSerializer(signature, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# =============================================================================
# Quick Reply Views
# =============================================================================

class QuickReplyViewSet(viewsets.ModelViewSet):
    """ViewSet for managing quick reply templates.

    Quick replies are a SHARED team library (get_queryset returns all rows;
    created_by is audit/display only). Any user with social access — staff, the
    social_integrations feature, or an explicit social permission toggle — can
    create/edit/delete them, so agents aren't blocked by a group whose
    feature-key set happens to omit social_integrations.
    """
    serializer_class = QuickReplySerializer
    permission_classes = [IsAuthenticated, CanAccessSocial]
    # Quick replies are a small picker library — return the whole set in one
    # response. With the global 20/page pagination the composer's selector only
    # ever fetched page 1, so newly created replies that sort past the first 20
    # (e.g. Georgian titles, which collate after Latin) were invisible.
    pagination_class = None

    def get_queryset(self):
        queryset = QuickReply.objects.select_related('created_by').order_by('position', '-use_count', 'title')

        # Filter by platform
        platform = self.request.query_params.get('platform')
        if platform:
            # Match 'all' or the specific platform
            queryset = queryset.filter(
                Q(platforms__contains=['all']) | Q(platforms__contains=[platform])
            )

        # Filter by category
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category=category)

        # Search in title or message
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) | Q(message__icontains=search)
            )

        # Filter by shortcut
        shortcut = self.request.query_params.get('shortcut')
        if shortcut:
            queryset = queryset.filter(shortcut=shortcut)

        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=['post'])
    def use(self, request, pk=None):
        """
        Use a quick reply - replaces variables and increments use count.
        Returns the processed message with variables replaced.
        """
        quick_reply = self.get_object()

        # Get variable values from request
        use_serializer = QuickReplyUseSerializer(data=request.data)
        use_serializer.is_valid(raise_exception=True)
        variables = use_serializer.validated_data

        # Add current date/time variables
        variables['current_date'] = datetime.now().strftime('%Y-%m-%d')
        variables['current_time'] = datetime.now().strftime('%H:%M')

        # Process the message - replace variables
        processed_message = quick_reply.message
        for var_name, var_value in variables.items():
            if var_value:
                processed_message = processed_message.replace(f'{{{{{var_name}}}}}', str(var_value))

        # Increment use count
        quick_reply.use_count = F('use_count') + 1
        quick_reply.save(update_fields=['use_count'])
        quick_reply.refresh_from_db()

        return Response({
            'original_message': quick_reply.message,
            'processed_message': processed_message,
            'use_count': quick_reply.use_count
        })

    @action(detail=False, methods=['get'])
    def variables(self, request):
        """Get the list of available variables for quick replies"""
        variables = QuickReply.get_available_variables()
        serializer = QuickReplyVariablesSerializer(variables, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def categories(self, request):
        """Get the list of distinct categories"""
        categories = QuickReply.objects.exclude(
            category=''
        ).values_list('category', flat=True).distinct()
        return Response(list(categories))

    @action(detail=False, methods=['post'])
    def reorder(self, request):
        """
        Reorder quick replies by updating their position values.
        Expects a list of {id: position} pairs.
        """
        items = request.data.get('items', [])
        if not items:
            return Response(
                {'error': 'No items provided'},
                status=status.HTTP_400_BAD_REQUEST
            )

        for item in items:
            quick_reply_id = item.get('id')
            position = item.get('position')
            if quick_reply_id is not None and position is not None:
                QuickReply.objects.filter(id=quick_reply_id).update(position=position)

        return Response({'status': 'reordered'})


# =============================================================================
# Social Client ViewSets
# =============================================================================

class SocialClientViewSet(viewsets.ModelViewSet):
    """ViewSet for managing social clients"""
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'list':
            return SocialClientListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return SocialClientCreateSerializer
        return SocialClientSerializer

    def get_queryset(self):
        queryset = SocialClient.objects.select_related(
            'created_by',
        ).prefetch_related(
            'social_accounts',
            'custom_field_values',
            'custom_field_values__field',
        ).annotate(
            _booking_count=Count('bookings', distinct=True),
            _booking_completed=Count(
                'bookings',
                filter=Q(bookings__status='completed'),
                distinct=True,
            ),
            _booking_upcoming=Count(
                'bookings',
                filter=Q(
                    bookings__date__gte=timezone.now().date(),
                    bookings__status__in=['pending', 'confirmed'],
                ),
                distinct=True,
            ),
        ).all()

        # Search filter
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) |
                Q(email__icontains=search) |
                Q(phone__icontains=search) |
                Q(first_name__icontains=search) |
                Q(last_name__icontains=search)
            )

        # Platform filter
        platform = self.request.query_params.get('platform')
        if platform:
            queryset = queryset.filter(social_accounts__platform=platform).distinct()

        # Booking enabled filter
        is_booking_enabled = self.request.query_params.get('is_booking_enabled')
        if is_booking_enabled is not None:
            is_enabled = is_booking_enabled.lower() in ('true', '1', 'yes')
            queryset = queryset.filter(is_booking_enabled=is_enabled)

        # Client type filter (convenience filter)
        client_type = self.request.query_params.get('client_type')
        if client_type == 'booking':
            queryset = queryset.filter(is_booking_enabled=True)
        elif client_type == 'social':
            queryset = queryset.filter(is_booking_enabled=False)

        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=['get'])
    def by_account(self, request):
        """
        Get client by social account details.
        Query params: platform, platform_id, account_connection_id

        For widget chats, the stable identifier is ``visitor_id`` (persisted
        in visitor localStorage). If no SocialAccount is linked yet we fall
        back to the most recent WidgetSession's visitor_name/email so the
        frontend can prefill the "new client" form without the agent
        retyping.
        """
        platform = request.query_params.get('platform')
        platform_id = request.query_params.get('platform_id')
        account_connection_id = request.query_params.get('account_connection_id')

        if not all([platform, platform_id, account_connection_id]):
            return Response(
                {'error': 'platform, platform_id, and account_connection_id are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            social_account = SocialAccount.objects.select_related('client').get(
                platform=platform,
                platform_id=platform_id,
                account_connection_id=account_connection_id
            )
            return Response({
                'found': True,
                'client': SocialClientSerializer(social_account.client).data,
                'social_account': SocialAccountSerializer(social_account).data
            })
        except SocialAccount.DoesNotExist:
            prefill = None
            if platform == 'widget':
                try:
                    conn_id = int(account_connection_id)
                except (TypeError, ValueError):
                    conn_id = None
                if conn_id is not None:
                    session = (
                        WidgetSession.objects
                        .filter(connection_id=conn_id, visitor_id=platform_id)
                        .order_by('-last_seen_at')
                        .first()
                    )
                    if session:
                        prefill = {
                            'name': session.visitor_name or '',
                            'email': session.visitor_email or '',
                            'visitor_id': session.visitor_id,
                            'session_id': session.session_id,
                        }
            return Response({
                'found': False,
                'client': None,
                'social_account': None,
                'prefill': prefill,
            })

    @action(detail=True, methods=['post'])
    def link_account(self, request, pk=None):
        """Link a social account to this client"""
        client = self.get_object()
        serializer = SocialAccountLinkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        # Check if this account is already linked to another client
        existing = SocialAccount.objects.filter(
            platform=data['platform'],
            platform_id=data['platform_id'],
            account_connection_id=data['account_connection_id']
        ).exclude(client=client).first()

        if existing:
            return Response(
                {'error': f'This account is already linked to client: {existing.client.name}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create or update the social account
        social_account, created = SocialAccount.objects.update_or_create(
            platform=data['platform'],
            platform_id=data['platform_id'],
            account_connection_id=data['account_connection_id'],
            defaults={
                'client': client,
                'display_name': data.get('display_name', ''),
                'username': data.get('username', ''),
                'profile_pic_url': data.get('profile_pic_url'),
                'is_auto_created': False,
            }
        )

        # Update client's profile picture if not set
        if not client.profile_picture and social_account.profile_pic_url:
            client.profile_picture = social_account.profile_pic_url
            client.save(update_fields=['profile_picture'])

        return Response({
            'status': 'linked' if created else 'updated',
            'social_account': SocialAccountSerializer(social_account).data
        })

    @action(detail=True, methods=['post'])
    def unlink_account(self, request, pk=None):
        """Unlink a social account from this client"""
        client = self.get_object()
        serializer = SocialAccountLinkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        try:
            social_account = SocialAccount.objects.get(
                client=client,
                platform=data['platform'],
                platform_id=data['platform_id'],
                account_connection_id=data['account_connection_id']
            )
            social_account.delete()
            return Response({'status': 'unlinked'})
        except SocialAccount.DoesNotExist:
            return Response(
                {'error': 'Social account not found or not linked to this client'},
                status=status.HTTP_404_NOT_FOUND
            )

    @action(detail=True, methods=['get'])
    def booking_history(self, request, pk=None):
        """Get booking history for a client (only for booking-enabled clients)"""
        client = self.get_object()

        if not client.is_booking_enabled:
            return Response({
                'error': 'This client does not have booking enabled',
                'bookings': []
            })

        try:
            # Import here to avoid circular imports
            from booking_management.models import Booking
            from booking_management.serializers import BookingListSerializer

            bookings = Booking.objects.filter(client=client).order_by('-date', '-start_time')

            # Pagination support
            page_size = int(request.query_params.get('page_size', 10))
            page = int(request.query_params.get('page', 1))
            offset = (page - 1) * page_size

            total = bookings.count()
            bookings = bookings[offset:offset + page_size]

            return Response({
                'results': BookingListSerializer(bookings, many=True).data,
                'count': total,
                'page': page,
                'page_size': page_size,
            })
        except Exception as e:
            return Response({
                'error': f'Could not fetch booking history: {str(e)}',
                'bookings': []
            })

    @action(detail=True, methods=['get'])
    def booking_stats(self, request, pk=None):
        """Get booking statistics for a client"""
        client = self.get_object()

        if not client.is_booking_enabled:
            return Response({
                'error': 'This client does not have booking enabled',
                'stats': None
            })

        try:
            from booking_management.models import Booking
            from django.db.models import Sum
            from django.utils import timezone

            today = timezone.now().date()

            total = client.bookings.count()
            completed = client.bookings.filter(status='completed').count()
            upcoming = client.bookings.filter(date__gte=today, status__in=['pending', 'confirmed']).count()
            cancelled = client.bookings.filter(status='cancelled').count()
            total_spent = client.bookings.filter(
                payment_status__in=['fully_paid', 'deposit_paid']
            ).aggregate(total=Sum('paid_amount'))['total'] or 0

            return Response({
                'total': total,
                'completed': completed,
                'upcoming': upcoming,
                'cancelled': cancelled,
                'total_spent': float(total_spent),
            })
        except Exception as e:
            return Response({
                'error': f'Could not fetch booking stats: {str(e)}',
                'stats': None
            })


class SocialClientCustomFieldViewSet(viewsets.ModelViewSet):
    """ViewSet for managing custom field definitions"""
    serializer_class = SocialClientCustomFieldSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = SocialClientCustomField.objects.select_related('created_by').all()

        # Filter by active status
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')

        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=['post'])
    def reorder(self, request):
        """
        Reorder custom fields by updating their position values.
        Expects a list of {id: position} pairs.
        """
        items = request.data.get('items', [])
        if not items:
            return Response(
                {'error': 'No items provided'},
                status=status.HTTP_400_BAD_REQUEST
            )

        for item in items:
            field_id = item.get('id')
            position = item.get('position')
            if field_id is not None and position is not None:
                SocialClientCustomField.objects.filter(id=field_id).update(position=position)

        return Response({'status': 'reordered'})


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def mark_all_conversations_read(request):
    """
    Mark all unread messages across all conversations as read by staff.

    Request body (optional):
    {
        "platform": "facebook" | "instagram" | "whatsapp" | "email" | "all" | "facebook,instagram,whatsapp"  (default: "all")
    }

    Supports comma-separated platform values (e.g., "facebook,instagram,whatsapp" to exclude email).

    Returns count of messages marked as read.
    """
    logger = logging.getLogger(__name__)

    try:
        platform_param = request.data.get('platform', 'all').lower()
        now = timezone.now()
        total_updated = 0

        # Support comma-separated platforms
        platforms = [p.strip() for p in platform_param.split(',')]
        include_all = 'all' in platforms

        # Mark Facebook messages as read
        if include_all or 'facebook' in platforms:
            count = FacebookMessage.objects.filter(
                is_from_page=False,
                is_read_by_staff=False
            ).update(
                is_read_by_staff=True,
                read_by_staff_at=now
            )
            total_updated += count
            logger.info(f"Marked {count} Facebook messages as read")

        # Mark Instagram messages as read
        if include_all or 'instagram' in platforms:
            count = InstagramMessage.objects.filter(
                is_from_business=False,
                is_read_by_staff=False
            ).update(
                is_read_by_staff=True,
                read_by_staff_at=now
            )
            total_updated += count
            logger.info(f"Marked {count} Instagram messages as read")

        # Mark WhatsApp messages as read
        if include_all or 'whatsapp' in platforms:
            count = WhatsAppMessage.objects.filter(
                is_from_business=False,
                is_read_by_staff=False
            ).update(
                is_read_by_staff=True,
                read_by_staff_at=now
            )
            total_updated += count
            logger.info(f"Marked {count} WhatsApp messages as read")

        # Mark Email messages as read
        if include_all or 'email' in platforms:
            count = EmailMessage.objects.filter(
                is_from_business=False,
                is_read_by_staff=False
            ).update(
                is_read_by_staff=True,
                read_by_staff_at=now
            )
            total_updated += count
            logger.info(f"Marked {count} Email messages as read")

        # Mark Widget messages as read
        if include_all or 'widget' in platforms:
            count = WidgetMessage.objects.filter(
                is_from_visitor=True,
                is_read_by_staff=False,
            ).update(
                is_read_by_staff=True,
                read_by_staff_at=now,
            )
            total_updated += count
            logger.info(f"Marked {count} Widget messages as read")

        # Bulk read-state broadcast: beta clients treat conversation_id=None
        # as "clear all unread for the listed platform(s)" so the sidebar
        # badges drop instantly without a list refetch. Emitted once per
        # touched platform so handlers can keep platform-scoped logic clean.
        for plat in [p for p in platforms if p != 'all'] or (
            ['facebook', 'instagram', 'whatsapp', 'email', 'widget'] if include_all else []
        ):
            _broadcast_read_state(
                request,
                platform=plat,
                conversation_id=None,  # null => bulk clear
                account_id=None,
                unread_count=0,
                last_read_at=now,
            )

        return Response({
            'success': True,
            'messages_marked_read': total_updated,
            'platform': platform_param
        })

    except Exception as e:
        logger.error(f"Error marking all conversations as read: {e}")
        return Response({
            'error': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def archive_conversation(request):
    """
    Archive one or more conversations. Archived conversations are hidden
    from the main list and shown in the history/archive tab.

    Request body:
    {
        "conversations": [
            {
                "platform": "facebook" | "instagram" | "whatsapp" | "email",
                "conversation_id": "sender_id or from_number or thread_id",
                "account_id": "page_id or account_id or waba_id or connection_id"
            }
        ]
    }

    Returns count of conversations archived.
    """
    logger = logging.getLogger(__name__)

    try:
        conversations = request.data.get('conversations', [])

        if not conversations:
            return Response({
                'error': 'No conversations provided'
            }, status=status.HTTP_400_BAD_REQUEST)

        archived_count = 0
        errors = []

        for conv in conversations:
            platform = conv.get('platform')
            conversation_id = conv.get('conversation_id')
            account_id = conv.get('account_id')

            if not all([platform, conversation_id, account_id]):
                errors.append({
                    'conversation': conv,
                    'error': 'Missing required fields (platform, conversation_id, account_id)'
                })
                continue

            # Create or update the archive record
            archive, created = ConversationArchive.objects.get_or_create(
                platform=platform,
                conversation_id=conversation_id,
                account_id=account_id,
                defaults={
                    'archived_by': request.user
                }
            )

            # Mark all messages in this conversation as read
            now = timezone.now()
            if platform == 'facebook':
                FacebookMessage.objects.filter(
                    sender_id=conversation_id,
                    is_from_page=False,
                    is_read_by_staff=False
                ).update(is_read_by_staff=True, read_by_staff_at=now)
            elif platform == 'instagram':
                InstagramMessage.objects.filter(
                    sender_id=conversation_id,
                    is_from_business=False,
                    is_read_by_staff=False
                ).update(is_read_by_staff=True, read_by_staff_at=now)
            elif platform == 'whatsapp':
                WhatsAppMessage.objects.filter(
                    from_number=conversation_id,
                    is_from_business=False,
                    is_read_by_staff=False
                ).update(is_read_by_staff=True, read_by_staff_at=now)
            elif platform == 'email':
                EmailMessage.objects.filter(
                    thread_id=conversation_id,
                    is_from_business=False,
                    is_read_by_staff=False
                ).update(is_read_by_staff=True, read_by_staff_at=now)
            elif platform == 'widget':
                WidgetMessage.objects.filter(
                    session__session_id=conversation_id,
                    is_from_visitor=True,
                    is_read_by_staff=False,
                ).update(is_read_by_staff=True, read_by_staff_at=now)
                # Archiving a widget chat is also the agent saying "I'm done
                # with this conversation". Close the underlying session and
                # tell the visitor's iframe so it surfaces the post-chat
                # review form. Without this, the visitor could keep typing
                # into a thread the agent already moved out of their inbox.
                try:
                    widget_session = WidgetSession.objects.filter(
                        session_id=conversation_id
                    ).first()
                    if widget_session and widget_session.ended_at is None:
                        widget_session.ended_at = now
                        widget_session.ended_by = 'agent'
                        widget_session.save(update_fields=['ended_at', 'ended_by'])
                        # Broadcast so the iframe flips to review immediately.
                        from asgiref.sync import async_to_sync
                        from channels.layers import get_channel_layer
                        channel_layer = get_channel_layer()
                        if channel_layer:
                            tenant_schema = getattr(request.tenant, 'schema_name', None)
                            if tenant_schema:
                                async_to_sync(channel_layer.group_send)(
                                    f'messages_{tenant_schema}',
                                    {
                                        'type': 'session_ended',
                                        'session_id': conversation_id,
                                        'connection_id': widget_session.connection_id,
                                        'conversation_id': f'widget_{widget_session.connection_id}_{conversation_id}',
                                        'ended_by': 'agent',
                                        'ended_at': now.isoformat(),
                                        'message': 'The agent moved this conversation to history.',
                                    },
                                )
                except Exception as e:
                    logger.warning(
                        f"Failed to end widget session on archive: {e}"
                    )

            if created:
                archived_count += 1
                logger.info(f"Archived conversation: {platform}/{account_id}/{conversation_id}")
            else:
                # Already archived, update the timestamp
                archive.archived_at = timezone.now()
                archive.archived_by = request.user
                archive.save()
                logger.info(f"Updated archive timestamp: {platform}/{account_id}/{conversation_id}")

            # Cross-user reactivity: beta clients move the row from active
            # inbox into the Archive tab on this frame, no list refetch.
            _broadcast_archive_change(
                request,
                platform=platform,
                conversation_id=conversation_id,
                account_id=account_id,
                archived=True,
                archived_at=archive.archived_at,
            )

        response_data = {
            'success': True,
            'archived_count': archived_count,
            'message': f"Archived {archived_count} conversation(s)"
        }

        if errors:
            response_data['errors'] = errors

        return Response(response_data)

    except Exception as e:
        logger.error(f"Error archiving conversations: {e}")
        return Response({
            'error': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def unarchive_conversation(request):
    """
    Unarchive (restore) one or more conversations back to the main list.

    Request body:
    {
        "conversations": [
            {
                "platform": "facebook" | "instagram" | "whatsapp" | "email",
                "conversation_id": "sender_id or from_number or thread_id",
                "account_id": "page_id or account_id or waba_id or connection_id"
            }
        ]
    }

    Returns count of conversations unarchived.
    """
    logger = logging.getLogger(__name__)

    try:
        conversations = request.data.get('conversations', [])

        if not conversations:
            return Response({
                'error': 'No conversations provided'
            }, status=status.HTTP_400_BAD_REQUEST)

        unarchived_count = 0
        errors = []

        for conv in conversations:
            platform = conv.get('platform')
            conversation_id = conv.get('conversation_id')
            account_id = conv.get('account_id')

            if not all([platform, conversation_id, account_id]):
                errors.append({
                    'conversation': conv,
                    'error': 'Missing required fields (platform, conversation_id, account_id)'
                })
                continue

            # Delete the archive record
            deleted_count, _ = ConversationArchive.objects.filter(
                platform=platform,
                conversation_id=conversation_id,
                account_id=account_id
            ).delete()

            if deleted_count > 0:
                unarchived_count += 1
                logger.info(f"Unarchived conversation: {platform}/{account_id}/{conversation_id}")
                _broadcast_archive_change(
                    request,
                    platform=platform,
                    conversation_id=conversation_id,
                    account_id=account_id,
                    archived=False,
                    archived_at=None,
                )

        response_data = {
            'success': True,
            'unarchived_count': unarchived_count,
            'message': f"Unarchived {unarchived_count} conversation(s)"
        }

        if errors:
            response_data['errors'] = errors

        return Response(response_data)

    except Exception as e:
        logger.error(f"Error unarchiving conversations: {e}")
        return Response({
            'error': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def archive_all_conversations(request):
    """
    Archive all conversations (move everything to history).
    This will archive all active (non-archived) conversations across all platforms.

    Request body (optional):
    {
        "platform": "facebook" | "instagram" | "whatsapp" | "email" | "all" | "facebook,instagram,whatsapp"  (default: "all")
    }

    Supports comma-separated platform values (e.g., "facebook,instagram,whatsapp" to exclude email).

    Returns count of conversations archived.
    """
    logger = logging.getLogger(__name__)

    try:
        platform_param = request.data.get('platform', 'all').lower()
        archived_count = 0

        # Support comma-separated platforms
        platforms = [p.strip() for p in platform_param.split(',')]
        include_all = 'all' in platforms

        # Get all existing archived conversation keys for quick lookup
        existing_archives = set(
            ConversationArchive.objects.values_list('platform', 'conversation_id', 'account_id')
        )

        # Get all active assignments to skip (don't archive assigned chats)
        active_assignments = set(
            ChatAssignment.objects.filter(status='in_session')
            .values_list('platform', 'conversation_id', 'account_id')
        )

        conversations_to_archive = []

        # Gather Facebook conversations
        if include_all or 'facebook' in platforms:
            fb_pages = FacebookPageConnection.objects.filter(is_active=True)
            for page in fb_pages:
                # Get unique sender_ids for this page
                sender_ids = FacebookMessage.objects.filter(
                    page_connection=page,
                    is_deleted=False
                ).values_list('sender_id', flat=True).distinct()

                for sender_id in sender_ids:
                    key = ('facebook', sender_id, page.page_id)
                    if key not in existing_archives and key not in active_assignments:
                        conversations_to_archive.append({
                            'platform': 'facebook',
                            'conversation_id': sender_id,
                            'account_id': page.page_id
                        })

        # Gather Instagram conversations
        if include_all or 'instagram' in platforms:
            ig_accounts = InstagramAccountConnection.objects.filter(is_active=True)
            for account in ig_accounts:
                sender_ids = InstagramMessage.objects.filter(
                    account_connection=account,
                    is_deleted=False
                ).values_list('sender_id', flat=True).distinct()

                for sender_id in sender_ids:
                    key = ('instagram', sender_id, account.instagram_account_id)
                    if key not in existing_archives and key not in active_assignments:
                        conversations_to_archive.append({
                            'platform': 'instagram',
                            'conversation_id': sender_id,
                            'account_id': account.instagram_account_id
                        })

        # Gather WhatsApp conversations
        if include_all or 'whatsapp' in platforms:
            wa_accounts = WhatsAppBusinessAccount.objects.filter(is_active=True)
            for account in wa_accounts:
                from_numbers = WhatsAppMessage.objects.filter(
                    business_account=account,
                    is_deleted=False
                ).values_list('from_number', flat=True).distinct()

                for from_number in from_numbers:
                    # Normalize phone number
                    normalized = from_number.lstrip('+') if from_number else from_number
                    key = ('whatsapp', normalized, account.waba_id)
                    if key not in existing_archives and key not in active_assignments:
                        conversations_to_archive.append({
                            'platform': 'whatsapp',
                            'conversation_id': normalized,
                            'account_id': account.waba_id
                        })

        # Gather Email conversations (only INBOX threads)
        if include_all or 'email' in platforms:
            email_connections = EmailConnection.objects.filter(is_active=True)
            for connection in email_connections:
                thread_ids = EmailMessage.objects.filter(
                    connection=connection,
                    is_deleted=False,
                    folder='INBOX'
                ).values_list('thread_id', flat=True).distinct()

                for thread_id in thread_ids:
                    key = ('email', thread_id, str(connection.id))
                    if key not in existing_archives and key not in active_assignments:
                        conversations_to_archive.append({
                            'platform': 'email',
                            'conversation_id': thread_id,
                            'account_id': str(connection.id)
                        })

        # Bulk create archive records
        archive_objects = [
            ConversationArchive(
                platform=conv['platform'],
                conversation_id=conv['conversation_id'],
                account_id=conv['account_id'],
                archived_by=request.user
            )
            for conv in conversations_to_archive
        ]

        if archive_objects:
            ConversationArchive.objects.bulk_create(archive_objects, ignore_conflicts=True)
            archived_count = len(archive_objects)

        # Mark all messages as read for archived conversations
        now = timezone.now()
        messages_marked_read = 0

        if include_all or 'facebook' in platforms:
            count = FacebookMessage.objects.filter(
                is_from_page=False,
                is_read_by_staff=False
            ).update(is_read_by_staff=True, read_by_staff_at=now)
            messages_marked_read += count

        if include_all or 'instagram' in platforms:
            count = InstagramMessage.objects.filter(
                is_from_business=False,
                is_read_by_staff=False
            ).update(is_read_by_staff=True, read_by_staff_at=now)
            messages_marked_read += count

        if include_all or 'whatsapp' in platforms:
            count = WhatsAppMessage.objects.filter(
                is_from_business=False,
                is_read_by_staff=False
            ).update(is_read_by_staff=True, read_by_staff_at=now)
            messages_marked_read += count

        if include_all or 'email' in platforms:
            count = EmailMessage.objects.filter(
                is_from_business=False,
                is_read_by_staff=False
            ).update(is_read_by_staff=True, read_by_staff_at=now)
            messages_marked_read += count

        logger.info(f"Archived {archived_count} conversations, marked {messages_marked_read} messages as read (platform: {platform_param})")

        # Bulk archive_update broadcast: emit one frame per touched platform
        # with conversation_id=None so beta clients can re-evaluate the entire
        # platform's archived map at once instead of receiving N frames. Same
        # convention as the bulk mark-all-read path above.
        bulk_platforms = (
            [p for p in platforms if p != 'all']
            or (['facebook', 'instagram', 'whatsapp', 'email', 'widget'] if include_all else [])
        )
        for plat in bulk_platforms:
            _broadcast_archive_change(
                request,
                platform=plat,
                conversation_id=None,
                account_id=None,
                archived=True,
                archived_at=now,
            )

        return Response({
            'success': True,
            'archived_count': archived_count,
            'platform': platform_param,
            'message': f"Archived {archived_count} conversation(s)"
        })

    except Exception as e:
        logger.error(f"Error archiving all conversations: {e}")
        return Response({
            'error': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ===========================
# Auto-Posting Endpoints
# ===========================

@api_view(['GET'])
@permission_classes([IsAuthenticated, CanManageSocialConnections])
def facebook_oauth_start_publishing(request):
    """Generate Facebook OAuth URL for publishing permissions (opt-in).
    Same flow as facebook_oauth_start but requests additional scopes:
    pages_manage_posts, instagram_content_publish
    """
    try:
        fb_app_id = getattr(settings, 'SOCIAL_INTEGRATIONS', {}).get('FACEBOOK_APP_ID')
        if not fb_app_id:
            return Response({'error': 'Facebook App ID not configured'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        redirect_uri = f'https://{settings.API_DOMAIN}/api/social/facebook/oauth/callback/'

        from urllib.parse import quote
        tenant_obj = getattr(request, "tenant", None)
        if tenant_obj and hasattr(tenant_obj, 'schema_name'):
            tenant_name = tenant_obj.schema_name
        elif tenant_obj and hasattr(tenant_obj, 'name'):
            tenant_name = tenant_obj.name
        else:
            tenant_name = "amanati"

        # Include publishing flag in state so callback knows to set has_publishing_permission
        state_raw = f'tenant={tenant_name}&publishing=true'
        state = quote(state_raw)

        # Request all existing scopes PLUS publishing scopes
        scopes = (
            'business_management,pages_show_list,pages_manage_metadata,pages_messaging,'
            'pages_read_engagement,instagram_basic,instagram_manage_messages,public_profile,email,'
            'pages_manage_posts,instagram_content_publish'
        )

        oauth_url = (
            f"https://www.facebook.com/v23.0/dialog/oauth?"
            f"client_id={fb_app_id}&"
            f"redirect_uri={quote(redirect_uri)}&"
            f"scope={scopes}&"
            f"state={state}&"
            f"response_type=code&"
            f"auth_type=rerequest&"
            f"display=popup"
        )

        return Response({
            'oauth_url': oauth_url,
            'redirect_uri': redirect_uri,
            'instructions': 'Visit the OAuth URL to grant publishing permissions for your Facebook pages'
        })
    except Exception as e:
        return Response({'error': f'Failed to generate OAuth URL: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def auto_post_publishing_status(request):
    """Check which connections have publishing permissions"""
    facebook_pages = FacebookPageConnection.objects.filter(is_active=True).values(
        'id', 'page_id', 'page_name', 'has_publishing_permission'
    )
    instagram_accounts = InstagramAccountConnection.objects.filter(is_active=True).values(
        'id', 'instagram_account_id', 'username'
    )
    # Instagram publishing permission is tied to its facebook_page connection
    instagram_data = []
    for ig in InstagramAccountConnection.objects.filter(is_active=True).select_related('facebook_page'):
        instagram_data.append({
            'id': ig.id,
            'instagram_account_id': ig.instagram_account_id,
            'username': ig.username,
            'has_publishing_permission': ig.facebook_page.has_publishing_permission if ig.facebook_page else False,
        })

    return Response({
        'facebook_pages': list(facebook_pages),
        'instagram_accounts': instagram_data,
    })


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated, CanManageSocialSettings])
def auto_post_settings(request):
    """Get or update auto-post settings (singleton per tenant)"""
    settings_obj, _ = AutoPostSettings.objects.get_or_create(pk=1, defaults={})

    if request.method == 'GET':
        serializer = AutoPostSettingsSerializer(settings_obj)
        return Response(serializer.data)

    serializer = AutoPostSettingsSerializer(settings_obj, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def auto_post_list(request):
    """List auto-post contents with optional status filter"""
    queryset = AutoPostContent.objects.all().select_related('featured_product', 'approved_by', 'rejected_by')
    status_filter = request.GET.get('status')
    if status_filter:
        queryset = queryset.filter(status=status_filter)

    page = int(request.GET.get('page', 1))
    page_size = int(request.GET.get('page_size', 20))
    start = (page - 1) * page_size
    end = start + page_size
    total = queryset.count()

    serializer = AutoPostContentSerializer(queryset[start:end], many=True)
    return Response({
        'count': total,
        'next': f'?page={page + 1}&page_size={page_size}' if end < total else None,
        'previous': f'?page={page - 1}&page_size={page_size}' if page > 1 else None,
        'results': serializer.data,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, CanViewSocialMessages])
def auto_post_detail(request, pk):
    """Get auto-post content detail"""
    try:
        post = AutoPostContent.objects.select_related('featured_product', 'approved_by', 'rejected_by').get(pk=pk)
    except AutoPostContent.DoesNotExist:
        return Response({'error': 'Post not found'}, status=status.HTTP_404_NOT_FOUND)
    serializer = AutoPostContentSerializer(post)
    return Response(serializer.data)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialSettings])
def auto_post_approve(request, pk):
    """Approve a draft post"""
    try:
        post = AutoPostContent.objects.get(pk=pk)
    except AutoPostContent.DoesNotExist:
        return Response({'error': 'Post not found'}, status=status.HTTP_404_NOT_FOUND)

    if post.status != 'draft':
        return Response({'error': f'Cannot approve post with status: {post.status}'}, status=status.HTTP_400_BAD_REQUEST)

    post.status = 'approved'
    post.approved_by = request.user
    post.approved_at = timezone.now()
    post.save()
    serializer = AutoPostContentSerializer(post)
    return Response(serializer.data)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialSettings])
def auto_post_reject(request, pk):
    """Reject a draft post"""
    try:
        post = AutoPostContent.objects.get(pk=pk)
    except AutoPostContent.DoesNotExist:
        return Response({'error': 'Post not found'}, status=status.HTTP_404_NOT_FOUND)

    if post.status != 'draft':
        return Response({'error': f'Cannot reject post with status: {post.status}'}, status=status.HTTP_400_BAD_REQUEST)

    post.status = 'rejected'
    post.rejected_by = request.user
    post.rejected_at = timezone.now()
    post.rejection_reason = request.data.get('reason', '')
    post.save()
    serializer = AutoPostContentSerializer(post)
    return Response(serializer.data)


@api_view(['PUT'])
@permission_classes([IsAuthenticated, CanManageSocialSettings])
def auto_post_edit(request, pk):
    """Edit a draft post's text"""
    try:
        post = AutoPostContent.objects.get(pk=pk)
    except AutoPostContent.DoesNotExist:
        return Response({'error': 'Post not found'}, status=status.HTTP_404_NOT_FOUND)

    if post.status not in ('draft', 'rejected'):
        return Response({'error': f'Cannot edit post with status: {post.status}'}, status=status.HTTP_400_BAD_REQUEST)

    allowed_fields = ['facebook_text', 'instagram_text', 'image_url', 'target_facebook', 'target_instagram']
    for field in allowed_fields:
        if field in request.data:
            setattr(post, field, request.data[field])

    # Reset to draft if it was rejected and now edited
    if post.status == 'rejected':
        post.status = 'draft'
        post.rejected_by = None
        post.rejected_at = None
        post.rejection_reason = ''

    post.save()
    serializer = AutoPostContentSerializer(post)
    return Response(serializer.data)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialSettings])
def auto_post_generate(request):
    """Manually trigger post generation"""
    from .services.ai_content_service import AIContentService

    try:
        auto_settings, _ = AutoPostSettings.objects.get_or_create(pk=1, defaults={})

        service = AIContentService()
        result = service.generate_post_for_tenant(auto_settings)

        serializer = AutoPostContentSerializer(result)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    except Exception as e:
        logger.error(f"Error generating auto-post: {e}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CanManageSocialSettings])
def auto_post_publish(request, pk):
    """Manually publish an approved post"""
    try:
        post = AutoPostContent.objects.get(pk=pk)
    except AutoPostContent.DoesNotExist:
        return Response({'error': 'Post not found'}, status=status.HTTP_404_NOT_FOUND)

    if post.status not in ('approved', 'failed'):
        return Response({'error': f'Cannot publish post with status: {post.status}'}, status=status.HTTP_400_BAD_REQUEST)

    from .services.facebook_publisher import FacebookPublisher
    from .services.instagram_publisher import InstagramPublisher

    errors = []

    if post.target_facebook:
        try:
            fb_publisher = FacebookPublisher()
            fb_post_id = fb_publisher.publish(post)
            post.facebook_post_id = fb_post_id
        except Exception as e:
            errors.append(f"Facebook: {str(e)}")
            logger.error(f"Facebook publish error for post {pk}: {e}")

    if post.target_instagram:
        try:
            ig_publisher = InstagramPublisher()
            ig_media_id = ig_publisher.publish(post)
            post.instagram_media_id = ig_media_id
        except Exception as e:
            errors.append(f"Instagram: {str(e)}")
            logger.error(f"Instagram publish error for post {pk}: {e}")

    if errors:
        post.status = 'failed'
        post.error_message = '; '.join(errors)
    else:
        post.status = 'published'
        post.published_at = timezone.now()

    post.save()
    serializer = AutoPostContentSerializer(post)
    return Response(serializer.data)