"""
High-throughput bulk importer for the normalized vault.

The importer streams the input in batches of ``_STAGE_ROWS`` lines, so memory
stays flat across millions of lines and thousands of files:

  1. Validate every field against its format and keep only its canonical
     spelling (see vault.validators). A line that does not match its format
     is counted as skipped.
  2. Stage each batch in a temporary table (one executemany call).
  3. Create the missing reference rows with INSERT OR IGNORE straight from
     the staged rows. The unique index on each ``name`` removes duplicates,
     so no ids travel through Python.
  4. Create the Account rows with one INSERT OR IGNORE ... SELECT. The ids
     come from joins on the unique name indexes, and the fingerprint is the
     same value Account.save() computes (``vault_fp`` calls
     ``record_fingerprint``). The unique fingerprint drops records that are
     already stored.

Each batch runs in a single transaction. Measured on the development box
(2 vCPU, SQLite WAL), this is about 5x faster than the earlier ORM-based
importer; see README.md, "Performance".

Canonicalizing in step 2 is what keeps the reference tables free of
duplicates: the same email written as ``Person@Example.COM`` or
``person@example.com`` becomes one row, and a value that does not match its
format (``not-an-ip`` in the IP column, ``b@@ad`` in the identifier column)
is counted as skipped instead of being stored.

It is used by the web file/folder import flows and by the importfolder
management command.
"""

import os

from django.db import connection, transaction
from django.utils import timezone

from .models import record_fingerprint
from .utils import NO_IP, CanonicalRecord, parse_line
from .validators import (
    classify_identifier, normalize_ip, normalize_password, normalize_url,
)

# Chunk IN(...) queries to stay well below SQLite's 999-variable limit.
STREAM_BATCH_SIZE = 50000

_BINARY_EXTS = {
    ".sqlite3", ".db", ".pyc", ".pyo", ".mo", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".ico", ".pdf", ".zip", ".tar", ".gz", ".7z", ".exe",
    ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".eot",
}


_STAGE_DDL = (
    "CREATE TEMP TABLE IF NOT EXISTS vault_import_stage ("
    " url TEXT NOT NULL, account_type TEXT NOT NULL, ident TEXT NOT NULL,"
    " secret TEXT NOT NULL, ip TEXT)"
)

# Reference rows are created with INSERT OR IGNORE straight from the staged
# batch: the unique index on `name` is what removes duplicates, so no id has
# to travel through Python.
_REFERENCE_INSERTS = (
    ("vault_url",
     "INSERT OR IGNORE INTO vault_url (name, created_at, updated_at)"
     " SELECT DISTINCT url, ?, ? FROM temp.vault_import_stage"),
    ("vault_pass",
     "INSERT OR IGNORE INTO vault_pass (name, created_at, updated_at)"
     " SELECT DISTINCT secret, ?, ? FROM temp.vault_import_stage"),
    ("vault_email",
     "INSERT OR IGNORE INTO vault_email (name, created_at, updated_at)"
     " SELECT DISTINCT ident, ?, ? FROM temp.vault_import_stage"
     " WHERE account_type = 'email'"),
    ("vault_user",
     "INSERT OR IGNORE INTO vault_user (name, created_at, updated_at)"
     " SELECT DISTINCT ident, ?, ? FROM temp.vault_import_stage"
     " WHERE account_type <> 'email'"),
    ("vault_ip",
     "INSERT OR IGNORE INTO vault_ip (name, created_at, updated_at)"
     " SELECT DISTINCT ip, ?, ? FROM temp.vault_import_stage"
     " WHERE ip IS NOT NULL"),
)

# Account rows: resolve the ids by joining on the unique name indexes, compute
# the same fingerprint as Account.save() (through a SQLite function that calls
# record_fingerprint), and let the unique fingerprint drop anything stored.
_ACCOUNT_INSERT = (
    "INSERT OR IGNORE INTO vault_account (url_id, account_type, email_id,"
    " user_id, password_id, ip_id, fingerprint, created_at, updated_at)"
    " SELECT k.url_id, k.account_type, k.email_id, k.user_id, k.password_id,"
    " k.ip_id,"
    " vault_fp(k.url_id, k.account_type, k.email_id, k.user_id,"
    "          k.password_id, k.ip_id),"
    " ?, ?"
    " FROM ("
    "   SELECT DISTINCT u.id AS url_id, s.account_type AS account_type,"
    "     CASE WHEN s.account_type = 'email' THEN e.id END AS email_id,"
    "     CASE WHEN s.account_type <> 'email' THEN us.id END AS user_id,"
    "     p.id AS password_id, i.id AS ip_id"
    "   FROM temp.vault_import_stage AS s"
    "   JOIN vault_url AS u ON u.name = s.url"
    "   JOIN vault_pass AS p ON p.name = s.secret"
    "   LEFT JOIN vault_email AS e"
    "     ON s.account_type = 'email' AND e.name = s.ident"
    "   LEFT JOIN vault_user AS us"
    "     ON s.account_type <> 'email' AND us.name = s.ident"
    "   LEFT JOIN vault_ip AS i ON i.name = s.ip"
    " ) AS k"
    " WHERE (k.account_type = 'email' AND k.email_id IS NOT NULL)"
    "    OR (k.account_type <> 'email' AND k.user_id IS NOT NULL)"
)

# Rows per SQL round trip; one transaction per batch keeps memory flat.
_STAGE_ROWS = 100000


def _fingerprint_udf(url_id, account_type, email_id, user_id, pass_id, ip_id):
    return record_fingerprint(
        url_id, account_type, email_id, user_id, pass_id, ip_id
    )


def _raw_connection():
    """The sqlite3 connection, with the fingerprint function registered."""
    connection.ensure_connection()
    raw = connection.connection
    raw.create_function('vault_fp', 6, _fingerprint_udf, deterministic=True)
    raw.execute(_STAGE_DDL)
    return raw


def _flush_batch(rows):
    """
    Store one batch of canonical (url, type, ident, secret, ip) tuples.

    Returns the number of new Account rows. Everything runs as set-based SQL
    inside one transaction, so the cost per record is a few index probes
    instead of several ORM round trips.
    """
    if not rows:
        return 0
    raw = _raw_connection()
    now = timezone.now()
    stamp = connection.ops.adapt_datetimefield_value(now)
    with transaction.atomic():
        cursor = raw.cursor()
        cursor.execute('DELETE FROM temp.vault_import_stage')
        cursor.executemany(
            'INSERT INTO temp.vault_import_stage'
            ' (url, account_type, ident, secret, ip) VALUES (?, ?, ?, ?, ?)',
            rows,
        )
        for _table, sql in _REFERENCE_INSERTS:
            cursor.execute(sql, (stamp, stamp))
        cursor.execute(_ACCOUNT_INSERT, (stamp, stamp))
        created = cursor.rowcount
        cursor.close()
    return max(created, 0)


def canonical_record(result):
    """
    Validate one parsed-line dict and return it with canonical values.

    Returns None when any of the five values does not match its format, so
    that a bad line is counted as skipped instead of stored half-parsed.
    Records that arrive from `parse_line` are already validated and are
    passed straight through.
    """
    if not result:
        return None
    if isinstance(result, CanonicalRecord):
        return result

    url = normalize_url(result.get('url'))
    password = normalize_password(result.get('secret_data'))
    account_type, identifier = classify_identifier(
        result.get('account_identifier')
    )
    ip_value = normalize_ip(result.get('ip_address'))

    if not (url and password and identifier and account_type):
        return None

    return {
        'url': url,
        'account_type': account_type,
        'account_identifier': identifier,
        'secret_data': password,
        'ip_address': ip_value,
    }


def bulk_import_lines(parsed_iter):
    """
    Consume an iterable of parsed-line dicts (None for invalid lines) and
    store them in bounded batches.

    Returns (accounts_created, lines_skipped).
    """
    rows = []
    total_created = 0
    skipped = 0

    for result in parsed_iter:
        record = canonical_record(result)
        if record is None:
            skipped += 1
            continue

        # parse_line reports a missing address as 'N/A'; store no relation.
        ip_value = record['ip_address']
        if not ip_value or ip_value == NO_IP:
            ip_value = None

        rows.append((
            record['url'],
            record['account_type'],
            record['account_identifier'],
            record['secret_data'],
            ip_value,
        ))

        if len(rows) >= _STAGE_ROWS:
            total_created += _flush_batch(rows)
            rows = []

    total_created += _flush_batch(rows)
    return total_created, skipped


def _is_candidate_file(filename, ext="*"):
    lower = filename.lower()
    _, file_ext = os.path.splitext(lower)
    if file_ext in _BINARY_EXTS:
        return False
    if ext == "*" or not ext:
        return True
    normalized_ext = ext.lower().lstrip(".")
    return lower.endswith(f".{normalized_ext}")


def collect_files_from_path(target_path, ext="*"):
    """
    Return a sorted list of readable text files from a file or directory path.
    """
    if os.path.isfile(target_path):
        return [target_path]
    if not os.path.isdir(target_path):
        return []

    matched = []
    for root, dirs, filenames in os.walk(target_path):
        # Skip hidden or system directories
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".") and d != "__pycache__"
        )
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            if _is_candidate_file(filename, ext=ext):
                matched.append(os.path.join(root, filename))
    return matched


def import_path(target_path, ext="*", on_file=None):
    """
    Import all valid lines from a single file or recursively from every
    matching file inside a folder.

    Returns (total_created, total_skipped, files_count).
    """
    files = collect_files_from_path(target_path, ext=ext)
    if not files:
        return 0, 0, 0

    total_created = 0
    total_skipped = 0
    processed_files = 0

    for filepath in files:
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as handle:
                parsed = (parse_line(line) for line in handle)
                created, skipped = bulk_import_lines(parsed)
        except OSError:
            continue

        processed_files += 1
        total_created += created
        total_skipped += skipped
        if on_file is not None:
            on_file(filepath, created, skipped, total_created)

    return total_created, total_skipped, processed_files
