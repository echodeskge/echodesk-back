"""Shadow models mirroring Asterisk 18 realtime tables.

These models describe the schema of the ``asterisk_state`` Postgres DB that
Asterisk 18 reads via ``res_config_pgsql`` + sorcery realtime. Django owns
writes; Asterisk owns reads. We set ``managed = True`` so the schema can be
produced from a Django migration targeted at the ``asterisk`` DB alias, but
keep the column names and types aligned with the official Asterisk 18
realtime schema (see docs/CREATE-tables for reference):

    https://wiki.asterisk.org/wiki/display/AST/Asterisk+Configuration+for+Realtime

``PsContact`` is the one exception — those rows are written by Asterisk itself
during SIP registration, so we mark that model ``managed=False`` and use it
read-only from Django.

All models carry ``app_label='asterisk_state'`` so the router in
``amanati_crm.db_routers`` can route them to the right DB alias without
per-model hints.
"""
from __future__ import annotations

from django.db import models


# ---------------------------------------------------------------------------
# PJSIP realtime objects
# ---------------------------------------------------------------------------


class PsEndpoint(models.Model):
    """Row in Asterisk's ``ps_endpoints`` realtime table (pjsip endpoint).

    ``id`` is the endpoint name exposed in pjsip — we use tenant-prefixed
    values like ``acme_100`` or ``acme_trunk_magti`` to keep a single flat
    namespace across tenants.
    """

    id = models.CharField(max_length=40, primary_key=True)
    transport = models.CharField(max_length=40, null=True, blank=True)
    aors = models.CharField(max_length=200, null=True, blank=True)
    auth = models.CharField(max_length=100, null=True, blank=True)
    context = models.CharField(max_length=40, null=True, blank=True)
    disallow = models.CharField(max_length=200, null=True, blank=True)
    allow = models.CharField(max_length=200, null=True, blank=True)
    direct_media = models.CharField(max_length=3, null=True, blank=True)
    dtmf_mode = models.CharField(max_length=40, null=True, blank=True)
    force_rport = models.CharField(max_length=3, null=True, blank=True)
    ice_support = models.CharField(max_length=3, null=True, blank=True)
    identify_by = models.CharField(max_length=80, null=True, blank=True)
    mailboxes = models.CharField(max_length=40, null=True, blank=True)
    moh_suggest = models.CharField(max_length=40, null=True, blank=True)
    outbound_auth = models.CharField(max_length=100, null=True, blank=True)
    rewrite_contact = models.CharField(max_length=3, null=True, blank=True)
    rtp_symmetric = models.CharField(max_length=3, null=True, blank=True)
    send_rpid = models.CharField(max_length=3, null=True, blank=True)
    timers = models.CharField(max_length=40, null=True, blank=True)
    use_avpf = models.CharField(max_length=3, null=True, blank=True)
    media_encryption = models.CharField(max_length=40, null=True, blank=True)
    media_use_received_transport = models.CharField(max_length=3, null=True, blank=True)
    rtcp_mux = models.CharField(max_length=3, null=True, blank=True)
    callerid = models.CharField(max_length=80, null=True, blank=True)
    callerid_tag = models.CharField(max_length=40, null=True, blank=True)
    named_call_group = models.CharField(max_length=40, null=True, blank=True)
    pickup_group = models.CharField(max_length=40, null=True, blank=True)
    from_user = models.CharField(max_length=40, null=True, blank=True)
    from_domain = models.CharField(max_length=40, null=True, blank=True)
    trust_id_inbound = models.CharField(max_length=3, null=True, blank=True)
    trust_id_outbound = models.CharField(max_length=3, null=True, blank=True)
    webrtc = models.CharField(max_length=3, null=True, blank=True)
    media_encryption_optimistic = models.CharField(max_length=3, null=True, blank=True)
    dtls_auto_generate_cert = models.CharField(max_length=3, null=True, blank=True)

    class Meta:
        app_label = "asterisk_state"
        db_table = "ps_endpoints"
        managed = True

    def __str__(self):  # pragma: no cover
        return f"PsEndpoint({self.id})"


class PsAuth(models.Model):
    """Row in Asterisk's ``ps_auths`` realtime table.

    ``id`` is referenced by ``PsEndpoint.auth`` / ``outbound_auth``.
    """

    id = models.CharField(max_length=40, primary_key=True)
    auth_type = models.CharField(max_length=40, null=True, blank=True)
    password = models.CharField(max_length=80, null=True, blank=True)
    username = models.CharField(max_length=40, null=True, blank=True)
    md5_cred = models.CharField(max_length=40, null=True, blank=True)
    realm = models.CharField(max_length=40, null=True, blank=True)
    nonce_lifetime = models.IntegerField(null=True, blank=True)

    class Meta:
        app_label = "asterisk_state"
        db_table = "ps_auths"
        managed = True

    def __str__(self):  # pragma: no cover
        return f"PsAuth({self.id})"


class PsAor(models.Model):
    """Row in Asterisk's ``ps_aors`` realtime table."""

    id = models.CharField(max_length=40, primary_key=True)
    contact = models.CharField(max_length=255, null=True, blank=True)
    max_contacts = models.IntegerField(null=True, blank=True)
    qualify_frequency = models.IntegerField(null=True, blank=True)
    remove_existing = models.CharField(max_length=3, null=True, blank=True)
    authenticate_qualify = models.CharField(max_length=3, null=True, blank=True)
    default_expiration = models.IntegerField(null=True, blank=True)
    mailboxes = models.CharField(max_length=80, null=True, blank=True)
    maximum_expiration = models.IntegerField(null=True, blank=True)
    minimum_expiration = models.IntegerField(null=True, blank=True)

    class Meta:
        app_label = "asterisk_state"
        db_table = "ps_aors"
        managed = True

    def __str__(self):  # pragma: no cover
        return f"PsAor({self.id})"


class PsIdentify(models.Model):
    """Row in Asterisk's ``ps_identifies`` realtime table.

    Used for inbound IP-based identification (provider trunks).
    """

    id = models.CharField(max_length=40, primary_key=True)
    endpoint = models.CharField(max_length=40, null=True, blank=True)
    match = models.CharField(max_length=80, null=True, blank=True)
    srv_lookups = models.CharField(max_length=3, null=True, blank=True)
    match_header = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        app_label = "asterisk_state"
        db_table = "ps_identifies"
        managed = True

    def __str__(self):  # pragma: no cover
        return f"PsIdentify({self.id}→{self.endpoint})"


class PsContact(models.Model):
    """Read-only view of Asterisk-managed contacts (``ps_contacts``).

    Contacts are written by Asterisk itself when SIP endpoints register;
    Django never writes this table. ``managed=False`` so the migration that
    creates ``asterisk_state`` does not touch it.
    """

    id = models.CharField(max_length=255, primary_key=True)
    uri = models.CharField(max_length=255, null=True, blank=True)
    expiration_time = models.BigIntegerField(null=True, blank=True)
    qualify_frequency = models.IntegerField(null=True, blank=True)
    qualify_timeout = models.FloatField(null=True, blank=True)
    endpoint = models.CharField(max_length=40, null=True, blank=True)
    reg_server = models.CharField(max_length=255, null=True, blank=True)
    authenticate_qualify = models.CharField(max_length=3, null=True, blank=True)
    via_addr = models.CharField(max_length=40, null=True, blank=True)
    via_port = models.IntegerField(null=True, blank=True)
    call_id = models.CharField(max_length=255, null=True, blank=True)
    user_agent = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        app_label = "asterisk_state"
        db_table = "ps_contacts"
        managed = False  # Asterisk owns the lifecycle of this table

    def __str__(self):  # pragma: no cover
        return f"PsContact({self.id})"


class PsRegistration(models.Model):
    """Row in Asterisk's ``ps_registrations`` realtime table.

    Represents outbound REGISTER dialogs — e.g. a provider trunk that requires
    Asterisk to register against it before it will accept inbound calls.
    Asterisk 18 supports outbound registrations in realtime via the
    ``registration`` sorcery type.
    """

    id = models.CharField(max_length=40, primary_key=True)
    server_uri = models.CharField(max_length=255, null=True, blank=True)
    client_uri = models.CharField(max_length=255, null=True, blank=True)
    contact_user = models.CharField(max_length=40, null=True, blank=True)
    expiration = models.IntegerField(null=True, blank=True)
    retry_interval = models.IntegerField(null=True, blank=True)
    forbidden_retry_interval = models.IntegerField(null=True, blank=True)
    fatal_retry_interval = models.IntegerField(null=True, blank=True)
    transport = models.CharField(max_length=40, null=True, blank=True)
    outbound_auth = models.CharField(max_length=40, null=True, blank=True)
    outbound_proxy = models.CharField(max_length=255, null=True, blank=True)
    max_retries = models.IntegerField(null=True, blank=True)
    auth_rejection_permanent = models.CharField(max_length=3, null=True, blank=True)
    support_path = models.CharField(max_length=3, null=True, blank=True)

    class Meta:
        app_label = "asterisk_state"
        db_table = "ps_registrations"
        managed = True

    def __str__(self):  # pragma: no cover
        return f"PsRegistration({self.id})"


# ---------------------------------------------------------------------------
# Queue realtime objects
# ---------------------------------------------------------------------------


class AsteriskQueue(models.Model):
    """Row in Asterisk's ``queues`` realtime table (app_queue).

    Column names mirror Asterisk's ``queues.conf`` options verbatim. Only the
    ``monitor_type_dup`` column is renamed from the on-disk duplicate
    ``monitor-type`` key used by chan_dahdi-aware builds; it is retained here
    in case it is mapped via ``extconfig.conf``.
    """

    name = models.CharField(max_length=128, primary_key=True)
    musicclass = models.CharField(max_length=128, null=True, blank=True)
    announce = models.CharField(max_length=128, null=True, blank=True)
    context = models.CharField(max_length=128, null=True, blank=True)
    timeout = models.IntegerField(null=True, blank=True)
    ringinuse = models.CharField(max_length=3, null=True, blank=True)
    setinterfacevar = models.CharField(max_length=3, null=True, blank=True)
    setqueuevar = models.CharField(max_length=3, null=True, blank=True)
    setqueueentryvar = models.CharField(max_length=3, null=True, blank=True)
    monitor_format = models.CharField(max_length=8, null=True, blank=True)
    monitor_type = models.CharField(max_length=128, null=True, blank=True)
    queue_youarenext = models.CharField(max_length=128, null=True, blank=True)
    queue_thereare = models.CharField(max_length=128, null=True, blank=True)
    queue_callswaiting = models.CharField(max_length=128, null=True, blank=True)
    queue_quantity1 = models.CharField(max_length=128, null=True, blank=True)
    queue_quantity2 = models.CharField(max_length=128, null=True, blank=True)
    queue_holdtime = models.CharField(max_length=128, null=True, blank=True)
    queue_minutes = models.CharField(max_length=128, null=True, blank=True)
    queue_minute = models.CharField(max_length=128, null=True, blank=True)
    queue_seconds = models.CharField(max_length=128, null=True, blank=True)
    queue_thankyou = models.CharField(max_length=128, null=True, blank=True)
    queue_callerannounce = models.CharField(max_length=128, null=True, blank=True)
    queue_reporthold = models.CharField(max_length=128, null=True, blank=True)
    announce_frequency = models.IntegerField(null=True, blank=True)
    announce_to_first_user = models.CharField(max_length=3, null=True, blank=True)
    min_announce_frequency = models.IntegerField(null=True, blank=True)
    announce_round_seconds = models.IntegerField(null=True, blank=True)
    announce_holdtime = models.CharField(max_length=8, null=True, blank=True)
    announce_position = models.CharField(max_length=8, null=True, blank=True)
    announce_position_limit = models.IntegerField(null=True, blank=True)
    periodic_announce = models.CharField(max_length=255, null=True, blank=True)
    periodic_announce_frequency = models.IntegerField(null=True, blank=True)
    random_periodic_announce = models.CharField(max_length=3, null=True, blank=True)
    relative_periodic_announce = models.CharField(max_length=3, null=True, blank=True)
    retry = models.IntegerField(null=True, blank=True)
    wrapuptime = models.IntegerField(null=True, blank=True)
    penaltymemberslimit = models.IntegerField(null=True, blank=True)
    autofill = models.CharField(max_length=3, null=True, blank=True)
    monitor_type_dup = models.CharField(max_length=128, null=True, blank=True)
    autopause = models.CharField(max_length=8, null=True, blank=True)
    autopausedelay = models.IntegerField(null=True, blank=True)
    autopausebusy = models.CharField(max_length=3, null=True, blank=True)
    autopauseunavail = models.CharField(max_length=3, null=True, blank=True)
    maxlen = models.IntegerField(null=True, blank=True)
    servicelevel = models.IntegerField(null=True, blank=True)
    strategy = models.CharField(max_length=32, null=True, blank=True)
    joinempty = models.CharField(max_length=128, null=True, blank=True)
    leavewhenempty = models.CharField(max_length=128, null=True, blank=True)
    eventmemberstatus = models.CharField(max_length=3, null=True, blank=True)
    eventwhencalled = models.CharField(max_length=3, null=True, blank=True)
    reportholdtime = models.CharField(max_length=3, null=True, blank=True)
    memberdelay = models.IntegerField(null=True, blank=True)
    weight = models.IntegerField(null=True, blank=True)
    timeoutrestart = models.CharField(max_length=3, null=True, blank=True)
    defaultrule = models.CharField(max_length=128, null=True, blank=True)
    timeoutpriority = models.CharField(max_length=128, null=True, blank=True)

    class Meta:
        app_label = "asterisk_state"
        db_table = "queues"
        managed = True

    def __str__(self):  # pragma: no cover
        return f"AsteriskQueue({self.name})"


class AsteriskQueueMember(models.Model):
    """Row in Asterisk's ``queue_members`` realtime table.

    Agents are identified by ``interface`` (typically ``PJSIP/<endpoint>``).
    The ``uniqueid`` column is an auto-incrementing surrogate key because the
    natural key (queue_name, interface) is not globally unique across all
    Asterisk builds — realtime tolerates the surrogate.
    """

    uniqueid = models.AutoField(primary_key=True)
    queue_name = models.CharField(max_length=128)
    interface = models.CharField(max_length=128)
    membername = models.CharField(max_length=128, null=True, blank=True)
    state_interface = models.CharField(max_length=128, null=True, blank=True)
    penalty = models.IntegerField(null=True, blank=True)
    paused = models.IntegerField(null=True, blank=True)
    reason_paused = models.CharField(max_length=80, null=True, blank=True)
    wrapuptime = models.IntegerField(null=True, blank=True)

    class Meta:
        app_label = "asterisk_state"
        db_table = "queue_members"
        managed = True
        unique_together = [("queue_name", "interface")]

    def __str__(self):  # pragma: no cover
        return f"AsteriskQueueMember({self.queue_name}→{self.interface})"
