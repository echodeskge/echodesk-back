"""Request-scoped board broadcast batching (E11, E12)."""
from .realtime import MUTATION_HEADER, parse_client_mutation_id, request_context


class TicketBroadcastMiddleware:
    """Open one ``BroadcastBatch`` per request and flush it at the end.

    Reads ``X-Client-Mutation`` so every board event the request causes
    carries it as ``client_mutation_id`` (the sender recognises its own
    echo). Placed after the auth middleware; DRF's token user is written back
    onto the request during the view, so signals see the acting user.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        mutation_id = parse_client_mutation_id(request.META.get(MUTATION_HEADER))
        with request_context(request, mutation_id):
            return self.get_response(request)
