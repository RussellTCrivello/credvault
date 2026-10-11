"""
PostgreSQL bulk writer: ``COPY`` into a staging table, then set-based inserts.

Why it is faster than the ORM writer:

* ``COPY`` streams rows to the server in one command. It is one to two orders
  of magnitude cheaper per row than ``INSERT`` statements, which is what
  dominates the ORM writer.
* The deduplication runs inside the database as joins and ``ON CONFLICT DO
  NOTHING``, so the batch is never read back into Python. Reference values
  are looked up by their unique index, and records by their fingerprint.
* The record fingerprint is computed by PostgreSQL (``sha256``) with exactly
  the formula of ``vault.models.record_fingerprint``. Rows written by either
  writer therefore deduplicate against each other. A test checks this.

Each batch is one transaction: either every record in it is stored, or none
is. A batch may also run with ``synchronous_commit`` off (Fast and Maximum
profiles). That lets the server acknowledge the commit before the write-ahead
log is flushed. A crash can lose the last few batches, but it cannot corrupt
the database, and the import can simply be run again because duplicates are
skipped.
"""

from django.db import DEFAULT_DB_ALIAS, connections, transaction

_CREATE_STAGE = """
CREATE TEMP TABLE IF NOT EXISTS vault_import_stage (
    url text NOT NULL,
    account_type varchar(10) NOT NULL,
    identifier text NOT NULL,
    secret text NOT NULL,
    ip text
)
"""

_COPY_STAGE = (
    "COPY vault_import_stage (url, account_type, identifier, secret, ip) "
    "FROM STDIN"
)

_INSERT_URLS = """
INSERT INTO vault_url (name, created_at, updated_at)
SELECT DISTINCT url, now(), now() FROM vault_import_stage
ON CONFLICT (name) DO NOTHING
"""

_INSERT_EMAILS = """
INSERT INTO vault_email (name, created_at, updated_at)
SELECT DISTINCT identifier, now(), now() FROM vault_import_stage
WHERE account_type = 'email'
ON CONFLICT (name) DO NOTHING
"""

_INSERT_USERS = """
INSERT INTO vault_user (name, created_at, updated_at)
SELECT DISTINCT identifier, now(), now() FROM vault_import_stage
WHERE account_type = 'user'
ON CONFLICT (name) DO NOTHING
"""

_INSERT_PASSES = """
INSERT INTO vault_pass (name, created_at, updated_at)
SELECT DISTINCT secret, now(), now() FROM vault_import_stage
ON CONFLICT (name) DO NOTHING
"""

_INSERT_IPS = """
INSERT INTO vault_ip (name, created_at, updated_at)
SELECT DISTINCT ip, now(), now() FROM vault_import_stage
WHERE ip IS NOT NULL
ON CONFLICT (name) DO NOTHING
"""

# The fingerprint formula must match vault.models.record_fingerprint:
#   "{url_id}|{account_type}|{email_id}|{user_id}|{password_id}|{ip_id}"
# with an empty string for every missing id, hashed as UTF-8 with SHA-256.
_INSERT_ACCOUNTS = """
INSERT INTO vault_account (
    url_id, account_type, email_id, user_id, password_id, ip_id,
    fingerprint, created_at, updated_at
)
SELECT
    j.url_id, j.account_type, j.email_id, j.user_id, j.password_id, j.ip_id,
    encode(sha256(convert_to(
        coalesce(j.url_id::text, '') || '|' || j.account_type || '|' ||
        coalesce(j.email_id::text, '') || '|' || coalesce(j.user_id::text, '') || '|' ||
        coalesce(j.password_id::text, '') || '|' || coalesce(j.ip_id::text, ''),
        'UTF8'
    )), 'hex'),
    now(), now()
FROM (
    SELECT DISTINCT
        u.id AS url_id,
        s.account_type,
        CASE WHEN s.account_type = 'email' THEN e.id END AS email_id,
        CASE WHEN s.account_type = 'user' THEN usr.id END AS user_id,
        p.id AS password_id,
        i.id AS ip_id
    FROM vault_import_stage s
    JOIN vault_url u ON u.name = s.url
    JOIN vault_pass p ON p.name = s.secret
    LEFT JOIN vault_email e ON s.account_type = 'email' AND e.name = s.identifier
    LEFT JOIN vault_user usr ON s.account_type = 'user' AND usr.name = s.identifier
    LEFT JOIN vault_ip i ON s.ip IS NOT NULL AND i.name = s.ip
) j
WHERE (j.account_type = 'email' AND j.email_id IS NOT NULL)
   OR (j.account_type = 'user' AND j.user_id IS NOT NULL)
ON CONFLICT (fingerprint) DO NOTHING
"""


def copy_import_rows(rows, using=DEFAULT_DB_ALIAS, fast_commit=False,
                     work_mem=None):
    """
    Store row tuples ``(url, account_type, identifier, secret, ip)`` in one
    transaction and return ``(accounts_created, lines_skipped)``.

    ``rows`` yields row tuples, or None for a line that was not a valid record.
    """
    connection = connections[using]
    skipped = 0
    created = 0

    with transaction.atomic(using=using):
        with connection.cursor() as cursor:
            if fast_commit:
                cursor.execute('SET LOCAL synchronous_commit = off')
            if work_mem:
                cursor.execute(
                    "SELECT set_config('work_mem', %s, true)", [work_mem]
                )

            cursor.execute(_CREATE_STAGE)
            cursor.execute('TRUNCATE vault_import_stage')

            written = 0
            with cursor.copy(_COPY_STAGE) as copy:
                for row in rows:
                    if row is None:
                        skipped += 1
                        continue
                    copy.write_row(row)
                    written += 1

            if written:
                # The planner has no size for a temporary table until it is
                # analyzed; without this it can pick a plan that is far too slow
                # for a large batch.
                cursor.execute('ANALYZE vault_import_stage')
                cursor.execute(_INSERT_URLS)
                cursor.execute(_INSERT_EMAILS)
                cursor.execute(_INSERT_USERS)
                cursor.execute(_INSERT_PASSES)
                cursor.execute(_INSERT_IPS)
                cursor.execute(_INSERT_ACCOUNTS)
                created = cursor.rowcount

    return created, skipped
