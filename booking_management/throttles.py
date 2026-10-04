from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle


class BookingAuthThrottle(AnonRateThrottle):
    """Login / register / code endpoints: slows code and password guessing."""
    scope = 'booking_auth'


class BookingGuestThrottle(AnonRateThrottle):
    """Guest bookings: slows a script from filling a business's calendar."""
    scope = 'booking_guest'


class BookingManageThrottle(AnonRateThrottle):
    """Cancelling through a private link. Separate from guest booking so a
    shared connection (salon tablet, mobile carrier NAT) doesn't run one
    budget dry with the other."""
    scope = 'booking_manage'


class BookingClientCreateThrottle(SimpleRateThrottle):
    """Bookings created by a logged-in customer, counted per account."""
    scope = 'booking_client_create'

    def get_cache_key(self, request, view):
        if request.method != 'POST' or getattr(view, 'action', None) != 'create':
            return None
        client_id = getattr(request.user, 'id', None)
        if not client_id:
            return None
        from django.db import connection
        return self.cache_format % {'scope': self.scope, 'ident': f'{connection.schema_name}:{client_id}'}
