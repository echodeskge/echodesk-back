from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.auth.hashers import make_password, check_password
from decimal import Decimal
import uuid
import base64


class Language(models.Model):
    """
    Available languages for multilanguage product content
    Default languages: English (en) and Georgian (ka)
    Users can add additional languages as needed
    """
    code = models.CharField(
        max_length=10,
        unique=True,
        help_text="Language code (e.g., 'en', 'ka', 'ru', 'de')"
    )
    name = models.JSONField(
        help_text="Language name in different languages: {'en': 'English', 'ka': 'ინგლისური'}"
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Whether this is a default required language"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this language is active and available for use"
    )
    sort_order = models.IntegerField(default=0, help_text="Display order")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'code']
        verbose_name = 'Language'
        verbose_name_plural = 'Languages'
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['is_active', 'sort_order']),
        ]

    def __str__(self):
        if isinstance(self.name, dict):
            return self.name.get('en', self.name.get(list(self.name.keys())[0], self.code))
        return str(self.name)

    def get_name(self, language='en'):
        """Get language name in specific language"""
        if isinstance(self.name, dict):
            return self.name.get(language, self.name.get('en', self.code))
        return str(self.name)


class AttributeDefinition(models.Model):
    """
    Define dynamic attributes that can be assigned to products
    (e.g., Color, Size, Material, etc.)
    """
    ATTRIBUTE_TYPES = [
        ('multiselect', 'Multi Select'),
        ('number', 'Number'),
    ]

    name = models.JSONField(
        help_text="Attribute name in different languages: {'en': 'Color', 'ka': 'ფერი', 'ru': 'Цвет'}"
    )
    key = models.CharField(
        max_length=100,
        unique=True,
        help_text="Unique key for code reference (e.g., 'color', 'size')"
    )
    attribute_type = models.CharField(max_length=20, choices=ATTRIBUTE_TYPES, default='multiselect')
    options = models.JSONField(
        blank=True,
        default=list,
        help_text="For select/multiselect types: [{'en': 'Red', 'ka': 'წითელი', 'ru': 'Красный', 'value': 'red'}, ...]"
    )
    unit = models.CharField(
        max_length=50,
        blank=True,
        help_text="Unit of measurement (e.g., 'cm', 'kg', 'ml')"
    )
    is_required = models.BooleanField(default=False)
    is_filterable = models.BooleanField(
        default=True,
        help_text="Whether this attribute can be used for filtering products"
    )
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'id']
        verbose_name = 'Attribute Definition'
        verbose_name_plural = 'Attribute Definitions'
        indexes = [
            models.Index(fields=['key']),
            models.Index(fields=['is_active', 'sort_order']),
        ]

    def __str__(self):
        if isinstance(self.name, dict):
            return self.name.get('en', self.name.get(list(self.name.keys())[0], self.key))
        return str(self.name)

    def get_name(self, language='en'):
        """Get attribute name in specific language"""
        if isinstance(self.name, dict):
            return self.name.get(language, self.name.get('en', self.key))
        return str(self.name)


class Product(models.Model):
    """Main product model"""

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('out_of_stock', 'Out of Stock'),
    ]

    # Basic Information
    sku = models.CharField(max_length=100, unique=True, help_text="Stock Keeping Unit")
    name = models.JSONField(
        help_text="Product name in different languages: {'en': 'Laptop', 'ka': 'ლეპტოპი', 'ru': 'Ноутбук'}"
    )
    description = models.JSONField(
        blank=True,
        default=dict,
        help_text="Product description in different languages"
    )
    short_description = models.JSONField(
        blank=True,
        default=dict,
        help_text="Short product description in different languages"
    )

    # Using dynamic attributes instead of fixed product types and categories

    # Pricing
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Base price in GEL"
    )
    compare_at_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Original price for showing discounts"
    )
    cost_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Cost price for profit calculation"
    )

    # Media
    image = models.URLField(
        max_length=2000,
        blank=True,
        null=True,
        help_text="Main product image URL"
    )

    # Inventory
    track_inventory = models.BooleanField(default=True)
    quantity = models.IntegerField(default=0, help_text="Stock quantity")
    low_stock_threshold = models.IntegerField(
        default=10,
        help_text="Alert when stock falls below this level"
    )

    # Status and metadata
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', db_index=True)
    is_featured = models.BooleanField(default=False, db_index=True)
    weight = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Weight in kg"
    )
    dimensions = models.JSONField(
        blank=True,
        default=dict,
        help_text="Dimensions: {'length': 10, 'width': 5, 'height': 3, 'unit': 'cm'}"
    )

    # SEO
    meta_title = models.JSONField(
        blank=True,
        default=dict,
        help_text="SEO title in different languages"
    )
    meta_description = models.JSONField(
        blank=True,
        default=dict,
        help_text="SEO description in different languages"
    )
    slug = models.SlugField(max_length=200, unique=True)

    # Timestamps and user tracking
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_products'
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_products'
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Product'
        verbose_name_plural = 'Products'
        indexes = [
            models.Index(fields=['sku']),
            models.Index(fields=['slug']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['is_featured', 'status']),
            models.Index(fields=['quantity'], name='ecommerce_c_quantit_idx'),
        ]

    def __str__(self):
        if isinstance(self.name, dict):
            name = self.name.get('en', self.name.get(list(self.name.keys())[0], 'Unnamed'))
        else:
            name = str(self.name)
        return f"{self.sku} - {name}"

    def get_name(self, language='en'):
        """Get product name in specific language"""
        if isinstance(self.name, dict):
            return self.name.get(language, self.name.get('en', ''))
        return str(self.name)

    def get_description(self, language='en'):
        """Get product description in specific language"""
        if isinstance(self.description, dict):
            return self.description.get(language, self.description.get('en', ''))
        return str(self.description)

    @property
    def is_low_stock(self):
        """Check if product is low on stock"""
        if not self.track_inventory:
            return False
        return self.quantity <= self.low_stock_threshold

    @property
    def is_in_stock(self):
        """Check if product is in stock"""
        if not self.track_inventory:
            return True
        return self.quantity > 0

    @property
    def discount_percentage(self):
        """Calculate discount percentage if compare_at_price is set"""
        if self.compare_at_price and self.compare_at_price > self.price:
            return round(((self.compare_at_price - self.price) / self.compare_at_price) * 100, 2)
        return 0

    def save(self, *args, **kwargs):
        """Auto-generate slug from SKU if not provided"""
        if not self.slug:
            from django.utils.text import slugify
            import uuid
            # Generate slug from SKU (always ASCII-safe)
            base_slug = slugify(self.sku)
            if not base_slug:
                # Fallback to uuid-based slug
                base_slug = f"product-{uuid.uuid4().hex[:8]}"

            # Ensure uniqueness
            slug = base_slug
            counter = 1
            while Product.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)


class ProductImage(models.Model):
    """Additional product images"""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    image = models.URLField(max_length=2000, help_text="Product image URL")
    alt_text = models.JSONField(
        blank=True,
        default=dict,
        help_text="Alt text in different languages for SEO"
    )
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sort_order', 'id']
        verbose_name = 'Product Image'
        verbose_name_plural = 'Product Images'

    def __str__(self):
        return f"Image for {self.product.sku}"


class ProductAttributeValue(models.Model):
    """
    Store attribute values for products
    Allows dynamic attributes without schema changes
    """
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='attribute_values')
    attribute = models.ForeignKey(AttributeDefinition, on_delete=models.CASCADE, related_name='product_values')

    # Store value based on attribute type
    value_text = models.TextField(blank=True)
    value_number = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    value_boolean = models.BooleanField(null=True, blank=True)
    value_date = models.DateField(null=True, blank=True)
    value_json = models.JSONField(
        blank=True,
        default=dict,
        help_text="For complex values like multiselect or multilanguage text"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['product', 'attribute']
        verbose_name = 'Product Attribute Value'
        verbose_name_plural = 'Product Attribute Values'
        indexes = [
            models.Index(fields=['product', 'attribute']),
            models.Index(fields=['attribute', 'value_text'], name='ecommerce_c_attr_txt_idx'),
            models.Index(fields=['attribute', 'value_boolean'], name='ecommerce_c_attr_bool_idx'),
            models.Index(fields=['attribute', 'value_number'], name='ecommerce_c_attr_num_idx'),
            models.Index(fields=['value_text'], name='ecommerce_c_val_txt_idx'),
        ]

    def __str__(self):
        return f"{self.product.sku} - {self.attribute.key}"

    def get_value(self):
        """Get the appropriate value based on attribute type"""
        attribute_type = self.attribute.attribute_type

        if attribute_type == 'number':
            return self.value_number
        elif attribute_type in ['select', 'multiselect']:
            return self.value_json

        return None

    def set_value(self, value):
        """Set the appropriate value field based on attribute type"""
        attribute_type = self.attribute.attribute_type

        if attribute_type == 'number':
            self.value_number = Decimal(str(value))
        elif attribute_type in ['select', 'multiselect']:
            self.value_json = value


class ProductVariant(models.Model):
    """
    Product variants (e.g., different sizes or colors of the same product)
    """
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    sku = models.CharField(max_length=100, unique=True, help_text="Variant SKU")
    name = models.JSONField(
        help_text="Variant name in different languages: {'en': 'Blue - Large', 'ka': 'ლურჯი - დიდი'}"
    )

    # Pricing (can override parent product price)
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Variant price (if different from base product)"
    )

    # Inventory
    quantity = models.IntegerField(default=0)
    image = models.URLField(
        max_length=2000,
        blank=True,
        null=True,
        help_text="Product variant image URL"
    )

    # Status
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'id']
        verbose_name = 'Product Variant'
        verbose_name_plural = 'Product Variants'
        indexes = [
            models.Index(fields=['product', 'is_active']),
            models.Index(fields=['sku']),
        ]

    def __str__(self):
        if isinstance(self.name, dict):
            name = self.name.get('en', self.name.get(list(self.name.keys())[0], self.sku))
        else:
            name = str(self.name)
        return f"{self.product.sku} - {name}"

    @property
    def effective_price(self):
        """Get effective price (variant price or product price)"""
        return self.price if self.price else self.product.price


class ProductVariantAttributeValue(models.Model):
    """
    Store attribute values for product variants
    (e.g., Color=Blue, Size=Large for a specific variant)
    """
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name='attribute_values')
    attribute = models.ForeignKey(AttributeDefinition, on_delete=models.CASCADE)
    value_json = models.JSONField(help_text="Attribute value for this variant")

    class Meta:
        unique_together = ['variant', 'attribute']
        verbose_name = 'Variant Attribute Value'
        verbose_name_plural = 'Variant Attribute Values'

    def __str__(self):
        return f"{self.variant.sku} - {self.attribute.key}"


class EcommerceClient(models.Model):
    """
    Ecommerce client/customer model for customer registration and authentication
    Separate from the main User model - these are customers of the ecommerce store
    """
    # Personal Information
    first_name = models.CharField(max_length=150, help_text="Client's first name")
    last_name = models.CharField(max_length=150, help_text="Client's last name")
    email = models.EmailField(unique=True, help_text="Client's email address (used for login)")
    phone_number = models.CharField(
        max_length=20,
        unique=True,
        help_text="Client's phone number (used for login)"
    )

    # Authentication
    password = models.CharField(max_length=128, help_text="Hashed password")

    # Additional Information
    date_of_birth = models.DateField(null=True, blank=True, help_text="Client's date of birth")

    # Status and Timestamps
    is_active = models.BooleanField(
        default=True,
        help_text="Designates whether this client should be treated as active"
    )
    is_verified = models.BooleanField(
        default=False,
        help_text="Email/phone verification status"
    )
    last_login = models.DateTimeField(null=True, blank=True, help_text="Last login timestamp")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Ecommerce Client'
        verbose_name_plural = 'Ecommerce Clients'
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['phone_number']),
            models.Index(fields=['is_active', '-created_at']),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})"

    def set_password(self, raw_password):
        """Hash and set the password"""
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        """Verify a password against the stored hash"""
        return check_password(raw_password, self.password)

    @property
    def full_name(self):
        """Return the client's full name"""
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_authenticated(self):
        """
        Always return True for authenticated clients.
        This is required by DRF's IsAuthenticated permission class.
        """
        return True

    def update_last_login(self):
        """Update the last_login timestamp"""
        from django.utils import timezone
        self.last_login = timezone.now()
        self.save(update_fields=['last_login'])


class ClientVerificationCode(models.Model):
    """Model to store email verification codes for clients"""
    email = models.EmailField(help_text="Email address to verify")
    code = models.CharField(max_length=6, help_text="6-digit verification code")
    token = models.CharField(max_length=100, unique=True, help_text="Verification token")
    is_used = models.BooleanField(default=False, help_text="Whether code has been used")
    expires_at = models.DateTimeField(help_text="Expiration time for code")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Client Verification Code'
        verbose_name_plural = 'Client Verification Codes'
        indexes = [
            models.Index(fields=['email', 'token']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f"{self.email} - {self.code}"

    def is_valid(self):
        """Check if code is still valid (not used and not expired)"""
        from django.utils import timezone
        return not self.is_used and timezone.now() < self.expires_at


class ClientAddress(models.Model):
    """
    Delivery addresses for ecommerce clients
    Clients can have multiple addresses (home, work, etc.)
    """
    client = models.ForeignKey(
        EcommerceClient,
        on_delete=models.CASCADE,
        related_name='addresses',
        help_text="Client who owns this address"
    )
    label = models.CharField(
        max_length=50,
        help_text="Address label (e.g., 'Home', 'Work', 'Office')"
    )
    address = models.TextField(help_text="Full street address")
    city = models.CharField(max_length=100, help_text="City name")
    extra_instructions = models.TextField(
        blank=True,
        help_text="Special delivery instructions for courier"
    )

    # Geographic coordinates from Google Maps
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="Latitude coordinate from Google Maps"
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="Longitude coordinate from Google Maps"
    )

    postal_code = models.CharField(max_length=20, blank=True, default='')
    country = models.CharField(max_length=100, blank=True, default='')

    # Default address flag
    is_default = models.BooleanField(
        default=False,
        help_text="Mark this as the default delivery address"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', '-created_at']
        verbose_name = 'Client Address'
        verbose_name_plural = 'Client Addresses'
        indexes = [
            models.Index(fields=['client', '-is_default']),
            models.Index(fields=['client', '-created_at']),
        ]

    def __str__(self):
        return f"{self.client.full_name} - {self.label} ({self.city})"

    def save(self, *args, **kwargs):
        """Override save to ensure only one default address per client"""
        if self.is_default:
            # Set all other addresses for this client to non-default
            ClientAddress.objects.filter(
                client=self.client,
                is_default=True
            ).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class FavoriteProduct(models.Model):
    """
    Client's favorite/wishlist products
    Many-to-many relationship between clients and products
    """
    client = models.ForeignKey(
        EcommerceClient,
        on_delete=models.CASCADE,
        related_name='favorites',
        help_text="Client who favorited the product"
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='favorited_by',
        help_text="Product that was favorited"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['client', 'product']
        ordering = ['-created_at']
        verbose_name = 'Favorite Product'
        verbose_name_plural = 'Favorite Products'
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['product']),
        ]

    def __str__(self):
        return f"{self.client.full_name} - {self.product.sku}"


class Cart(models.Model):
    """
    Shopping cart for ecommerce clients
    Each client has one active cart at a time
    """
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('abandoned', 'Abandoned'),
        ('converted', 'Converted to Order'),
    ]

    client = models.ForeignKey(
        EcommerceClient,
        on_delete=models.CASCADE,
        related_name='carts',
        help_text="Client who owns this cart"
    )
    delivery_address = models.ForeignKey(
        ClientAddress,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='carts',
        help_text="Selected delivery address for this cart"
    )
    selected_card = models.ForeignKey(
        'ClientCard',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='carts',
        help_text="Selected payment card for this cart"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='active',
        help_text="Cart status"
    )
    notes = models.TextField(blank=True, help_text="Special instructions or notes")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Shopping Cart'
        verbose_name_plural = 'Shopping Carts'
        indexes = [
            models.Index(fields=['client', 'status']),
            models.Index(fields=['status', '-updated_at']),
        ]

    def __str__(self):
        return f"Cart #{self.id} - {self.client.full_name} ({self.status})"

    @property
    def total_amount(self):
        """Calculate total cart amount"""
        return sum(item.subtotal for item in self.items.all())

    @property
    def total_items(self):
        """Get total number of items in cart"""
        return sum(item.quantity for item in self.items.all())


class CartItem(models.Model):
    """
    Individual items in a shopping cart
    """
    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name='items',
        help_text="Cart this item belongs to"
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='cart_items',
        help_text="Product in cart"
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='cart_items',
        help_text="Product variant (if applicable)"
    )
    quantity = models.IntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Quantity of this product"
    )
    price_at_add = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Price when added to cart (to track price changes)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Cart Item'
        verbose_name_plural = 'Cart Items'
        indexes = [
            models.Index(fields=['cart', 'product']),
        ]

    def __str__(self):
        return f"{self.product.sku} x{self.quantity} in Cart #{self.cart.id}"

    @property
    def subtotal(self):
        """Calculate subtotal for this cart item"""
        return self.price_at_add * self.quantity

    def save(self, *args, **kwargs):
        """Auto-set price_at_add if not provided"""
        if not self.price_at_add:
            if self.variant and self.variant.price:
                self.price_at_add = self.variant.price
            else:
                self.price_at_add = self.product.price
        super().save(*args, **kwargs)


class Order(models.Model):
    """
    Customer orders created from cart checkout
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'),
        ('processing', 'Processing'),
        ('shipped', 'Shipped'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
        ('refunded', 'Refunded'),
    ]

    PAYMENT_STATUS_CHOICES = [
        ('pending', 'Pending Payment'),
        ('paid', 'Paid'),
        ('failed', 'Payment Failed'),
        ('refunded', 'Refunded'),
        ('partially_refunded', 'Partially Refunded'),
    ]

    order_number = models.CharField(
        max_length=50,
        unique=True,
        help_text="Unique order number"
    )
    client = models.ForeignKey(
        EcommerceClient,
        on_delete=models.PROTECT,
        related_name='orders',
        help_text="Client who placed the order"
    )
    delivery_address = models.ForeignKey(
        ClientAddress,
        on_delete=models.PROTECT,
        related_name='orders',
        help_text="Delivery address for this order"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        help_text="Order status"
    )
    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Total order amount"
    )

    # Payment fields
    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default='pending',
        help_text="Payment status"
    )
    payment_method = models.CharField(
        max_length=50,
        blank=True,
        default='card',
        help_text="Payment method (card, cash_on_delivery, etc.)"
    )
    bog_order_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Bank of Georgia order ID"
    )
    payment_url = models.URLField(
        blank=True,
        null=True,
        help_text="BOG payment page URL"
    )
    payment_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Payment gateway response data"
    )

    # Shipping fields
    tracking_number = models.CharField(max_length=100, blank=True, default='')
    courier_provider = models.CharField(max_length=50, blank=True, default='')
    shipping_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    estimated_delivery_date = models.DateField(null=True, blank=True)
    shipping_method = models.ForeignKey(
        'ShippingMethod', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='orders', help_text="Shipping method selected for this order"
    )

    # Tax and pricing breakdown
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    promo_code = models.ForeignKey(
        'PromoCode', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='orders', help_text="Promo code applied to this order"
    )

    notes = models.TextField(
        blank=True,
        help_text="Customer notes or special instructions"
    )
    admin_notes = models.TextField(
        blank=True,
        help_text="Internal admin notes"
    )
    # Unguessable token used by guest visitors to view their order
    # confirmation + status without authenticating. Generated on save
    # for every order; emailed to the customer in the confirmation
    # template (link looks like /order-confirmation?order_id=X&token=Y).
    public_token = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text="URL-safe token for public order lookup (guest tracking)."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True, help_text="When payment was completed")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    processing_at = models.DateTimeField(null=True, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Order'
        verbose_name_plural = 'Orders'
        indexes = [
            models.Index(fields=['order_number']),
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['payment_status', '-created_at']),
            models.Index(fields=['bog_order_id']),
        ]

    def __str__(self):
        return f"Order {self.order_number} - {self.client.full_name}"

    @property
    def total_items(self):
        """Get total number of items in order"""
        return sum(item.quantity for item in self.items.all())

    @staticmethod
    def generate_order_number():
        """Generate unique order number"""
        import random
        import string
        from django.utils import timezone

        timestamp = timezone.now().strftime('%Y%m%d')
        random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return f"ORD-{timestamp}-{random_str}"

    def save(self, *args, **kwargs):
        # Mint a public token on first save so guest order links work
        # without an extra DB write later. Existing rows are backfilled
        # via the data migration shipped with this field.
        if not self.public_token:
            import secrets as _secrets
            self.public_token = _secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    def cancel(self, reason='', restock=True):
        """Cancel this order, restoring inventory and releasing the promo use.

        Idempotent and concurrency-safe: takes a row lock and re-checks status
        so a double cancel (or a cancel racing the abandoned-order sweeper) can't
        restock twice. Stock is restored with an atomic ``F()`` update so it can't
        lose a concurrent checkout decrement. Returns True if it cancelled, False
        if the order was already cancelled.
        """
        from django.db import transaction
        from django.db.models import F
        from django.utils import timezone

        if self.status == 'cancelled':
            return False

        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=self.pk)
            if order.status == 'cancelled':
                return False

            if restock:
                for item in order.items.select_related('product'):
                    product = item.product
                    if product and product.track_inventory:
                        Product.objects.filter(pk=product.pk).update(
                            quantity=F('quantity') + item.quantity
                        )
                # Free the promo usage this order had consumed.
                if order.promo_code_id:
                    PromoCode.objects.filter(
                        pk=order.promo_code_id, times_used__gt=0
                    ).update(times_used=F('times_used') - 1)

            order.status = 'cancelled'
            order.cancelled_at = timezone.now()
            if reason:
                note = f"[cancelled] {reason}"
                order.admin_notes = f"{order.admin_notes}\n{note}".strip() if order.admin_notes else note
            order.save(update_fields=['status', 'cancelled_at', 'admin_notes', 'updated_at'])

            # Keep the in-memory instance consistent for the caller.
            self.status = order.status
            self.cancelled_at = order.cancelled_at
            self.admin_notes = order.admin_notes

        return True


class OrderItem(models.Model):
    """
    Individual items in an order
    Snapshot of product details at time of order
    """
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='items',
        help_text="Order this item belongs to"
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name='order_items',
        help_text="Product ordered"
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='order_items',
        help_text="Product variant (if applicable)"
    )
    product_name = models.JSONField(
        help_text="Product name at time of order (multilingual)"
    )
    quantity = models.IntegerField(
        validators=[MinValueValidator(1)],
        help_text="Quantity ordered"
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Price per item at time of order"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Order Item'
        verbose_name_plural = 'Order Items'
        indexes = [
            models.Index(fields=['order', 'product']),
        ]

    def __str__(self):
        return f"{self.product.sku} x{self.quantity} in {self.order.order_number}"

    @property
    def subtotal(self):
        """Calculate subtotal for this order item"""
        return self.price * self.quantity


class EcommerceSettings(models.Model):
    """
    Per-tenant ecommerce settings including encrypted payment gateway credentials
    Each tenant can configure their own BOG API credentials
    """
    # OneToOne with Tenant (from tenants app)
    tenant = models.OneToOneField(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        related_name='ecommerce_settings',
        help_text="Tenant these settings belong to"
    )

    # Payment provider for ecommerce orders
    PAYMENT_PROVIDER_CHOICES = [
        ('bog', 'Bank of Georgia'),
        ('tbc', 'TBC Bank'),
        ('flitt', 'Flitt'),
        ('paddle', 'Paddle'),
    ]
    ecommerce_payment_provider = models.CharField(
        max_length=20,
        choices=PAYMENT_PROVIDER_CHOICES,
        default='bog',
        help_text='Payment provider used for ecommerce orders'
    )

    # Active payment providers (JSON list)
    active_payment_providers = models.JSONField(
        default=list,
        blank=True,
        help_text='Active provider keys: ["bog","tbc","flitt","paddle","cash"]'
    )

    # BOG Payment Gateway Settings
    bog_client_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Bank of Georgia Client ID"
    )
    bog_client_secret_encrypted = models.BinaryField(
        blank=True,
        null=True,
        help_text="Encrypted Bank of Georgia Client Secret"
    )
    bog_return_url_success = models.URLField(
        max_length=500,
        blank=True,
        help_text="URL to redirect after successful payment (e.g., https://yourstore.com/payment/success)"
    )
    bog_return_url_fail = models.URLField(
        max_length=500,
        blank=True,
        help_text="URL to redirect after failed payment (e.g., https://yourstore.com/payment/fail)"
    )

    # Google Ads / GA4 conversion tracking. The storefront drops in
    # gtag.js only when a conversion_id is set, and fires `purchase`
    # events on order-confirmation only when both id + label are set.
    # Per-tenant: each tenant configures their own AW-xxx id from
    # admin → Settings → Ecommerce → Marketing.
    google_ads_conversion_id = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text="Google Ads tag ID (e.g. AW-18133924374). Loads gtag.js on every page when set."
    )
    google_ads_purchase_label = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text="Conversion label suffix from Google Ads (without the AW- prefix). Required to fire `purchase` events."
    )
    # Google Analytics 4 measurement ID (`G-XXXXXXXXXX`). Independent
    # of Google Ads — drives general visitor/session/page tracking,
    # not ad-conversion attribution. Both can run side by side; the
    # storefront calls `gtag('config', ...)` once per ID.
    google_analytics_id = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text="Google Analytics 4 Measurement ID (e.g. G-TS7YN24F1C). Loads gtag.js + sends visitor / page-view data when set."
    )
    # Microsoft Clarity — heatmaps + session recordings. Tenants paste
    # either the full snippet or just the project ID; the admin form
    # extracts the 10-char ID via regex before saving here.
    clarity_project_id = models.CharField(
        max_length=32,
        blank=True,
        default='',
        help_text="Microsoft Clarity project ID (e.g. p1q2r3s4t5). Loads the Clarity heatmap + session-replay tag when set."
    )
    # Google Maps JavaScript API key. When set, the storefront swaps
    # out the default Leaflet/OpenStreetMap address picker for the
    # Google Maps experience: Places Autocomplete on the address
    # input, auto-pin on selection, drag-to-refine. Per-tenant so
    # each merchant manages their own Google Cloud billing + domain
    # restrictions.
    google_maps_api_key = models.CharField(
        max_length=128,
        blank=True,
        default='',
        help_text="Google Maps JavaScript API key (with Places + Maps enabled). When set, the address pickers use Google Maps + Places Autocomplete instead of the default Leaflet/OpenStreetMap picker."
    )

    # Pickup option — tenant offers customers the option of picking the
    # order up at the shop instead of having it couriered. Reuses the
    # Quickshipper pickup address fields (quickshipper_pickup_address /
    # _city / _phone / _contact_name / _extra_instructions) as the
    # pickup-point details since they're the same physical place.
    # Business rule: cash on delivery is only available when the
    # customer chose pickup; courier orders must be paid by card so the
    # merchant doesn't have to chase cash through couriers.
    allow_pickup = models.BooleanField(
        default=False,
        help_text="Let customers pick up orders at the store instead of using a courier. Pickup address is read from the Quickshipper pickup fields."
    )

    # TBC Bank Payment Gateway Settings
    tbc_client_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="TBC Bank Client ID"
    )
    tbc_client_secret_encrypted = models.BinaryField(
        blank=True,
        null=True,
        help_text="Encrypted TBC Bank Client Secret"
    )
    tbc_api_key = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="TBC Bank API Key"
    )
    tbc_use_production = models.BooleanField(
        default=False,
        help_text="Use TBC production environment (unchecked = sandbox)"
    )

    # Paddle Payment Settings
    paddle_api_key_encrypted = models.BinaryField(
        blank=True,
        null=True,
        help_text="Encrypted Paddle API Key"
    )
    paddle_webhook_secret = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="Paddle Webhook Secret for signature verification"
    )
    paddle_client_token = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="Paddle Client Token for frontend Paddle.js"
    )
    paddle_use_production = models.BooleanField(
        default=False,
        help_text="Use Paddle production environment (unchecked = sandbox)"
    )

    # Flitt (formerly Fondy) Payment Gateway Settings
    flitt_merchant_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="Flitt Merchant ID"
    )
    flitt_password_encrypted = models.BinaryField(
        blank=True,
        null=True,
        help_text="Encrypted Flitt Merchant Password"
    )

    # Quickshipper Courier Integration Settings
    quickshipper_enabled = models.BooleanField(
        default=False,
        help_text="When ON, the storefront uses Quickshipper for live courier quotes "
                  "and replaces static ShippingMethod rows at checkout."
    )
    quickshipper_api_key_encrypted = models.BinaryField(
        blank=True,
        null=True,
        help_text="Encrypted Quickshipper API key"
    )
    quickshipper_use_production = models.BooleanField(
        default=False,
        help_text="Use Quickshipper production endpoint (unchecked = sandbox)"
    )
    quickshipper_webhook_secret = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text="Auto-generated secret used to verify inbound Quickshipper webhook signatures"
    )

    # Pickup address sent to Quickshipper for every shipment created from this tenant
    quickshipper_pickup_contact_name = models.CharField(
        max_length=200, blank=True,
        help_text="Name of the person courier should ask for at pickup"
    )
    quickshipper_pickup_phone = models.CharField(
        max_length=50, blank=True,
        help_text="Pickup contact phone (E.164 preferred)"
    )
    quickshipper_pickup_address = models.CharField(
        max_length=500, blank=True,
        help_text="Pickup street address"
    )
    quickshipper_pickup_city = models.CharField(
        max_length=100, blank=True,
        help_text="Pickup city (e.g., Tbilisi)"
    )
    quickshipper_pickup_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        help_text="Pickup latitude — set via the admin map picker"
    )
    quickshipper_pickup_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        help_text="Pickup longitude — set via the admin map picker"
    )
    quickshipper_pickup_extra_instructions = models.TextField(
        blank=True,
        help_text="Pickup notes for the courier (door code, floor, etc.)"
    )

    # Payment settings
    enable_cash_on_delivery = models.BooleanField(
        default=True,
        help_text="Allow cash on delivery payments"
    )
    enable_card_payment = models.BooleanField(
        default=True,
        help_text="Allow card payments via BOG"
    )

    # Store information
    store_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Store name for payment descriptions"
    )
    store_email = models.EmailField(
        blank=True,
        help_text="Store contact email"
    )
    store_phone = models.CharField(
        max_length=50,
        blank=True,
        help_text="Store contact phone"
    )

    # E-commerce Frontend Deployment
    ecommerce_frontend_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        help_text="URL of the deployed e-commerce storefront"
    )
    vercel_project_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Vercel project ID for the e-commerce frontend"
    )
    deployment_status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('deploying', 'Deploying'),
            ('deployed', 'Deployed'),
            ('failed', 'Failed'),
        ],
        default='pending',
        help_text="Current deployment status of the e-commerce frontend"
    )
    custom_domain = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Custom domain for the e-commerce storefront (e.g., shop.example.com)"
    )

    # Theme Configuration (shadcn/ui CSS variables)
    theme_preset = models.CharField(
        max_length=50,
        default='default',
        choices=[
            ('default', 'Default'),
            ('rounded', 'Rounded'),
            ('sharp', 'Sharp'),
            ('soft', 'Soft'),
            ('custom', 'Custom'),
        ],
        help_text="Predefined theme preset"
    )
    theme_primary_color = models.CharField(
        max_length=50,
        default="221 83% 53%",
        help_text="Primary color in HSL format (e.g., '221 83% 53%')"
    )
    theme_secondary_color = models.CharField(
        max_length=50,
        default="215 16% 47%",
        help_text="Secondary color in HSL format"
    )
    theme_accent_color = models.CharField(
        max_length=50,
        default="221 83% 53%",
        help_text="Accent color in HSL format"
    )
    theme_background_color = models.CharField(
        max_length=50,
        default="0 0% 100%",
        help_text="Background color in HSL format"
    )
    theme_foreground_color = models.CharField(
        max_length=50,
        default="0 0% 9%",
        help_text="Foreground/text color in HSL format"
    )
    theme_muted_color = models.CharField(
        max_length=50,
        default="0 0% 96%",
        help_text="Muted background color in HSL format"
    )
    theme_muted_foreground_color = models.CharField(
        max_length=50,
        default="0 0% 45%",
        help_text="Muted foreground/text color in HSL format"
    )
    theme_destructive_color = models.CharField(
        max_length=50,
        default="0 84.2% 60.2%",
        help_text="Destructive/error color in HSL format"
    )
    theme_border_color = models.CharField(
        max_length=50,
        default="0 0% 90%",
        help_text="Border color in HSL format"
    )
    theme_border_radius = models.CharField(
        max_length=20,
        default="0.5rem",
        help_text="Border radius value (e.g., '0.5rem', '1rem', '0')"
    )
    theme_card_color = models.CharField(
        max_length=50,
        default="0 0% 100%",
        help_text="Card background color in HSL format"
    )
    theme_card_foreground_color = models.CharField(
        max_length=50,
        default="0 0% 9%",
        help_text="Card foreground/text color in HSL format"
    )

    # Tax configuration
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text='Tax rate percentage, e.g. 18.00'
    )
    tax_inclusive = models.BooleanField(
        default=False,
        help_text='True = product prices include tax'
    )
    tax_label = models.CharField(
        max_length=50, default='VAT', blank=True,
        help_text='Label for tax (e.g. VAT, GST, Sales Tax)'
    )

    # Homepage variant
    HOMEPAGE_VARIANT_CHOICES = [
        ('classic', 'Classic'),
        ('modern', 'Modern'),
        ('minimal', 'Minimal'),
        ('boutique', 'Boutique'),
        ('marketplace', 'Marketplace'),
    ]
    homepage_variant = models.CharField(
        max_length=20,
        choices=HOMEPAGE_VARIANT_CHOICES,
        blank=True,
        default='',
    )

    # Storefront visual template — orthogonal to `homepage_variant` (which
    # only affects the homepage section composition). This switches the
    # entire storefront's visual language: fonts, colour tokens, header /
    # footer / page shells. Each tenant picks one; default is the existing
    # clean shadcn look.
    STOREFRONT_TEMPLATE_CHOICES = [
        ('classic', 'Classic — clean & neutral'),
        ('voltage', 'Voltage — bold electronics'),
    ]
    storefront_template = models.CharField(
        max_length=20,
        choices=STOREFRONT_TEMPLATE_CHOICES,
        default='classic',
        help_text='Which visual template the storefront renders.',
    )

    # Voltage-template tweaks. Only applied when storefront_template = 'voltage'.
    # Mirror the data-* attribute axes the prototype uses on <html>:
    #   data-theme    — accent colour pair
    #   data-mode     — light / dark
    #   data-density  — paddings + row heights
    #   data-radius   — corner radius scale
    #   data-fontpair — display + UI font pair
    VOLTAGE_THEME_CHOICES = [
        ('refurb', 'Refurb (voltage yellow + cobalt)'),
        ('cobalt', 'Cobalt (cobalt + voltage yellow)'),
        ('ember', 'Ember (orange + ink)'),
        ('forest', 'Forest (green + voltage yellow)'),
        ('violet', 'Violet (violet + voltage yellow)'),
        ('mono', 'Mono (ink only)'),
        ('rose', 'Rose (rose + ink)'),
    ]
    VOLTAGE_MODE_CHOICES = [('light', 'Light'), ('dark', 'Dark')]
    VOLTAGE_DENSITY_CHOICES = [
        ('compact', 'Compact'),
        ('cozy', 'Cozy'),
        ('comfortable', 'Comfortable'),
    ]
    VOLTAGE_RADIUS_CHOICES = [
        ('sharp', 'Sharp'),
        ('soft', 'Soft'),
        ('rounded', 'Rounded'),
    ]
    VOLTAGE_FONTPAIR_CHOICES = [
        ('bricolage-inter', 'Bricolage Grotesque + Inter'),
        ('space-dm', 'Space Grotesk + DM Sans'),
        ('serif-inter', 'Instrument Serif + Inter'),
        ('mono-inter', 'JetBrains Mono + Inter'),
    ]

    voltage_theme_preset = models.CharField(
        max_length=20, choices=VOLTAGE_THEME_CHOICES, default='refurb',
        help_text='Voltage colour preset (--accent / --accent-2 pair).',
    )
    voltage_color_mode = models.CharField(
        max_length=10, choices=VOLTAGE_MODE_CHOICES, default='light',
        help_text='Voltage light / dark mode.',
    )
    voltage_density = models.CharField(
        max_length=15, choices=VOLTAGE_DENSITY_CHOICES, default='cozy',
        help_text='Voltage UI density (paddings / row heights).',
    )
    voltage_radius = models.CharField(
        max_length=10, choices=VOLTAGE_RADIUS_CHOICES, default='soft',
        help_text='Voltage corner-radius scale.',
    )
    voltage_font_pair = models.CharField(
        max_length=20, choices=VOLTAGE_FONTPAIR_CHOICES, default='bricolage-inter',
        help_text='Voltage display + UI font pair.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Ecommerce Settings'
        verbose_name_plural = 'Ecommerce Settings'

    def __str__(self):
        return f"Ecommerce Settings for {self.tenant.name}"

    def set_bog_secret(self, secret: str):
        """Encrypt and store BOG client secret"""
        from cryptography.fernet import Fernet
        from django.conf import settings

        # Use Django's SECRET_KEY for encryption
        key = settings.SECRET_KEY[:32].encode().ljust(32, b'0')
        fernet = Fernet(base64.urlsafe_b64encode(key))
        self.bog_client_secret_encrypted = fernet.encrypt(secret.encode())

    def get_bog_secret(self) -> str:
        """Decrypt and return BOG client secret"""
        if not self.bog_client_secret_encrypted:
            return ''

        from cryptography.fernet import Fernet
        from django.conf import settings

        key = settings.SECRET_KEY[:32].encode().ljust(32, b'0')
        fernet = Fernet(base64.urlsafe_b64encode(key))
        return fernet.decrypt(bytes(self.bog_client_secret_encrypted)).decode()

    @property
    def has_bog_credentials(self) -> bool:
        """Check if BOG credentials are configured"""
        return bool(self.bog_client_id and self.bog_client_secret_encrypted)

    # --- TBC Bank encryption helpers ---

    def set_tbc_secret(self, secret: str):
        """Encrypt and store TBC Bank client secret"""
        from cryptography.fernet import Fernet
        from django.conf import settings

        key = settings.SECRET_KEY[:32].encode().ljust(32, b'0')
        fernet = Fernet(base64.urlsafe_b64encode(key))
        self.tbc_client_secret_encrypted = fernet.encrypt(secret.encode())

    def get_tbc_secret(self) -> str:
        """Decrypt and return TBC Bank client secret"""
        if not self.tbc_client_secret_encrypted:
            return ''

        from cryptography.fernet import Fernet
        from django.conf import settings

        key = settings.SECRET_KEY[:32].encode().ljust(32, b'0')
        fernet = Fernet(base64.urlsafe_b64encode(key))
        return fernet.decrypt(bytes(self.tbc_client_secret_encrypted)).decode()

    @property
    def has_tbc_credentials(self) -> bool:
        """Check if TBC Bank credentials are configured"""
        return bool(self.tbc_client_id and self.tbc_client_secret_encrypted and self.tbc_api_key)

    # --- Flitt encryption helpers ---

    def set_flitt_password(self, password: str):
        """Encrypt and store Flitt merchant password"""
        from cryptography.fernet import Fernet
        from django.conf import settings

        key = settings.SECRET_KEY[:32].encode().ljust(32, b'0')
        fernet = Fernet(base64.urlsafe_b64encode(key))
        self.flitt_password_encrypted = fernet.encrypt(password.encode())

    def get_flitt_password(self) -> str:
        """Decrypt and return Flitt merchant password"""
        if not self.flitt_password_encrypted:
            return ''

        from cryptography.fernet import Fernet
        from django.conf import settings

        key = settings.SECRET_KEY[:32].encode().ljust(32, b'0')
        fernet = Fernet(base64.urlsafe_b64encode(key))
        return fernet.decrypt(bytes(self.flitt_password_encrypted)).decode()

    @property
    def has_flitt_credentials(self) -> bool:
        """Check if Flitt credentials are configured"""
        return bool(self.flitt_merchant_id and self.flitt_password_encrypted)

    def set_paddle_api_key(self, api_key: str):
        """Encrypt and store Paddle API key"""
        import base64
        from cryptography.fernet import Fernet
        from django.conf import settings

        key = settings.SECRET_KEY[:32].encode().ljust(32, b'0')
        fernet = Fernet(base64.urlsafe_b64encode(key))
        self.paddle_api_key_encrypted = fernet.encrypt(api_key.encode())

    def get_paddle_api_key(self) -> str:
        """Decrypt and return Paddle API key"""
        if not self.paddle_api_key_encrypted:
            return ''
        import base64
        from cryptography.fernet import Fernet
        from django.conf import settings

        key = settings.SECRET_KEY[:32].encode().ljust(32, b'0')
        fernet = Fernet(base64.urlsafe_b64encode(key))
        return fernet.decrypt(bytes(self.paddle_api_key_encrypted)).decode()

    @property
    def has_paddle_credentials(self) -> bool:
        """Check if Paddle credentials are configured"""
        return bool(self.paddle_api_key_encrypted and self.paddle_client_token)

    # --- Quickshipper encryption helpers ---

    def set_quickshipper_api_key(self, api_key: str):
        """Encrypt and store Quickshipper API key"""
        from cryptography.fernet import Fernet
        from django.conf import settings

        key = settings.SECRET_KEY[:32].encode().ljust(32, b'0')
        fernet = Fernet(base64.urlsafe_b64encode(key))
        self.quickshipper_api_key_encrypted = fernet.encrypt(api_key.encode())

    def get_quickshipper_api_key(self) -> str:
        """Decrypt and return Quickshipper API key"""
        if not self.quickshipper_api_key_encrypted:
            return ''
        from cryptography.fernet import Fernet
        from django.conf import settings

        key = settings.SECRET_KEY[:32].encode().ljust(32, b'0')
        fernet = Fernet(base64.urlsafe_b64encode(key))
        return fernet.decrypt(bytes(self.quickshipper_api_key_encrypted)).decode()

    @property
    def has_quickshipper_credentials(self) -> bool:
        """True iff this tenant has stored a Quickshipper API key."""
        return bool(self.quickshipper_api_key_encrypted)

    def ensure_quickshipper_webhook_secret(self) -> str:
        """Lazily allocate a per-tenant webhook secret on first use. Returned
        as the existing value if already set, or freshly generated and saved.
        Quickshipper uses this secret to HMAC-sign callbacks back to us so the
        webhook view can prove the payload originated from a configured tenant."""
        if self.quickshipper_webhook_secret:
            return self.quickshipper_webhook_secret
        import secrets as _secrets
        self.quickshipper_webhook_secret = _secrets.token_urlsafe(32)
        self.save(update_fields=['quickshipper_webhook_secret'])
        return self.quickshipper_webhook_secret


class PasswordResetToken(models.Model):
    """
    Password reset tokens for ecommerce clients
    Tokens expire after 24 hours
    """
    client = models.ForeignKey(
        EcommerceClient,
        on_delete=models.CASCADE,
        related_name='password_reset_tokens'
    )
    token = models.CharField(max_length=100, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    is_used = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Password Reset Token'
        verbose_name_plural = 'Password Reset Tokens'
        indexes = [
            models.Index(fields=['token', 'is_used']),
            models.Index(fields=['client', '-created_at']),
        ]

    def __str__(self):
        return f"Reset token for {self.client.email} - {'Used' if self.is_used else 'Active'}"

    @staticmethod
    def generate_token():
        """Generate a secure random token"""
        import secrets
        return secrets.token_urlsafe(32)

    @staticmethod
    def generate_code():
        """Generate a 6-digit verification code"""
        import random
        return ''.join([str(random.randint(0, 9)) for _ in range(6)])

    def is_valid(self):
        """Check if token is still valid (not used and not expired)"""
        from django.utils import timezone
        return not self.is_used and self.expires_at > timezone.now()

    def mark_as_used(self):
        """Mark token as used"""
        from django.utils import timezone
        self.is_used = True
        self.used_at = timezone.now()
        self.save(update_fields=['is_used', 'used_at'])


class ClientCard(models.Model):
    """
    Store saved payment card details for ecommerce clients
    Cards are validated with 0 GEL transactions and saved for future purchases
    """
    client = models.ForeignKey(
        EcommerceClient,
        on_delete=models.CASCADE,
        related_name='saved_cards',
        help_text='Client who owns this saved card'
    )

    # BOG order ID for recurring charges
    parent_order_id = models.CharField(
        max_length=100,
        unique=True,
        help_text='BOG parent order ID used for charging this card'
    )

    # Card details (masked/safe to store)
    card_type = models.CharField(
        max_length=20,
        blank=True,
        help_text='Card type (e.g., mc, visa)'
    )
    masked_card_number = models.CharField(
        max_length=20,
        blank=True,
        help_text='Masked card number (e.g., 531125***1450)'
    )
    card_expiry = models.CharField(
        max_length=7,
        blank=True,
        help_text='Card expiry date (MM/YY format)'
    )

    # Default card flag
    is_default = models.BooleanField(
        default=False,
        help_text='Whether this is the default payment card for this client'
    )

    # Status
    is_active = models.BooleanField(
        default=True,
        help_text='Whether this card can be used for payments'
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', '-created_at']
        verbose_name = 'Client Payment Card'
        verbose_name_plural = 'Client Payment Cards'
        indexes = [
            models.Index(fields=['client', 'is_active']),
            models.Index(fields=['parent_order_id']),
        ]

    def __str__(self):
        return f"{self.client.email} - {self.masked_card_number}"

    def save(self, *args, **kwargs):
        # If this card is being set as default, unset other defaults for this client
        if self.is_default:
            ClientCard.objects.filter(
                client=self.client,
                is_default=True
            ).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class HomepageSection(models.Model):
    """
    Configurable homepage section for ecommerce storefronts.
    Tenants can create, reorder, and configure sections to customize their homepage.
    """
    SECTION_TYPE_CHOICES = [
        ('hero_banner', 'Hero Banner/Slider'),
        ('featured_products', 'Featured Products'),
        ('category_grid', 'Category Grid'),
        ('product_by_attribute', 'Products by Attribute'),
        ('statistics', 'Statistics/Achievements'),
        ('branches', 'Store Branches/Locations'),
        ('custom_content', 'Custom ItemList Content'),
    ]

    DISPLAY_MODE_CHOICES = [
        ('slider', 'Slider/Carousel'),
        ('grid', 'Grid Layout'),
        ('single', 'Single Item'),
        ('list', 'Vertical List'),
    ]

    # Basic info
    title = models.JSONField(
        help_text='Section title in different languages: {"en": "Featured Products", "ka": "რჩეული პროდუქტები"}'
    )
    subtitle = models.JSONField(
        default=dict,
        blank=True,
        help_text='Optional subtitle in different languages'
    )
    section_type = models.CharField(
        max_length=30,
        choices=SECTION_TYPE_CHOICES,
        help_text='Type of content this section displays'
    )
    position = models.IntegerField(
        default=0,
        help_text='Order position on the homepage (lower numbers appear first)'
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Whether this section is visible on the homepage'
    )

    # Data source - link to ItemList for dynamic content
    item_list = models.ForeignKey(
        'tickets.ItemList',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='homepage_sections',
        help_text='ItemList to use as data source (for banners, categories, branches, etc.)'
    )

    # For product filtering by attribute
    attribute_key = models.CharField(
        max_length=100,
        blank=True,
        help_text='Attribute key to filter products (for product_by_attribute type)'
    )
    attribute_value = models.CharField(
        max_length=255,
        blank=True,
        help_text='Attribute value to filter products'
    )

    # Display configuration
    display_mode = models.CharField(
        max_length=20,
        choices=DISPLAY_MODE_CHOICES,
        default='grid',
        help_text='How to display items in this section'
    )
    settings = models.JSONField(
        default=dict,
        blank=True,
        help_text='Additional display settings: {"columns": 3, "autoSlide": true, "slideInterval": 5000, "itemsPerView": 4}'
    )

    # Styling
    background_color = models.CharField(
        max_length=20,
        blank=True,
        help_text='Background color (e.g., "#f5f5f5", "transparent")'
    )
    background_image_url = models.URLField(
        blank=True,
        help_text='Background image URL for this section'
    )
    text_color = models.CharField(
        max_length=20,
        blank=True,
        help_text='Text color for the section'
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position', 'created_at']
        verbose_name = 'Homepage Section'
        verbose_name_plural = 'Homepage Sections'
        indexes = [
            models.Index(fields=['position']),
            models.Index(fields=['is_active', 'position']),
        ]

    def __str__(self):
        title_str = self.title.get('en', list(self.title.values())[0]) if isinstance(self.title, dict) else str(self.title)
        return f"{title_str} ({self.get_section_type_display()})"

    def get_title(self, language='en'):
        """Get title in specified language with fallback"""
        if isinstance(self.title, dict):
            return self.title.get(language, self.title.get('en', ''))
        return str(self.title)

    def get_subtitle(self, language='en'):
        """Get subtitle in specified language with fallback"""
        if isinstance(self.subtitle, dict):
            return self.subtitle.get(language, self.subtitle.get('en', ''))
        return str(self.subtitle) if self.subtitle else ''


class ShippingMethod(models.Model):
    """
    Configurable shipping methods for ecommerce orders.
    Supports multilingual names/descriptions and free shipping thresholds.
    """
    name = models.JSONField(
        default=dict,
        help_text='Multilingual name: {"en": "Standard Shipping", "ka": "..."}'
    )
    description = models.JSONField(
        default=dict, blank=True,
        help_text='Multilingual description'
    )
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    free_shipping_threshold = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Orders above this amount get free shipping'
    )
    is_active = models.BooleanField(default=True)
    estimated_days = models.IntegerField(default=3, help_text='Estimated delivery days')
    position = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position']
        verbose_name = 'Shipping Method'
        verbose_name_plural = 'Shipping Methods'
        indexes = [
            models.Index(fields=['is_active', 'position']),
        ]

    def __str__(self):
        if isinstance(self.name, dict):
            return self.name.get('en', str(list(self.name.values())[0]) if self.name else 'Unnamed')
        return str(self.name)

    def get_name(self, language='en'):
        if isinstance(self.name, dict):
            return self.name.get(language, self.name.get('en', ''))
        return str(self.name)

    def get_effective_price(self, order_subtotal):
        """Return 0 if order meets free shipping threshold, else method price."""
        if self.free_shipping_threshold and order_subtotal >= self.free_shipping_threshold:
            return Decimal('0')
        return self.price


class PromoCode(models.Model):
    """
    Promotional codes for ecommerce discounts.
    Supports percentage and fixed-amount discounts with usage limits and validity periods.
    """
    DISCOUNT_TYPES = [
        ('percentage', 'Percentage'),
        ('fixed', 'Fixed Amount'),
    ]

    code = models.CharField(max_length=50, unique=True)
    discount_type = models.CharField(max_length=10, choices=DISCOUNT_TYPES)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    min_order_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Minimum order subtotal required to use this code'
    )
    max_uses = models.IntegerField(
        null=True, blank=True,
        help_text='Maximum number of times this code can be used (null = unlimited)'
    )
    times_used = models.IntegerField(default=0)
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Promo Code'
        verbose_name_plural = 'Promo Codes'
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['is_active', 'valid_from', 'valid_until']),
        ]

    def __str__(self):
        return f"{self.code} ({self.get_discount_type_display()})"

    def is_valid(self, subtotal=None):
        """Check if this promo code can be used."""
        from django.utils import timezone
        now = timezone.now()
        if not self.is_active:
            return False, 'This promo code is not active.'
        if now < self.valid_from:
            return False, 'This promo code is not yet valid.'
        if now > self.valid_until:
            return False, 'This promo code has expired.'
        if self.max_uses is not None and self.times_used >= self.max_uses:
            return False, 'This promo code has reached its usage limit.'
        if subtotal is not None and self.min_order_amount and subtotal < self.min_order_amount:
            return False, f'Minimum order amount of {self.min_order_amount} required.'
        return True, 'Valid'

    def calculate_discount(self, subtotal):
        """Calculate the discount amount for a given subtotal."""
        if self.discount_type == 'percentage':
            discount = (subtotal * self.discount_value) / Decimal('100')
            return min(discount, subtotal)
        else:
            return min(self.discount_value, subtotal)


class ProductReview(models.Model):
    """
    Product reviews from ecommerce clients.
    One review per product per client, with verified purchase tracking.
    """
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='reviews'
    )
    client = models.ForeignKey(
        EcommerceClient, on_delete=models.CASCADE, related_name='reviews'
    )
    rating = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    title = models.CharField(max_length=200, blank=True)
    content = models.TextField(blank=True)
    is_verified_purchase = models.BooleanField(default=False)
    is_approved = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['product', 'client']
        ordering = ['-created_at']
        verbose_name = 'Product Review'
        verbose_name_plural = 'Product Reviews'
        indexes = [
            models.Index(fields=['product', '-created_at']),
            models.Index(fields=['client']),
            models.Index(fields=['is_approved', '-created_at']),
        ]

    def __str__(self):
        return f"Review by {self.client.email} for {self.product.sku} ({self.rating}/5)"


