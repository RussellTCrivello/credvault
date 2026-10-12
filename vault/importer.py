"""
High-throughput bulk importer for the normalized vault.

The importer streams the input in batches of ``stage_rows()`` lines (250,000
rows by default, more on a machine with free memory), so memory stays flat
across millions of lines and thousands of files:

  1. Validate every field against its format and keep only its canonical
     spelling (see vault.validators). A line that does not match its format
     is counted as skipped.
  2. Copy each batch into a temporary table with COPY (the fastest bulk path
     PostgreSQL offers).
  3. Create the missing reference rows with INSERT ... ON CONFLICT DO NOTHING
     straight from the staged rows. The unique constraint on each ``name``
     removes duplicates, so no ids travel through Python.
  4. Create the Account rows with one INSERT ... SELECT. The ids come from
     joins on the unique name constraints, and the fingerprint is computed in
     SQL (sha256 over the same text as ``record_fingerprint``), so bulk-loaded
     and ORM-saved records match exactly. The unique fingerprint drops records
     that are already stored.

Each batch runs in a single transaction. See README.md, "Performance", for
measured speeds.

Canonicalizing in step 2 is what keeps the reference tables free of
duplicates: the same email written as ``Person@Example.COM`` or
``person@example.com`` becomes one row, and a value that does not match its
format (``not-an-ip`` in the IP column, ``b@@ad`` in the identifier column)
is counted as skipped instead of being stored.

The web and management-command entry points use ``vault.import_engine``
for durable jobs and per-line ledgers. This lower-level bulk API remains
available for compatibility and performance tests; callers that use it
directly do not get job tracking or automatic recovery.

Speed for very large jobs:

* Batches hold ``VAULT_IMPORT_BATCH_ROWS`` rows (250,000 by default, more on
  a machine with plenty of free memory), so the per-batch transaction, ANALYZE
  and planning overhead is spread over more lines.
* Parsing and canonicalizing run in worker processes (``VAULT_IMPORT_WORKERS``,
  default: up to 4 or the CPU count) while the previous batch is already being
  copied and merged, so parsing overlaps the database round-trips instead of
  adding to them. Workers start with ``fork`` where it is available and with
  ``spawn`` where it is not (Windows); without an explicit worker setting,
  platforms that only offer ``spawn`` parse inline in one process.
* Optional "fast mode" (``--fast`` on importfolder, the checkbox on the import
  page) drops the five trigram search indexes for the duration and rebuilds
  them once at the end. Updating those indexes on every new reference value is
  the largest single cost of loading into a large database; rebuilding them
  once afterwards is much cheaper. The rebuild runs even when the import
  fails.
"""

import os
import re
from contextlib import contextmanager

from django.db import connection, transaction
from django.utils import timezone

from .utils import NO_IP, CanonicalRecord, parse_line
from .validators import (
    classify_identifier, normalize_ip, normalize_password, normalize_url,
)

STREAM_BATCH_SIZE = 50000

_BINARY_EXTS = {
    ".sqlite3", ".db", ".pyc", ".pyo", ".mo", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".ico", ".pdf", ".zip", ".tar", ".gz", ".7z", ".exe",
    ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".eot",
}


_STAGE_DDL = (
    "CREATE TEMP TABLE IF NOT EXISTS vault_import_stage ("
    " url text NOT NULL, account_type text NOT NULL, ident text NOT NULL,"
    " secret text NOT NULL, ip text)"
)

# Reference rows are created straight from the staged batch. The unique
# constraint on each `name` removes duplicates (ON CONFLICT DO NOTHING), so
# no id travels through Python.
_REFERENCE_INSERTS = (
    ("vault_url",
     "INSERT INTO vault_url (name, created_at, updated_at)"
     " SELECT DISTINCT url, %s, %s FROM vault_import_stage"
     " ON CONFLICT (name) DO NOTHING"),
    ("vault_pass",
     "INSERT INTO vault_pass (name, created_at, updated_at)"
     " SELECT DISTINCT secret, %s, %s FROM vault_import_stage"
     " ON CONFLICT (name) DO NOTHING"),
    ("vault_email",
     "INSERT INTO vault_email (name, created_at, updated_at)"
     " SELECT DISTINCT ident, %s, %s FROM vault_import_stage"
     " WHERE account_type = 'email'"
     " ON CONFLICT (name) DO NOTHING"),
    ("vault_user",
     "INSERT INTO vault_user (name, created_at, updated_at)"
     " SELECT DISTINCT ident, %s, %s FROM vault_import_stage"
     " WHERE account_type <> 'email'"
     " ON CONFLICT (name) DO NOTHING"),
    ("vault_ip",
     "INSERT INTO vault_ip (name, created_at, updated_at)"
     " SELECT DISTINCT ip, %s, %s FROM vault_import_stage"
     " WHERE ip IS NOT NULL"
     " ON CONFLICT (name) DO NOTHING"),
)

# Account rows: resolve the ids by joining on the unique name constraints and
# compute the fingerprint in SQL. The expression builds the same text payload
# as record_fingerprint() and hashes it with SHA-256, so bulk-loaded and
# ORM-saved records get identical fingerprints. The unique fingerprint then
# drops records that are already stored.
_FINGERPRINT_SQL = (
    "encode(sha256(convert_to("
    " coalesce(k.url_id::text, '') || '|' || k.account_type || '|' ||"
    " coalesce(k.email_id::text, '') || '|' ||"
    " coalesce(k.user_id::text, '') || '|' ||"
    " coalesce(k.password_id::text, '') || '|' ||"
    " coalesce(k.ip_id::text, ''), 'UTF8')), 'hex')"
)

_ACCOUNT_INSERT = (
    "INSERT INTO vault_account (url_id, account_type, email_id, user_id,"
    " password_id, ip_id, fingerprint, created_at, updated_at)"
    " SELECT k.url_id, k.account_type, k.email_id, k.user_id, k.password_id,"
    " k.ip_id, " + _FINGERPRINT_SQL + ", %s, %s"
    " FROM ("
    "   SELECT DISTINCT u.id AS url_id, s.account_type AS account_type,"
    "     CASE WHEN s.account_type = 'email' THEN e.id END AS email_id,"
    "     CASE WHEN s.account_type <> 'email' THEN us.id END AS user_id,"
    "     p.id AS password_id, i.id AS ip_id"
    "   FROM vault_import_stage AS s"
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
    " ON CONFLICT (fingerprint) DO NOTHING"
)

#: Rows per COPY batch; one transaction per batch keeps memory flat. Every
#: batch costs one transaction, one COPY, one ANALYZE and one plan, so a
#: larger batch means less overhead per line -- but the parser has to fill a
#: batch before the database work on it can start, so a batch bigger than the
#: input delays everything and measured slower than this default above about
#: half a million rows (see README.md, "Import speed", for the curve).
#: ``VAULT_IMPORT_BATCH_ROWS`` overrides the size (minimum 10,000).
DEFAULT_STAGE_ROWS = 250000

#: One buffered row tuple costs about 370 bytes in Python (measured on the
#: five stored values), so 700 rows per free megabyte keeps the buffer near a
#: quarter of the free memory. That only lowers the default on a machine (or
#: container) with very little memory left; more memory does not raise it,
#: because the measurements above say a bigger batch is not faster.
_ROWS_PER_FREE_MB = 700

#: Raw lines handed to one parse task; small enough for even progress between
#: workers, large enough that pickling overhead is noise.
_PARSE_CHUNK_LINES = 25000

#: Chunks allowed in flight per worker before the parent applies backpressure.
_MAX_PENDING_CHUNKS_PER_WORKER = 3

#: ``maintenance_work_mem`` used while the trigram indexes are rebuilt
#: (``VAULT_MAINTENANCE_WORK_MEM`` override, e.g. ``2GB`` on a big server).
DEFAULT_MAINTENANCE_WORK_MEM = "512MB"

_MEMORY_SETTING_RE = re.compile(r"^[0-9]+(?:kB|MB|GB|TB)?$", re.IGNORECASE)


def _cgroup_free_megabytes():
    """
    Free memory inside a container limit, or None when there is no limit.

    ``os.sysconf`` reports the memory of the whole machine, which is not the
    memory a container may use; the control groups in /sys tell the real
    limit.
    """
    for limit_file, usage_file in (
        ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes",
         "/sys/fs/cgroup/memory/memory.usage_in_bytes"),
    ):
        try:
            with open(limit_file, encoding="utf-8") as handle:
                limit = handle.read().strip()
            if not limit or limit == "max":
                continue
            limit_bytes = int(limit)
            with open(usage_file, encoding="utf-8") as handle:
                usage_bytes = int(handle.read().strip())
        except (OSError, TypeError, ValueError):
            continue
        # Limits that do not fit a signed 64-bit number mean "no limit".
        if limit_bytes <= 0 or limit_bytes >= (1 << 62):
            continue
        return max(0, (limit_bytes - usage_bytes) // (1024 * 1024))
    return None


def _host_free_megabytes():
    """Memory the machine can still hand out in MB, or None if unknown."""
    # MemAvailable counts page cache that can be reclaimed; the free page
    # count from sysconf does not, which would understate a busy server.
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, IndexError, ValueError):
        pass
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return (int(pages) * int(page_size)) // (1024 * 1024)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _free_megabytes():
    """Free memory in MB: the host's, inside a container limit if there is
    one, or None when the platform does not say."""
    host_free = _host_free_megabytes()
    container_free = _cgroup_free_megabytes()
    if host_free is None:
        return container_free
    if container_free is None:
        return host_free
    return min(host_free, container_free)


def stage_rows():
    """
    Rows per batch.

    ``VAULT_IMPORT_BATCH_ROWS`` wins. Otherwise the default is
    ``DEFAULT_STAGE_ROWS`` (250,000 rows, about 90 MB of buffered rows, and
    the fastest size measured end to end); it only shrinks when the machine
    or container has less than about 360 MB of memory left, so the buffer
    stays near a quarter of the free memory even there.
    """
    raw = os.environ.get("VAULT_IMPORT_BATCH_ROWS")
    if raw:
        try:
            return max(10000, int(raw))
        except (TypeError, ValueError):
            pass
    free_mb = _free_megabytes()
    if free_mb is None:
        return DEFAULT_STAGE_ROWS
    return max(10000, min(DEFAULT_STAGE_ROWS, free_mb * _ROWS_PER_FREE_MB))


def worker_count():
    """
    Parse workers to use (``VAULT_IMPORT_WORKERS`` override).

    An explicit override is honored on every platform: workers run in a
    fork pool where fork exists and in a spawn pool where it does not
    (Windows) -- see ``_parse_context``.  Without the override the
    default is up to 4 or the CPU count, whichever is smaller, on POSIX;
    other platforms default to 1 (inline parsing), because spawn pays a
    process-startup cost that only pays off when it is asked for.
    """
    try:
        value = int(os.environ.get("VAULT_IMPORT_WORKERS", 0))
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    if os.name != "posix":
        return 1
    return max(1, min(4, os.cpu_count() or 1))


def _parse_context():
    """
    multiprocessing context for the parse workers.

    ``fork`` is the cheapest start method and is used wherever POSIX
    provides it.  Windows has no fork context -- asking for one raises
    ``ValueError`` -- so the fallback is ``spawn``.  The workers only run
    ``_parse_chunk`` (a module-level function over plain str/bytes
    lines), so both contexts parse identically; spawn children re-import
    this module without touching the database, and the parent's main
    module is re-imported under multiprocessing's ``__mp_main__`` guard
    (``manage.py`` is guarded), so no recursive process creation.
    """
    import multiprocessing

    methods = multiprocessing.get_all_start_methods()
    for method in ("fork", "spawn"):
        if method in methods:
            return multiprocessing.get_context(method)
    return multiprocessing.get_context()


def _flush_batch(rows):
    """
    Store one batch of canonical (url, type, ident, secret, ip) tuples.

    Returns the number of new Account rows. The batch is copied into a temp
    table with COPY (the fastest bulk path PostgreSQL offers), then everything
    runs as set-based SQL inside one transaction.
    """
    if not rows:
        return 0
    now = timezone.now()
    with transaction.atomic(), connection.cursor() as cursor:
        # Bulk loads do not need to wait for the WAL flush of every batch; a
        # power loss can at worst lose the last batches, not corrupt data.
        cursor.execute("SET LOCAL synchronous_commit = off")
        cursor.execute(_STAGE_DDL)
        cursor.execute('TRUNCATE vault_import_stage')
        with cursor.copy(
            'COPY vault_import_stage (url, account_type, ident, secret, ip)'
            ' FROM STDIN'
        ) as copy:
            for row in rows:
                copy.write_row(row)
        # Fresh statistics so the planner joins the batch efficiently.
        cursor.execute('ANALYZE vault_import_stage')
        for _table, sql in _REFERENCE_INSERTS:
            cursor.execute(sql, (now, now))
        cursor.execute(_ACCOUNT_INSERT, (now, now))
        created = cursor.rowcount
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


def _row_from(record):
    """Canonical dict -> the 5-tuple staged in the temp table."""
    # parse_line reports a missing address as 'N/A'; store no relation.
    ip_value = record['ip_address']
    if not ip_value or ip_value == NO_IP:
        ip_value = None
    return (
        record['url'],
        record['account_type'],
        record['account_identifier'],
        record['secret_data'],
        ip_value,
    )


def _parse_chunk(lines):
    """
    Parse and canonicalize a chunk of raw lines.

    Runs inside a worker process when the session uses parse workers, and in
    the main process otherwise. Returns (rows, skipped).
    """
    rows = []
    skipped = 0
    for line in lines:
        record = canonical_record(parse_line(line))
        if record is None:
            skipped += 1
            continue
        rows.append(_row_from(record))
    return rows, skipped


class BulkImportSession:
    """
    One import job: accumulate lines, store them in large COPY batches.

    Raw lines handed to :meth:`add_lines` are parsed in worker processes
    (fork where available, spawn elsewhere) while the parent process copies
    and merges the previous batch, so parsing and database work overlap.
    Use it as a context manager or call :meth:`finish` exactly once.

    ``batch_rows`` and ``workers`` default to the environment-tunable values
    (``VAULT_IMPORT_BATCH_ROWS`` / ``VAULT_IMPORT_WORKERS``).
    """

    def __init__(self, batch_rows=None, workers=None):
        self._batch_rows = stage_rows() if batch_rows is None else batch_rows
        self._workers = worker_count() if workers is None else workers
        self._rows = []
        self._pool = None
        self.created = 0
        self.skipped = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def _parse_pool(self):
        if self._pool is None:
            self._pool = _parse_context().Pool(processes=self._workers)
        return self._pool

    def _absorb(self, rows, skipped):
        self.skipped += skipped
        self._rows.extend(rows)
        if len(self._rows) >= self._batch_rows:
            self.created += _flush_batch(self._rows)
            self._rows.clear()

    def add_parsed(self, parsed_iter):
        """
        Feed an iterable of already-parsed dicts (None for invalid lines) --
        the historical ``bulk_import_lines`` input. No parse workers are used.
        """
        rows = []
        skipped = 0
        for result in parsed_iter:
            record = canonical_record(result)
            if record is None:
                skipped += 1
                continue
            rows.append(_row_from(record))
        self._absorb(rows, skipped)

    def add_lines(self, lines):
        """
        Feed raw lines (str or bytes); they are parsed for you.

        With more than one worker, chunks of lines are parsed in worker
        processes while the parent stores earlier results, so parsing overlaps
        the database round-trips.
        """
        if self._workers <= 1:
            self.add_parsed(parse_line(line) for line in lines)
            return

        pool = self._parse_pool()
        chunk = []
        pending = []
        max_pending = self._workers * _MAX_PENDING_CHUNKS_PER_WORKER
        for line in lines:
            chunk.append(line)
            if len(chunk) >= _PARSE_CHUNK_LINES:
                pending.append(pool.apply_async(_parse_chunk, (chunk,)))
                chunk = []
                while len(pending) >= max_pending:
                    self._absorb(*pending.pop(0).get())
        if chunk:
            pending.append(pool.apply_async(_parse_chunk, (chunk,)))
        while pending:
            self._absorb(*pending.pop(0).get())

    def finish(self):
        """Store the final partial batch; returns (created, skipped)."""
        if self._rows:
            self.created += _flush_batch(self._rows)
            self._rows.clear()
        self.close()
        return self.created, self.skipped

    def close(self):
        if self._pool is not None:
            self._pool.close()
            self._pool.join()
            self._pool = None


def bulk_import_lines(parsed_iter):
    """
    Consume an iterable of parsed-line dicts (None for invalid lines) and
    store them in bounded batches.

    Returns (accounts_created, lines_skipped).
    """
    session = BulkImportSession()
    session.add_parsed(parsed_iter)
    return session.finish()


# ---- Fast mode: suspend the trigram search indexes during the load ----
#
# `ILIKE '%text%'` searches use these GIN indexes, and keeping them updated
# while millions of reference values stream in is the single largest import
# cost. Dropping them for the duration and building them once at the end is
# much faster; the statements match migration 0003_trigram_search_indexes.
# Exact (B-tree) lookups and every unique constraint stay in place, so
# deduplication is unaffected.

_TRIGRAM_INDEXES = (
    ("vault_url_name_trgm",
     "CREATE INDEX vault_url_name_trgm ON vault_url"
     " USING gin (UPPER(name::text) gin_trgm_ops)"),
    ("vault_email_name_trgm",
     "CREATE INDEX vault_email_name_trgm ON vault_email"
     " USING gin (UPPER(name::text) gin_trgm_ops)"),
    ("vault_user_name_trgm",
     "CREATE INDEX vault_user_name_trgm ON vault_user"
     " USING gin (UPPER(name::text) gin_trgm_ops)"),
    ("vault_pass_name_trgm",
     "CREATE INDEX vault_pass_name_trgm ON vault_pass"
     " USING gin (UPPER(name::text) gin_trgm_ops)"),
    ("vault_ip_name_trgm",
     "CREATE INDEX vault_ip_name_trgm ON vault_ip"
     " USING gin (UPPER(name::text) gin_trgm_ops)"),
)


def maintenance_work_mem():
    """Memory for the index rebuild, from the environment when it is valid."""
    value = os.environ.get("VAULT_MAINTENANCE_WORK_MEM", "")
    if value and _MEMORY_SETTING_RE.match(value.strip()):
        return value.strip()
    return DEFAULT_MAINTENANCE_WORK_MEM


def drop_search_indexes():
    """Drop the five trigram search indexes (no-op when already absent)."""
    with transaction.atomic(), connection.cursor() as cursor:
        for name, _ddl in _TRIGRAM_INDEXES:
            cursor.execute(f"DROP INDEX IF EXISTS {name}")


def ensure_search_indexes():
    """
    Create the five trigram search indexes when missing.

    ``maintenance_work_mem`` is raised for the build so large reference
    tables index quickly; the setting is local to this transaction.
    """
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('maintenance_work_mem', %s, true)",
            (maintenance_work_mem(),),
        )
        for _name, ddl in _TRIGRAM_INDEXES:
            cursor.execute(
                ddl.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
            )


@contextmanager
def search_indexes_suspended():
    """
    Drop the trigram search indexes for the duration of the block and always
    rebuild them afterwards -- including when the block raises.
    """
    drop_search_indexes()
    try:
        yield
    finally:
        ensure_search_indexes()


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


def import_path(target_path, ext="*", on_file=None, fast_mode=False):
    """
    Import all valid lines from a single file or recursively from every
    matching file inside a folder.

    With ``fast_mode`` the trigram search indexes are dropped for the
    duration and rebuilt once after the last file (also on failure).

    Returns (total_created, total_skipped, files_count).
    """
    files = collect_files_from_path(target_path, ext=ext)
    if not files:
        return 0, 0, 0

    if fast_mode:
        return _import_files(files, on_file=on_file)

    total_created = 0
    total_skipped = 0
    processed_files = 0

    for filepath in files:
        try:
            with open(filepath, "rb") as handle:
                created, skipped = _import_one_file_session(handle)
        except OSError:
            continue

        processed_files += 1
        total_created += created
        total_skipped += skipped
        if on_file is not None:
            on_file(filepath, created, skipped, total_created)

    return total_created, total_skipped, processed_files


def _import_one_file_session(handle):
    """
    Import one open binary file through a BulkImportSession.

    One session per file: created/skipped counts stay exact per file and each
    file commits on its own, so an interrupted folder import keeps the files
    already stored.
    """
    session = BulkImportSession()
    try:
        # Iterating the binary handle gives whole lines without end-of-line
        # translation, and parse_line decodes UTF-8 (ignoring errors) one
        # line at a time -- the same tolerance as the previous text-mode
        # errors='ignore' read. add_lines() chunks the stream for the parse
        # workers and stores in large COPY batches.
        session.add_lines(handle)
        return session.finish()
    finally:
        session.close()


def _import_files(files, on_file=None):
    """
    Import every file in one pass with the search indexes suspended.

    Fast mode for very large jobs: a fresh session per file keeps per-file
    counts exact, while the trigram indexes are rebuilt once at the end
    instead of being updated for every reference value.
    """
    total_created = 0
    total_skipped = 0
    processed_files = 0
    with search_indexes_suspended():
        for filepath in files:
            try:
                with open(filepath, "rb") as handle:
                    created, skipped = _import_one_file_session(handle)
            except OSError:
                continue

            processed_files += 1
            total_created += created
            total_skipped += skipped
            if on_file is not None:
                on_file(filepath, created, skipped, total_created)
    return total_created, total_skipped, processed_files
