"""
DRF authentication backends for the dashboard API.

``MultiTokenAuthentication`` accepts the same ``Authorization: Token <key>``
header as DRF's stock ``TokenAuthentication`` but resolves the key against two
tables: the shared per-user ``authtoken.Token`` (web dashboard, unchanged) and
the per-device ``users.AuthDeviceToken`` (mobile apps).
"""
from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions
from rest_framework.authentication import TokenAuthentication

from users.models import AuthDeviceToken

# How stale ``last_used_at`` may get before a request writes it again.  Keeps
# the per-request cost to a single SELECT for the common case.
LAST_USED_WRITE_INTERVAL = timedelta(minutes=5)


class MultiTokenAuthentication(TokenAuthentication):
    """
    Shared DRF token first, then per-device token.

    Subclassing ``TokenAuthentication`` keeps the ``Token`` keyword (so the
    header is byte-identical for every client) and lets drf-spectacular's
    ``TokenScheme`` (``match_subclasses = True``) document it as ``tokenAuth``.
    """

    def authenticate_credentials(self, key):
        try:
            return super().authenticate_credentials(key)
        except exceptions.AuthenticationFailed:
            pass

        try:
            token = AuthDeviceToken.objects.select_related('user').get(key=key)
        except AuthDeviceToken.DoesNotExist:
            raise exceptions.AuthenticationFailed(_('Invalid token.'))

        if not token.user.is_active:
            raise exceptions.AuthenticationFailed(_('User inactive or deleted.'))

        if token.is_expired:
            raise exceptions.AuthenticationFailed(_('Token has expired.'))

        self._touch(token)
        return (token.user, token)

    @staticmethod
    def _touch(token):
        """Throttled ``last_used_at`` update — at most one write per interval."""
        now = timezone.now()
        if token.last_used_at is None or now - token.last_used_at > LAST_USED_WRITE_INTERVAL:
            token.last_used_at = now
            AuthDeviceToken.objects.filter(key=token.key).update(last_used_at=now)
