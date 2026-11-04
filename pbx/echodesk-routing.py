#!/usr/bin/env python3
"""
EchoDesk PBX Routing AGI Script

Called by Asterisk on each incoming call to determine:
- Whether it's working hours
- Which sound files to play
- Whether to route to queue, voicemail, or forward

Deploy to: /var/lib/asterisk/agi-bin/echodesk-routing.py
Make executable: chmod +x /var/lib/asterisk/agi-bin/echodesk-routing.py

Requires: pip3 install requests

Environment variables (set in /etc/default/asterisk or /etc/asterisk/asterisk.conf):
  ECHODESK_API_URL - Base URL of EchoDesk API (e.g., https://api.echodesk.ge)
  ECHODESK_PBX_TOKEN - This PBX's EchoDesk token (sent as X-PBX-Token; picks the tenant)
  PBX_SHARED_SECRET - Legacy shared secret (only for servers without ECHODESK_PBX_TOKEN)
"""

import os
import sys
import json
import logging

logging.basicConfig(
    filename='/var/log/asterisk/echodesk-routing.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)

# ============================================================================
# AGI HELPERS
# ============================================================================

def read_agi_env():
    """Read AGI environment variables from stdin."""
    env = {}
    while True:
        line = sys.stdin.readline().strip()
        if not line:
            break
        key, _, val = line.partition(': ')
        env[key.strip()] = val.strip()
    return env


def agi_set_variable(name, value):
    """Set an Asterisk channel variable."""
    sys.stdout.write(f'SET VARIABLE {name} "{value}"\n')
    sys.stdout.flush()
    sys.stdin.readline()  # Read response


def agi_verbose(message, level=1):
    """Send a verbose message to Asterisk."""
    sys.stdout.write(f'VERBOSE "{message}" {level}\n')
    sys.stdout.flush()
    sys.stdin.readline()


# ============================================================================
# API QUERY
# ============================================================================

def query_echodesk_api(did):
    """Query the EchoDesk call routing API."""
    import requests

    api_url = os.environ.get('ECHODESK_API_URL', 'https://api.echodesk.ge')
    pbx_token = os.environ.get('ECHODESK_PBX_TOKEN', '')
    secret = os.environ.get('PBX_SHARED_SECRET', '')

    url = f'{api_url}/api/pbx/call-routing/'
    headers = {}
    if pbx_token:
        headers['X-PBX-Token'] = pbx_token
    if secret:
        headers['Authorization'] = f'Bearer {secret}'

    try:
        resp = requests.get(
            url,
            params={'did': did},
            headers=headers,
            timeout=3,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logging.error(f'API request failed for DID {did}: {e}')
        return None


# ============================================================================
# MAIN
# ============================================================================

def main():
    env = read_agi_env()
    did = env.get('agi_extension', env.get('agi_dnid', ''))
    caller = env.get('agi_callerid', 'unknown')

    logging.info(f'Incoming call: DID={did}, Caller={caller}')
    agi_verbose(f'EchoDesk routing: DID={did}, Caller={caller}')

    # Query API
    data = query_echodesk_api(did)

    if data is None:
        # API unreachable — fallback to open (route to queue), legacy queue name.
        logging.warning('API unreachable, using fallback defaults')
        agi_set_variable('IS_WORKING', 'true')
        agi_set_variable('ROUTE_ACTION', 'queue')
        agi_set_variable('SOUND_GREETING', '')
        agi_set_variable('SOUND_AFTER_HOURS', '')
        agi_set_variable('SOUND_QUEUE_HOLD', '')
        agi_set_variable('SOUND_VOICEMAIL_PROMPT', '')
        agi_set_variable('VM_ENABLED', 'false')
        agi_set_variable('FORWARD_NUMBER', '')
        agi_set_variable('QUEUE_NAME', 'support')
        agi_set_variable('QUEUE_SLUG', 'support')
        agi_set_variable('TENANT_SCHEMA', '')
        agi_set_variable('DEST_EXTENSIONS', '')
        agi_set_variable('IVR_CONTEXT', '')
        agi_verbose('EchoDesk: API unreachable, fallback to queue')
        return

    # Extract routing data
    is_working = data.get('is_working_hours', True)
    action = data.get('action', 'queue')
    sounds = data.get('sounds', {})
    vm_enabled = data.get('voicemail_enabled', False)
    after_hours_action = data.get('after_hours_action', 'announcement')
    forward_number = data.get('forward_number', '')

    # Set channel variables
    agi_set_variable('IS_WORKING', 'true' if is_working else 'false')
    agi_set_variable('ROUTE_ACTION', action)
    agi_set_variable('VM_ENABLED', 'true' if vm_enabled else 'false')
    agi_set_variable('AFTER_HOURS_ACTION', after_hours_action)
    agi_set_variable('FORWARD_NUMBER', forward_number or '')

    # Set sound URLs
    for sound_key in ['greeting', 'after_hours', 'queue_hold', 'voicemail_prompt', 'thank_you', 'transfer_hold']:
        url = sounds.get(sound_key, '') or ''
        agi_set_variable(f'SOUND_{sound_key.upper()}', url)

    # InboundRoute-driven fields (new in PBX management panel):
    # - QUEUE_NAME: Asterisk queue name to dial when action=queue. Before ARA
    #   cutover this is the raw slug (e.g. "support"); after cutover it's the
    #   tenant-prefixed name (e.g. "amanati_support").
    # - QUEUE_SLUG: the product-level slug, independent of Asterisk naming.
    # - TENANT_SCHEMA: helpful for logs + multi-tenant context switching later.
    # - DEST_EXTENSIONS: comma-joined extensions for direct-extension routes.
    # - IVR_CONTEXT: dialplan context name when action=ivr_custom.
    agi_set_variable('QUEUE_NAME', data.get('queue_name') or '')
    agi_set_variable('QUEUE_SLUG', data.get('queue_slug') or '')
    agi_set_variable('TENANT_SCHEMA', data.get('tenant_schema') or '')
    extensions = data.get('extensions') or []
    agi_set_variable('DEST_EXTENSIONS', ','.join(extensions))
    route_info = data.get('inbound_route') or {}
    agi_set_variable('IVR_CONTEXT', route_info.get('ivr_custom_context') or '')

    status_msg = (
        f'EchoDesk: working={is_working}, action={action}, vm={vm_enabled}, '
        f'queue={data.get("queue_name") or "-"}, tenant={data.get("tenant_schema") or "-"}'
    )
    logging.info(status_msg)
    agi_verbose(status_msg)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logging.exception(f'Fatal error in echodesk-routing.py: {e}')
        # Set safe defaults so Asterisk doesn't crash
        try:
            agi_set_variable('IS_WORKING', 'true')
            agi_set_variable('ROUTE_ACTION', 'queue')
        except Exception:
            pass
