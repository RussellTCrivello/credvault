"""
High-throughput importer for the normalized vault.

A line of an imported file goes through four steps:

  1. Parse and validate it (``vault.utils.parse_line``, ``vault.validators``)
     and keep only the canonical spelling of each value. A line that breaks a
     format is counted as skipped instead of being stored half-parsed. The
     result is one row tuple ``(url, account_type, identifier, secret, ip)``.
  2. Parse in parallel worker processes when the import profile asks for it.
     Parsing is pure Python and CPU-bound, so this is the only step that
     scales with cores on the Python side.
  3. Write the rows in batches. Two writers exist:

     * the ORM writer (``bulk_import_rows``), used on SQLite and by the
       Balanced profile: it collects the distinct reference values of a batch,
       looks up the ones already stored, bulk-creates the rest, then creates the
       records that are not stored yet;
     * the COPY writer (``vault.fastimport.copy_import_rows``), used on
       PostgreSQL by the Fast and Maximum profiles: the batch is streamed with
       ``COPY`` into a staging table and written with set-based statements that
       deduplicate on the database side.

  4. Deduplicate. Reference values are unique by their canonical spelling, and
     each record is unique by its fingerprint, so a value or a record that is
     already stored is reused and never inserted again.

``import_sources`` drives a whole import (files or uploads) batch by batch and
yields progress events, which the web import streams to the progress card and
the management command prints.
"""

import os
import time
from collections import deque, namedtuple
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from itertools import islice

from django.db import DEFAULT_DB_ALIAS, transaction

from .db import analyze_tables, is_postgresql
from .models import Account, Email, Ip, Pass, Url, User, record_fingerprint
from .performance import import_profile
from .utils import NO_IP, CanonicalRecord, parse_line
from .validators import (
    classify_identifier, normalize_ip, normalize_password, normalize_url,
)

# Chunk IN(...) queries to stay well below SQLite's 999-variable limit.
_CHUNK = 500
_BATCH = 2000
STREAM_BATCH_SIZE = 50000
#: Lines handed to one parse worker at a time.
PARSE_CHUNK = 5000

_BINARY_EXTS = {
    ".sqlite3", ".db", ".pyc", ".pyo", ".mo", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".ico", ".pdf", ".zip", ".tar", ".gz", ".7z", ".exe",
    ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".eot",
}

#: A file (or upload) to read: kind is 'path' or 'upload'.
Source = namedtuple('Source', 'kind ref name size line_count')


def _chunks(iterable, size):
    it = iter(iterable)
    while True:
        batch = list(islice(it, size))
        if not batch:
            return
        yield batch


def _bulk_get_or_create_names(model, names):
    """
    Return a {name: id} map for every canonical name, creating missing rows.

    The names arriving here are already canonical, so the lookup is an exact
    match on the unique `name` column: a value that is already stored is
    reused, never inserted a second time.
    """
    name_set = set(names)
    mapping = {}
    if not name_set:
        return mapping

    for chunk in _chunks(name_set, _CHUNK):
        for name, pk in model.objects.filter(
            name__in=chunk
        ).values_list('name', 'id'):
            mapping[name] = pk

    missing_names = [name for name in name_set if name not in mapping]
    if missing_names:
        missing_objs = [model(name=name) for name in missing_names]
        model.objects.bulk_create(
            missing_objs, batch_size=_BATCH, ignore_conflicts=True
        )
        # Re-read only the newly created names to pick up their IDs.
        for chunk in _chunks(missing_names, _CHUNK):
            for name, pk in model.objects.filter(
                name__in=chunk
            ).values_list('name', 'id'):
                mapping[name] = pk

    return mapping


@transaction.atomic
def _flush_batch(keys, urls, emails, users, passes, ips):
    """Persist one batch of deduplicated account keys and return created count."""
    if not keys:
        return 0

    url_map = _bulk_get_or_create_names(Url, urls)
    email_map = _bulk_get_or_create_names(Email, emails)
    user_map = _bulk_get_or_create_names(User, users)
    pass_map = _bulk_get_or_create_names(Pass, passes)
    ip_map = _bulk_get_or_create_names(Ip, ips)

    # One Account per fingerprint: the same URL + identifier + password + IP
    # combination appears once in the batch no matter how many lines carried
    # it, and the unique fingerprint column keeps that true across batches,
    # files and imports.
    candidates = {}
    for (url, account_type, ident, secret, ip_value) in keys:
        url_id = url_map.get(url)
        pass_id = pass_map.get(secret)
        if not (url_id and pass_id):
            continue

        if account_type == 'email':
            email_id = email_map.get(ident)
            user_id = None
            if not email_id:
                continue
        else:
            email_id = None
            user_id = user_map.get(ident)
            if not user_id:
                continue

        ip_id = ip_map.get(ip_value) if ip_value else None
        fingerprint = record_fingerprint(
            url_id, account_type, email_id, user_id, pass_id, ip_id
        )
        if fingerprint in candidates:
            continue
        candidates[fingerprint] = Account(
            url_id=url_id,
            account_type=account_type,
            email_id=email_id,
            user_id=user_id,
            password_id=pass_id,
            ip_id=ip_id,
            fingerprint=fingerprint,
        )

    # Drop the records already stored -- one indexed lookup per chunk.
    already_stored = set()
    for chunk in _chunks(list(candidates), _CHUNK):
        already_stored.update(
            Account.objects.filter(
                fingerprint__in=chunk
            ).values_list('fingerprint', flat=True)
        )

    objs = [
        account for fingerprint, account in candidates.items()
        if fingerprint not in already_stored
    ]
    if objs:
        Account.objects.bulk_create(
            objs, batch_size=_BATCH, ignore_conflicts=True
        )

    return len(objs)


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


def row_from_record(result):
    """
    One parsed record as the row tuple every writer consumes:
    ``(url, account_type, identifier, secret, ip)``, with ``ip`` None when
    the record has no address. Returns None for an invalid record.
    """
    record = canonical_record(result)
    if record is None:
        return None
    ip_value = record['ip_address']
    if not ip_value or ip_value == NO_IP:
        ip_value = None
    return (
        record['url'], record['account_type'],
        record['account_identifier'], record['secret_data'], ip_value,
    )


def _parse_chunk(lines):
    """Parse a list of raw lines into row tuples (runs in worker processes)."""
    return [row_from_record(parse_line(line)) for line in lines]


def iter_rows(lines, pool=None):
    """
    Yield one row tuple (or None for an invalid line) per input line, in order.

    With a worker ``pool`` the lines are parsed in chunks by several processes.
    The number of chunks in flight is bounded, so a huge input is never held in
    memory at once.
    """
    if pool is None:
        for line in lines:
            yield row_from_record(parse_line(line))
        return

    window = max(2, getattr(pool, 'max_workers', 2) * 2)
    pending = deque()
    for chunk in _chunks(lines, PARSE_CHUNK):
        pending.append(pool.submit(_parse_chunk, chunk))
        while len(pending) > window:
            yield from pending.popleft().result()
    while pending:
        yield from pending.popleft().result()


def bulk_import_rows(rows):
    """
    Store row tuples with the ORM writer, in bounded batches.

    ``rows`` yields row tuples or None (an invalid line).
    Returns ``(accounts_created, lines_skipped)``.
    """
    urls = set()
    emails = set()
    users = set()
    passes = set()
    ips = set()
    keys = set()

    total_created = 0
    skipped = 0

    for row in rows:
        if row is None:
            skipped += 1
            continue

        url, account_type, ident, secret, ip_value = row
        urls.add(url)
        passes.add(secret)
        if account_type == 'email':
            emails.add(ident)
        else:
            users.add(ident)
        if ip_value:
            ips.add(ip_value)

        keys.add(row)

        if len(keys) >= STREAM_BATCH_SIZE:
            total_created += _flush_batch(
                keys, urls, emails, users, passes, ips
            )
            urls.clear()
            emails.clear()
            users.clear()
            passes.clear()
            ips.clear()
            keys.clear()

    if keys:
        total_created += _flush_batch(
            keys, urls, emails, users, passes, ips
        )

    return total_created, skipped


def bulk_import_lines(parsed_iter):
    """
    Consume an iterable of parsed-line dicts (None for invalid lines) and
    store them efficiently in bounded streaming batches.

    Returns (accounts_created, lines_skipped).
    """
    return bulk_import_rows(row_from_record(result) for result in parsed_iter)


def store_lines(lines, profile=None, pool=None, using=None):
    """
    Parse and store one batch of raw lines with the given (or saved) profile.

    Returns ``(accounts_created, lines_skipped)``.
    """
    profile = profile or import_profile()
    rows = iter_rows(lines, pool)
    if profile.use_copy and is_postgresql(using):
        from .fastimport import copy_import_rows
        return copy_import_rows(
            rows, using=using or DEFAULT_DB_ALIAS,
            fast_commit=profile.fast_commit, work_mem=profile.work_mem,
        )
    return bulk_import_rows(rows)


@contextmanager
def worker_pool(profile):
    """A process pool for parsing, or None when the profile parses in-process."""
    if profile.parse_workers <= 1:
        yield None
        return
    with ProcessPoolExecutor(max_workers=profile.parse_workers) as pool:
        yield pool


def _batched(lines, size):
    batch = []
    for line in lines:
        batch.append(line)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


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


def count_lines(source):
    """Exact number of lines in a source (a path or an uploaded file)."""
    if source.kind == 'upload':
        source.ref.seek(0)
        total = sum(1 for _line in source.ref)
        source.ref.seek(0)
        return total
    with open(source.ref, 'r', encoding='utf-8', errors='ignore') as handle:
        return sum(1 for _line in handle)


def path_source(filepath):
    return Source('path', filepath, filepath, os.path.getsize(filepath), None)


def upload_source(uploaded_file):
    return Source(
        'upload', uploaded_file, uploaded_file.name,
        int(getattr(uploaded_file, 'size', 0) or 0), None,
    )


def _open_source(source):
    if source.kind == 'upload':
        source.ref.seek(0)
        return source.ref, False
    return open(source.ref, 'r', encoding='utf-8', errors='ignore'), True


def _bytes_done(handle, source):
    """Bytes consumed so far from a source (approximate for text files)."""
    if source.kind == 'upload':
        try:
            return handle.tell()
        except (OSError, ValueError):
            return 0
    try:
        return handle.buffer.tell()
    except (OSError, ValueError, AttributeError):
        return 0


def import_sources(sources, profile=None, using=None):
    """
    Import every source batch by batch, yielding progress events:

    * ``{'type': 'batch', ...}`` after each batch of lines is stored,
    * ``{'type': 'file', ...}`` after each source is finished (it carries the
      same cumulative fields as a batch event, plus ``file_created`` and
      ``file_skipped`` for that source),
    * ``{'type': 'error', 'reason': 'unreadable' | 'changed', ...}`` when a
      source cannot be read or changed while it was being imported. The
      import moves on to the next source; a consumer that wants to stop breaks
      out of the loop,
    * ``{'type': 'done', ...}`` with the totals as the last event.
    """
    profile = profile or import_profile()
    started = time.perf_counter()
    total_bytes = sum(source.size for source in sources)
    counted = all(source.line_count is not None for source in sources)
    total_lines = (
        sum(source.line_count for source in sources) if counted else None
    )

    created_total = 0
    skipped_total = 0
    lines_done = 0
    bytes_base = 0

    with worker_pool(profile) as pool:
        for index, source in enumerate(sources, start=1):
            try:
                handle, close_handle = _open_source(source)
            except OSError:
                yield {'type': 'error', 'reason': 'unreadable',
                       'name': source.name}
                continue  # the consumer may stop here by breaking out

            file_lines = 0
            file_created = 0
            file_skipped = 0
            try:
                for batch in _batched(handle, profile.batch_size):
                    created, skipped = store_lines(
                        batch, profile, pool, using=using,
                    )
                    created_total += created
                    skipped_total += skipped
                    file_created += created
                    file_skipped += skipped
                    file_lines += len(batch)
                    lines_done += len(batch)
                    bytes_done = bytes_base + _bytes_done(handle, source)
                    elapsed = time.perf_counter() - started
                    yield {
                        'type': 'batch',
                        'lines_done': lines_done,
                        'lines_total': total_lines,
                        'bytes_done': min(bytes_done, total_bytes),
                        'bytes_total': total_bytes,
                        'file_index': index,
                        'total_files': len(sources),
                        'created': created_total,
                        'skipped': skipped_total,
                        'elapsed': elapsed,
                        'rate': lines_done / elapsed if elapsed else 0.0,
                    }
            finally:
                if close_handle:
                    handle.close()

            if source.line_count is not None and file_lines != source.line_count:
                yield {'type': 'error', 'reason': 'changed',
                       'name': source.name}
                continue

            bytes_base += source.size
            elapsed = time.perf_counter() - started
            yield {
                'type': 'file',
                'name': source.name,
                'ref': source.ref,
                'index': index,
                'file_index': index,
                'total_files': len(sources),
                'lines': file_lines,
                'file_created': file_created,
                'file_skipped': file_skipped,
                'created': created_total,
                'skipped': skipped_total,
                'lines_done': lines_done,
                'lines_total': total_lines,
                'bytes_done': min(bytes_base, total_bytes),
                'bytes_total': total_bytes,
                'elapsed': elapsed,
                'rate': lines_done / elapsed if elapsed else 0.0,
            }

    if profile.use_copy and is_postgresql(using) and lines_done:
        analyze_tables(
            (Url, Email, User, Pass, Ip, Account), using=using,
        )

    elapsed = time.perf_counter() - started
    yield {
        'type': 'done',
        'created': created_total,
        'skipped': skipped_total,
        'files': len(sources),
        'lines': lines_done,
        'elapsed': elapsed,
        'rate': lines_done / elapsed if elapsed else 0.0,
        'mode': profile.key,
    }


def import_path(target_path, ext="*", on_file=None, profile=None):
    """
    Import all valid lines from a single file or recursively from every
    matching file inside a folder.

    Returns (total_created, total_skipped, files_count).
    """
    profile = profile or import_profile()
    files = collect_files_from_path(target_path, ext=ext)
    if not files:
        return 0, 0, 0

    sources = []
    for filepath in files:
        try:
            sources.append(path_source(filepath))
        except OSError:
            continue

    if profile.count_lines:
        sources = [
            source._replace(line_count=count_lines(source)) for source in sources
        ]

    total_created = 0
    total_skipped = 0
    processed_files = 0
    for event in import_sources(sources, profile):
        if event['type'] == 'file':
            processed_files += 1
            if on_file is not None:
                on_file(event['ref'], event['file_created'],
                        event['file_skipped'], event['created'])
        elif event['type'] == 'error':
            # Unreadable or changed files are skipped, as before.
            continue
        elif event['type'] == 'done':
            total_created = event['created']
            total_skipped = event['skipped']
    return total_created, total_skipped, processed_files
