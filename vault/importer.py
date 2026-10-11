"""
High-throughput bulk importer for the normalized vault.

A naive "one ``get_or_create`` per line" import does ~7 queries per record,
which is hopeless for millions of lines. This module works in *batches*
instead, and keeps memory flat:

  1. Read a fixed-size block of lines (streaming; files are never loaded).
  2. Collect the distinct values of that block.
  3. Bulk-insert the missing reference rows with ``INSERT ... ON CONFLICT
     DO NOTHING`` semantics (``ignore_conflicts``), then read their ids back.
  4. Build the relationship rows, deduplicate them in memory, and bulk-insert
     them. The UNIQUE index on the relationship makes duplicates against
     *already stored* rows disappear in the database, so nothing has to be
     loaded into memory to check for them.
  5. Repeat.

The consequence is that importing the same folder twice is safe: every value
is reused and no duplicate record is created.

It backs the web "import a file / a whole folder" flow and the
``importfolder`` management command, which is the recommended path for very
large volumes.
"""

import os
from collections import namedtuple
from itertools import islice

from django.db import connection, transaction
from django.utils import timezone

from .models import Account, Email, Ip, Pass, Url, User
from .utils import (
    TEXT_EXTENSIONS, extract_domain, is_importable_name, looks_like_text,
    parse_line, iter_decoded_lines,
)

# Lines held in memory per batch. Values are capped by the model's max
# lengths, so a batch stays a few MB even with very long URLs.
DEFAULT_BATCH_SIZE = 10_000

# Stay comfortably below SQLite's historical 999-variable limit while keeping
# the number of round-trips low.
IN_CHUNK = 900
BULK_BATCH = 450

# A single normalized input line. ``host`` is left empty here on purpose: it
# is derived once per *distinct* URL while the batch is being written, not
# once per line.
Row = namedtuple(
    'Row', 'url host account_type identifier secret ip ip_value'
)


class ImportStats(tuple):
    """
    Result of an import.

    Behaves like the ``(created, skipped)`` tuple callers traditionally
    unpack, while also carrying the extra counters the bulk flows report.
    """

    def __new__(cls, created, skipped, *, lines=0, prepared=0, files=0):
        stats = super().__new__(cls, (created, skipped))
        stats.lines = lines
        stats.prepared = prepared
        stats.files = files
        return stats

    @property
    def created(self):
        return self[0]

    @property
    def skipped(self):
        return self[1]

    @property
    def reused(self):
        """Valid lines that did not become new rows (values already stored)."""
        return max(self.prepared - self.created, 0)

    def merge(self, other):
        return ImportStats(
            self.created + other.created,
            self.skipped + other.skipped,
            lines=self.lines + other.lines,
            prepared=self.prepared + other.prepared,
            files=self.files + other.files,
        )

    def __repr__(self):
        return (f'ImportStats(created={self.created}, skipped={self.skipped}, '
                f'lines={self.lines}, prepared={self.prepared}, '
                f'files={self.files})')


def _chunks(iterable, size):
    it = iter(iterable)
    while True:
        batch = list(islice(it, size))
        if not batch:
            return
        yield batch


def _normalize(parsed):
    """
    Turn a ``parse_line`` result into a :class:`Row`.

    Returns ``None`` when the requirements are not met: a URL, an identifier
    and a secret are mandatory, and every value must fit its column.
    """
    if isinstance(parsed, Row):
        return parsed
    if isinstance(parsed, (str, bytes)):
        parsed = parse_line(parsed)
    if not parsed:
        return None

    url = (parsed.get('url') or '').strip()
    secret = parsed.get('secret_data') or ''
    identifier = (parsed.get('account_identifier') or '').strip()
    account_type = parsed.get('account_type') or 'user'
    ip_value = (parsed.get('ip_address') or '').strip()

    if not (url and secret and identifier):
        return None
    if len(url) > 2048 or len(identifier) > 255 or len(secret) > 500:
        return None

    ip = None
    if ip_value and ip_value.upper() != 'N/A' and len(ip_value) <= 45:
        # Anything that cannot be an address (a stray column, a fragment of
        # a malformed line) is dropped rather than rejecting the record.
        if all(c in '0123456789.:%abcdefABCDEF-' for c in ip_value):
            ip = ip_value

    return Row(
        url=url,
        host='',
        account_type='email' if account_type == 'email' else 'user',
        identifier=identifier,
        secret=secret,
        ip=ip,
        ip_value=ip_value or 'N/A',
    )


def _insert_ignoring_duplicates(model, columns, rows):
    """
    INSERT a batch of tuples, skipping the ones that violate a UNIQUE index.

    ``QuerySet.bulk_create(ignore_conflicts=True)`` is the portable way to say
    this, but it builds a model instance per row and recompiles the statement
    per batch, which costs more than the insert itself once a file reaches
    millions of lines. Feeding ``executemany`` a prepared statement is roughly
    five times faster on the same data, so it is the default whenever the
    backend can express "ignore the conflict" in one statement. Anything else
    falls back to the ORM.
    """
    if not rows:
        return 0

    vendor = connection.vendor
    table = model._meta.db_table
    quoted = ', '.join(
        connection.ops.quote_name(column) for column in columns
    )
    placeholders = ', '.join(['?'] * len(columns)) if vendor == 'sqlite' \
        else ', '.join('%s' for _ in columns)

    if vendor == 'sqlite':
        sql = (f'INSERT OR IGNORE INTO {connection.ops.quote_name(table)} '
               f'({quoted}) VALUES ({placeholders})')
    elif vendor == 'postgresql':
        sql = (f'INSERT INTO {connection.ops.quote_name(table)} ({quoted}) '
               f'VALUES ({placeholders}) ON CONFLICT DO NOTHING')
    else:
        sql = None

    if sql is None:
        instances = [model(**dict(zip(columns, row))) for row in rows]
        model.objects.bulk_create(
            instances, batch_size=BULK_BATCH, ignore_conflicts=True
        )
        return len(instances)

    with connection.cursor() as cursor:
        cursor.executemany(sql, rows)
    return len(rows)


def _timestamp():
    """A single "now" for a whole batch, adapted for the active backend."""
    now = timezone.now()
    adapt = connection.ops.adapt_datetimefield_value
    return adapt(now), adapt(now)


def _get_or_create_ids(model, names, extra_fields=None):
    """
    Return ``{name: pk}`` for every name, creating the missing rows.

    ``extra_fields`` maps a name to additional column values, used to fill
    in the derived ``host`` of a URL row.
    """
    mapping = {}
    name_list = list(names)
    if not name_list:
        return mapping

    for chunk in _chunks(name_list, IN_CHUNK):
        mapping.update(
            model.objects.filter(name__in=chunk)
            .values_list('name', 'id')
        )

    missing = [n for n in name_list if n not in mapping]
    if missing:
        created_at, updated_at = _timestamp()
        columns = ['name', 'created_at', 'updated_at']
        extra_keys = []
        if extra_fields:
            probe = extra_fields(missing[0]) or {}
            extra_keys = [key for key in probe if key != 'name']
            columns += extra_keys
        rows = []
        seen_names = set()
        for name in missing:
            if name in seen_names:
                continue
            seen_names.add(name)
            row = [name, created_at, updated_at]
            if extra_keys:
                extra = extra_fields(name) or {}
                row += [extra.get(key) for key in extra_keys]
            rows.append(tuple(row))
        _insert_ignoring_duplicates(model, columns, rows)
        # Re-read only the names we just inserted to pick up their ids.
        for chunk in _chunks(missing, IN_CHUNK):
            found = model.objects.filter(name__in=chunk).values_list(
                'name', 'id'
            )
            mapping.update(dict(found))

    return mapping


def _import_rows(rows):
    """
    Store one batch of normalized rows. Returns how many were prepared.

    Duplicates -- both within the batch and against rows already in the
    database -- are dropped: in memory for speed, by the UNIQUE index for
    correctness.
    """
    if not rows:
        return 0

    url_hosts = {}
    emails, users, secrets, ips = set(), set(), set(), set()
    for row in rows:
        url_hosts.setdefault(row.url, None)
        if row.account_type == 'email':
            emails.add(row.identifier)
        else:
            users.add(row.identifier)
        secrets.add(row.secret)
        if row.ip:
            ips.add(row.ip)

    with transaction.atomic():
        url_map = _get_or_create_ids(
            Url, url_hosts.keys(),
            extra_fields=lambda name: {'host': extract_domain(name)[:255]},
        )
        email_map = _get_or_create_ids(Email, emails)
        user_map = _get_or_create_ids(User, users)
        pass_map = _get_or_create_ids(Pass, secrets)
        ip_map = _get_or_create_ids(Ip, ips)

        created_at, updated_at = _timestamp()
        seen = set()
        pending = []
        for row in rows:
            url_id = url_map.get(row.url)
            password_id = pass_map.get(row.secret)
            if not url_id or not password_id:
                continue  # reference vanished; nothing sane to insert
            email_id = email_map.get(row.identifier) \
                if row.account_type == 'email' else None
            user_id = user_map.get(row.identifier) \
                if row.account_type == 'user' else None
            ip_id = ip_map.get(row.ip) if row.ip else None
            key = (
                url_id, row.account_type, email_id or 0, user_id or 0,
                password_id, ip_id or 0,
            )
            if key in seen:
                continue
            seen.add(key)
            pending.append((
                url_id, row.account_type, email_id, user_id, password_id,
                ip_id, created_at, updated_at,
            ))

        _insert_ignoring_duplicates(
            Account,
            ('url_id', 'account_type', 'email_id', 'user_id', 'password_id',
             'ip_id', 'created_at', 'updated_at'),
            pending,
        )

    return len(pending)


def _count_accounts():
    return Account.objects.count()


def bulk_import_rows(parsed_rows, batch_size=DEFAULT_BATCH_SIZE,
                     measure=True):
    """
    Import an iterable of ``parse_line`` dicts (raw text lines also work).

    Returns an :class:`ImportStats`: ``created`` is the exact number of new
    records, ``skipped`` the number of lines that were not usable records,
    and ``prepared`` the number of distinct relationships seen (so
    ``prepared - created`` is how many were already stored and got reused).
    """
    lines = prepared = skipped = 0
    batch = []

    if measure:
        before = _count_accounts()

    for parsed in parsed_rows:
        lines += 1
        row = parsed if isinstance(parsed, Row) else _normalize(parsed)
        if row is None:
            skipped += 1
            continue
        batch.append(row)
        if len(batch) >= batch_size:
            prepared += _import_rows(batch)
            batch = []
    if batch:
        prepared += _import_rows(batch)

    created = (_count_accounts() - before) if measure else prepared

    return ImportStats(
        created, skipped, lines=lines, prepared=prepared
    )


def bulk_import_lines(lines, batch_size=DEFAULT_BATCH_SIZE, measure=True):
    """Import an iterable of raw text lines (decoded lazily, streamed)."""
    return bulk_import_rows(
        (parse_line(line) for line in lines),
        batch_size=batch_size,
        measure=measure,
    )


def _meaningful_lines(handle):
    """Non-blank text lines of a binary stream (comments stay, they are counted)."""
    for line in iter_decoded_lines(handle):
        if line.strip():
            yield line


def import_text_stream(handle, batch_size=DEFAULT_BATCH_SIZE,
                       measure=True):
    """Import every line of a binary text stream (e.g. an upload)."""
    return bulk_import_lines(
        _meaningful_lines(handle),
        batch_size=batch_size,
        measure=measure,
    )


def import_text_file(path, batch_size=DEFAULT_BATCH_SIZE, measure=True):
    """Import every line of a file on disk."""
    with open(path, 'rb') as handle:
        return import_text_stream(
            handle, batch_size=batch_size, measure=measure
        )


def collect_files(paths, extensions=TEXT_EXTENSIONS, allow_any=False):
    """
    Expand folders and files into a sorted list of importable file paths.

    Folders are walked recursively; only text-looking files are kept unless
    ``allow_any`` is set.
    """
    found = []
    for path in paths:
        if os.path.isfile(path):
            found.append(path)
            continue
        if not os.path.isdir(path):
            continue
        for root, _dirs, filenames in os.walk(path):
            for filename in filenames:
                if not is_importable_name(filename, extensions, allow_any):
                    continue
                candidate = os.path.join(root, filename)
                found.append(candidate)
    return sorted(set(found))


def scan_lines(lines, max_examples=5):
    """
    Read-only dry run: count what *would* be stored, touch nothing.

    Returns ``(lines, valid, invalid, examples)`` where ``examples`` holds a
    handful of rejected lines, which is what you actually want to see when a
    file "imported nothing".
    """
    total = valid = invalid = 0
    examples = []
    for line in lines:
        total += 1
        if _normalize(parse_line(line)) is None:
            invalid += 1
            if len(examples) < max_examples:
                examples.append(line.strip()[:160])
        else:
            valid += 1
    return total, valid, invalid, examples


def scan_file(path, max_examples=5):
    with open(path, 'rb') as handle:
        return scan_lines(
            (line for line in iter_decoded_lines(handle) if line.strip()),
            max_examples=max_examples,
        )


def import_paths(paths, extensions=TEXT_EXTENSIONS, allow_any=False,
                 on_file=None, batch_size=DEFAULT_BATCH_SIZE):
    """
    Import every line of every file found in ``paths``.

    One measurement is taken for the whole run, so scanning thousands of
    files does not turn into thousands of ``COUNT(*)`` queries.
    """
    files = collect_files(paths, extensions, allow_any)
    before = _count_accounts()
    total = ImportStats(0, 0, files=len(files))

    for filepath in files:
        try:
            with open(filepath, 'rb') as handle:
                head = handle.read(1024)
                handle.seek(0)
                if not allow_any and not looks_like_text(head):
                    stats = ImportStats(0, 0)
                else:
                    stats = import_text_stream(
                        handle, batch_size=batch_size, measure=False
                    )
        except OSError as error:
            if on_file:
                on_file(filepath, ImportStats(0, 0), error)
            continue

        total = total.merge(stats)
        if on_file:
            on_file(filepath, stats, None)

    created = _count_accounts() - before
    return ImportStats(
        created, total.skipped, lines=total.lines,
        prepared=total.prepared, files=len(files),
    )


def import_streams(named_handles, on_file=None,
                   batch_size=DEFAULT_BATCH_SIZE):
    """
    Import a list of ``(label, binary stream)`` pairs (browser uploads).

    A single measurement covers the whole submission.
    """
    before = _count_accounts()
    total = ImportStats(0, 0, files=len(named_handles))

    for label, handle in named_handles:
        try:
            stats = import_text_stream(
                handle, batch_size=batch_size, measure=False
            )
        except OSError as error:
            if on_file:
                on_file(label, ImportStats(0, 0), error)
            continue

        total = total.merge(stats)
        if on_file:
            on_file(label, stats, None)

    created = _count_accounts() - before
    return ImportStats(
        created, total.skipped, lines=total.lines,
        prepared=total.prepared, files=len(named_handles),
    )
