from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import HelpCategory, HelpArticle


SUPPORTED_LANGUAGES = ('en', 'ka', 'ru')


def get_request_language(context, default='en'):
    """Resolve the `lang` query param from a serializer context (falls back to `en`)."""
    request = context.get('request') if context else None
    if request is not None and hasattr(request, 'query_params'):
        return request.query_params.get('lang', default) or default
    return default


def get_visibility_mode(request):
    """
    Resolve which visibility flag applies to a public help request.

    Returns ``'dashboard'`` when ``for_dashboard=true`` is sent, ``'public'``
    when ``for_public=true`` is sent (and ``for_dashboard`` is not), else
    ``None`` (no visibility filtering).

    ``for_dashboard`` takes precedence over ``for_public`` so that clients
    which send both (e.g. the web dashboard search widget) get dashboard
    content rather than silently falling back to the public catalogue.
    """
    if request is None or not hasattr(request, 'query_params'):
        return None
    if request.query_params.get('for_dashboard') == 'true':
        return 'dashboard'
    if request.query_params.get('for_public') == 'true':
        return 'public'
    return None


class LocalisedSerializerMixin:
    """Shared `lang` resolution for the localised public serializers."""

    def _get_language(self):
        return get_request_language(self.context)


# =============================================================================
# Category serializers
# =============================================================================

class HelpCategoryListSerializer(LocalisedSerializerMixin, serializers.ModelSerializer):
    """Serializer for category listing"""
    name = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    article_count = serializers.SerializerMethodField()

    class Meta:
        model = HelpCategory
        fields = [
            'id', 'name', 'slug', 'description', 'icon',
            'position', 'is_active', 'show_on_public',
            'show_in_dashboard', 'required_feature_key',
            'article_count', 'created_at', 'updated_at'
        ]

    @extend_schema_field(OpenApiTypes.STR)
    def get_name(self, obj):
        return obj.get_name(self._get_language())

    @extend_schema_field(OpenApiTypes.STR)
    def get_description(self, obj):
        return obj.get_description(self._get_language())

    @extend_schema_field(OpenApiTypes.INT)
    def get_article_count(self, obj):
        if hasattr(obj, '_article_count'):
            return obj._article_count
        return obj.articles.filter(is_active=True).count()


class HelpCategoryAdminSerializer(serializers.ModelSerializer):
    """Serializer for admin CRUD - returns raw JSON fields"""

    class Meta:
        model = HelpCategory
        fields = '__all__'


# =============================================================================
# Article serializers
# =============================================================================

class HelpArticleListSerializer(LocalisedSerializerMixin, serializers.ModelSerializer):
    """Serializer for article listing"""
    title = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()
    category_name = serializers.SerializerMethodField()
    category_slug = serializers.CharField(source='category.slug', read_only=True)

    class Meta:
        model = HelpArticle
        fields = [
            'id', 'title', 'slug', 'summary', 'content_type',
            'video_thumbnail', 'video_duration',
            'position', 'is_active', 'is_featured',
            'show_on_public', 'show_in_dashboard',
            'category_name', 'category_slug',
            'created_at', 'updated_at'
        ]

    @extend_schema_field(OpenApiTypes.STR)
    def get_title(self, obj):
        return obj.get_title(self._get_language())

    @extend_schema_field(OpenApiTypes.STR)
    def get_summary(self, obj):
        return obj.get_summary(self._get_language())

    @extend_schema_field(OpenApiTypes.STR)
    def get_category_name(self, obj):
        return obj.category.get_name(self._get_language())


class HelpGuideStepSerializer(serializers.Serializer):
    """One localised step of a step-by-step guide (schema only, read-only)."""
    step = serializers.IntegerField()
    title = serializers.CharField()
    content = serializers.CharField(help_text='Localised HTML fragment')
    image = serializers.CharField(allow_blank=True)


class HelpFAQItemSerializer(serializers.Serializer):
    """One localised FAQ question/answer pair (schema only, read-only)."""
    question = serializers.CharField()
    answer = serializers.CharField(help_text='Localised HTML fragment')


class HelpArticleDetailSerializer(LocalisedSerializerMixin, serializers.ModelSerializer):
    """Serializer for article detail view"""
    title = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()
    content = serializers.SerializerMethodField()
    guide_steps = serializers.SerializerMethodField()
    faq_items = serializers.SerializerMethodField()
    category = HelpCategoryListSerializer(read_only=True)

    class Meta:
        model = HelpArticle
        fields = [
            'id', 'title', 'slug', 'summary', 'content_type',
            'content', 'video_url', 'video_thumbnail', 'video_duration',
            'guide_steps', 'faq_items',
            'position', 'is_active', 'is_featured',
            'show_on_public', 'show_in_dashboard',
            'meta_title', 'meta_description',
            'category', 'created_at', 'updated_at', 'published_at'
        ]

    @extend_schema_field(OpenApiTypes.STR)
    def get_title(self, obj):
        return obj.get_title(self._get_language())

    @extend_schema_field(OpenApiTypes.STR)
    def get_summary(self, obj):
        return obj.get_summary(self._get_language())

    @extend_schema_field(OpenApiTypes.STR)
    def get_content(self, obj):
        return obj.get_content(self._get_language())

    @extend_schema_field(HelpGuideStepSerializer(many=True))
    def get_guide_steps(self, obj):
        if not obj.guide_steps:
            return []
        lang = self._get_language()
        # Localize step titles and content
        localized_steps = []
        for step in obj.guide_steps:
            localized_step = {
                'step': step.get('step', 0),
                'title': step.get('title', {}).get(lang, step.get('title', {}).get('en', '')),
                'content': step.get('content', {}).get(lang, step.get('content', {}).get('en', '')),
                'image': step.get('image', '')
            }
            localized_steps.append(localized_step)
        return localized_steps

    @extend_schema_field(HelpFAQItemSerializer(many=True))
    def get_faq_items(self, obj):
        if not obj.faq_items:
            return []
        lang = self._get_language()
        # Localize questions and answers
        localized_items = []
        for item in obj.faq_items:
            localized_item = {
                'question': item.get('question', {}).get(lang, item.get('question', {}).get('en', '')),
                'answer': item.get('answer', {}).get(lang, item.get('answer', {}).get('en', ''))
            }
            localized_items.append(localized_item)
        return localized_items


class HelpArticleAdminSerializer(serializers.ModelSerializer):
    """Serializer for admin CRUD - returns raw JSON fields"""

    class Meta:
        model = HelpArticle
        fields = '__all__'


# =============================================================================
# Category detail (needs HelpArticleListSerializer defined above)
# =============================================================================

class HelpCategoryDetailSerializer(HelpCategoryListSerializer):
    """Serializer for category detail with its (non-paginated) active articles"""
    articles = serializers.SerializerMethodField()

    class Meta(HelpCategoryListSerializer.Meta):
        fields = [
            'id', 'name', 'slug', 'description', 'icon',
            'position', 'is_active', 'show_on_public',
            'show_in_dashboard', 'required_feature_key',
            'article_count', 'articles', 'created_at', 'updated_at'
        ]

    @extend_schema_field(HelpArticleListSerializer(many=True))
    def get_articles(self, obj):
        # select_related avoids one category query per article when the
        # list serializer resolves category_name / category_slug.
        articles = obj.articles.filter(is_active=True).select_related('category')

        # Filter based on visibility (for_dashboard wins over for_public)
        mode = get_visibility_mode(self.context.get('request'))
        if mode == 'dashboard':
            articles = articles.filter(show_in_dashboard=True)
        elif mode == 'public':
            articles = articles.filter(show_on_public=True)

        return HelpArticleListSerializer(articles, many=True, context=self.context).data
