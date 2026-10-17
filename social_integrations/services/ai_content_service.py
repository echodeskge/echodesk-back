import logging
import json
from django.conf import settings
from openai import OpenAI, PermissionDeniedError

from social_integrations.models import AutoPostSettings, AutoPostContent
from social_integrations.services.ai_providers import AINotConfiguredError

logger = logging.getLogger(__name__)

FALLBACK_MODELS = ['gpt-4o-mini', 'gpt-3.5-turbo']


LANGUAGE_NAMES = {
    'en': 'English',
    'ka': 'Georgian (ქართული)',
    'ru': 'Russian',
    'de': 'German',
    'fr': 'French',
    'es': 'Spanish',
    'it': 'Italian',
    'tr': 'Turkish',
    'ar': 'Arabic',
    'zh': 'Chinese',
    'ja': 'Japanese',
    'ko': 'Korean',
    'pt': 'Portuguese',
    'nl': 'Dutch',
    'pl': 'Polish',
    'uk': 'Ukrainian',
    'he': 'Hebrew',
    'hi': 'Hindi',
    'pa': 'Punjabi',
}


class AIContentService:
    def __init__(self):
        api_key = getattr(settings, 'OPENAI_API_KEY', '')
        if not api_key:
            raise AINotConfiguredError("OPENAI_API_KEY is not configured")
        self.client = OpenAI(api_key=api_key)
        self.model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')

    def generate_post_for_tenant(self, auto_settings: AutoPostSettings) -> AutoPostContent:
        """Generate a social media post based on tenant settings."""
        from ecommerce_crm.models import Product
        from django.utils import timezone
        import random

        product = None
        image_url = None

        # Determine content source
        use_product = auto_settings.content_source in ('products', 'both')
        use_company = auto_settings.content_source in ('company', 'both')

        if use_product:
            # Select a product, preferring ones not recently featured
            recent_product_ids = AutoPostContent.objects.filter(
                featured_product__isnull=False,
            ).order_by('-created_at').values_list('featured_product_id', flat=True)[:10]

            products = Product.objects.filter(status='active')
            unfeatured = products.exclude(id__in=recent_product_ids)
            product_pool = unfeatured if unfeatured.exists() else products

            if product_pool.exists():
                product = random.choice(list(product_pool[:20]))
                image_url = product.image
            elif not use_company:
                # No products available and content_source is 'products' only
                raise ValueError("No active products available to feature")

        # If no product selected and we should use company info, or content_source is 'both' and random
        should_post_about_company = (
            product is None or
            (use_company and use_product and random.random() < 0.3)
        )

        if should_post_about_company:
            product = None
            image_url = None

        # Build context for AI
        previous_posts = list(
            AutoPostContent.objects.order_by('-created_at')
            .values_list('facebook_text', flat=True)[:5]
        )

        tenant_context = {
            'company_description': auto_settings.company_description,
            'language': auto_settings.content_language,
            'tone': auto_settings.tone,
            'previous_posts': previous_posts,
        }

        if product:
            product_name = product.name
            if isinstance(product_name, dict):
                product_name = product_name.get(auto_settings.content_language, product_name.get('en', str(product_name)))

            product_desc = product.description
            if isinstance(product_desc, dict):
                product_desc = product_desc.get(auto_settings.content_language, product_desc.get('en', ''))

            tenant_context['product'] = {
                'name': str(product_name),
                'description': str(product_desc) if product_desc else '',
                'price': str(product.price) if product.price else '',
            }

        result = self._call_openai(tenant_context)

        # Always generate image with DALL-E if no product image available
        if not image_url:
            from .image_service import ImageService
            image_service = ImageService()
            if product:
                default_prompt = f"Marketing product photo for: {tenant_context['product']['name']}"
            else:
                default_prompt = f"Marketing image for: {auto_settings.company_description[:100]}"
            try:
                image_url = image_service.generate_image(
                    result.get('image_prompt', default_prompt)
                )
            except Exception as e:
                logger.warning(f"Failed to generate image: {e}")
                image_url = None

        # Fallback to page logo if no image
        if not image_url:
            try:
                from social_integrations.models import FacebookPageConnection
                page = FacebookPageConnection.objects.filter(is_active=True).first()
                if page and page.page_access_token:
                    import requests as http_requests
                    resp = http_requests.get(
                        f"https://graph.facebook.com/v23.0/{page.page_id}/picture",
                        params={"redirect": "false", "type": "large", "access_token": page.page_access_token}
                    )
                    if resp.ok:
                        pic_data = resp.json().get("data", {})
                        if pic_data.get("url"):
                            image_url = pic_data["url"]
                            logger.info(f"Using page logo as fallback image")
            except Exception as e:
                logger.warning(f"Failed to get page logo fallback: {e}")

        # Determine schedule
        now = timezone.now()
        scheduled_for = now

        # Create the content record
        post = AutoPostContent.objects.create(
            status='draft' if auto_settings.require_approval else 'approved',
            facebook_text=result.get('facebook_text', ''),
            instagram_text=result.get('instagram_text', ''),
            image_url=image_url or '',
            featured_product=product,
            target_facebook=auto_settings.post_to_facebook,
            target_instagram=auto_settings.post_to_instagram,
            scheduled_for=scheduled_for,
            ai_model_used=self.model,
        )

        return post

    def _call_openai(self, context: dict) -> dict:
        """Call OpenAI to generate post content."""
        product_info = ""
        if 'product' in context:
            p = context['product']
            product_info = f"""
Product to feature:
- Name: {p['name']}
- Description: {p.get('description', 'N/A')}
- Price: {p.get('price', 'N/A')}
"""

        previous = ""
        if context.get('previous_posts'):
            previous = "\nRecent posts (avoid repetition):\n" + "\n".join(
                f"- {p[:100]}..." for p in context['previous_posts'] if p
            )

        lang_code = context.get('language', 'en')
        lang_name = LANGUAGE_NAMES.get(lang_code, lang_code)

        prompt = f"""Generate a social media marketing post for a business.

Company info: {context.get('company_description', 'A modern business')}
Tone: {context.get('tone', 'professional')}
Language: {lang_name} (code: {lang_code})
{product_info}
{previous}

Return a JSON object with these fields:
- "facebook_text": The post text optimized for Facebook (no hashtags, engaging, under 500 chars)
- "instagram_text": The post text optimized for Instagram (include 5-10 relevant hashtags at the end, under 2200 chars)
- "image_prompt": A short DALL-E prompt for generating a matching marketing image (only if no product image is used)

Important:
- You MUST write ALL text content in {lang_name}. Use the {lang_name} script and alphabet. Do NOT use any other language.
- Make it engaging and on-brand
- Do NOT repeat content from recent posts
- Facebook: conversational, call-to-action
- Instagram: visually descriptive, with hashtags
- The image_prompt should always be in English (for DALL-E)

Respond ONLY with valid JSON, no markdown."""

        models_to_try = [self.model] + [m for m in FALLBACK_MODELS if m != self.model]
        messages = [
            {"role": "system", "content": "You are a social media marketing expert. Always respond with valid JSON only."},
            {"role": "user", "content": prompt},
        ]

        last_error = None
        for model in models_to_try:
            try:
                response = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.8,
                    max_tokens=1000,
                )

                if model != self.model:
                    logger.info(f"OpenAI: fell back from {self.model} to {model} successfully")

                content = response.choices[0].message.content.strip()
                # Strip markdown code fences if present
                if content.startswith('```'):
                    content = content.split('\n', 1)[1] if '\n' in content else content[3:]
                    if content.endswith('```'):
                        content = content[:-3]
                    content = content.strip()

                result = json.loads(content)
                return result

            except PermissionDeniedError as e:
                logger.warning(f"OpenAI model {model} not accessible: {e}. Trying fallback...")
                last_error = e
                continue
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse OpenAI response as JSON: {e}")
                return {
                    'facebook_text': 'Check out our latest offerings!',
                    'instagram_text': 'Check out our latest offerings! #business #marketing',
                    'image_prompt': 'Modern business marketing image',
                }
            except Exception as e:
                logger.error(f"OpenAI API error: {e}")
                raise

        # All models failed with permission errors
        logger.error(f"All OpenAI models failed with permission errors. Last error: {last_error}")
        raise last_error
