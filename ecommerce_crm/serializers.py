from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from .models import (
    Language,
    AttributeDefinition,
    Product,
    ProductImage,
    ProductAttributeValue,
    ProductVariant,
    ProductVariantAttributeValue,
    EcommerceClient,
    ClientAddress,
    FavoriteProduct,
    Cart,
    CartItem,
    Order,
    OrderItem,
    EcommerceSettings,
    ClientCard,
    HomepageSection,
    ShippingMethod,
    PromoCode,
    ProductReview,
)
from tickets.models import ItemList, ListItem


class LanguageSerializer(serializers.ModelSerializer):
    """Serializer for languages"""
    class Meta:
        model = Language
        fields = ['id', 'code', 'name', 'is_default', 'is_active', 'sort_order', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class AttributeDefinitionSerializer(serializers.ModelSerializer):
    """Serializer for attribute definitions"""

    class Meta:
        model = AttributeDefinition
        fields = [
            'id', 'name', 'key', 'attribute_type', 'options', 'unit',
            'is_required', 'is_filterable',
            'sort_order', 'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']


class ProductImageSerializer(serializers.ModelSerializer):
    """Serializer for product images"""

    class Meta:
        model = ProductImage
        fields = ['id', 'image', 'alt_text', 'sort_order', 'created_at']
        read_only_fields = ['created_at']


class ProductAttributeValueSerializer(serializers.ModelSerializer):
    """Serializer for product attribute values"""
    attribute = AttributeDefinitionSerializer(read_only=True)
    attribute_id = serializers.PrimaryKeyRelatedField(
        queryset=AttributeDefinition.objects.all(),
        source='attribute',
        write_only=True
    )
    value = serializers.SerializerMethodField()

    class Meta:
        model = ProductAttributeValue
        fields = [
            'id', 'attribute', 'attribute_id', 'value',
            'value_text', 'value_number', 'value_boolean', 'value_date', 'value_json',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def get_value(self, obj):
        """Get the appropriate value based on attribute type"""
        return obj.get_value()


class ProductVariantAttributeValueSerializer(serializers.ModelSerializer):
    """Serializer for product variant attribute values"""
    attribute = AttributeDefinitionSerializer(read_only=True)
    attribute_id = serializers.PrimaryKeyRelatedField(
        queryset=AttributeDefinition.objects.all(),
        source='attribute',
        write_only=True
    )

    class Meta:
        model = ProductVariantAttributeValue
        fields = ['id', 'attribute', 'attribute_id', 'value_json']


class ProductVariantSerializer(serializers.ModelSerializer):
    """Serializer for product variants"""
    attribute_values = ProductVariantAttributeValueSerializer(many=True, read_only=True)
    effective_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = ProductVariant
        fields = [
            'id', 'sku', 'name', 'price', 'effective_price', 'quantity',
            'image', 'is_active', 'sort_order', 'attribute_values',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']


def _first_image_url(raw):
    """Some legacy uploads concatenate multiple URLs into the single
    ``Product.image`` URLField separated by ``", "``. The field is
    declared as one URL and downstream consumers (storefront,
    open-graph scrapers, image CDNs) all assume one URL — return only
    the first valid http(s) URL so a comma-separated string doesn't
    leak through verbatim and break ``next/image`` etc.
    """
    if not raw:
        return raw
    for part in str(raw).split(','):
        s = part.strip()
        if s.startswith('http://') or s.startswith('https://'):
            return s
    return raw


def _split_image_urls(raw):
    """Split a comma-separated ``image`` string into a clean list of
    URLs. Used by the read-only ``images_resolved`` field so storefront
    consumers always have an array regardless of whether uploads went
    through the admin's bulk-paste flow (saving "url1, url2" into
    ``Product.image``) or the proper ``add_image`` endpoint (which
    creates ``ProductImage`` rows).
    """
    if not raw:
        return []
    out = []
    for part in str(raw).split(','):
        s = part.strip()
        if (s.startswith('http://') or s.startswith('https://')) and s not in out:
            out.append(s)
    return out


def _merged_product_images(obj):
    """Merge legacy comma-separated ``Product.image`` URLs with real
    ``ProductImage`` rows into the storefront-expected list shape.
    Pseudo-rows from the legacy field get negative IDs so React keys
    don't collide with real rows. Deduped by URL."""
    seen = set()
    out = []
    for idx, url in enumerate(_split_image_urls(obj.image)):
        if url in seen:
            continue
        seen.add(url)
        out.append({
            'id': -(idx + 1),
            'image': url,
            'alt_text': None,
            'sort_order': idx,
            'created_at': obj.updated_at,
        })
    # Sort in Python so a prefetched `images` cache is reused. Calling
    # .order_by() on the related manager would clone the queryset and force a
    # fresh per-product query, defeating the prefetch.
    rows = sorted(obj.images.all(), key=lambda r: (r.sort_order, r.id))
    for row in rows:
        if row.image in seen:
            continue
        seen.add(row.image)
        out.append(ProductImageSerializer(row).data)
    return out


def _annotated_average_rating(obj):
    """Read the ``_avg_rating`` annotation set by ProductViewSet.get_queryset,
    falling back to a query when the object wasn't loaded through it."""
    if hasattr(obj, '_avg_rating'):
        avg = obj._avg_rating
    else:
        from django.db.models import Avg
        avg = obj.reviews.filter(is_approved=True).aggregate(avg=Avg('rating'))['avg']
    return round(avg, 1) if avg else None


def _annotated_review_count(obj):
    """Read the ``_review_count`` annotation, falling back to a COUNT query."""
    if hasattr(obj, '_review_count'):
        return obj._review_count or 0
    return obj.reviews.filter(is_approved=True).count()


class ProductListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for product listings"""
    discount_percentage = serializers.FloatField(read_only=True)
    is_low_stock = serializers.BooleanField(read_only=True)
    is_in_stock = serializers.BooleanField(read_only=True)
    attribute_values = ProductAttributeValueSerializer(many=True, read_only=True)
    average_rating = serializers.SerializerMethodField()
    review_count = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    # Listing cards use the second image (when present) for the hover
    # swap. Expose the same merged list the detail page sees so the
    # storefront can render it without a second fetch.
    images = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'sku', 'slug', 'name', 'short_description',
            'price', 'compare_at_price', 'discount_percentage',
            'image', 'images', 'quantity', 'status', 'is_featured',
            'is_low_stock', 'is_in_stock', 'attribute_values',
            'average_rating', 'review_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def get_image(self, obj):
        return _first_image_url(obj.image)

    @extend_schema_field(ProductImageSerializer(many=True))
    def get_images(self, obj):
        return _merged_product_images(obj)

    def get_average_rating(self, obj):
        return _annotated_average_rating(obj)

    def get_review_count(self, obj):
        return _annotated_review_count(obj)


class ProductDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for product with all related data"""
    images = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    attribute_values = ProductAttributeValueSerializer(many=True, read_only=True)
    variants = ProductVariantSerializer(many=True, read_only=True)
    discount_percentage = serializers.FloatField(read_only=True)
    is_low_stock = serializers.BooleanField(read_only=True)
    is_in_stock = serializers.BooleanField(read_only=True)
    average_rating = serializers.SerializerMethodField()
    review_count = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()

    def get_image(self, obj):
        return _first_image_url(obj.image)

    @extend_schema_field(ProductImageSerializer(many=True))
    def get_images(self, obj):
        return _merged_product_images(obj)

    class Meta:
        model = Product
        fields = [
            'id', 'sku', 'slug', 'name', 'description', 'short_description',
            'price', 'compare_at_price', 'cost_price', 'discount_percentage',
            'image', 'images', 'track_inventory', 'quantity', 'low_stock_threshold',
            'is_low_stock', 'is_in_stock', 'status', 'is_featured',
            'weight', 'dimensions', 'meta_title', 'meta_description',
            'attribute_values', 'variants',
            'average_rating', 'review_count',
            'created_at', 'updated_at', 'created_by', 'created_by_name',
            'updated_by', 'updated_by_name'
        ]
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'updated_by']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return f"{obj.created_by.first_name} {obj.created_by.last_name}".strip() or obj.created_by.email
        return None

    def get_updated_by_name(self, obj):
        if obj.updated_by:
            return f"{obj.updated_by.first_name} {obj.updated_by.last_name}".strip() or obj.updated_by.email
        return None

    def get_average_rating(self, obj):
        return _annotated_average_rating(obj)

    def get_review_count(self, obj):
        return _annotated_review_count(obj)


class ProductCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating products with attributes"""
    slug = serializers.CharField(required=False, allow_blank=True, help_text="Auto-generated from SKU if not provided")
    image = serializers.CharField(required=False, allow_null=True, allow_blank=True, help_text="Image URL string")
    attributes = serializers.ListField(
        child=serializers.DictField(),
        write_only=True,
        required=False,
        help_text="List of attribute values: [{'attribute_id': 1, 'value_text': 'Red'}, ...]"
    )
    images_data = serializers.ListField(
        child=serializers.DictField(),
        write_only=True,
        required=False,
        help_text="List of additional images: [{'image': file, 'alt_text': {...}, 'sort_order': 0}, ...]"
    )

    class Meta:
        model = Product
        fields = [
            'id', 'sku', 'slug', 'name', 'description', 'short_description',
            'price', 'compare_at_price', 'cost_price',
            'image', 'track_inventory', 'quantity', 'low_stock_threshold',
            'status', 'is_featured', 'weight', 'dimensions',
            'meta_title', 'meta_description', 'attributes', 'images_data'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate_sku(self, value):
        """Validate SKU length"""
        if len(value) > 100:
            raise serializers.ValidationError(
                f"SKU must be 100 characters or less. Current length: {len(value)}"
            )
        return value

    def validate_slug(self, value):
        """Validate slug length - allow blank for auto-generation"""
        if value and len(value) > 200:
            raise serializers.ValidationError(
                f"Slug must be 200 characters or less. Current length: {len(value)}"
            )
        return value

    def validate_image(self, value):
        """Validate image URL length"""
        if value and len(value) > 2000:
            raise serializers.ValidationError(
                f"Image URL must be 2000 characters or less. Current length: {len(value)}. "
                f"Please use a shorter URL or filename."
            )
        return value

    def _normalise_image_field(self, validated_data):
        """The admin's image-upload widget sometimes posts multiple URLs
        joined with ", " into the single `Product.image` URLField. The
        field is declared as one URL and downstream consumers (storefront,
        OG scrapers, JSON-LD) all assume one URL — so split here at write
        time: keep the first URL as the primary `image`, and stash the
        rest in `_extra_image_urls` for after-save ProductImage creation.
        """
        raw = validated_data.get('image')
        extras = []
        if raw and ',' in str(raw):
            parts = [p.strip() for p in str(raw).split(',')]
            urls = [p for p in parts if p.startswith('http://') or p.startswith('https://')]
            if urls:
                validated_data['image'] = urls[0]
                extras = urls[1:]
            else:
                validated_data['image'] = raw  # leave raw if no valid URLs
        return extras

    def _create_extra_images(self, product, urls):
        if not urls:
            return
        existing = set(
            product.images.values_list('image', flat=True)
        ) if product.pk else set()
        for idx, url in enumerate(urls):
            if url in existing:
                continue
            ProductImage.objects.create(
                product=product,
                image=url,
                sort_order=idx + 1,  # primary occupies sort_order 0 implicitly
            )

    def create(self, validated_data):
        # Extract nested data
        attributes_data = validated_data.pop('attributes', [])
        images_data = validated_data.pop('images_data', [])

        # Split a comma-joined `image` URL string into primary + extras
        # (legacy admin upload pattern — see _normalise_image_field).
        extra_image_urls = self._normalise_image_field(validated_data)

        # Set created_by from request user
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['created_by'] = request.user
            validated_data['updated_by'] = request.user

        # Auto-generate slug if not provided
        if not validated_data.get('slug'):
            from django.utils.text import slugify
            import uuid
            # Try to generate from SKU first
            base_slug = slugify(validated_data.get('sku', ''))
            if not base_slug:
                # Fallback to uuid
                base_slug = f"product-{uuid.uuid4().hex[:8]}"
            slug = base_slug
            counter = 1
            while Product.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            validated_data['slug'] = slug

        # Create product
        product = super().create(validated_data)

        # Persist any extra URLs from the comma-joined input as proper
        # ProductImage rows so the storefront can render the gallery.
        self._create_extra_images(product, extra_image_urls)

        # Create attribute values
        for attr_data in attributes_data:
            attribute_id = attr_data.get('attribute_id')
            if attribute_id:
                attr_value = ProductAttributeValue(
                    product=product,
                    attribute_id=attribute_id
                )
                # Set appropriate value field based on attribute type
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

        # Create additional images
        for img_data in images_data:
            ProductImage.objects.create(
                product=product,
                **img_data
            )

        return product

    def update(self, instance, validated_data):
        # Extract nested data
        attributes_data = validated_data.pop('attributes', None)
        images_data = validated_data.pop('images_data', None)

        # Split a comma-joined `image` URL string into primary + extras
        # so updates from the legacy admin form persist correctly.
        extra_image_urls = self._normalise_image_field(validated_data)

        # Set updated_by from request user
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['updated_by'] = request.user

        # Update product
        product = super().update(instance, validated_data)

        # Persist any new extra URLs as proper ProductImage rows.
        self._create_extra_images(product, extra_image_urls)

        # Update attributes if provided
        if attributes_data is not None:
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

        # Update images if provided
        if images_data is not None:
            # Note: This doesn't delete existing images, just adds new ones
            # You may want to implement a different strategy
            for img_data in images_data:
                ProductImage.objects.create(
                    product=product,
                    **img_data
                )

        return product


class ClientAddressSerializer(serializers.ModelSerializer):
    """Serializer for client addresses (Admin only)"""

    class Meta:
        model = ClientAddress
        fields = [
            'id', 'client', 'label', 'address', 'city',
            'extra_instructions', 'latitude', 'longitude',
            'postal_code', 'country',
            'is_default', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, attrs):
        """Ensure coordinates are both set or both unset"""
        latitude = attrs.get('latitude')
        longitude = attrs.get('longitude')

        if (latitude is not None and longitude is None) or (latitude is None and longitude is not None):
            raise serializers.ValidationError(
                "Both latitude and longitude must be provided together"
            )

        return attrs


class FavoriteProductSerializer(serializers.ModelSerializer):
    """Serializer for favorite products with nested product details"""
    product = serializers.SerializerMethodField()

    class Meta:
        model = FavoriteProduct
        fields = ['id', 'client', 'product', 'created_at']
        read_only_fields = ['id', 'created_at']

    def get_product(self, obj):
        """Return product details in the current language context"""
        from .serializers import ProductListSerializer
        language = self.context.get('language', 'en')
        return ProductListSerializer(obj.product, context={'language': language}).data


class FavoriteProductCreateSerializer(serializers.ModelSerializer):
    """Serializer for adding products to favorites"""

    class Meta:
        model = FavoriteProduct
        fields = ['id', 'client', 'product', 'created_at']
        read_only_fields = ['id', 'created_at']


class EcommerceClientListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for client lists - excludes nested addresses and favorites"""
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = None  # Will be set dynamically
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'phone_number', 'date_of_birth', 'is_active', 'is_verified', 'last_login', 'created_at'
        ]
        read_only_fields = ['id', 'is_verified', 'created_at']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .models import EcommerceClient
        self.Meta.model = EcommerceClient


class EcommerceClientSerializer(serializers.ModelSerializer):
    """Serializer for listing and viewing ecommerce clients"""
    full_name = serializers.CharField(read_only=True)
    addresses = ClientAddressSerializer(many=True, read_only=True)
    favorites = serializers.SerializerMethodField()

    class Meta:
        model = None  # Will be set dynamically
        fields = [
            'id', 'first_name', 'last_name', 'full_name', 'email',
            'phone_number', 'date_of_birth', 'is_active', 'is_verified',
            'last_login', 'created_at', 'updated_at', 'addresses', 'favorites'
        ]
        read_only_fields = ['id', 'is_verified', 'last_login', 'created_at', 'updated_at']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .models import EcommerceClient
        self.Meta.model = EcommerceClient

    def get_favorites(self, obj):
        """Return client's favorite products with product details"""
        from .serializers import FavoriteProductSerializer
        language = self.context.get('language', 'en')
        favorites = obj.favorites.all()
        return FavoriteProductSerializer(favorites, many=True, context={'language': language}).data


class ClientRegistrationSerializer(serializers.Serializer):
    """Serializer for client registration"""
    first_name = serializers.CharField(max_length=150, required=True)
    last_name = serializers.CharField(max_length=150, required=True)
    email = serializers.EmailField(required=True)
    phone_number = serializers.CharField(max_length=20, required=True)
    password = serializers.CharField(write_only=True, required=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True, required=True)
    date_of_birth = serializers.DateField(required=False, allow_null=True)

    def validate_email(self, value):
        """Validate that email is unique"""
        from .models import EcommerceClient
        if EcommerceClient.objects.filter(email=value).exists():
            raise serializers.ValidationError("A client with this email already exists.")
        return value

    def validate_phone_number(self, value):
        """Validate that phone number is unique"""
        from .models import EcommerceClient
        if EcommerceClient.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError("A client with this phone number already exists.")
        return value

    def validate(self, data):
        """Validate that passwords match"""
        if data.get('password') != data.get('password_confirm'):
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        return data

    def create(self, validated_data):
        """Create and return a new client"""
        from .models import EcommerceClient

        # Remove password_confirm from validated data
        validated_data.pop('password_confirm')

        # Extract password
        password = validated_data.pop('password')

        # Create client
        client = EcommerceClient(**validated_data)
        client.set_password(password)
        client.save()

        return client


class ClientLoginSerializer(serializers.Serializer):
    """Serializer for client login (supports email or phone)"""
    identifier = serializers.CharField(
        required=True,
        help_text="Email or phone number"
    )
    password = serializers.CharField(write_only=True, required=True)

    def validate(self, data):
        """Authenticate the client"""
        from .models import EcommerceClient
        from django.db.models import Q

        identifier = data.get('identifier')
        password = data.get('password')

        # Try to find client by email or phone
        try:
            client = EcommerceClient.objects.get(
                Q(email=identifier) | Q(phone_number=identifier)
            )
        except EcommerceClient.DoesNotExist:
            raise serializers.ValidationError("Invalid credentials.")

        # Check if client is active
        if not client.is_active:
            raise serializers.ValidationError("This account has been deactivated.")

        # Verify password
        if not client.check_password(password):
            raise serializers.ValidationError("Invalid credentials.")

        # Update last login
        client.update_last_login()

        # Add client to validated data
        data['client'] = client
        return data

class CartItemSerializer(serializers.ModelSerializer):
    """Serializer for cart items with product details"""
    product = ProductListSerializer(read_only=True)
    variant = ProductVariantSerializer(read_only=True)
    subtotal = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = CartItem
        fields = ['id', 'cart', 'product', 'variant', 'quantity', 'price_at_add', 'subtotal', 'created_at', 'updated_at']
        read_only_fields = ['id', 'price_at_add', 'created_at', 'updated_at']


class CartListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for cart lists - excludes nested items for faster list views"""
    client_name = serializers.CharField(source='client.full_name', read_only=True)
    items_count = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        fields = ['id', 'client', 'client_name', 'status', 'items_count', 'updated_at']
        read_only_fields = ['id', 'updated_at']

    def get_items_count(self, obj):
        return getattr(obj, '_items_count', obj.items.count())


class CartSerializer(serializers.ModelSerializer):
    """Serializer for shopping cart with nested items"""
    items = CartItemSerializer(many=True, read_only=True)
    delivery_address = ClientAddressSerializer(read_only=True)
    total_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    total_items = serializers.IntegerField(read_only=True)

    class Meta:
        model = Cart
        fields = ['id', 'client', 'delivery_address', 'status', 'notes', 'items', 'total_amount', 'total_items', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class CartItemCreateSerializer(serializers.ModelSerializer):
    """Serializer for adding items to cart"""
    
    class Meta:
        model = CartItem
        fields = ['id', 'cart', 'product', 'variant', 'quantity', 'price_at_add']
        read_only_fields = ['id', 'price_at_add']


class OrderItemSerializer(serializers.ModelSerializer):
    """Serializer for order items"""
    subtotal = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    product_image = serializers.CharField(source='product.image', read_only=True)

    class Meta:
        model = OrderItem
        fields = ['id', 'order', 'product', 'variant', 'product_name', 'product_image', 'quantity', 'price', 'subtotal', 'created_at']
        read_only_fields = ['id', 'created_at']


class OrderListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for order lists - excludes nested items for faster list views"""
    client_name = serializers.CharField(source='client.full_name', read_only=True)
    client_email = serializers.CharField(source='client.email', read_only=True)
    total_items = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'client', 'client_name', 'client_email',
            'total_items', 'status', 'payment_status', 'total_amount',
            'tracking_number', 'shipping_cost', 'tax_amount', 'subtotal',
            'discount_amount', 'created_at'
        ]
        read_only_fields = ['id', 'order_number', 'created_at']

    def get_total_items(self, obj):
        # Reads the annotation from OrderViewSet.get_queryset (list action);
        # falls back to a COUNT for other callers.
        count = getattr(obj, '_items_count', None)
        return count if count is not None else obj.items.count()


class ShippingMethodSerializer(serializers.ModelSerializer):
    """Serializer for shipping methods"""
    class Meta:
        model = ShippingMethod
        fields = [
            'id', 'name', 'description', 'price', 'free_shipping_threshold',
            'is_active', 'estimated_days', 'position', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class PromoCodeSerializer(serializers.ModelSerializer):
    """Serializer for promo codes (admin)"""
    class Meta:
        model = PromoCode
        fields = [
            'id', 'code', 'discount_type', 'discount_value',
            'min_order_amount', 'max_uses', 'times_used',
            'valid_from', 'valid_until', 'is_active', 'created_at'
        ]
        read_only_fields = ['id', 'times_used', 'created_at']


class ProductReviewSerializer(serializers.ModelSerializer):
    """Serializer for product reviews"""
    client_name = serializers.SerializerMethodField()

    class Meta:
        model = ProductReview
        fields = [
            'id', 'product', 'client', 'client_name', 'rating', 'title',
            'content', 'is_verified_purchase', 'is_approved', 'created_at'
        ]
        read_only_fields = [
            'id', 'client', 'is_verified_purchase', 'is_approved', 'created_at'
        ]

    def get_client_name(self, obj):
        return obj.client.first_name or 'Anonymous'


class ProductReviewCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating product reviews (client-facing)"""
    class Meta:
        model = ProductReview
        fields = ['id', 'rating', 'title', 'content', 'created_at']
        read_only_fields = ['id', 'created_at']


class OrderSerializer(serializers.ModelSerializer):
    """Serializer for orders with nested items"""
    items = OrderItemSerializer(many=True, read_only=True)
    delivery_address = ClientAddressSerializer(read_only=True)
    total_items = serializers.IntegerField(read_only=True)
    client_details = serializers.SerializerMethodField()
    shipping_method_details = ShippingMethodSerializer(source='shipping_method', read_only=True)

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'public_token',
            'client', 'client_details', 'delivery_address',
            'status', 'total_amount', 'notes', 'admin_notes', 'items', 'total_items',
            # Shipping fields
            'tracking_number', 'courier_provider', 'shipping_cost',
            'estimated_delivery_date', 'shipping_method', 'shipping_method_details',
            # Tax and pricing
            'tax_amount', 'subtotal', 'discount_amount', 'promo_code',
            # Payment fields
            'payment_status', 'payment_method', 'bog_order_id', 'payment_url',
            'payment_metadata',
            # Timestamps
            'created_at', 'updated_at', 'paid_at', 'confirmed_at', 'processing_at',
            'shipped_at', 'delivered_at', 'cancelled_at'
        ]
        read_only_fields = [
            'id', 'order_number', 'public_token', 'created_at', 'updated_at',
            'paid_at', 'bog_order_id', 'payment_url', 'payment_metadata'
        ]

    def get_client_details(self, obj):
        """Return client basic info"""
        return {
            'id': obj.client.id,
            'full_name': obj.client.full_name,
            'email': obj.client.email,
            'phone_number': obj.client.phone_number
        }


class EcommerceSettingsSerializer(serializers.ModelSerializer):
    """Serializer for ecommerce settings"""
    # BOG secret (write-only)
    bog_client_secret = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="BOG client secret (write-only, will be encrypted)"
    )
    has_bog_credentials = serializers.BooleanField(read_only=True)

    # TBC secret (write-only)
    tbc_client_secret = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="TBC Bank client secret (write-only, will be encrypted)"
    )
    has_tbc_credentials = serializers.BooleanField(read_only=True)

    # Flitt password (write-only)
    flitt_password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="Flitt merchant password (write-only, will be encrypted)"
    )
    has_flitt_credentials = serializers.BooleanField(read_only=True)

    # Paddle API key (write-only)
    paddle_api_key = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="Paddle API Key (write-only, will be encrypted)"
    )
    has_paddle_credentials = serializers.BooleanField(read_only=True)

    # Quickshipper API key (write-only)
    quickshipper_api_key = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="Quickshipper API key (write-only, will be encrypted). Leave blank to keep the existing one."
    )
    has_quickshipper_credentials = serializers.BooleanField(read_only=True)

    class Meta:
        model = EcommerceSettings
        fields = [
            'id', 'tenant',
            'ecommerce_payment_provider', 'active_payment_providers',
            # BOG fields
            'bog_client_id', 'bog_client_secret', 'has_bog_credentials',
            'bog_return_url_success', 'bog_return_url_fail',
            # Google Ads / GA4 tracking
            'google_ads_conversion_id', 'google_ads_purchase_label',
            'google_analytics_id', 'clarity_project_id',
            # Google Maps + Places autocomplete
            'google_maps_api_key',
            # Pickup option
            'allow_pickup',
            # TBC fields
            'tbc_client_id', 'tbc_client_secret', 'tbc_api_key', 'tbc_use_production',
            'has_tbc_credentials',
            # Flitt fields
            'flitt_merchant_id', 'flitt_password', 'has_flitt_credentials',
            # Paddle fields
            'paddle_api_key', 'paddle_webhook_secret', 'paddle_client_token',
            'paddle_use_production', 'has_paddle_credentials',
            # Quickshipper courier fields
            'quickshipper_enabled', 'quickshipper_api_key', 'has_quickshipper_credentials',
            'quickshipper_use_production', 'quickshipper_webhook_secret',
            'quickshipper_pickup_contact_name', 'quickshipper_pickup_phone',
            'quickshipper_pickup_address', 'quickshipper_pickup_city',
            'quickshipper_pickup_latitude', 'quickshipper_pickup_longitude',
            'quickshipper_pickup_extra_instructions',
            # Payment settings
            'enable_cash_on_delivery', 'enable_card_payment',
            'store_name', 'store_email', 'store_phone',
            'ecommerce_frontend_url', 'deployment_status', 'vercel_project_id', 'custom_domain',
            # Theme configuration fields
            'theme_preset', 'theme_primary_color', 'theme_secondary_color', 'theme_accent_color',
            'theme_background_color', 'theme_foreground_color', 'theme_muted_color',
            'theme_muted_foreground_color', 'theme_destructive_color', 'theme_border_color',
            'theme_border_radius', 'theme_card_color', 'theme_card_foreground_color',
            # Tax configuration
            'tax_rate', 'tax_inclusive', 'tax_label',
            # Homepage variant
            'homepage_variant',
            # Storefront visual template + Voltage tweaks
            'storefront_template',
            'voltage_theme_preset', 'voltage_color_mode',
            'voltage_density', 'voltage_radius', 'voltage_font_pair',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'tenant', 'ecommerce_frontend_url', 'deployment_status',
            'vercel_project_id', 'custom_domain',
            'has_bog_credentials', 'has_tbc_credentials', 'has_flitt_credentials', 'has_paddle_credentials',
            'has_quickshipper_credentials', 'quickshipper_webhook_secret',
            'created_at', 'updated_at',
        ]

    def create(self, validated_data):
        # Handle encrypted secrets
        bog_secret = validated_data.pop('bog_client_secret', None)
        tbc_secret = validated_data.pop('tbc_client_secret', None)
        flitt_password = validated_data.pop('flitt_password', None)
        paddle_api_key = validated_data.pop('paddle_api_key', None)
        quickshipper_api_key = validated_data.pop('quickshipper_api_key', None)

        instance = super().create(validated_data)

        needs_save = False
        if bog_secret:
            instance.set_bog_secret(bog_secret)
            needs_save = True
        if tbc_secret:
            instance.set_tbc_secret(tbc_secret)
            needs_save = True
        if flitt_password:
            instance.set_flitt_password(flitt_password)
            needs_save = True
        if paddle_api_key:
            instance.set_paddle_api_key(paddle_api_key)
            needs_save = True
        if quickshipper_api_key:
            instance.set_quickshipper_api_key(quickshipper_api_key)
            needs_save = True
        if needs_save:
            instance.save()

        # Allocate the webhook secret on first create so the admin UI sees a
        # value (the storefront's webhook handler validates against this).
        if instance.quickshipper_enabled and not instance.quickshipper_webhook_secret:
            instance.ensure_quickshipper_webhook_secret()

        return instance

    def update(self, instance, validated_data):
        # Handle encrypted secrets
        bog_secret = validated_data.pop('bog_client_secret', None)
        tbc_secret = validated_data.pop('tbc_client_secret', None)
        flitt_password = validated_data.pop('flitt_password', None)
        paddle_api_key = validated_data.pop('paddle_api_key', None)
        quickshipper_api_key = validated_data.pop('quickshipper_api_key', None)

        instance = super().update(instance, validated_data)

        needs_save = False
        if bog_secret:
            instance.set_bog_secret(bog_secret)
            needs_save = True
        if tbc_secret:
            instance.set_tbc_secret(tbc_secret)
            needs_save = True
        if flitt_password:
            instance.set_flitt_password(flitt_password)
            needs_save = True
        if paddle_api_key:
            instance.set_paddle_api_key(paddle_api_key)
            needs_save = True
        if quickshipper_api_key:
            instance.set_quickshipper_api_key(quickshipper_api_key)
            needs_save = True
        if needs_save:
            instance.save()

        # Allocate the webhook secret the first time Quickshipper is turned on.
        if instance.quickshipper_enabled and not instance.quickshipper_webhook_secret:
            instance.ensure_quickshipper_webhook_secret()

        return instance

    def validate(self, attrs):
        """Validate that return URLs are provided when card payment is enabled"""
        # Get enable_card_payment value from attrs or existing instance
        enable_card_payment = attrs.get('enable_card_payment')
        if enable_card_payment is None and self.instance:
            enable_card_payment = self.instance.enable_card_payment

        # Only validate if card payment is being enabled
        if enable_card_payment:
            # Get return URL values from attrs or existing instance
            bog_return_url_success = attrs.get('bog_return_url_success')
            if bog_return_url_success is None and self.instance:
                bog_return_url_success = self.instance.bog_return_url_success

            bog_return_url_fail = attrs.get('bog_return_url_fail')
            if bog_return_url_fail is None and self.instance:
                bog_return_url_fail = self.instance.bog_return_url_fail

            # Validate URLs are provided
            if not bog_return_url_success:
                raise serializers.ValidationError({
                    'bog_return_url_success': 'Success return URL is required when card payment is enabled'
                })
            if not bog_return_url_fail:
                raise serializers.ValidationError({
                    'bog_return_url_fail': 'Failure return URL is required when card payment is enabled'
                })

        return attrs


class OrderCreateSerializer(serializers.Serializer):
    """Serializer for creating an order from cart"""
    cart_id = serializers.IntegerField(required=True)
    delivery_address_id = serializers.IntegerField(required=True)
    card_id = serializers.IntegerField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    promo_code = serializers.CharField(required=False, allow_blank=True)
    shipping_method_id = serializers.IntegerField(required=False, allow_null=True)

    # Quickshipper-selected courier (when the storefront is in live-quote
    # mode). All four are needed by `book_quickshipper_courier` to book
    # exactly the option the customer paid for instead of re-quoting and
    # silently picking the cheapest. Stored on `Order.payment_metadata
    # .quickshipper_quote` and used as the shipping cost line.
    quickshipper_provider_id = serializers.IntegerField(required=False, allow_null=True)
    quickshipper_provider_fee_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    quickshipper_parcel_dimensions_id = serializers.IntegerField(required=False, allow_null=True)
    quickshipper_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True,
    )
    quickshipper_provider_name = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    # Pickup vs courier. Defaults to 'courier' for backward compatibility.
    # When 'pickup', the courier-booking task is skipped, shipping_cost is
    # forced to 0, and the cash-on-delivery payment method becomes
    # available (it's blocked for couriers — couriers don't collect cash).
    delivery_method = serializers.ChoiceField(
        choices=[('courier', 'Courier'), ('pickup', 'Pickup at store')],
        required=False,
        default='courier',
    )

    def validate_cart_id(self, value):
        """Validate cart exists and has items"""
        try:
            cart = Cart.objects.get(id=value, status='active')
            if not cart.items.exists():
                raise serializers.ValidationError("Cart is empty")
            return value
        except Cart.DoesNotExist:
            raise serializers.ValidationError("Cart not found")

    def validate_delivery_address_id(self, value):
        """Validate delivery address exists"""
        try:
            ClientAddress.objects.get(id=value)
            return value
        except ClientAddress.DoesNotExist:
            raise serializers.ValidationError("Delivery address not found")

    def validate_card_id(self, value):
        """Validate card exists and belongs to client (if provided)"""
        if value is None:
            return value
        try:
            card = ClientCard.objects.get(id=value, is_active=True)
            return value
        except ClientCard.DoesNotExist:
            raise serializers.ValidationError("Card not found or inactive")

    def validate_shipping_method_id(self, value):
        """Validate shipping method exists and is active"""
        if value is None:
            return value
        try:
            ShippingMethod.objects.get(id=value, is_active=True)
            return value
        except ShippingMethod.DoesNotExist:
            raise serializers.ValidationError("Shipping method not found or inactive")

    def create(self, validated_data):
        """Create order from cart with stock management, tax, shipping, and promo code"""
        from django.db import transaction
        from decimal import Decimal

        cart = Cart.objects.prefetch_related(
            'items__product', 'items__variant'
        ).get(id=validated_data['cart_id'])
        delivery_address = ClientAddress.objects.get(id=validated_data['delivery_address_id'])

        # Calculate subtotal from cart
        subtotal = Decimal('0')
        for cart_item in cart.items.all():
            subtotal += cart_item.price_at_add * cart_item.quantity

        # Handle promo code
        promo = None
        discount_amount = Decimal('0')
        promo_code_str = validated_data.get('promo_code', '').strip()
        if promo_code_str:
            try:
                promo = PromoCode.objects.get(code__iexact=promo_code_str)
                is_valid, message = promo.is_valid(subtotal=subtotal)
                if is_valid:
                    discount_amount = promo.calculate_discount(subtotal)
                # Silently ignore invalid promo codes during order creation
            except PromoCode.DoesNotExist:
                pass

        # Handle shipping method
        shipping_method = None
        shipping_cost = Decimal('0')
        shipping_method_id = validated_data.get('shipping_method_id')
        if shipping_method_id:
            try:
                shipping_method = ShippingMethod.objects.get(id=shipping_method_id, is_active=True)
                shipping_cost = shipping_method.get_effective_price(subtotal)
            except ShippingMethod.DoesNotExist:
                pass

        # Pickup orders short-circuit the shipping pipeline: free, no
        # courier booking, and the delivery_method is stamped so the
        # task layer can confirm.
        delivery_method = validated_data.get('delivery_method') or 'courier'
        if delivery_method == 'pickup':
            shipping_cost = Decimal('0')

        # Quickshipper-selected courier: when the storefront passed a quote,
        # use its price for shipping_cost and remember the choice on the
        # order so the booking task books exactly that option.
        quickshipper_quote_meta = None
        qs_price = validated_data.get('quickshipper_price')
        qs_provider_id = validated_data.get('quickshipper_provider_id')
        qs_provider_fee_id = validated_data.get('quickshipper_provider_fee_id')
        qs_parcel_dimensions_id = validated_data.get('quickshipper_parcel_dimensions_id')
        qs_provider_name = validated_data.get('quickshipper_provider_name')
        if delivery_method != 'pickup' and qs_price is not None and qs_provider_id is not None:
            shipping_cost = Decimal(str(qs_price))
            quickshipper_quote_meta = {
                'provider_id': qs_provider_id,
                'provider_fee_id': qs_provider_fee_id,
                'provider_name': qs_provider_name,
                'parcel_dimensions_id': qs_parcel_dimensions_id,
                'price': float(qs_price),
            }

        # Calculate tax
        tax_amount = Decimal('0')
        try:
            from .models import EcommerceSettings
            ecommerce_settings = EcommerceSettings.objects.first()
            if ecommerce_settings and ecommerce_settings.tax_rate > 0:
                taxable_amount = subtotal - discount_amount
                if ecommerce_settings.tax_inclusive:
                    # Prices already include tax, extract it
                    tax_amount = taxable_amount - (taxable_amount / (1 + ecommerce_settings.tax_rate / Decimal('100')))
                else:
                    # Tax is added on top
                    tax_amount = taxable_amount * ecommerce_settings.tax_rate / Decimal('100')
                tax_amount = tax_amount.quantize(Decimal('0.01'))
        except Exception:
            pass

        # Calculate total
        if tax_amount > 0 and ecommerce_settings and not ecommerce_settings.tax_inclusive:
            total_amount = subtotal - discount_amount + shipping_cost + tax_amount
        else:
            total_amount = subtotal - discount_amount + shipping_cost

        # Generate unique order number
        order_number = Order.generate_order_number()

        with transaction.atomic():
            # Stock validation and decrement
            for cart_item in cart.items.select_for_update().select_related('product'):
                product = cart_item.product
                if product.track_inventory:
                    if product.quantity < cart_item.quantity:
                        raise serializers.ValidationError(
                            f'Insufficient stock for {product.get_name("en")}. '
                            f'Available: {product.quantity}, Requested: {cart_item.quantity}'
                        )
                    product.quantity -= cart_item.quantity
                    product.save(update_fields=['quantity'])

            # Create order
            order = Order.objects.create(
                order_number=order_number,
                client=cart.client,
                delivery_address=delivery_address,
                subtotal=subtotal,
                discount_amount=discount_amount,
                promo_code=promo,
                shipping_method=shipping_method,
                shipping_cost=shipping_cost,
                tax_amount=tax_amount,
                total_amount=total_amount,
                notes=validated_data.get('notes', ''),
                status='pending',
                # Persist the Quickshipper quote (if present) so
                # `book_quickshipper_courier` books the exact option the
                # customer chose at checkout, not a re-quoted cheapest.
                # Also stamp delivery_method so the booking task can
                # bail on pickup orders.
                payment_metadata=({
                    **({'quickshipper_quote': quickshipper_quote_meta}
                       if quickshipper_quote_meta else {}),
                    'delivery_method': delivery_method,
                }),
            )

            # Create order items from cart items
            for cart_item in cart.items.all():
                OrderItem.objects.create(
                    order=order,
                    product=cart_item.product,
                    variant=cart_item.variant,
                    product_name=cart_item.product.name,
                    quantity=cart_item.quantity,
                    price=cart_item.price_at_add
                )

            # Increment promo code usage atomically, re-checking the cap under a
            # row lock so concurrent checkouts can't push a limited promo past
            # max_uses (the earlier validation is outside this transaction).
            if promo and discount_amount > 0:
                from django.db.models import F
                locked_promo = PromoCode.objects.select_for_update().get(pk=promo.pk)
                if locked_promo.max_uses is not None and locked_promo.times_used >= locked_promo.max_uses:
                    raise serializers.ValidationError('This promo code has reached its usage limit.')
                locked_promo.times_used = F('times_used') + 1
                locked_promo.save(update_fields=['times_used'])

            # Mark cart as converted
            cart.status = 'converted'
            cart.save()

        return order


class PasswordResetRequestSerializer(serializers.Serializer):
    """Serializer for requesting password reset"""
    email = serializers.EmailField(required=True)

    def validate_email(self, value):
        """Validate that email exists"""
        from .models import EcommerceClient
        try:
            client = EcommerceClient.objects.get(email=value, is_active=True)
            self.context['client'] = client
            return value
        except EcommerceClient.DoesNotExist:
            raise serializers.ValidationError("No active account found with this email address.")


class PasswordResetConfirmSerializer(serializers.Serializer):
    """Serializer for confirming password reset"""
    email = serializers.EmailField(required=True)
    code = serializers.CharField(required=True, max_length=6, min_length=6)
    new_password = serializers.CharField(write_only=True, required=True, min_length=8)
    new_password_confirm = serializers.CharField(write_only=True, required=True)

    def validate(self, data):
        """Validate code and passwords match"""
        from .models import PasswordResetToken, EcommerceClient

        # Validate passwords match
        if data.get('new_password') != data.get('new_password_confirm'):
            raise serializers.ValidationError({"new_password_confirm": "Passwords do not match."})

        # Validate client exists
        try:
            client = EcommerceClient.objects.get(email=data['email'], is_active=True)
        except EcommerceClient.DoesNotExist:
            raise serializers.ValidationError({"email": "No active account found with this email address."})

        # Validate code
        try:
            reset_token = PasswordResetToken.objects.get(
                client=client,
                token=data['code'],
                is_used=False
            )
        except PasswordResetToken.DoesNotExist:
            raise serializers.ValidationError({"code": "Invalid or expired verification code."})

        if not reset_token.is_valid():
            raise serializers.ValidationError({"code": "This verification code has expired or been used."})

        data['reset_token'] = reset_token
        return data


class ClientCardSerializer(serializers.ModelSerializer):
    """Serializer for client saved payment cards"""

    class Meta:
        model = ClientCard
        fields = [
            'id',
            'card_type',
            'masked_card_number',
            'card_expiry',
            'is_default',
            'is_active',
            'created_at'
        ]
        read_only_fields = [
            'id',
            'card_type',
            'masked_card_number',
            'card_expiry',
            'created_at'
        ]


class ListItemSerializer(serializers.ModelSerializer):
    """Serializer for items within an ItemList"""
    children = serializers.SerializerMethodField()

    class Meta:
        model = ListItem
        fields = [
            'id',
            'label',
            'custom_id',
            'position',
            'is_active',
            'custom_data',
            'children',
        ]
        read_only_fields = ['id']

    def get_children(self, obj):
        """Recursively serialize child items"""
        # Use prefetched children if available (avoids N+1 query)
        if hasattr(obj, '_prefetched_objects_cache') and 'children' in obj._prefetched_objects_cache:
            children = [c for c in obj.children.all() if c.is_active]
        else:
            # Fallback for non-prefetched queries
            if not obj.children.exists():
                return []
            children = obj.children.filter(is_active=True)
        return ListItemSerializer(children, many=True).data if children else []


class ItemListMinimalSerializer(serializers.ModelSerializer):
    """Minimal serializer for public item lists"""
    items_count = serializers.SerializerMethodField()

    class Meta:
        model = ItemList
        fields = [
            'id',
            'title',
            'description',
            'is_public',
            'is_active',
            'items_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_items_count(self, obj):
        """Return count of items in this list"""
        return obj.items.filter(is_active=True).count()


class ItemListDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for item lists with all items"""
    items = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()

    class Meta:
        model = ItemList
        fields = [
            'id',
            'title',
            'description',
            'is_public',
            'is_active',
            'custom_fields_schema',
            'items',
            'items_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_items(self, obj):
        """Return only root-level items (items without parent)"""
        root_items = obj.items.filter(parent__isnull=True, is_active=True).order_by('position')
        return ListItemSerializer(root_items, many=True).data

    def get_items_count(self, obj):
        """Return count of items in this list"""
        return obj.items.filter(is_active=True).count()


class HomepageSectionSerializer(serializers.ModelSerializer):
    """Serializer for homepage section configuration (admin)"""
    item_list_title = serializers.CharField(source='item_list.title', read_only=True, allow_null=True)
    section_type_display = serializers.CharField(source='get_section_type_display', read_only=True)
    display_mode_display = serializers.CharField(source='get_display_mode_display', read_only=True)

    class Meta:
        model = HomepageSection
        fields = [
            'id',
            'title',
            'subtitle',
            'section_type',
            'section_type_display',
            'position',
            'is_active',
            'item_list',
            'item_list_title',
            'attribute_key',
            'attribute_value',
            'display_mode',
            'display_mode_display',
            'settings',
            'background_color',
            'background_image_url',
            'text_color',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'section_type_display', 'display_mode_display', 'item_list_title']


class HomepageSectionPublicSerializer(serializers.ModelSerializer):
    """Serializer for public homepage API - includes resolved data"""
    data = serializers.SerializerMethodField()

    class Meta:
        model = HomepageSection
        fields = [
            'id',
            'title',
            'subtitle',
            'section_type',
            'position',
            'display_mode',
            'settings',
            'background_color',
            'background_image_url',
            'text_color',
            'attribute_key',
            'attribute_value',
            'data',
        ]

    def get_data(self, obj):
        """Resolve data from ItemList if available"""
        if obj.item_list:
            # Use prefetched items if available (avoids N+1 query)
            if hasattr(obj.item_list, 'prefetched_root_items'):
                root_items = obj.item_list.prefetched_root_items
            else:
                # Fallback for non-prefetched queries
                root_items = obj.item_list.items.filter(parent__isnull=True, is_active=True).order_by('position')
            return ListItemSerializer(root_items, many=True).data
        return []


class HomepageSectionReorderSerializer(serializers.Serializer):
    """Serializer for reordering homepage sections"""
    section_ids = serializers.ListField(
        child=serializers.IntegerField(),
        help_text='List of section IDs in desired order'
    )
