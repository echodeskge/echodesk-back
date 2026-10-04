from rest_framework import serializers
from django.contrib.auth import get_user_model, authenticate
from rest_framework.authtoken.models import Token
from .models import Tenant, SavedCard, TenantSubscription, DashboardAppearanceSettings, SecurityLog, TenantIPWhitelist
import re

User = get_user_model()


class TenantSerializer(serializers.ModelSerializer):
    """Serializer for Tenant model"""
    
    class Meta:
        model = Tenant
        fields = (
            'id', 'schema_name', 'domain_url', 'name', 'description', 'admin_email',
            'admin_name', 'plan', 'max_users', 'max_storage', 'preferred_language',
            'frontend_url', 'deployment_status', 'is_active', 'created_on',
            'min_users_per_ticket', 'only_superadmin_can_delete_tickets'
        )
        read_only_fields = ('id', 'schema_name', 'created_on')


class TenantCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating new tenants"""
    
    domain = serializers.CharField(write_only=True, help_text="Subdomain for the tenant (e.g., 'acme' for acme.echodesk.ge)")
    
    class Meta:
        model = Tenant
        fields = (
            'name', 'description', 'admin_email', 'admin_name', 
            'plan', 'max_users', 'max_storage', 'preferred_language', 'domain'
        )
    
    def create(self, validated_data):
        domain_name = validated_data.pop('domain')
        schema_name = domain_name.lower().replace('-', '_')
        
        # Create the tenant with domain_url
        tenant = Tenant.objects.create(
            domain_url=f"{domain_name}.echodesk.ge",
            schema_name=schema_name,
            **validated_data
        )
        
        return tenant
    
    def validate_domain(self, value):
        """Validate that the domain is unique and follows naming conventions"""
        import re
        
        # Check format (only lowercase letters, numbers, and hyphens)
        if not re.match(r'^[a-z0-9-]+$', value):
            raise serializers.ValidationError(
                "Domain can only contain lowercase letters, numbers, and hyphens."
            )
        
        # Check if domain already exists
        full_domain = f"{value}.echodesk.ge"
        if Tenant.objects.filter(domain_url=full_domain).exists():
            raise serializers.ValidationError(
                f"Domain '{full_domain}' is already taken."
            )
        
        # Reserved subdomains
        reserved = ['www', 'api', 'admin', 'mail', 'ftp', 'public', 'app', 'book']
        if value.lower() in reserved:
            raise serializers.ValidationError(
                f"'{value}' is a reserved subdomain."
            )
        
        return value


class TenantRegistrationSerializer(serializers.Serializer):
    """
    Public serializer for tenant registration with admin user creation
    """
    # Tenant information
    company_name = serializers.CharField(max_length=100, help_text="Your company or organization name")
    domain = serializers.CharField(max_length=63, help_text="Subdomain for your tenant (e.g., 'acme' for acme.echodesk.ge)")
    description = serializers.CharField(max_length=500, required=False, help_text="Brief description of your organization")

    # Feature-based subscription selection
    feature_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=True,
        help_text="List of feature IDs for subscription"
    )
    pricing_model = serializers.ChoiceField(
        choices=[('agent', 'Agent-based'), ('crm', 'CRM-based')],
        help_text="Pricing model preference"
    )
    agent_count = serializers.IntegerField(
        default=10,
        min_value=10,
        help_text="Number of agents (required for agent-based pricing)"
    )
    
    # Admin user information
    admin_email = serializers.EmailField(help_text="Email address for the admin user")
    admin_password = serializers.CharField(min_length=8, write_only=True, help_text="Password for the admin user")
    admin_first_name = serializers.CharField(max_length=30, help_text="Admin's first name")
    admin_last_name = serializers.CharField(max_length=30, help_text="Admin's last name")
    
    # Language preference
    preferred_language = serializers.ChoiceField(
        choices=[
            ('en', 'English'),
            ('ru', 'Russian'), 
            ('ka', 'Georgian'),
        ],
        default='en',
        help_text="Preferred language for the frontend dashboard"
    )

    def validate(self, attrs):
        """Cross-field validation"""
        from .feature_models import Feature

        feature_ids = attrs.get('feature_ids', [])
        pricing_model = attrs.get('pricing_model')
        agent_count = attrs.get('agent_count', 10)

        # Validate all features exist and are active
        if feature_ids:
            valid_features = Feature.objects.filter(id__in=feature_ids, is_active=True)
            if valid_features.count() != len(feature_ids):
                raise serializers.ValidationError({
                    'feature_ids': 'One or more invalid or inactive features selected'
                })

        # Validate agent count for agent-based pricing
        if pricing_model == 'agent' and agent_count < 10:
            raise serializers.ValidationError("Agent count must be at least 10 for agent-based pricing")

        # For CRM-based pricing, agent_count is not used
        if pricing_model == 'crm':
            attrs['agent_count'] = 10  # Set default for CRM-based

        return attrs
    
    def validate_domain(self, value):
        """Validate that the domain is unique and follows naming conventions"""
        import re
        
        # Check format (only lowercase letters, numbers, and hyphens)
        if not re.match(r'^[a-z0-9-]+$', value):
            raise serializers.ValidationError(
                "Domain can only contain lowercase letters, numbers, and hyphens."
            )
        
        # Check if domain already exists
        full_domain = f"{value}.echodesk.ge"
        if Tenant.objects.filter(domain_url=full_domain).exists():
            raise serializers.ValidationError(
                f"Domain '{full_domain}' is already taken."
            )
        
        # Reserved subdomains
        reserved = ['www', 'api', 'admin', 'mail', 'ftp', 'public', 'app', 'support', 'help', 'book']
        if value.lower() in reserved:
            raise serializers.ValidationError(
                f"'{value}' is a reserved subdomain."
            )
        
        return value
    
    def validate_admin_password(self, value):
        """Validate password strength"""
        import re
        
        if len(value) < 8:
            raise serializers.ValidationError("Password must be at least 8 characters long.")
        
        if not re.search(r'[A-Z]', value):
            raise serializers.ValidationError("Password must contain at least one uppercase letter.")
        
        if not re.search(r'[a-z]', value):
            raise serializers.ValidationError("Password must contain at least one lowercase letter.")
        
        if not re.search(r'\d', value):
            raise serializers.ValidationError("Password must contain at least one number.")
        
        return value


class TenantLoginSerializer(serializers.Serializer):
    """
    Serializer for tenant-specific login
    """
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    
    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')
        
        if email and password:
            # Get the current tenant from the request context
            request = self.context.get('request')
            if not hasattr(request, 'tenant'):
                raise serializers.ValidationError('This endpoint is only available from tenant subdomains')
            
            user = authenticate(request=request, username=email, password=password)
            
            if not user:
                raise serializers.ValidationError('Invalid email or password')
            
            if not user.is_active:
                raise serializers.ValidationError('User account is disabled')
            
            attrs['user'] = user
        else:
            raise serializers.ValidationError('Must include email and password')
        
        return attrs


class TenantDashboardDataSerializer(serializers.Serializer):
    """
    Serializer for tenant dashboard data
    """
    tenant_info = serializers.SerializerMethodField()
    user_info = serializers.SerializerMethodField()
    statistics = serializers.SerializerMethodField()
    
    def get_tenant_info(self, obj):
        """Get tenant information"""
        request = self.context.get('request')
        tenant = request.tenant
        
        return {
            'id': tenant.id,
            'name': tenant.name,
            'description': tenant.description,
            'domain_url': tenant.domain_url,
            'preferred_language': tenant.preferred_language,
            'plan': tenant.plan,
            'frontend_url': tenant.frontend_url,
            'deployment_status': tenant.deployment_status,
            'created_on': tenant.created_on,
            'is_active': tenant.is_active
        }
    
    def get_user_info(self, obj):
        """Get current user information"""
        user = obj
        return {
            'id': user.id,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'is_staff': user.is_staff,
            'is_superuser': user.is_superuser,
            'date_joined': user.date_joined
        }
    
    def get_statistics(self, obj):
        """Get tenant statistics including SIP functionality"""
        from django.contrib.auth import get_user_model
        from tickets.models import Ticket
        from crm.models import Client, CallLog, SipConfiguration
        from django.utils import timezone
        
        User = get_user_model()
        
        try:
            # Count users in current tenant
            total_users = User.objects.count()
            active_users = User.objects.filter(is_active=True).count()
            
            # Count tickets if tickets app exists
            try:
                total_tickets = Ticket.objects.count()
                open_tickets = Ticket.objects.filter(status__in=['open', 'in_progress']).count()
            except:
                total_tickets = 0
                open_tickets = 0
            
            # Count clients if CRM app exists
            try:
                total_clients = Client.objects.count()
                active_clients = Client.objects.filter(is_active=True).count()
            except:
                total_clients = 0
                active_clients = 0
            
            # SIP and call statistics
            try:
                # SIP configurations
                sip_configs = SipConfiguration.objects.filter(created_by__tenant=self.context['request'].tenant)
                active_sip_configs = sip_configs.filter(is_active=True)
                default_sip_config = sip_configs.filter(is_default=True, is_active=True).first()
                
                # Call statistics (today)
                today = timezone.now().date()
                calls_today = CallLog.objects.filter(started_at__date=today)
                
                # Call statistics (this week)
                week_start = timezone.now().date() - timezone.timedelta(days=7)
                calls_week = CallLog.objects.filter(started_at__date__gte=week_start)
                
                sip_stats = {
                    'configurations': {
                        'total': sip_configs.count(),
                        'active': active_sip_configs.count(),
                        'has_default': default_sip_config is not None
                    },
                    'calls_today': {
                        'total': calls_today.count(),
                        'answered': calls_today.filter(status='answered').count(),
                        'missed': calls_today.filter(status='missed').count(),
                        'inbound': calls_today.filter(direction='inbound').count(),
                        'outbound': calls_today.filter(direction='outbound').count()
                    },
                    'calls_week': {
                        'total': calls_week.count(),
                        'answered': calls_week.filter(status='answered').count(),
                        'missed': calls_week.filter(status='missed').count()
                    },
                    'default_config': {
                        'id': default_sip_config.id if default_sip_config else None,
                        'name': default_sip_config.name if default_sip_config else None,
                        'server': default_sip_config.sip_server if default_sip_config else None
                    } if default_sip_config else None
                }
            except:
                sip_stats = {
                    'configurations': {'total': 0, 'active': 0, 'has_default': False},
                    'calls_today': {'total': 0, 'answered': 0, 'missed': 0, 'inbound': 0, 'outbound': 0},
                    'calls_week': {'total': 0, 'answered': 0, 'missed': 0},
                    'default_config': None
                }
            
            return {
                'users': {
                    'total': total_users,
                    'active': active_users
                },
                'tickets': {
                    'total': total_tickets,
                    'open': open_tickets
                },
                'clients': {
                    'total': total_clients,
                    'active': active_clients
                },
                'sip': sip_stats
            }
        except Exception as e:
            return {
                'error': f'Could not fetch statistics: {str(e)}'
            }


class SavedCardSerializer(serializers.ModelSerializer):
    """Serializer for SavedCard model - shows masked card details"""

    class Meta:
        model = SavedCard
        fields = (
            'id', 'card_type', 'masked_card_number', 'card_expiry',
            'saved_at', 'is_active', 'is_default', 'card_save_type'
        )
        read_only_fields = ('id', 'card_type', 'masked_card_number', 'card_expiry', 'saved_at', 'card_save_type')


class TenantSubscriptionSerializer(serializers.ModelSerializer):
    """Serializer for TenantSubscription model"""

    class Meta:
        model = TenantSubscription
        fields = (
            'id', 'tenant', 'is_active', 'starts_at', 'expires_at',
            'current_users', 'whatsapp_messages_used', 'storage_used_gb',
            'last_billed_at', 'next_billing_date', 'is_trial', 'trial_ends_at',
            'subscription_type', 'agent_count',
            'created_at', 'updated_at'
        )
        read_only_fields = (
            'id', 'tenant', 'starts_at', 'current_users', 'whatsapp_messages_used',
            'storage_used_gb', 'created_at', 'updated_at'
        )


class DashboardAppearanceSettingsSerializer(serializers.ModelSerializer):
    """Serializer for dashboard appearance settings"""

    class Meta:
        model = DashboardAppearanceSettings
        fields = (
            'primary_color', 'primary_color_dark', 'secondary_color',
            'accent_color', 'sidebar_background', 'sidebar_primary',
            'border_radius', 'sidebar_order', 'updated_at'
        )
        read_only_fields = ('updated_at',)

    def validate_border_radius(self, value):
        """Validate border radius format"""
        valid_values = ['0', '0.3rem', '0.5rem', '0.75rem', '1rem']
        if value not in valid_values:
            raise serializers.ValidationError(
                f"Border radius must be one of: {', '.join(valid_values)}"
            )
        return value

    def validate(self, attrs):
        """Validate HSL color format for all color fields"""
        hsl_pattern = r'^\d{1,3}(\.\d+)?\s+\d{1,3}(\.\d+)?%\s+\d{1,3}(\.\d+)?%$'

        color_fields = [
            'primary_color', 'primary_color_dark', 'secondary_color',
            'accent_color', 'sidebar_background', 'sidebar_primary'
        ]

        for field in color_fields:
            if field in attrs:
                value = attrs[field].strip()
                if not re.match(hsl_pattern, value):
                    raise serializers.ValidationError({
                        field: f"Invalid HSL format. Expected format: 'H S% L%' (e.g., '240 5.9% 10%')"
                    })
                attrs[field] = value

        return attrs


# ============================================================
# Security Serializers
# ============================================================

class SecurityLogSerializer(serializers.ModelSerializer):
    """Serializer for SecurityLog model - read-only for viewing logs"""
    user_email = serializers.SerializerMethodField()
    event_type_display = serializers.CharField(source='get_event_type_display', read_only=True)
    device_type_display = serializers.CharField(source='get_device_type_display', read_only=True)

    class Meta:
        model = SecurityLog
        fields = (
            'id', 'user_id', 'user_email', 'attempted_email', 'event_type', 'event_type_display',
            'ip_address', 'user_agent', 'device_type', 'device_type_display',
            'browser', 'operating_system', 'city', 'country', 'country_code',
            'failure_reason', 'created_at'
        )
        read_only_fields = fields

    def get_user_email(self, obj):
        """Get user email - primarily from attempted_email since we store it on login"""
        # attempted_email is always populated on login, so use that
        return obj.attempted_email or None


class TenantIPWhitelistSerializer(serializers.ModelSerializer):
    """Serializer for TenantIPWhitelist model"""
    created_by_email = serializers.SerializerMethodField()

    class Meta:
        model = TenantIPWhitelist
        fields = (
            'id', 'ip_address', 'cidr_notation', 'description',
            'is_active', 'created_by', 'created_by_email', 'created_at', 'updated_at'
        )
        read_only_fields = ('id', 'created_by', 'created_by_email', 'created_at', 'updated_at')

    def get_created_by_email(self, obj):
        """Get creator email if exists"""
        if obj.created_by:
            return obj.created_by.email
        return None

    def validate_ip_address(self, value):
        """Validate IP address format"""
        import ipaddress
        try:
            ipaddress.ip_address(value)
        except ValueError:
            raise serializers.ValidationError("Invalid IP address format")
        return value

    def validate_cidr_notation(self, value):
        """Validate CIDR notation if provided"""
        if not value:
            return value

        # CIDR should be just the prefix length (e.g., "24" not "/24")
        value = value.strip().lstrip('/')

        try:
            prefix_length = int(value)
            if prefix_length < 0 or prefix_length > 128:
                raise serializers.ValidationError("CIDR prefix must be between 0 and 128")
        except ValueError:
            raise serializers.ValidationError("CIDR notation must be a number (e.g., '24' for /24)")

        return value

    def validate(self, attrs):
        """Validate IP address and CIDR combination"""
        import ipaddress

        ip_address = attrs.get('ip_address')
        cidr_notation = attrs.get('cidr_notation', '')

        if ip_address and cidr_notation:
            try:
                # Validate the network address
                ipaddress.ip_network(f"{ip_address}/{cidr_notation}", strict=False)
            except ValueError as e:
                raise serializers.ValidationError({
                    'cidr_notation': f"Invalid IP/CIDR combination: {str(e)}"
                })

        return attrs
