"""
Django settings for amanati_crm project.
"""

import os
import logging
from pathlib import Path
from decouple import config
import sentry_sdk

# Initialize Sentry for error tracking and performance monitoring
SENTRY_DSN = config('SENTRY_DSN', default='')
sentry_sdk.init(
    dsn=SENTRY_DSN,
    send_default_pii=False,  # Don't send PII to Sentry (GDPR)
    traces_sample_rate=0.2,  # Sample 20% of transactions (was 100%)
    profile_session_sample_rate=0.1,  # Sample 10% of profiles (was 100%)
    # Set profile_lifecycle to "trace" to automatically run the profiler on when there is an active transaction
    profile_lifecycle="trace",
    # Environment tag
    environment=config('SENTRY_ENVIRONMENT', default='production'),
)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY')  # Required - no default for security

# Fernet key for encrypting per-tenant PBX credentials (PbxServer.*_password).
# Generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
# Default is derived deterministically from SECRET_KEY so dev environments
# work without extra setup; production MUST set an explicit FERNET_KEY.
def _default_fernet_key(secret: str) -> str:
    import base64, hashlib
    digest = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()

FERNET_KEY = config('FERNET_KEY', default=_default_fernet_key(SECRET_KEY))
FERNET_KEYS_FALLBACK = [k for k in config('FERNET_KEYS_FALLBACK', default='').split(',') if k]

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config('DEBUG', default=False, cast=bool)

# Main domain configuration
MAIN_DOMAIN = config('MAIN_DOMAIN', default='echodesk.ge')
API_DOMAIN = config('API_DOMAIN', default='api.echodesk.ge')

SERVER_IP = config('SERVER_IP', default='')

ALLOWED_HOSTS = [
    MAIN_DOMAIN,  # Main domain for public schema
    f'.{MAIN_DOMAIN}',  # Wildcard for tenant frontend subdomains
    API_DOMAIN,  # API domain
    f'.{API_DOMAIN}',  # Wildcard for tenant API subdomains
    '.ondigitalocean.app',  # DigitalOcean app platform
    '.vercel.app',  # Vercel hosting
    'localhost',
    '127.0.0.1',
]

# Add server IP if configured (prevents DisallowedHost errors from direct IP access)
if SERVER_IP:
    ALLOWED_HOSTS.append(SERVER_IP)

# CSRF trusted origins for cross-origin requests
CSRF_TRUSTED_ORIGINS = [
    f'https://{MAIN_DOMAIN}',
    f'https://*.{MAIN_DOMAIN}',
    f'https://{API_DOMAIN}',
    f'https://*.{API_DOMAIN}',
    'https://*.ondigitalocean.app',
    'https://*.vercel.app',
]

# Shared applications - available to all tenants
SHARED_APPS = [
    'tenant_schemas',  # mandatory, should always be before any django app
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.admin',
    'rest_framework',
    'rest_framework.authtoken',
    'drf_spectacular',
    'corsheaders',
    'channels',  # Add channels for WebSocket support
    'storages',  # For DigitalOcean Spaces file storage

    # Shared apps
    'tenants',  # This must be in SHARED_APPS
    'users',    # Add users to shared apps so public schema gets the table
    'crm',      # Move CRM to shared apps so it's available in public schema too
    'help_center',  # Help center and documentation - global content
    'blog',     # Marketing-site blog + AI-drafted posts — public schema only
    'landing_pages',  # Marketing-site landing pages + AI-drafted — public schema only
    'marketing',  # Marketing-site testimonials + contact form + newsletter — public schema only
    'widget_registry',  # Embeddable chat widget tokens + config — public schema only
    'asterisk_state',  # Shadow models for the Asterisk realtime DB (routed to ``asterisk`` alias)
]

# Tenant-specific applications
TENANT_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.admin',
    'rest_framework',
    'rest_framework.authtoken',
    'rest_framework_simplejwt.token_blacklist',
    'drf_spectacular',
    'django_filters',
    'channels',  # Add channels for WebSocket support

    # Tenant-specific apps
    'users',    # Only available in tenant schemas
    'crm',      # Keep in tenant apps too for backwards compatibility
    'tickets',
    'social_integrations',
    'notifications',
    'ecommerce_crm',  # E-commerce product management
    'booking_management',  # Booking management for service-based businesses
    'leave_management',  # Employee leave and absence management
    'invoices',  # Invoice management and generation
]

INSTALLED_APPS = list(SHARED_APPS) + [app for app in TENANT_APPS if app not in SHARED_APPS]

# N+1 query detection (only in DEBUG mode)
if DEBUG:
    INSTALLED_APPS += ['nplusone.ext.django']

# Tenant model
TENANT_MODEL = "tenants.Tenant"

MIDDLEWARE = [
    'amanati_crm.middleware.BotBlockerMiddleware',  # Block bots/scanners early (before CommonMiddleware)
    'amanati_crm.middleware.WidgetPublicCorsMiddleware',  # Open CORS on /api/widget/public/* (arbitrary tenant origins)
    'amanati_crm.middleware.EcommerceClientCustomDomainCorsMiddleware',  # Open CORS on /api/ecommerce/* for verified TenantDomain custom domains
    'amanati_crm.middleware.EchoDeskTenantMiddleware',  # Custom tenant middleware (must be first)
    'tenants.subscription_middleware.SubscriptionMiddleware',  # Subscription feature middleware
    'amanati_crm.debug_middleware.TransactionDebugMiddleware',  # Debug transaction errors
    'amanati_crm.middleware.RequestLoggingMiddleware',  # Custom request logging middleware
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # Static file serving
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'tenants.ip_whitelist_middleware.IPWhitelistMiddleware',  # IP whitelist enforcement (after auth)
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# N+1 query detection (only in DEBUG mode)
if DEBUG:
    MIDDLEWARE.insert(2, 'nplusone.ext.django.NPlusOneMiddleware')
    NPLUSONE_RAISE = False  # Log warnings instead of raising exceptions
    NPLUSONE_LOGGER = logging.getLogger('nplusone')

ROOT_URLCONF = 'amanati_crm.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'amanati_crm.wsgi.application'

# Database
DATABASES = {
    'default': {
        'ENGINE': 'tenant_schemas.postgresql_backend',
        'NAME': config('DB_NAME', default='echodesk_db'),
        'USER': config('DB_USER', default='postgres'),
        'PASSWORD': config('DB_PASSWORD', default='password'),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
        'OPTIONS': {
            'sslmode': config('DB_SSLMODE', default='require'),
        } if config('DB_SSLMODE', default=None) else {},
    }
}

# Asterisk realtime DB (ARA) — Phase 2 (BYO Asterisk).
#
# There is NO static ``DATABASES['asterisk']`` alias anymore. Each tenant
# registers their own Asterisk via :class:`crm.models.PbxServer`, and the
# sync layer dynamically creates a per-tenant alias (``asterisk_{schema}``)
# via :func:`crm.asterisk_db.register_pbx_alias`.
#
# ``ASTERISK_SYNC_ENABLED`` used to be a global env flag. Now sync is
# always "enabled" at the module level — whether anything actually writes
# is a per-tenant decision based on PbxServer.status. The sync service
# short-circuits when no active PbxServer exists for the current tenant.
ASTERISK_SYNC_ENABLED = True

# Router order matters: the asterisk router short-circuits for its own app
# label, then the tenant-schemas router handles everything else.
DATABASE_ROUTERS = (
    'amanati_crm.db_routers.AsteriskStateRouter',
    'tenant_schemas.routers.TenantSyncRouter',
)

# Custom user model
# User model
AUTH_USER_MODEL = 'users.User'

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# WhiteNoise configuration for static files
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# DigitalOcean Spaces Configuration
AWS_ACCESS_KEY_ID = config('DO_SPACES_ACCESS_KEY', default='')
AWS_SECRET_ACCESS_KEY = config('DO_SPACES_SECRET_KEY', default='')
AWS_STORAGE_BUCKET_NAME = config('DO_SPACES_BUCKET_NAME', default='echodesk-spaces')
AWS_S3_ENDPOINT_URL = config('DO_SPACES_ENDPOINT_URL', default='https://fra1.digitaloceanspaces.com')
AWS_S3_REGION_NAME = config('DO_SPACES_REGION', default='fra1')
AWS_S3_OBJECT_PARAMETERS = {
    'CacheControl': 'max-age=86400',
}
AWS_LOCATION = 'media'
# IMPORTANT: Must be 'public-read' with HYPHEN, not 'public_read' with underscore
AWS_DEFAULT_ACL = 'public-read'
AWS_S3_CUSTOM_DOMAIN = config(
    'DO_SPACES_CUSTOM_DOMAIN',
    default=f'{AWS_STORAGE_BUCKET_NAME}.{AWS_S3_REGION_NAME}.digitaloceanspaces.com'
)

# Use DigitalOcean Spaces for media file storage
DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
MEDIA_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/{AWS_LOCATION}/'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Django REST Framework
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.TokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_PAGINATION_CLASS': 'ecommerce_crm.pagination.DynamicPageSizePagination',
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.ScopedRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'auth': '10/min',
    },
}

# Simple JWT Settings
from datetime import timedelta
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
}

# Spectacular settings
SPECTACULAR_SETTINGS = {
    'TITLE': 'EchoDesk API',
    'DESCRIPTION': 'Multi-tenant CRM system for EchoDesk',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'DEFAULT_SCHEMA_CLASS': 'amanati_crm.schema.FeatureAwareAutoSchema',
    'DEFAULT_GENERATOR_CLASS': 'amanati_crm.schema.TenantAwareSchemaGenerator',
    'PREPROCESSING_HOOKS': [
        'amanati_crm.schema.feature_based_preprocessing_hook',
    ],
    'SECURITY': [
        {
            'tokenAuth': [],
        }
    ],
    'COMPONENT_SPLIT_REQUEST': True,
    'SCHEMA_PATH_PREFIX': '/api',
}

# CORS settings - Allow wildcard subdomains for multi-tenant frontend
CORS_ALLOW_ALL_ORIGINS = False  # Use CORS_ALLOWED_ORIGIN_REGEXES instead

# If you want to be more restrictive in production, use this instead:
# CORS_ALLOW_ALL_ORIGINS = DEBUG  # Allow all in development, restrict in production

CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    f"https://{MAIN_DOMAIN}",
    f"http://{MAIN_DOMAIN}",
    f"https://{API_DOMAIN}",
    f"http://{API_DOMAIN}",
]

# Allow all subdomains with regex patterns
escaped_main_domain = MAIN_DOMAIN.replace('.', r'\.')
escaped_api_domain = API_DOMAIN.replace('.', r'\.')

CORS_ALLOWED_ORIGIN_REGEXES = [
    f"https://.*\\.{escaped_main_domain}",  # *.echodesk.ge
    f"http://.*\\.{escaped_main_domain}",   # *.echodesk.ge (dev)
    f"https://.*\\.{escaped_api_domain}",   # *.api.echodesk.ge
    f"http://.*\\.{escaped_api_domain}",    # *.api.echodesk.ge (dev)
    r"https://.*\.ondigitalocean\.app",      # DigitalOcean app domains
    r"http://.*\.localhost:3000",            # Local development subdomains
]

# CORS configuration for multi-tenant setup
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_ALL_HEADERS = True
CORS_ALLOWED_HEADERS = [
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
    'x-tenant-subdomain',
    'x-tenant-domain',
    'cache-control',
]

# Allow all HTTP methods
CORS_ALLOWED_METHODS = [
    'DELETE',
    'GET',
    'OPTIONS',
    'PATCH',
    'POST',
    'PUT',
]

# Preflight cache time
CORS_PREFLIGHT_MAX_AGE = 86400

# Tenant settings
PUBLIC_SCHEMA_URLCONF = 'amanati_crm.urls_public'

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'detailed': {
            'format': '{asctime} [{levelname}] {name}: {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'level': 'INFO',
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'debug.log',
            'formatter': 'detailed',
        },
        'request_file': {
            'level': 'INFO',
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'requests.log',
            'formatter': 'detailed',
        },
        'console': {
            'level': 'DEBUG' if DEBUG else 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
    },
    'filters': {
        'skip_disallowed_host': {
            '()': 'django.utils.log.CallbackFilter',
            'callback': lambda record: record.name != 'django.security.DisallowedHost',
        },
    },
    'loggers': {
        'django.security.DisallowedHost': {
            'handlers': ['file'],
            'level': 'WARNING',
            'propagate': False,  # Don't send to Sentry
        },
        'django': {
            'handlers': ['file', 'console'] if DEBUG else ['file'],
            'level': 'INFO',
            'propagate': True,
        },
        'django.request': {
            'handlers': ['request_file', 'console'] if DEBUG else ['request_file'],
            'level': 'INFO',
            'propagate': False,  # Don't duplicate in django logger
        },
        'django.server': {
            'handlers': ['file', 'console'] if DEBUG else ['file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.db.backends': {
            'handlers': ['console'],
            'level': 'WARNING',  # Only log warnings and errors, not all SQL queries
            'propagate': False,
        },
        'channels': {
            'handlers': ['console'],
            'level': 'INFO',  # Log WebSocket connection info
            'propagate': False,
        },
        'daphne': {
            'handlers': ['console'],
            'level': 'INFO',  # Log Daphne server info
            'propagate': False,
        },
    },
}

# Frontend Configuration
FRONTEND_BASE_URL = config('FRONTEND_BASE_URL', default='echodesk.ge')
REVALIDATION_SECRET = config('REVALIDATION_SECRET', default='')

# Optional: For advanced deployments
VERCEL_TOKEN = config('VERCEL_TOKEN', default='')
VERCEL_PROJECT_ID = config('VERCEL_PROJECT_ID', default='')

# Facebook Integration Settings
FACEBOOK_APP_ID = config('FACEBOOK_APP_ID', default='')
FACEBOOK_APP_SECRET = config('FACEBOOK_APP_SECRET', default='')
FACEBOOK_APP_VERSION = config('FACEBOOK_APP_VERSION', default='v23.0')

# OpenAI Configuration (for AI Auto-Posting)
OPENAI_API_KEY = config('OPENAI_API_KEY', default='')
OPENAI_MODEL = config('OPENAI_MODEL', default='gpt-4o')

# Anthropic — powers the blog-post AI drafting pipeline.
# The blog app reads these at request time; unset key ⇒ management
# command exits with a clear error instead of crashing silently.
ANTHROPIC_API_KEY = config('ANTHROPIC_API_KEY', default='')
BLOG_AI_MODEL = config('BLOG_AI_MODEL', default='claude-sonnet-4-6')
BLOG_DAILY_POST_LIMIT = config('BLOG_DAILY_POST_LIMIT', default=2, cast=int)
# When true, AI drafts go straight to status='published' instead of 'review'.
# Flip off (or unset) to restore the human-in-the-loop approval flow.
BLOG_AUTO_PUBLISH = config('BLOG_AUTO_PUBLISH', default=False, cast=bool)
# Landing-pages app uses the same pattern; shares ANTHROPIC_API_KEY, BLOG_AI_MODEL,
# and BLOG_DAILY_POST_LIMIT but has its own auto-publish toggle.
LANDING_AUTO_PUBLISH = config('LANDING_AUTO_PUBLISH', default=False, cast=bool)

# AI Companion (auto-answers + conversation summaries). Global kill switch —
# tenant-level enablement lives in social_integrations.AICompanionSettings.
AI_COMPANION_ENABLED = config('AI_COMPANION_ENABLED', default=True, cast=bool)

# TikTok Shop Partner Center Integration Settings
TIKTOK_SHOP_APP_KEY = config('TIKTOK_SHOP_APP_KEY', default='')
TIKTOK_SHOP_APP_SECRET = config('TIKTOK_SHOP_APP_SECRET', default='')
TIKTOK_SHOP_SERVICE_ID = config('TIKTOK_SHOP_SERVICE_ID', default='')
TIKTOK_SHOP_REDIRECT_URI = config('TIKTOK_SHOP_REDIRECT_URI', default='https://api.echodesk.ge/api/social/tiktok/oauth/callback/')

# Social Integration Configuration
SOCIAL_INTEGRATIONS = {
    'FACEBOOK_APP_ID': FACEBOOK_APP_ID,
    'FACEBOOK_APP_SECRET': FACEBOOK_APP_SECRET,
    'FACEBOOK_API_VERSION': FACEBOOK_APP_VERSION,
    'FACEBOOK_VERIFY_TOKEN': config('FACEBOOK_WEBHOOK_VERIFY_TOKEN', default=''),
    'FACEBOOK_SCOPES': [
        'business_management',  # Essential for accessing Pages and Business assets
        'pages_messaging',  # Read and send messages on behalf of pages
        'pages_show_list',  # Access list of pages
        'pages_read_engagement',  # Read page posts and comments
        'pages_manage_metadata',  # Access page metadata
        'public_profile',  # Basic profile information
        'email',  # Email address
    ],
    # TikTok Shop Settings
    'TIKTOK_SHOP_APP_KEY': TIKTOK_SHOP_APP_KEY,
    'TIKTOK_SHOP_APP_SECRET': TIKTOK_SHOP_APP_SECRET,
    'TIKTOK_SHOP_SERVICE_ID': TIKTOK_SHOP_SERVICE_ID,
    'TIKTOK_SHOP_REDIRECT_URI': TIKTOK_SHOP_REDIRECT_URI,
    # Telegram MTProto (platform-wide app credentials from my.telegram.org;
    # NOT the TELEGRAM_BOT_TOKEN used for deploy notifications)
    'TELEGRAM_API_ID': config('TELEGRAM_API_ID', default='', cast=str),
    'TELEGRAM_API_HASH': config('TELEGRAM_API_HASH', default=''),
}

# Internal URL of the telegram-worker service (Docker network)
TELEGRAM_WORKER_URL = config('TELEGRAM_WORKER_URL', default='http://telegram-worker:8010')

# ASGI Application for WebSocket support
ASGI_APPLICATION = 'amanati_crm.asgi.application'

# Channel Layers configuration for WebSocket support
# Password is included in the hosts tuple format if needed
redis_password = config('REDIS_PASSWORD', default=None)
redis_host = config('REDIS_HOST', default='127.0.0.1')
redis_port = config('REDIS_PORT', default=6379, cast=int)

if redis_password:
    # Format: rediss:// (with double 's' for SSL) for DigitalOcean managed Redis
    # channels_redis doesn't support ssl_cert_reqs in the URL, so we pass SSL
    # config as a dict with ssl.CERT_NONE for self-signed certs.
    import ssl as _ssl
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {
                "hosts": [{
                    "address": f'rediss://:{redis_password}@{redis_host}:{redis_port}/0',
                    "ssl_cert_reqs": _ssl.CERT_NONE,
                }],
            },
        },
    }
else:
    # Local development without password
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {
                "hosts": [(redis_host, redis_port)],
            },
        },
    }

# Cache configuration using Redis (DB 1 to avoid collision with Channels on DB 0)
if redis_password:
    cache_redis_url = f'rediss://:{redis_password}@{redis_host}:{redis_port}/1?ssl_cert_reqs=none'
else:
    cache_redis_url = f'redis://{redis_host}:{redis_port}/1'

CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': cache_redis_url,
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        },
        'KEY_PREFIX': 'ecommerce',
        'TIMEOUT': 300,  # 5 minutes default
    }
}

# VAPID Keys for Web Push Notifications.
# No hardcoded default for the private key — a committed key can be used to
# forge Web Push notifications to every user. Must be supplied via env.
VAPID_PRIVATE_KEY = config('VAPID_PRIVATE_KEY', default='')
VAPID_PUBLIC_KEY = config('VAPID_PUBLIC_KEY', default='')
VAPID_ADMIN_EMAIL = config('VAPID_ADMIN_EMAIL', default='mailto:info@echodesk.ge')

# Celery Configuration (Redis DB 2)
if redis_password:
    celery_redis_url = f'rediss://:{redis_password}@{redis_host}:{redis_port}/2?ssl_cert_reqs=none'
else:
    celery_redis_url = f'redis://{redis_host}:{redis_port}/2'

CELERY_BROKER_URL = celery_redis_url
CELERY_RESULT_BACKEND = celery_redis_url
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = 300
CELERY_TASK_TIME_LIMIT = 600

from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    # Payment tasks — owned by Celery beat again since 2026-08-12. They were
    # previously removed in favor of DO Functions cron triggers (to prevent
    # double-charging while both existed); those Functions died with the
    # DigitalOcean account in the June 2026 Hetzner migration, which silently
    # stopped all recurring billing. Schedules mirror the old DO cadence.
    # process_recurring_payments has a 25-day recent-bill guard, so a stray
    # double trigger cannot double-charge.
    'process-recurring-payments': {
        'task': 'tenants.tasks.process_recurring_payments',
        'schedule': crontab(hour=2, minute=0),
    },
    'check-subscription-status': {
        'task': 'tenants.tasks.check_subscription_status',
        'schedule': crontab(hour=3, minute=0),
    },
    'process-trial-expirations': {
        'task': 'tenants.tasks.process_trial_expirations',
        'schedule': crontab(hour=9, minute=0),
    },
    'process-payment-retries': {
        'task': 'tenants.tasks.process_payment_retries',
        'schedule': crontab(minute=30),  # hourly; only acts on due retry rows
    },
    'calculate-platform-metrics': {
        'task': 'tenants.tasks.calculate_platform_metrics',
        'schedule': crontab(minute=30, hour=0),
    },
    'sync-all-tenant-emails': {
        'task': 'social_integrations.tasks.sync_all_tenant_emails',
        'schedule': 300.0,  # every 5 minutes
    },
    'generate-daily-posts': {
        'task': 'social_integrations.tasks.generate_daily_posts',
        'schedule': crontab(minute=0),  # every hour
    },
    'publish-approved-posts': {
        'task': 'social_integrations.tasks.publish_approved_posts',
        'schedule': 300.0,  # every 5 minutes
    },
    # Booking management tasks
    'create-recurring-bookings': {
        'task': 'booking_management.tasks.create_recurring_bookings',
        'schedule': crontab(hour=0, minute=0),  # Daily at midnight
    },
    'send-booking-reminders': {
        'task': 'booking_management.tasks.send_booking_reminders',
        'schedule': crontab(hour=9, minute=0),  # Daily at 9 AM
    },
    'cancel-unpaid-bookings': {
        'task': 'booking_management.tasks.cancel_unpaid_bookings',
        'schedule': crontab(minute=0, hour='*/6'),  # Every 6 hours
    },
    # Ecommerce low stock check
    'check-low-stock': {
        'task': 'ecommerce_crm.tasks.check_low_stock_products',
        'schedule': crontab(hour=8, minute=0),  # Daily at 8 AM
    },
    # Restore stock reserved by abandoned/never-paid card orders.
    'cancel-unpaid-orders': {
        'task': 'ecommerce_crm.tasks.cancel_unpaid_orders',
        'schedule': crontab(minute=0, hour='*/6'),  # Every 6 hours
    },
    # Blog: draft pending topics into BlogPost(review) via Claude.
    # Daily at 06:00 UTC = 10:00 Tbilisi. Drafts BLOG_DAILY_POST_LIMIT posts.
    'generate-daily-blog-posts': {
        'task': 'blog.tasks.generate_daily_blog_posts',
        'schedule': crontab(hour=6, minute=0),
    },
    # Landing pages: draft pending topics into LandingPage(review) via Claude.
    # Daily at 07:00 UTC = 11:00 Tbilisi — one hour after the blog to keep
    # API spend evenly distributed. Draft count limited by BLOG_DAILY_POST_LIMIT.
    'generate-daily-landing-pages': {
        'task': 'landing_pages.tasks.generate_daily_landing_pages',
        'schedule': crontab(hour=7, minute=0),
    },
}

# Bank of Georgia (BOG) Payment Gateway Configuration
# Documentation: https://api.bog.ge/docs/en/payments/introduction
# Get credentials from BOG merchant portal
#
# TEST ENVIRONMENT (current default):
#   Auth: https://account-ob-test.bog.ge/auth/realms/bog-test/protocol/openid-connect/token
#   API: https://api-test.bog.ge/payments/v1
#
# PRODUCTION ENVIRONMENT:
#   Auth: https://oauth2.bog.ge/auth/realms/bog/protocol/openid-connect/token
#   API: https://api.bog.ge/payments/v1
# TEST ENVIRONMENT:
#   Auth: https://account-ob-test.bog.ge/auth/realms/bog-test/protocol/openid-connect/token
#   API: https://api-test.bog.ge/payments/v1

# BOG Payment Gateway Configuration
# All credentials must be provided via environment variables
BOG_CLIENT_ID = config('BOG_CLIENT_ID', default='')
BOG_CLIENT_SECRET = config('BOG_CLIENT_SECRET', default='')
BOG_AUTH_URL = config('BOG_AUTH_URL', default='https://oauth2.bog.ge/auth/realms/bog/protocol/openid-connect/token')
BOG_API_BASE_URL = config('BOG_API_BASE_URL', default='https://api.bog.ge/payments/v1')

# Cron Job Security Token (for scheduled task HTTP endpoints)
CRON_SECRET_TOKEN = config('CRON_SECRET_TOKEN', default='')

# PBX shared secret (Asterisk AGI authentication)
PBX_SHARED_SECRET = config('PBX_SHARED_SECRET', default='')

# Telegram Bot Configuration (for subscription notifications)
TELEGRAM_BOT_TOKEN = config('TELEGRAM_BOT_TOKEN', default='')
TELEGRAM_CHAT_ID = config('TELEGRAM_CHAT_ID', default='')

# Test Mode for Billing Intervals
# When True, subscriptions renew every 2 minutes instead of 30 days (for testing)
# PRODUCTION: Set TEST_BILLING_INTERVAL=false or remove from .env
# TEST: Set TEST_BILLING_INTERVAL=true in .env to enable test mode
TEST_BILLING_INTERVAL = config('TEST_BILLING_INTERVAL', default=False, cast=bool)

# Paddle Billing Configuration
# Docs: https://developer.paddle.com/api-reference/overview
# Sandbox: sandbox-api.paddle.com / Production: api.paddle.com
PADDLE_API_KEY = config('PADDLE_API_KEY', default='')
PADDLE_WEBHOOK_SECRET = config('PADDLE_WEBHOOK_SECRET', default='')
PADDLE_ENVIRONMENT = config('PADDLE_ENVIRONMENT', default='sandbox')  # 'sandbox' or 'production'
PADDLE_CLIENT_TOKEN = config('PADDLE_CLIENT_TOKEN', default='')  # Frontend token for Paddle.js

# SendGrid Email Configuration
SENDGRID_API_KEY = config('SENDGRID_API_KEY', default='')
SENDGRID_FROM_EMAIL = config('SENDGRID_FROM_EMAIL', default='noreply@email.echodesk.ge')
SENDGRID_FROM_NAME = config('SENDGRID_FROM_NAME', default='EchoDesk')

# Email Backend Configuration
# Use SendGrid SMTP or API for sending emails
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.sendgrid.net'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = 'apikey'  # This is always 'apikey' for SendGrid
EMAIL_HOST_PASSWORD = SENDGRID_API_KEY
DEFAULT_FROM_EMAIL = SENDGRID_FROM_EMAIL
SERVER_EMAIL = SENDGRID_FROM_EMAIL

# Marketing-site sales inbox — new contact-form submissions go here.
SALES_EMAIL = config('SALES_EMAIL', default='info@echodesk.ge')
SITE_URL = config('SITE_URL', default='https://echodesk.ge')

# Vercel Deployment Configuration
VERCEL_TOKEN = config('VERCEL_TOKEN', default='')
VERCEL_TEAM_ID = config('VERCEL_TEAM_ID', default='')
VERCEL_GITHUB_REPO = config('VERCEL_GITHUB_REPO', default='echodeskge/echodesk-frontend-template-one')

# Multi-tenant Ecommerce Vercel Project Configuration
# This is the shared project that serves all tenant storefronts via wildcard domain
VERCEL_SHARED_ECOMMERCE_PROJECT_ID = config('VERCEL_SHARED_ECOMMERCE_PROJECT_ID', default='')
VERCEL_SHARED_ECOMMERCE_PROJECT_NAME = config('VERCEL_SHARED_ECOMMERCE_PROJECT_NAME', default='echodesk-ecommerce')

# Netlify Deployment Configuration
NETLIFY_TOKEN = config('NETLIFY_TOKEN', default='')
NETLIFY_TEAM_SLUG = config('NETLIFY_TEAM_SLUG', default='')
NETLIFY_GITHUB_REPO = config('NETLIFY_GITHUB_REPO', default='')  # Uses VERCEL_GITHUB_REPO if not set
