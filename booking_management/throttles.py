from rest_framework.throttling import AnonRateThrottle


class BookingAuthThrottle(AnonRateThrottle):
    """Login / register / code endpoints: slows code and password guessing."""
    scope = 'booking_auth'


class BookingGuestThrottle(AnonRateThrottle):
    """Guest bookings: slows a script from filling a business's calendar."""
    scope = 'booking_guest'
