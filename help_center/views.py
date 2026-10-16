from django.db.models import Count, Q
from django.utils.http import http_date, parse_http_date_safe
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response

from .models import HelpArticle, HelpCategory
from .serializers import (
    SUPPORTED_LANGUAGES,
    HelpArticleAdminSerializer,
    HelpArticleDetailSerializer,
    HelpArticleListSerializer,
    HelpCategoryAdminSerializer,
    HelpCategoryDetailSerializer,
    HelpCategoryListSerializer,
    get_visibility_mode,
)


# =============================================================================
# Shared OpenAPI query parameters for the public endpoints
# =============================================================================

LANG_PARAM = OpenApiParameter(
    'lang', OpenApiTypes.STR, OpenApiParameter.QUERY,
    enum=list(SUPPORTED_LANGUAGES),
    description='Language for the localised fields (name, title, summary, content, ...). Defaults to `en`.',
)
FOR_DASHBOARD_PARAM = OpenApiParameter(
    'for_dashboard', OpenApiTypes.BOOL, OpenApiParameter.QUERY,
    description='`true` → only content flagged `show_in_dashboard` (tenant dashboard help). '
                'Takes precedence over `for_public` when both are sent.',
)
FOR_PUBLIC_PARAM = OpenApiParameter(
    'for_public', OpenApiTypes.BOOL, OpenApiParameter.QUERY,
    description='`true` → only content flagged `show_on_public` (public /docs site). '
                'Ignored when `for_dashboard=true`.',
)
VISIBILITY_PARAMS = [LANG_PARAM, FOR_DASHBOARD_PARAM, FOR_PUBLIC_PARAM]

CATEGORY_PARAM = OpenApiParameter(
    'category', OpenApiTypes.STR, OpenApiParameter.QUERY,
    description='Filter by category slug.',
)
CONTENT_TYPE_PARAM = OpenApiParameter(
    'content_type', OpenApiTypes.STR, OpenApiParameter.QUERY,
    enum=[key for key, _ in HelpArticle.CONTENT_TYPE_CHOICES],
    description='Filter by article content type.',
)
FEATURED_PARAM = OpenApiParameter(
    'featured', OpenApiTypes.BOOL, OpenApiParameter.QUERY,
    description='`true` → only featured articles.',
)
ORDERING_PARAM = OpenApiParameter(
    'ordering', OpenApiTypes.STR, OpenApiParameter.QUERY,
    description='Sort field; prefix with `-` for descending. One of `position`, `created_at`, `updated_at`.',
)


# =============================================================================
# Public ViewSets (No Authentication Required)
# =============================================================================

@extend_schema_view(
    list=extend_schema(
        summary='List help categories',
        description='Active help categories ordered by position. Content is global (shared across tenants).',
        parameters=VISIBILITY_PARAMS + [ORDERING_PARAM],
    ),
    retrieve=extend_schema(
        summary='Get a help category with its articles',
        description='One active category (by slug) with its active articles embedded '
                    '(not paginated; `for_dashboard`/`for_public` filter the embedded articles too).',
        parameters=VISIBILITY_PARAMS,
    ),
)
class PublicHelpCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Public read-only ViewSet for help categories.
    Accessible without authentication.
    """
    permission_classes = [AllowAny]
    lookup_field = 'slug'
    # No SearchFilter: the view never exposed a working `search` param, and
    # leaving the project default in place advertised a bogus one in the schema.
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['position', 'created_at', 'updated_at']
    queryset = HelpCategory.objects.none()  # real queryset built in get_queryset()

    def get_queryset(self):
        # article_count is annotated DB-side; the list serializer never walks
        # `articles`, so there is nothing to prefetch here.
        queryset = HelpCategory.objects.filter(is_active=True).annotate(
            _article_count=Count('articles', filter=Q(articles__is_active=True))
        )

        # Filter by visibility (for_dashboard wins over for_public)
        mode = get_visibility_mode(self.request)
        if mode == 'dashboard':
            queryset = queryset.filter(show_in_dashboard=True)
        elif mode == 'public':
            queryset = queryset.filter(show_on_public=True)

        return queryset.order_by('position', 'created_at')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return HelpCategoryDetailSerializer
        return HelpCategoryListSerializer


@extend_schema_view(
    list=extend_schema(
        summary='List help articles',
        description='Active articles in active categories. Paginated. '
                    'Default ordering: category position, article position, newest first.',
        parameters=VISIBILITY_PARAMS + [CATEGORY_PARAM, CONTENT_TYPE_PARAM, FEATURED_PARAM, ORDERING_PARAM],
    ),
    retrieve=extend_schema(
        summary='Get a help article',
        description='One active article by slug (slugs are unique across categories). '
                    'The response carries a `Last-Modified` header (from `updated_at`) and honours '
                    '`If-Modified-Since` with `304 Not Modified`.',
        parameters=VISIBILITY_PARAMS,
        responses={
            200: HelpArticleDetailSerializer,
            304: None,
        },
    ),
)
class PublicHelpArticleViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Public read-only ViewSet for help articles.
    Accessible without authentication.
    """
    permission_classes = [AllowAny]
    lookup_field = 'slug'
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['position', 'created_at', 'updated_at']
    ordering = ['category__position', 'position', '-created_at']
    queryset = HelpArticle.objects.none()  # real queryset built in get_queryset()

    def get_queryset(self):
        queryset = HelpArticle.objects.filter(
            is_active=True,
            category__is_active=True
        ).select_related('category')

        # Filter by visibility (for_dashboard wins over for_public)
        mode = get_visibility_mode(self.request)
        if mode == 'dashboard':
            queryset = queryset.filter(show_in_dashboard=True, category__show_in_dashboard=True)
        elif mode == 'public':
            queryset = queryset.filter(show_on_public=True, category__show_on_public=True)

        # Filter by category
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category__slug=category)

        # Filter by content type
        content_type = self.request.query_params.get('content_type')
        if content_type:
            queryset = queryset.filter(content_type=content_type)

        # Filter featured only
        featured = self.request.query_params.get('featured')
        if featured == 'true':
            queryset = queryset.filter(is_featured=True)

        return queryset

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return HelpArticleDetailSerializer
        return HelpArticleListSerializer

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        last_modified = int(instance.updated_at.timestamp()) if instance.updated_at else None

        # Conditional GET: 304 when the client's copy is still current.
        if_modified_since = parse_http_date_safe(request.META.get('HTTP_IF_MODIFIED_SINCE', ''))
        if last_modified is not None and if_modified_since is not None and last_modified <= if_modified_since:
            response = Response(status=status.HTTP_304_NOT_MODIFIED)
        else:
            serializer = self.get_serializer(instance)
            response = Response(serializer.data)

        if last_modified is not None:
            response['Last-Modified'] = http_date(last_modified)
        # Without this, browsers / NSURLCache would apply heuristic freshness
        # to a Last-Modified response and could show stale articles after an
        # admin edit. `no-cache` keeps today's always-revalidate behaviour.
        response['Cache-Control'] = 'no-cache'
        return response

    @extend_schema(
        operation_id='help_public_articles_featured_list',
        summary='Featured help articles',
        description='Up to 6 articles flagged `is_featured`, same filters as the list. Bare list, not paginated.',
        parameters=VISIBILITY_PARAMS + [CATEGORY_PARAM, CONTENT_TYPE_PARAM],
        responses={200: HelpArticleListSerializer(many=True)},
    )
    @action(detail=False, methods=['get'], pagination_class=None)
    def featured(self, request):
        """Get featured articles"""
        queryset = self.get_queryset().filter(is_featured=True)[:6]
        serializer = HelpArticleListSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)


@extend_schema(
    operation_id='help_public_search_list',
    summary='Search help articles',
    description='Case-insensitive substring search over title, summary, content and FAQ items '
                '(all languages). Returns at most 20 matches as a bare list; `q` shorter than 2 '
                'characters returns `[]`. No authentication required.',
    parameters=[
        OpenApiParameter(
            'q', OpenApiTypes.STR, OpenApiParameter.QUERY, required=True,
            description='Search text (minimum 2 characters).',
        ),
    ] + VISIBILITY_PARAMS,
    responses={200: HelpArticleListSerializer(many=True)},
)
@api_view(['GET'])
@permission_classes([AllowAny])
def search_help(request):
    """
    Search help articles by query.
    Public endpoint - no authentication required.
    """
    query = request.query_params.get('q', '')

    if len(query) < 2:
        return Response([])

    queryset = HelpArticle.objects.filter(is_active=True, category__is_active=True)

    # Apply visibility filters (for_dashboard wins over for_public; same
    # category-level gating as the article list).
    mode = get_visibility_mode(request)
    if mode == 'dashboard':
        queryset = queryset.filter(show_in_dashboard=True, category__show_in_dashboard=True)
    elif mode == 'public':
        queryset = queryset.filter(show_on_public=True, category__show_on_public=True)

    # Search in title, summary, and content (JSON fields contain the search term)
    queryset = queryset.filter(
        Q(title__icontains=query) |
        Q(summary__icontains=query) |
        Q(content__icontains=query) |
        Q(faq_items__icontains=query)
    ).select_related('category')[:20]

    serializer = HelpArticleListSerializer(queryset, many=True, context={'request': request})
    return Response(serializer.data)


# =============================================================================
# Admin ViewSets (Superuser Required)
# =============================================================================

class HelpCategoryViewSet(viewsets.ModelViewSet):
    """
    Admin ViewSet for managing help categories.
    Requires superuser authentication.
    """
    queryset = HelpCategory.objects.prefetch_related('articles').all()
    serializer_class = HelpCategoryAdminSerializer
    permission_classes = [IsAdminUser]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'slug', 'description']
    ordering_fields = ['position', 'created_at', 'updated_at']
    ordering = ['position', 'created_at']
    lookup_field = 'slug'


class HelpArticleViewSet(viewsets.ModelViewSet):
    """
    Admin ViewSet for managing help articles.
    Requires superuser authentication.
    """
    queryset = HelpArticle.objects.all().select_related('category')
    serializer_class = HelpArticleAdminSerializer
    permission_classes = [IsAdminUser]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['title', 'slug', 'summary', 'content']
    ordering_fields = ['position', 'created_at', 'updated_at']
    ordering = ['category__position', 'position', '-created_at']
    lookup_field = 'slug'

    def get_queryset(self):
        queryset = super().get_queryset()

        # Filter by category
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category__slug=category)

        # Filter by content type
        content_type = self.request.query_params.get('content_type')
        if content_type:
            queryset = queryset.filter(content_type=content_type)

        # Filter by active status
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active == 'true')

        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)
