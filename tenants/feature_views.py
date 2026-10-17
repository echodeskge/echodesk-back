"""
API views for Feature and Permission management
"""

from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes as drf_permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework import serializers
from drf_spectacular.utils import extend_schema, inline_serializer, OpenApiResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from .models import (
    Feature, Permission, TenantFeature, TenantPermission, TenantSubscription,
    SavedCard, PaymentOrder, Invoice
)
from .feature_serializers import (
    FeatureSerializer, PermissionSerializer,
    TenantFeatureSerializer, TenantPermissionSerializer,
    FeatureCheckSerializer
)
from .subscription_service import SubscriptionService
from .bog_payment import bog_service
from .security_views import IsStaffOrSuperUser
import logging
import uuid

logger = logging.getLogger(__name__)


class FeatureViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for viewing available features

    list: Get all active features
    retrieve: Get a specific feature with its permissions
    """
    queryset = Feature.objects.filter(is_active=True)
    serializer_class = FeatureSerializer
    permission_classes = [AllowAny]  # Public endpoint for pricing calculator and registration

    def get_queryset(self):
        """Filter features by category if provided"""
        from django.db.models import Prefetch
        from .feature_models import FeaturePermission

        # Prefetch permissions with their content_type to avoid N+1 queries
        queryset = super().get_queryset().prefetch_related(
            Prefetch(
                'permissions',
                queryset=FeaturePermission.objects.select_related(
                    'permission__content_type'
                )
            )
        )
        category = self.request.query_params.get('category', None)
        if category:
            queryset = queryset.filter(category=category)
        return queryset

    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)


class PermissionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for viewing Django's built-in permissions

    list: Get all permissions
    retrieve: Get a specific permission
    """
    queryset = Permission.objects.all().select_related('content_type').order_by('content_type__app_label', 'codename')
    serializer_class = PermissionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Filter permissions by app_label if provided"""
        queryset = super().get_queryset()
        app_label = self.request.query_params.get('app_label', None)
        if app_label:
            queryset = queryset.filter(content_type__app_label=app_label)
        return queryset


class TenantFeatureViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for viewing tenant's enabled features

    list: Get all features enabled for current tenant
    retrieve: Get details of a specific tenant feature
    check: Check if tenant has a specific feature
    """
    serializer_class = TenantFeatureSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Get features for current tenant"""
        if not hasattr(self.request, 'tenant'):
            return TenantFeature.objects.none()

        return TenantFeature.objects.filter(
            tenant=self.request.tenant,
            is_active=True
        ).select_related('feature')

    @action(detail=False, methods=['post'])
    def check(self, request):
        """
        Check if tenant has a specific feature

        POST /api/tenant-features/check/
        Body: {"feature_key": "whatsapp_integration"}
        """
        serializer = FeatureCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        feature_key = serializer.validated_data['feature_key']
        is_available = SubscriptionService.check_tenant_feature(
            request.tenant, feature_key
        )

        return Response({
            'feature_key': feature_key,
            'is_available': is_available
        })


class TenantPermissionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for viewing tenant's available permissions

    list: Get all permissions available to current tenant
    retrieve: Get details of a specific tenant permission
    """
    serializer_class = TenantPermissionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Get permissions for current tenant"""
        if not hasattr(self.request, 'tenant'):
            return TenantPermission.objects.none()

        return TenantPermission.objects.filter(
            tenant=self.request.tenant,
            is_active=True
        ).select_related('permission', 'granted_by_feature')


# UserPermission ViewSet removed
# User permissions are managed via the existing User model fields (can_view_all_tickets, etc.)
# Tenant admins grant these permissions through the users app
# TenantPermission (above) shows which permissions are available to grant based on package features


# Subscription changes move money (prorated charges) and change what every
# user can use, so they are limited to staff / superusers -- the same people
# the dashboard shows the Subscription settings page to.
BILLING_PERMISSIONS = [IsAuthenticated, IsStaffOrSuperUser]

_ERROR = inline_serializer(name='SubscriptionErrorResponse', fields={'error': serializers.CharField()})
_FORBIDDEN = OpenApiResponse(description='Not staff/superuser')

_FEATURE_REF = inline_serializer(
    name='SubscriptionFeatureRef',
    fields={
        'id': serializers.IntegerField(),
        'key': serializers.CharField(),
        'name': serializers.CharField(),
        'price_per_user_gel': serializers.FloatField(required=False),
    },
)


@extend_schema(
    operation_id='subscription_features_add_create',
    summary='Add a feature to the subscription',
    description=(
        'Staff or superuser only. When a prorated charge is due (and `charge_immediately` '
        'is not false) nothing changes yet: the response has `requires_payment: true` and a '
        '`payment_url` to complete. Otherwise the feature is added at once (`success: true`).'
    ),
    request=inline_serializer(
        name='AddSubscriptionFeature',
        fields={
            'feature_id': serializers.IntegerField(),
            'charge_immediately': serializers.BooleanField(required=False, default=True),
        },
    ),
    responses={
        200: inline_serializer(
            name='AddSubscriptionFeatureResponse',
            fields={
                'success': serializers.BooleanField(),
                'message': serializers.CharField(),
                'requires_payment': serializers.BooleanField(required=False),
                'payment_url': serializers.URLField(required=False),
                'feature': _FEATURE_REF,
                'prorated_cost': serializers.FloatField(),
                'new_monthly_cost': serializers.FloatField(required=False),
                'days_remaining': serializers.IntegerField(),
            },
        ),
        400: _ERROR, 403: _FORBIDDEN, 404: _ERROR, 500: _ERROR,
    },
    tags=['Subscription'],
)
@api_view(['POST'])
@drf_permission_classes(BILLING_PERMISSIONS)
def add_feature_to_subscription(request):
    """
    Add a feature to the tenant's subscription

    POST /api/subscription/features/add/
    Body: {"feature_id": 1, "charge_immediately": true}

    This will:
    1. Calculate prorated cost for the feature
    2. Charge the ecommerce saved card (if available and charge_immediately=true)
    3. Add the feature to selected_features
    4. Update monthly_cost
    """
    feature_id = request.data.get('feature_id')
    charge_immediately = request.data.get('charge_immediately', True)

    if not feature_id:
        return Response(
            {'error': 'feature_id is required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'No tenant found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Get the feature
    try:
        feature = Feature.objects.get(id=feature_id, is_active=True)
    except Feature.DoesNotExist:
        return Response(
            {'error': 'Feature not found or inactive'},
            status=status.HTTP_404_NOT_FOUND
        )

    # Get tenant subscription
    subscription = TenantSubscription.objects.filter(
        tenant=request.tenant,
        is_active=True
    ).first()

    if not subscription:
        return Response(
            {'error': 'No active subscription found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Check if feature is already added
    if subscription.selected_features.filter(id=feature_id).exists():
        return Response(
            {'error': 'Feature already added to subscription'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Calculate prorated cost for remaining days in billing cycle
    today = timezone.now().date()
    if subscription.next_billing_date:
        # Handle both datetime and date types
        next_billing = subscription.next_billing_date
        if hasattr(next_billing, 'date'):
            next_billing = next_billing.date()
        days_remaining = (next_billing - today).days
    else:
        days_remaining = 30

    monthly_feature_cost = feature.price_per_user_gel * subscription.agent_count
    prorated_cost = (Decimal(days_remaining) / Decimal(30)) * monthly_feature_cost

    # Always redirect user to payment page for 3DS authentication
    # Even with saved card, user must complete payment flow
    payment_result = None
    payment_required = prorated_cost > 0 and charge_immediately

    if payment_required:
        # Create a payment session for the prorated amount
        # User will be redirected to complete payment (with 3DS if needed)
        try:
            from django.conf import settings as django_settings
            api_host = request.get_host()
            frontend_host = api_host.replace('api.', '')
            frontend_url = f"https://{frontend_host}"

            api_domain = django_settings.API_DOMAIN
            if not api_domain.startswith('http'):
                api_domain = f"https://{api_domain}"
            callback_url = f"{api_domain}/api/payments/webhook/"

            external_order_id = f"FEAT-{uuid.uuid4().hex[:12].upper()}"

            # Check if we should save card for future use
            has_ecommerce_card = SavedCard.objects.filter(
                tenant=request.tenant,
                is_active=True,
                card_save_type='ecommerce'
            ).exists()

            payment_response = bog_service.create_payment(
                amount=float(prorated_cost),
                currency='GEL',
                description=f'Add feature: {feature.name}',
                customer_email=request.tenant.admin_email,
                customer_name=request.tenant.admin_name,
                return_url_success=f"{frontend_url}/settings/subscription?feature_added=success",
                return_url_fail=f"{frontend_url}/settings/subscription?feature_added=failed",
                callback_url=callback_url,
                external_order_id=external_order_id,
            )

            # Enable BOTH card saving types:
            # 1. Ecommerce card - for variable amount charges (future feature additions)
            # 2. Subscription card - for fixed recurring charges (monthly payments)
            bog_service.enable_card_saving(payment_response['order_id'])  # Ecommerce card
            bog_service.enable_subscription_card_saving(payment_response['order_id'])  # Subscription card

            # Create payment order
            PaymentOrder.objects.create(
                order_id=external_order_id,
                bog_order_id=payment_response['order_id'],
                tenant=request.tenant,
                package=None,
                amount=float(prorated_cost),
                currency='GEL',
                agent_count=subscription.agent_count,
                payment_url=payment_response['payment_url'],
                status='pending',
                metadata={
                    'type': 'feature_addition',
                    'feature_id': feature.id,
                    'feature_key': feature.key,
                    'prorated_cost': float(prorated_cost),
                    'save_both_cards': True  # Save both ecommerce and subscription cards
                }
            )

            logger.info(f'Feature addition payment created for tenant {request.tenant.schema_name}: {prorated_cost} GEL')

            return Response({
                'success': False,
                'requires_payment': True,
                'message': 'Payment required to add this feature',
                'payment_url': payment_response['payment_url'],
                'feature': {
                    'id': feature.id,
                    'key': feature.key,
                    'name': feature.name,
                    'price_per_user_gel': float(feature.price_per_user_gel),
                },
                'prorated_cost': float(prorated_cost),
                'days_remaining': days_remaining,
            })

        except Exception as e:
            logger.error(f'Failed to create payment for feature addition: {e}')
            return Response(
                {'error': f'Failed to create payment: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    # If no payment required (prorated_cost is 0), add feature directly
    # Add feature to subscription
    subscription.selected_features.add(feature)

    # Recalculate monthly cost from all selected features
    total_feature_cost = sum(
        f.price_per_user_gel for f in subscription.selected_features.all()
    )
    new_monthly_cost = total_feature_cost * subscription.agent_count
    subscription.save()

    response_data = {
        'success': True,
        'message': f'Feature "{feature.name}" added successfully',
        'feature': {
            'id': feature.id,
            'key': feature.key,
            'name': feature.name,
            'price_per_user_gel': float(feature.price_per_user_gel),
        },
        'prorated_cost': float(prorated_cost),
        'new_monthly_cost': float(new_monthly_cost),
        'days_remaining': days_remaining,
    }

    if payment_result:
        response_data['payment_charged'] = True
        response_data['payment_order_id'] = payment_result['order_id']

    return Response(response_data)


@extend_schema(
    operation_id='subscription_features_remove_create',
    summary='Remove a feature from the subscription',
    description='Staff or superuser only. Core features cannot be removed.',
    request=inline_serializer(
        name='RemoveSubscriptionFeature',
        fields={'feature_id': serializers.IntegerField()},
    ),
    responses={
        200: inline_serializer(
            name='RemoveSubscriptionFeatureResponse',
            fields={
                'success': serializers.BooleanField(),
                'message': serializers.CharField(),
                'feature': _FEATURE_REF,
                'cost_reduction': serializers.FloatField(),
                'new_monthly_cost': serializers.FloatField(),
            },
        ),
        400: _ERROR, 403: _FORBIDDEN, 404: _ERROR,
    },
    tags=['Subscription'],
)
@api_view(['POST'])
@drf_permission_classes(BILLING_PERMISSIONS)
def remove_feature_from_subscription(request):
    """
    Remove a feature from the tenant's subscription

    POST /api/subscription/features/remove/
    Body: {"feature_id": 1}

    Note: Core features cannot be removed
    """
    feature_id = request.data.get('feature_id')
    if not feature_id:
        return Response(
            {'error': 'feature_id is required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'No tenant found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Get the feature
    try:
        feature = Feature.objects.get(id=feature_id)
    except Feature.DoesNotExist:
        return Response(
            {'error': 'Feature not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    # Check if feature is a core feature (cannot be removed)
    if feature.category == 'core':
        return Response(
            {'error': 'Core features cannot be removed'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Get tenant subscription
    subscription = TenantSubscription.objects.filter(
        tenant=request.tenant,
        is_active=True
    ).first()

    if not subscription:
        return Response(
            {'error': 'No active subscription found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Check if feature is actually in subscription
    if not subscription.selected_features.filter(id=feature_id).exists():
        return Response(
            {'error': 'Feature not in current subscription'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Calculate cost reduction
    monthly_feature_cost = feature.price_per_user_gel * subscription.agent_count

    # Remove feature from subscription
    subscription.selected_features.remove(feature)

    # monthly_cost is a computed property, no need to set it
    subscription.save()

    return Response({
        'success': True,
        'message': f'Feature "{feature.name}" removed successfully',
        'feature': {
            'id': feature.id,
            'key': feature.key,
            'name': feature.name,
        },
        'cost_reduction': float(monthly_feature_cost),
        'new_monthly_cost': float(subscription.monthly_cost),
    })


@extend_schema(
    operation_id='subscription_agent_count_update',
    summary='Change the subscription agent count',
    description='Staff or superuser only.',
    request=inline_serializer(
        name='UpdateAgentCount',
        fields={'agent_count': serializers.IntegerField(min_value=1)},
    ),
    responses={
        200: inline_serializer(
            name='UpdateAgentCountResponse',
            fields={
                'success': serializers.BooleanField(),
                'message': serializers.CharField(),
                'old_agent_count': serializers.IntegerField(),
                'new_agent_count': serializers.IntegerField(),
                'old_monthly_cost': serializers.FloatField(),
                'new_monthly_cost': serializers.FloatField(),
                'prorated_difference': serializers.FloatField(),
                'days_remaining': serializers.IntegerField(),
            },
        ),
        400: _ERROR, 403: _FORBIDDEN,
    },
    tags=['Subscription'],
)
@api_view(['PUT'])
@drf_permission_classes(BILLING_PERMISSIONS)
def update_agent_count(request):
    """
    Update the agent count for the subscription

    PUT /api/subscription/agent-count/
    Body: {"agent_count": 15}

    This recalculates the monthly cost based on new agent count
    """
    new_agent_count = request.data.get('agent_count')
    if not new_agent_count or not isinstance(new_agent_count, int) or new_agent_count < 1:
        return Response(
            {'error': 'Valid agent_count (positive integer) is required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'No tenant found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Get tenant subscription
    subscription = TenantSubscription.objects.filter(
        tenant=request.tenant,
        is_active=True
    ).first()

    if not subscription:
        return Response(
            {'error': 'No active subscription found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    old_agent_count = subscription.agent_count
    old_monthly_cost = subscription.monthly_cost

    # Calculate new monthly cost
    total_feature_cost = sum(
        feature.price_per_user_gel for feature in subscription.selected_features.all()
    )
    new_monthly_cost = total_feature_cost * new_agent_count

    # Calculate prorated difference
    today = timezone.now().date()
    if subscription.next_billing_date:
        days_remaining = (subscription.next_billing_date - today).days
    else:
        days_remaining = 30

    cost_difference = new_monthly_cost - old_monthly_cost
    prorated_difference = (Decimal(days_remaining) / Decimal(30)) * cost_difference

    # Update subscription (monthly_cost is a computed property)
    subscription.agent_count = new_agent_count
    subscription.save()

    return Response({
        'success': True,
        'message': f'Agent count updated from {old_agent_count} to {new_agent_count}',
        'old_agent_count': old_agent_count,
        'new_agent_count': new_agent_count,
        'old_monthly_cost': float(old_monthly_cost),
        'new_monthly_cost': float(new_monthly_cost),
        'prorated_difference': float(prorated_difference),
        'days_remaining': days_remaining,
    })


@extend_schema(
    operation_id='subscription_features_available_retrieve',
    summary='Features available to add to the subscription',
    description=(
        'Staff or superuser only. `features_by_category` maps a category display name '
        'to its features, with pricing for the current agent count.'
    ),
    responses={
        200: inline_serializer(
            name='SubscriptionAvailableFeatures',
            fields={
                'agent_count': serializers.IntegerField(),
                'features_by_category': serializers.DictField(
                    child=inline_serializer(
                        name='SubscriptionAvailableFeature', many=True,
                        fields={
                            'id': serializers.IntegerField(),
                            'key': serializers.CharField(),
                            'name': serializers.CharField(),
                            'description': serializers.CharField(),
                            'price_per_user_gel': serializers.FloatField(),
                            'price_unlimited_gel': serializers.FloatField(allow_null=True),
                            'monthly_cost_for_current_agents': serializers.FloatField(),
                            'is_selected': serializers.BooleanField(),
                            'icon': serializers.CharField(),
                        },
                    ),
                ),
            },
        ),
        400: _ERROR, 403: _FORBIDDEN,
    },
    tags=['Subscription'],
)
@api_view(['GET'])
@drf_permission_classes(BILLING_PERMISSIONS)
def get_available_features(request):
    """
    Get all available features that can be added to subscription

    GET /api/subscription/features/available/

    Returns features grouped by category with pricing info
    """
    if not hasattr(request, 'tenant'):
        return Response(
            {'error': 'No tenant found'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Get tenant subscription
    subscription = TenantSubscription.objects.filter(
        tenant=request.tenant,
        is_active=True
    ).first()

    current_feature_ids = []
    agent_count = 1
    if subscription:
        current_feature_ids = list(subscription.selected_features.values_list('id', flat=True))
        agent_count = subscription.agent_count

    # Get all active features
    features = Feature.objects.filter(is_active=True).order_by('category', 'sort_order', 'name')

    # Group by category
    categorized_features = {}
    for feature in features:
        category = feature.get_category_display()
        if category not in categorized_features:
            categorized_features[category] = []

        categorized_features[category].append({
            'id': feature.id,
            'key': feature.key,
            'name': feature.name,
            'description': feature.description,
            'price_per_user_gel': float(feature.price_per_user_gel),
            'price_unlimited_gel': float(feature.price_unlimited_gel) if feature.price_unlimited_gel else None,
            'monthly_cost_for_current_agents': float(feature.price_per_user_gel * agent_count),
            'is_selected': feature.id in current_feature_ids,
            'icon': feature.icon,
        })

    return Response({
        'agent_count': agent_count,
        'features_by_category': categorized_features,
    })
