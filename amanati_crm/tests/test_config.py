"""Tests for amanati_crm project configuration: installed apps, middleware, DRF, CORS."""
from django.test import SimpleTestCase, override_settings
from django.conf import settings


class TestInstalledApps(SimpleTestCase):
    """Verify all required apps are present in INSTALLED_APPS."""

    def test_tenant_schemas_present(self):
        self.assertIn('tenant_schemas', settings.INSTALLED_APPS)

    def test_rest_framework_present(self):
        self.assertIn('rest_framework', settings.INSTALLED_APPS)

    def test_rest_framework_authtoken_present(self):
        self.assertIn('rest_framework.authtoken', settings.INSTALLED_APPS)

    def test_drf_spectacular_present(self):
        self.assertIn('drf_spectacular', settings.INSTALLED_APPS)

    def test_corsheaders_present(self):
        self.assertIn('corsheaders', settings.INSTALLED_APPS)

    def test_channels_present(self):
        self.assertIn('channels', settings.INSTALLED_APPS)

    def test_storages_present(self):
        self.assertIn('storages', settings.INSTALLED_APPS)

    def test_tenants_in_shared_apps(self):
        self.assertIn('tenants', settings.SHARED_APPS)

    def test_users_in_shared_apps(self):
        self.assertIn('users', settings.SHARED_APPS)

    def test_crm_in_shared_apps(self):
        self.assertIn('crm', settings.SHARED_APPS)

    def test_invoices_in_tenant_apps(self):
        self.assertIn('invoices', settings.TENANT_APPS)

    def test_tickets_in_tenant_apps(self):
        self.assertIn('tickets', settings.TENANT_APPS)

    def test_ecommerce_crm_in_tenant_apps(self):
        self.assertIn('ecommerce_crm', settings.TENANT_APPS)

    def test_booking_management_in_tenant_apps(self):
        self.assertIn('booking_management', settings.TENANT_APPS)

    def test_leave_management_in_tenant_apps(self):
        self.assertIn('leave_management', settings.TENANT_APPS)

    def test_social_integrations_in_tenant_apps(self):
        self.assertIn('social_integrations', settings.TENANT_APPS)

    def test_notifications_in_tenant_apps(self):
        self.assertIn('notifications', settings.TENANT_APPS)

    def test_django_filters_in_tenant_apps(self):
        self.assertIn('django_filters', settings.TENANT_APPS)

    def test_help_center_in_shared_apps(self):
        self.assertIn('help_center', settings.SHARED_APPS)


class TestMiddlewareOrder(SimpleTestCase):
    """Verify middleware is in the correct order."""

    def test_bot_blocker_before_tenant(self):
        mw = settings.MIDDLEWARE
        bot_idx = mw.index('amanati_crm.middleware.BotBlockerMiddleware')
        tenant_idx = mw.index('amanati_crm.middleware.EchoDeskTenantMiddleware')
        self.assertLess(bot_idx, tenant_idx)

    def test_tenant_before_subscription(self):
        mw = settings.MIDDLEWARE
        tenant_idx = mw.index('amanati_crm.middleware.EchoDeskTenantMiddleware')
        sub_idx = mw.index('tenants.subscription_middleware.SubscriptionMiddleware')
        self.assertLess(tenant_idx, sub_idx)

    def test_session_before_auth(self):
        mw = settings.MIDDLEWARE
        session_idx = mw.index('django.contrib.sessions.middleware.SessionMiddleware')
        auth_idx = mw.index('django.contrib.auth.middleware.AuthenticationMiddleware')
        self.assertLess(session_idx, auth_idx)

    def test_cors_before_common(self):
        mw = settings.MIDDLEWARE
        cors_idx = mw.index('corsheaders.middleware.CorsMiddleware')
        common_idx = mw.index('django.middleware.common.CommonMiddleware')
        self.assertLess(cors_idx, common_idx)

    def test_security_middleware_present(self):
        self.assertIn(
            'django.middleware.security.SecurityMiddleware', settings.MIDDLEWARE,
        )

    def test_whitenoise_present(self):
        self.assertIn(
            'whitenoise.middleware.WhiteNoiseMiddleware', settings.MIDDLEWARE,
        )

    def test_ip_whitelist_after_auth(self):
        mw = settings.MIDDLEWARE
        auth_idx = mw.index('django.contrib.auth.middleware.AuthenticationMiddleware')
        ip_idx = mw.index('tenants.ip_whitelist_middleware.IPWhitelistMiddleware')
        self.assertLess(auth_idx, ip_idx)


class TestDatabaseConfig(SimpleTestCase):
    """Verify database settings are valid."""

    def test_default_database_exists(self):
        self.assertIn('default', settings.DATABASES)

    def test_database_engine_is_tenant_schemas(self):
        engine = settings.DATABASES['default']['ENGINE']
        self.assertEqual(engine, 'tenant_schemas.postgresql_backend')

    def test_database_routers_configured(self):
        self.assertIn(
            'tenant_schemas.routers.TenantSyncRouter',
            settings.DATABASE_ROUTERS,
        )

    def test_tenant_model_configured(self):
        self.assertEqual(settings.TENANT_MODEL, 'tenants.Tenant')

    def test_auth_user_model_configured(self):
        self.assertEqual(settings.AUTH_USER_MODEL, 'users.User')


class TestRestFrameworkConfig(SimpleTestCase):
    """Verify DRF settings are present and correct."""

    def test_rest_framework_setting_exists(self):
        self.assertTrue(hasattr(settings, 'REST_FRAMEWORK'))

    def test_jwt_authentication_configured(self):
        auth_classes = settings.REST_FRAMEWORK.get('DEFAULT_AUTHENTICATION_CLASSES', [])
        self.assertIn(
            'rest_framework_simplejwt.authentication.JWTAuthentication',
            auth_classes,
        )

    def test_session_authentication_configured(self):
        auth_classes = settings.REST_FRAMEWORK.get('DEFAULT_AUTHENTICATION_CLASSES', [])
        self.assertIn(
            'rest_framework.authentication.SessionAuthentication',
            auth_classes,
        )

    def test_token_authentication_configured(self):
        auth_classes = settings.REST_FRAMEWORK.get('DEFAULT_AUTHENTICATION_CLASSES', [])
        # Subclass of DRF's TokenAuthentication that also accepts per-device tokens.
        self.assertIn(
            'users.authentication.MultiTokenAuthentication',
            auth_classes,
        )

    def test_default_permission_is_authenticated(self):
        perms = settings.REST_FRAMEWORK.get('DEFAULT_PERMISSION_CLASSES', [])
        self.assertIn('rest_framework.permissions.IsAuthenticated', perms)

    def test_filter_backends_configured(self):
        backends = settings.REST_FRAMEWORK.get('DEFAULT_FILTER_BACKENDS', [])
        self.assertIn('django_filters.rest_framework.DjangoFilterBackend', backends)
        self.assertIn('rest_framework.filters.SearchFilter', backends)
        self.assertIn('rest_framework.filters.OrderingFilter', backends)

    def test_schema_class_is_spectacular(self):
        schema = settings.REST_FRAMEWORK.get('DEFAULT_SCHEMA_CLASS', '')
        self.assertEqual(schema, 'drf_spectacular.openapi.AutoSchema')

    def test_pagination_configured(self):
        pagination = settings.REST_FRAMEWORK.get('DEFAULT_PAGINATION_CLASS', '')
        self.assertTrue(len(pagination) > 0)

    def test_throttle_rates_configured(self):
        rates = settings.REST_FRAMEWORK.get('DEFAULT_THROTTLE_RATES', {})
        self.assertIn('auth', rates)


class TestCORSConfig(SimpleTestCase):
    """Verify CORS settings are configured for multi-tenant operation."""

    def test_cors_allow_all_origins_false(self):
        self.assertFalse(settings.CORS_ALLOW_ALL_ORIGINS)

    def test_cors_allowed_origins_includes_localhost(self):
        self.assertIn('http://localhost:3000', settings.CORS_ALLOWED_ORIGINS)

    def test_cors_allow_credentials(self):
        self.assertTrue(settings.CORS_ALLOW_CREDENTIALS)

    def test_cors_allowed_methods(self):
        methods = settings.CORS_ALLOWED_METHODS
        for method in ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']:
            self.assertIn(method, methods)

    def test_cors_allowed_headers_includes_authorization(self):
        self.assertIn('authorization', settings.CORS_ALLOWED_HEADERS)

    def test_cors_allowed_headers_includes_content_type(self):
        self.assertIn('content-type', settings.CORS_ALLOWED_HEADERS)

    def test_cors_allowed_headers_includes_tenant_headers(self):
        self.assertIn('x-tenant-subdomain', settings.CORS_ALLOWED_HEADERS)
        self.assertIn('x-tenant-domain', settings.CORS_ALLOWED_HEADERS)

    def test_cors_allowed_origin_regexes_exist(self):
        self.assertIsInstance(settings.CORS_ALLOWED_ORIGIN_REGEXES, list)
        self.assertGreater(len(settings.CORS_ALLOWED_ORIGIN_REGEXES), 0)

    def test_cors_preflight_max_age(self):
        self.assertEqual(settings.CORS_PREFLIGHT_MAX_AGE, 86400)


class TestSimpleJWTConfig(SimpleTestCase):
    """Verify Simple JWT settings are configured."""

    def test_simple_jwt_setting_exists(self):
        self.assertTrue(hasattr(settings, 'SIMPLE_JWT'))

    def test_access_token_lifetime(self):
        from datetime import timedelta
        self.assertEqual(
            settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'],
            timedelta(hours=1),
        )

    def test_refresh_token_lifetime(self):
        from datetime import timedelta
        self.assertEqual(
            settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'],
            timedelta(days=7),
        )

    def test_rotate_refresh_tokens(self):
        self.assertTrue(settings.SIMPLE_JWT['ROTATE_REFRESH_TOKENS'])

    def test_blacklist_after_rotation(self):
        self.assertTrue(settings.SIMPLE_JWT['BLACKLIST_AFTER_ROTATION'])

    def test_auth_header_type(self):
        self.assertIn('Bearer', settings.SIMPLE_JWT['AUTH_HEADER_TYPES'])


class TestSpectacularConfig(SimpleTestCase):
    """Verify drf-spectacular settings."""

    def test_spectacular_settings_exist(self):
        self.assertTrue(hasattr(settings, 'SPECTACULAR_SETTINGS'))

    def test_api_title(self):
        self.assertEqual(settings.SPECTACULAR_SETTINGS['TITLE'], 'EchoDesk API')

    def test_schema_path_prefix(self):
        self.assertEqual(settings.SPECTACULAR_SETTINGS['SCHEMA_PATH_PREFIX'], '/api')

    def test_custom_schema_class(self):
        self.assertEqual(
            settings.SPECTACULAR_SETTINGS['DEFAULT_SCHEMA_CLASS'],
            'amanati_crm.schema.FeatureAwareAutoSchema',
        )

    def test_custom_generator_class(self):
        self.assertEqual(
            settings.SPECTACULAR_SETTINGS['DEFAULT_GENERATOR_CLASS'],
            'amanati_crm.schema.TenantAwareSchemaGenerator',
        )


class TestCeleryConfig(SimpleTestCase):
    """Verify Celery configuration."""

    def test_celery_broker_url(self):
        self.assertTrue(hasattr(settings, 'CELERY_BROKER_URL'))

    def test_celery_result_backend(self):
        self.assertTrue(hasattr(settings, 'CELERY_RESULT_BACKEND'))

    def test_celery_task_serializer_json(self):
        self.assertEqual(settings.CELERY_TASK_SERIALIZER, 'json')

    def test_celery_beat_schedule_exists(self):
        self.assertTrue(hasattr(settings, 'CELERY_BEAT_SCHEDULE'))
        self.assertIsInstance(settings.CELERY_BEAT_SCHEDULE, dict)

    def test_celery_timezone(self):
        self.assertEqual(settings.CELERY_TIMEZONE, 'UTC')


class TestChannelLayersConfig(SimpleTestCase):
    """Verify channel layers configuration."""

    def test_channel_layers_configured(self):
        self.assertTrue(hasattr(settings, 'CHANNEL_LAYERS'))
        self.assertIn('default', settings.CHANNEL_LAYERS)

    def test_asgi_application_configured(self):
        self.assertEqual(settings.ASGI_APPLICATION, 'amanati_crm.asgi.application')


class TestURLConfig(SimpleTestCase):
    """Verify URL configuration."""

    def test_root_urlconf(self):
        self.assertEqual(settings.ROOT_URLCONF, 'amanati_crm.urls')

    def test_public_schema_urlconf(self):
        self.assertEqual(settings.PUBLIC_SCHEMA_URLCONF, 'amanati_crm.urls_public')


class TestStorageConfig(SimpleTestCase):
    """Verify file storage configuration."""

    def test_default_file_storage(self):
        self.assertEqual(
            settings.DEFAULT_FILE_STORAGE,
            'storages.backends.s3boto3.S3Boto3Storage',
        )

    def test_static_files_storage(self):
        self.assertEqual(
            settings.STATICFILES_STORAGE,
            'whitenoise.storage.CompressedManifestStaticFilesStorage',
        )
