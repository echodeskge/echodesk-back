"""
OpenAPI contract tests for the ticket / board endpoints.

The web dashboard and the mobile app generate typed clients from this
schema, so operation ids, response types and request bodies are part of the
API contract: renaming `boards_kanban_board_retrieve` or dropping its `Board`
response breaks their builds on the next regeneration.
"""
from amanati_crm.schema import TenantAwareSchemaGenerator
from tickets.tests.conftest import TicketTestCase


class TestTicketOpenApiContract(TicketTestCase):
    _schema = None

    @classmethod
    def schema(cls):
        if cls._schema is None:
            cls._schema = TenantAwareSchemaGenerator().get_schema(request=None, public=True)
        return cls._schema

    def resolve(self, ref):
        """'#/components/schemas/X' -> the X component."""
        name = ref.rsplit('/', 1)[-1]
        return self.schema()['components']['schemas'][name]

    def test_kanban_board_operation_keeps_name_and_board_response(self):
        op = self.schema()['paths']['/api/boards/{id}/kanban_board/']['get']
        self.assertEqual(op['operationId'], 'boards_kanban_board_retrieve')
        content = op['responses']['200']['content']['application/json']['schema']
        self.assertEqual(content['$ref'], '#/components/schemas/Board')
        self.assertIn('lite', [p['name'] for p in op['parameters']])

    def test_id_path_parameters_stay_strings(self):
        """The web client calls e.g. ticketsRetrieve(id: string); keep that type."""
        paths = self.schema()['paths']
        for path in (
            '/api/tickets/{id}/',
            '/api/tickets/{id}/move_to_column/',
            '/api/boards/{id}/',
            '/api/boards/{id}/kanban_board/',
        ):
            for method, op in paths[path].items():
                id_param = next(p for p in op['parameters'] if p['in'] == 'path' and p['name'] == 'id')
                self.assertEqual(id_param['schema']['type'], 'string', f'{method.upper()} {path}')

    def test_ticket_list_exposes_board_filter(self):
        op = self.schema()['paths']['/api/tickets/']['get']
        names = {p['name'] for p in op['parameters']}
        self.assertIn('board', names)
        self.assertTrue({'priority', 'column', 'assigned_to', 'tags'} <= names)

    def test_move_and_reorder_have_typed_request_bodies(self):
        paths = self.schema()['paths']
        move = paths['/api/tickets/{id}/move_to_column/']['patch']
        body = move['requestBody']['content']['application/json']['schema']
        props = self.resolve(body['$ref'])['properties']
        self.assertEqual(props['column_id']['type'], 'integer')
        self.assertEqual(props['position_in_column']['minimum'], 0)
        self.assertEqual(
            move['responses']['200']['content']['application/json']['schema']['$ref'],
            '#/components/schemas/Ticket',
        )

        # PATCH bodies are emitted as a `Patched...` component (nothing required)
        reorder = paths['/api/tickets/{id}/reorder_in_column/']['patch']
        body = reorder['requestBody']['content']['application/json']['schema']
        component = self.resolve(body['$ref'])
        self.assertEqual(component['properties']['position_in_column']['type'], 'integer')
        self.assertEqual(component['properties']['position_in_column']['minimum'], 0)
