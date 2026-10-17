"""
Enum component names the generated web / mobile clients depend on stay
stable (ENUM_NAME_OVERRIDES).
"""
from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator


class TestStableEnumNames(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schemas = SchemaGenerator().get_schema(request=None, public=True)['components']['schemas']

    def ref(self, component, field):
        prop = self.schemas[component]['properties'][field]
        ref = prop.get('$ref') or next(item['$ref'] for item in prop.get('allOf', prop.get('oneOf', [])) if '$ref' in item)
        return ref.rsplit('/', 1)[-1]

    def test_platform_enum_keeps_tenant_login_name(self):
        self.assertEqual(self.schemas['TenantLoginPlatformEnum']['enum'], ['web', 'ios', 'android'])
        self.assertEqual(self.ref('TenantLoginRequest', 'platform'), 'TenantLoginPlatformEnum')
        self.assertEqual(self.ref('AuthDevice', 'platform'), 'TenantLoginPlatformEnum')
        self.assertFalse([k for k in self.schemas if k.startswith('Platform') and k.endswith('Enum')
                          and self.schemas[k].get('enum') == ['web', 'ios', 'android']])

    def test_event_type_enums(self):
        self.assertIn('initiated', self.schemas['EventTypeEnum']['enum'])
        self.assertEqual(self.ref('CallEvent', 'event_type'), 'EventTypeEnum')
        self.assertNotIn('CallEventEventTypeEnum', self.schemas)
        self.assertEqual(self.ref('SecurityLog', 'event_type'), 'SecurityLogEventTypeEnum')

    def test_lockout_code_enum(self):
        self.assertNotIn('CodeEnum', self.schemas)
        self.assertEqual(
            self.schemas['IPWhitelistLockoutCodeEnum']['enum'],
            ['no_active_entries', 'would_lock_out', 'would_lock_out_superusers'],
        )
        self.assertEqual(self.ref('IPWhitelistLockoutError', 'code'), 'IPWhitelistLockoutCodeEnum')
