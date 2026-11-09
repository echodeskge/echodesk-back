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


# Parameter names, in order, of the operations whose generated-client
# signatures are positional (E10). Taken from schema-tickets-base.yml (the
# 0040-era schema). New filters on these operations must stay undocumented.
PINNED_PARAMETERS = {
    ('/api/tickets/', 'get'): [
        'assigned_groups', 'assigned_to', 'board', 'column', 'created_by', 'ordering',
        'page', 'page_size', 'priority', 'search', 'tags',
    ],
    ('/api/boards/', 'get'): ['ordering', 'page', 'page_size', 'search'],
    ('/api/columns/', 'get'): ['board', 'ordering', 'page', 'page_size'],
    ('/api/tags/', 'get'): ['ordering', 'page', 'page_size', 'search'],
    ('/api/boards/{id}/kanban_board/', 'get'): ['id', 'lite'],
}
PINNED_OPERATION_IDS = {
    ('/api/tickets/', 'get'): 'tickets_list',
    ('/api/boards/', 'get'): 'boards_list',
    ('/api/columns/', 'get'): 'columns_list',
    ('/api/tags/', 'get'): 'tags_list',
    ('/api/boards/{id}/kanban_board/', 'get'): 'boards_kanban_board_retrieve',
}


class TestPinnedListParameters(TicketTestCase):
    """E10: the redesign's filters (assignee, unassigned, priority__in,
    tags__in, due, due_before, due_after, is_closed, q, group, lite on boards)
    are accepted but never documented on the existing operations."""
    _schema = None
    schema = TestTicketOpenApiContract.__dict__['schema']
    resolve = TestTicketOpenApiContract.resolve

    def test_pinned_operations_keep_their_parameters_in_order(self):
        paths = self.schema()['paths']
        for (path, method), expected in PINNED_PARAMETERS.items():
            op = paths[path][method]
            self.assertEqual(op['operationId'], PINNED_OPERATION_IDS[(path, method)])
            self.assertEqual([p['name'] for p in op.get('parameters', [])], expected, path)

    def test_new_operations_are_documented(self):
        op_ids = {
            op['operationId']
            for ops in self.schema()['paths'].values()
            for op in ops.values() if isinstance(op, dict) and 'operationId' in op
        }
        for op_id in (
            'tickets_view_counts_retrieve', 'tickets_column_counts_retrieve', 'tickets_navigation_retrieve',
            'tickets_timer_retrieve', 'tickets_timer_start', 'tickets_timer_stop',
            'time_entries_current_retrieve', 'time_entries_current_stop',
        ):
            self.assertIn(op_id, op_ids)

    def test_new_operation_shapes(self):
        paths = self.schema()['paths']

        def props(path):
            ref = paths[path]['get']['responses']['200']['content']['application/json']['schema']['$ref']
            return self.resolve(ref)['properties']

        self.assertEqual(set(props('/api/tickets/view_counts/')), {'mine', 'unassigned', 'urgent', 'due_this_week'})
        self.assertEqual(set(props('/api/tickets/column_counts/')), {'counts'})
        self.assertEqual(set(props('/api/tickets/{id}/navigation/')), {'index', 'total', 'prev_id', 'next_id'})
        nav_id = next(p for p in paths['/api/tickets/{id}/navigation/']['get']['parameters'] if p['name'] == 'id')
        self.assertEqual(nav_id['schema']['type'], 'string')

    def test_new_operations_have_no_required_query_param_after_an_optional_one(self):
        """The generated TS clients (web and mobile, @gordela/api-generator)
        take path params, then query params in alphabetical order, as
        positional arguments. A required one after an optional one does not
        compile (TS1016), so new operations must not document that."""
        paths = self.schema()['paths']
        for path in ('/api/tickets/view_counts/', '/api/tickets/column_counts/',
                     '/api/tickets/{id}/navigation/'):
            query = sorted(
                (p for p in paths[path]['get'].get('parameters', []) if p['in'] == 'query'),
                key=lambda p: p['name'],
            )
            seen_optional = False
            for p in query:
                if p.get('required'):
                    self.assertFalse(seen_optional, f"{path}: required '{p['name']}' after an optional param")
                else:
                    seen_optional = True

    def test_comment_request_schema_still_has_user_id(self):
        body = self.schema()['paths']['/api/comments/']['post']['requestBody']
        ref = body['content']['application/json']['schema']['$ref']
        self.assertIn('user_id', self.resolve(ref)['properties'])
