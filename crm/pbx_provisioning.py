"""Provisioning helpers for a tenant's BYO Asterisk.

The full flow:

1. Admin clicks "Connect your PBX" in ``/settings/pbx/server/`` and submits
   ``name``, ``fqdn``, ``public_ip``.
2. :func:`provision_pbx_server_db` is called: create a dedicated Postgres
   DB for this tenant (``asterisk_<schema>``), create a DB role with
   SELECT/INSERT/UPDATE/DELETE on that DB only, run the realtime migration
   against the new DB, return the generated RW credentials.
3. Caller saves those credentials onto the :class:`crm.models.PbxServer`
   row (they're stored encrypted via :class:`EncryptedCharField`).
4. Tenant runs the one-line install script on their Asterisk.

The role password is generated fresh each time — the server is the only
place it's ever stored in plaintext (in memory, briefly, for the install
script response).
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.db import connection, connections
from django.utils import timezone

log = logging.getLogger(__name__)


@dataclass
class ProvisionedDB:
    """Credentials for a freshly-provisioned per-tenant realtime DB."""

    host: str
    port: int
    dbname: str
    rw_user: str
    rw_password: str  # plaintext, caller must encrypt before storing
    sslmode: str = "require"


def _slug(schema: str) -> str:
    """Sanitise tenant schema name for use as a Postgres identifier."""
    return re.sub(r"[^a-z0-9_]", "_", schema.lower())


def _admin_dsn() -> dict:
    """Pull admin credentials for the shared Postgres cluster from the
    ``default`` DB config — that's the superuser we use to create the new
    DB + role.
    """
    default = settings.DATABASES["default"]
    return {
        "host": getattr(settings, "ASTERISK_ADMIN_DB_HOST", "") or default["HOST"],
        "port": int(getattr(settings, "ASTERISK_ADMIN_DB_PORT", "") or default["PORT"] or 5432),
        "dbname": "defaultdb",  # connect to maintenance DB for CREATE
        "user": default["USER"],
        "password": default["PASSWORD"],
        "sslmode": default.get("OPTIONS", {}).get("sslmode", "require"),
    }


def _psql(conn, sql: str, *, autocommit: bool = True) -> None:
    import psycopg2
    conn.set_isolation_level(
        psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT if autocommit else 1
    )
    with conn.cursor() as cur:
        cur.execute(sql)


def provision_pbx_server_db(tenant_schema: str) -> ProvisionedDB:
    """Create a per-tenant Asterisk realtime DB + RW role.

    Idempotent: if the DB or role already exist, rotates the password and
    re-runs the grants so the returned ``rw_password`` is always fresh.
    """
    import psycopg2

    admin = _admin_dsn()
    slug = _slug(tenant_schema)
    dbname = f"asterisk_{slug}"
    rw_user = f"asterisk_rw_{slug}"
    rw_password = secrets.token_urlsafe(24)

    log.info("Provisioning realtime DB %s for tenant %s", dbname, tenant_schema)

    admin_conn = psycopg2.connect(**admin)
    try:
        # 1. Role: create or rotate password.
        import psycopg2.sql as psql
        with admin_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [rw_user])
            exists = cur.fetchone() is not None

        if exists:
            with admin_conn.cursor() as cur:
                cur.execute(
                    psql.SQL("ALTER ROLE {role} WITH LOGIN PASSWORD {pw}").format(
                        role=psql.Identifier(rw_user),
                        pw=psql.Literal(rw_password),
                    )
                )
        else:
            _psql(
                admin_conn,
                psql.SQL("CREATE ROLE {role} LOGIN PASSWORD {pw}").format(
                    role=psql.Identifier(rw_user),
                    pw=psql.Literal(rw_password),
                ).as_string(admin_conn),
            )
        admin_conn.commit()

        # 2. Database: CREATE if missing. OWNED BY rw_user so it has full
        #    control of its own tables.
        with admin_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", [dbname])
            db_exists = cur.fetchone() is not None

        if not db_exists:
            _psql(
                admin_conn,
                psql.SQL("CREATE DATABASE {db} OWNER {role}").format(
                    db=psql.Identifier(dbname),
                    role=psql.Identifier(rw_user),
                ).as_string(admin_conn),
            )
            log.info("  created database %s", dbname)
        else:
            log.info("  database %s already exists — reusing", dbname)

        # 3. Grants on the new DB.
        _psql(
            admin_conn,
            psql.SQL("GRANT CONNECT ON DATABASE {db} TO {role}").format(
                db=psql.Identifier(dbname),
                role=psql.Identifier(rw_user),
            ).as_string(admin_conn),
        )
    finally:
        admin_conn.close()

    # 4. Inside the new DB: grant schema-level perms on ``public``
    #    (Django migrations create the tables here since Asterisk's
    #    res_config_pgsql hardcodes public).
    inside = psycopg2.connect(
        host=admin["host"], port=admin["port"], dbname=dbname,
        user=admin["user"], password=admin["password"],
        sslmode=admin["sslmode"],
    )
    try:
        with inside.cursor() as cur:
            cur.execute(psql.SQL(
                "GRANT USAGE ON SCHEMA public TO {role}"
            ).format(role=psql.Identifier(rw_user)).as_string(inside))
            cur.execute(psql.SQL(
                "GRANT CREATE ON SCHEMA public TO {role}"
            ).format(role=psql.Identifier(rw_user)).as_string(inside))
            cur.execute(psql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
            ).format(
                admin=psql.Identifier(admin["user"]),
                role=psql.Identifier(rw_user),
            ).as_string(inside))
            cur.execute(psql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public "
                "GRANT USAGE, SELECT ON SEQUENCES TO {role}"
            ).format(
                admin=psql.Identifier(admin["user"]),
                role=psql.Identifier(rw_user),
            ).as_string(inside))
        inside.commit()
    finally:
        inside.close()

    return ProvisionedDB(
        host=admin["host"],
        port=admin["port"],
        dbname=dbname,
        rw_user=rw_user,
        rw_password=rw_password,
    )


def migrate_pbx_server_db(pbx_server) -> None:
    """Register the PbxServer's DB alias and run asterisk_state migrations
    against it.
    """
    from crm.asterisk_db import register_pbx_alias
    from django.core.management import call_command

    alias = register_pbx_alias(pbx_server)
    # Use our custom migrate_asterisk command (bypasses tenant-schemas wrapper).
    call_command("migrate_asterisk", "--database", alias, verbosity=1)
    log.info("Ran asterisk_state migrations on alias=%s", alias)


def provision_and_bootstrap(pbx_server) -> None:
    """End-to-end: create DB + role, save credentials onto the PbxServer,
    run migrations.

    Call from a view that's already in the tenant schema context so
    ``connection.schema_name`` resolves to the right tenant. Mutates
    ``pbx_server`` in place and saves.
    """
    schema = connection.schema_name
    db = provision_pbx_server_db(schema)

    pbx_server.realtime_db_host = db.host
    pbx_server.realtime_db_port = db.port
    pbx_server.realtime_db_name = db.dbname
    pbx_server.realtime_db_user = db.rw_user
    pbx_server.realtime_db_password = db.rw_password
    pbx_server.realtime_db_sslmode = db.sslmode
    pbx_server.status = pbx_server.STATUS_PROVISIONING
    pbx_server.save()

    migrate_pbx_server_db(pbx_server)

    # Ready for the tenant to run the install script.
    if not pbx_server.enrollment_expires_at:
        pbx_server.enrollment_expires_at = (
            timezone.now() + timezone.timedelta(hours=24)
            if hasattr(timezone, "timedelta")
            else timezone.now()
        )
        # `timezone.timedelta` doesn't exist; use datetime.timedelta.
        from datetime import timedelta
        pbx_server.enrollment_expires_at = timezone.now() + timedelta(hours=24)
        pbx_server.save(update_fields=["enrollment_expires_at"])
