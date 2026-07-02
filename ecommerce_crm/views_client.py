"""
Client-facing ecommerce API endpoints

These ViewSets are designed for ecommerce clients (customers) to access their own data.
Uses EcommerceClientJWTAuthentication for client-specific access control.
"""
import requests
from rest_framework import viewsets, filters, status, serializers
from rest_framework.decorators import action, api_view, permission_classes, authentication_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiParameter, OpenApiExample, extend_schema_view, inline_serializer
from drf_spectacular.openapi import AutoSchema
from django_filters.rest_framework import DjangoFilterBackend
from django.views.decorators.cache import cache_page
from django.utils.decorators import method_decorator
from .authentication import EcommerceClientJWTAuthentication
from .models import (
    EcommerceClient,
    Product,
    ProductAttributeValue,
    AttributeDefinition,
    ClientAddress,
    FavoriteProduct,
    Cart,
    CartItem,
    Order,
    OrderItem,
    ClientCard,
    EcommerceSettings,
    Language,
    ShippingMethod,
    PromoCode,
    ProductReview,
)
from tickets.models import ItemList, ListItem
from .serializers import (
    EcommerceClientSerializer,
    ProductListSerializer,
    ProductDetailSerializer,
    AttributeDefinitionSerializer,
    FavoriteProductSerializer,
    FavoriteProductCreateSerializer,
    CartSerializer,
    CartItemSerializer,
    CartItemCreateSerializer,
    OrderSerializer,
    OrderCreateSerializer,
    ClientCardSerializer,
    ItemListMinimalSerializer,
    ItemListDetailSerializer,
    ListItemSerializer,
    LanguageSerializer,
    HomepageSectionPublicSerializer,
    ShippingMethodSerializer,
    ProductReviewSerializer,
    ProductReviewCreateSerializer,
)
from .models import HomepageSection


class ClientAddressSerializer(serializers.ModelSerializer):
    """Serializer for client addresses (client-facing, auto-sets client from token)"""
    client = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = ClientAddress
        fields = [
            'id', 'client', 'label', 'address', 'city',
            'extra_instructions', 'latitude', 'longitude',
            'postal_code', 'country',
            'is_default', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'client', 'created_at', 'updated_at']

    def validate(self, attrs):
        """Ensure coordinates are both set or both unset"""
        latitude = attrs.get('latitude')
        longitude = attrs.get('longitude')

        if (latitude is not None and longitude is None) or (latitude is None and longitude is not None):
            raise serializers.ValidationError(
                "Both latitude and longitude must be provided together"
            )

        return attrs


class ClientProfileViewSet(viewsets.GenericViewSet):
    """
    Client profile management
    Authenticated clients can view and update their own profile
    """
    serializer_class = EcommerceClientSerializer
    authentication_classes = [EcommerceClientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=['Ecommerce Client - Profile'],
        summary='Get authenticated client profile',
        description='Get the profile information of the authenticated client'
    )
    @action(detail=False, methods=['get'])
    def me(self, request):
        """Get authenticated client's profile"""
        serializer = self.get_serializer(request.user)
        return Response(serializer.data)

    @extend_schema(
        tags=['Ecommerce Client - Profile'],
        summary='Update authenticated client profile',
        description='Update the profile information of the authenticated client'
    )
    @action(detail=False, methods=['patch'])
    def update_profile(self, request):
        """Update authenticated client's profile"""
        serializer = self.get_serializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


@extend_schema(
    tags=['Ecommerce Client - Auth'],
    summary='Change client password',
    description='Change the password of the authenticated client. Requires current password for verification.',
    request=inline_serializer(
        name='ChangePasswordRequest',
        fields={
            'current_password': serializers.CharField(),
            'new_password': serializers.CharField(),
        },
    ),
    responses={
        200: inline_serializer(
            name='ChangePasswordResponse',
            fields={
                'message': serializers.CharField(),
            },
        ),
        400: OpenApiResponse(description='Validation error'),
    },
)
@api_view(['POST'])
@authentication_classes([EcommerceClientJWTAuthentication])
@permission_classes([IsAuthenticated])
def change_password(request):
    """Change password for the authenticated ecommerce client"""
    current_password = request.data.get('current_password')
    new_password = request.data.get('new_password')

    if not current_password or not new_password:
        return Response(
            {'error': 'current_password and new_password are required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    client = request.user
    if not client.check_password(current_password):
        return Response(
            {'error': 'Current password is incorrect'},
            status=status.HTTP_400_BAD_REQUEST
        )

    client.set_password(new_password)
    client.save(update_fields=['password'])

    return Response({'message': 'Password changed successfully'})


class ClientAttributeViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Client-facing attribute browsing (read-only, public access)
    Returns only filterable attributes for product filtering
    """
    queryset = AttributeDefinition.objects.filter(is_active=True, is_filterable=True)
    serializer_class = AttributeDefinitionSerializer
    authentication_classes = []
    permission_classes = [AllowAny]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['attribute_type']
    ordering_fields = ['sort_order', 'key']
    ordering = ['sort_order', 'id']

    def get_serializer_context(self):
        context = super().get_serializer_context()
        language = self.request.query_params.get('language', 'en')
        context['language'] = language
        return context

    @extend_schema(
        tags=['Ecommerce Client - Attributes'],
        summary='List filterable attributes',
        description='Get all filterable attributes for product filtering'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Attributes'],
        summary='Get attribute details',
        description='View detailed attribute information including options'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Attributes'],
        summary='Get faceted search data',
        description='''Get attribute filter options with product counts.
        Returns available filter values and their product counts for each attribute.
        Supports existing filter selections to show refined counts.'''
    )
    @action(detail=False, methods=['get'])
    def facets(self, request):
        """
        Get faceted search data for all filterable attributes
        Returns option values with product counts
        """
        from django.db.models import Count, Q, Min, Max

        # Get query parameters for existing filters
        existing_filters = {}
        for key, value in request.GET.items():
            if key.startswith('attr_'):
                attr_key = key[5:]  # Remove 'attr_' prefix
                existing_filters[attr_key] = value.split(',')  # Support multiple values

        # Base product queryset (only active products)
        products_qs = Product.objects.filter(status='active')

        # Apply existing filters to narrow down products
        for attr_key, values in existing_filters.items():
            try:
                attribute = AttributeDefinition.objects.get(key=attr_key, is_filterable=True)
                q_filter = Q()
                for value in values:
                    if attribute.attribute_type == 'select':
                        q_filter |= Q(
                            attribute_values__attribute=attribute,
                            attribute_values__value_text=value
                        )
                    elif attribute.attribute_type == 'multiselect':
                        q_filter |= Q(
                            attribute_values__attribute=attribute,
                            attribute_values__value_json__contains=value
                        )
                    elif attribute.attribute_type == 'number':
                        # Support range: min-max format
                        if '-' in value:
                            min_val, max_val = value.split('-')
                            q_filter |= Q(
                                attribute_values__attribute=attribute,
                                attribute_values__value_number__gte=float(min_val),
                                attribute_values__value_number__lte=float(max_val)
                            )
                products_qs = products_qs.filter(q_filter).distinct()
            except AttributeDefinition.DoesNotExist:
                continue

        # Get all filterable attributes
        attributes = AttributeDefinition.objects.filter(is_active=True, is_filterable=True).order_by('sort_order')

        facet_data = []
        language = request.query_params.get('language', 'en')

        for attribute in attributes:
            facet = {
                'attribute_key': attribute.key,
                'attribute_name': attribute.get_name(language),
                'attribute_type': attribute.attribute_type,
                'options': []
            }

            if attribute.attribute_type in ['select', 'multiselect']:
                # For select types, count products per option value
                for option in attribute.options:
                    value = option.get('value', '')
                    label = option.get(language, option.get('en', value))

                    # Count products with this option value
                    count = products_qs.filter(
                        attribute_values__attribute=attribute,
                        attribute_values__value_text=value
                    ).distinct().count()

                    if count > 0:  # Only include options that have products
                        facet['options'].append({
                            'value': value,
                            'label': label,
                            'count': count
                        })

            elif attribute.attribute_type == 'number':
                # For number types, return min and max range
                value_range = products_qs.filter(
                    attribute_values__attribute=attribute
                ).aggregate(
                    min_value=Min('attribute_values__value_number'),
                    max_value=Max('attribute_values__value_number')
                )

                if value_range['min_value'] is not None:
                    facet['range'] = {
                        'min': float(value_range['min_value']),
                        'max': float(value_range['max_value']),
                        'unit': attribute.unit
                    }

            # Only include facets that have data
            if facet.get('options') or facet.get('range'):
                facet_data.append(facet)

        return Response({
            'facets': facet_data,
            'total_products': products_qs.distinct().count()
        })


class ClientProductAutoSchema(AutoSchema):
    """Custom schema for ClientProductViewSet that dynamically generates attribute filter parameters"""

    def get_override_parameters(self):
        """Add dynamic attribute filter parameters to the schema"""
        import logging
        from django.db import connection
        from tenant_schemas.utils import schema_context, get_public_schema_name
        from tenants.models import Tenant

        logger = logging.getLogger(__name__)
        parameters = super().get_override_parameters()

        # Only add dynamic parameters for the list action
        if self.method.lower() != 'get' or self.path.endswith('{id}/'):
            return parameters

        # Collect unique attributes from all tenants
        all_attributes = {}

        try:
            # Get all active tenants
            with schema_context(get_public_schema_name()):
                tenants = Tenant.objects.all()

            # Query attributes from each tenant schema
            for tenant in tenants:
                try:
                    with schema_context(tenant.schema_name):
                        # Use 'name' field (JSONField) instead of 'label'
                        attrs = AttributeDefinition.objects.filter(is_filterable=True).values('key', 'name', 'attribute_type')

                        for attr in attrs:
                            attr_key = attr['key']
                            # Store unique attributes by key (avoid duplicates across tenants)
                            if attr_key not in all_attributes:
                                # Extract label from JSONField 'name' (multilingual)
                                attr_name = attr['name']
                                if isinstance(attr_name, dict):
                                    # Try to get English name, fallback to first available or key
                                    attr_label = attr_name.get('en', next(iter(attr_name.values()), attr_key.title()))
                                else:
                                    attr_label = attr_key.title()

                                all_attributes[attr_key] = {
                                    'key': attr_key,
                                    'label': attr_label,
                                    'type': attr['attribute_type']
                                }
                except Exception as e:
                    # Log the error but continue with other tenants
                    logger.warning(f"Error querying attributes for tenant {tenant.schema_name}: {e}")
                    continue

            # Generate parameters for all unique attributes
            for attr_key, attr_data in all_attributes.items():
                attr_label = attr_data['label']
                attr_type = attr_data['type']

                # Create description based on attribute type
                if attr_type == 'select':
                    description = f'Filter by {attr_label}. Supports multiple values separated by comma (OR logic). Example: ?attr_{attr_key}=value1,value2'
                elif attr_type == 'multiselect':
                    description = f'Filter by {attr_label}. Supports multiple values separated by comma. Example: ?attr_{attr_key}=value1,value2'
                elif attr_type == 'boolean':
                    description = f'Filter by {attr_label}. Use true or false. Example: ?attr_{attr_key}=true'
                elif attr_type == 'number':
                    description = f'Filter by {attr_label}. Supports ranges with hyphen (min-max) or exact values. Example: ?attr_{attr_key}=10-20'
                elif attr_type == 'text':
                    description = f'Filter by {attr_label}. Supports partial text matching. Example: ?attr_{attr_key}=search_term'
                else:
                    description = f'Filter by {attr_label}. Example: ?attr_{attr_key}=value'

                parameters.append(
                    OpenApiParameter(
                        name=f'attr_{attr_key}',
                        type=str,
                        location=OpenApiParameter.QUERY,
                        description=description,
                        required=False,
                    )
                )

            logger.info(f"Generated {len(all_attributes)} dynamic attribute parameters for OpenAPI schema")

        except Exception as e:
            # Log the error for debugging
            logger.error(f"Failed to generate dynamic attribute parameters: {e}")
            pass

        return parameters


class ClientProductViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Client-facing product browsing (read-only, public access)
    Anyone can view active products without authentication
    """
    queryset = Product.objects.filter(status='active').select_related(
        'created_by',
        'updated_by'
    ).prefetch_related(
        'images',
        'attribute_values__attribute',
        'variants__attribute_values__attribute'
    )
    authentication_classes = []
    permission_classes = [AllowAny]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_featured']
    search_fields = ['sku', 'slug']
    ordering_fields = ['price', 'created_at', 'name']
    ordering = ['-created_at']
    schema = ClientProductAutoSchema()

    def get_queryset(self):
        """
        Filter products by attributes using query params like: ?attr_color=red,blue&attr_size=large
        Also supports price range: ?min_price=10&max_price=100
        And on_sale filter: ?on_sale=true
        """
        from django.db.models import Q, F

        queryset = super().get_queryset()

        # Price range filtering
        min_price = self.request.query_params.get('min_price')
        max_price = self.request.query_params.get('max_price')
        if min_price:
            queryset = queryset.filter(price__gte=min_price)
        if max_price:
            queryset = queryset.filter(price__lte=max_price)

        # On sale filter - products where compare_at_price > price
        on_sale = self.request.query_params.get('on_sale')
        if on_sale and on_sale.lower() == 'true':
            queryset = queryset.filter(
                compare_at_price__isnull=False,
                compare_at_price__gt=F('price')
            )

        # Attribute-based filtering
        for key, value in self.request.GET.items():
            if key.startswith('attr_'):
                attr_key = key[5:]  # Remove 'attr_' prefix
                values = value.split(',')  # Support multiple values (OR logic)

                try:
                    attribute = AttributeDefinition.objects.get(key=attr_key, is_filterable=True)
                    q_filter = Q()

                    for val in values:
                        if attribute.attribute_type == 'select':
                            q_filter |= Q(
                                attribute_values__attribute=attribute,
                                attribute_values__value_text=val
                            )
                        elif attribute.attribute_type == 'multiselect':
                            q_filter |= Q(
                                attribute_values__attribute=attribute,
                                attribute_values__value_json__contains=val
                            )
                        elif attribute.attribute_type == 'number':
                            # Support range: min-max format
                            if '-' in val:
                                min_val, max_val = val.split('-')
                                q_filter |= Q(
                                    attribute_values__attribute=attribute,
                                    attribute_values__value_number__gte=float(min_val),
                                    attribute_values__value_number__lte=float(max_val)
                                )
                            else:
                                q_filter |= Q(
                                    attribute_values__attribute=attribute,
                                    attribute_values__value_number=float(val)
                                )

                    if q_filter:
                        queryset = queryset.filter(q_filter).distinct()

                except AttributeDefinition.DoesNotExist:
                    continue

        return queryset

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ProductDetailSerializer
        return ProductListSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        language = self.request.query_params.get('language', 'en')
        context['language'] = language
        return context

    @extend_schema(
        tags=['Ecommerce Client - Products'],
        summary='List products',
        description='''Browse active products with filtering options.

        **Dynamic Attribute Filters:**
        Filter by any product attribute using the pattern `attr_{attribute_key}`.
        The available attribute filters are dynamically generated based on your attribute definitions.

        Examples:
        - `?attr_color=red` - Single value
        - `?attr_color=red,blue,green` - Multiple values (OR logic)
        - `?attr_size=large&attr_color=red` - Multiple attributes (AND logic)
        - `?attr_width=10-20` - Number ranges
        - `?attr_waterproof=true` - Boolean values

        **Ordering Options:**
        - `?ordering=price` - Sort by price ascending
        - `?ordering=-price` - Sort by price descending
        - `?ordering=created_at` - Sort by creation date ascending (oldest first)
        - `?ordering=-created_at` - Sort by creation date descending (newest first)
        - `?ordering=name` - Sort by name ascending
        - `?ordering=-name` - Sort by name descending

        **Pagination:**
        - `?page=2` - Get specific page
        - `?page_size=50` - Items per page (default: 20, max: 100)

        **Other Filters:**
        - Basic: `?is_featured=true`
        - On Sale: `?on_sale=true`
        - Search: `?search=laptop`
        - Price: `?min_price=100&max_price=500`
        - Language: `?language=ka`''',
        parameters=[
            OpenApiParameter(
                name='min_price',
                type=float,
                location=OpenApiParameter.QUERY,
                description='Minimum price filter',
                required=False,
                examples=[
                    OpenApiExample('Example', value=10.00)
                ]
            ),
            OpenApiParameter(
                name='max_price',
                type=float,
                location=OpenApiParameter.QUERY,
                description='Maximum price filter',
                required=False,
                examples=[
                    OpenApiExample('Example', value=500.00)
                ]
            ),
            OpenApiParameter(
                name='on_sale',
                type=bool,
                location=OpenApiParameter.QUERY,
                description='Filter to show only products on sale (compare_at_price > price)',
                required=False,
                examples=[
                    OpenApiExample('On Sale Only', value=True),
                ]
            ),
            OpenApiParameter(
                name='ordering',
                type=str,
                location=OpenApiParameter.QUERY,
                description='Sort products by field. Available options: price, -price, created_at, -created_at, name, -name. Prefix with - for descending order.',
                required=False,
                examples=[
                    OpenApiExample('Price Low to High', value='price'),
                    OpenApiExample('Price High to Low', value='-price'),
                    OpenApiExample('Newest First', value='-created_at'),
                    OpenApiExample('Oldest First', value='created_at'),
                    OpenApiExample('Name A-Z', value='name'),
                    OpenApiExample('Name Z-A', value='-name'),
                ]
            ),
            OpenApiParameter(
                name='language',
                type=str,
                location=OpenApiParameter.QUERY,
                description='Language code for multilingual content (e.g., en, ka, ru)',
                required=False,
                examples=[
                    OpenApiExample('English', value='en'),
                    OpenApiExample('Georgian', value='ka'),
                ]
            ),
            OpenApiParameter(
                name='page',
                type=int,
                location=OpenApiParameter.QUERY,
                description='Page number for pagination',
                required=False,
                examples=[
                    OpenApiExample('First page', value=1),
                    OpenApiExample('Second page', value=2),
                ]
            ),
            OpenApiParameter(
                name='page_size',
                type=int,
                location=OpenApiParameter.QUERY,
                description='Number of items per page (default: 20, max: 100)',
                required=False,
                examples=[
                    OpenApiExample('Default (20)', value=20),
                    OpenApiExample('50 items', value=50),
                    OpenApiExample('Maximum (100)', value=100),
                ]
            ),
            # Dynamic attribute parameters are added via ClientProductAutoSchema
        ]
    )
    @method_decorator(cache_page(60 * 5))  # Cache for 5 minutes
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Products'],
        summary='Get product details',
        description='View detailed product information'
    )
    @method_decorator(cache_page(60 * 5))  # Cache for 5 minutes
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)


class ClientAddressViewSet(viewsets.ModelViewSet):
    """
    Client-facing address management
    Clients can only access their own addresses
    """
    serializer_class = ClientAddressSerializer
    authentication_classes = [EcommerceClientJWTAuthentication]
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['is_default']
    ordering_fields = ['created_at', 'is_default']
    ordering = ['-is_default', '-created_at']

    def get_queryset(self):
        """Return only addresses belonging to the authenticated client"""
        return ClientAddress.objects.filter(client=self.request.user)

    def perform_create(self, serializer):
        """Automatically set client from authenticated user"""
        serializer.save(client=self.request.user)

    @extend_schema(
        tags=['Ecommerce Client - Addresses'],
        summary='List my addresses',
        description='Get all delivery addresses for authenticated client'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Addresses'],
        summary='Get address details',
        description='View details of a specific address'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Addresses'],
        summary='Create new address',
        description='Add a new delivery address (client auto-set from token)'
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Addresses'],
        summary='Update address'
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Addresses'],
        summary='Partially update address'
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Addresses'],
        summary='Delete address'
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Addresses'],
        summary='Set address as default'
    )
    @action(detail=True, methods=['post'])
    def set_default(self, request, pk=None):
        """Set an address as the default for the client"""
        address = self.get_object()
        address.is_default = True
        address.save()
        serializer = self.get_serializer(address)
        return Response(serializer.data)


class ClientFavoriteViewSet(viewsets.ModelViewSet):
    """
    Client-facing favorites/wishlist management
    Clients can only access their own favorites
    """
    authentication_classes = [EcommerceClientJWTAuthentication]
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['product']
    ordering_fields = ['created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        """Return only favorites belonging to the authenticated client"""
        return FavoriteProduct.objects.filter(client=self.request.user).select_related('product', 'client').prefetch_related('product__images')

    def get_serializer_class(self):
        if self.action == 'create':
            return FavoriteProductCreateSerializer
        return FavoriteProductSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        language = self.request.query_params.get('language', 'en')
        context['language'] = language
        return context

    def perform_create(self, serializer):
        """Automatically set client from authenticated user"""
        serializer.save(client=self.request.user)

    @extend_schema(
        tags=['Ecommerce Client - Favorites'],
        summary='List my favorites',
        description='Get all favorite products for authenticated client'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Favorites'],
        summary='Add to favorites'
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Favorites'],
        summary='Remove from favorites'
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


class ClientCartViewSet(viewsets.ModelViewSet):
    """
    Client-facing shopping cart management
    Clients can only access their own carts
    """
    serializer_class = CartSerializer
    authentication_classes = [EcommerceClientJWTAuthentication]
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['status']
    ordering_fields = ['created_at', 'updated_at']
    ordering = ['-updated_at']

    def get_queryset(self):
        """Return only carts belonging to the authenticated client"""
        return Cart.objects.filter(client=self.request.user).select_related('client', 'delivery_address').prefetch_related('items', 'items__product', 'items__product__images', 'items__variant')

    @extend_schema(
        tags=['Ecommerce Client - Cart'],
        summary='Get or create active cart',
        description='Get authenticated client\'s active cart or create new one'
    )
    @action(detail=False, methods=['get'])
    def get_or_create(self, request):
        """Get or create active cart for authenticated client"""
        client = request.user

        cart, created = Cart.objects.get_or_create(
            client=client,
            status='active',
            defaults={'status': 'active'}
        )
        serializer = self.get_serializer(cart)
        return Response({
            'cart': serializer.data,
            'created': created
        })


class ClientCartItemViewSet(viewsets.ModelViewSet):
    """
    Client-facing cart item management
    Clients can only access items in their own carts
    """
    authentication_classes = [EcommerceClientJWTAuthentication]
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['cart', 'product']

    def get_queryset(self):
        """Return only cart items belonging to the authenticated client's carts"""
        return CartItem.objects.filter(cart__client=self.request.user).select_related('cart', 'product', 'cart__client')

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return CartItemCreateSerializer
        return CartItemSerializer

    @extend_schema(
        tags=['Ecommerce Client - Cart Items'],
        summary='List cart items',
        description='Get items in authenticated client\'s carts'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Cart Items'],
        summary='Add item to cart'
    )
    def create(self, request, *args, **kwargs):
        """If same product+variant already in cart, increment quantity instead of duplicating."""
        cart_id = request.data.get('cart')
        product_id = request.data.get('product')
        variant_id = request.data.get('variant')
        add_quantity = int(request.data.get('quantity', 1))

        if cart_id and product_id:
            existing = CartItem.objects.filter(
                cart_id=cart_id,
                cart__client=request.user,
                product_id=product_id,
                variant_id=variant_id,
            ).first()

            if existing:
                existing.quantity += add_quantity
                existing.save(update_fields=['quantity'])
                serializer = CartItemSerializer(existing)
                return Response(serializer.data, status=status.HTTP_200_OK)

        return super().create(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Cart Items'],
        summary='Update cart item quantity'
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Cart Items'],
        summary='Remove item from cart'
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


class ClientOrderViewSet(viewsets.ModelViewSet):
    """
    Client-facing order management
    Clients can only access their own orders
    """
    authentication_classes = [EcommerceClientJWTAuthentication]
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['status']
    ordering_fields = ['created_at', 'total_amount']
    ordering = ['-created_at']

    def get_queryset(self):
        """Return only orders belonging to the authenticated client"""
        return Order.objects.filter(client=self.request.user).select_related('client', 'delivery_address').prefetch_related('items', 'items__product', 'items__variant')

    def get_serializer_class(self):
        if self.action == 'create':
            return OrderCreateSerializer
        return OrderSerializer

    @extend_schema(
        tags=['Ecommerce Client - Orders'],
        summary='List my orders',
        description='Get all orders for authenticated client'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Orders'],
        summary='Get order details',
        description='View detailed order information'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Orders'],
        summary='Create order from cart',
        description='''Submit cart and create order.
        - If card_id is provided: Charges the saved card directly
        - If card_id is null/not provided: Returns a BOG payment URL for new payment
        - If payment_method is "cash_on_delivery": Creates order without payment processing
        - Optional promo_code and shipping_method_id for discounts and shipping'''
    )
    def create(self, request, *args, **kwargs):
        from tenants.bog_payment import bog_service
        from django.conf import settings
        from django.db import connection

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = serializer.save()

        # Automatically generate payment URL
        payment_method = request.data.get('payment_method', 'card')

        # Send order confirmation email — only for COD orders, where the
        # order is final the moment it's placed. Card-payment orders go
        # through BOG, and the confirmation email fires from the BOG
        # webhook once payment actually clears (see ecommerce_payment_webhook
        # in views.py). Sending it here too would mean the customer gets
        # an "Order Confirmed" email before they've even paid.
        if payment_method == 'cash_on_delivery':
            try:
                from .tasks import send_order_email
                send_order_email.delay(connection.schema_name, order.id, 'confirmation')
            except Exception:
                pass  # Don't fail order creation if email task fails

        if payment_method == 'cash_on_delivery':
            # No BOG payment needed, just mark as COD
            order.payment_method = 'cash_on_delivery'
            order.payment_status = 'pending'
            order.save()

            # Book Quickshipper courier — for COD orders we hand off to the
            # courier the moment the order is created. The booking task is
            # idempotent and a no-op if Quickshipper isn't configured.
            try:
                from .tasks import book_quickshipper_courier
                book_quickshipper_courier.delay(connection.schema_name, order.id)
            except Exception:
                pass

            output_serializer = OrderSerializer(order)
            response_data = output_serializer.data
            response_data['payment_method'] = 'cash_on_delivery'
            response_data['message'] = 'Order will be paid on delivery'
            return Response(response_data, status=status.HTTP_201_CREATED)

        # Build the BOG service with the tenant's own merchant credentials (or
        # the platform's when the tenant hasn't set any). If the tenant HAS
        # credentials configured but they can't be loaded, this raises rather
        # than silently charging the customer into the platform's BOG account.
        from .models import EcommerceSettings
        from .payment_utils import get_tenant_bog_service, BogCredentialError
        try:
            bog_service_instance = get_tenant_bog_service(request.tenant)
        except BogCredentialError:
            import logging
            logging.getLogger(__name__).exception(
                'BOG credential error for tenant %s on order %s',
                getattr(request.tenant, 'schema_name', request.tenant), order.order_number
            )
            order.cancel(reason='Payment configuration error')
            return Response(
                {'error': 'Payment is temporarily unavailable. Please try again later.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        # Check if a card_id was provided in the request for charging
        card_id = request.data.get('card_id')
        if card_id:
            # Use provided saved card to charge
            try:
                card = ClientCard.objects.get(id=card_id, client=request.user, is_active=True)
                callback_url = f"https://{request.get_host()}/api/ecommerce/payment-webhook/"

                payment_result = bog_service_instance.charge_saved_card(
                    parent_order_id=card.parent_order_id,
                    amount=float(order.total_amount),
                    currency='GEL',
                    callback_url=callback_url,
                    external_order_id=order.order_number
                )

                # Update order with payment info
                order.bog_order_id = payment_result['order_id']
                order.payment_status = 'processing'
                order.payment_method = 'saved_card'
                order.payment_metadata = payment_result
                order.save()

                # Return order data with payment info
                output_serializer = OrderSerializer(order)
                response_data = output_serializer.data
                response_data['payment_method'] = 'saved_card'
                response_data['bog_order_id'] = payment_result['order_id']

                # Check if user authentication is required (3D Secure)
                if payment_result.get('requires_authentication'):
                    response_data['payment_url'] = payment_result['payment_url']
                    response_data['message'] = 'Please complete payment authentication'
                else:
                    response_data['message'] = 'Order charged to saved card'

                return Response(response_data, status=status.HTTP_201_CREATED)

            except ClientCard.DoesNotExist:
                # Card not found, return error
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f'Card {card_id} not found for order {order.order_number}')
                return Response({
                    'error': 'Card not found or inactive'
                }, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                # If saved card charge fails, return error instead of falling back
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f'Saved card charge failed for order {order.order_number}: {str(e)}')
                return Response({
                    'error': 'Failed to charge saved card',
                    'details': str(e)
                }, status=status.HTTP_400_BAD_REQUEST)

        # Create BOG payment (new payment or fallback from failed saved card charge)
        try:
            callback_url = f"https://{request.get_host()}/api/ecommerce/payment-webhook/"

            # Use return URLs from EcommerceSettings if configured, otherwise from request
            try:
                ecommerce_settings = EcommerceSettings.objects.get(tenant=request.tenant)
                return_url_success = ecommerce_settings.bog_return_url_success or request.data.get('return_url_success', '')
                return_url_fail = ecommerce_settings.bog_return_url_fail or request.data.get('return_url_fail', '')
            except EcommerceSettings.DoesNotExist:
                return_url_success = request.data.get('return_url_success', '')
                return_url_fail = request.data.get('return_url_fail', '')

            payment_result = bog_service_instance.create_payment(
                amount=float(order.total_amount),
                currency='GEL',
                description=f"Order {order.order_number}",
                customer_email=order.client.email,
                customer_name=order.client.full_name,
                customer_phone=order.client.phone_number or '',
                return_url_success=return_url_success,
                return_url_fail=return_url_fail,
                callback_url=callback_url,
                external_order_id=order.order_number,
                metadata={
                    'order_id': order.id,
                    'order_number': order.order_number,
                    'tenant_id': request.tenant.id
                }
            )

            # Update order with payment info
            order.bog_order_id = payment_result['order_id']
            order.payment_url = payment_result['payment_url']
            order.payment_status = 'pending'
            order.payment_method = 'card'
            order.payment_metadata = payment_result
            order.save()

            # Return order data with payment info
            output_serializer = OrderSerializer(order)
            response_data = output_serializer.data
            response_data['payment_url'] = payment_result['payment_url']
            response_data['bog_order_id'] = payment_result['order_id']
            response_data['payment_amount'] = payment_result['amount']
            response_data['payment_currency'] = payment_result['currency']
            return Response(response_data, status=status.HTTP_201_CREATED)

        except Exception as e:
            # Payment initialization failed. The order and its stock reservation
            # already exist, so cancel (restock) it and report the failure rather
            # than returning 201 with a stranded, unpayable order that would hold
            # inventory hostage.
            import logging
            logger = logging.getLogger(__name__)
            logger.exception('BOG payment init failed for order %s: %s', order.order_number, e)
            try:
                order.cancel(reason='Payment initialization failed')
            except Exception:
                logger.exception('Failed to restock after payment-init failure for order %s', order.order_number)
            return Response(
                {'error': 'Payment could not be initialized. Please try again.', 'details': str(e)},
                status=status.HTTP_402_PAYMENT_REQUIRED
            )

    @extend_schema(
        tags=['Ecommerce Client - Orders'],
        summary='Cancel order',
        description='Cancel an order that is still pending or confirmed. Restores product stock.'
    )
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel an order and restore stock"""
        order = self.get_object()

        if order.status not in ['pending', 'confirmed']:
            return Response(
                {'error': 'Cannot cancel this order. Only pending or confirmed orders can be cancelled.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Order.cancel restocks atomically (F() update) and releases the promo
        # use, under a row lock so it can't race the abandoned-order sweeper.
        order.cancel(reason=request.data.get('reason', 'Cancelled by customer'))

        serializer = OrderSerializer(order)
        return Response(serializer.data)


@extend_schema(
    tags=['Ecommerce Client - Cards'],
    summary='Add new payment card',
    description='Initiate 0 GEL card validation payment to save a new card for future orders'
)
@api_view(['POST'])
@authentication_classes([EcommerceClientJWTAuthentication])
@permission_classes([IsAuthenticated])
def add_client_card(request):
    """
    Initiate 0 GEL payment to validate and save a new card for the client
    """
    from django.conf import settings
    from tenants.bog_payment import BOGPaymentService
    import uuid
    import logging

    logger = logging.getLogger(__name__)
    client = request.user

    try:
        # Try to get ecommerce settings
        try:
            ecommerce_settings = EcommerceSettings.objects.get(tenant=request.tenant)
            logger.info(f'Ecommerce settings found for tenant {request.tenant.schema_name}')
        except EcommerceSettings.DoesNotExist:
            ecommerce_settings = None
            logger.info(f'No ecommerce settings for tenant {request.tenant.schema_name}, will use environment credentials')

        # Configure BOG service with tenant or environment credentials
        bog_service = BOGPaymentService()

        if ecommerce_settings and ecommerce_settings.has_bog_credentials:
            # Use tenant's own BOG credentials
            logger.info(f'Using tenant BOG credentials for {request.tenant.schema_name}')
            bog_service.client_id = ecommerce_settings.bog_client_id
            bog_service.client_secret = ecommerce_settings.get_bog_secret()
            bog_service.auth_url = settings.BOG_AUTH_URL
            bog_service.base_url = settings.BOG_API_BASE_URL
        else:
            # Use credentials from environment variables
            logger.info(f'Using environment BOG credentials for {request.tenant.schema_name}')
            bog_service.client_id = settings.BOG_CLIENT_ID
            bog_service.client_secret = settings.BOG_CLIENT_SECRET
            bog_service.auth_url = settings.BOG_AUTH_URL
            bog_service.base_url = settings.BOG_API_BASE_URL

        if not bog_service.is_configured():
            logger.error(f'BOG service not configured properly. client_id: {bool(bog_service.client_id)}, client_secret: {bool(bog_service.client_secret)}')
            return Response({
                'error': 'Payment gateway not configured - missing BOG credentials'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        # Generate unique order ID for card validation
        order_id = f'card_{client.id}_{uuid.uuid4().hex[:12]}'

        # Get callback and return URLs
        callback_url = f"https://{request.get_host()}/api/ecommerce/payment-webhook/"
        return_url = ecommerce_settings.bog_return_url_success if (ecommerce_settings and ecommerce_settings.bog_return_url_success) else f'https://{request.tenant.schema_name}.echodesk.ge/payment/success'

        # Create 0 GEL payment for card validation
        try:
            # Get OAuth token first
            access_token = bog_service._get_access_token()

            # Prepare payment data for recurring payment (NOT subscription)
            payment_data = {
                'callback_url': callback_url,
                'purchase_units': {
                    'currency': 'GEL',
                    'total_amount': 0.01,  # Minimum 0.01 GEL for card validation (BOG requirement)
                    'basket': [{
                        'quantity': 1,
                        'unit_price': 0.01,
                        'product_id': 'card_validation',
                        'description': 'Card validation'
                    }]
                },
                'redirect_urls': {
                    'success': return_url,
                    'fail': return_url
                },
                'external_order_id': order_id,
                'payment_method': ['card'],
                'industry': 'ecommerce',
                'capture': 'automatic'
            }

            # Make API request with Bearer token
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {access_token}',
                'Accept-Language': 'en'
            }

            response = requests.post(
                f'{bog_service.base_url}/ecommerce/orders',
                json=payment_data,
                headers=headers,
                timeout=30
            )

            if response.status_code in [200, 201]:
                bog_response = response.json()
                bog_order_id = bog_response['id']
                payment_url = bog_response['_links']['redirect']['href']

                # Enable card saving for this order
                enable_result = bog_service.enable_card_saving(bog_order_id)
                if not enable_result:
                    logger.warning(f'Failed to enable card saving for order {bog_order_id}')

                logger.info(f'Card validation initiated for client {client.id}: {order_id}, bog_order_id: {bog_order_id}, card_saving_enabled: {enable_result}')

                return Response({
                    'order_id': order_id,
                    'payment_url': payment_url,
                    'message': 'Please complete card validation'
                }, status=status.HTTP_200_OK)
            else:
                error_details = response.text
                logger.error(f'BOG API error for card validation: {response.status_code} - {error_details}')
                return Response({
                    'error': 'Failed to initiate card validation',
                    'details': error_details
                }, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            logger.error(f'Card validation request failed for client {client.id}: {str(e)}')
            return Response({
                'error': 'Failed to initiate card validation',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    except Exception as e:
        logger.error(f'Unexpected error in add_client_card for client {client.id}: {str(e)}')
        return Response({
            'error': 'An unexpected error occurred',
            'message': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(
    tags=['Ecommerce Client - Cards'],
    summary='List saved payment cards',
    description='Get all saved payment cards for the authenticated client'
)
@api_view(['GET'])
@authentication_classes([EcommerceClientJWTAuthentication])
@permission_classes([IsAuthenticated])
def list_client_cards(request):
    """
    List all saved payment cards for the authenticated client
    """
    client = request.user

    cards = ClientCard.objects.filter(client=client, is_active=True)
    serializer = ClientCardSerializer(cards, many=True)

    return Response(serializer.data)


@extend_schema(
    tags=['Ecommerce Client - Cards'],
    summary='Delete payment card',
    description='Soft-delete a saved payment card'
)
@api_view(['DELETE'])
@authentication_classes([EcommerceClientJWTAuthentication])
@permission_classes([IsAuthenticated])
def delete_client_card(request, card_id):
    """
    Delete a saved payment card
    """
    client = request.user

    try:
        card = ClientCard.objects.get(id=card_id, client=client)
        card.is_active = False
        card.save()

        return Response({
            'message': 'Card deleted successfully'
        }, status=status.HTTP_200_OK)

    except ClientCard.DoesNotExist:
        return Response({
            'error': 'Card not found'
        }, status=status.HTTP_404_NOT_FOUND)


@extend_schema(
    tags=['Ecommerce Client - Cards'],
    summary='Set default payment card',
    description='Set a card as the default payment method for the authenticated client'
)
@api_view(['POST'])
@authentication_classes([EcommerceClientJWTAuthentication])
@permission_classes([IsAuthenticated])
def set_default_client_card(request, card_id):
    """
    Set a card as the default payment method
    """
    client = request.user

    try:
        card = ClientCard.objects.get(id=card_id, client=client, is_active=True)

        # Set this card as default (the model's save method handles unsetting others)
        card.is_default = True
        card.save()

        return Response({
            'message': 'Default card updated successfully'
        }, status=status.HTTP_200_OK)

    except ClientCard.DoesNotExist:
        return Response({
            'error': 'Card not found'
        }, status=status.HTTP_404_NOT_FOUND)



class ClientItemListViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Ecommerce clients can access public item lists
    These are the same item lists used in tickets, but filtered to show only public ones
    No authentication required - public access
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['title', 'description']
    ordering_fields = ['title', 'created_at']
    ordering = ['title']

    def get_queryset(self):
        """Return only public and active item lists with their items"""
        return ItemList.objects.filter(
            is_public=True,
            is_active=True
        ).prefetch_related('items', 'items__children')

    def get_serializer_class(self):
        """Use detailed serializer for retrieve, minimal for list"""
        if self.action == 'retrieve':
            return ItemListDetailSerializer
        return ItemListMinimalSerializer

    @extend_schema(
        tags=['Ecommerce Client - Item Lists'],
        summary='List public item lists',
        description='Get all public item lists that are marked as accessible to clients'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Item Lists'],
        summary='Get item list with all items',
        description='Get detailed item list including all items in the list (hierarchical structure)'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Item Lists'],
        summary='Get specific item from item list',
        description='Get a specific item by its ID from within an item list',
        responses={200: ListItemSerializer}
    )
    @action(detail=True, methods=['get'], url_path='items/(?P<item_id>[^/.]+)')
    def get_item(self, request, pk=None, item_id=None):
        """
        Get a specific item from an item list
        GET /api/ecommerce/client/item-lists/{id}/items/{item_id}/
        """
        # Get the item list first (ensures it's public and active)
        item_list = self.get_object()

        # Try to get the specific item
        try:
            item = ListItem.objects.get(
                id=item_id,
                item_list=item_list,
                is_active=True
            )
            serializer = ListItemSerializer(item)
            return Response(serializer.data)
        except ListItem.DoesNotExist:
            return Response(
                {'error': 'Item not found in this list'},
                status=status.HTTP_404_NOT_FOUND
            )


@extend_schema_view(
    list=extend_schema(
        tags=['Ecommerce Client - Languages'],
        summary='List available languages',
        description='Get all active languages available for the storefront'
    ),
    retrieve=extend_schema(
        tags=['Ecommerce Client - Languages'],
        summary='Get language details',
        description='Get details of a specific language'
    )
)
class ClientLanguageViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Client-facing language endpoint for storefronts.
    Returns active languages that can be used for UI localization.
    No authentication required - public access.
    """
    serializer_class = LanguageSerializer
    permission_classes = [AllowAny]
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['sort_order', 'code', 'name']
    ordering = ['sort_order']

    def get_queryset(self):
        """Return only active languages ordered by sort_order"""
        return Language.objects.filter(is_active=True).order_by('sort_order')


@extend_schema(
    tags=['Ecommerce Client - Homepage'],
    summary='Get homepage configuration',
    description='''Get the complete homepage configuration with resolved section data.
    Returns all active sections ordered by position, with data from linked ItemLists resolved.
    This endpoint is public and does not require authentication.''',
    responses={200: HomepageSectionPublicSerializer(many=True)}
)
@api_view(['GET'])
@permission_classes([AllowAny])
@cache_page(60 * 10)  # Cache for 10 minutes
def get_homepage_config(request):
    """
    Get the homepage configuration for the storefront.
    Returns all active sections with their resolved data from ItemLists.

    GET /api/ecommerce/client/homepage/
    """
    from django.db.models import Prefetch
    from tickets.models import ListItem

    sections = HomepageSection.objects.filter(
        is_active=True
    ).select_related('item_list').prefetch_related(
        Prefetch(
            'item_list__items',
            queryset=ListItem.objects.filter(
                is_active=True,
                parent__isnull=True
            ).prefetch_related('children').order_by('position'),
            to_attr='prefetched_root_items'
        )
    ).order_by('position')

    serializer = HomepageSectionPublicSerializer(sections, many=True)

    return Response({
        'sections': serializer.data
    })


@extend_schema(
    operation_id='get_store_theme',
    summary='Get store theme configuration',
    description='Get the theme configuration (colors, border radius) for the storefront. This endpoint is public and does not require authentication.',
    responses={
        200: inline_serializer(
            name='StoreThemeResponse',
            fields={
                'preset': serializers.CharField(),
                'colors': serializers.DictField(child=serializers.CharField()),
                'radius': serializers.CharField(),
                'store_name': serializers.CharField(),
                'payment': inline_serializer(
                    name='PaymentConfigResponse',
                    fields={
                        'active_providers': serializers.ListField(child=serializers.CharField()),
                        'enable_cash_on_delivery': serializers.BooleanField(),
                        'enable_card_payment': serializers.BooleanField(),
                        'currency': serializers.CharField(),
                        'tax_rate': serializers.DecimalField(max_digits=5, decimal_places=2, required=False),
                        'tax_label': serializers.CharField(required=False),
                        'tax_inclusive': serializers.BooleanField(required=False),
                    }
                ),
                'chat_widget': inline_serializer(
                    name='ChatWidgetConfigResponse',
                    fields={
                        'token': serializers.CharField(allow_null=True),
                    }
                ),
            }
        ),
    },
    tags=['Store']
)
@api_view(['GET'])
@permission_classes([AllowAny])
@authentication_classes([])
def get_store_theme(request):
    """
    Get the theme configuration for the storefront.
    Returns theme colors, border radius, and preset information.

    GET /api/ecommerce/client/theme/
    """
    # Resolve the active embeddable chat widget for this tenant. The
    # storefront drops in <script async src="…/widget.js?t=TOKEN"> on
    # every page when this is non-null, so visitors automatically get
    # the support chat without the tenant having to paste a snippet.
    # WidgetConnection lives on the public schema, keyed by schema name;
    # SocialIntegrationSettings.widget_enabled is the tenant-side master
    # switch on the current schema.
    chat_widget_payload = {'token': None}
    try:
        from django.db import connection as _db_connection
        from widget_registry.models import WidgetConnection
        from social_integrations.models import SocialIntegrationSettings

        social = SocialIntegrationSettings.objects.first()
        if social and getattr(social, 'widget_enabled', False):
            conn = (
                WidgetConnection.objects
                .filter(
                    tenant_schema=_db_connection.schema_name,
                    is_active=True,
                )
                .order_by('-created_at')
                .first()
            )
            if conn:
                chat_widget_payload = {'token': conn.widget_token}
    except Exception:
        # Never let widget lookup break the theme endpoint — storefront
        # falls through with no widget, no chat bubble, no harm done.
        pass

    try:
        settings = EcommerceSettings.objects.first()
        if not settings:
            # Return defaults if no settings exist
            return Response({
                'preset': 'default',
                'colors': {
                    'primary': '221 83% 53%',
                    'secondary': '215 16% 47%',
                    'accent': '221 83% 53%',
                    'background': '0 0% 100%',
                    'foreground': '0 0% 9%',
                    'muted': '0 0% 96%',
                    'muted_foreground': '0 0% 45%',
                    'destructive': '0 84.2% 60.2%',
                    'border': '0 0% 90%',
                    'card': '0 0% 100%',
                    'card_foreground': '0 0% 9%',
                },
                'radius': '0.5rem',
                'store_name': '',
                # Same default storefront block the populated branch ships, so
                # the storefront's `<TemplateSwitch>` doesn't crash on tenants
                # without an `EcommerceSettings` row yet.
                'storefront': {
                    'template': 'classic',
                    'voltage': {
                        'theme': 'refurb',
                        'mode': 'light',
                        'density': 'cozy',
                        'radius': 'soft',
                        'fontPair': 'bricolage-inter',
                    },
                },
                'chat_widget': chat_widget_payload,
            })

        return Response({
            'preset': settings.theme_preset,
            'colors': {
                'primary': settings.theme_primary_color,
                'secondary': settings.theme_secondary_color,
                'accent': settings.theme_accent_color,
                'background': settings.theme_background_color,
                'foreground': settings.theme_foreground_color,
                'muted': settings.theme_muted_color,
                'muted_foreground': settings.theme_muted_foreground_color,
                'destructive': settings.theme_destructive_color,
                'border': settings.theme_border_color,
                'card': settings.theme_card_color,
                'card_foreground': settings.theme_card_foreground_color,
            },
            'radius': settings.theme_border_radius,
            'store_name': settings.store_name,
            'payment': {
                'active_providers': settings.active_payment_providers or [],
                'enable_cash_on_delivery': settings.enable_cash_on_delivery,
                'enable_card_payment': settings.enable_card_payment,
                'currency': 'GEL',
                'tax_rate': str(settings.tax_rate) if settings.tax_rate else '0',
                'tax_label': settings.tax_label or 'VAT',
                'tax_inclusive': settings.tax_inclusive,
            },
            # Shipping integration flags. The storefront flips on the map
            # picker + live-quote step when `quickshipper_enabled` is true,
            # otherwise it falls back to the static ShippingMethod table.
            'shipping': {
                'quickshipper_enabled': bool(
                    settings.quickshipper_enabled
                    and settings.has_quickshipper_credentials
                ),
            },
            # Storefront visual template selection. Drives the
            # `<TemplateSwitch>` server component that picks between the
            # classic shadcn UI and the bold Voltage UI. Voltage tweaks
            # only apply when template == 'voltage'; serialised always so
            # the storefront doesn't need a second round-trip when the
            # tenant flips templates.
            'storefront': {
                'template': settings.storefront_template or 'classic',
                'voltage': {
                    'theme': settings.voltage_theme_preset or 'refurb',
                    'mode': settings.voltage_color_mode or 'light',
                    'density': settings.voltage_density or 'cozy',
                    'radius': settings.voltage_radius or 'soft',
                    'fontPair': settings.voltage_font_pair or 'bricolage-inter',
                },
            },
            # Embeddable chat widget — non-null token tells the storefront
            # to drop in <script src="…/widget.js?t=TOKEN"> on every page.
            'chat_widget': chat_widget_payload,
            # Google Ads / GA4 tracking. The storefront drops in gtag.js
            # only when conversion_id is set; fires `purchase` events on
            # order-confirmation only when both id + label are set.
            'analytics': {
                'google_ads_conversion_id': settings.google_ads_conversion_id or '',
                'google_ads_purchase_label': settings.google_ads_purchase_label or '',
                'google_analytics_id': settings.google_analytics_id or '',
                'clarity_project_id': settings.clarity_project_id or '',
            },
            # Google Maps API key — drives the map + autocomplete
            # experience on every address picker. Empty = use the
            # default Leaflet/OSM picker.
            'maps': {
                'google_maps_api_key': settings.google_maps_api_key or '',
            },
            # Pickup option — when allow_pickup is True the storefront
            # offers "Pickup at store" alongside courier delivery in
            # checkout step 1. The address block doubles as the receipt
            # for the customer to actually go pick up the order.
            'pickup': (
                {
                    'enabled': True,
                    'address': settings.quickshipper_pickup_address or '',
                    'city': settings.quickshipper_pickup_city or '',
                    'phone': settings.quickshipper_pickup_phone or '',
                    'contact_name': settings.quickshipper_pickup_contact_name or '',
                    'extra_instructions': settings.quickshipper_pickup_extra_instructions or '',
                }
                if settings.allow_pickup
                and settings.quickshipper_pickup_address
                and settings.quickshipper_pickup_city
                else {'enabled': False}
            ),
        })
    except Exception as e:
        return Response(
            {'error': f'Failed to fetch theme: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


class ClientShippingMethodViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Client-facing shipping methods (read-only, public access).
    Returns active shipping methods for order checkout.
    """
    serializer_class = ShippingMethodSerializer
    authentication_classes = []
    permission_classes = [AllowAny]
    filter_backends = [filters.OrderingFilter]
    ordering = ['position']

    def get_queryset(self):
        return ShippingMethod.objects.filter(is_active=True)

    @extend_schema(
        tags=['Ecommerce Client - Shipping'],
        summary='List available shipping methods',
        description='Get all active shipping methods for checkout'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Shipping'],
        summary='Get shipping method details'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)


@extend_schema(
    tags=['Ecommerce Client - Promo'],
    summary='Validate promo code',
    description='Validate a promo code and return the discount amount',
    request=inline_serializer(
        name='PromoValidateRequest',
        fields={
            'code': serializers.CharField(),
            'subtotal': serializers.DecimalField(max_digits=10, decimal_places=2),
        }
    ),
    responses={
        200: inline_serializer(
            name='PromoValidateResponse',
            fields={
                'valid': serializers.BooleanField(),
                'discount_amount': serializers.CharField(required=False),
                'discount_type': serializers.CharField(required=False),
                'discount_value': serializers.CharField(required=False),
                'message': serializers.CharField(),
            }
        )
    }
)
@api_view(['POST'])
@permission_classes([AllowAny])
def validate_promo_code(request):
    """
    Validate a promo code.
    POST /api/ecommerce/client/promo/validate/
    Body: {"code": "SAVE10", "subtotal": 100.00}
    """
    from decimal import Decimal, InvalidOperation

    code = request.data.get('code', '').strip()
    if not code:
        return Response(
            {'valid': False, 'message': 'Promo code is required.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        subtotal_raw = request.data.get('subtotal', 0)
        subtotal = Decimal(str(subtotal_raw))
    except (InvalidOperation, TypeError, ValueError):
        return Response(
            {'valid': False, 'message': 'Invalid subtotal value.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        promo = PromoCode.objects.get(code__iexact=code)
    except PromoCode.DoesNotExist:
        return Response({
            'valid': False,
            'message': 'Promo code not found.'
        })

    is_valid, message = promo.is_valid(subtotal=subtotal)
    if not is_valid:
        return Response({
            'valid': False,
            'message': message
        })

    discount_amount = promo.calculate_discount(subtotal)
    return Response({
        'valid': True,
        'discount_amount': str(discount_amount),
        'discount_type': promo.discount_type,
        'discount_value': str(promo.discount_value),
        'message': message,
    })


class ClientProductReviewViewSet(viewsets.ModelViewSet):
    """
    Client-facing product review management.
    List reviews (public), create reviews (authenticated clients only).
    """
    authentication_classes = [EcommerceClientJWTAuthentication]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    ordering_fields = ['created_at', 'rating']
    ordering = ['-created_at']

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_authentication_classes(self):
        if self.action in ['list', 'retrieve']:
            return []
        return [EcommerceClientJWTAuthentication]

    def get_queryset(self):
        product_id = self.kwargs.get('product_pk')
        qs = ProductReview.objects.filter(is_approved=True)
        if product_id:
            qs = qs.filter(product_id=product_id)
        return qs.select_related('client', 'product')

    def get_serializer_class(self):
        if self.action == 'create':
            return ProductReviewCreateSerializer
        return ProductReviewSerializer

    @extend_schema(
        tags=['Ecommerce Client - Reviews'],
        summary='List product reviews',
        description='Get all approved reviews for a product'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Client - Reviews'],
        summary='Create product review',
        description='Submit a review for a product. One review per product per client.'
    )
    def create(self, request, *args, **kwargs):
        product_id = self.kwargs.get('product_pk')

        try:
            product = Product.objects.get(id=product_id, status='active')
        except Product.DoesNotExist:
            return Response(
                {'error': 'Product not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Check if client already reviewed this product
        if ProductReview.objects.filter(product=product, client=request.user).exists():
            return Response(
                {'error': 'You have already reviewed this product'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Check if this is a verified purchase
        is_verified = OrderItem.objects.filter(
            order__client=request.user,
            product=product,
            order__status='delivered'
        ).exists()

        serializer.save(
            product=product,
            client=request.user,
            is_verified_purchase=is_verified,
        )

        return Response(serializer.data, status=status.HTTP_201_CREATED)


@extend_schema(
    operation_id='get_order_by_public_token',
    tags=['Ecommerce Client - Orders'],
    summary='Look up an order by public token',
    description=(
        'Public endpoint guests use to view their order confirmation + '
        'tracking without authenticating. The token is generated per '
        'order and emailed in the confirmation message; brute-forcing '
        'is impractical against a 256-bit URL-safe random string.'
    ),
    parameters=[
        OpenApiParameter(name='token', required=True, location=OpenApiParameter.QUERY, type=str),
    ],
    responses={
        200: OrderSerializer,
        404: OpenApiResponse(description='No order matches the token.'),
    },
)
@api_view(['GET'])
@permission_classes([AllowAny])
@authentication_classes([])
def get_order_by_public_token(request):
    token = (request.query_params.get('token') or '').strip()
    if not token:
        return Response({'error': 'token is required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        order = (
            Order.objects
            .select_related('client', 'delivery_address')
            .prefetch_related('items', 'items__product', 'items__variant')
            .get(public_token=token)
        )
    except Order.DoesNotExist:
        return Response({'error': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
    return Response(OrderSerializer(order).data)


@extend_schema(
    tags=['Ecommerce Client - Guest Checkout'],
    summary='Guest checkout',
    description='Place an order without authentication. Creates or finds a client by email.',
    request=inline_serializer(
        name='GuestCheckoutRequest',
        fields={
            'email': serializers.EmailField(),
            'first_name': serializers.CharField(),
            'last_name': serializers.CharField(),
            'phone': serializers.CharField(),
            'address': inline_serializer(
                name='GuestAddress',
                fields={
                    'address': serializers.CharField(),
                    'city': serializers.CharField(),
                    'label': serializers.CharField(required=False, default='Delivery'),
                },
            ),
            'items': serializers.ListField(
                child=inline_serializer(
                    name='GuestCheckoutItem',
                    fields={
                        'product_id': serializers.IntegerField(),
                        'quantity': serializers.IntegerField(),
                        'variant_id': serializers.IntegerField(required=False, allow_null=True),
                    },
                )
            ),
            'payment_method': serializers.CharField(required=False, default='cash_on_delivery'),
            'shipping_method_id': serializers.IntegerField(required=False, allow_null=True),
            'promo_code': serializers.CharField(required=False, allow_blank=True),
            'notes': serializers.CharField(required=False, allow_blank=True),
        },
    ),
    responses={
        201: OpenApiResponse(description='Order created successfully'),
        400: OpenApiResponse(description='Validation error'),
    },
)
@api_view(['POST'])
@permission_classes([AllowAny])
def guest_checkout(request):
    """Place order without authentication. Creates/finds client by email."""
    import uuid
    from django.db import transaction, connection
    from decimal import Decimal

    data = request.data

    # --- Validate required fields ---
    required_fields = ['email', 'first_name', 'last_name', 'phone', 'address', 'items']
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        return Response(
            {'error': f'Missing required fields: {", ".join(missing)}'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    items_data = data.get('items', [])
    if not items_data:
        return Response({'error': 'Items list cannot be empty.'}, status=status.HTTP_400_BAD_REQUEST)

    address_data = data.get('address', {})
    if not address_data.get('address') or not address_data.get('city'):
        return Response(
            {'error': 'Address must include "address" and "city".'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Parse lat/lng off the address payload. Stored as Decimal on
    # ClientAddress (max_digits=9, decimal_places=6), so we coerce here
    # and handle both numeric + stringified inputs from the storefront.
    raw_lat = address_data.get('latitude')
    raw_lng = address_data.get('longitude')
    address_lat = None
    address_lng = None
    if raw_lat is not None and raw_lng is not None:
        try:
            address_lat = Decimal(str(raw_lat))
            address_lng = Decimal(str(raw_lng))
        except (ValueError, TypeError):
            address_lat = None
            address_lng = None

    # --- Find or create EcommerceClient ---
    email = data['email'].strip().lower()
    client, created = EcommerceClient.objects.get_or_create(
        email=email,
        defaults={
            'first_name': data['first_name'],
            'last_name': data['last_name'],
            'phone_number': data['phone'],
            'password': '',
            'is_verified': True,
        },
    )
    if created:
        client.set_password(uuid.uuid4().hex)
        client.save(update_fields=['password'])

    # --- Create ClientAddress ---
    delivery_address = ClientAddress.objects.create(
        client=client,
        label=address_data.get('label', 'Delivery'),
        address=address_data['address'],
        city=address_data['city'],
        latitude=address_lat,
        longitude=address_lng,
    )

    # --- Validate items, compute subtotal ---
    order_items_to_create = []
    subtotal = Decimal('0')
    for item_data in items_data:
        product_id = item_data.get('product_id')
        quantity = item_data.get('quantity', 1)
        variant_id = item_data.get('variant_id')

        if not product_id or quantity < 1:
            return Response(
                {'error': 'Each item must have a valid product_id and quantity >= 1.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            product = Product.objects.get(id=product_id, status='active')
        except Product.DoesNotExist:
            return Response(
                {'error': f'Product with id {product_id} not found or inactive.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        variant = None
        if variant_id:
            try:
                from .models import ProductVariant
                variant = ProductVariant.objects.get(id=variant_id, product=product, is_active=True)
            except ProductVariant.DoesNotExist:
                return Response(
                    {'error': f'Variant with id {variant_id} not found for product {product_id}.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        price = variant.effective_price if variant else product.price
        subtotal += price * quantity
        order_items_to_create.append({
            'product': product,
            'variant': variant,
            'quantity': quantity,
            'price': price,
            'product_name': product.name,
        })

    # --- Promo code ---
    promo = None
    discount_amount = Decimal('0')
    promo_code_str = (data.get('promo_code') or '').strip()
    if promo_code_str:
        try:
            promo = PromoCode.objects.get(code__iexact=promo_code_str)
            is_valid, _msg = promo.is_valid(subtotal=subtotal)
            if is_valid:
                discount_amount = promo.calculate_discount(subtotal)
        except PromoCode.DoesNotExist:
            pass

    # --- Tenant config (used by pickup + tax + downstream BOG paths) ---
    ecommerce_settings = EcommerceSettings.objects.first()

    # --- Delivery method (pickup vs courier) ---
    # Storefront sends `delivery_method='pickup'` when the customer
    # chose to pick up at the store. Pickup is free and doesn't go
    # through Quickshipper. Validation:
    #   - pickup must be allowed by the tenant config
    #   - pickup + card OR pickup + cash on delivery — both fine
    #   - courier (default) + cash on delivery is rejected because we
    #     don't trust couriers with cash collection
    delivery_method = (data.get('delivery_method') or 'courier').strip().lower()
    if delivery_method not in ('pickup', 'courier'):
        delivery_method = 'courier'
    if delivery_method == 'pickup':
        if not (ecommerce_settings and ecommerce_settings.allow_pickup):
            return Response(
                {'error': 'Pickup is not enabled for this store.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # --- Shipping method ---
    shipping_method = None
    shipping_cost = Decimal('0')
    shipping_method_id = data.get('shipping_method_id')
    if shipping_method_id:
        try:
            shipping_method = ShippingMethod.objects.get(id=shipping_method_id, is_active=True)
            shipping_cost = shipping_method.get_effective_price(subtotal)
        except ShippingMethod.DoesNotExist:
            pass

    # --- Quickshipper-selected courier ---
    # When the storefront ran the live guest-quote flow it sends back
    # the price + provider IDs the customer picked. Use those to set
    # shipping_cost so the order totals match what the visitor saw,
    # and stash the meta for the booking task to honour later.
    quickshipper_quote_meta = None
    qs_price_raw = data.get('quickshipper_price')
    qs_provider_id = data.get('quickshipper_provider_id')
    qs_provider_fee_id = data.get('quickshipper_provider_fee_id')
    qs_parcel_dimensions_id = data.get('quickshipper_parcel_dimensions_id')
    qs_provider_name = data.get('quickshipper_provider_name')
    if qs_price_raw is not None and qs_provider_id is not None:
        try:
            shipping_cost = Decimal(str(qs_price_raw))
            quickshipper_quote_meta = {
                'provider_id': qs_provider_id,
                'provider_fee_id': qs_provider_fee_id,
                'provider_name': qs_provider_name,
                'parcel_dimensions_id': qs_parcel_dimensions_id,
                'price': float(qs_price_raw),
            }
        except (ValueError, TypeError):
            pass

    # --- Tax ---
    tax_amount = Decimal('0')
    if ecommerce_settings and ecommerce_settings.tax_rate > 0:
        taxable_amount = subtotal - discount_amount
        if ecommerce_settings.tax_inclusive:
            tax_amount = taxable_amount - (taxable_amount / (1 + ecommerce_settings.tax_rate / Decimal('100')))
        else:
            tax_amount = taxable_amount * ecommerce_settings.tax_rate / Decimal('100')
        tax_amount = tax_amount.quantize(Decimal('0.01'))

    # --- Total ---
    if tax_amount > 0 and ecommerce_settings and not ecommerce_settings.tax_inclusive:
        total_amount = subtotal - discount_amount + shipping_cost + tax_amount
    else:
        total_amount = subtotal - discount_amount + shipping_cost

    # --- Create order within transaction ---
    try:
        with transaction.atomic():
            # Stock validation & decrement
            for item_info in order_items_to_create:
                product = item_info['product']
                qty = item_info['quantity']
                if product.track_inventory:
                    # Re-fetch with lock
                    locked_product = Product.objects.select_for_update().get(pk=product.pk)
                    if locked_product.quantity < qty:
                        raise serializers.ValidationError(
                            f'Insufficient stock for {product.get_name("en")}. '
                            f'Available: {locked_product.quantity}, Requested: {qty}'
                        )
                    locked_product.quantity -= qty
                    locked_product.save(update_fields=['quantity'])

            order = Order.objects.create(
                order_number=Order.generate_order_number(),
                client=client,
                delivery_address=delivery_address,
                subtotal=subtotal,
                discount_amount=discount_amount,
                promo_code=promo,
                shipping_method=shipping_method,
                shipping_cost=shipping_cost,
                tax_amount=tax_amount,
                total_amount=total_amount,
                notes=data.get('notes', ''),
                status='pending',
                # Persist the Quickshipper quote (if present) so the
                # booking task books the exact option the visitor chose
                # at checkout, not a re-quoted cheapest. Mirrors the
                # authenticated OrderCreate flow. Also stamp the
                # delivery_method so the booking task can skip
                # Quickshipper booking on pickup orders.
                payment_metadata=({
                    **({'quickshipper_quote': quickshipper_quote_meta}
                       if quickshipper_quote_meta else {}),
                    'delivery_method': delivery_method,
                }),
            )

            for item_info in order_items_to_create:
                OrderItem.objects.create(
                    order=order,
                    product=item_info['product'],
                    variant=item_info['variant'],
                    product_name=item_info['product_name'],
                    quantity=item_info['quantity'],
                    price=item_info['price'],
                )

            if promo and discount_amount > 0:
                promo.times_used += 1
                promo.save(update_fields=['times_used'])

    except serializers.ValidationError as ve:
        return Response({'error': str(ve.detail)}, status=status.HTTP_400_BAD_REQUEST)

    # --- Payment handling ---
    payment_method = data.get('payment_method', 'cash_on_delivery')

    # Reject the cash-on-delivery + courier combination. Couriers can't
    # be trusted with cash collection in our setup — if the visitor
    # picked courier delivery they have to pay by card. (UI gates this
    # already; the backend check is belt-and-braces.)
    if payment_method == 'cash_on_delivery' and delivery_method == 'courier':
        return Response(
            {'error': 'Cash on delivery is only available for pickup orders. Choose card payment for courier delivery.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # --- Send confirmation email (COD only) ---
    # For card payments the BOG webhook fires the confirmation once
    # payment actually clears. Sending it here would mean the customer
    # gets an "Order Confirmed" email before they've paid.
    if payment_method == 'cash_on_delivery':
        try:
            from .tasks import send_order_email
            send_order_email.delay(connection.schema_name, order.id, 'confirmation')
        except Exception:
            pass

    if payment_method == 'cash_on_delivery':
        order.payment_method = 'cash_on_delivery'
        order.payment_status = 'pending'
        order.save(update_fields=['payment_method', 'payment_status'])

        output_serializer = OrderSerializer(order)
        response_data = output_serializer.data
        response_data['payment_method'] = 'cash_on_delivery'
        response_data['message'] = 'Order will be paid on delivery'
        return Response(response_data, status=status.HTTP_201_CREATED)

    # Card payment via BOG
    try:
        from django.conf import settings as django_settings
        from tenants.bog_payment import BOGPaymentService

        try:
            ec_settings = EcommerceSettings.objects.get(tenant=request.tenant)
            if ec_settings.has_bog_credentials:
                client_id = ec_settings.bog_client_id
                client_secret = ec_settings.get_bog_secret()
            else:
                client_id = django_settings.BOG_CLIENT_ID
                client_secret = django_settings.BOG_CLIENT_SECRET
        except Exception:
            client_id = django_settings.BOG_CLIENT_ID
            client_secret = django_settings.BOG_CLIENT_SECRET

        bog_service_instance = BOGPaymentService()
        bog_service_instance.client_id = client_id
        bog_service_instance.client_secret = client_secret
        bog_service_instance.auth_url = django_settings.BOG_AUTH_URL
        bog_service_instance.base_url = django_settings.BOG_API_BASE_URL

        callback_url = f"https://{request.get_host()}/api/ecommerce/payment-webhook/"

        try:
            ec_settings = EcommerceSettings.objects.get(tenant=request.tenant)
            return_url_success = ec_settings.bog_return_url_success or ''
            return_url_fail = ec_settings.bog_return_url_fail or ''
        except EcommerceSettings.DoesNotExist:
            return_url_success = ''
            return_url_fail = ''

        payment_result = bog_service_instance.create_payment(
            amount=float(order.total_amount),
            currency='GEL',
            description=f"Order {order.order_number}",
            customer_email=client.email,
            customer_name=client.full_name,
            customer_phone=client.phone_number or '',
            return_url_success=return_url_success,
            return_url_fail=return_url_fail,
            callback_url=callback_url,
            external_order_id=order.order_number,
            metadata={
                'order_id': order.id,
                'order_number': order.order_number,
                'tenant_id': request.tenant.id,
            },
        )

        order.bog_order_id = payment_result['order_id']
        order.payment_url = payment_result['payment_url']
        order.payment_status = 'pending'
        order.payment_method = 'card'
        order.payment_metadata = payment_result
        order.save()

        output_serializer = OrderSerializer(order)
        response_data = output_serializer.data
        response_data['payment_url'] = payment_result['payment_url']
        response_data['bog_order_id'] = payment_result['order_id']
        return Response(response_data, status=status.HTTP_201_CREATED)

    except Exception as e:
        output_serializer = OrderSerializer(order)
        response_data = output_serializer.data
        response_data['payment_error'] = str(e)
        response_data['message'] = 'Order created but payment initialization failed'
        return Response(response_data, status=status.HTTP_201_CREATED)

