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

Because PBX admins set those hosts, :func:`open_recording` also refuses a
host that resolves to any non-public address and serves the bytes with an
audio content type only (:func:`safe_recording_content_type`).
"""

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

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


# ---------------------------------------------------------------------------
# Fetching: only public addresses, pinned to the address that was checked
# ---------------------------------------------------------------------------
#
# The allow-list above is partly tenant-controlled (PbxServer and
# SipConfiguration hosts are set by PBX admins), so a host on it may still
# resolve to loopback, the cloud metadata service or an internal docker
# host. ``open_recording`` therefore resolves the host, refuses the fetch
# unless *every* address is public, and connects to the checked address
# (Host header / TLS SNI and certificate check still use the hostname), so
# a DNS answer that changes between the check and the connect cannot
# redirect the request.

class UnsafeRecordingURL(Exception):
    """The recording URL must not be fetched (scheme, host or address)."""


def is_public_ip(value):
    """True for a globally routable unicast address: not loopback, private,
    link-local, multicast, reserved, unspecified or shared (CGNAT)."""
    try:
        addr = ipaddress.ip_address(str(value).split('%', 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped or addr.sixtofour
        if mapped is not None:
            addr = mapped
    if (addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_multicast
            or addr.is_reserved or addr.is_unspecified):
        return False
    return addr.is_global


def resolve_public_addresses(host, port):
    """Every address ``host`` resolves to; raises UnsafeRecordingURL when it
    does not resolve or when any address is not public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise UnsafeRecordingURL(f'cannot resolve {host}') from exc
    addresses = []
    for info in infos:
        ip = str(info[4][0]).split('%', 1)[0]
        if ip not in addresses:
            addresses.append(ip)
    if not addresses:
        raise UnsafeRecordingURL(f'cannot resolve {host}')
    for ip in addresses:
        if not is_public_ip(ip):
            raise UnsafeRecordingURL(f'{host} resolves to a non-public address')
    return addresses


def _pinned_adapter(hostname):
    from requests.adapters import HTTPAdapter

    class _PinnedHostAdapter(HTTPAdapter):
        """Connect to an IP literal but send SNI / verify the certificate
        for ``hostname``."""

        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            pool_kwargs['server_hostname'] = hostname
            pool_kwargs['assert_hostname'] = hostname
            super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    return _PinnedHostAdapter()


def open_recording(url, headers=None, timeout=(5, 30)):
    """GET ``url`` as a stream, without following redirects, after checking
    it (allow-listed host, http/https, public addresses only). Raises
    UnsafeRecordingURL when it must not be fetched; network errors are
    ``requests`` exceptions."""
    import requests

    if not is_allowed_recording_url(url):
        raise UnsafeRecordingURL('host is not an allowed recording host')
    parts = urlsplit(url.strip())
    scheme = parts.scheme
    host = parts.hostname
    try:
        port = parts.port or (443 if scheme == 'https' else 80)
    except ValueError as exc:
        raise UnsafeRecordingURL('invalid port') from exc
    ip = resolve_public_addresses(host, port)[0]

    ip_netloc = f'[{ip}]' if ':' in ip else ip
    pinned_url = urlunsplit((scheme, f'{ip_netloc}:{port}', parts.path or '/', parts.query, ''))
    default_port = (scheme == 'https' and port == 443) or (scheme == 'http' and port == 80)
    host_header = host if ':' not in host else f'[{host}]'
    if not default_port:
        host_header = f'{host_header}:{port}'
    send_headers = dict(headers or {})
    send_headers['Host'] = host_header

    session = requests.Session()
    session.trust_env = False  # no proxy / netrc from the environment
    if scheme == 'https':
        session.mount('https://', _pinned_adapter(host))
    auth = (parts.username, parts.password or '') if parts.username else None
    return session.get(
        pinned_url,
        headers=send_headers,
        auth=auth,
        stream=True,
        timeout=timeout,
        allow_redirects=False,
    )


def safe_recording_content_type(upstream_content_type, url):
    """Content-Type for a proxied recording: the upstream ``audio/*`` type,
    else the type of a .wav/.mp3/.ogg URL, else application/octet-stream.
    Never text/html or anything else a browser would render."""
    ct = (upstream_content_type or '').split(';', 1)[0].strip().lower()
    if ct.startswith('audio/') and all(ch.isalnum() or ch in '.+-/' for ch in ct):
        return ct
    path = (urlsplit(url or '').path or '').lower()
    for ext, mapped in (('.wav', 'audio/wav'), ('.mp3', 'audio/mpeg'),
                        ('.ogg', 'audio/ogg'), ('.gsm', 'audio/x-gsm')):
        if path.endswith(ext):
            return mapped
    return 'application/octet-stream'
