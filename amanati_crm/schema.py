"""
Custom OpenAPI schema preprocessing for feature-based endpoint filtering
"""
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.plumbing import build_basic_type
from django.urls import resolve


# Mapping of URL patterns to required features
FEATURE_REQUIREMENTS = {
    # Ecommerce endpoints
    'ecommerce': 'ecommerce_crm',
    'api/ecommerce/': 'ecommerce_crm',

    # Booking endpoints
    'bookings': 'booking_management',
    'api/bookings/': 'booking_management',

    # Social integrations
    'social': 'whatsapp_integration',  # Will need more granular mapping
    'api/social/whatsapp': 'whatsapp_integration',
    'api/social/facebook': 'facebook_integration',
    'api/social/instagram': 'instagram_integration',

    # Tickets (core - always available)
    'tickets': 'ticket_management',
    'api/tickets/': 'ticket_management',
}


def get_required_feature_for_path(path):
    """
    Determine which feature is required for a given API path

    Args:
        path: The API endpoint path

    Returns:
        str or None: The feature key required, or None if always available
    """
    # Check exact matches first
    for pattern, feature in FEATURE_REQUIREMENTS.items():
        if pattern in path:
            return feature

    # Check for view-level feature requirements (if view has feature_required attribute)
    try:
        resolved = resolve(path)
        view = resolved.func

        # Check if view has feature_required attribute
        if hasattr(view, 'cls'):
            view_class = view.cls
            if hasattr(view_class, 'feature_required'):
                return view_class.feature_required
        elif hasattr(view, 'feature_required'):
            return view.feature_required
    except:
        pass

    return None


def feature_based_preprocessing_hook(endpoints):
    """
    Preprocessing hook for drf-spectacular that filters endpoints based on features

    This function is called during schema generation and filters out endpoints
    that the requesting user doesn't have access to based on their tenant's
    subscribed features.

    Args:
        endpoints: List of (path, path_regex, method, callback) tuples

    Returns:
        List of filtered endpoints
    """
    # Import here to avoid circular imports
    from django.contrib.auth import get_user_model
    from rest_framework.request import Request

    # Get the current request from thread local storage (set by middleware)
    from threading import local
    _thread_locals = local()

    if not hasattr(_thread_locals, 'request'):
        # If no request context, return all endpoints (e.g., during schema generation command)
        return endpoints

    request = _thread_locals.request

    # If user is not authenticated, return public endpoints only
    if not request or not hasattr(request, 'user') or not request.user.is_authenticated:
        filtered = []
        for path, path_regex, method, callback in endpoints:
            # Include public endpoints (auth, registration, etc.)
            if any(public in path for public in ['login', 'register', 'verify', 'password-reset', 'auth']):
                filtered.append((path, path_regex, method, callback))
        return filtered

    user = request.user

    # Superusers see everything
    if user.is_superuser:
        return endpoints

    # Get user's available features
    try:
        user_features = set(user.get_feature_keys())
    except:
        user_features = set()

    # Filter endpoints based on features
    filtered_endpoints = []

    for path, path_regex, method, callback in endpoints:
        required_feature = get_required_feature_for_path(path)

        # If no feature is required, include the endpoint
        if not required_feature:
            filtered_endpoints.append((path, path_regex, method, callback))
            continue

        # If feature is required, check if user has access
        if required_feature in user_features:
            filtered_endpoints.append((path, path_regex, method, callback))

    return filtered_endpoints


class EchoDeskAutoSchema(AutoSchema):
    """
    The project's default AutoSchema (REST_FRAMEWORK['DEFAULT_SCHEMA_CLASS']).

    Behaves exactly like drf-spectacular's AutoSchema apart from the opt-in
    path-parameter typing below.
    """

    def _resolve_path_parameters(self, variables):
        """
        Honour a view's ``schema_path_param_types`` mapping.

        Views whose ``get_queryset`` used to be opaque to schema generation had
        their ``{id}`` path parameters documented as strings, and the dashboard's
        generated client was built against that. A view that later exposes its
        model (e.g. to document its filterset) can pin the old type with::

            schema_path_param_types = {'id': OpenApiTypes.STR}
        """
        parameters = super()._resolve_path_parameters(variables)
        pinned = getattr(self.view, 'schema_path_param_types', None) or {}
        for parameter in parameters:
            if parameter['name'] in pinned:
                parameter['schema'] = build_basic_type(pinned[parameter['name']])
        return parameters


class FeatureAwareAutoSchema(EchoDeskAutoSchema):
    """
    Custom AutoSchema that respects feature requirements

    This can be used as DEFAULT_SCHEMA_CLASS to add feature metadata
    to individual endpoints.
    """

    def get_tags(self):
        """Add feature information to tags"""
        tags = super().get_tags()

        # Try to get feature requirement
        view = self.view
        feature_required = None

        if hasattr(view, 'feature_required'):
            feature_required = view.feature_required
        else:
            # Try to determine from path
            try:
                path = self.path
                feature_required = get_required_feature_for_path(path)
            except:
                pass

        # Add feature to tags if found
        if feature_required and feature_required not in tags:
            # Capitalize and format feature name for display
            feature_display = feature_required.replace('_', ' ').title()
            tags.append(f"Feature: {feature_display}")

        return tags

    def get_operation(self, path, path_regex, path_prefix, method, registry):
        """
        Override to add feature requirements to operation metadata
        """
        operation = super().get_operation(path, path_regex, path_prefix, method, registry)

        # Add feature requirement to operation description
        view = self.view
        feature_required = None

        if hasattr(view, 'feature_required'):
            feature_required = view.feature_required
        else:
            try:
                feature_required = get_required_feature_for_path(path)
            except:
                pass

        if feature_required:
            feature_display = feature_required.replace('_', ' ').title()
            feature_note = f"\n\n**Required Feature:** {feature_display}"

            if 'description' in operation:
                operation['description'] += feature_note
            else:
                operation['description'] = feature_note.strip()

        return operation


class TenantAwareSchemaGenerator(SchemaGenerator):
    """
    Custom schema generator that filters endpoints per tenant

    This generator respects both feature availability and user permissions.
    """

    def get_schema(self, request=None, public=False):
        """
        Generate schema with tenant-aware filtering
        """
        # Store request in thread local for preprocessing hook
        if request:
            from threading import local
            _thread_locals = local()
            _thread_locals.request = request

        schema = super().get_schema(request, public)

        # Add custom info about tenant and features
        if request and hasattr(request, 'user') and request.user.is_authenticated:
            try:
                features = request.user.get_feature_keys()
                schema['info']['x-tenant-features'] = features
            except:
                pass

        return schema
