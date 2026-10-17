import logging
import uuid
import requests as http_requests
from io import BytesIO
from django.conf import settings
from openai import OpenAI

from .ai_providers import AINotConfiguredError

logger = logging.getLogger(__name__)


class ImageService:
    def __init__(self):
        api_key = getattr(settings, 'OPENAI_API_KEY', '')
        if not api_key:
            raise AINotConfiguredError("OPENAI_API_KEY is not configured")
        self.client = OpenAI(api_key=api_key)

    def generate_image(self, prompt: str) -> str:
        """Generate an image with DALL-E 3, upload to DO Spaces, return public URL."""
        try:
            response = self.client.images.generate(
                model="dall-e-3",
                prompt=prompt,
                size="1024x1024",
                quality="standard",
                n=1,
            )

            image_url = response.data[0].url

            # Download the image
            img_response = http_requests.get(image_url, timeout=30)
            img_response.raise_for_status()

            # Upload to DigitalOcean Spaces
            return self._upload_to_spaces(img_response.content)

        except Exception as e:
            logger.error(f"Image generation/upload failed: {e}")
            raise

    def _upload_to_spaces(self, image_bytes: bytes) -> str:
        """Upload image bytes to DigitalOcean Spaces and return public URL."""
        import boto3

        bucket_name = getattr(settings, 'AWS_STORAGE_BUCKET_NAME', 'echodesk-spaces')
        endpoint_url = getattr(settings, 'AWS_S3_ENDPOINT_URL', 'https://fra1.digitaloceanspaces.com')
        region = getattr(settings, 'AWS_S3_REGION_NAME', 'fra1')
        access_key = getattr(settings, 'AWS_ACCESS_KEY_ID', '')
        secret_key = getattr(settings, 'AWS_SECRET_ACCESS_KEY', '')

        if not access_key or not secret_key:
            raise ValueError("DigitalOcean Spaces credentials not configured")

        s3_client = boto3.client(
            's3',
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

        filename = f"media/auto-posts/{uuid.uuid4().hex}.png"

        s3_client.put_object(
            Bucket=bucket_name,
            Key=filename,
            Body=image_bytes,
            ContentType='image/png',
            ACL='public-read',
        )

        custom_domain = getattr(settings, 'AWS_S3_CUSTOM_DOMAIN', f'{bucket_name}.{region}.digitaloceanspaces.com')
        return f"https://{custom_domain}/{filename}"
