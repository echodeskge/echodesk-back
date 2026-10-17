"""
OpenAPI contract for the settings / ticket-admin endpoints the mobile app
generates its client from (tenant settings, notification preferences,
columns, item lists, ticket forms, board Telegram connection).
"""
from amanati_crm.schema import TenantAwareSchemaGenerator
from tickets.tests.conftest import TicketTestCase


class TestAdminOpenApiContract(TicketTestCase):
    _schema = None

    @classmethod
    def schema(cls):
        if cls._schema is None:
            cls._schema = TenantAwareSchemaGenerator().get_schema(request=None, public=True)
        return cls._schema

    def component(self, name):
        return self.schema()['components']['schemas'][name]

    def body(self, path, method, status='200', key='responses'):
        op = self.schema()['paths'][path][method]
        if key == 'requestBody':
            return op['requestBody']['content']['application/json']['schema']
        return op['responses'][status]['content']['application/json']['schema']

    def test_method_fields_are_typed(self):
        board = self.component('Board')['properties']
        self.assertEqual(board['payment_summary']['allOf'][0]['$ref'], '#/components/schemas/BoardPaymentSummary')
        self.assertTrue(board['payment_summary']['nullable'])
        item_list = self.component('ItemList')['properties']
        self.assertEqual(item_list['items_count']['type'], 'integer')
        self.assertEqual(item_list['root_items']['items']['$ref'], '#/components/schemas/ListItem')
        self.assertEqual(self.component('ItemListMinimal')['properties']['items_count']['type'], 'integer')
        list_item = self.component('ListItem')['properties']
        self.assertEqual(list_item['children']['items']['$ref'], '#/components/schemas/ListItem')
        self.assertIn('item_list', list_item)
        form = self.component('TicketForm')['properties']
        self.assertEqual(form['child_forms_count']['type'], 'integer')
        self.assertEqual(form['submissions_count']['type'], 'integer')
        self.assertTrue(form['parent_form']['nullable'])
        minimal = self.component('TicketFormMinimal')['properties']
        self.assertEqual(minimal['child_forms']['items']['$ref'], '#/components/schemas/TicketFormRef')
        self.assertTrue(minimal['parent_form']['nullable'])
        self.assertEqual(minimal['submissions_count']['type'], 'integer')
        submission = self.component('TicketFormSubmission')['properties']
        self.assertTrue(submission['ticket_data']['nullable'])

    def test_column_create_and_update_return_id(self):
        self.assertIn('id', self.component('TicketColumnCreate')['properties'])
        self.assertIn('id', self.component('TicketColumnUpdate')['properties'])
        self.assertNotIn('id', self.component('TicketColumnCreateRequest')['properties'])

    def test_column_reorder_contract(self):
        req = self.body('/api/columns/{id}/reorder/', 'post', key='requestBody')
        self.assertEqual(req['$ref'], '#/components/schemas/TicketColumnReorderRequest')
        resp = self.body('/api/columns/{id}/reorder/', 'post')
        props = self.component(resp['$ref'].rsplit('/', 1)[-1])['properties']
        self.assertEqual(props['columns']['items']['$ref'], '#/components/schemas/TicketColumn')
        self.assertIn('message', props)

    def test_tenant_settings_get_and_patch(self):
        paths = self.schema()['paths']['/api/tenant-settings/']
        self.assertEqual(paths['get']['operationId'], 'tenant_settings_get')
        self.assertEqual(paths['patch']['operationId'], 'tenant_settings_update')
        props = self.component('TenantSettings')['properties']
        self.assertTrue({'logo', 'company_name', 'min_users_per_ticket',
                         'only_superadmin_can_delete_tickets', 'preferred_language'} <= set(props))
        patch = self.body('/api/tenant-settings/', 'patch', key='requestBody')
        patch_props = self.component(patch['$ref'].rsplit('/', 1)[-1])['properties']
        self.assertNotIn('logo', patch_props)
        self.assertNotIn('company_name', patch_props)

    def test_notification_preferences_typed(self):
        listing = self.body('/api/notification-preferences/', 'get')
        self.assertEqual(listing['items']['$ref'], '#/components/schemas/NotificationPreference')
        bulk_req = self.body('/api/notification-preferences/bulk/', 'put', key='requestBody')
        self.assertEqual(bulk_req['type'], 'array')
        bulk_resp = self.body('/api/notification-preferences/bulk/', 'put')
        self.assertEqual(bulk_resp['items']['$ref'], '#/components/schemas/NotificationPreference')
        types = self.body('/api/notification-preferences/types/', 'get')
        self.assertEqual(types['items']['$ref'], '#/components/schemas/NotificationPreferenceType')

    def test_profile_documents_permissions_list(self):
        schema = self.body('/api/auth/profile/', 'get')
        self.assertEqual(schema['properties']['permissions_list']['type'], 'array')
        self.assertEqual(schema['properties']['all_permissions']['type'], 'array')

    def test_board_actions_typed(self):
        paths = self.schema()['paths']
        self.assertNotIn('requestBody', paths['/api/boards/{id}/set_default/']['post'])
        tg = paths['/api/boards/{id}/telegram-connection/']
        self.assertEqual(
            tg['get']['responses']['200']['content']['application/json']['schema']['$ref'],
            '#/components/schemas/BoardTelegramConnection',
        )
        self.assertIn('201', tg['post']['responses'])

    def test_list_filters_documented_and_optional(self):
        paths = self.schema()['paths']
        for path, names in (
            ('/api/item-lists/', {'is_active'}),
            ('/api/ticket-forms/', {'is_active', 'is_default'}),
            ('/api/list-items/', {'item_list_id', 'root_only'}),
            ('/api/form-submissions/by_form/', {'form_id'}),
            ('/api/form-submissions/by_ticket/', {'ticket_id'}),
        ):
            params = {p['name']: p for p in paths[path]['get'].get('parameters', [])}
            self.assertTrue(names <= set(params), path)
            for name in names:
                self.assertFalse(params[name].get('required', False), f'{path} {name}')
