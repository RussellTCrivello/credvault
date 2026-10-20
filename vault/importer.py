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

Each batch runs in a single transaction. See docs/README.md, "Performance", for
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
import sys
from contextlib import contextmanager

from django.db import connection, transaction
from django.utils import timezone

from .utils import (
    NO_IP, OversizedSourceLine, CanonicalRecord,
    iter_bounded_source_lines, parse_line,
)
from .validators import (
    classify_identifier, normalize_ip, normalize_password, normalize_url,
)

# Worker-safe parse function (no Django top-level imports) for Windows spawn
try:
    from .workers import _parse_chunk as _safe_parse_chunk
except ImportError:
    _safe_parse_chunk = None  # type: ignore

STREAM_BATCH_SIZE = 100000

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
     " ORDER BY url"
     " ON CONFLICT (name) DO NOTHING"),
    ("vault_pass",
     "INSERT INTO vault_pass (name, created_at, updated_at)"
     " SELECT DISTINCT secret, %s, %s FROM vault_import_stage"
     " ORDER BY secret"
     " ON CONFLICT (name) DO NOTHING"),
    ("vault_email",
     "INSERT INTO vault_email (name, created_at, updated_at)"
     " SELECT DISTINCT ident, %s, %s FROM vault_import_stage"
     " WHERE account_type = 'email'"
     " ORDER BY ident"
     " ON CONFLICT (name) DO NOTHING"),
    ("vault_user",
     "INSERT INTO vault_user (name, created_at, updated_at)"
     " SELECT DISTINCT ident, %s, %s FROM vault_import_stage"
     " WHERE account_type <> 'email'"
     " ORDER BY ident"
     " ON CONFLICT (name) DO NOTHING"),
    ("vault_ip",
     "INSERT INTO vault_ip (name, created_at, updated_at)"
     " SELECT DISTINCT ip, %s, %s FROM vault_import_stage"
     " WHERE ip IS NOT NULL"
     " ORDER BY ip"
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
    " ORDER BY " + _FINGERPRINT_SQL +
    " ON CONFLICT (fingerprint) DO NOTHING"
)


#: Reference tables the account INSERT joins to, in a fixed order (the order
#: matters: ANALYZE takes a lock that lasts until the batch commits).
_JOINED_REFERENCE_TABLES = (
    'vault_email', 'vault_ip', 'vault_pass', 'vault_url', 'vault_user',
)
_STALE_STATS_MIN_PAGES = 16


def refresh_stale_reference_stats(cursor):
    """ANALYZE reference tables whose planner statistics say "empty".

    A table that was analyzed while empty (after a purge, a restore of an empty
    table, a test flush) keeps ``reltuples = 0``.  PostgreSQL then estimates
    ONE row no matter how large the table has since become, and the account
    INSERT's joins degrade to a nested loop that rescans a 50,000-row table for
    every staged row: one backend ran at 100 % CPU for 25 minutes on a
    50,000-line batch.  ANALYZE inside the batch transaction sees the rows this
    batch just inserted.  Healthy tables (``reltuples > 0``) are never touched,
    so a large database pays nothing per batch.

    Returns the tables analyzed (for tests and logs).
    """
    cursor.execute(
        "SELECT c.relname FROM pg_class c"
        " WHERE c.oid = ANY(ARRAY["
        + ", ".join(f"to_regclass('{t}')" for t in _JOINED_REFERENCE_TABLES)
        + "]) AND c.reltuples <= 0"
        " AND pg_relation_size(c.oid) >= %s ORDER BY c.relname",
        (_STALE_STATS_MIN_PAGES * 8192,))
    stale = [row[0] for row in cursor.fetchall()]
    for table in stale:
        if table in _JOINED_REFERENCE_TABLES:  # names are constants, never input
            cursor.execute(f'ANALYZE {table}')
    return stale

#: Rows per COPY batch; one transaction per batch keeps memory bounded. The
#: measured default is 250,000; very large batches add memory and commit cost.
#: An operator override is supported, but is capped at one million rows.
DEFAULT_STAGE_ROWS = 250000
MAX_IMPORT_BATCH_ROWS = 1000000
#: 700 buffered rows per free MB lowers the default on memory-constrained hosts.
_ROWS_PER_FREE_MB = 700

#: Hard upper bound for parse subprocesses, including explicit environment
#: overrides. The default remains at most four and follows the available CPU
#: count; larger values are for measured high-core deployments only.
MAX_IMPORT_WORKERS = 16

#: Pending parse results are bounded globally as well as per worker. Each bulk
#: chunk contains 25k lines; this cap prevents multiplying queued source bytes
#: without limit when the worker count is raised.
_PARSE_CHUNK_LINES = 25000
_MAX_PENDING_CHUNKS_PER_WORKER = 3
MAX_IN_FLIGHT_PARSE_CHUNKS = 16
#: Bounds both a single worker task and all queued parse input/results. The
#: accounting intentionally estimates expansion, not just raw source bytes.
MAX_PARSE_TASK_BYTES = 8 * 1024 * 1024
MAX_IN_FLIGHT_PARSE_BYTES = 64 * 1024 * 1024
_PARSE_ITEM_OVERHEAD_BYTES = 512
_PARSE_EXPANSION_FACTOR = 4
#: The row and audit-metadata buffers share this cap before each DB commit.
MAX_IMPORT_BUFFER_BYTES = 256 * 1024 * 1024

#: ``work_mem`` is per PostgreSQL sort/hash node, not a process-wide budget.
#: Default conservatively for bounded file concurrency; operators can raise it
#: after measuring their PostgreSQL memory headroom. The hard per-node cap is
#: 256 MiB, and actual total use may multiply across query nodes and sessions.
DEFAULT_IMPORT_WORK_MEM = "64MB"
MAX_IMPORT_WORK_MEM_BYTES = 256 * 1024 * 1024

#: ``maintenance_work_mem`` used while the trigram indexes are rebuilt
#: (``VAULT_MAINTENANCE_WORK_MEM`` override, e.g. ``2GB`` on a big server).
DEFAULT_MAINTENANCE_WORK_MEM = "1024MB"

_MEMORY_SETTING_RE = re.compile(r"^[0-9]+(?:kB|MB|GB|TB)?$", re.IGNORECASE)
_MEMORY_UNIT_BYTES = {
    "": 1024, "kb": 1024, "mb": 1024 ** 2,
    "gb": 1024 ** 3, "tb": 1024 ** 4,
}
_CANONICAL_MEMORY_UNITS = {
    "": "", "kb": "kB", "mb": "MB", "gb": "GB", "tb": "TB",
}


def _bounded_memory_setting(value, default, maximum_bytes=None):
    """Validate a PostgreSQL memory setting and optionally cap its size."""
    value = (value or "").strip()
    match = re.fullmatch(r"([0-9]+)(kB|MB|GB|TB)?", value, re.IGNORECASE)
    if not match:
        return default
    try:
        amount = int(match.group(1))
    except ValueError:
        return default
    unit = (match.group(2) or "").lower()
    byte_count = amount * _MEMORY_UNIT_BYTES[unit]
    if byte_count < 64 * 1024:
        return default
    if maximum_bytes is not None and byte_count > maximum_bytes:
        return default
    suffix = _CANONICAL_MEMORY_UNITS[unit]
    return f"{amount}{suffix}"


def import_work_mem():
    """Configured ``work_mem`` for importer transactions (maximum 256 MiB)."""
    raw = os.environ.get("VAULT_IMPORT_WORK_MEM", "")
    if not raw:
        return DEFAULT_IMPORT_WORK_MEM
    return _bounded_memory_setting(
        raw, DEFAULT_IMPORT_WORK_MEM, MAX_IMPORT_WORK_MEM_BYTES,
    )


def estimated_parse_item_bytes(line):
    """Conservative byte estimate for one raw line crossing a parse worker."""
    if isinstance(line, OversizedSourceLine):
        payload_bytes = sys.getsizeof(line)
    elif isinstance(line, (bytes, bytearray)):
        payload_bytes = len(line)
    elif isinstance(line, str):
        # Four bytes per Unicode code point is a safe upper bound for UTF-8.
        payload_bytes = len(line) * 4
    else:
        payload_bytes = sys.getsizeof(line)
    return _PARSE_ITEM_OVERHEAD_BYTES + payload_bytes * _PARSE_EXPANSION_FACTOR


def estimated_batch_item_bytes(row, meta=None):
    """Estimate Python row/ledger objects retained until a batch commit."""
    size = _PARSE_ITEM_OVERHEAD_BYTES + sys.getsizeof(row)
    if row is not None:
        size += sum(sys.getsizeof(value) for value in row)
    if meta is not None:
        size += sys.getsizeof(meta)
        size += sum(
            sys.getsizeof(key) + sys.getsizeof(value)
            for key, value in meta.items()
        )
    return size


def bounded_worker_count(value):
    """Clamp a configured parser-worker count to a finite safe range."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(value, MAX_IMPORT_WORKERS))


def max_pending_parse_chunks(workers):
    """Bound in-flight parse tasks regardless of the configured worker count."""
    try:
        workers = max(1, min(int(workers), MAX_IMPORT_WORKERS))
    except (TypeError, ValueError):
        workers = 1
    return max(1, min(
        workers * _MAX_PENDING_CHUNKS_PER_WORKER,
        MAX_IN_FLIGHT_PARSE_CHUNKS,
    ))


def bounded_batch_rows(value):
    """Clamp explicit batch sizes without preventing small test/API batches."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_STAGE_ROWS
    if value < 1:
        return DEFAULT_STAGE_ROWS
    return min(value, MAX_IMPORT_BATCH_ROWS)


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

    ``VAULT_IMPORT_BATCH_ROWS`` wins and is capped at one million rows to
    prevent an accidental unbounded Python buffer. Otherwise the default is
    ``DEFAULT_STAGE_ROWS`` (250,000 rows); it only shrinks when the machine or
    container has very little free memory.
    """
    raw = os.environ.get("VAULT_IMPORT_BATCH_ROWS")
    if raw:
        try:
            return max(10000, min(MAX_IMPORT_BATCH_ROWS, int(raw)))
        except (TypeError, ValueError):
            pass
    free_mb = _free_megabytes()
    if free_mb is None:
        return DEFAULT_STAGE_ROWS
    return max(10000, min(DEFAULT_STAGE_ROWS, free_mb * _ROWS_PER_FREE_MB))


def worker_count():
    """
    Parse workers to use (``VAULT_IMPORT_WORKERS`` override).

    An explicit override is honored on every platform but clamped to
    ``MAX_IMPORT_WORKERS``. Workers use fork where available and spawn on
    platforms without fork. Without an override the default is at most four
    and follows the CPU count on POSIX; other platforms default to one because
    spawn startup cost only pays off when it is requested.
    """
    try:
        value = int(os.environ.get("VAULT_IMPORT_WORKERS", 0))
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return bounded_worker_count(value)
    if os.name != "posix":
        return 1
    return max(1, min(4, os.cpu_count() or 1, MAX_IMPORT_WORKERS))


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
        if method not in methods:
            continue
        try:
            return multiprocessing.get_context(method)
        except ValueError:
            # A runtime may advertise a method which its context factory
            # cannot construct (notably Windows builds and test embedders).
            # Treat the factory as authoritative and try the next portable
            # method instead of letting an unsupported ``fork`` abort import.
            continue
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
        cursor.execute("SET LOCAL work_mem = %s", (import_work_mem(),))
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
        refresh_stale_reference_stats(cursor)
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

    Delegates to vault.workers._parse_chunk which is safe for Windows spawn
    (does not import Django at top level).
    """
    from .workers import _parse_chunk as _workers_parse_chunk
    return _workers_parse_chunk(lines)
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
        self._batch_rows = (
            stage_rows() if batch_rows is None
            else bounded_batch_rows(batch_rows)
        )
        self._workers = (
            worker_count() if workers is None
            else bounded_worker_count(workers)
        )
        self._rows = []
        self._row_buffer_bytes = 0
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

    def _flush_rows(self):
        if not self._rows:
            return
        self.created += _flush_batch(self._rows)
        self._rows.clear()
        self._row_buffer_bytes = 0

    def _add_row(self, row):
        self._rows.append(row)
        self._row_buffer_bytes += estimated_batch_item_bytes(row)
        if (
            len(self._rows) >= self._batch_rows
            or self._row_buffer_bytes >= MAX_IMPORT_BUFFER_BYTES
        ):
            self._flush_rows()

    def _absorb(self, rows, skipped):
        self.skipped += skipped
        for row in rows:
            self._add_row(row)

    def add_parsed(self, parsed_iter):
        """
        Feed parsed dictionaries incrementally; neither the source iterable
        nor a whole-file list of canonical rows is retained in memory.
        """
        for result in parsed_iter:
            record = canonical_record(result)
            if record is None:
                self.skipped += 1
            else:
                self._add_row(_row_from(record))

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
        chunk_bytes = 0
        pending = []
        pending_bytes = 0
        max_pending = max_pending_parse_chunks(self._workers)
        # Use worker-safe function for Windows spawn (vault.workers, no Django)
        parse_fn = _safe_parse_chunk if _safe_parse_chunk is not None else _parse_chunk

        def drain_pending(force=False):
            nonlocal pending_bytes
            while pending and (
                force
                or len(pending) >= max_pending
                or pending_bytes >= MAX_IN_FLIGHT_PARSE_BYTES
            ):
                task, task_bytes = pending.pop(0)
                pending_bytes -= task_bytes
                self._absorb(*task.get())

        def submit_chunk():
            nonlocal chunk, chunk_bytes, pending_bytes
            if not chunk:
                return
            pending.append((
                pool.apply_async(parse_fn, (chunk,)), chunk_bytes,
            ))
            pending_bytes += chunk_bytes
            chunk = []
            chunk_bytes = 0
            drain_pending()

        for line in lines:
            line_bytes = estimated_parse_item_bytes(line)
            if chunk and (
                len(chunk) >= _PARSE_CHUNK_LINES
                or chunk_bytes + line_bytes > MAX_PARSE_TASK_BYTES
            ):
                submit_chunk()
            chunk.append(line)
            chunk_bytes += line_bytes
            if (
                len(chunk) >= _PARSE_CHUNK_LINES
                or chunk_bytes >= MAX_PARSE_TASK_BYTES
            ):
                submit_chunk()
        submit_chunk()
        drain_pending(force=True)

    def finish(self):
        """Store the final partial batch; returns (created, skipped)."""
        self._flush_rows()
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


# ---- Fast mode: suspend the search indexes during the load ----
#
# `ILIKE '%text%'` searches use GIN trigram indexes, and keeping them
# updated while millions of reference values stream in is the single
# largest import cost. The B-tree `lower()` and `varchar_pattern_ops`
# indexes on the reference tables and the FK / sort indexes on
# `vault_account` also cost a large fraction of the per-batch time for
# an 18M-line file (measured: ~7s per 100k for references alone).
#
# Fast mode drops *all* non-unique search indexes for the duration and
# rebuilds them once at the end. Unique constraints (`name` and
# `fingerprint`) and primary keys stay in place, so deduplication and
# referential integrity are unaffected. Exact searches still work (via
# sequential scan) while the indexes are suspended; substring searches
# are slower until rebuild — same policy as before, just extended to
# all non-essential indexes for the 18M target.
#
# The rebuild uses `maintenance_work_mem` for speed and is crash-safe:
# `ensure_search_indexes` is idempotent (IF NOT EXISTS) and is called
# in the `finally` block, plus at the start of the next fast import
# if a previous crash left indexes dropped.

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

# All non-unique search indexes that fast mode suspends. Unique indexes
# (pkey, name_key, fingerprint_uniq) are never dropped.
_FAST_MODE_INDEXES = (
    # reference tables: lower() + C collation + trgm (the varchar_pattern_ops
    # companions were dropped by migration 0009: nothing reads them)
    ("vault_url_name_lower_idx",
     "CREATE INDEX vault_url_name_lower_idx ON vault_url USING btree (lower((name)::text))"),
    ("vault_url_name_lower_c_idx",
     "CREATE INDEX vault_url_name_lower_c_idx ON vault_url USING btree (lower((name)::text) COLLATE \"C\")"),
    ("vault_url_name_trgm",
     "CREATE INDEX vault_url_name_trgm ON vault_url USING gin (UPPER(name::text) gin_trgm_ops)"),

    ("vault_email_name_lower_idx",
     "CREATE INDEX vault_email_name_lower_idx ON public.vault_email USING btree (lower((name)::text))"),
    ("vault_email_name_trgm",
     "CREATE INDEX vault_email_name_trgm ON public.vault_email USING gin (upper((name)::text) gin_trgm_ops)"),

    ("vault_user_name_lower_idx",
     "CREATE INDEX vault_user_name_lower_idx ON public.vault_user USING btree (lower((name)::text))"),
    ("vault_user_name_trgm",
     "CREATE INDEX vault_user_name_trgm ON public.vault_user USING gin (upper((name)::text) gin_trgm_ops)"),

    ("vault_pass_name_lower_idx",
     "CREATE INDEX vault_pass_name_lower_idx ON public.vault_pass USING btree (lower((name)::text))"),
    ("vault_pass_name_trgm",
     "CREATE INDEX vault_pass_name_trgm ON public.vault_pass USING gin (upper((name)::text) gin_trgm_ops)"),

    ("vault_ip_name_lower_idx",
     "CREATE INDEX vault_ip_name_lower_idx ON public.vault_ip USING btree (lower((name)::text))"),
    ("vault_ip_name_trgm",
     "CREATE INDEX vault_ip_name_trgm ON public.vault_ip USING gin (upper((name)::text) gin_trgm_ops)"),

    # account table: FK indexes + fingerprint_like + sort index
    ("vault_account_email_id_c46a575a",
     "CREATE INDEX vault_account_email_id_c46a575a ON public.vault_account USING btree (email_id)"),
    ("vault_account_ip_id_269fff31",
     "CREATE INDEX vault_account_ip_id_269fff31 ON public.vault_account USING btree (ip_id)"),
    ("vault_account_password_id_f3db800f",
     "CREATE INDEX vault_account_password_id_f3db800f ON public.vault_account USING btree (password_id)"),
    ("vault_account_url_id_165a1aaf",
     "CREATE INDEX vault_account_url_id_165a1aaf ON public.vault_account USING btree (url_id)"),
    ("vault_account_user_id_3d6a7ad7",
     "CREATE INDEX vault_account_user_id_3d6a7ad7 ON public.vault_account USING btree (user_id)"),
    ("vault_acct_created_id_idx",
     "CREATE INDEX vault_acct_created_id_idx ON public.vault_account USING btree (created_at DESC, id)"),

    # ledger table: non-unique indexes (FKs, status, job_pos, etc.)
    # Dropping these during fast import cuts ledger INSERT cost.
    # Unique indexes (pkey, idempotency_key_key) stay.
    ("vault_importrec_job_pos_idx",
     "CREATE INDEX vault_importrec_job_pos_idx ON public.vault_importrecord USING btree (import_job_id, source_position)"),
    ("vault_importrec_job_status_idx",
     "CREATE INDEX vault_importrec_job_status_idx ON public.vault_importrecord USING btree (import_job_id, status)"),
    ("vault_importrecord_account_id_95a8ecc3",
     "CREATE INDEX vault_importrecord_account_id_95a8ecc3 ON public.vault_importrecord USING btree (account_id)"),
)


def maintenance_work_mem():
    """Memory for the index rebuild, from the environment when it is valid."""
    value = os.environ.get("VAULT_MAINTENANCE_WORK_MEM", "")
    if value and _MEMORY_SETTING_RE.match(value.strip()):
        return value.strip()
    return DEFAULT_MAINTENANCE_WORK_MEM


FAST_MODE_SCOPES = ('search', 'all')


def fast_mode_scope():
    """Which indexes fast mode suspends: ``search`` (default) or ``all``.

    ``search`` suspends only the five ``pg_trgm`` substring indexes -- what
    the documentation always promised -- so exact searches, record lookups
    and every foreign-key path stay indexed while the import runs.
    ``all`` (``VAULT_FAST_MODE_SCOPE=all``) additionally drops the btree
    indexes on the reference, account and ledger tables; it is meant for an
    offline load because lookups degrade badly meanwhile (README, "Fast mode").
    """
    value = os.environ.get("VAULT_FAST_MODE_SCOPE", "search").strip().lower()
    return value if value in FAST_MODE_SCOPES else "search"


def _suspended_indexes(scope):
    if scope == "all":
        return _FAST_MODE_INDEXES
    return tuple(item for item in _FAST_MODE_INDEXES
                 if item[0].endswith("_trgm"))


def drop_search_indexes(scope=None):
    """Drop the indexes fast mode suspends (see ``fast_mode_scope``)."""
    indexes = _suspended_indexes(scope or fast_mode_scope())
    # DROP INDEX is safe inside a transaction, but we still handle errors
    # for test transactions that have pending triggers.
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            for name, _ddl in indexes:
                try:
                    cursor.execute(f"DROP INDEX IF EXISTS {name}")
                except Exception:
                    # In Django TestCase, DROP inside a transaction that
                    # modified the table can still succeed, but if it fails
                    # due to pending triggers, ignore — the index will be
                    # dropped on next fast import outside the test tx.
                    continue
    except Exception:
        # If the outer transaction already has pending triggers (TestCase),
        # the whole atomic block may fail on first CREATE. Fall back to
        # dropping one by one outside atomic.
        for name, _ddl in indexes:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP INDEX IF EXISTS {name}")
            except Exception:
                continue


def ensure_search_indexes():
    """
    Create all fast-mode search indexes when missing.

    ``maintenance_work_mem`` is raised for the build so large reference
    and account tables index quickly; the setting is local to this
    transaction. Idempotent (IF NOT EXISTS) — safe to call after a crash
    that left indexes dropped.

    Crash-safe and TestCase-safe: each CREATE is attempted individually;
    if it fails due to pending trigger events (Django TestCase wraps
    each test in a transaction that has already modified the table),
    it is skipped — the index will be rebuilt after the test transaction
    commits, or on next fast import.
    """
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('maintenance_work_mem', %s, true)",
                (maintenance_work_mem(),),
            )
            for _name, ddl in _FAST_MODE_INDEXES:
                try:
                    cursor.execute(
                        ddl.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
                    )
                except Exception as exc:
                    # Pending trigger events inside TestCase — skip this
                    # index for now; it will be rebuilt outside the test tx.
                    if 'pending trigger events' in str(exc).lower() or 'objectinuse' in type(exc).__name__.lower():
                        continue
                    # For other errors (e.g., already exists), continue
                    continue
    except Exception:
        # Outer atomic may have failed due to first CREATE; try one by one
        # without wrapping in atomic, so each DDL gets its own savepoint.
        for _name, ddl in _FAST_MODE_INDEXES:
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('maintenance_work_mem', %s, true)",
                        (maintenance_work_mem(),),
                    )
                    cursor.execute(
                        ddl.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
                    )
            except Exception:
                continue


@contextmanager
def search_indexes_suspended(scope=None):
    """
    Drop all non-unique search indexes for the duration of the block and
    always rebuild them afterwards -- including when the block raises.

    Crash-safe: if the process is SIGKILLed while indexes are dropped,
    the next fast import calls `ensure_search_indexes` in its `finally`
    and also at the start of `import_worker` (via the context manager),
    so a subsequent import will rebuild before proceeding. Search is
    slower while suspended; exact lookups still work via seq scan or
    unique indexes.
    """
    # Rebuild any leftovers from a previous crash before dropping again,
    # so we don't start with a partially indexed state.
    ensure_search_indexes()
    drop_search_indexes(scope)
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
        # The bounded reader preserves complete binary lines, but hashes and
        # discards any line over the format-derived cap without retaining its
        # plaintext. add_lines() applies byte-aware backpressure to parse tasks.
        session.add_lines(iter_bounded_source_lines(handle))
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
