"""Which hosts call recordings may be fetched from.

``CallLogViewSet.stream_recording`` fetches the recording server-side when
staff press play, so a recording URL must point at one of the tenant's own
PBX hosts. Otherwise anyone who can write a call log's ``recording_url``
could make the API fetch internal addresses (blind SSRF).

Allowed hosts (case-insensitive, port ignored):

* ``settings.CALL_RECORDING_ALLOWED_HOSTS`` (defaults to the EchoDesk PBX
  hosts);
* each ``PbxServer``'s ``fqdn``, ``public_ip`` and ``recording_base_url`` host;
* each ``SipConfiguration``'s ``sip_server`` (legacy single-PBX tenants).
"""

from urllib.parse import urlsplit

from django.conf import settings

DEFAULT_ALLOWED_HOSTS = ('pbx2.echodesk.cloud', 'pbx.echodesk.cloud')


def _host(value):
    value = (value or '').strip()
    if not value:
        return ''
    if '//' not in value:
        value = f'//{value}'
    try:
        return (urlsplit(value).hostname or '').lower()
    except ValueError:
        return ''


def allowed_recording_hosts():
    from .models import PbxServer, SipConfiguration

    hosts = {
        h.lower() for h in getattr(settings, 'CALL_RECORDING_ALLOWED_HOSTS', DEFAULT_ALLOWED_HOSTS)
    }
    for fqdn, public_ip, base_url in PbxServer.objects.values_list(
        'fqdn', 'public_ip', 'recording_base_url',
    ):
        hosts.update({_host(fqdn), _host(public_ip), _host(base_url)})
    for sip_server in SipConfiguration.objects.values_list('sip_server', flat=True):
        hosts.add(_host(sip_server))
    hosts.discard('')
    return hosts


def is_allowed_recording_url(url):
    """True when ``url`` is http(s) on an allowed recording host."""
    try:
        parts = urlsplit((url or '').strip())
    except ValueError:
        return False
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return False
    return parts.hostname.lower() in allowed_recording_hosts()
