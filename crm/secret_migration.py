"""Data-migration helpers that move PBX secrets to encrypted-at-rest storage.

``crm.fields.EncryptedCharField.from_db_value`` returns ``''`` whenever a
stored value is not a valid Fernet token. That means an ``AlterField`` from a
plain ``CharField`` to ``EncryptedCharField`` silently *blanks* every secret
until the rows are re-written as cipher text. The functions here are therefore
run from the very same migration that alters the columns
(``crm/migrations/0018_calls_integration.py``), right after the ``AlterField``
operations, so no deploy ever sees a blank password.

Design notes
------------
* Raw SQL only, through ``schema_editor.connection`` and **unqualified** table
  names: django-tenant-schemas sets ``search_path`` per schema and the
  migration runs once per tenant, so each run touches exactly that tenant's
  rows. Historical ORM models are avoided so the helpers do not depend on how
  the field class behaves at a given point in the migration graph.
* Idempotent: a value that already decrypts with the active key (or one of
  ``FERNET_KEYS_FALLBACK``) is left alone, so re-running the step is a no-op.
* Reversible: ``decrypt_existing`` writes the plaintext back and leaves values
  that do not decrypt untouched (they were never ours to begin with).

``SECRET_COLUMNS`` is also used by ``manage.py verify_pbx_secrets``.
"""
from __future__ import annotations

from cryptography.fernet import InvalidToken

from crm.fields import _load_fernets

# table -> columns stored through EncryptedCharField
SECRET_COLUMNS: dict[str, tuple[str, ...]] = {
    'crm_sipconfiguration': ('password', 'turn_password'),
    'crm_userphoneassignment': ('extension_password',),
    'crm_trunk': ('password',),
    'crm_pbxsettings': ('sms_api_key',),
}


def _try_decrypt(fernets, value: str):
    """Return the plaintext for a Fernet token, or ``None`` when it is not one."""
    try:
        return fernets.decrypt(value.encode()).decode()
    except (InvalidToken, TypeError, ValueError):
        return None


def _iter_non_empty(cursor, table: str, column: str):
    cursor.execute(
        f'SELECT id, "{column}" FROM "{table}" '
        f'WHERE "{column}" IS NOT NULL AND "{column}" <> %s',
        [''],
    )
    return cursor.fetchall()


def _table_exists(cursor, table: str) -> bool:
    # ``to_regclass`` honours search_path, so this is per-schema too.
    cursor.execute('SELECT to_regclass(%s) IS NOT NULL', [table])
    return bool(cursor.fetchone()[0])


def encrypt_table(connection, table: str, column: str) -> dict:
    """Encrypt every plaintext value in ``table.column``.

    Returns ``{'encrypted': n, 'skipped': m}`` where ``skipped`` counts rows
    that were already cipher text.
    """
    fernets = _load_fernets()
    stats = {'encrypted': 0, 'skipped': 0}
    with connection.cursor() as cursor:
        if not _table_exists(cursor, table):
            return stats
        for row_id, value in _iter_non_empty(cursor, table, column):
            if _try_decrypt(fernets, value) is not None:
                stats['skipped'] += 1
                continue
            token = fernets.encrypt(value.encode()).decode()
            cursor.execute(
                f'UPDATE "{table}" SET "{column}" = %s WHERE id = %s',
                [token, row_id],
            )
            stats['encrypted'] += 1
    return stats


def decrypt_table(connection, table: str, column: str) -> dict:
    """Write the plaintext back for every value that decrypts.

    Returns ``{'decrypted': n, 'skipped': m}``; ``skipped`` are values that
    are not Fernet tokens (already plaintext, or undecryptable).
    """
    fernets = _load_fernets()
    stats = {'decrypted': 0, 'skipped': 0}
    with connection.cursor() as cursor:
        if not _table_exists(cursor, table):
            return stats
        for row_id, value in _iter_non_empty(cursor, table, column):
            plaintext = _try_decrypt(fernets, value)
            if plaintext is None:
                stats['skipped'] += 1
                continue
            cursor.execute(
                f'UPDATE "{table}" SET "{column}" = %s WHERE id = %s',
                [plaintext, row_id],
            )
            stats['decrypted'] += 1
    return stats


def count_undecryptable(connection, table: str, column: str) -> dict:
    """Count non-empty raw values that ``EncryptedCharField`` would read as ``''``."""
    fernets = _load_fernets()
    stats = {'total': 0, 'undecryptable': 0}
    with connection.cursor() as cursor:
        if not _table_exists(cursor, table):
            return stats
        for _row_id, value in _iter_non_empty(cursor, table, column):
            stats['total'] += 1
            if _try_decrypt(fernets, value) is None:
                stats['undecryptable'] += 1
    return stats


def encrypt_existing(apps, schema_editor):
    """RunPython forward: plaintext -> Fernet for every column in SECRET_COLUMNS."""
    connection = schema_editor.connection
    for table, columns in SECRET_COLUMNS.items():
        for column in columns:
            encrypt_table(connection, table, column)


def decrypt_existing(apps, schema_editor):
    """RunPython reverse: Fernet -> plaintext so the plain CharField reads it again."""
    connection = schema_editor.connection
    for table, columns in SECRET_COLUMNS.items():
        for column in columns:
            decrypt_table(connection, table, column)
