from rest_framework import viewsets, filters, status, serializers
from rest_framework.decorators import action, api_view, permission_classes, authentication_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiParameter, inline_serializer
from django_filters.rest_framework import DjangoFilterBackend, FilterSet, CharFilter, NumberFilter, BooleanFilter
from django.db.models import Q, F, Count, Avg
from django.core.cache import cache
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from tenants.models import Tenant


class NoCacheMixin:
    """Mixin to prevent browser caching for admin viewsets"""

    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)
from .models import (
    Language,
    AttributeDefinition,
    Product,
    ProductImage,
    ProductVariant,
    ProductAttributeValue,
    EcommerceClient,
    ClientAddress,
    FavoriteProduct,
    Cart,
    CartItem,
    Order,
    OrderItem,
    EcommerceSettings,
    HomepageSection,
    ShippingMethod,
    PromoCode,
    ProductReview,
)
from .serializers import (
    LanguageSerializer,
    AttributeDefinitionSerializer,
    ProductListSerializer,
    ProductDetailSerializer,
    ProductCreateUpdateSerializer,
    ProductImageSerializer,
    ProductVariantSerializer,
    EcommerceClientSerializer,
    EcommerceClientListSerializer,
    ClientRegistrationSerializer,
    ClientLoginSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    ClientAddressSerializer,
    FavoriteProductSerializer,
    FavoriteProductCreateSerializer,
    CartSerializer,
    CartListSerializer,
    CartItemSerializer,
    CartItemCreateSerializer,
    OrderSerializer,
    OrderListSerializer,
    OrderCreateSerializer,
    EcommerceSettingsSerializer,
    HomepageSectionSerializer,
    HomepageSectionPublicSerializer,
    HomepageSectionReorderSerializer,
    ShippingMethodSerializer,
    PromoCodeSerializer,
    ProductReviewSerializer,
)


class LanguageViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """
    ViewSet for managing available languages
    """
    queryset = Language.objects.all()
    serializer_class = LanguageSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['sort_order', 'code']
    ordering = ['sort_order', 'code']

    def _clear_language_cache(self):
        """Clear all language-related caches"""
        # Clear all cache - simple approach for language management
        # Languages are rarely modified so this is acceptable
        cache.clear()

    @extend_schema(
        tags=['Ecommerce Admin - Languages'],
        summary='List all languages'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Languages'],
        summary='Get language details'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Languages'],
        summary='Create new language'
    )
    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        self._clear_language_cache()
        return response

    @extend_schema(
        tags=['Ecommerce Admin - Languages'],
        summary='Update language'
    )
    def update(self, request, *args, **kwargs):
        response = super().update(request, *args, **kwargs)
        self._clear_language_cache()
        return response

    @extend_schema(
        tags=['Ecommerce Admin - Languages'],
        summary='Partially update language'
    )
    def partial_update(self, request, *args, **kwargs):
        response = super().partial_update(request, *args, **kwargs)
        self._clear_language_cache()
        return response

    @extend_schema(
        tags=['Ecommerce Admin - Languages'],
        summary='Delete language'
    )
    def destroy(self, request, *args, **kwargs):
        response = super().destroy(request, *args, **kwargs)
        self._clear_language_cache()
        return response


class AttributeDefinitionViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """
    ViewSet for attribute definitions (Public access for frontend)
    """
    queryset = AttributeDefinition.objects.filter(is_active=True)
    serializer_class = AttributeDefinitionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter, DjangoFilterBackend]
    search_fields = ['key']
    filterset_fields = ['attribute_type', 'is_filterable']
    ordering_fields = ['sort_order', 'created_at']
    ordering = ['sort_order', 'id']

    @extend_schema(
        tags=['Ecommerce Admin - Attributes'],
        summary='List all product attributes'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Attributes'],
        summary='Get attribute details'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Attributes'],
        summary='Create new attribute'
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Attributes'],
        summary='Update attribute'
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Attributes'],
        summary='Partially update attribute'
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Attributes'],
        summary='Delete attribute'
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


class ProductFilter(FilterSet):
    """Custom filter for products with attribute filtering support"""
    min_price = NumberFilter(field_name='price', lookup_expr='gte')
    max_price = NumberFilter(field_name='price', lookup_expr='lte')
    search = CharFilter(method='search_filter')
    in_stock = BooleanFilter(method='filter_in_stock')
    low_stock = BooleanFilter(method='filter_low_stock')
    attributes = CharFilter(method='filter_by_attributes')

    class Meta:
        model = Product
        fields = ['status', 'is_featured']

    def search_filter(self, queryset, name, value):
        """Search across multiple fields"""
        return queryset.filter(
            Q(sku__icontains=value) |
            Q(slug__icontains=value)
        )

    def filter_in_stock(self, queryset, name, value):
        """Filter products that are in stock"""
        if value:
            return queryset.filter(
                Q(track_inventory=False) | Q(quantity__gt=0)
            )
        return queryset.filter(track_inventory=True, quantity=0)

    def filter_low_stock(self, queryset, name, value):
        """Filter products with low stock"""
        if value:
            return queryset.filter(
                track_inventory=True,
                quantity__lte=F('low_stock_threshold'),
                quantity__gt=0
            )
        return queryset

    def filter_by_attributes(self, queryset, name, value):
        """
        Filter products by attributes
        Format: ?attributes=color:red,size:large
        This filters products that have BOTH color=red AND size=large
        """
        if not value:
            return queryset

        # Parse attribute filters: "color:red,size:large"
        attribute_filters = []
        for attr_filter in value.split(','):
            if ':' not in attr_filter:
                continue

            key, val = attr_filter.split(':', 1)
            key = key.strip()
            val = val.strip()

            if key and val:
                attribute_filters.append((key, val))

        # Apply filters (AND logic - product must match all attributes)
        for attr_key, attr_value in attribute_filters:
            # Filter by attribute key (attribute definition) and value
            queryset = queryset.filter(
                attribute_values__attribute__key=attr_key,
                attribute_values__value_text__iexact=attr_value
            )

        return queryset.distinct()


class ProductViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """
    ViewSet for products with advanced filtering and sorting (Public access for frontend)
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ProductFilter
    search_fields = ['sku', 'slug']
    ordering_fields = ['price', 'quantity', 'created_at', 'updated_at', 'sku']
    ordering = ['-created_at']

    def get_queryset(self):
        """Optimize queryset with prefetch_related"""
        queryset = Product.objects.select_related(
            'created_by',
            'updated_by'
        ).prefetch_related(
            'images',
            'attribute_values__attribute',
            'variants__attribute_values__attribute'
        ).annotate(
            # Serializers read these instead of firing an AVG + COUNT per row.
            _avg_rating=Avg('reviews__rating', filter=Q(reviews__is_approved=True)),
            _review_count=Count('reviews', filter=Q(reviews__is_approved=True), distinct=True),
        )

        if self.action == 'list':
            status_filter = self.request.query_params.get('status')
            if not status_filter:
                queryset = queryset.filter(status='active')

        return queryset

    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action == 'list':
            return ProductListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return ProductCreateUpdateSerializer
        return ProductDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        language = self.request.query_params.get('language', 'en')
        context['language'] = language
        return context

    @extend_schema(
        tags=['Ecommerce Admin - Products'],
        summary='List all products',
        description='''Get a list of products with advanced filtering:
        - Filter by price range: ?min_price=10&max_price=100
        - Filter by status: ?status=active
        - Filter by stock: ?in_stock=true&low_stock=false
        - Filter by attributes: ?attributes=color:red,size:large
        - Search: ?search=keyword
        '''
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Products'],
        summary='Get product details',
        description='Retrieve detailed information about a specific product including variants and attributes'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Products'],
        summary='Create new product',
        description='Create a new product with multilingual support'
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Products'],
        summary='Update product',
        description='Update all fields of an existing product'
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Products'],
        summary='Partially update product',
        description='Update specific fields of an existing product'
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Products'],
        summary='Delete product'
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Products'],
        summary='Get featured products',
        description='Retrieve all products marked as featured'
    )
    @action(detail=False, methods=['get'])
    def featured(self, request):
        """Get featured products"""
        products = self.get_queryset().filter(is_featured=True, status='active')
        page = self.paginate_queryset(products)
        if page is not None:
            serializer = ProductListSerializer(page, many=True, context=self.get_serializer_context())
            return self.get_paginated_response(serializer.data)
        serializer = ProductListSerializer(products, many=True, context=self.get_serializer_context())
        return Response(serializer.data)

    @extend_schema(
        tags=['Ecommerce Admin - Products'],
        summary='Get low stock products',
        description='Retrieve all products with stock levels at or below their low stock threshold'
    )
    @action(detail=False, methods=['get'])
    def low_stock(self, request):
        """Get products with low stock"""
        products = self.get_queryset().filter(
            track_inventory=True,
            quantity__lte=F('low_stock_threshold'),
            quantity__gt=0
        )
        page = self.paginate_queryset(products)
        if page is not None:
            serializer = ProductListSerializer(page, many=True, context=self.get_serializer_context())
            return self.get_paginated_response(serializer.data)
        serializer = ProductListSerializer(products, many=True, context=self.get_serializer_context())
        return Response(serializer.data)

    @extend_schema(
        tags=['Ecommerce Admin - Products'],
        summary='Add image to product',
        description='Upload and attach an image to a specific product'
    )
    @action(detail=True, methods=['post'])
    def add_image(self, request, pk=None):
        """Add an image to a product"""
        product = self.get_object()
        serializer = ProductImageSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save(product=product)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        tags=['Ecommerce Admin - Products'],
        summary='Remove image from product',
        description='Delete a specific image from a product'
    )
    @action(detail=True, methods=['delete'], url_path='remove_image/(?P<image_id>[^/.]+)')
    def remove_image(self, request, pk=None, image_id=None):
        """Remove an image from a product"""
        product = self.get_object()
        try:
            image = product.images.get(id=image_id)
            image.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except ProductImage.DoesNotExist:
            return Response(
                {'error': 'Image not found'},
                status=status.HTTP_404_NOT_FOUND
            )

    @extend_schema(
        tags=['Ecommerce Admin - Products'],
        summary='Update product attributes',
        description='Update all attributes for a specific product'
    )
    @action(detail=True, methods=['post'])
    def update_attributes(self, request, pk=None):
        """Update product attributes"""
        product = self.get_object()
        attributes_data = request.data.get('attributes', [])

        # Clear existing attributes
        product.attribute_values.all().delete()

        # Create new attribute values
        for attr_data in attributes_data:
            attribute_id = attr_data.get('attribute_id')
            if attribute_id:
                attr_value = ProductAttributeValue(
                    product=product,
                    attribute_id=attribute_id
                )
                if 'value_text' in attr_data:
                    attr_value.value_text = attr_data['value_text']
                elif 'value_number' in attr_data:
                    attr_value.value_number = attr_data['value_number']
                elif 'value_boolean' in attr_data:
                    attr_value.value_boolean = attr_data['value_boolean']
                elif 'value_date' in attr_data:
                    attr_value.value_date = attr_data['value_date']
                elif 'value_json' in attr_data:
                    attr_value.value_json = attr_data['value_json']
                attr_value.save()

        # Return updated product detail
        serializer = ProductDetailSerializer(product, context=self.get_serializer_context())
        return Response(serializer.data)

    @extend_schema(
        tags=['Ecommerce Admin - Products'],
        summary='Bulk update product status',
        description='Update the status of multiple products at once',
        request=inline_serializer(
            name='BulkProductUpdateRequest',
            fields={
                'product_ids': serializers.ListField(child=serializers.IntegerField()),
                'status': serializers.CharField(),
            },
        ),
        responses={
            200: inline_serializer(
                name='BulkProductUpdateResponse',
                fields={
                    'updated': serializers.IntegerField(),
                },
            ),
            400: OpenApiResponse(description='Validation error'),
        },
    )
    @action(detail=False, methods=['post'], url_path='bulk-update')
    def bulk_update(self, request):
        """Bulk update product status"""
        product_ids = request.data.get('product_ids', [])
        status_value = request.data.get('status')

        if not product_ids or not status_value:
            return Response({'error': 'product_ids and status are required'}, status=status.HTTP_400_BAD_REQUEST)

        updated = Product.objects.filter(id__in=product_ids).update(status=status_value)
        return Response({'updated': updated})


class ProductImageViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """ViewSet for product images (Admin only)"""
    queryset = ProductImage.objects.all()
    serializer_class = ProductImageSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['product']
    ordering_fields = ['sort_order', 'created_at']
    ordering = ['sort_order', 'id']


class ProductVariantViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """ViewSet for product variants (Admin only)"""
    queryset = ProductVariant.objects.filter(is_active=True)
    serializer_class = ProductVariantSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['product', 'is_active']
    search_fields = ['sku']
    ordering_fields = ['sort_order', 'price', 'quantity', 'created_at']
    ordering = ['sort_order', 'id']

    def get_queryset(self):
        return super().get_queryset().select_related('product').prefetch_related(
            'attribute_values__attribute'
        )


class EcommerceClientViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """ViewSet for managing ecommerce clients (Public access for frontend)"""
    queryset = EcommerceClient.objects.all()
    serializer_class = EcommerceClientSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['first_name', 'last_name', 'email', 'phone_number']
    filterset_fields = ['is_active', 'is_verified']
    ordering_fields = ['created_at', 'last_login', 'first_name', 'last_name']
    ordering = ['-created_at']

    def get_queryset(self):
        """Optimize queryset with prefetch_related for related data"""
        queryset = super().get_queryset()
        # Only prefetch related data for detail views
        if self.action != 'list':
            queryset = queryset.prefetch_related(
                'addresses',
                'favorites__product__attribute_values__attribute',
                'favorites__product__images',
            )
        return queryset

    def get_serializer_class(self):
        """Return lightweight serializer for list, full serializer for detail"""
        if self.action == 'list':
            return EcommerceClientListSerializer
        return EcommerceClientSerializer

    @extend_schema(
        tags=['Ecommerce Admin - Clients'],
        summary='List all ecommerce clients'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Clients'],
        summary='Get client details'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Clients'],
        summary='Update client'
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Clients'],
        summary='Partially update client'
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Clients'],
        summary='Delete client'
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


@extend_schema(
    operation_id='register_client',
    summary='Register a new ecommerce client',
    description='Create a new ecommerce client account. Returns a verification token that should be used with the verification code sent via email.',
    request=ClientRegistrationSerializer,
    responses={
        201: OpenApiResponse(
            description='Client registered successfully. Verification code sent to email.',
            response=inline_serializer(
                'ClientRegistrationResponse',
                fields={
                    'client': EcommerceClientSerializer(),
                    'verification_token': serializers.CharField(help_text='Token to use for email verification'),
                    'message': serializers.CharField(),
                }
            )
        ),
        400: OpenApiResponse(description='Validation error')
    },
    tags=['Ecommerce - Client Auth']
)
@api_view(['POST'])
@permission_classes([AllowAny])
def register_client(request):
    """Register a new ecommerce client and send verification code"""
    import random
    import secrets
    from datetime import timedelta
    from django.utils import timezone
    from .email_utils import send_verification_code_email
    from .models import ClientVerificationCode

    serializer = ClientRegistrationSerializer(data=request.data)

    if serializer.is_valid():
        client = serializer.save()

        # Generate 6-digit verification code
        verification_code = ''.join([str(random.randint(0, 9)) for _ in range(6)])

        # Generate unique verification token
        verification_token = secrets.token_urlsafe(32)

        # Set expiration time (15 minutes from now)
        expires_at = timezone.now() + timedelta(minutes=15)

        # Store verification code
        ClientVerificationCode.objects.create(
            email=client.email,
            code=verification_code,
            token=verification_token,
            expires_at=expires_at
        )

        # Send verification code email
        send_verification_code_email(
            email=client.email,
            code=verification_code,
            client_name=client.first_name
        )

        response_serializer = EcommerceClientSerializer(client)
        return Response({
            'client': response_serializer.data,
            'verification_token': verification_token,
            'message': 'Registration successful. Please check your email for verification code.'
        }, status=status.HTTP_201_CREATED)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    operation_id='login_client',
    summary='Login ecommerce client',
    description='Authenticate a client using email or phone number with password. Returns JWT access and refresh tokens.',
    request=ClientLoginSerializer,
    responses={
        200: OpenApiResponse(
            description='Login successful',
            response=EcommerceClientSerializer
        ),
        400: OpenApiResponse(description='Invalid credentials or validation error')
    },
    tags=['Ecommerce - Client Auth']
)
@api_view(['POST'])
@permission_classes([AllowAny])
def login_client(request):
    """Login ecommerce client with email or phone"""
    from rest_framework_simplejwt.tokens import RefreshToken

    serializer = ClientLoginSerializer(data=request.data)

    if serializer.is_valid():
        client = serializer.validated_data['client']

        # Generate JWT tokens
        refresh = RefreshToken()
        refresh['client_id'] = client.id
        refresh['email'] = client.email

        response_serializer = EcommerceClientSerializer(client)
        return Response({
            'message': 'Login successful',
            'client': response_serializer.data,
            'access': str(refresh.access_token),
            'refresh': str(refresh)
        }, status=status.HTTP_200_OK)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    operation_id='refresh_client_token',
    summary='Refresh client access token',
    description='Use refresh token to get a new access token',
    request={
        'application/json': {
            'type': 'object',
            'properties': {
                'refresh': {'type': 'string', 'description': 'Refresh token'}
            },
            'required': ['refresh']
        }
    },
    responses={
        200: {
            'type': 'object',
            'properties': {
                'access': {'type': 'string', 'description': 'New access token'},
                'refresh': {'type': 'string', 'description': 'New refresh token'}
            }
        },
        401: {'description': 'Invalid or expired refresh token'}
    },
    tags=['Ecommerce - Auth']
)
@api_view(['POST'])
@permission_classes([AllowAny])
def refresh_client_token(request):
    """Refresh client access token using refresh token"""
    from rest_framework_simplejwt.tokens import RefreshToken
    from rest_framework_simplejwt.exceptions import TokenError, InvalidToken

    refresh_token = request.data.get('refresh')

    if not refresh_token:
        return Response({
            'error': 'Refresh token is required'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Validate the refresh token
        refresh = RefreshToken(refresh_token)

        # Check if it contains client_id (ecommerce client token)
        client_id = refresh.get('client_id')
        if not client_id:
            return Response({
                'error': 'Invalid token type'
            }, status=status.HTTP_401_UNAUTHORIZED)

        # Verify client still exists and is active
        try:
            from .models import EcommerceClient
            client = EcommerceClient.objects.get(id=client_id, is_active=True)
        except EcommerceClient.DoesNotExist:
            return Response({
                'error': 'Client not found or inactive'
            }, status=status.HTTP_401_UNAUTHORIZED)

        # Generate new tokens
        new_refresh = RefreshToken()
        new_refresh['client_id'] = client.id
        new_refresh['email'] = client.email

        return Response({
            'access': str(new_refresh.access_token),
            'refresh': str(new_refresh)
        }, status=status.HTTP_200_OK)

    except (TokenError, InvalidToken):
        return Response({
            'error': 'Invalid or expired refresh token'
        }, status=status.HTTP_401_UNAUTHORIZED)
    except Exception:
        # Catch-all so a malformed/corrupt token never escapes as a 500.
        # Anything that isn't a clean refresh attempt should bounce the
        # caller back to /login (401) so they can re-authenticate. The
        # storefront's axios refresh interceptor already maps 401 to
        # "clear local tokens, redirect to /login" — a 500 leaks the
        # exception trace and the interceptor gets stuck retrying.
        import logging
        logging.getLogger(__name__).warning(
            'Unexpected error refreshing client token', exc_info=True
        )
        return Response({
            'error': 'Invalid or expired refresh token'
        }, status=status.HTTP_401_UNAUTHORIZED)


@extend_schema(
    operation_id='logout_client',
    summary='Logout client',
    description='Invalidate the client refresh token to logout. The frontend should also clear stored tokens.',
    request={
        'application/json': {
            'type': 'object',
            'properties': {
                'refresh': {'type': 'string', 'description': 'Refresh token to invalidate'}
            },
            'required': ['refresh']
        }
    },
    responses={
        200: {
            'type': 'object',
            'properties': {
                'message': {'type': 'string', 'description': 'Logout success message'}
            }
        },
        400: {'description': 'Refresh token is required'},
        401: {'description': 'Invalid or expired refresh token'}
    },
    tags=['Ecommerce - Client Auth']
)
@api_view(['POST'])
@permission_classes([AllowAny])
def logout_client(request):
    """Logout ecommerce client by invalidating refresh token"""
    from rest_framework_simplejwt.tokens import RefreshToken
    from rest_framework_simplejwt.exceptions import TokenError, InvalidToken

    refresh_token = request.data.get('refresh')

    if not refresh_token:
        return Response({
            'error': 'Refresh token is required'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Validate the refresh token
        token = RefreshToken(refresh_token)

        # Check if it's a client token
        client_id = token.get('client_id')
        if not client_id:
            return Response({
                'error': 'Invalid token type'
            }, status=status.HTTP_401_UNAUTHORIZED)

        # Blacklist the token (invalidate it)
        try:
            token.blacklist()
        except AttributeError:
            # Token blacklisting is not enabled, but we'll still return success
            # The frontend should clear its stored tokens
            pass

        return Response({
            'message': 'Logout successful'
        }, status=status.HTTP_200_OK)

    except (TokenError, InvalidToken) as e:
        return Response({
            'error': 'Invalid or expired refresh token'
        }, status=status.HTTP_401_UNAUTHORIZED)


@extend_schema(
    operation_id='verify_email',
    summary='Verify email with code',
    description='Verify client email using the verification token and code sent via email. Returns JWT tokens upon successful verification.',
    request=inline_serializer(
        'EmailVerificationRequest',
        fields={
            'verification_token': serializers.CharField(help_text='Token received during registration'),
            'code': serializers.CharField(help_text='6-digit code received via email'),
        }
    ),
    responses={
        200: OpenApiResponse(
            description='Email verified successfully',
            response=inline_serializer(
                'EmailVerificationResponse',
                fields={
                    'message': serializers.CharField(),
                    'client': EcommerceClientSerializer(),
                    'access': serializers.CharField(help_text='JWT access token'),
                    'refresh': serializers.CharField(help_text='JWT refresh token'),
                }
            )
        ),
        400: OpenApiResponse(description='Invalid or expired code')
    },
    tags=['Ecommerce - Client Auth']
)
@api_view(['POST'])
@permission_classes([AllowAny])
def verify_email(request):
    """Verify client email with verification code"""
    from .models import ClientVerificationCode, EcommerceClient
    from django.utils import timezone
    from rest_framework_simplejwt.tokens import RefreshToken

    verification_token = request.data.get('verification_token')
    code = request.data.get('code')

    if not verification_token or not code:
        return Response({
            'error': 'Both verification_token and code are required'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Find verification code record
        verification = ClientVerificationCode.objects.get(
            token=verification_token,
            code=code
        )

        # Check if code is valid
        if not verification.is_valid():
            return Response({
                'error': 'Verification code is invalid or has expired'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Mark code as used
        verification.is_used = True
        verification.save()

        # Get the client and mark as verified
        try:
            client = EcommerceClient.objects.get(email=verification.email)
            client.is_verified = True
            client.save()

            # Generate JWT tokens
            refresh = RefreshToken()
            refresh['client_id'] = client.id
            refresh['email'] = client.email

            response_serializer = EcommerceClientSerializer(client)
            return Response({
                'message': 'Email verified successfully',
                'client': response_serializer.data,
                'access': str(refresh.access_token),
                'refresh': str(refresh)
            }, status=status.HTTP_200_OK)

        except EcommerceClient.DoesNotExist:
            return Response({
                'error': 'Client not found'
            }, status=status.HTTP_404_NOT_FOUND)

    except ClientVerificationCode.DoesNotExist:
        return Response({
            'error': 'Invalid verification token or code'
        }, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    operation_id='resend_verification_code',
    summary='Resend verification code',
    description='Resend verification code to client email. Use this if the client did not receive the initial verification email.',
    request=inline_serializer(
        'ResendVerificationCodeRequest',
        fields={
            'email': serializers.EmailField(help_text='Email address used during registration'),
        }
    ),
    responses={
        200: OpenApiResponse(
            description='Verification code resent successfully',
            response=inline_serializer(
                'ResendVerificationCodeResponse',
                fields={
                    'verification_token': serializers.CharField(help_text='New verification token to use'),
                    'message': serializers.CharField(),
                }
            )
        ),
        400: OpenApiResponse(description='Email already verified or client not found'),
        429: OpenApiResponse(description='Too many requests - please wait before requesting another code')
    },
    tags=['Ecommerce - Client Auth']
)
@api_view(['POST'])
@permission_classes([AllowAny])
def resend_verification_code(request):
    """Resend verification code to client email"""
    import random
    import secrets
    from datetime import timedelta
    from django.utils import timezone
    from .email_utils import send_verification_code_email
    from .models import ClientVerificationCode, EcommerceClient

    email = request.data.get('email')

    if not email:
        return Response({
            'error': 'Email is required'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Check if client exists
        client = EcommerceClient.objects.get(email=email)

        # Check if already verified
        if client.is_verified:
            return Response({
                'error': 'Email is already verified'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Rate limiting: Check if a code was sent recently (within last 1 minute)
        recent_code = ClientVerificationCode.objects.filter(
            email=email,
            created_at__gte=timezone.now() - timedelta(minutes=1)
        ).order_by('-created_at').first()

        if recent_code:
            return Response({
                'error': 'A verification code was recently sent. Please wait before requesting another one.',
                'retry_after': 60  # seconds
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Generate new 6-digit verification code
        verification_code = ''.join([str(random.randint(0, 9)) for _ in range(6)])

        # Generate unique verification token
        verification_token = secrets.token_urlsafe(32)

        # Set expiration time (15 minutes from now)
        expires_at = timezone.now() + timedelta(minutes=15)

        # Store verification code
        ClientVerificationCode.objects.create(
            email=client.email,
            code=verification_code,
            token=verification_token,
            expires_at=expires_at
        )

        # Send verification code email
        send_verification_code_email(
            email=client.email,
            code=verification_code,
            client_name=client.first_name
        )

        return Response({
            'verification_token': verification_token,
            'message': 'Verification code has been resent. Please check your email.'
        }, status=status.HTTP_200_OK)

    except EcommerceClient.DoesNotExist:
        return Response({
            'error': 'No account found with this email address'
        }, status=status.HTTP_404_NOT_FOUND)


@extend_schema(
    operation_id='password_reset_request',
    summary='Request password reset',
    description='Send a password reset email to the client',
    request=PasswordResetRequestSerializer,
    responses={
        200: OpenApiResponse(description='Password reset email sent'),
        400: OpenApiResponse(description='Validation error')
    },
    tags=['Ecommerce - Client Auth']
)
@api_view(['POST'])
@permission_classes([AllowAny])
def request_password_reset(request):
    """Request password reset for ecommerce client"""
    from .email_utils import send_password_reset_email
    from .models import PasswordResetToken
    from django.utils import timezone
    from datetime import timedelta

    serializer = PasswordResetRequestSerializer(data=request.data)

    if serializer.is_valid():
        client = serializer.context.get('client')

        # Generate 6-digit verification code
        code = PasswordResetToken.generate_code()
        expires_at = timezone.now() + timedelta(hours=1)  # Code expires in 1 hour

        # Invalidate any existing unused codes for this client
        PasswordResetToken.objects.filter(
            client=client,
            is_used=False
        ).update(is_used=True)

        # Create password reset token with code
        reset_token = PasswordResetToken.objects.create(
            client=client,
            token=code,
            expires_at=expires_at
        )

        # Send password reset email with code
        send_password_reset_email(client, code)

        return Response({
            'message': 'Verification code sent to your email. Please check your inbox.'
        }, status=status.HTTP_200_OK)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    operation_id='password_reset_confirm',
    summary='Confirm password reset',
    description='Reset password using the 6-digit verification code received via email',
    request=PasswordResetConfirmSerializer,
    responses={
        200: OpenApiResponse(description='Password reset successful'),
        400: OpenApiResponse(description='Invalid code or validation error')
    },
    tags=['Ecommerce - Client Auth']
)
@api_view(['POST'])
@permission_classes([AllowAny])
def confirm_password_reset(request):
    """Confirm password reset with verification code"""
    serializer = PasswordResetConfirmSerializer(data=request.data)

    if serializer.is_valid():
        reset_token = serializer.validated_data['reset_token']
        new_password = serializer.validated_data['new_password']

        # Update client password
        client = reset_token.client
        client.set_password(new_password)
        client.save()

        # Mark code as used
        reset_token.mark_as_used()

        return Response({
            'message': 'Password has been reset successfully. You can now login with your new password.'
        }, status=status.HTTP_200_OK)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    operation_id='get_current_client',
    summary='Get current client profile',
    description='Retrieve the authenticated client\'s profile information',
    responses={
        200: OpenApiResponse(
            description='Client profile',
            response=EcommerceClientSerializer
        ),
        401: OpenApiResponse(description='Not authenticated')
    },
    tags=['Ecommerce - Client Auth']
)
@api_view(['GET'])
@authentication_classes([])  # Bypass global authentication
@permission_classes([AllowAny])
def get_current_client(request):
    """Get current authenticated client profile"""
    from rest_framework_simplejwt.tokens import AccessToken
    from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
    from .models import EcommerceClient

    # Extract token from Authorization header
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return Response(
            {'error': 'Authentication credentials were not provided.'},
            status=status.HTTP_401_UNAUTHORIZED
        )

    token_string = auth_header.split(' ')[1]

    try:
        # Decode the JWT token
        token = AccessToken(token_string)

        # Extract client_id from token
        client_id = token.get('client_id')
        if not client_id:
            return Response(
                {'error': 'Token does not contain client_id.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Get the client
        try:
            client = EcommerceClient.objects.get(id=client_id)
            serializer = EcommerceClientSerializer(client)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except EcommerceClient.DoesNotExist:
            return Response(
                {'error': 'Client not found.'},
                status=status.HTTP_404_NOT_FOUND
            )

    except (InvalidToken, TokenError) as e:
        return Response(
            {'error': f'Invalid token: {str(e)}'},
            status=status.HTTP_401_UNAUTHORIZED
        )


class ClientAddressViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """ViewSet for managing client addresses (Admin only)"""
    queryset = ClientAddress.objects.all()
    serializer_class = ClientAddressSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['is_default', 'client']
    ordering_fields = ['created_at', 'is_default']
    ordering = ['-is_default', '-created_at']

    @extend_schema(
        tags=['Ecommerce Admin - Client Addresses'],
        summary='List client addresses',
        description='Get all delivery addresses (admin only)'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Client Addresses'],
        summary='Get address details',
        description='Retrieve detailed information about a specific address'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Client Addresses'],
        summary='Create new address',
        description='Add a new delivery address (admin only, client ID required)'
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Client Addresses'],
        summary='Update address',
        description='Update all fields of an existing address'
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Client Addresses'],
        summary='Partially update address',
        description='Update specific fields of an existing address'
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Client Addresses'],
        summary='Delete address',
        description='Remove an address from a client'
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Client Addresses'],
        summary='Set address as default',
        description='Mark a specific address as the default delivery address for the client'
    )
    @action(detail=True, methods=['post'])
    def set_default(self, request, pk=None):
        """Set an address as the default for the client"""
        address = self.get_object()
        address.is_default = True
        address.save()
        serializer = self.get_serializer(address)
        return Response(serializer.data)


class FavoriteProductViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """ViewSet for managing client favorite products/wishlist (Admin only)"""
    queryset = FavoriteProduct.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['client', 'product']
    ordering_fields = ['created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        """Optimize queryset with select_related and prefetch_related"""
        return super().get_queryset().select_related(
            'client',
            'product'
        ).prefetch_related(
            'product__images',
            'product__attribute_values__attribute',
        )

    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action in ['create']:
            return FavoriteProductCreateSerializer
        return FavoriteProductSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        language = self.request.query_params.get('language', 'en')
        context['language'] = language
        return context

    @extend_schema(
        tags=['Ecommerce Admin - Favorites'],
        summary='List favorite products',
        description='Get all favorite products with optional filtering by client or product'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Favorites'],
        summary='Get favorite details',
        description='Retrieve details of a specific favorite item'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Favorites'],
        summary='Add product to favorites',
        description='Add a product to client\'s favorites/wishlist'
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Favorites'],
        summary='Remove from favorites',
        description='Remove a product from favorites/wishlist'
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Favorites'],
        summary='Check if product is favorited',
        description='Check if a specific product is in client\'s favorites',
        parameters=[
            OpenApiParameter(name='client', type=int, required=True),
            OpenApiParameter(name='product', type=int, required=True),
        ]
    )
    @action(detail=False, methods=['get'])
    def is_favorited(self, request):
        """Check if a product is in client's favorites"""
        client_id = request.query_params.get('client')
        product_id = request.query_params.get('product')

        if not client_id or not product_id:
            return Response(
                {'error': 'Both client and product IDs are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        is_favorited = FavoriteProduct.objects.filter(
            client_id=client_id,
            product_id=product_id
        ).exists()

        return Response({'is_favorited': is_favorited})

    @extend_schema(
        tags=['Ecommerce Admin - Favorites'],
        summary='Toggle favorite',
        description='Add or remove a product from favorites',
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'client': {'type': 'integer'},
                    'product': {'type': 'integer'},
                }
            }
        }
    )
    @action(detail=False, methods=['post'])
    def toggle(self, request):
        """Toggle product in favorites (add if not exists, remove if exists)"""
        client_id = request.data.get('client')
        product_id = request.data.get('product')

        if not client_id or not product_id:
            return Response(
                {'error': 'Both client and product IDs are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        favorite, created = FavoriteProduct.objects.get_or_create(
            client_id=client_id,
            product_id=product_id
        )

        if not created:
            # Already exists, so remove it
            favorite.delete()
            return Response({
                'message': 'Removed from favorites',
                'is_favorited': False
            })
        else:
            # Newly created
            serializer = self.get_serializer(favorite)
            return Response({
                'message': 'Added to favorites',
                'is_favorited': True,
                'favorite': serializer.data
            }, status=status.HTTP_201_CREATED)


class CartViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """ViewSet for managing shopping carts (Admin only)"""
    queryset = Cart.objects.all()
    serializer_class = CartSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['client', 'status']
    ordering_fields = ['created_at', 'updated_at']
    ordering = ['-updated_at']

    def get_queryset(self):
        """Optimize queryset with select_related and prefetch_related"""
        queryset = super().get_queryset().select_related(
            'client',
            'delivery_address'
        )
        if self.action == 'list':
            queryset = queryset.annotate(
                _items_count=Count('items'),
            )
        else:
            queryset = queryset.prefetch_related(
                'items__product__images',
                'items__product__attribute_values__attribute',
                'items__variant',
                'items__variant__attribute_values__attribute',
            )
        return queryset

    def get_serializer_class(self):
        """Return lightweight serializer for list, full serializer for detail"""
        if self.action == 'list':
            return CartListSerializer
        return CartSerializer

    @extend_schema(
        tags=['Ecommerce Admin - Cart'],
        summary='List all carts',
        description='Get all shopping carts with optional filtering'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Cart'],
        summary='Get cart details',
        description='Retrieve cart with all items and totals'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Cart'],
        summary='Create new cart'
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Cart'],
        summary='Update cart',
        description='Update cart details (e.g., delivery address, notes)'
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Cart'],
        summary='Delete cart'
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Cart'],
        summary='Set delivery address',
        description='Set or update delivery address for cart'
    )
    @action(detail=True, methods=['post'])
    def set_address(self, request, pk=None):
        """Set delivery address for cart"""
        cart = self.get_object()
        address_id = request.data.get('address_id')
        
        if not address_id:
            return Response({'error': 'Address ID required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            address = ClientAddress.objects.get(id=address_id, client=cart.client)
            cart.delivery_address = address
            cart.save()
            serializer = self.get_serializer(cart)
            return Response(serializer.data)
        except ClientAddress.DoesNotExist:
            return Response({'error': 'Address not found'}, status=status.HTTP_404_NOT_FOUND)

    @extend_schema(
        tags=['Ecommerce Admin - Cart'],
        summary='Clear cart',
        description='Remove all items from cart'
    )
    @action(detail=True, methods=['post'])
    def clear(self, request, pk=None):
        """Clear all items from cart"""
        cart = self.get_object()
        cart.items.all().delete()
        serializer = self.get_serializer(cart)
        return Response(serializer.data)


class CartItemViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """ViewSet for managing cart items (Admin only)"""
    queryset = CartItem.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['cart', 'product']

    def get_queryset(self):
        """Optimize queryset with select_related and prefetch_related"""
        return super().get_queryset().select_related(
            'cart',
            'product',
            'variant'
        ).prefetch_related(
            'product__attribute_values__attribute',
            'variant__attribute_values__attribute',
        )

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return CartItemCreateSerializer
        return CartItemSerializer

    @extend_schema(
        tags=['Ecommerce Admin - Cart Items'],
        summary='List cart items'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Cart Items'],
        summary='Add item to cart',
        description='Add a product to shopping cart'
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Cart Items'],
        summary='Update cart item',
        description='Update quantity or variant'
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Cart Items'],
        summary='Remove item from cart'
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


class OrderViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """ViewSet for managing orders (Admin only)"""
    queryset = Order.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['order_number', 'client__first_name', 'client__last_name', 'client__email']
    filterset_fields = ['client', 'status']
    ordering_fields = ['created_at', 'total_amount', 'status']
    ordering = ['-created_at']

    def get_queryset(self):
        """Optimize queryset with select_related and prefetch_related"""
        queryset = super().get_queryset().select_related(
            'client',
            'delivery_address'
        )
        # Only prefetch items for detail views. For the list, annotate the item
        # count so the serializer doesn't fire a COUNT per order row.
        if self.action == 'list':
            queryset = queryset.annotate(_items_count=Count('items'))
        else:
            queryset = queryset.prefetch_related(
                'items__product',
                'items__variant'
            )
        return queryset

    def get_serializer_class(self):
        if self.action == 'list':
            return OrderListSerializer
        if self.action == 'create':
            return OrderCreateSerializer
        return OrderSerializer

    @extend_schema(
        tags=['Ecommerce Admin - Orders'],
        summary='List all orders',
        description='Get all orders with filtering and search'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Orders'],
        summary='Get order details',
        description='Retrieve order with all items and client info'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Orders'],
        summary='Create order from cart',
        description='Submit cart and create order with automatic BOG payment URL generation'
    )
    def create(self, request, *args, **kwargs):
        from tenants.bog_payment import bog_service
        from django.conf import settings

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = serializer.save()

        # Automatically generate payment URL
        payment_method = request.data.get('payment_method', 'card')

        if payment_method == 'cash_on_delivery':
            # No BOG payment needed, just mark as COD
            order.payment_method = 'cash_on_delivery'
            order.payment_status = 'pending'
            order.save()

            output_serializer = OrderSerializer(order)
            response_data = output_serializer.data
            response_data['payment_method'] = 'cash_on_delivery'
            response_data['message'] = 'Order will be paid on delivery'
            return Response(response_data, status=status.HTTP_201_CREATED)

        # Build the BOG service with the tenant's own merchant credentials (or
        # the platform's when unset). If the tenant HAS credentials but they
        # can't be loaded, refuse rather than silently using the platform account.
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

        # Create BOG payment
        try:
            callback_url = f"{request.scheme}://{request.get_host()}/api/ecommerce/payment-webhook/"
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
            # If payment creation fails, return order without payment URL
            # User can try to initiate payment later
            output_serializer = OrderSerializer(order)
            response_data = output_serializer.data
            response_data['payment_error'] = str(e)
            response_data['message'] = 'Order created but payment initialization failed'
            return Response(response_data, status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=['Ecommerce Admin - Orders'],
        summary='Update order',
        description='Update order status, notes, etc.'
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Orders'],
        summary='Update order status',
        description='Change order status (pending, confirmed, shipped, etc.). '
                    'Optionally include tracking_number, courier_provider, estimated_delivery_date '
                    'when setting status to shipped.'
    )
    @action(detail=True, methods=['post'])
    def update_status(self, request, pk=None):
        """Update order status and trigger appropriate email notifications"""
        from django.utils import timezone
        from django.db import connection

        order = self.get_object()
        new_status = request.data.get('status')

        if not new_status:
            return Response({'error': 'Status required'}, status=status.HTTP_400_BAD_REQUEST)

        if new_status not in dict(Order.STATUS_CHOICES):
            return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)

        order.status = new_status

        # Update timestamps based on status
        if new_status == 'confirmed' and not order.confirmed_at:
            order.confirmed_at = timezone.now()
        elif new_status == 'processing' and not order.processing_at:
            order.processing_at = timezone.now()
        elif new_status == 'shipped' and not order.shipped_at:
            order.shipped_at = timezone.now()
            # Allow setting shipping details when marking as shipped
            tracking_number = request.data.get('tracking_number')
            courier_provider = request.data.get('courier_provider')
            estimated_delivery_date = request.data.get('estimated_delivery_date')
            if tracking_number:
                order.tracking_number = tracking_number
            if courier_provider:
                order.courier_provider = courier_provider
            if estimated_delivery_date:
                order.estimated_delivery_date = estimated_delivery_date
        elif new_status == 'delivered' and not order.delivered_at:
            order.delivered_at = timezone.now()
        elif new_status == 'cancelled' and not order.cancelled_at:
            order.cancelled_at = timezone.now()

        order.save()

        # Send email notifications for status changes
        email_type = None
        if new_status == 'shipped':
            email_type = 'shipped'
        elif new_status == 'delivered':
            email_type = 'delivered'

        if email_type:
            try:
                from .tasks import send_order_email
                send_order_email.delay(connection.schema_name, order.id, email_type)
            except Exception:
                pass  # Don't fail status update if email task fails

        serializer = self.get_serializer(order)
        return Response(serializer.data)

    @extend_schema(
        tags=['Ecommerce Admin - Orders'],
        summary='Mark order as paid',
        description='Mark a cash-on-delivery or pending order as paid. Sets payment_status to paid and records paid_at timestamp.'
    )
    @action(detail=True, methods=['post'], url_path='mark-paid')
    def mark_paid(self, request, pk=None):
        """Mark an order as paid (for cash on delivery or manual payment confirmation)"""
        from django.utils import timezone as tz

        order = self.get_object()

        if order.payment_status == 'paid':
            return Response({'error': 'Order is already marked as paid'}, status=status.HTTP_400_BAD_REQUEST)

        order.payment_status = 'paid'
        order.paid_at = tz.now()

        # Optionally update order status to confirmed if still pending
        if order.status == 'pending':
            order.status = 'confirmed'
            if not order.confirmed_at:
                order.confirmed_at = tz.now()

        # Accept optional payment notes
        payment_notes = request.data.get('notes')
        if payment_notes:
            order.admin_notes = f"{order.admin_notes}\n[Payment] {payment_notes}".strip() if order.admin_notes else f"[Payment] {payment_notes}"

        order.save()

        # Book Quickshipper courier (no-op if not configured for tenant)
        try:
            from .tasks import book_quickshipper_courier
            from django.db import connection as _db_conn
            book_quickshipper_courier.delay(_db_conn.schema_name, order.id)
        except Exception:
            pass

        serializer = self.get_serializer(order)
        return Response(serializer.data)

    @extend_schema(
        tags=['Ecommerce Admin - Orders'],
        summary='Refund order',
        description='Mark an order as refunded and restore stock for inventory-tracked products',
        request=None,
        responses={
            200: inline_serializer(
                name='OrderRefundResponse',
                fields={
                    'status': serializers.CharField(),
                },
            ),
            400: OpenApiResponse(description='Order already refunded or cancelled'),
        },
    )
    @action(detail=True, methods=['post'], url_path='refund')
    def refund(self, request, pk=None):
        """Mark order as refunded and restore stock"""
        order = self.get_object()

        if order.status in ['refunded', 'cancelled']:
            return Response({'error': 'Order already refunded or cancelled'}, status=status.HTTP_400_BAD_REQUEST)

        # Restore stock
        for item in order.items.all():
            if item.product and item.product.track_inventory:
                item.product.quantity += item.quantity
                item.product.save(update_fields=['quantity'])

        order.status = 'refunded'
        order.payment_status = 'refunded'
        order.save(update_fields=['status', 'payment_status'])

        return Response({'status': 'refunded'})

    @extend_schema(
        tags=['Ecommerce Admin - Orders'],
        summary='Order analytics',
        description='Get order analytics: revenue, order counts, top products, status breakdown'
    )
    @action(detail=False, methods=['get'], url_path='analytics')
    def analytics(self, request):
        """Get order analytics for the admin dashboard"""
        from django.db.models import Sum, Count, Q
        from django.db.models.functions import TruncDate
        from django.utils import timezone
        from datetime import timedelta

        now = timezone.now()
        today = now.date()
        week_ago = today - timedelta(days=7)
        month_ago = today - timedelta(days=30)
        prev_month_start = today - timedelta(days=60)

        orders = Order.objects.exclude(status='cancelled')
        paid_orders = orders.filter(payment_status='paid')

        # Revenue by period
        revenue_today = paid_orders.filter(
            created_at__date=today
        ).aggregate(total=Sum('total_amount'))['total'] or 0
        revenue_week = paid_orders.filter(
            created_at__date__gte=week_ago
        ).aggregate(total=Sum('total_amount'))['total'] or 0
        revenue_month = paid_orders.filter(
            created_at__date__gte=month_ago
        ).aggregate(total=Sum('total_amount'))['total'] or 0
        revenue_prev_month = paid_orders.filter(
            created_at__date__gte=prev_month_start,
            created_at__date__lt=month_ago
        ).aggregate(total=Sum('total_amount'))['total'] or 0
        revenue_total = paid_orders.aggregate(
            total=Sum('total_amount')
        )['total'] or 0

        # Order counts
        orders_today = orders.filter(created_at__date=today).count()
        orders_week = orders.filter(created_at__date__gte=week_ago).count()
        orders_month = orders.filter(created_at__date__gte=month_ago).count()
        orders_prev_month = orders.filter(
            created_at__date__gte=prev_month_start,
            created_at__date__lt=month_ago
        ).count()
        orders_total = orders.count()

        # Percentage changes
        revenue_change = (
            round(((float(revenue_month) - float(revenue_prev_month)) / float(revenue_prev_month)) * 100, 1)
            if revenue_prev_month else 0
        )
        orders_change = (
            round(((orders_month - orders_prev_month) / orders_prev_month) * 100, 1)
            if orders_prev_month else 0
        )
        avg_order_value = float(revenue_total) / orders_total if orders_total else 0

        from django.db.models import F

        # Top products
        top_products = list(
            OrderItem.objects.values('product__name').annotate(
                sold=Sum('quantity'),
                revenue=Sum(F('price') * F('quantity'))
            ).order_by('-sold')[:10]
        )

        # Order status breakdown
        status_breakdown = dict(
            orders.values_list('status').annotate(
                count=Count('id')
            ).values_list('status', 'count')
        )

        # Sales trend (daily revenue for last 7 days)
        sales_trend = list(
            paid_orders.filter(
                created_at__date__gte=week_ago
            ).annotate(
                date=TruncDate('created_at')
            ).values('date').annotate(
                revenue=Sum('total_amount'),
                orders=Count('id')
            ).order_by('date')
        )
        # Convert dates to strings
        for item in sales_trend:
            item['date'] = item['date'].isoformat()
            item['revenue'] = float(item['revenue'] or 0)

        # Customer insights
        from .models import EcommerceClient
        total_customers = EcommerceClient.objects.filter(is_active=True).count()
        new_customers = EcommerceClient.objects.filter(
            created_at__date__gte=month_ago
        ).count()
        # Returning = customers with more than 1 order
        returning_customers = EcommerceClient.objects.annotate(
            order_count=Count('orders', filter=Q(orders__status__in=['confirmed', 'processing', 'shipped', 'delivered']))
        ).filter(order_count__gt=1).count()

        # Revenue by payment method
        revenue_by_method = list(
            paid_orders.values('payment_method').annotate(
                revenue=Sum('total_amount'),
                count=Count('id')
            ).order_by('-revenue')
        )
        method_total = sum(float(m['revenue'] or 0) for m in revenue_by_method)
        for m in revenue_by_method:
            m['revenue'] = float(m['revenue'] or 0)
            m['percentage'] = round((m['revenue'] / method_total) * 100, 1) if method_total else 0

        # Recent orders (last 10)
        recent_orders = list(
            orders.select_related('client').order_by('-created_at')[:10].values(
                'id', 'order_number', 'status', 'payment_status',
                'total_amount', 'created_at',
                'client__first_name', 'client__last_name', 'client__email'
            )
        )
        for o in recent_orders:
            o['created_at'] = o['created_at'].isoformat()
            o['total_amount'] = float(o['total_amount'])
            o['client_name'] = f"{o.pop('client__first_name', '')} {o.pop('client__last_name', '')}".strip()
            o.pop('client__email', None)

        return Response({
            'revenue': {
                'today': float(revenue_today),
                'week': float(revenue_week),
                'month': float(revenue_month),
                'total': float(revenue_total),
                'change': revenue_change,
            },
            'orders': {
                'today': orders_today,
                'week': orders_week,
                'month': orders_month,
                'total': orders_total,
                'change': orders_change,
            },
            'avg_order_value': round(avg_order_value, 2),
            'top_products': top_products,
            'order_status_breakdown': status_breakdown,
            'sales_trend': sales_trend,
            'customer_insights': {
                'total': total_customers,
                'new': new_customers,
                'returning': returning_customers,
            },
            'revenue_by_method': revenue_by_method,
            'recent_orders': recent_orders,
        })

    @extend_schema(
        tags=['Ecommerce Admin - Orders'],
        summary='Bulk update order status',
        description='Update the status of multiple orders at once. Sends email notifications for shipped/delivered.',
        request=inline_serializer(
            name='BulkUpdateStatusRequest',
            fields={
                'order_ids': serializers.ListField(child=serializers.IntegerField()),
                'status': serializers.ChoiceField(choices=Order.STATUS_CHOICES),
            },
        ),
        responses={
            200: OpenApiResponse(description='Orders updated successfully'),
            400: OpenApiResponse(description='Validation error'),
        },
    )
    @action(detail=False, methods=['post'], url_path='bulk-update-status')
    def bulk_update_status(self, request):
        """Bulk update the status of multiple orders"""
        from django.utils import timezone
        from django.db import connection

        order_ids = request.data.get('order_ids', [])
        new_status = request.data.get('status')

        if not order_ids:
            return Response({'error': 'order_ids is required'}, status=status.HTTP_400_BAD_REQUEST)

        if not new_status:
            return Response({'error': 'status is required'}, status=status.HTTP_400_BAD_REQUEST)

        if new_status not in dict(Order.STATUS_CHOICES):
            return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)

        orders = Order.objects.filter(id__in=order_ids)
        if orders.count() != len(order_ids):
            found_ids = set(orders.values_list('id', flat=True))
            missing = [oid for oid in order_ids if oid not in found_ids]
            return Response(
                {'error': f'Orders not found: {missing}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated_orders = []
        for order in orders:
            order.status = new_status
            if new_status == 'confirmed' and not order.confirmed_at:
                order.confirmed_at = timezone.now()
            elif new_status == 'processing' and not order.processing_at:
                order.processing_at = timezone.now()
            elif new_status == 'shipped' and not order.shipped_at:
                order.shipped_at = timezone.now()
            elif new_status == 'delivered' and not order.delivered_at:
                order.delivered_at = timezone.now()
            elif new_status == 'cancelled' and not order.cancelled_at:
                order.cancelled_at = timezone.now()
            order.save()
            updated_orders.append(order)

            # Send email notifications for shipped/delivered
            email_type = None
            if new_status == 'shipped':
                email_type = 'shipped'
            elif new_status == 'delivered':
                email_type = 'delivered'

            if email_type:
                try:
                    from .tasks import send_order_email
                    send_order_email.delay(connection.schema_name, order.id, email_type)
                except Exception:
                    pass

        return Response({
            'updated': len(updated_orders),
            'status': new_status,
            'order_ids': [o.id for o in updated_orders],
        })

    @extend_schema(
        tags=['Ecommerce Admin - Orders'],
        summary='Export orders as CSV',
        description='Download all orders (respecting current filters) as a CSV file.',
        responses={
            200: OpenApiResponse(description='CSV file download'),
        },
    )
    @action(detail=False, methods=['get'], url_path='export')
    def export_orders(self, request):
        """Export filtered orders as CSV"""
        import csv
        from django.http import HttpResponse

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="orders.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Order Number', 'Date', 'Client', 'Email', 'Phone',
            'Total', 'Subtotal', 'Shipping', 'Tax', 'Discount',
            'Status', 'Payment Status', 'Payment Method',
        ])

        orders = self.filter_queryset(self.get_queryset()).select_related('client')
        for order in orders:
            writer.writerow([
                order.order_number,
                order.created_at.strftime('%Y-%m-%d %H:%M:%S') if order.created_at else '',
                order.client.full_name if order.client else '',
                order.client.email if order.client else '',
                order.client.phone_number if order.client else '',
                order.total_amount,
                order.subtotal,
                order.shipping_cost,
                order.tax_amount,
                order.discount_amount,
                order.status,
                order.payment_status,
                order.payment_method,
            ])
        return response

    @extend_schema(
        tags=['Ecommerce Admin - Orders'],
        summary='Initiate payment for order',
        description='Create BOG payment session for an order'
    )
    @action(detail=True, methods=['post'])
    def initiate_payment(self, request, pk=None):
        """
        Initiate payment for an order
        Creates a BOG payment session and returns payment URL
        """
        from tenants.bog_payment import bog_service
        import uuid

        order = self.get_object()

        # Check if order is already paid
        if order.payment_status == 'paid':
            return Response(
                {'error': 'Order is already paid'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if payment is pending
        if order.payment_url and order.payment_status == 'pending':
            return Response({
                'payment_url': order.payment_url,
                'bog_order_id': order.bog_order_id,
                'status': 'pending'
            })

        # Get tenant's ecommerce settings — tenant MUST provide their own credentials
        try:
            from .models import EcommerceSettings
            ecommerce_settings = EcommerceSettings.objects.get(tenant=request.tenant)
        except EcommerceSettings.DoesNotExist:
            return Response(
                {'error': 'Payment not configured. Please set up payment credentials in ecommerce settings.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not ecommerce_settings.has_bog_credentials:
            return Response(
                {'error': 'BOG payment credentials not configured. Please add your BOG Client ID and Secret in ecommerce settings.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        client_id = ecommerce_settings.bog_client_id
        client_secret = ecommerce_settings.get_bog_secret()

        # Check payment method
        payment_method = request.data.get('payment_method', 'card')

        if payment_method == 'cash_on_delivery':
            # No BOG payment needed, just mark as COD
            order.payment_method = 'cash_on_delivery'
            order.payment_status = 'pending'
            order.save()
            return Response({
                'payment_method': 'cash_on_delivery',
                'message': 'Order will be paid on delivery'
            })

        # Create BOG payment
        try:
            # Generate callback URL
            callback_url = f"{request.scheme}://{request.get_host()}/api/ecommerce/payment-webhook/"
            return_url_success = request.data.get('return_url_success', '')
            return_url_fail = request.data.get('return_url_fail', '')

            # Create payment with tenant-specific or default credentials
            payment_result = bog_service.create_payment(
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

            return Response({
                'payment_url': payment_result['payment_url'],
                'bog_order_id': payment_result['order_id'],
                'amount': payment_result['amount'],
                'currency': payment_result['currency'],
                'status': 'pending'
            })

        except Exception as e:
            return Response(
                {'error': f'Failed to create payment: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


@extend_schema(
    operation_id='ecommerce_payment_webhook',
    summary='BOG Payment Webhook for Ecommerce Orders',
    description='Webhook endpoint for receiving payment status updates from Bank of Georgia for ecommerce orders',
    responses={
        200: OpenApiResponse(description='Webhook processed successfully'),
        400: OpenApiResponse(description='Invalid payload')
    },
    tags=['Ecommerce Admin - Orders']
)
@api_view(['POST'])
@permission_classes([AllowAny])  # Webhook from BOG, no auth
def ecommerce_payment_webhook(request):
    """
    Handle webhook notifications from Bank of Georgia payment gateway for ecommerce orders
    
    BOG Callback format:
    {
        "event": "order_payment",
        "zoned_request_time": "2024-01-01T12:00:00.000000Z",
        "body": {
            "order_id": "...",
            "order_status": {"key": "completed", ...},
            "external_order_id": "ORD-20250211-XYZ789",
            ...
        }
    }
    """
    import logging
    from django.utils import timezone
    
    logger = logging.getLogger(__name__)
    
    # Process payment event
    event_type = request.data.get('event')
    body = request.data.get('body', {})
    
    if event_type != 'order_payment':
        logger.warning(f'Unexpected webhook event type: {event_type}')
        return Response({'error': 'Unexpected event type'}, status=status.HTTP_400_BAD_REQUEST)
    
    # Extract payment details
    bog_order_id = body.get('order_id') or body.get('id')
    external_order_id = body.get('external_order_id')  # This is our order_number
    order_status_obj = body.get('order_status', {})
    bog_status = order_status_obj.get('key', '')
    
    # Get response code from payment_detail
    payment_detail = body.get('payment_detail', {})
    response_code = payment_detail.get('code', '')
    transaction_id = payment_detail.get('transaction_id', '')
    
    logger.info(f'Ecommerce payment webhook: bog_order_id={bog_order_id}, order_number={external_order_id}, status={bog_status}, code={response_code}')
    
    # Handle successful payment (BOG status: 'completed' with response code '100')
    if bog_status == 'completed' and response_code == '100':
        try:
            # Find order by order_number (external_order_id)
            if not external_order_id:
                logger.error('Missing external_order_id in webhook payload')
                return Response({'error': 'Invalid payload'}, status=status.HTTP_400_BAD_REQUEST)

            if not bog_order_id:
                logger.error('Missing BOG order_id in webhook payload')
                return Response({'error': 'Invalid payload'}, status=status.HTTP_400_BAD_REQUEST)

            # SECURITY: never trust the webhook body. Ask BOG directly whether
            # this order actually completed before treating it as paid — a forged
            # POST claiming "completed" will not survive this check.
            from .payment_utils import get_tenant_bog_service, BogCredentialError
            try:
                bog_service = get_tenant_bog_service(getattr(request, 'tenant', None))
                confirmation = bog_service.check_payment_status(bog_order_id)
            except BogCredentialError:
                logger.exception('Cannot verify BOG payment %s — tenant credential error', bog_order_id)
                return Response({'error': 'verification_unavailable'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            except Exception:
                logger.exception('BOG status re-query failed for %s', bog_order_id)
                return Response({'error': 'verification_unavailable'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

            if confirmation.get('status') in ('error', 'unknown'):
                # Couldn't reach a verdict — ask BOG to retry the callback later.
                logger.warning('BOG status inconclusive for %s: %s', bog_order_id, confirmation.get('error'))
                return Response({'error': 'verification_unavailable'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

            confirmed = (
                confirmation.get('bog_status') == 'completed'
                and str(confirmation.get('response_code')) == '100'
            )
            if not confirmed:
                logger.warning(
                    'BOG did NOT confirm payment for %s (bog_status=%s) — ignoring webhook',
                    bog_order_id, confirmation.get('bog_status')
                )
                return Response({'status': 'ignored', 'message': 'Payment not confirmed by BOG'}, status=status.HTTP_200_OK)

            # Check if this is a card validation transaction (order_id starts with 'card_')
            if external_order_id.startswith('card_'):
                # Extract client ID from order_id format: card_{client_id}_{random}
                try:
                    parts = external_order_id.split('_')
                    client_id = int(parts[1])

                    from .models import EcommerceClient, ClientCard
                    client = EcommerceClient.objects.get(id=client_id)

                    # Extract card details from payment_detail
                    payment_detail = body.get('payment_detail', {})
                    payer_identifier = payment_detail.get('payer_identifier', '')  # e.g., "531125***1450"
                    card_type = payment_detail.get('card_type', '')  # e.g., "mc" or "visa"
                    card_expiry = payment_detail.get('card_expiry_date', '')  # e.g., "05/27"

                    # Format card type for display
                    card_type_display = {
                        'mc': 'Mastercard',
                        'visa': 'Visa',
                        'amex': 'American Express'
                    }.get(card_type.lower(), card_type.upper())

                    # Create or update saved card
                    card, created = ClientCard.objects.update_or_create(
                        parent_order_id=bog_order_id,
                        defaults={
                            'client': client,
                            'card_type': card_type_display,
                            'masked_card_number': payer_identifier,
                            'card_expiry': card_expiry,
                            'is_active': True,
                            'is_default': not ClientCard.objects.filter(client=client, is_active=True).exists()  # First card is default
                        }
                    )

                    action = 'created' if created else 'updated'
                    logger.info(f'Card {action} for client {client_id}: {payer_identifier} ({card_type_display})')

                    return Response({
                        'status': 'success',
                        'action': f'card_{action}',
                        'card_id': card.id
                    })

                except (IndexError, ValueError, EcommerceClient.DoesNotExist) as e:
                    logger.error(f'Error processing card validation: {e}')
                    return Response({'error': 'Invalid card validation order'}, status=status.HTTP_400_BAD_REQUEST)

            from django.db import connection, transaction

            with transaction.atomic():
                try:
                    order = Order.objects.select_for_update().get(order_number=external_order_id)
                except Order.DoesNotExist:
                    logger.error(f'Order not found: {external_order_id}')
                    return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)

                # Idempotency, re-checked under the row lock so concurrent
                # duplicate callbacks can't both run the side effects.
                if order.payment_status == 'paid' and order.paid_at:
                    logger.info(f'Webhook already processed for order: {external_order_id}')
                    return Response({
                        'status': 'success',
                        'message': 'Payment already processed'
                    }, status=status.HTTP_200_OK)

                # BOG confirmed payment but we had cancelled the order (e.g. the
                # abandoned-order sweeper already restocked it). Money moved, so
                # don't silently confirm or silently drop — escalate for a human.
                if order.status == 'cancelled':
                    logger.error(
                        'BOG CONFIRMED payment for CANCELLED order %s (bog_order_id=%s) — '
                        'manual reconciliation required', external_order_id, bog_order_id
                    )
                    return Response({
                        'status': 'conflict',
                        'message': 'Order was cancelled; manual reconciliation required'
                    }, status=status.HTTP_200_OK)

                # Mark paid + auto-confirm.
                order.payment_status = 'paid'
                order.paid_at = timezone.now()
                order.status = 'confirmed'
                order.confirmed_at = timezone.now()
                order.payment_metadata.update({
                    'bog_order_id': bog_order_id,
                    'transaction_id': transaction_id,
                    'response_code': response_code,
                    'paid_at': timezone.now().isoformat()
                })
                order.save()

                # Dispatch side effects only AFTER the paid state commits, so the
                # worker sees the paid order and a rollback can't orphan tasks.
                _schema = connection.schema_name
                _order_id = order.id

                def _dispatch_post_payment():
                    from .tasks import send_order_email, book_quickshipper_courier
                    try:
                        send_order_email.delay(_schema, _order_id, 'confirmation')
                    except Exception:
                        logger.exception('Failed to queue confirmation email for order %s', _order_id)
                    try:
                        book_quickshipper_courier.delay(_schema, _order_id)
                    except Exception:
                        logger.exception('Failed to queue courier booking for order %s', _order_id)

                transaction.on_commit(_dispatch_post_payment)

            logger.info(f'Payment completed for order: {external_order_id}')

            return Response({
                'status': 'success',
                'action': 'payment_completed',
                'order_number': order.order_number
            })
        
        except Exception as e:
            logger.error(f'Error processing webhook: {e}')
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    # Handle failed payment (BOG status: 'rejected')
    elif bog_status == 'rejected':
        logger.warning(f'Payment failed: order_number={external_order_id}, code={response_code}')
        from django.db import transaction

        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(order_number=external_order_id)
                # Never downgrade an order BOG already confirmed as paid — a late
                # or duplicated 'rejected' must not corrupt a paid order.
                if order.payment_status == 'paid':
                    logger.warning('Ignoring rejected webhook for already-paid order %s', external_order_id)
                    return Response({'status': 'ignored', 'message': 'Order already paid'}, status=status.HTTP_200_OK)
                order.payment_status = 'failed'
                order.payment_metadata.update({
                    'bog_status': bog_status,
                    'response_code': response_code,
                    'failed_at': timezone.now().isoformat()
                })
                order.save(update_fields=['payment_status', 'payment_metadata', 'updated_at'])
            # Stock is intentionally NOT restored here: the customer may retry
            # payment on this order, and restoring now would let a later success
            # oversell. Never-paid orders are restocked by the cancel_unpaid_orders
            # sweeper after a grace period.
        except Order.DoesNotExist:
            logger.error(f'Order not found for failed payment: {external_order_id}')
    
    # Handle other statuses
    else:
        logger.info(f'Webhook received with status: {bog_status}, code: {response_code}')

    return Response({'status': 'received'})


@extend_schema(
    operation_id='tbc_payment_webhook',
    summary='TBC Bank Payment Webhook for Ecommerce Orders',
    description='Webhook endpoint for receiving payment status updates from TBC Bank for ecommerce orders',
    responses={
        200: OpenApiResponse(description='Webhook processed successfully'),
        400: OpenApiResponse(description='Invalid payload')
    },
    tags=['Ecommerce Admin - Orders']
)
@api_view(['POST'])
@permission_classes([AllowAny])
def tbc_payment_webhook(request):
    """
    Handle webhook notifications from TBC Bank payment gateway for ecommerce orders.

    TBC callback sends JSON with payment result including:
    - payId: TBC payment ID
    - status: payment status (Succeeded, Failed, etc.)
    - merchantPaymentId: our external_order_id / order_number
    """
    import logging
    from django.utils import timezone

    logger = logging.getLogger(__name__)

    data = request.data
    if not data:
        logger.warning('TBC webhook received empty payload')
        return Response({'error': 'Empty payload'}, status=status.HTTP_400_BAD_REQUEST)

    pay_id = data.get('payId', '')
    tbc_status = data.get('status', '').lower()
    merchant_payment_id = data.get('merchantPaymentId', '')
    amount_data = data.get('amount', {})

    logger.info(
        f'TBC payment webhook: payId={pay_id}, '
        f'merchantPaymentId={merchant_payment_id}, status={tbc_status}'
    )

    if not merchant_payment_id:
        logger.error('TBC webhook missing merchantPaymentId')
        return Response({'error': 'Missing merchantPaymentId'}, status=status.HTTP_400_BAD_REQUEST)

    # Handle successful payment
    # NOTE: TBC callbacks are not signature-verified and this handler does not
    # yet re-query TBC's status API server-side, so it still trusts the body.
    # The structural hardening below (row lock, idempotency, state guards) fixes
    # the duplicate/resurrection/downgrade bugs; adding a server-side re-query
    # via TBCPaymentProvider.check_payment_status is a scoped follow-up.
    if tbc_status in ('succeeded', 'completed'):
        from django.db import connection, transaction
        try:
            with transaction.atomic():
                try:
                    order = Order.objects.select_for_update().get(order_number=merchant_payment_id)
                except Order.DoesNotExist:
                    logger.error(f'Order not found for TBC webhook: {merchant_payment_id}')
                    return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)

                # Idempotency, re-checked under the row lock.
                if order.payment_status == 'paid' and order.paid_at:
                    logger.info(f'TBC webhook already processed for order: {merchant_payment_id}')
                    return Response({
                        'status': 'success',
                        'message': 'Payment already processed'
                    }, status=status.HTTP_200_OK)

                if order.status == 'cancelled':
                    logger.error(
                        'TBC confirmed payment for CANCELLED order %s — manual reconciliation required',
                        merchant_payment_id
                    )
                    return Response({
                        'status': 'conflict',
                        'message': 'Order was cancelled; manual reconciliation required'
                    }, status=status.HTTP_200_OK)

                order.payment_status = 'paid'
                order.paid_at = timezone.now()
                order.status = 'confirmed'
                order.confirmed_at = timezone.now()
                order.payment_metadata.update({
                    'tbc_pay_id': pay_id,
                    'tbc_status': tbc_status,
                    'tbc_amount': amount_data,
                    'paid_at': timezone.now().isoformat(),
                })
                order.save()

                _schema = connection.schema_name
                _order_id = order.id

                def _dispatch_courier():
                    from .tasks import book_quickshipper_courier
                    try:
                        book_quickshipper_courier.delay(_schema, _order_id)
                    except Exception:
                        logger.exception('Failed to queue courier booking for order %s', _order_id)

                transaction.on_commit(_dispatch_courier)

            logger.info(f'TBC payment completed for order: {merchant_payment_id}')
            return Response({
                'status': 'success',
                'action': 'payment_completed',
                'order_number': order.order_number,
            })

        except Exception as e:
            logger.error(f'Error processing TBC webhook: {e}')
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Handle failed payment
    elif tbc_status in ('failed', 'rejected', 'expired'):
        logger.warning(f'TBC payment failed: merchantPaymentId={merchant_payment_id}, status={tbc_status}')
        from django.db import transaction
        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(order_number=merchant_payment_id)
                if order.payment_status == 'paid':
                    logger.warning('Ignoring failed TBC webhook for already-paid order %s', merchant_payment_id)
                    return Response({'status': 'ignored', 'message': 'Order already paid'}, status=status.HTTP_200_OK)
                order.payment_status = 'failed'
                order.payment_metadata.update({
                    'tbc_pay_id': pay_id,
                    'tbc_status': tbc_status,
                    'failed_at': timezone.now().isoformat(),
                })
                order.save(update_fields=['payment_status', 'payment_metadata', 'updated_at'])
        except Order.DoesNotExist:
            logger.error(f'Order not found for failed TBC payment: {merchant_payment_id}')

    else:
        logger.info(f'TBC webhook received with status: {tbc_status}')

    return Response({'status': 'received'})


@extend_schema(
    operation_id='flitt_payment_webhook',
    summary='Flitt Payment Webhook for Ecommerce Orders',
    description='Webhook endpoint for receiving payment status updates from Flitt for ecommerce orders',
    responses={
        200: OpenApiResponse(description='Webhook processed successfully'),
        400: OpenApiResponse(description='Invalid payload')
    },
    tags=['Ecommerce Admin - Orders']
)
@api_view(['POST'])
@permission_classes([AllowAny])
def flitt_payment_webhook(request):
    """
    Handle webhook notifications from Flitt (Fondy) payment gateway for ecommerce orders.

    Flitt callback sends POST data with:
    - order_id: our external_order_id / order_number
    - order_status: payment status (approved, declined, etc.)
    - signature: HMAC-SHA1 signature for verification
    - amount: amount in cents
    - payment_id: Flitt payment ID
    """
    import logging
    from django.utils import timezone

    logger = logging.getLogger(__name__)

    data = request.data
    if not data:
        logger.warning('Flitt webhook received empty payload')
        return Response({'error': 'Empty payload'}, status=status.HTTP_400_BAD_REQUEST)

    order_id = data.get('order_id', '')
    order_status_value = data.get('order_status', '').lower()
    payment_id = data.get('payment_id', '')
    received_signature = data.get('signature', '')

    logger.info(
        f'Flitt payment webhook: order_id={order_id}, '
        f'payment_id={payment_id}, status={order_status_value}'
    )

    if not order_id:
        logger.error('Flitt webhook missing order_id')
        return Response({'error': 'Missing order_id'}, status=status.HTTP_400_BAD_REQUEST)

    # Verify signature. Flitt signs every callback with an HMAC-SHA1 over the
    # request params, so a genuine callback always carries a valid signature.
    # Reject a missing or invalid one — that's what stops a forged "approved".
    try:
        from tenants.payment_providers.flitt import FlittPaymentProvider
        provider = FlittPaymentProvider()
        body_bytes = request.body if hasattr(request, 'body') else b''
        signature_ok = bool(received_signature) and provider.verify_webhook({}, body_bytes)
    except Exception as e:
        logger.warning(f'Flitt signature verification error: {e}')
        signature_ok = False

    if not signature_ok:
        logger.warning(f'Flitt webhook signature verification failed for order: {order_id}')
        return Response({'error': 'Invalid signature'}, status=status.HTTP_403_FORBIDDEN)

    # Handle successful payment
    if order_status_value == 'approved':
        from django.db import connection, transaction
        try:
            with transaction.atomic():
                try:
                    order = Order.objects.select_for_update().get(order_number=order_id)
                except Order.DoesNotExist:
                    logger.error(f'Order not found for Flitt webhook: {order_id}')
                    return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)

                # Idempotency, re-checked under the row lock.
                if order.payment_status == 'paid' and order.paid_at:
                    logger.info(f'Flitt webhook already processed for order: {order_id}')
                    return Response({
                        'status': 'success',
                        'message': 'Payment already processed'
                    }, status=status.HTTP_200_OK)

                if order.status == 'cancelled':
                    logger.error(
                        'Flitt confirmed payment for CANCELLED order %s — manual reconciliation required',
                        order_id
                    )
                    return Response({
                        'status': 'conflict',
                        'message': 'Order was cancelled; manual reconciliation required'
                    }, status=status.HTTP_200_OK)

                order.payment_status = 'paid'
                order.paid_at = timezone.now()
                order.status = 'confirmed'
                order.confirmed_at = timezone.now()

                flitt_meta = {
                    'flitt_payment_id': payment_id,
                    'flitt_order_status': order_status_value,
                    'paid_at': timezone.now().isoformat(),
                }
                rectoken = data.get('rectoken')
                if rectoken:
                    flitt_meta['flitt_rectoken'] = rectoken

                order.payment_metadata.update(flitt_meta)
                order.save()

                _schema = connection.schema_name
                _order_id = order.id

                def _dispatch_courier():
                    from .tasks import book_quickshipper_courier
                    try:
                        book_quickshipper_courier.delay(_schema, _order_id)
                    except Exception:
                        logger.exception('Failed to queue courier booking for order %s', _order_id)

                transaction.on_commit(_dispatch_courier)

            logger.info(f'Flitt payment completed for order: {order_id}')
            return Response({
                'status': 'success',
                'action': 'payment_completed',
                'order_number': order.order_number,
            })

        except Exception as e:
            logger.error(f'Error processing Flitt webhook: {e}')
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Handle failed/declined payment
    elif order_status_value in ('declined', 'expired', 'reversed'):
        logger.warning(f'Flitt payment failed: order_id={order_id}, status={order_status_value}')
        from django.db import transaction
        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(order_number=order_id)
                if order.payment_status == 'paid':
                    logger.warning('Ignoring failed Flitt webhook for already-paid order %s', order_id)
                    return Response({'status': 'ignored', 'message': 'Order already paid'}, status=status.HTTP_200_OK)
                order.payment_status = 'failed'
                order.payment_metadata.update({
                    'flitt_payment_id': payment_id,
                    'flitt_order_status': order_status_value,
                    'failed_at': timezone.now().isoformat(),
                })
                order.save(update_fields=['payment_status', 'payment_metadata', 'updated_at'])
        except Order.DoesNotExist:
            logger.error(f'Order not found for failed Flitt payment: {order_id}')

    else:
        logger.info(f'Flitt webhook received with status: {order_status_value}')

    return Response({'status': 'received'})


class EcommerceSettingsViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """ViewSet for managing ecommerce settings including BOG payment configuration"""
    serializer_class = EcommerceSettingsSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Return settings for the current tenant only"""
        return EcommerceSettings.objects.filter(tenant=self.request.tenant)

    def perform_create(self, serializer):
        """Automatically set tenant when creating settings"""
        serializer.save(tenant=self.request.tenant)

    @extend_schema(
        tags=['Ecommerce Admin - Settings'],
        summary='Get ecommerce settings',
        description='Retrieve ecommerce settings for current tenant including BOG configuration'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Settings'],
        summary='Get settings detail',
        description='Retrieve detailed ecommerce settings'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Settings'],
        summary='Create ecommerce settings',
        description='Create ecommerce settings for tenant including BOG credentials and return URLs'
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Settings'],
        summary='Update ecommerce settings',
        description='Update ecommerce settings including BOG configuration'
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Settings'],
        summary='Partial update settings',
        description='Partially update ecommerce settings'
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Settings'],
        summary='Deploy frontend website',
        description='Deploy the ecommerce frontend website. Adds the tenant subdomain to the shared multi-tenant Vercel project.',
        responses={
            201: inline_serializer(
                name='DeploymentResponse',
                fields={
                    'success': serializers.BooleanField(),
                    'message': serializers.CharField(),
                    'url': serializers.URLField(),
                    'project_id': serializers.CharField(),
                    'project_name': serializers.CharField(),
                }
            ),
            400: OpenApiResponse(description='Deployment already in progress or already deployed'),
            500: OpenApiResponse(description='Deployment failed'),
        }
    )
    @action(detail=False, methods=['post'], url_path='deploy-frontend')
    def deploy_frontend(self, request):
        """
        Deploy frontend website to Vercel (Multi-Tenant)

        This adds the tenant's subdomain to a shared multi-tenant Vercel project.
        All tenants share a single deployment - tenant configuration is resolved
        at runtime via middleware based on the hostname.

        The subdomain format is: {schema_name}.ecommerce.echodesk.ge
        """
        from .services.vercel_deployment import deploy_tenant_frontend, ECOMMERCE_DOMAIN_SUFFIX
        from tenant_schemas.utils import get_public_schema_name, schema_context

        tenant = request.tenant

        # Get or create EcommerceSettings for this tenant
        settings, created = EcommerceSettings.objects.get_or_create(
            tenant=tenant,
            defaults={'store_name': tenant.name}
        )

        # Check current deployment status
        if settings.deployment_status == 'deploying':
            return Response(
                {"error": "Deployment already in progress"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # If already deployed, just return the existing URL
        # With multi-tenant architecture, there's no need for "redeployment"
        # since all tenants share the same project and code updates propagate automatically
        if settings.deployment_status == 'deployed' and settings.vercel_project_id and settings.ecommerce_frontend_url:
            return Response({
                "success": True,
                "message": "Frontend is already deployed. Code updates propagate automatically.",
                "url": settings.ecommerce_frontend_url,
                "project_id": settings.vercel_project_id,
                "project_name": "echodesk-ecommerce"
            }, status=status.HTTP_200_OK)

        # Update status to deploying
        settings.deployment_status = 'deploying'
        settings.save(update_fields=['deployment_status'])

        try:
            # Add subdomain to shared Vercel project
            with schema_context(get_public_schema_name()):
                tenant_obj = Tenant.objects.get(id=tenant.id)
                result = deploy_tenant_frontend(tenant_obj)

            if result.get("success"):
                # Update EcommerceSettings with deployment info
                settings.vercel_project_id = result.get("project_id")
                settings.ecommerce_frontend_url = result.get("url")
                settings.deployment_status = 'deployed'
                settings.save(update_fields=['vercel_project_id', 'ecommerce_frontend_url', 'deployment_status'])

                response_data = {
                    "success": True,
                    "message": result.get("message", "Frontend deployed successfully"),
                    "url": result.get("url"),
                    "project_id": result.get("project_id"),
                    "project_name": result.get("project_name"),
                    "domain": result.get("domain"),
                    "verified": result.get("verified", False),
                }

                # Include DNS configuration if domain is not yet verified
                if result.get("dns_config"):
                    response_data["dns_config"] = result.get("dns_config")

                return Response(response_data, status=status.HTTP_201_CREATED)
            else:
                # Deployment failed
                settings.deployment_status = 'failed'
                settings.save(update_fields=['deployment_status'])

                return Response({
                    "success": False,
                    "error": result.get("error", "Deployment failed"),
                    "code": result.get("code", "UNKNOWN")
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        except Exception as e:
            # Reset status on error
            settings.deployment_status = 'failed'
            settings.save(update_fields=['deployment_status'])

            return Response({
                "success": False,
                "error": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        summary="Delete frontend deployment",
        description="Remove the tenant's subdomain from the shared Vercel project and reset deployment status",
        responses={
            200: OpenApiResponse(description='Deployment deleted successfully'),
            400: OpenApiResponse(description='No deployment to delete'),
            500: OpenApiResponse(description='Failed to delete deployment'),
        }
    )
    @action(detail=False, methods=['delete'], url_path='delete-deployment')
    def delete_deployment(self, request):
        """
        Remove frontend deployment (Multi-Tenant)

        This removes the tenant's subdomain from the shared Vercel project.
        It does NOT delete the shared project itself (which would break other tenants).
        """
        from .services.vercel_deployment import VercelDeploymentService, ECOMMERCE_DOMAIN_SUFFIX

        tenant = request.tenant

        # Get or create EcommerceSettings for this tenant
        settings, created = EcommerceSettings.objects.get_or_create(
            tenant=tenant,
            defaults={'store_name': tenant.name}
        )

        # Check if there's a deployment to delete
        if not settings.vercel_project_id or settings.deployment_status == 'pending':
            return Response(
                {"error": "No deployment to delete"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            service = VercelDeploymentService()

            # Remove the subdomain from the shared project (not delete the project!)
            subdomain = f"{tenant.schema_name}{ECOMMERCE_DOMAIN_SUFFIX}"
            result = service.remove_domain(settings.vercel_project_id, subdomain)

            if result.get("success"):
                # Reset deployment info
                settings.vercel_project_id = None
                settings.ecommerce_frontend_url = None
                settings.deployment_status = 'pending'
                settings.save(update_fields=['vercel_project_id', 'ecommerce_frontend_url', 'deployment_status'])

                return Response({
                    "success": True,
                    "message": f"Subdomain {subdomain} removed successfully"
                }, status=status.HTTP_200_OK)
            else:
                # If domain not found or not assigned, still reset the settings
                error_msg = result.get("error", "").lower()
                if "not found" in error_msg or "not assigned" in error_msg or "does not exist" in error_msg:
                    settings.vercel_project_id = None
                    settings.ecommerce_frontend_url = None
                    settings.deployment_status = 'pending'
                    settings.save(update_fields=['vercel_project_id', 'ecommerce_frontend_url', 'deployment_status'])

                    return Response({
                        "success": True,
                        "message": "Deployment reset (subdomain was already removed)"
                    }, status=status.HTTP_200_OK)

                return Response({
                    "success": False,
                    "error": result.get("error", "Failed to remove subdomain")
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        except Exception as e:
            return Response({
                "success": False,
                "error": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        tags=['Ecommerce Admin - Settings'],
        summary='Get custom domains',
        description='Get all custom domains for the deployed frontend with their verification status',
        responses={
            200: inline_serializer(
                name='DomainsListResponse',
                fields={
                    'success': serializers.BooleanField(),
                    'domains': serializers.ListField(child=serializers.DictField()),
                }
            ),
            400: OpenApiResponse(description='No deployment exists'),
        }
    )
    @action(detail=False, methods=['get'], url_path='domains')
    def get_domains(self, request):
        """
        Get all custom domains for the frontend deployment
        """
        from .services.vercel_deployment import VercelDeploymentService

        tenant = request.tenant

        settings, created = EcommerceSettings.objects.get_or_create(
            tenant=tenant,
            defaults={'store_name': tenant.name}
        )

        if not settings.vercel_project_id:
            return Response(
                {"error": "No deployment exists. Deploy frontend first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            service = VercelDeploymentService()
            result = service.get_domains(settings.vercel_project_id)

            if result.get("success"):
                # Filter domains to only show this tenant's domains
                from django.db import connection
                schema_name = getattr(connection, 'schema_name', '')
                tenant_domains = []
                for domain in result.get("domains", []):
                    domain_name = domain.get("name", "")
                    # Include if: matches tenant subdomain pattern, or is tenant's custom domain
                    if (schema_name and schema_name in domain_name) or \
                       (settings.custom_domain and domain_name == settings.custom_domain):
                        tenant_domains.append(domain)
                result["domains"] = tenant_domains
                return Response(result, status=status.HTTP_200_OK)
            else:
                return Response({
                    "success": False,
                    "error": result.get("error", "Failed to get domains")
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        except Exception as e:
            return Response({
                "success": False,
                "error": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        tags=['Ecommerce Admin - Settings'],
        summary='Add custom domain',
        description='Add a custom domain to the deployed frontend. Returns DNS configuration instructions.',
        request=inline_serializer(
            name='AddDomainRequest',
            fields={
                'domain': serializers.CharField(help_text='Domain name (e.g., shop.example.com)')
            }
        ),
        responses={
            201: inline_serializer(
                name='AddDomainResponse',
                fields={
                    'success': serializers.BooleanField(),
                    'domain': serializers.CharField(),
                    'verified': serializers.BooleanField(),
                    'verification': serializers.ListField(child=serializers.DictField()),
                    'dns_instructions': serializers.DictField(),
                }
            ),
            400: OpenApiResponse(description='No deployment exists or invalid domain'),
        }
    )
    @action(detail=False, methods=['post'], url_path='add-domain')
    def add_domain(self, request):
        """
        Add a custom domain to the frontend deployment
        """
        from .services.vercel_deployment import VercelDeploymentService

        tenant = request.tenant
        domain = request.data.get('domain', '').strip().lower()

        if not domain:
            return Response(
                {"error": "Domain is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        settings, created = EcommerceSettings.objects.get_or_create(
            tenant=tenant,
            defaults={'store_name': tenant.name}
        )

        if not settings.vercel_project_id:
            return Response(
                {"error": "No deployment exists. Deploy frontend first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            service = VercelDeploymentService()
            result = service.add_domain(settings.vercel_project_id, domain)

            if result.get("success"):
                # Add DNS instructions
                is_subdomain = '.' in domain and not domain.startswith('www.')
                apex_name = result.get("apexName", domain)

                dns_instructions = {
                    "domain": domain,
                    "is_subdomain": is_subdomain,
                }

                if is_subdomain:
                    # For subdomain like shop.example.com
                    subdomain_part = domain.replace(f".{apex_name}", "")
                    dns_instructions["instructions"] = [
                        {
                            "type": "CNAME",
                            "name": subdomain_part,
                            "value": "cname.vercel-dns.com",
                            "description": f"Point {subdomain_part} to Vercel"
                        }
                    ]
                else:
                    # For root domain like example.com
                    dns_instructions["instructions"] = [
                        {
                            "type": "A",
                            "name": "@",
                            "value": "76.76.21.21",
                            "description": "Point root domain to Vercel"
                        }
                    ]

                result["dns_instructions"] = dns_instructions

                # Save custom domain to settings
                settings.custom_domain = domain
                settings.save(update_fields=['custom_domain'])

                return Response(result, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    "success": False,
                    "error": result.get("error", "Failed to add domain")
                }, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            return Response({
                "success": False,
                "error": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        tags=['Ecommerce Admin - Settings'],
        summary='Remove custom domain',
        description='Remove a custom domain from the deployed frontend',
        request=inline_serializer(
            name='RemoveDomainRequest',
            fields={
                'domain': serializers.CharField(help_text='Domain name to remove')
            }
        ),
        responses={
            200: OpenApiResponse(description='Domain removed successfully'),
            400: OpenApiResponse(description='No deployment exists or domain not found'),
        }
    )
    @action(detail=False, methods=['post'], url_path='remove-domain')
    def remove_domain(self, request):
        """
        Remove a custom domain from the frontend deployment
        """
        from .services.vercel_deployment import VercelDeploymentService

        tenant = request.tenant
        domain = request.data.get('domain', '').strip().lower()

        if not domain:
            return Response(
                {"error": "Domain is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        settings, created = EcommerceSettings.objects.get_or_create(
            tenant=tenant,
            defaults={'store_name': tenant.name}
        )

        if not settings.vercel_project_id:
            return Response(
                {"error": "No deployment exists. Deploy frontend first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            service = VercelDeploymentService()
            result = service.remove_domain(settings.vercel_project_id, domain)

            if result.get("success"):
                # Clear custom domain from settings if it matches
                if settings.custom_domain == domain:
                    settings.custom_domain = None
                    settings.save(update_fields=['custom_domain'])

                return Response({
                    "success": True,
                    "message": f"Domain {domain} removed successfully"
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    "success": False,
                    "error": result.get("error", "Failed to remove domain")
                }, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            return Response({
                "success": False,
                "error": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        tags=['Ecommerce Admin - Settings'],
        summary='Verify domain DNS',
        description='Check if the DNS records for a domain have been configured correctly',
        request=inline_serializer(
            name='VerifyDomainRequest',
            fields={
                'domain': serializers.CharField(help_text='Domain name to verify')
            }
        ),
        responses={
            200: inline_serializer(
                name='VerifyDomainResponse',
                fields={
                    'success': serializers.BooleanField(),
                    'verified': serializers.BooleanField(),
                    'verification': serializers.ListField(child=serializers.DictField()),
                }
            ),
        }
    )
    @action(detail=False, methods=['post'], url_path='verify-domain')
    def verify_domain(self, request):
        """
        Verify DNS configuration for a custom domain
        """
        from .services.vercel_deployment import VercelDeploymentService

        tenant = request.tenant
        domain = request.data.get('domain', '').strip().lower()

        if not domain:
            return Response(
                {"error": "Domain is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        settings, created = EcommerceSettings.objects.get_or_create(
            tenant=tenant,
            defaults={'store_name': tenant.name}
        )

        if not settings.vercel_project_id:
            return Response(
                {"error": "No deployment exists. Deploy frontend first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            service = VercelDeploymentService()
            result = service.verify_domain(settings.vercel_project_id, domain)

            return Response(result, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({
                "success": False,
                "error": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class HomepageSectionViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """
    ViewSet for managing homepage sections (tenant admin).
    Allows CRUD operations and reordering of homepage sections.
    """
    queryset = HomepageSection.objects.all()
    serializer_class = HomepageSectionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['section_type', 'is_active']
    ordering_fields = ['position', 'created_at']
    ordering = ['position']

    def get_queryset(self):
        """Optimize queryset with select_related for item_list"""
        return super().get_queryset().select_related('item_list')

    @extend_schema(
        tags=['Ecommerce Admin - Homepage Builder'],
        summary='List homepage sections',
        description='Get all homepage sections for this tenant, ordered by position'
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Homepage Builder'],
        summary='Create homepage section',
        description='Add a new section to the homepage'
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Homepage Builder'],
        summary='Get homepage section details',
        description='View detailed configuration of a specific section'
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Homepage Builder'],
        summary='Update homepage section',
        description='Modify section configuration'
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Homepage Builder'],
        summary='Partially update homepage section',
        description='Modify specific fields of a section'
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Homepage Builder'],
        summary='Delete homepage section',
        description='Remove a section from the homepage'
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Homepage Builder'],
        summary='Reorder homepage sections',
        description='Update the order of homepage sections by providing an ordered list of section IDs',
        request=HomepageSectionReorderSerializer,
        responses={200: HomepageSectionSerializer(many=True)},
        parameters=[]  # Explicitly exclude inherited query parameters
    )
    @action(detail=False, methods=['post'], url_path='reorder', pagination_class=None, filter_backends=[])
    def reorder(self, request):
        """
        Reorder homepage sections based on provided order of IDs.
        POST body: {"section_ids": [3, 1, 2, 5, 4]}
        """
        serializer = HomepageSectionReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        section_ids = serializer.validated_data['section_ids']

        # Update positions based on the order in the list
        for position, section_id in enumerate(section_ids):
            HomepageSection.objects.filter(id=section_id).update(position=position)

        # Return updated sections in new order
        sections = HomepageSection.objects.all().order_by('position')
        return Response(HomepageSectionSerializer(sections, many=True).data)

    @extend_schema(
        tags=['Ecommerce Admin - Homepage Builder'],
        summary='Get section type choices',
        description='Get available section types and display modes'
    )
    @action(detail=False, methods=['get'], url_path='choices')
    def get_choices(self, request):
        """
        Get available section types and display modes for the frontend form
        """
        return Response({
            'section_types': [
                {'value': choice[0], 'label': choice[1]}
                for choice in HomepageSection.SECTION_TYPE_CHOICES
            ],
            'display_modes': [
                {'value': choice[0], 'label': choice[1]}
                for choice in HomepageSection.DISPLAY_MODE_CHOICES
            ]
        })

    @extend_schema(
        tags=['Ecommerce Admin - Homepage Builder'],
        summary='List available homepage variants',
        description='Get all predefined homepage variant templates with their section layouts'
    )
    @action(detail=False, methods=['get'], url_path='variants')
    def list_variants(self, request):
        """List all available homepage variant templates."""
        from .homepage_variants import HOMEPAGE_VARIANTS
        variants = []
        for key, v in HOMEPAGE_VARIANTS.items():
            variants.append({
                'key': key,
                'name': v['name'],
                'description': v['description'],
                'section_count': len(v['sections']),
                'section_types': [s['section_type'] for s in v['sections']],
            })
        return Response(variants)

    @extend_schema(
        tags=['Ecommerce Admin - Homepage Builder'],
        summary='Generate homepage from variant',
        description='Delete all existing homepage sections and generate new ones from a predefined variant template'
    )
    @action(detail=False, methods=['post'], url_path='generate-from-variant')
    def generate_from_variant(self, request):
        """Generate homepage sections from a predefined variant."""
        from .homepage_variants import HOMEPAGE_VARIANTS

        variant_key = request.data.get('variant')
        if variant_key not in HOMEPAGE_VARIANTS:
            return Response(
                {'error': f'Invalid variant. Choose from: {list(HOMEPAGE_VARIANTS.keys())}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        variant = HOMEPAGE_VARIANTS[variant_key]

        # Delete existing sections
        HomepageSection.objects.all().delete()

        # Create new sections
        created = []
        for section_config in variant['sections']:
            section = HomepageSection.objects.create(**section_config)
            created.append(section)

        # Update settings with selected variant
        settings = EcommerceSettings.objects.first()
        if settings:
            settings.homepage_variant = variant_key
            settings.save(update_fields=['homepage_variant'])

        serializer = HomepageSectionSerializer(created, many=True)
        return Response({
            'variant': variant_key,
            'sections': serializer.data,
            'message': f'Generated {len(created)} sections from "{variant["name"]}" variant'
        })


class ShippingMethodViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """Admin CRUD for shipping methods"""
    queryset = ShippingMethod.objects.all()
    serializer_class = ShippingMethodSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['is_active']
    ordering_fields = ['position', 'price', 'created_at']
    ordering = ['position']

    @extend_schema(tags=['Ecommerce Admin - Shipping'], summary='List shipping methods')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Shipping'], summary='Create shipping method')
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Shipping'], summary='Get shipping method details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Shipping'], summary='Update shipping method')
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Shipping'], summary='Partial update shipping method')
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Shipping'], summary='Delete shipping method')
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


class PromoCodeViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """Admin CRUD for promo codes"""
    queryset = PromoCode.objects.all()
    serializer_class = PromoCodeSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['code']
    filterset_fields = ['is_active', 'discount_type']
    ordering_fields = ['created_at', 'valid_from', 'valid_until', 'times_used']
    ordering = ['-created_at']

    @extend_schema(tags=['Ecommerce Admin - Promo Codes'], summary='List promo codes')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Promo Codes'], summary='Create promo code')
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Promo Codes'], summary='Get promo code details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Promo Codes'], summary='Update promo code')
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Promo Codes'], summary='Partial update promo code')
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Promo Codes'], summary='Delete promo code')
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


class ProductReviewAdminViewSet(NoCacheMixin, viewsets.ModelViewSet):
    """Admin management for product reviews (approve/reject)"""
    queryset = ProductReview.objects.all()
    serializer_class = ProductReviewSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['product', 'is_approved', 'is_verified_purchase', 'rating']
    ordering_fields = ['created_at', 'rating']
    ordering = ['-created_at']

    def get_queryset(self):
        return super().get_queryset().select_related('product', 'client')

    @extend_schema(tags=['Ecommerce Admin - Reviews'], summary='List all reviews')
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Reviews'], summary='Get review details')
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Reviews'], summary='Update review (approve/reject)')
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Reviews'], summary='Partial update review')
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(tags=['Ecommerce Admin - Reviews'], summary='Delete review')
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        tags=['Ecommerce Admin - Reviews'],
        summary='Approve review',
        description='Approve a product review to make it visible on the storefront'
    )
    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        review = self.get_object()
        review.is_approved = True
        review.save(update_fields=['is_approved'])
        return Response(ProductReviewSerializer(review).data)

    @extend_schema(
        tags=['Ecommerce Admin - Reviews'],
        summary='Reject review',
        description='Reject a product review to hide it from the storefront'
    )
    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        review = self.get_object()
        review.is_approved = False
        review.save(update_fields=['is_approved'])
        return Response(ProductReviewSerializer(review).data)


# ============================================================================
# Quickshipper courier integration
# ============================================================================
#
# Three endpoints:
#  - admin "Test Connection"      POST /api/ecommerce/admin/quickshipper/test-connection/
#  - storefront live quote        POST /api/ecommerce/client/shipping/quote/
#  - inbound status webhook       POST /api/ecommerce/shipping-webhook/quickshipper/
#
# All three keep tenant resolution implicit via django-tenant-schemas — the
# request is already in the right schema context, EcommerceSettings is a
# OneToOne on the tenant, so settings = EcommerceSettings.objects.first()
# (matching the convention used by the BOG handlers).

import hashlib
import hmac
import logging as _qs_logging

from django.conf import settings as django_settings
from django.utils import timezone

from .services.quickshipper import (
    QuickshipperClient,
    QuickshipperError,
    client_from_settings,
)
# Aliased to a private name so it doesn't conflict with anything in this
# already-large module's namespace, and because the storefront's
# ``/shipping/quote/`` is the only place we currently use it inside
# ``views.py`` (the rest live in views_client.py).
from .authentication import EcommerceClientJWTAuthentication as _EcommerceClientJWTAuthentication

_qs_logger = _qs_logging.getLogger(__name__)


def _quickshipper_settings_or_none():
    """Return the per-tenant EcommerceSettings or None if missing."""
    return EcommerceSettings.objects.first()


def _build_drop_off_payload(address, fallback_phone: str = "") -> dict:
    """Map a :class:`ClientAddress` onto Quickshipper's OrderDropOffModel."""
    return {
        "address": address.address or "",
        "addressComment": address.extra_instructions or "",
        "city": address.city or "",
        "country": address.country or "",
        "latitude": float(address.latitude) if address.latitude is not None else 0,
        "longitude": float(address.longitude) if address.longitude is not None else 0,
        "name": address.client.full_name if address.client_id else "",
        "phone": (address.client.phone if address.client_id and address.client.phone else fallback_phone) or "",
        "phonePrefix": "",
    }


def _build_pickup_payload(s) -> dict:
    """Map EcommerceSettings pickup fields onto Quickshipper's OrderPickUpModel."""
    return {
        "address": s.quickshipper_pickup_address or "",
        "addressComment": s.quickshipper_pickup_extra_instructions or "",
        "city": s.quickshipper_pickup_city or "",
        "country": "",
        "latitude": float(s.quickshipper_pickup_latitude) if s.quickshipper_pickup_latitude is not None else 0,
        "longitude": float(s.quickshipper_pickup_longitude) if s.quickshipper_pickup_longitude is not None else 0,
        "name": s.quickshipper_pickup_contact_name or "",
        "phone": s.quickshipper_pickup_phone or "",
        "phonePrefix": "",
    }


@extend_schema(
    operation_id='ecommerce_quickshipper_test_connection',
    tags=['Ecommerce Admin - Delivery'],
    summary='Test Quickshipper credentials',
    description=(
        "Calls Quickshipper's GET /v1/Account with the tenant's stored API key. "
        "Returns 200 with the account profile on success, 4xx with the upstream "
        "error message on failure. Used by the admin 'Test Connection' button."
    ),
    request=None,
    responses={
        200: OpenApiResponse(description='Credentials valid'),
        400: OpenApiResponse(description='Quickshipper rejected the request'),
        424: OpenApiResponse(description='Quickshipper not configured for this tenant'),
    },
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def quickshipper_test_connection(request):
    settings_obj = _quickshipper_settings_or_none()
    client = client_from_settings(settings_obj)
    if client is None:
        return Response(
            {'success': False, 'error': 'Quickshipper API key not configured for this tenant'},
            status=status.HTTP_424_FAILED_DEPENDENCY,
        )
    try:
        info = client.account_info()
    except QuickshipperError as exc:
        return Response(
            {'success': False, 'error': str(exc), 'errors': exc.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response({
        'success': True,
        'shop_name': info.get('shopName'),
        'user_name': info.get('userName'),
        'balance': info.get('balance'),
        'has_providers': info.get('hasProviders', False),
    })


@extend_schema(
    operation_id='ecommerce_quickshipper_quote',
    tags=['Ecommerce Client - Shipping'],
    summary='Get live shipping quote from Quickshipper',
    description=(
        "Computes a courier delivery quote from the tenant's Quickshipper-configured "
        "pickup address to the cart's selected delivery address. Used by checkout to "
        "replace static ShippingMethod rows with a real-time price + ETA when "
        "Quickshipper is enabled."
    ),
    request=inline_serializer(
        name='QuickshipperQuoteRequest',
        fields={
            'cart_id': serializers.IntegerField(),
            'delivery_address_id': serializers.IntegerField(),
        },
    ),
    responses={
        200: OpenApiResponse(description='Quote computed'),
        400: OpenApiResponse(description='Bad request (missing fields or no quote available)'),
        404: OpenApiResponse(description='Cart or address not found'),
        424: OpenApiResponse(description='Quickshipper not configured for this tenant'),
    },
)
@api_view(['POST'])
@authentication_classes([_EcommerceClientJWTAuthentication])
@permission_classes([IsAuthenticated])
def quickshipper_quote(request):
    cart_id = request.data.get('cart_id')
    delivery_address_id = request.data.get('delivery_address_id')
    if not cart_id or not delivery_address_id:
        return Response(
            {'error': 'cart_id and delivery_address_id are required'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    settings_obj = _quickshipper_settings_or_none()
    if not settings_obj or not settings_obj.quickshipper_enabled:
        return Response(
            {'error': 'Quickshipper is not enabled for this tenant'},
            status=status.HTTP_424_FAILED_DEPENDENCY,
        )
    client = client_from_settings(settings_obj)
    if client is None:
        return Response(
            {'error': 'Quickshipper API key not configured'},
            status=status.HTTP_424_FAILED_DEPENDENCY,
        )

    try:
        cart = Cart.objects.prefetch_related('items').get(id=cart_id, client=request.user)
    except Cart.DoesNotExist:
        return Response({'error': 'Cart not found'}, status=status.HTTP_404_NOT_FOUND)
    try:
        address = ClientAddress.objects.select_related('client').get(
            id=delivery_address_id, client=request.user,
        )
    except ClientAddress.DoesNotExist:
        return Response({'error': 'Address not found'}, status=status.HTTP_404_NOT_FOUND)

    if address.latitude is None or address.longitude is None:
        return Response(
            {'error': 'Delivery address is missing lat/lng — drop a pin on the map first.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if settings_obj.quickshipper_pickup_latitude is None or settings_obj.quickshipper_pickup_longitude is None:
        return Response(
            {'error': 'Tenant pickup address is missing lat/lng. Ask the shop owner to set it under Settings → Ecommerce → Delivery.'},
            status=status.HTTP_424_FAILED_DEPENDENCY,
        )

    cart_amount = float(cart.total_amount or 0)
    # Best-effort weight: sum of item weights (kg). If a product has no
    # weight on file, default to 0.5kg per unit so the quote isn't
    # rejected for missing weight.
    cart_weight = 0.0
    for item in cart.items.all():
        w = getattr(item.product, 'weight', None) if item.product_id else None
        cart_weight += float(w) * item.quantity if w else 0.5 * item.quantity

    try:
        envelope = client.get_quote(
            from_lat=settings_obj.quickshipper_pickup_latitude,
            from_lng=settings_obj.quickshipper_pickup_longitude,
            from_street=settings_obj.quickshipper_pickup_address,
            from_city=settings_obj.quickshipper_pickup_city,
            to_lat=address.latitude,
            to_lng=address.longitude,
            to_street=address.address,
            to_city=address.city,
            cart_amount=cart_amount,
            cart_weight=cart_weight,
        )
    except QuickshipperError as exc:
        _qs_logger.info('Quickshipper quote failed: %s', exc)
        return Response(
            {'error': str(exc), 'errors': exc.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    fees = envelope.get('fees') or []
    if not fees:
        return Response(
            {'error': 'No couriers available for this route — try a different address or check back later.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Flatten Quickshipper's nested (provider × prices) into a flat list of
    # selectable courier options. Each entry is one (courier, weight-tier)
    # pair the customer can pick at checkout. Includes everything the booking
    # task needs later — provider_id, provider_fee_id, parcel_dimensions_id —
    # so we can persist the customer's exact choice and book what they paid
    # for, not just the cheapest re-quote.
    #
    # Field names below match the LIVE Delivery API response (probed against
    # delivery.quickshipper.app — the OpenAPI spec at /swagger/v1/swagger.json
    # uses generic camelCase names that don't match runtime payloads).
    options: list[dict] = []
    for fee in fees:
        if fee.get('isActive') is False:
            continue
        for price in (fee.get('prices') or []):
            # Live response uses `amount` for the per-tier price. Fall back to
            # `userPrice` for forward-compat in case Quickshipper aligns the
            # field names with their published spec later.
            amount = price.get('amount')
            if amount is None:
                amount = price.get('userPrice')
            if amount is None:
                continue
            currency = (
                price.get('currencyName')
                or price.get('currency')
                or 'GEL'
            )
            # The Lari sign (₾) shows up as `currency` on some tiers — coerce
            # to the ISO code for consistency on the storefront.
            if currency in ('₾', '₾'):
                currency = 'GEL'
            options.append({
                'provider_id': fee.get('providerId'),
                'provider_name': fee.get('providerName'),
                'provider_code': fee.get('providerCode'),
                'provider_logo_url': fee.get('providerLogoUrl'),
                'provider_note': fee.get('providerNote'),
                'allow_cash_on_delivery': fee.get('hasCashOnDelivery', False),
                # Per-tier ID is plain `id` in the live response; the
                # `providerFeeId` name in the spec is unused. It can be
                # alphanumeric (e.g. `"asap"`, `"Bike"`, `"57"`).
                'provider_fee_id': price.get('id') or price.get('providerFeeId'),
                'parcel_dimensions_id': price.get('parcelDimensionsId'),
                'delivery_speed_id': price.get('deliverySpeedId'),
                'price': float(amount),
                'currency': currency,
                'display_name': price.get('deliverySpeedName') or price.get('displayName'),
                'has_car_delivery': bool(price.get('hasCarDelivery')),
                'min_weight': price.get('minWeight'),
                'max_weight': price.get('maxWeight'),
                'estimated_minutes': price.get('estimatedMinutes') or price.get('estimatedDuration'),
                'service_fee': fee.get('serviceFee'),
            })

    if not options:
        return Response(
            {'error': 'No couriers available for this route — try a different address or check back later.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Default-selected = cheapest. Storefront pre-selects this so the
    # checkout still works one-click for users who don't care; users who do
    # care see the full list and can switch.
    options.sort(key=lambda o: o['price'])
    default_option = options[0]

    return Response({
        'provider': 'quickshipper',
        # Fields kept for backward compatibility with the v1 single-quote UI.
        'provider_id': default_option['provider_id'],
        'provider_name': default_option['provider_name'],
        'provider_logo_url': default_option['provider_logo_url'],
        'provider_fee_id': default_option['provider_fee_id'],
        'parcel_dimensions_id': default_option['parcel_dimensions_id'],
        'price': default_option['price'],
        'currency': default_option['currency'],
        'display_name': default_option['display_name'],
        'distance_km': envelope.get('distance'),
        # New: full list of selectable courier options + the cheapest as default.
        'options': options,
        'default_provider_fee_id': default_option['provider_fee_id'],
    })


@extend_schema(
    operation_id='ecommerce_quickshipper_quote_guest',
    tags=['Ecommerce Client - Shipping'],
    summary='Get live shipping quote (guest)',
    description=(
        "Same as the authenticated /shipping/quote/ endpoint, but accepts "
        "raw `items` + delivery lat/lng/street/city instead of requiring "
        "a saved cart and a saved-address row. Used by guest checkout."
    ),
    request=inline_serializer(
        name='QuickshipperQuoteGuestRequest',
        fields={
            'items': serializers.ListField(
                child=inline_serializer(
                    name='QuickshipperGuestItem',
                    fields={
                        'product_id': serializers.IntegerField(),
                        'quantity': serializers.IntegerField(),
                    },
                ),
            ),
            'to_lat': serializers.FloatField(),
            'to_lng': serializers.FloatField(),
            'to_street': serializers.CharField(),
            'to_city': serializers.CharField(),
        },
    ),
    responses={
        200: OpenApiResponse(description='Quote computed'),
        400: OpenApiResponse(description='Bad request'),
        424: OpenApiResponse(description='Quickshipper not configured'),
    },
)
@api_view(['POST'])
@permission_classes([AllowAny])
def quickshipper_quote_guest(request):
    """Public Quickshipper quote — same envelope, no auth, no cart row.

    Used by the guest checkout flow before an order or address row exists.
    Authenticated visitors should still hit /shipping/quote/ which goes
    through their saved cart + address.
    """
    items_data = request.data.get('items') or []
    to_lat = request.data.get('to_lat')
    to_lng = request.data.get('to_lng')
    to_street = request.data.get('to_street') or ''
    to_city = request.data.get('to_city') or ''

    if not items_data:
        return Response({'error': 'items is required'}, status=status.HTTP_400_BAD_REQUEST)
    if to_lat is None or to_lng is None:
        return Response(
            {'error': 'to_lat and to_lng are required — drop a pin on the map first.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    settings_obj = _quickshipper_settings_or_none()
    if not settings_obj or not settings_obj.quickshipper_enabled:
        return Response(
            {'error': 'Quickshipper is not enabled for this tenant'},
            status=status.HTTP_424_FAILED_DEPENDENCY,
        )
    qs_client = client_from_settings(settings_obj)
    if qs_client is None:
        return Response(
            {'error': 'Quickshipper API key not configured'},
            status=status.HTTP_424_FAILED_DEPENDENCY,
        )
    if settings_obj.quickshipper_pickup_latitude is None or settings_obj.quickshipper_pickup_longitude is None:
        return Response(
            {'error': 'Tenant pickup address is missing lat/lng. Ask the shop owner to set it under Settings → Ecommerce → Delivery.'},
            status=status.HTTP_424_FAILED_DEPENDENCY,
        )

    # Compute cart amount + weight from raw items, mirroring the auth path.
    from decimal import Decimal as _Decimal
    cart_amount = 0.0
    cart_weight = 0.0
    for item in items_data:
        try:
            product = Product.objects.get(id=item.get('product_id'), status='active')
        except Product.DoesNotExist:
            continue
        qty = int(item.get('quantity') or 1)
        cart_amount += float(product.price) * qty
        w = getattr(product, 'weight', None)
        cart_weight += float(w) * qty if w else 0.5 * qty

    try:
        envelope = qs_client.get_quote(
            from_lat=settings_obj.quickshipper_pickup_latitude,
            from_lng=settings_obj.quickshipper_pickup_longitude,
            from_street=settings_obj.quickshipper_pickup_address,
            from_city=settings_obj.quickshipper_pickup_city,
            to_lat=to_lat,
            to_lng=to_lng,
            to_street=to_street,
            to_city=to_city,
            cart_amount=cart_amount,
            cart_weight=cart_weight,
        )
    except QuickshipperError as exc:
        _qs_logger.info('Quickshipper guest quote failed: %s', exc)
        return Response(
            {'error': str(exc), 'errors': exc.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    fees = envelope.get('fees') or []
    if not fees:
        return Response(
            {'error': 'No couriers available for this route — try a different address or check back later.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Same flatten/normalise logic as the authed endpoint. Kept inline
    # rather than factored out because the only difference is the input
    # source (cart row vs raw items list); duplicating the 50 lines is
    # cheaper than adding a helper that has to know about both shapes.
    options: list[dict] = []
    for fee in fees:
        if fee.get('isActive') is False:
            continue
        for price in (fee.get('prices') or []):
            amount = price.get('amount')
            if amount is None:
                amount = price.get('userPrice')
            if amount is None:
                continue
            currency = (
                price.get('currencyName')
                or price.get('currency')
                or 'GEL'
            )
            if currency in ('₾', '₾'):
                currency = 'GEL'
            options.append({
                'provider_id': fee.get('providerId'),
                'provider_name': fee.get('providerName'),
                'provider_code': fee.get('providerCode'),
                'provider_logo_url': fee.get('providerLogoUrl'),
                'provider_note': fee.get('providerNote'),
                'allow_cash_on_delivery': fee.get('hasCashOnDelivery', False),
                'provider_fee_id': price.get('id') or price.get('providerFeeId'),
                'parcel_dimensions_id': price.get('parcelDimensionsId'),
                'delivery_speed_id': price.get('deliverySpeedId'),
                'price': float(amount),
                'currency': currency,
                'display_name': price.get('deliverySpeedName') or price.get('displayName'),
                'has_car_delivery': bool(price.get('hasCarDelivery')),
                'min_weight': price.get('minWeight'),
                'max_weight': price.get('maxWeight'),
                'estimated_minutes': price.get('estimatedMinutes') or price.get('estimatedDuration'),
                'service_fee': fee.get('serviceFee'),
            })

    if not options:
        return Response(
            {'error': 'No couriers available for this route — try a different address or check back later.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    options.sort(key=lambda o: o['price'])
    default_option = options[0]
    return Response({
        'provider': 'quickshipper',
        'provider_id': default_option['provider_id'],
        'provider_name': default_option['provider_name'],
        'provider_logo_url': default_option['provider_logo_url'],
        'provider_fee_id': default_option['provider_fee_id'],
        'parcel_dimensions_id': default_option['parcel_dimensions_id'],
        'price': default_option['price'],
        'currency': default_option['currency'],
        'display_name': default_option['display_name'],
        'distance_km': envelope.get('distance'),
        'options': options,
        'default_provider_fee_id': default_option['provider_fee_id'],
    })


@extend_schema(
    operation_id='ecommerce_quickshipper_webhook',
    tags=['Ecommerce Webhooks - Shipping'],
    summary='Quickshipper status callback',
    description=(
        "Inbound status webhook from Quickshipper. Verifies the X-QuickShipper-"
        "Signature HMAC against the tenant's stored webhook secret, then updates "
        "the matched Order's tracking timestamps. Idempotent."
    ),
    request=None,
    responses={
        200: OpenApiResponse(description='Webhook accepted'),
        401: OpenApiResponse(description='Invalid signature'),
        404: OpenApiResponse(description='Order not found'),
    },
)
@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
def quickshipper_webhook(request):
    raw_body = request.body or b''
    signature = (
        request.headers.get('X-QuickShipper-Signature')
        or request.headers.get('X-Signature')
        or ''
    )

    settings_obj = _quickshipper_settings_or_none()
    if not settings_obj:
        return Response({'error': 'Tenant not configured'}, status=status.HTTP_404_NOT_FOUND)

    secret = settings_obj.quickshipper_webhook_secret
    if secret and signature:
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return Response(
                {'error': 'Invalid signature'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

    payload = request.data or {}
    qs_order_id = payload.get('orderId') or payload.get('OrderId')
    integration_order_id = (
        payload.get('integrationOrderId')
        or payload.get('IntegrationOrderId')
        or payload.get('externalOrderId')
    )
    new_status = (payload.get('status') or payload.get('OrderStatus') or '').strip()

    order = None
    if integration_order_id:
        order = Order.objects.filter(order_number=integration_order_id).first()
    if order is None and qs_order_id is not None:
        order = Order.objects.filter(tracking_number=str(qs_order_id)).first()
    if order is None:
        return Response({'error': 'Order not matched'}, status=status.HTTP_404_NOT_FOUND)

    update_fields = []
    if qs_order_id and not order.tracking_number:
        order.tracking_number = str(qs_order_id)
        update_fields.append('tracking_number')
    if not order.courier_provider:
        order.courier_provider = 'quickshipper'
        update_fields.append('courier_provider')

    # Map upstream status to our local lifecycle. Only progress forward —
    # never rewind from delivered → shipped, never overwrite cancelled.
    status_normalized = new_status.lower()
    now = timezone.now()
    if status_normalized in {'pickedup', 'picked_up', 'pickup', 'inprogress', 'in_progress', 'delivering', 'shipping'} and order.status not in ('shipped', 'delivered', 'cancelled', 'refunded'):
        order.status = 'shipped'
        order.shipped_at = order.shipped_at or now
        update_fields += ['status', 'shipped_at']
    elif status_normalized in {'delivered', 'finished', 'completed'} and order.status not in ('delivered', 'cancelled', 'refunded'):
        order.status = 'delivered'
        order.delivered_at = order.delivered_at or now
        order.shipped_at = order.shipped_at or now
        update_fields += ['status', 'delivered_at', 'shipped_at']
    elif status_normalized in {'cancelled', 'canceled', 'rejected', 'failed'} and order.status not in ('delivered', 'refunded'):
        order.status = 'cancelled'
        order.cancelled_at = order.cancelled_at or now
        update_fields += ['status', 'cancelled_at']

    if update_fields:
        order.updated_at = now
        update_fields.append('updated_at')
        order.save(update_fields=list(set(update_fields)))

    return Response({'success': True, 'order_id': order.id, 'order_status': order.status})
