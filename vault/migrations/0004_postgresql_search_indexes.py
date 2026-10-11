"""
PostgreSQL search indexes for the five reference tables.

* ``<table>_name_prefix_idx`` -- B-tree on ``lower(name) text_pattern_ops``.
  Prefix searches (``lower(name) LIKE 'https://example.com/%'``) need it under
  a language collation such as en_US. Under the C or POSIX collation the
  model's ``<table>_name_lower_idx`` already serves them, so this index is
  skipped there: it would be an exact duplicate that costs import time.
* ``<table>_name_trgm_idx`` -- GIN trigram index on ``name``. It makes the
  case-insensitive substring search (``name__contains_ci``) an index lookup
  instead of a scan of the whole table. It needs the ``pg_trgm`` extension.
  Set ``CREDVAULT_TRIGRAM_SEARCH=0`` to skip it: the index is large, and a
  workspace that only uses exact and prefix search does not need it.

The indexes are created with ``CREATE INDEX CONCURRENTLY``, which does not
block writes while it runs. That command cannot run inside a transaction, so
this migration is not atomic; each statement is committed on its own. The
migration does nothing on SQLite.
"""

import os

from django.db import migrations

REFERENCE_TABLES = (
    'vault_url',
    'vault_email',
    'vault_user',
    'vault_pass',
    'vault_ip',
)


def _trigram_enabled():
    return os.environ.get('CREDVAULT_TRIGRAM_SEARCH', '1').strip().lower() not in (
        '0', 'false', 'no', 'off',
    )


def _collation_is_c(cursor, table):
    # Provider and collation of the ``name`` column. 'c' is libc; 'd' means the
    # database default, in which case the database's provider and collation apply.
    cursor.execute(
        "SELECT CASE WHEN c.collname IS NULL OR c.collname = 'default' "
        "THEN d.datlocprovider::text ELSE c.collprovider::text END, "
        "CASE WHEN c.collname IS NULL OR c.collname = 'default' "
        "THEN d.datcollate ELSE c.collcollate END "
        'FROM pg_attribute a '
        'LEFT JOIN pg_collation c ON c.oid = a.attcollation '
        'JOIN pg_database d ON d.datname = current_database() '
        'WHERE a.attrelid = %s::regclass AND a.attname = %s',
        [table, 'name'],
    )
    row = cursor.fetchone()
    # ICU and other providers never count as byte order, even when datcollate reads 'C'.
    return bool(row) and row[0] == 'c' and row[1] in ('C', 'POSIX')


def create_search_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return

    with schema_editor.connection.cursor() as cursor:
        trigram = _trigram_enabled()
        if trigram:
            cursor.execute(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm'"
            )
            if cursor.fetchone() is None:
                trigram = False  # server lacks the contrib module
            else:
                cursor.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')

        for table in REFERENCE_TABLES:
            if not _collation_is_c(cursor, table):
                cursor.execute(
                    f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {table}_name_prefix_idx '
                    f'ON {table} (lower(name) text_pattern_ops)'
                )
            if trigram:
                cursor.execute(
                    f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {table}_name_trgm_idx '
                    f'ON {table} USING gin (name gin_trgm_ops)'
                )


def drop_search_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return

    with schema_editor.connection.cursor() as cursor:
        for table in REFERENCE_TABLES:
            cursor.execute(
                f'DROP INDEX CONCURRENTLY IF EXISTS {table}_name_trgm_idx'
            )
            cursor.execute(
                f'DROP INDEX CONCURRENTLY IF EXISTS {table}_name_prefix_idx'
            )


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('vault', '0003_search_indexes_and_settings'),
    ]

    operations = [
        migrations.RunPython(create_search_indexes, drop_search_indexes),
    ]
