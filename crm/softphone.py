"""Transport settings the softphone endpoints add to a SIP configuration.

``my_config`` / ``webrtc_config`` return the legacy ``sip_server`` /
``stun_server`` / ``turn_*`` columns and, on top of them, two derived
fields the web client prefers when present:

* ``wss_url`` — the SIP-over-WebSocket URL. The tenant's active
  :class:`crm.models.PbxServer` wins when it has one; otherwise it is
  built from the configuration (``wss://{sip_server}:{sip_port}{path}``).
  Plain ``ws://`` is produced only for local development hosts.
* ``ice_servers`` — ``[{urls, username?, credential?}]`` ready for
  ``RTCPeerConnection``: the STUN server, then the TURN server. When the
  configuration carries a coturn ``static-auth-secret``
  (``turn_auth_secret``), the TURN entry gets ephemeral REST-API
  credentials (``username = "<expiry>:<extension>"``,
  ``credential = base64(HMAC-SHA1(secret, username))``, 12 h) and the
  static ``turn_password`` is not sent at all.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from django.utils.encoding import force_bytes

TURN_CREDENTIAL_TTL = 12 * 60 * 60  # seconds

_DEV_WS_HOSTS = {'localhost', '127.0.0.1', '::1', '[::1]', 'lvh.me'}


def is_dev_ws_host(host: str) -> bool:
    """Hosts for which an unencrypted ``ws://`` transport is acceptable."""
    host = (host or '').strip().lower()
    return host in _DEV_WS_HOSTS or host.endswith('.lvh.me')


def build_wss_url(sip_config, pbx=None) -> str:
    """The WebSocket URL the softphone should connect to.

    ``pbx`` is the tenant's active :class:`PbxServer` (or ``None``); its
    ``wss_url`` wins when set. The fallback uses the configuration's
    ``sip_server``/``sip_port``/``websocket_path`` (``/ws`` when blank).
    Returns ``''`` when there is no host to connect to.
    """
    explicit = (getattr(pbx, 'wss_url', '') or '').strip() if pbx is not None else ''
    if explicit:
        return explicit
    host = (sip_config.sip_server or '').strip()
    if not host:
        return ''
    path = (sip_config.websocket_path or '').strip() or '/ws'
    if not path.startswith('/'):
        path = '/' + path
    scheme = 'ws' if is_dev_ws_host(host) else 'wss'
    port = sip_config.sip_port or 8089
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'  # bare IPv6 literal
    return f'{scheme}://{host}:{port}{path}'


def ephemeral_turn_credentials(secret: str, extension: str, *, ttl: int = TURN_CREDENTIAL_TTL,
                               now: float | None = None) -> tuple[str, str]:
    """coturn REST-API ("use-auth-secret") credentials for ``extension``.

    ``username`` is ``"<unix expiry>:<extension>"`` and ``credential`` is
    the base64 HMAC-SHA1 of that username keyed with the shared secret;
    coturn recomputes the same HMAC and rejects the pair after ``expiry``.
    """
    expiry = int((time.time() if now is None else now) + ttl)
    username = f'{expiry}:{extension or ""}'
    digest = hmac.new(force_bytes(secret), force_bytes(username), hashlib.sha1).digest()
    return username, base64.b64encode(digest).decode('ascii')


def build_ice_servers(sip_config, extension: str) -> list[dict]:
    """``[{urls, username?, credential?}]`` for the softphone's
    ``RTCPeerConnection``: STUN first (when configured), then TURN with
    ephemeral credentials when ``turn_auth_secret`` is set, else the static
    ``turn_username``/``turn_password`` pair."""
    servers: list[dict] = []
    stun = (sip_config.stun_server or '').strip()
    if stun:
        servers.append({'urls': stun})
    turn = (sip_config.turn_server or '').strip()
    if turn:
        entry: dict = {'urls': turn}
        secret = sip_config.turn_auth_secret or ''
        if secret:
            username, credential = ephemeral_turn_credentials(secret, extension)
            entry['username'] = username
            entry['credential'] = credential
        else:
            if sip_config.turn_username:
                entry['username'] = sip_config.turn_username
            if sip_config.turn_password:
                entry['credential'] = sip_config.turn_password
        servers.append(entry)
    return servers


def softphone_transport(sip_config, extension: str, pbx=None) -> dict:
    """Both derived fields plus whether the static TURN password may be
    sent (it is withheld whenever ephemeral credentials are in use)."""
    return {
        'wss_url': build_wss_url(sip_config, pbx),
        'ice_servers': build_ice_servers(sip_config, extension),
        'static_turn_password': not bool(sip_config.turn_auth_secret),
    }
