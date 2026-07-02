import logging
import socket

from rest_framework import serializers as drf_serializers, viewsets, permissions, status, filters
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from datetime import timedelta
from drf_spectacular.utils import extend_schema, OpenApiParameter, inline_serializer
from drf_spectacular.types import OpenApiTypes
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from .models import CallLog, Client, SipConfiguration, CallEvent, CallRecording, UserPhoneAssignment, PbxSettings, CallRating
from .serializers import (
    CallLogSerializer, ClientSerializer, SipConfigurationSerializer,
    SipConfigurationListSerializer, SipConfigurationDetailSerializer,
    CallLogCreateSerializer, CallInitiateSerializer, CallStatusUpdateSerializer,
    CallLogDetailSerializer, CallEventSerializer, CallRecordingSerializer,
    UserPhoneAssignmentSerializer, UserPhoneAssignmentDetailSerializer,
    PbxSettingsSerializer, ConsultationInitiateSerializer, MergeConferenceSerializer,
    CallRatingSerializer,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Asterisk AMI helpers (raw TCP, no external library)
#
# Phase 2 (BYO Asterisk): AMI credentials come from the current tenant's
# PbxServer row, not module-level constants. Each helper now takes host +
# username + password (+ optional port) so callers can resolve these from
# their tenant's PbxServer and stay isolated from every other tenant's AMI.
# ---------------------------------------------------------------------------

def _ami_send_action(sock, action_lines):
    """Send a single AMI action (dict of key/value pairs) and return the raw response."""
    msg = ""
    for key, value in action_lines:
        msg += f"{key}: {value}\r\n"
    msg += "\r\n"
    sock.sendall(msg.encode("utf-8"))
    return _ami_read_response(sock)


def _ami_read_response(sock, timeout=5):
    """Read from the AMI socket until we get a complete response (ends with \\r\\n\\r\\n)."""
    sock.settimeout(timeout)
    data = b""
    while True:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            # AMI responses are terminated by a blank line
            if b"\r\n\r\n" in data:
                break
        except socket.timeout:
            break
    return data.decode("utf-8", errors="replace")


def _ami_connect_and_login(host, username, password, port=5038):
    """Open a TCP connection to AMI, read the banner, and log in.

    Returns the socket on success; raises on failure.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect((host, port))

    # Read the AMI banner (e.g. "Asterisk Call Manager/...")
    _ami_read_response(sock, timeout=3)

    # Login
    resp = _ami_send_action(sock, [
        ("Action", "Login"),
        ("Username", username),
        ("Secret", password),
    ])
    if "Success" not in resp:
        sock.close()
        raise ConnectionError(f"AMI login failed: {resp.strip()}")
    return sock


def _ami_logoff(sock):
    """Send Logoff and close the socket."""
    try:
        _ami_send_action(sock, [("Action", "Logoff")])
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass


def _ami_get_channels(host, username, password, port=5038):
    """Connect to AMI, run CoreShowChannels, and return a list of channel dicts.

    Each dict has at least:
        - channel: full channel name (e.g. PJSIP/geo-provider-endpoint-00000001)
        - context, exten, calleridnum, duration, application, bridgeid, ...
    """
    sock = _ami_connect_and_login(host, username, password, port=port)
    try:
        # CoreShowChannels returns multiple "Event: CoreShowChannel" messages
        # followed by "Event: CoreShowChannelsComplete".
        msg = "Action: CoreShowChannels\r\n\r\n"
        sock.sendall(msg.encode("utf-8"))

        raw = b""
        sock.settimeout(5)
        while True:
            try:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                raw += chunk
                if b"CoreShowChannelsComplete" in raw:
                    break
            except socket.timeout:
                break

        text = raw.decode("utf-8", errors="replace")
        channels = []
        # Split into blocks by double CRLF
        blocks = text.split("\r\n\r\n")
        for block in blocks:
            if "Event: CoreShowChannel\r\n" not in block and "Event: CoreShowChannel\n" not in block:
                continue
            channel_info = {}
            for line in block.strip().splitlines():
                if ": " in line:
                    key, _, value = line.partition(": ")
                    channel_info[key.strip().lower()] = value.strip()
            if channel_info.get("channel"):
                channels.append(channel_info)
        return channels
    finally:
        _ami_logoff(sock)


def _ami_redirect_to_confbridge(host, channel, conference_room, username, password, port=5038):
    """Redirect a single Asterisk channel into a ConfBridge room.

    Uses the ``confbridge-dynamic`` dialplan context.
    """
    sock = _ami_connect_and_login(host, username, password, port=port)
    try:
        resp = _ami_send_action(sock, [
            ("Action", "Redirect"),
            ("Channel", channel),
            ("Context", "confbridge-dynamic"),
            ("Exten", conference_room),
            ("Priority", "1"),
        ])
        if "Success" not in resp:
            raise RuntimeError(f"AMI Redirect failed for {channel}: {resp.strip()}")
        return resp
    finally:
        _ami_logoff(sock)


class SipConfigurationViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing SIP configurations.
    
    Allows tenants to configure their SIP settings for making and receiving calls.
    """
    serializer_class = SipConfigurationSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        # Return configurations for current tenant - tenant schema isolation handles filtering
        if hasattr(self.request, 'tenant'):
            # In tenant schema, all records are for the current tenant
            return SipConfiguration.objects.all()
        else:
            # Fallback for public schema or when tenant is not available
            return SipConfiguration.objects.all()
    
    def get_serializer_class(self):
        if self.action == 'list':
            return SipConfigurationListSerializer
        elif self.action in ['retrieve', 'webrtc_config']:
            return SipConfigurationDetailSerializer
        return SipConfigurationSerializer
    
    @extend_schema(
        summary="Get WebRTC configuration for calling",
        description="Retrieve SIP configuration details needed for WebRTC calling in the frontend",
        responses={
            200: SipConfigurationDetailSerializer,
            404: "SIP configuration not found"
        }
    )
    @action(detail=True, methods=['get'])
    def webrtc_config(self, request, pk=None):
        """Get WebRTC configuration for the frontend"""
        sip_config = self.get_object()
        if not sip_config.is_active:
            return Response(
                {'error': 'SIP configuration is not active'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        serializer = self.get_serializer(sip_config)
        return Response(serializer.data)
    
    @extend_schema(
        summary="Set as default SIP configuration",
        description="Set this SIP configuration as the default for outbound calls",
        responses={200: "Configuration set as default"}
    )
    @action(detail=True, methods=['post'])
    def set_default(self, request, pk=None):
        """Set this configuration as default"""
        sip_config = self.get_object()
        
        # Remove default from other configs in current tenant
        SipConfiguration.objects.filter(is_default=True).exclude(id=sip_config.id).update(is_default=False)
        
        # Set this as default
        sip_config.is_default = True
        sip_config.save()
        
        return Response({'message': 'Configuration set as default'})
    
    @extend_schema(
        summary="Test SIP configuration",
        description="Test the SIP configuration connectivity",
        responses={
            200: "Configuration test successful",
            400: "Configuration test failed"
        }
    )
    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        """Test SIP configuration (placeholder for actual SIP testing)"""
        sip_config = self.get_object()
        
        # In a real implementation, you would test SIP connectivity here
        # For now, just validate that required fields are present
        if not all([sip_config.sip_server, sip_config.username, sip_config.password]):
            return Response(
                {'error': 'Incomplete SIP configuration'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        return Response({
            'message': 'SIP configuration test successful',
            'server': sip_config.sip_server,
            'port': sip_config.sip_port
        })

    @action(detail=False, methods=['get'])
    def my_config(self, request):
        """Get the current user's phone assignment with full SIP config.
        Returns the user's primary active assignment, or falls back to the default SIP config."""
        assignment = UserPhoneAssignment.objects.filter(
            user=request.user, is_active=True, is_primary=True
        ).select_related('sip_configuration').first()

        if assignment:
            return Response(UserPhoneAssignmentDetailSerializer(assignment).data)

        # Fallback: return default config without assignment
        default_config = SipConfiguration.objects.filter(is_default=True, is_active=True).first()
        if default_config:
            return Response({
                'id': None,
                'user': request.user.id,
                'sip_configuration': SipConfigurationDetailSerializer(default_config).data,
                'extension': default_config.username,
                'extension_password': default_config.password,
                'phone_number': default_config.phone_number or '',
                'display_name': '',
                'is_primary': True,
                'is_active': True,
            })

        return Response({'detail': 'No SIP configuration found'}, status=status.HTTP_404_NOT_FOUND)


class UserPhoneAssignmentViewSet(viewsets.ModelViewSet):
    """ViewSet for managing user phone number assignments."""
    serializer_class = UserPhoneAssignmentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return UserPhoneAssignment.objects.select_related('user', 'sip_configuration').all()

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return UserPhoneAssignmentDetailSerializer
        return UserPhoneAssignmentSerializer


class CallLogViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing call logs with SIP integration.

    Handles both inbound and outbound calls, call status updates, and call history.
    """
    serializer_class = CallLogSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'direction', 'call_type']
    search_fields = ['caller_number', 'recipient_number', 'notes', 'client__name']
    ordering_fields = ['started_at', 'duration', 'status']
    ordering = ['-started_at']

    def get_queryset(self):
        # Return calls for users in current tenant - use request.tenant instead of user.tenant
        # Use select_related to avoid N+1 queries when serializer accesses client and sip_configuration
        if hasattr(self.request, 'tenant'):
            # Filter by tenant through the handled_by user's association with tenant tables
            return CallLog.objects.select_related('client', 'social_client', 'transferred_to_user', 'sip_configuration', 'handled_by', 'recording').prefetch_related('events').all()
        else:
            # Fallback for public schema or when tenant is not available
            return CallLog.objects.select_related('client', 'social_client', 'transferred_to_user', 'sip_configuration', 'handled_by', 'recording').prefetch_related('events').all()
    
    def get_serializer_class(self):
        if self.action == 'create':
            return CallLogCreateSerializer
        elif self.action == 'initiate_call':
            return CallInitiateSerializer
        elif self.action in ['update_status', 'end_call']:
            return CallStatusUpdateSerializer
        elif self.action == 'retrieve':
            return CallLogDetailSerializer
        return CallLogSerializer
    
    def perform_create(self, serializer):
        # Automatically set the current user as the handler if not specified
        if not serializer.validated_data.get('handled_by'):
            serializer.save(handled_by=self.request.user)
        else:
            serializer.save()
    
    def _create_call_event(self, call_log, event_type, metadata=None, user=None):
        """Helper method to create call events"""
        CallEvent.objects.create(
            call_log=call_log,
            event_type=event_type,
            metadata=metadata or {},
            user=user or self.request.user
        )

    @staticmethod
    def _match_client(phone_number):
        """Match a phone number to a social client by last 7 digits.
        Returns (crm_client, social_client) tuple."""
        if not phone_number:
            return None, None
        clean = phone_number.replace('+', '').replace(' ', '').replace('-', '')
        last_digits = clean[-7:] if len(clean) >= 7 else clean
        if not last_digits:
            return None, None

        # Try social_integrations.Client (the main client list)
        try:
            from social_integrations.models import Client as SocialClient
            social_client = SocialClient.objects.filter(phone__endswith=last_digits).first()
            if social_client:
                return None, social_client
        except Exception:
            pass

        # Fallback: try CRM Client
        crm_client = Client.objects.filter(phone__endswith=last_digits).first()
        if crm_client:
            return crm_client, None

        return None, None
    
    @extend_schema(
        summary="Initiate an outbound call",
        description="Start a new outbound call using SIP configuration",
        request=CallInitiateSerializer,
        responses={
            201: CallLogSerializer,
            400: "Invalid call data"
        }
    )
    @action(detail=False, methods=['post'])
    def initiate_call(self, request):
        """Initiate an outbound call"""
        serializer = CallInitiateSerializer(data=request.data)
        if serializer.is_valid():
            # Get SIP configuration
            sip_config_id = serializer.validated_data.get('sip_configuration')
            if sip_config_id:
                try:
                    sip_config = SipConfiguration.objects.get(
                        id=sip_config_id,
                        is_active=True
                    )
                except SipConfiguration.DoesNotExist:
                    return Response(
                        {'error': 'Invalid SIP configuration'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                # Use default SIP configuration
                sip_config = SipConfiguration.objects.filter(
                    is_default=True,
                    is_active=True
                ).first()
                
                if not sip_config:
                    return Response(
                        {'error': 'No default SIP configuration found'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            # Get caller number from user's phone assignment
            caller_number = ""
            assignment = UserPhoneAssignment.objects.filter(
                user=request.user, is_active=True, is_primary=True
            ).first()
            if assignment:
                caller_number = assignment.phone_number
            elif sip_config.phone_number:
                caller_number = sip_config.phone_number

            # Auto-match client by recipient phone number
            recipient_num = serializer.validated_data['recipient_number']
            crm_client, social_client = self._match_client(recipient_num)

            # Create call log
            call_log = CallLog.objects.create(
                caller_number=caller_number,
                recipient_number=recipient_num,
                direction='outbound',
                call_type=serializer.validated_data.get('call_type', 'voice'),
                status='initiated',
                handled_by=request.user,
                sip_configuration=sip_config,
                client=crm_client,
                social_client=social_client,
                started_at=timezone.now()
            )
            
            # Create initial call event
            self._create_call_event(
                call_log, 
                'initiated',
                metadata={
                    'sip_config': sip_config.name,
                    'recipient': serializer.validated_data['recipient_number'],
                    'call_type': serializer.validated_data.get('call_type', 'voice')
                }
            )
            
            # In a real implementation, you would initiate the actual SIP call here
            # For now, just return the call log
            
            response_serializer = CallLogSerializer(call_log)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @extend_schema(
        summary="Log an incoming call",
        description="Log an incoming call when it starts ringing",
        request=CallLogCreateSerializer,
        responses={
            201: CallLogSerializer,
            400: "Invalid call data"
        }
    )
    @action(detail=False, methods=['post'])
    def log_incoming_call(self, request):
        """Log an incoming call"""
        # Extract data from request
        caller_number = request.data.get('caller_number', '')
        recipient_number = request.data.get('recipient_number', '')
        sip_call_id = request.data.get('sip_call_id', '')
        
        # Get default SIP configuration
        sip_config = SipConfiguration.objects.filter(
            is_default=True,
            is_active=True
        ).first()
        
        if not sip_config:
            return Response(
                {'error': 'No default SIP configuration found'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Auto-match client by caller phone number
        crm_client, social_client = self._match_client(caller_number)

        # Create call log for incoming call
        call_log = CallLog.objects.create(
            caller_number=caller_number,
            recipient_number=recipient_number,
            direction='inbound',
            call_type='voice',
            status='ringing',
            handled_by=request.user,
            sip_configuration=sip_config,
            sip_call_id=sip_call_id,
            client=crm_client,
            social_client=social_client,
            started_at=timezone.now()
        )
        
        # Create initial call event
        self._create_call_event(
            call_log,
            'ringing',
            metadata={
                'caller': caller_number,
                'recipient': recipient_number,
                'sip_call_id': sip_call_id
            }
        )
        
        response_serializer = CallLogSerializer(call_log)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)
    
    @extend_schema(
        summary="Update call status",
        description="Update the status of an ongoing call",
        request=CallStatusUpdateSerializer,
        responses={
            200: CallLogSerializer,
            404: "Call not found"
        }
    )
    @action(detail=True, methods=['patch'])
    def update_status(self, request, pk=None):
        """Update call status"""
        call_log = self.get_object()
        serializer = CallStatusUpdateSerializer(data=request.data)
        
        if serializer.is_valid():
            old_status = call_log.status
            new_status = serializer.validated_data['status']
            
            # Update call status
            call_log.status = new_status
            
            # Set answered_at if call is answered
            if new_status == 'answered' and not call_log.answered_at:
                call_log.answered_at = timezone.now()
            
            # Set ended_at and calculate duration if call ended
            if new_status in ['ended', 'missed', 'failed', 'cancelled']:
                if not call_log.ended_at:
                    call_log.ended_at = timezone.now()
                    if call_log.answered_at:
                        call_log.duration = call_log.ended_at - call_log.answered_at
                    else:
                        call_log.duration = timedelta(seconds=0)
            
            # Update optional fields
            if 'notes' in serializer.validated_data:
                call_log.notes = serializer.validated_data['notes']
            if 'call_quality_score' in serializer.validated_data:
                call_log.call_quality_score = serializer.validated_data['call_quality_score']
            if 'recording_url' in serializer.validated_data:
                call_log.recording_url = serializer.validated_data['recording_url']
            
            call_log.save()
            
            # Create call event for status change
            event_metadata = {
                'old_status': old_status,
                'new_status': new_status
            }
            if 'notes' in serializer.validated_data:
                event_metadata['notes'] = serializer.validated_data['notes']
            if 'call_quality_score' in serializer.validated_data:
                event_metadata['quality_score'] = serializer.validated_data['call_quality_score']
            
            self._create_call_event(call_log, new_status, metadata=event_metadata)
            
            response_serializer = CallLogSerializer(call_log)
            return Response(response_serializer.data)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @extend_schema(
        summary="End a call",
        description="End an ongoing call and update duration",
        responses={
            200: CallLogSerializer,
            404: "Call not found",
            400: "Call already ended"
        }
    )
    @action(detail=True, methods=['post'])
    def end_call(self, request, pk=None):
        """End an ongoing call"""
        call_log = self.get_object()
        
        if call_log.status in ['ended', 'missed', 'failed', 'cancelled']:
            return Response(
                {'error': 'Call has already ended'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        call_log.status = 'ended'
        call_log.ended_at = timezone.now()
        
        if call_log.answered_at:
            call_log.duration = call_log.ended_at - call_log.answered_at
        else:
            call_log.duration = timedelta(seconds=0)
        
        call_log.save()
        
        # Create call event for call end
        self._create_call_event(
            call_log,
            'ended',
            metadata={
                'duration_seconds': call_log.duration.total_seconds() if call_log.duration else 0,
                'ended_by': 'user'
            }
        )
        
        serializer = CallLogSerializer(call_log)
        return Response(serializer.data)
    
    @extend_schema(
        summary="Get call statistics",
        description="Get call statistics for the current tenant",
        parameters=[
            OpenApiParameter(
                name='period',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description='Time period (today, week, month)',
                enum=['today', 'week', 'month']
            )
        ],
        responses={200: "Call statistics"}
    )
    @action(detail=False, methods=['get'])
    def statistics(self, request):
        """Get call statistics"""
        period = request.query_params.get('period', 'today')
        
        # Calculate date range
        now = timezone.now()
        if period == 'today':
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == 'week':
            start_date = now - timedelta(days=7)
        elif period == 'month':
            start_date = now - timedelta(days=30)
        else:
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        queryset = self.get_queryset().filter(started_at__gte=start_date)
        
        # Calculate statistics
        total_calls = queryset.count()
        answered_calls = queryset.filter(status='answered').count()
        missed_calls = queryset.filter(status='missed').count()
        inbound_calls = queryset.filter(direction='inbound').count()
        outbound_calls = queryset.filter(direction='outbound').count()
        
        # Calculate average duration for answered calls
        answered_durations = queryset.filter(
            status='answered', 
            duration__isnull=False
        ).values_list('duration', flat=True)
        
        avg_duration = None
        if answered_durations:
            total_seconds = sum(d.total_seconds() for d in answered_durations)
            avg_duration = total_seconds / len(answered_durations)
        
        return Response({
            'period': period,
            'total_calls': total_calls,
            'answered_calls': answered_calls,
            'missed_calls': missed_calls,
            'inbound_calls': inbound_calls,
            'outbound_calls': outbound_calls,
            'answer_rate': round((answered_calls / total_calls * 100) if total_calls > 0 else 0, 1),
            'average_duration_seconds': round(avg_duration) if avg_duration else 0
        })
    
    @extend_schema(
        summary="Add call event",
        description="Add a specific event to a call (hold, mute, etc.)",
        request=CallEventSerializer,
        responses={201: CallEventSerializer}
    )
    @action(detail=True, methods=['post'])
    def add_event(self, request, pk=None):
        """Add an event to a call"""
        call_log = self.get_object()
        
        event_data = request.data.copy()
        event_data['call_log'] = call_log.id
        
        serializer = CallEventSerializer(data=event_data)
        if serializer.is_valid():
            serializer.save(user=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @extend_schema(
        summary="Stream call recording audio",
        description=(
            "Proxy the recording WAV bytes from the PBX recording host through "
            "this API. Solves the browser's 403/CORS against the PBX recording "
            "host, keeps the underlying URL private, and supports HTTP Range "
            "so the <audio> element can seek."
        ),
        responses={
            200: OpenApiTypes.BINARY,
            404: OpenApiTypes.OBJECT,
            502: OpenApiTypes.OBJECT,
        },
    )
    @action(detail=True, methods=['get'], url_path='recording')
    def stream_recording(self, request, pk=None):
        """Stream the recording audio for a call log."""
        from django.http import StreamingHttpResponse
        import requests as _requests

        # Recordings are admin-only — agents shouldn't be able to play back
        # individual customer calls. Defense-in-depth alongside the serializer
        # gate that already hides the URL from non-admins.
        if not (request.user.is_staff or request.user.is_superuser):
            return Response(
                {'error': 'Only tenant admins can access call recordings.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        call_log = self.get_object()

        # Pick the first non-empty URL: legacy field, then related CallRecording.
        source_url = call_log.recording_url
        if not source_url:
            try:
                source_url = call_log.recording.file_url
            except CallRecording.DoesNotExist:
                source_url = None
        if not source_url:
            return Response(
                {'error': 'No recording on this call'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Forward a Range request so the audio element can seek.
        upstream_headers = {}
        if 'HTTP_RANGE' in request.META:
            upstream_headers['Range'] = request.META['HTTP_RANGE']

        try:
            upstream = _requests.get(
                source_url,
                headers=upstream_headers,
                stream=True,
                timeout=(5, 30),  # (connect, read)
            )
        except _requests.RequestException as exc:
            logger.warning(
                "Recording proxy unreachable for call %s (%s): %s",
                call_log.pk, source_url, exc,
            )
            return Response(
                {'error': 'Upstream recording server unreachable.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if upstream.status_code >= 400:
            logger.warning(
                "Recording proxy got upstream %s for call %s (%s)",
                upstream.status_code, call_log.pk, source_url,
            )
            upstream.close()
            return Response(
                {'error': f'Upstream recording server returned {upstream.status_code}.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        def iterator():
            try:
                for chunk in upstream.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        # Pick a Content-Type the browser's <audio> element will play. If the
        # upstream nginx ever forgets the wav mime mapping (the system
        # mime.types on Ubuntu doesn't ship with .wav) it sends
        # application/octet-stream which Chrome/Safari refuse to play in an
        # audio tag — override based on the source URL extension.
        upstream_ct = (upstream.headers.get('Content-Type') or '').lower()
        url_lower = source_url.lower()
        if url_lower.endswith('.wav') and (
            not upstream_ct or upstream_ct == 'application/octet-stream'
        ):
            content_type = 'audio/wav'
        elif url_lower.endswith('.mp3') and (
            not upstream_ct or upstream_ct == 'application/octet-stream'
        ):
            content_type = 'audio/mpeg'
        else:
            content_type = upstream_ct or 'audio/wav'

        response = StreamingHttpResponse(
            iterator(),
            status=upstream.status_code,
            content_type=content_type,
        )
        # Pass through seeking / caching headers where available.
        for header in ('Content-Length', 'Accept-Ranges', 'Content-Range', 'Last-Modified', 'ETag'):
            if header in upstream.headers:
                response[header] = upstream.headers[header]
        response['Cache-Control'] = 'private, max-age=600'
        response['Content-Disposition'] = f'inline; filename="call-{call_log.pk}.wav"'
        return response

    @extend_schema(
        summary="Start call recording",
        description="Start recording for an active call",
        responses={
            201: CallRecordingSerializer,
            400: "Recording already exists or call not active"
        }
    )
    @action(detail=True, methods=['post'])
    def start_recording(self, request, pk=None):
        """Start recording for a call"""
        call_log = self.get_object()
        
        # Check if call is active
        if call_log.status not in ['answered', 'on_hold']:
            return Response(
                {'error': 'Call must be answered to start recording'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if recording already exists
        if hasattr(call_log, 'recording'):
            return Response(
                {'error': 'Recording already exists for this call'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create recording record
        recording = CallRecording.objects.create(
            call_log=call_log,
            status='recording',
            started_at=timezone.now()
        )
        
        # Create call event
        self._create_call_event(
            call_log,
            'recording_started',
            metadata={'recording_id': str(recording.recording_id)}
        )
        
        # Update call status to indicate recording
        call_log.status = 'recording'
        call_log.save()
        
        serializer = CallRecordingSerializer(recording)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @extend_schema(
        summary="Stop call recording",
        description="Stop recording for an active call",
        responses={
            200: CallRecordingSerializer,
            400: "No active recording found"
        }
    )
    @action(detail=True, methods=['post'])
    def stop_recording(self, request, pk=None):
        """Stop recording for a call"""
        call_log = self.get_object()
        
        # Check if recording exists
        if not hasattr(call_log, 'recording'):
            return Response(
                {'error': 'No recording found for this call'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        recording = call_log.recording
        if recording.status != 'recording':
            return Response(
                {'error': 'Recording is not active'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Stop recording
        recording.status = 'processing'
        recording.completed_at = timezone.now()
        if recording.started_at:
            recording.duration = recording.completed_at - recording.started_at
        recording.save()
        
        # Create call event
        self._create_call_event(
            call_log,
            'recording_stopped',
            metadata={
                'recording_id': str(recording.recording_id),
                'duration_seconds': recording.duration.total_seconds() if recording.duration else 0
            }
        )
        
        # Update call status back to answered
        call_log.status = 'answered'
        call_log.save()
        
        serializer = CallRecordingSerializer(recording)
        return Response(serializer.data)
    
    @extend_schema(
        summary="Transfer call",
        description="Transfer an active call to another number",
        responses={200: CallLogSerializer}
    )
    @action(detail=True, methods=['post'])
    def transfer_call(self, request, pk=None):
        """Transfer call to another number"""
        call_log = self.get_object()
        transfer_to = request.data.get('transfer_to')
        
        if not transfer_to:
            return Response(
                {'error': 'transfer_to number is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if call_log.status not in ['answered', 'on_hold']:
            return Response(
                {'error': 'Call must be answered to transfer'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create transfer events
        self._create_call_event(
            call_log,
            'transfer_initiated',
            metadata={'transfer_to': transfer_to}
        )
        
        # In a real implementation, you would initiate the SIP transfer here
        # For now, just update the status and create completion event
        
        call_log.status = 'transferred'
        call_log.ended_at = timezone.now()
        if call_log.answered_at:
            call_log.duration = call_log.ended_at - call_log.answered_at
        call_log.save()
        
        self._create_call_event(
            call_log,
            'transfer_completed',
            metadata={'transfer_to': transfer_to}
        )
        
        serializer = CallLogSerializer(call_log)
        return Response(serializer.data)

    @extend_schema(
        summary="Initiate consultation call for attended transfer",
        description="Start a consultation call to a target number before completing an attended transfer",
        request=ConsultationInitiateSerializer,
        responses={
            201: "Consultation call initiated",
            400: "Invalid request or call not in correct state",
            404: "Call not found",
        },
    )
    @action(detail=True, methods=['post'])
    def initiate_consultation(self, request, pk=None):
        """Initiate a consultation call for an attended (warm) transfer."""
        original_call = self.get_object()

        serializer = ConsultationInitiateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        target_number = serializer.validated_data['target_number']
        target_user_id = serializer.validated_data.get('target_user_id')

        # Validate original call is in a transferable state
        if original_call.status not in ('answered', 'on_hold'):
            return Response(
                {'error': 'Call must be answered or on hold to initiate consultation'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create the consultation leg
        consultation_log = CallLog.objects.create(
            direction='outbound',
            caller_number=original_call.recipient_number if original_call.direction == 'inbound' else original_call.caller_number,
            recipient_number=target_number,
            parent_call=original_call,
            status='initiated',
            handled_by=request.user,
            sip_configuration=original_call.sip_configuration,
        )

        # If a target user was specified, record it
        if target_user_id:
            try:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                target_user = User.objects.get(pk=target_user_id)
                consultation_log.transferred_to_user = target_user
                consultation_log.save(update_fields=['transferred_to_user'])
            except User.DoesNotExist:
                pass

        # Create transfer_initiated event on the original call
        self._create_call_event(
            original_call,
            'transfer_initiated',
            metadata={
                'transfer_type': 'attended',
                'target_number': target_number,
                'consultation_log_id': consultation_log.id,
            },
        )

        # Put the original call on hold
        original_call.status = 'on_hold'
        original_call.save(update_fields=['status'])

        return Response(
            {
                'consultation_log_id': consultation_log.id,
                'consultation_call_id': str(consultation_log.call_id),
                'original_call_id': str(original_call.call_id),
            },
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Complete attended transfer",
        description="Complete an attended transfer after a successful consultation call",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "consultation_log_id": {"type": "integer"},
                    "target_number": {"type": "string"},
                },
                "required": ["consultation_log_id", "target_number"],
            }
        },
        responses={
            200: CallLogSerializer,
            400: "Invalid request or call not in correct state",
            404: "Call or consultation log not found",
        },
    )
    @action(detail=True, methods=['post'])
    def complete_attended_transfer(self, request, pk=None):
        """Complete an attended (warm) transfer."""
        original_call = self.get_object()

        consultation_log_id = request.data.get('consultation_log_id')
        target_number = request.data.get('target_number')

        if not consultation_log_id or not target_number:
            return Response(
                {'error': 'consultation_log_id and target_number are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            consultation_log = CallLog.objects.get(pk=consultation_log_id, parent_call=original_call)
        except CallLog.DoesNotExist:
            return Response(
                {'error': 'Consultation log not found for this call'},
                status=status.HTTP_404_NOT_FOUND,
            )

        now = timezone.now()

        # Mark original call as transferred
        original_call.status = 'transferred'
        original_call.transfer_type = 'attended'
        original_call.transferred_to = target_number
        original_call.transferred_at = now
        original_call.ended_at = now
        if original_call.answered_at:
            original_call.duration = now - original_call.answered_at

        # Try to set transferred_to_user from the consultation log's recipient
        if consultation_log.transferred_to_user:
            original_call.transferred_to_user = consultation_log.transferred_to_user
        else:
            # Try to find user by phone assignment
            assignment = UserPhoneAssignment.objects.filter(
                phone_number__endswith=target_number[-7:], is_active=True
            ).first()
            if assignment:
                original_call.transferred_to_user = assignment.user

        original_call.save()

        # Create transfer_completed event
        self._create_call_event(
            original_call,
            'transfer_completed',
            metadata={
                'transfer_type': 'attended',
                'target_number': target_number,
                'consultation_log_id': consultation_log.id,
            },
        )

        serializer = CallLogSerializer(original_call)
        return Response(serializer.data)

    @extend_schema(
        summary="Cancel consultation call",
        description="Cancel an ongoing consultation and return to the original call",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "consultation_log_id": {"type": "integer"},
                },
                "required": ["consultation_log_id"],
            }
        },
        responses={
            200: CallLogSerializer,
            400: "Invalid request",
            404: "Call or consultation log not found",
        },
    )
    @action(detail=True, methods=['post'])
    def cancel_consultation(self, request, pk=None):
        """Cancel a consultation call and resume the original call."""
        original_call = self.get_object()

        consultation_log_id = request.data.get('consultation_log_id')
        if not consultation_log_id:
            return Response(
                {'error': 'consultation_log_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            consultation_log = CallLog.objects.get(pk=consultation_log_id, parent_call=original_call)
        except CallLog.DoesNotExist:
            return Response(
                {'error': 'Consultation log not found for this call'},
                status=status.HTTP_404_NOT_FOUND,
            )

        now = timezone.now()

        # End the consultation call
        consultation_log.status = 'ended'
        consultation_log.ended_at = now
        if consultation_log.answered_at:
            consultation_log.duration = now - consultation_log.answered_at
        consultation_log.save()

        # Resume the original call
        original_call.status = 'answered'
        original_call.save(update_fields=['status'])

        # Record the cancellation event
        self._create_call_event(
            original_call,
            'transfer_initiated',
            metadata={'action': 'consultation_cancelled'},
        )

        serializer = CallLogSerializer(original_call)
        return Response(serializer.data)

    @extend_schema(
        summary="Merge into 3-way conference",
        description=(
            "Merge an attended-transfer consultation call into a 3-way conference. "
            "Both the original (on-hold) call and the consultation call are redirected "
            "into an Asterisk ConfBridge room via AMI."
        ),
        request=MergeConferenceSerializer,
        responses={
            200: inline_serializer(
                name="MergeConferenceResponse",
                fields={
                    "conference_room": drf_serializers.CharField(),
                    "original_call_id": drf_serializers.CharField(),
                    "consultation_call_id": drf_serializers.CharField(),
                    "channels_redirected": drf_serializers.ListField(
                        child=drf_serializers.CharField()
                    ),
                },
            ),
            400: "Invalid request or call not in correct state",
            404: "Call or consultation log not found",
            502: "AMI communication error",
        },
    )
    @action(detail=True, methods=["post"])
    def merge_conference(self, request, pk=None):
        """Merge an attended-transfer consultation into a 3-way conference via Asterisk AMI."""
        original_call = self.get_object()

        serializer = MergeConferenceSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        consultation_log_id = serializer.validated_data["consultation_log_id"]

        # --- Validate original call state --------------------------------
        if original_call.status != "on_hold":
            return Response(
                {"error": "Original call must be on hold to merge into conference"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if original_call.transfer_type != "attended":
            return Response(
                {"error": "Original call must have transfer_type='attended'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --- Look up the consultation leg --------------------------------
        try:
            consultation_log = CallLog.objects.get(
                pk=consultation_log_id, parent_call=original_call
            )
        except CallLog.DoesNotExist:
            return Response(
                {"error": "Consultation log not found for this call"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # --- Resolve this tenant's PbxServer for AMI creds --------------
        from crm.asterisk_db import get_active_pbx_for_current_tenant

        pbx = get_active_pbx_for_current_tenant()
        if pbx is None:
            return Response(
                {"error": "PBX not configured for this tenant"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        pbx_host = pbx.ami_host or pbx.fqdn
        ami_user = pbx.ami_username
        ami_pass = pbx.ami_password
        ami_port = pbx.ami_port or 5038
        if not (pbx_host and ami_user and ami_pass):
            return Response(
                {"error": "PbxServer missing AMI credentials"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # --- Generate a unique conference room ID -----------------------
        conference_room = f"conf_{original_call.id}"

        # --- Discover active channels via AMI ---------------------------
        try:
            all_channels = _ami_get_channels(pbx_host, ami_user, ami_pass, port=ami_port)
        except Exception as exc:
            logger.error("AMI CoreShowChannels failed on %s: %s", pbx_host, exc)
            return Response(
                {"error": f"Failed to list AMI channels: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Build a set of channel names we want to redirect.
        # Strategy: find channels whose calleridnum or connectedlinenum
        # matches the phone numbers involved in the two call legs, OR
        # whose channel name contains the agent's extension.
        agent_extension = None
        assignment = UserPhoneAssignment.objects.filter(
            user=request.user, is_active=True, is_primary=True
        ).first()
        if assignment:
            agent_extension = assignment.extension

        # Collect the relevant phone numbers (last 7 digits for matching)
        def _last7(number):
            clean = number.replace("+", "").replace(" ", "").replace("-", "")
            return clean[-7:] if len(clean) >= 7 else clean

        relevant_numbers = set()
        for num_field in [
            original_call.caller_number,
            original_call.recipient_number,
            consultation_log.caller_number,
            consultation_log.recipient_number,
        ]:
            if num_field:
                relevant_numbers.add(_last7(num_field))

        matched_channels = []
        for ch in all_channels:
            ch_name = ch.get("channel", "")

            # Match by agent extension in channel name (e.g. PJSIP/100-00000002)
            if agent_extension and f"/{agent_extension}-" in ch_name:
                matched_channels.append(ch_name)
                continue

            # Match by caller ID or connected-line number
            for field_key in ("calleridnum", "connectedlinenum"):
                num = ch.get(field_key, "")
                if num and _last7(num) in relevant_numbers:
                    matched_channels.append(ch_name)
                    break

        # Deduplicate while preserving order
        seen = set()
        unique_channels = []
        for ch_name in matched_channels:
            if ch_name not in seen:
                seen.add(ch_name)
                unique_channels.append(ch_name)

        if not unique_channels:
            return Response(
                {"error": "No active Asterisk channels found for this call"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --- Redirect each channel into the ConfBridge ------------------
        redirected = []
        errors = []
        for ch_name in unique_channels:
            try:
                _ami_redirect_to_confbridge(
                    pbx_host, ch_name, conference_room,
                    ami_user, ami_pass, port=ami_port,
                )
                redirected.append(ch_name)
            except Exception as exc:
                logger.error("AMI Redirect failed for %s: %s", ch_name, exc)
                errors.append(f"{ch_name}: {exc}")

        if not redirected:
            return Response(
                {"error": f"All AMI redirects failed: {'; '.join(errors)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # --- Update CallLog records ------------------------------------
        now = timezone.now()

        original_call.status = "answered"
        original_call.call_type = "conference"
        original_call.save(update_fields=["status", "call_type", "updated_at"])

        consultation_log.status = "answered"
        consultation_log.call_type = "conference"
        consultation_log.save(update_fields=["status", "call_type", "updated_at"])

        # --- Create CallEvents -----------------------------------------
        event_metadata = {
            "conference_room": conference_room,
            "channels_redirected": redirected,
            "consultation_log_id": consultation_log.id,
        }
        if errors:
            event_metadata["redirect_errors"] = errors

        self._create_call_event(
            original_call,
            "conference_started",
            metadata=event_metadata,
        )
        self._create_call_event(
            consultation_log,
            "conference_started",
            metadata=event_metadata,
        )

        return Response(
            {
                "conference_room": conference_room,
                "original_call_id": str(original_call.call_id),
                "consultation_call_id": str(consultation_log.call_id),
                "channels_redirected": redirected,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Hold/Unhold call",
        description="Put call on hold or resume from hold",
        responses={200: CallLogSerializer}
    )
    @action(detail=True, methods=['post'])
    def toggle_hold(self, request, pk=None):
        """Toggle hold status for a call"""
        call_log = self.get_object()
        
        if call_log.status == 'answered':
            new_status = 'on_hold'
            event_type = 'hold'
        elif call_log.status == 'on_hold':
            new_status = 'answered'
            event_type = 'unhold'
        else:
            return Response(
                {'error': 'Call must be answered or on hold'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        call_log.status = new_status
        call_log.save()
        
        self._create_call_event(call_log, event_type)

        serializer = CallLogSerializer(call_log)
        return Response(serializer.data)

    @extend_schema(
        summary="Send SMS review request for a completed call",
        description="Send an SMS with a rating link to the caller of a completed call.",
        responses={
            200: inline_serializer(
                name='SmsReviewResponse',
                fields={
                    'status': drf_serializers.CharField(),
                    'message_id': drf_serializers.CharField(allow_null=True),
                }
            ),
            400: "Bad request",
            502: "SMS delivery failed",
        }
    )
    @action(detail=True, methods=['post'])
    def send_sms_review(self, request, pk=None):
        """Send SMS review request for a completed call."""
        import secrets
        from django.db import connection
        from .sms_utils import send_sms

        call_log = self.get_object()
        caller_number = call_log.caller_number

        # Get PBX settings
        sip_config = call_log.sip_configuration
        if not sip_config:
            sip_config = SipConfiguration.objects.filter(is_default=True).first()
        if not sip_config:
            return Response({'error': 'No SIP config'}, status=status.HTTP_400_BAD_REQUEST)

        pbx_settings, _ = PbxSettings.objects.get_or_create(sip_configuration=sip_config)

        if not pbx_settings.sms_api_key:
            return Response({'error': 'SMS API key not configured'}, status=status.HTTP_400_BAD_REQUEST)

        # Check cooldown: don't send if a review was sent recently for this caller
        cooldown_cutoff = timezone.now() - timedelta(hours=pbx_settings.review_cooldown_hours)
        recent_rating = CallRating.objects.filter(
            caller_number__endswith=caller_number.replace('+', '').replace(' ', '')[-7:],
            sip_configuration=sip_config,
            created_at__gte=cooldown_cutoff,
        ).first()
        if recent_rating:
            return Response(
                {'error': 'Review request already sent recently for this caller'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Generate token
        token = secrets.token_urlsafe(48)
        expires = timezone.now() + timedelta(days=7)

        # Build rating link
        tenant = connection.schema_name
        link = f"https://{tenant}.echodesk.ge/review?token={token}&type=call"

        # Get template
        template = pbx_settings.sms_rating_template_ka or pbx_settings.sms_rating_template_en
        message = template.replace('{link}', link)

        # Send SMS
        result = send_sms(pbx_settings.sms_api_key, caller_number, message)

        if 'error' in result:
            return Response({'error': result['error']}, status=status.HTTP_502_BAD_GATEWAY)

        # Create CallRating record
        CallRating.objects.create(
            call_log=call_log,
            caller_number=caller_number,
            rated_user=call_log.handled_by,
            rating_token=token,
            token_expires_at=expires,
            review_method='sms',
            sip_configuration=sip_config,
            sms_message_id=result.get('messageId', ''),
        )

        return Response({'status': 'sent', 'message_id': result.get('messageId')})


class ClientViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing clients/customers.
    """
    serializer_class = ClientSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        # In a multi-tenant setup, you might want to filter by tenant
        return Client.objects.all()
    
    @extend_schema(
        summary="Get client call history",
        description="Get all calls associated with this client",
        responses={200: CallLogSerializer(many=True)}
    )
    @action(detail=True, methods=['get'])
    def call_history(self, request, pk=None):
        """Get call history for a specific client"""
        client = self.get_object()
        calls = CallLog.objects.filter(
            client=client
        ).select_related('client', 'sip_configuration', 'handled_by').order_by('-started_at')

        serializer = CallLogSerializer(calls, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="Lookup client by phone number (CRM + Social)",
        description=(
            "Find the most likely Client/SocialClient match for an arbitrary "
            "phone number. Cleans the input, takes the last 7 digits, and "
            "icontains-matches both crm.Client and social_integrations.SocialClient. "
            "Returns the first hit (CRM preferred since it's manually curated) "
            "in a unified shape so the call sidebar can render either source. "
            "Used by the live-call sidebar so a number like 597147515 finds a "
            "stored phone of '+995597147515'."
        ),
        parameters=[
            OpenApiParameter(
                name='phone',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=True,
                description='Phone number in any format (digits, spaces, dashes, leading +).',
            ),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    @action(detail=False, methods=['get'], url_path='lookup-by-phone')
    def lookup_by_phone(self, request):
        """Find a Client or SocialClient that owns this phone number."""
        phone = (request.query_params.get('phone') or '').strip()
        if not phone:
            return Response({'results': [], 'count': 0})

        cleaned = ''.join(ch for ch in phone if ch.isdigit())
        if not cleaned:
            return Response({'results': [], 'count': 0})
        last_digits = cleaned[-7:] if len(cleaned) >= 7 else cleaned

        # Prefer the CRM Client (manually curated) over SocialClient.
        client = Client.objects.filter(phone__icontains=last_digits).first()
        if client:
            return Response({
                'results': [{
                    'id': client.id,
                    'name': client.name,
                    'email': client.email,
                    'phone': client.phone,
                    'company': client.company,
                    'source': 'crm',
                }],
                'count': 1,
            })

        from social_integrations.models import SocialClient
        sc = SocialClient.objects.filter(phone__icontains=last_digits).first()
        if sc:
            display_name = sc.name or f'{sc.first_name or ""} {sc.last_name or ""}'.strip()
            return Response({
                'results': [{
                    'id': sc.id,
                    'name': display_name,
                    'email': sc.email or '',
                    'phone': sc.phone,
                    'company': '',
                    'source': 'social',
                }],
                'count': 1,
            })

        return Response({'results': [], 'count': 0})


@extend_schema(
    summary="SIP Call Event Webhook",
    description="Webhook endpoint for receiving call events from SIP server",
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "event_type": {"type": "string", "enum": ["call_initiated", "call_ringing", "call_answered", "call_ended", "call_failed"]},
                "call_id": {"type": "string"},
                "sip_call_id": {"type": "string"},
                "caller_number": {"type": "string"},
                "recipient_number": {"type": "string"},
                "timestamp": {"type": "string", "format": "date-time"},
                "metadata": {"type": "object"}
            },
            "required": ["event_type", "sip_call_id"]
        }
    },
    responses={200: "Event processed successfully"}
)
@api_view(['POST'])
@permission_classes([])
@csrf_exempt
def sip_webhook(request):
    """
    Webhook endpoint for receiving SIP call events
    This endpoint should be called by your SIP server to update call status
    """
    try:
        data = request.data
        event_type = data.get('event_type')
        sip_call_id = data.get('sip_call_id')
        caller_number = data.get('caller_number', '')
        recipient_number = data.get('recipient_number', '')
        metadata = data.get('metadata', {})
        
        if not event_type or not sip_call_id:
            return Response(
                {'error': 'event_type and sip_call_id are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Map SIP events to our call statuses
        event_status_map = {
            'call_initiated': 'initiated',
            'call_ringing': 'ringing',
            'call_answered': 'answered',
            'call_ended': 'ended',
            'call_failed': 'failed',
            'call_busy': 'busy',
            'call_no_answer': 'no_answer'
        }
        
        call_status = event_status_map.get(event_type, 'ringing')
        
        # Try to find existing call log by SIP call ID
        try:
            call_log = CallLog.objects.get(sip_call_id=sip_call_id)
            
            # Update existing call
            old_status = call_log.status
            call_log.status = call_status
            
            if call_status == 'answered' and not call_log.answered_at:
                call_log.answered_at = timezone.now()
            elif call_status in ['ended', 'failed', 'busy', 'no_answer']:
                if not call_log.ended_at:
                    call_log.ended_at = timezone.now()
                    if call_log.answered_at:
                        call_log.duration = call_log.ended_at - call_log.answered_at
                    else:
                        call_log.duration = timedelta(seconds=0)
            
            call_log.save()
            
            # Create call event
            CallEvent.objects.create(
                call_log=call_log,
                event_type=event_type.replace('call_', ''),
                metadata={
                    'sip_event': True,
                    'old_status': old_status,
                    'new_status': call_status,
                    **metadata
                }
            )
            
        except CallLog.DoesNotExist:
            # Create new call log for incoming calls
            if event_type in ['call_initiated', 'call_ringing']:
                # Get default SIP configuration
                sip_config = SipConfiguration.objects.filter(
                    is_default=True,
                    is_active=True
                ).first()
                
                call_log = CallLog.objects.create(
                    caller_number=caller_number,
                    recipient_number=recipient_number,
                    direction='inbound' if caller_number != recipient_number else 'outbound',
                    status=call_status,
                    sip_call_id=sip_call_id,
                    sip_configuration=sip_config,
                    started_at=timezone.now()
                )
                
                # Create initial event
                CallEvent.objects.create(
                    call_log=call_log,
                    event_type=event_type.replace('call_', ''),
                    metadata={
                        'sip_event': True,
                        'caller': caller_number,
                        'recipient': recipient_number,
                        **metadata
                    }
                )
            else:
                return Response(
                    {'error': f'Call not found for sip_call_id: {sip_call_id}'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        return Response({
            'message': 'Event processed successfully',
            'call_id': str(call_log.call_id),
            'status': call_log.status
        })
        
    except Exception as e:
        return Response(
            {'error': f'Error processing webhook: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@extend_schema(
    summary="Call Recording Webhook",
    description="Webhook endpoint for receiving call recording updates",
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "call_id": {"type": "string"},
                "recording_id": {"type": "string"},
                "status": {"type": "string", "enum": ["started", "completed", "failed"]},
                "file_url": {"type": "string"},
                "file_size": {"type": "integer"},
                "duration": {"type": "integer", "description": "Duration in seconds"},
                "format": {"type": "string"}
            },
            "required": ["call_id", "status"]
        }
    },
    responses={200: "Recording update processed successfully"}
)
@api_view(['POST'])
@permission_classes([])
@csrf_exempt
def recording_webhook(request):
    """
    Webhook endpoint for receiving call recording updates
    """
    try:
        data = request.data
        call_id = data.get('call_id')
        recording_status = data.get('status')
        file_url = data.get('file_url', '')
        file_size = data.get('file_size')
        duration_seconds = data.get('duration')
        format_type = data.get('format', 'wav')
        
        if not call_id or not recording_status:
            return Response(
                {'error': 'call_id and status are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Find call log
        try:
            call_log = CallLog.objects.get(call_id=call_id)
        except CallLog.DoesNotExist:
            return Response(
                {'error': f'Call not found for call_id: {call_id}'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get or create recording
        recording, created = CallRecording.objects.get_or_create(
            call_log=call_log,
            defaults={
                'status': 'pending',
                'format': format_type
            }
        )
        
        # Update recording
        recording.status = recording_status
        if file_url:
            recording.file_url = file_url
        if file_size:
            recording.file_size = file_size
        if duration_seconds:
            recording.duration = timedelta(seconds=duration_seconds)

        if recording_status == 'started':
            recording.started_at = timezone.now()
        elif recording_status == 'completed':
            recording.completed_at = timezone.now()

        recording.save()

        # Also mirror the URL onto the legacy CallLog.recording_url field so
        # list endpoints / older clients still see the recording even without
        # the serializer fallback.
        if file_url and call_log.recording_url != file_url:
            call_log.recording_url = file_url
            call_log.save(update_fields=['recording_url'])
        
        # Create call event
        event_type = f'recording_{recording_status}'
        CallEvent.objects.create(
            call_log=call_log,
            event_type=event_type,
            metadata={
                'recording_webhook': True,
                'recording_id': str(recording.recording_id),
                'file_url': file_url,
                'file_size': file_size,
                'duration_seconds': duration_seconds
            }
        )
        
        return Response({
            'message': 'Recording update processed successfully',
            'recording_id': str(recording.recording_id),
            'status': recording.status
        })
        
    except Exception as e:
        return Response(
            {'error': f'Error processing recording webhook: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
@permission_classes([])
@csrf_exempt
def call_rating_webhook(request):
    """
    Webhook endpoint for receiving call ratings from PBX.
    Called by Asterisk after the customer rates the call (1-5).
    """
    try:
        data = request.data
        caller_number = data.get('caller_number', '').strip()
        rating = data.get('rating')

        if not caller_number or not rating:
            return Response(
                {'error': 'caller_number and rating are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            rating = int(rating)
            if rating < 1 or rating > 5:
                raise ValueError
        except (ValueError, TypeError):
            return Response(
                {'error': 'rating must be an integer between 1 and 5'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Find the most recent inbound call from this number
        # Match by last 7 digits to handle format differences
        clean_number = caller_number.replace('+', '').replace(' ', '')
        last_digits = clean_number[-7:] if len(clean_number) >= 7 else clean_number

        call_log = CallLog.objects.filter(
            direction='inbound',
            caller_number__endswith=last_digits
        ).order_by('-started_at').first()

        if not call_log:
            return Response(
                {'error': f'No recent inbound call found from {caller_number}'},
                status=status.HTTP_404_NOT_FOUND
            )

        call_log.call_quality_score = float(rating)
        call_log.save(update_fields=['call_quality_score'])

        # Log the rating event
        CallEvent.objects.create(
            call_log=call_log,
            event_type='rating',
            metadata={
                'rating': rating,
                'caller_number': caller_number,
                'source': 'pbx_callback',
            }
        )

        # Also persist a CallRating row so the rating-statistics dashboard
        # picks it up. The webhook used to only update CallLog.call_quality_score
        # which left rating-statistics empty for callback-based reviews —
        # the page reads from CallRating exclusively. update_or_create keyed
        # on call_log + review_method='callback' makes this idempotent if
        # the same call gets rated twice (re-callback).
        if call_log.sip_configuration_id:
            from django.utils import timezone as _tz
            CallRating.objects.update_or_create(
                call_log=call_log,
                review_method='callback',
                defaults={
                    'caller_number': caller_number,
                    'rated_user': call_log.handled_by,
                    'sip_configuration_id': call_log.sip_configuration_id,
                    'rating': rating,
                    'rated_at': _tz.now(),
                },
            )

        return Response({
            'message': f'Rating {rating}/5 saved for call {call_log.call_id}',
            'call_id': str(call_log.call_id),
            'rating': rating,
        })

    except Exception as e:
        return Response(
            {'error': f'Error processing rating: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@csrf_exempt
@api_view(['POST'])
@permission_classes([])
def send_call_review_sms(request):
    """
    Called by PBX scripts to send SMS review for a caller.
    Finds the most recent call by caller_number and sends an SMS review link.

    Auth: Bearer token via PBX_SHARED_SECRET env var.

    POST body:
        caller_number: The caller's phone number
        did: (optional) The DID that was called, to locate the tenant/SIP config
    """
    import secrets
    from django.conf import settings as django_settings
    from django.db import connection
    from tenant_schemas.utils import schema_context
    from tenants.models import Tenant
    from .sms_utils import send_sms

    # Authenticate via shared secret
    auth_header = request.META.get('HTTP_AUTHORIZATION', '')
    expected_token = getattr(django_settings, 'PBX_SHARED_SECRET', '')
    if expected_token and not auth_header.endswith(expected_token):
        return Response({'error': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

    caller_number = request.data.get('caller_number', '').strip()
    did = request.data.get('did', '').strip()

    if not caller_number:
        return Response({'error': 'caller_number is required'}, status=status.HTTP_400_BAD_REQUEST)

    clean_caller = caller_number.replace('+', '').replace(' ', '').replace('-', '')
    last_digits = clean_caller[-7:] if len(clean_caller) >= 7 else clean_caller
    clean_did = did.replace('+', '').replace(' ', '').replace('-', '') if did else ''

    # Search across tenants
    tenants = Tenant.objects.exclude(schema_name='public')
    for tenant in tenants:
        try:
            with schema_context(tenant.schema_name):
                # Find matching SIP config
                sip_config = None
                if clean_did:
                    sip_config = SipConfiguration.objects.filter(
                        phone_number__endswith=clean_did[-7:]
                    ).first()
                if not sip_config:
                    sip_config = SipConfiguration.objects.filter(is_default=True).first()
                if not sip_config:
                    continue

                pbx_settings, _ = PbxSettings.objects.get_or_create(sip_configuration=sip_config)

                # Check if SMS review is enabled
                if pbx_settings.review_method not in ('sms', 'both'):
                    continue

                if not pbx_settings.sms_api_key:
                    continue

                # Find the most recent call from this caller
                call_log = CallLog.objects.filter(
                    direction='inbound',
                    caller_number__endswith=last_digits,
                ).order_by('-started_at').first()

                if not call_log:
                    continue

                # Check cooldown
                cooldown_cutoff = timezone.now() - timedelta(hours=pbx_settings.review_cooldown_hours)
                recent_rating = CallRating.objects.filter(
                    caller_number__endswith=last_digits,
                    sip_configuration=sip_config,
                    created_at__gte=cooldown_cutoff,
                ).first()
                if recent_rating:
                    return Response(
                        {'error': 'Review request already sent recently'},
                        status=status.HTTP_429_TOO_MANY_REQUESTS,
                    )

                # Generate token and send SMS
                token = secrets.token_urlsafe(48)
                expires = timezone.now() + timedelta(days=7)
                link = f"https://{tenant.schema_name}.echodesk.ge/review?token={token}&type=call"

                template = pbx_settings.sms_rating_template_ka or pbx_settings.sms_rating_template_en
                message = template.replace('{link}', link)

                result = send_sms(pbx_settings.sms_api_key, caller_number, message)

                if 'error' in result:
                    return Response({'error': result['error']}, status=status.HTTP_502_BAD_GATEWAY)

                CallRating.objects.create(
                    call_log=call_log,
                    caller_number=caller_number,
                    rated_user=call_log.handled_by,
                    rating_token=token,
                    token_expires_at=expires,
                    review_method='sms',
                    sip_configuration=sip_config,
                    sms_message_id=result.get('messageId', ''),
                )

                return Response({
                    'status': 'sent',
                    'message_id': result.get('messageId'),
                    'tenant': tenant.schema_name,
                })
        except Exception as exc:
            logger.error(f"send_call_review_sms error in tenant {tenant.schema_name}: {exc}")
            continue

    return Response(
        {'error': 'No matching tenant or call found'},
        status=status.HTTP_404_NOT_FOUND,
    )


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def extension_status(request):
    """Proxy to PBX status API — returns which extensions are online.

    Resolves the tenant's active :class:`PbxServer` first so each tenant
    gets a response from their own Asterisk. Falls back to the default
    SipConfiguration when no PbxServer is registered yet, keeping the
    legacy shared-pbx2 path alive during the BYO migration window.
    """
    import requests as http_requests
    from django.core.cache import cache
    from crm.asterisk_db import get_active_pbx_for_current_tenant

    try:
        pbx = get_active_pbx_for_current_tenant()
        if pbx is not None:
            pbx_host = pbx.ami_host or pbx.fqdn
        else:
            sip_config = SipConfiguration.objects.filter(
                is_default=True, is_active=True
            ).first()
            if not sip_config:
                return Response({'extensions': []})
            pbx_host = sip_config.sip_server

        if not pbx_host:
            return Response({'extensions': []})

        # Cache the PBX response briefly so a polling "who's online" UI doesn't
        # hit (and block a worker on) the PBX on every request.
        cache_key = f'pbx_extension_status:{pbx_host}'
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        resp = http_requests.get(
            f'http://{pbx_host}:8081/api/extensions/status', timeout=3
        )
        data = resp.json()
        cache.set(cache_key, data, timeout=5)
        return Response(data)
    except Exception:
        return Response({'extensions': []})


@api_view(['POST'])
@permission_classes([])
@csrf_exempt
def call_recording_url_webhook(request):
    """Save recording URL to the most recent call from a caller number."""
    try:
        data = request.data
        caller_number = data.get('caller_number', '').strip()
        recording_url = data.get('recording_url', '').strip()

        if not caller_number or not recording_url:
            return Response(
                {'error': 'caller_number and recording_url are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        clean_number = caller_number.replace('+', '').replace(' ', '')
        last_digits = clean_number[-7:] if len(clean_number) >= 7 else clean_number

        call_log = CallLog.objects.filter(
            caller_number__endswith=last_digits
        ).order_by('-started_at').first()

        if not call_log:
            # Also check recipient number for outbound calls
            call_log = CallLog.objects.filter(
                recipient_number__endswith=last_digits
            ).order_by('-started_at').first()

        if not call_log:
            return Response(
                {'error': f'No call found for {caller_number}'},
                status=status.HTTP_404_NOT_FOUND
            )

        call_log.recording_url = recording_url
        call_log.save(update_fields=['recording_url'])

        return Response({
            'message': f'Recording URL saved for call {call_log.call_id}',
            'call_id': str(call_log.call_id),
        })

    except Exception as e:
        return Response(
            {'error': f'Error: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ============================================================================
# PBX SETTINGS (Working Hours + Sound Management)
# ============================================================================


def _get_pbx_settings(sip_config_id):
    sip_config = SipConfiguration.objects.get(id=sip_config_id)
    settings_obj, _ = PbxSettings.objects.get_or_create(sip_configuration=sip_config)
    return settings_obj


@api_view(['GET', 'PATCH'])
@permission_classes([permissions.IsAuthenticated])
def pbx_settings_detail(request, sip_config_id):
    """Get or update PBX settings for a SIP configuration."""
    try:
        settings_obj = _get_pbx_settings(sip_config_id)
    except SipConfiguration.DoesNotExist:
        return Response({'error': 'SIP configuration not found'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        return Response(PbxSettingsSerializer(settings_obj).data)

    # PATCH
    serializer = PbxSettingsSerializer(settings_obj, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def pbx_settings_upload_sound(request, sip_config_id):
    """Upload a sound file for a specific sound type."""
    try:
        settings_obj = _get_pbx_settings(sip_config_id)
    except SipConfiguration.DoesNotExist:
        return Response({'error': 'SIP configuration not found'}, status=status.HTTP_404_NOT_FOUND)

    sound_type = request.data.get('sound_type')
    file = request.FILES.get('file')

    valid_types = [
        'greeting', 'after_hours', 'queue_hold',
        'voicemail_prompt', 'thank_you', 'transfer_hold',
        'review_prompt', 'review_invalid', 'review_thanks',
        'queue_position_1', 'queue_position_2', 'queue_position_3',
        'queue_position_4', 'queue_position_5', 'queue_position_6',
        'queue_position_7', 'queue_position_8', 'queue_position_9',
        'queue_position_10',
    ]
    if sound_type not in valid_types:
        return Response(
            {'error': f'Invalid sound_type. Must be one of: {", ".join(valid_types)}'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not file:
        return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

    allowed_extensions = ('.wav', '.mp3', '.ogg')
    if not file.name.lower().endswith(allowed_extensions):
        return Response(
            {'error': f'File must be {", ".join(allowed_extensions)}'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if file.size > 10 * 1024 * 1024:
        return Response({'error': 'File size must be under 10MB'}, status=status.HTTP_400_BAD_REQUEST)

    field_name = f'sound_{sound_type}'
    old_file = getattr(settings_obj, field_name)
    if old_file and old_file.name:
        old_file.delete(save=False)

    setattr(settings_obj, field_name, file)
    settings_obj.save(update_fields=[field_name, 'updated_at'])

    return Response(PbxSettingsSerializer(settings_obj).data)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def pbx_settings_remove_sound(request, sip_config_id):
    """Remove a custom sound (revert to default)."""
    try:
        settings_obj = _get_pbx_settings(sip_config_id)
    except SipConfiguration.DoesNotExist:
        return Response({'error': 'SIP configuration not found'}, status=status.HTTP_404_NOT_FOUND)

    sound_type = request.data.get('sound_type')
    valid_types = [
        'greeting', 'after_hours', 'queue_hold',
        'voicemail_prompt', 'thank_you', 'transfer_hold',
        'review_prompt', 'review_invalid', 'review_thanks',
        'queue_position_1', 'queue_position_2', 'queue_position_3',
        'queue_position_4', 'queue_position_5', 'queue_position_6',
        'queue_position_7', 'queue_position_8', 'queue_position_9',
        'queue_position_10',
    ]
    if sound_type not in valid_types:
        return Response({'error': 'Invalid sound_type'}, status=status.HTTP_400_BAD_REQUEST)

    field_name = f'sound_{sound_type}'
    old_file = getattr(settings_obj, field_name)
    if old_file and old_file.name:
        old_file.delete(save=False)
    setattr(settings_obj, field_name, None)
    settings_obj.save(update_fields=[field_name, 'updated_at'])

    return Response(PbxSettingsSerializer(settings_obj).data)


# ============================================================================
# PBX CALL ROUTING API (called by Asterisk AGI)
# ============================================================================


_PBX_TOKEN_CACHE_PREFIX = "pbx_token_tenant:"
_PBX_TOKEN_CACHE_TTL = 60 * 10  # 10 minutes


def _resolve_tenant_by_pbx_token(token: str):
    """Return the tenant schema_name bound to a PbxServer ``enrollment_token``.

    PbxServer rows live in tenant schemas, so we have to iterate the
    registry until we find a match. Results are cached in Django's cache
    keyed by the token so subsequent AGI hits hit the cache (critical on
    high call volumes — every inbound ring fires this endpoint).

    Returns ``None`` if no active PbxServer matches the token.
    """
    from django.core.cache import cache
    from tenant_schemas.utils import schema_context
    from tenants.models import Tenant

    if not token:
        return None

    cache_key = f"{_PBX_TOKEN_CACHE_PREFIX}{token}"
    cached = cache.get(cache_key)
    if cached:
        return cached  # schema_name string

    try:
        tenants = list(Tenant.objects.exclude(schema_name='public'))
    except Exception:  # noqa: BLE001
        return None

    for tenant in tenants:
        try:
            with schema_context(tenant.schema_name):
                from crm.models import PbxServer
                pbx = PbxServer.objects.filter(enrollment_token=token).first()
                if pbx is None:
                    continue
                cache.set(cache_key, tenant.schema_name, _PBX_TOKEN_CACHE_TTL)
                return tenant.schema_name
        except Exception:  # noqa: BLE001
            continue
    return None


def _resolve_routing_for_tenant(tenant_schema: str, did: str):
    """Run the DID → InboundRoute resolution inside ``tenant_schema``.

    Returns a response dict ready to return from :func:`call_routing`, or
    ``None`` if no SIP config / routing data was found in this tenant.
    """
    from django.conf import settings as django_settings
    from tenant_schemas.utils import schema_context

    clean_did = did.replace('+', '').replace(' ', '').replace('-', '')

    with schema_context(tenant_schema):
        from crm.models import InboundRoute, Queue, Trunk

        # Try matching by phone number first, then fall back to default config
        sip_config = SipConfiguration.objects.filter(
            phone_number__endswith=clean_did[-7:]
        ).first()
        if not sip_config:
            sip_config = SipConfiguration.objects.filter(is_default=True).first()
        if not sip_config:
            sip_config = SipConfiguration.objects.first()
        if not sip_config:
            return None

        pbx_settings, _ = PbxSettings.objects.get_or_create(
            sip_configuration=sip_config
        )

        is_working = pbx_settings.is_working_hours_now()
        sound_urls = pbx_settings.get_sound_urls()

        # Resolve InboundRoute for this DID (exact, then any route whose
        # trunk owns this DID). When matched we can override extensions
        # and action with the route's configured destination.
        route = InboundRoute.objects.filter(
            did=did, is_active=True
        ).first() or InboundRoute.objects.filter(
            did__endswith=clean_did[-9:], is_active=True
        ).first()
        if not route:
            trunk_for_did = Trunk.objects.filter(
                phone_numbers__contains=[did], is_active=True
            ).first()
            if trunk_for_did:
                route = InboundRoute.objects.filter(
                    trunk=trunk_for_did, is_active=True
                ).order_by('priority').first()

        inbound_route_info = None
        queue_name = None
        queue_slug = None
        destination_extensions = []
        if route:
            dest_type = str(route.destination_type or 'queue')
            inbound_route_info = {
                'id': route.id,
                'did': route.did,
                'destination_type': dest_type,
                'priority': route.priority,
            }
            if dest_type == 'queue' and route.destination_queue_id:
                queue = Queue.objects.filter(
                    id=route.destination_queue_id, is_active=True
                ).select_related('group').first()
                if queue:
                    # Asterisk queue name. For BYO PbxServers with
                    # use_tenant_prefix=False (isolated DB), the slug is
                    # used verbatim. For legacy shared-DB deployments
                    # (use_tenant_prefix=True) we prepend the schema.
                    from crm.asterisk_db import get_active_pbx_for_current_tenant
                    pbx = get_active_pbx_for_current_tenant()
                    use_prefix = bool(
                        pbx is not None and pbx.use_tenant_prefix
                    )
                    queue_slug = queue.slug
                    queue_name = (
                        f"{tenant_schema}_{queue.slug}" if use_prefix else queue.slug
                    )
                    inbound_route_info['queue_slug'] = queue.slug
                    inbound_route_info['queue_name'] = queue_name
                    # Derive ringing extensions from the backing group.
                    destination_extensions = list(
                        UserPhoneAssignment.objects.filter(
                            user__tenant_groups=queue.group,
                            is_active=True,
                        ).values_list('extension', flat=True).distinct()
                    )
            elif dest_type == 'extension' and route.destination_extension_id:
                assignment = UserPhoneAssignment.objects.filter(
                    id=route.destination_extension_id, is_active=True
                ).first()
                if assignment:
                    destination_extensions = [assignment.extension]
                    inbound_route_info['extension'] = assignment.extension
            elif dest_type == 'ivr_custom':
                inbound_route_info['ivr_custom_context'] = route.ivr_custom_context
            elif dest_type in ('voicemail', 'hangup'):
                pass  # handled below by action

        # Legacy: if no InboundRoute matched, fall back to all active
        # extensions on the SIP config (prior behavior).
        fallback_extensions = list(
            UserPhoneAssignment.objects.filter(
                sip_configuration=sip_config, is_active=True
            ).values_list('extension', flat=True)
        )
        extensions = destination_extensions or fallback_extensions

        # Determine action: InboundRoute overrides, else working-hours gate
        if route and not is_working:
            route_action = 'after_hours'
        elif route and route.destination_type:
            route_action = str(route.destination_type)
        else:
            route_action = 'queue' if is_working else 'after_hours'

        return {
            'is_working_hours': is_working,
            'action': route_action,
            'sounds': sound_urls,
            'extensions': extensions,
            'voicemail_enabled': pbx_settings.voicemail_enabled,
            'after_hours_action': pbx_settings.after_hours_action,
            'forward_number': (
                pbx_settings.forward_number
                if pbx_settings.after_hours_action == 'forward' else None
            ),
            'review_method': pbx_settings.review_method,
            'inbound_route': inbound_route_info,
            'queue_name': queue_name,
            'queue_slug': queue_slug,
            'tenant_schema': tenant_schema,
        }


@csrf_exempt
@api_view(['GET'])
@permission_classes([])
def call_routing(request):
    """
    Lightweight endpoint for Asterisk to query on each incoming call.
    Returns working hours status, routing action, and sound URLs.

    Query params:
        did: The DID/phone number that was called (e.g., +995322421219)
        pbx_token: (optional) Tenant-scoping token; fallback for the header.

    Headers:
        X-PBX-Token: (optional) When set, resolves tenant via
            ``PbxServer.enrollment_token`` for O(1) routing — Asterisk's
            install script bakes this in so we don't have to iterate every
            tenant on every call.
        Authorization: Bearer <PBX_SHARED_SECRET> — legacy shared secret,
            still accepted for backwards compat during the BYO migration.
    """
    from django.conf import settings as django_settings
    from tenants.models import Tenant

    # Authenticate via shared secret (legacy). When X-PBX-Token is present
    # we treat the token itself as sufficient auth — the token is a 64+ char
    # secret minted by EchoDesk at enrollment time.
    pbx_token = (
        request.META.get('HTTP_X_PBX_TOKEN', '')
        or request.GET.get('pbx_token', '')
    ).strip()

    auth_header = request.META.get('HTTP_AUTHORIZATION', '')
    expected_shared_secret = getattr(django_settings, 'PBX_SHARED_SECRET', '')
    if not pbx_token:
        # Legacy path: require the shared secret.
        if expected_shared_secret and not auth_header.endswith(expected_shared_secret):
            return Response(
                {'error': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED
            )

    did = request.GET.get('did', '').strip()
    if not did:
        return Response({'error': 'did parameter required'}, status=status.HTTP_400_BAD_REQUEST)

    # ------------------------------------------------------------------
    # Fast path: X-PBX-Token identifies the tenant without scanning all.
    # ------------------------------------------------------------------
    if pbx_token:
        tenant_schema = _resolve_tenant_by_pbx_token(pbx_token)
        if tenant_schema is None:
            return Response(
                {'error': 'Unknown X-PBX-Token'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        try:
            resolved = _resolve_routing_for_tenant(tenant_schema, did)
        except Exception:  # noqa: BLE001
            logger.exception(
                "call_routing: resolution failed for tenant=%s did=%s",
                tenant_schema, did,
            )
            resolved = None
        if resolved is not None:
            return Response(resolved)
        # Fall through to the legacy scan as a last resort so a
        # mis-wired DID doesn't 404 when the tenant actually exists.

    # ------------------------------------------------------------------
    # Legacy all-tenant scan. Kept for backwards compatibility until every
    # deployed Asterisk install script includes X-PBX-Token. Emits a
    # warning so we notice if we accidentally regress to the slow path.
    # ------------------------------------------------------------------
    logger.warning(
        "call_routing called without X-PBX-Token — O(N) fallback (did=%s)", did,
    )
    tenants = Tenant.objects.exclude(schema_name='public')
    for tenant in tenants:
        try:
            resolved = _resolve_routing_for_tenant(tenant.schema_name, did)
        except Exception:  # noqa: BLE001
            continue
        if resolved is not None:
            return Response(resolved)

    # DID not found in any tenant — default to open
    return Response({
        'is_working_hours': True,
        'action': 'queue',
        'sounds': {},
        'extensions': [],
        'voicemail_enabled': False,
        'after_hours_action': 'announcement',
    })
