"""
High-throughput bulk importer for the normalized vault, with live progress.

The previous single-file import created one row at a time with get_or_create,
which is far too slow for millions of lines. This module batches the work:

  1. Stream the input in bounded batches so memory stays flat even across
     millions of lines and thousands of files.
  2. Collect the *distinct* referenced values (URLs, emails, usernames,
     passwords, IPs) seen in each batch.
  3. Bulk-create the missing reference rows (chunked to stay under the
     SQLite variable limit).
  4. Build the Account rows in memory, deduplicate against only the
     matching URL/password subset in the database, and bulk-create them.

It is used by the web file/folder import flows and by the importfolder
management command.

Progress reporting
------------------
Every file is read through :func:`iter_lines`, which walks the input in fixed
256 KiB chunks and reports each chunk to an optional *tracker*. Because the
tracker sees real byte counts against the known size of every file, the
workspace can show a percentage that is measured rather than estimated -- for
one file or for a folder tree of ten thousand.

A tracker is any object exposing this small protocol (see
:class:`~vault.import_jobs.ImportJob`); :class:`NullTracker` is the no-op
default so callers that do not care about progress pay nothing:

    file_bytes(count)          # bytes consumed from the current file
    note(**counts)             # line/record deltas since the last report
    stopped() -> bool          # cancel request, checked every 16k lines

Counting is deliberately granular: a line is only ever one of
``blank`` (empty or comment), ``invalid`` (not a record), ``duplicate``
(a record that already exists) or ``created`` (a new record), so the four
numbers always add up to the number of lines read.
"""

import os
from itertools import islice

from django.db import transaction

from .models import Account, Email, Ip, Pass, Url, User
from .utils import parse_line

# Chunk IN(...) queries to stay well below SQLite's 999-variable limit.
_CHUNK = 500
_BATCH = 2000

# Distinct keys buffered before a database flush. Small enough that the live
# counters tick several times a second on fast disks, large enough that the
# bulk path stays the fastest one available.
_STREAM_BATCH = 10000

# Bytes per read. Progress is reported per chunk, so the bar stays smooth even
# inside a single huge file, while the read size stays cache friendly.
READ_CHUNK = 256 * 1024

# A file without a single newline must not grow the read buffer without bound.
MAX_LINE_BYTES = 4 * 1024 * 1024

# Look for a cancel request every 2**14 lines: negligible overhead, and the
# import stops within a blink of the user pressing "Stop".
_CANCEL_MASK = 0x3FFF

_BINARY_EXTS = {
    ".sqlite3", ".db", ".pyc", ".pyo", ".mo", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".ico", ".pdf", ".zip", ".tar", ".gz", ".7z", ".exe",
    ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".eot",
}


class ImportCancelled(Exception):
    """Raised inside the import loop when the job asked to be stopped."""


class ImportStats:
    """
    Outcome of one import -- a single file, a stream, or a whole folder.

    Unpacking as ``(created, skipped)`` keeps the older call sites (and the
    ``importfolder`` command) working unchanged; new code reads the named
    counters instead.
    """

    __slots__ = (
        'lines', 'blank', 'invalid', 'valid', 'duplicates', 'created',
        'skipped', 'flushes', 'cancelled',
    )

    def __init__(self, lines=0, blank=0, invalid=0, valid=0, duplicates=0,
                 created=0, flushes=0, cancelled=False):
        self.lines = lines
        self.blank = blank
        self.invalid = invalid
        self.valid = valid
        self.duplicates = duplicates
        self.created = created
        self.flushes = flushes
        # True when the run stopped early on request: the counters below are
        # then a partial result, and the caller must not present them as a
        # complete pass over the file.
        self.cancelled = cancelled
        # Legacy alias kept for the CLI report and the form messages:
        # every line that did not become a new record on its own merits.
        self.skipped = blank + invalid

    def merge(self, other):
        for field in ('lines', 'blank', 'invalid', 'valid', 'duplicates',
                      'created', 'flushes'):
            setattr(self, field, getattr(self, field) + getattr(other, field))
        self.cancelled = self.cancelled or other.cancelled
        self.skipped = self.blank + self.invalid
        return self

    @property
    def malformed(self):
        """Lines that looked like data but were not a usable record."""
        return self.invalid

    def as_dict(self):
        return {
            'lines': self.lines,
            'blank': self.blank,
            'invalid': self.invalid,
            'valid': self.valid,
            'duplicates': self.duplicates,
            'created': self.created,
            'skipped': self.skipped,
            'cancelled': self.cancelled,
        }

    # Backwards compatibility: `created, skipped = stats`.
    def __iter__(self):
        yield self.created
        yield self.skipped

    def __repr__(self):
        return (f"ImportStats(created={self.created}, "
                f"duplicates={self.duplicates}, invalid={self.invalid}, "
                f"blank={self.blank}, lines={self.lines})")


class NullTracker:
    """No-op tracker for callers that do not want live progress."""

    __slots__ = ()

    def file_bytes(self, count):
        return None

    def note(self, **counts):
        return None

    def file_error(self, path, message):
        return None

    def stopped(self):
        return False


NULL_TRACKER = NullTracker()


def _tracker(obj):
    return NULL_TRACKER if obj is None else obj


def _chunks(iterable, size):
    it = iter(iterable)
    while True:
        batch = list(islice(it, size))
        if not batch:
            return
        yield batch


def _bulk_get_or_create_names(model, names):
    """Return a {name: id} map for every name, creating missing rows."""
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
    """
    Persist one batch of deduplicated account keys.

    Returns ``(created, duplicates)`` where *duplicates* counts the records
    that were perfectly valid but already stored -- a distinction the progress
    indicator needs, because "nothing new" and "malformed input" are very
    different messages for the user.
    """
    if not keys:
        return 0, 0

    url_map = _bulk_get_or_create_names(Url, urls)
    email_map = _bulk_get_or_create_names(Email, emails)
    user_map = _bulk_get_or_create_names(User, users)
    pass_map = _bulk_get_or_create_names(Pass, passes)
    ip_map = _bulk_get_or_create_names(Ip, ips)

    url_ids = list(url_map.values())
    pass_ids = list(pass_map.values())

    # Query existing accounts only for the URLs (or passwords) in this batch,
    # never scanning the entire Account table.
    existing = set()
    if len(url_ids) <= len(pass_ids):
        for chunk in _chunks(url_ids, _CHUNK):
            existing.update(
                Account.objects.filter(url_id__in=chunk).values_list(
                    'url_id', 'account_type', 'email_id', 'user_id',
                    'password_id', 'ip_id'
                )
            )
    else:
        for chunk in _chunks(pass_ids, _CHUNK):
            existing.update(
                Account.objects.filter(password_id__in=chunk).values_list(
                    'url_id', 'account_type', 'email_id', 'user_id',
                    'password_id', 'ip_id'
                )
            )

    objs = []
    duplicates = 0
    for (url, account_type, ident, secret, ip_key) in keys:
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

        ip_id = ip_map.get(ip_key) if ip_key else None
        row_key = (url_id, account_type, email_id, user_id, pass_id, ip_id)
        if row_key in existing:
            duplicates += 1
            continue

        existing.add(row_key)
        objs.append(Account(
            url_id=url_id,
            account_type=account_type,
            email_id=email_id,
            user_id=user_id,
            password_id=pass_id,
            ip_id=ip_id,
        ))

    if objs:
        Account.objects.bulk_create(
            objs, batch_size=_BATCH, ignore_conflicts=True
        )

    return len(objs), duplicates


def bulk_import_lines(parsed_iter, tracker=None, blank_tally=None, stats=None):
    """
    Consume an iterable of parsed-line dicts (None for invalid lines) and
    store them efficiently in bounded streaming batches.

    ``blank_tally`` is an optional ``{'blank': int}`` dict owned by a caller
    that classifies empty/comment lines upstream; reading it live keeps the
    returned :class:`ImportStats` a complete, non-overlapping breakdown of
    every line that was read.

    Progress is pushed to ``tracker.note()`` as *deltas* since the previous
    report, which happens at most once per batch -- the per-line loop never
    takes a lock or calls into anything.

    Returns an :class:`ImportStats`, which unpacks as
    ``(created, skipped)`` for older call sites.
    """
    tracker = _tracker(tracker)

    urls = set()
    emails = set()
    users = set()
    passes = set()
    ips = set()
    keys = set()

    stats = stats or ImportStats()

    lines = 0
    invalid = 0
    valid = 0
    created = 0
    duplicates = 0
    flushes = 0
    sent = {
        'lines': 0, 'blank': 0, 'invalid': 0, 'valid': 0,
        'created': 0, 'duplicate': 0,
    }

    def current_blank():
        return blank_tally.get('blank', 0) if blank_tally else 0

    def publish():
        """Copy the running totals into *stats* (idempotent)."""
        blank = current_blank()
        stats.lines = lines + blank
        stats.blank = blank
        stats.invalid = invalid
        stats.valid = valid
        stats.created = created
        stats.duplicates = duplicates
        stats.flushes = flushes
        stats.skipped = stats.blank + invalid

    def report():
        blank = current_blank()
        latest = {
            'lines': lines + blank,
            'blank': blank,
            'invalid': invalid,
            'valid': valid,
            'created': created,
            'duplicate': duplicates,
        }
        delta = {
            key: value - sent[key]
            for key, value in latest.items() if value != sent[key]
        }
        if delta:
            sent.update(latest)
            tracker.note(**delta)

    index = 0
    for result in parsed_iter:
        index += 1
        lines += 1

        # Periodic progress, checked at the top of the loop so it happens for
        # every kind of line (including the ones the branches below skip).
        # The cost is one modulo per line; the report itself is rate limited
        # inside report(), and cancellation is checked here rather than in a
        # signal or a thread because a plain loop check is both cheaper and
        # the only kind that cannot misfire between two statements.
        if index % _CHUNK == 0:
            report()
            if tracker.stopped():
                # Stop, but not at a cost: the lines already read are written
                # before the exception leaves, so cancelling an import never
                # throws away finished work.
                if keys:
                    made, dups = _flush_batch(
                        keys, urls, emails, users, passes, ips
                    )
                    created += made
                    duplicates += dups
                    flushes += 1
                    urls.clear()
                    emails.clear()
                    users.clear()
                    passes.clear()
                    ips.clear()
                    keys.clear()
                stats.cancelled = True
                publish()
                report()
                raise ImportCancelled()

        if not result:
            invalid += 1
        else:
            url = (result.get('url') or '').strip()
            secret = result.get('secret_data') or ''
            ident = (result.get('account_identifier') or '').strip()
            account_type = result.get('account_type') or 'user'
            ip_value = (result.get('ip_address') or '').strip()

            if not (url and secret and ident):
                invalid += 1
            elif len(url) > 2048 or len(ident) > 255 or len(secret) > 500:
                invalid += 1
            else:
                valid += 1
                ip_key = None
                if ip_value and ip_value.upper() != 'N/A' and len(ip_value) <= 45:
                    ip_key = ip_value

                key = (url, account_type, ident, secret, ip_key)
                if key in keys:
                    # The same record twice in one batch: the second copy is a
                    # duplicate, and counting it here is what keeps
                    # lines == blank + invalid + valid and
                    # valid == created + duplicates true while reading.
                    duplicates += 1
                    continue

                keys.add(key)
                urls.add(url)
                passes.add(secret)
                if account_type == 'email':
                    emails.add(ident)
                else:
                    users.add(ident)
                if ip_key:
                    ips.add(ip_key)

        if lines & _CANCEL_MASK == 0 and tracker.stopped():
            report()
            raise ImportCancelled()

        if len(keys) >= _STREAM_BATCH:
            made, dups = _flush_batch(
                keys, urls, emails, users, passes, ips
            )
            created += made
            duplicates += dups
            flushes += 1
            urls.clear()
            emails.clear()
            users.clear()
            passes.clear()
            ips.clear()
            keys.clear()
            report()

    if keys:
        made, dups = _flush_batch(keys, urls, emails, users, passes, ips)
        created += made
        duplicates += dups
        flushes += 1

    publish()
    report()
    return stats


def _is_blank_or_comment(raw_line):
    """True for empty lines and ``#`` comments, which are skipped silently."""
    if isinstance(raw_line, (bytes, bytearray)):
        stripped = bytes(raw_line).strip()
        return not stripped or stripped.startswith(b'#')
    if not isinstance(raw_line, str):
        return False
    stripped = raw_line.strip()
    return not stripped or stripped.startswith('#')


def iter_lines(stream, tracker=None, chunk_size=READ_CHUNK):
    """
    Yield decoded text lines from a *binary* stream, reporting every byte to
    ``tracker.file_bytes()``.

    Chunked reading is what makes progress accurate: the byte count advances
    continuously while the file is being digested instead of only at the end,
    and a multi-gigabyte file never has to be seeked through twice.
    """
    tracker = _tracker(tracker)
    buffer = b''
    position = 0

    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        position += len(chunk)
        tracker.file_bytes(len(chunk))
        buffer += chunk

        while True:
            newline = buffer.find(b'\n')
            if newline == -1:
                break
            raw, buffer = buffer[:newline], buffer[newline + 1:]
            yield raw.decode('utf-8', errors='ignore')

        if len(buffer) > MAX_LINE_BYTES:
            # No newline in sight: flush what we have rather than ballooning.
            raw, buffer = buffer[:MAX_LINE_BYTES], buffer[MAX_LINE_BYTES:]
            yield raw.decode('utf-8', errors='ignore')

    if buffer:
        yield buffer.decode('utf-8', errors='ignore')


def import_lines(lines_iter, tracker=None):
    """
    Parse and store raw text lines (``str`` or ``bytes``) from any iterable.

    Blank and ``#`` comment lines are counted separately from malformed lines,
    so the progress panel can tell the user "3 empty lines ignored" instead of
    "3 lines failed".
    """
    tracker = _tracker(tracker)
    tally = {'blank': 0}

    def parsed_iter():
        for raw in lines_iter:
            if _is_blank_or_comment(raw):
                tally['blank'] += 1
                continue
            yield parse_line(raw)

    return bulk_import_lines(
        parsed_iter(), tracker=tracker, blank_tally=tally
    )


def import_stream(stream, tracker=None):
    """Import every valid record from a binary file-like object."""
    return import_lines(iter_lines(stream, tracker=tracker), tracker=tracker)


def file_size(path):
    """Size of *path* in bytes, or ``None`` when it cannot be determined."""
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _is_candidate_file(filename, ext="*"):
    lower = filename.lower()
    _, file_ext = os.path.splitext(lower)
    if file_ext in _BINARY_EXTS:
        return False
    if ext == "*" or not ext:
        return True
    normalized_ext = ext.lower().lstrip(".")
    return lower.endswith(f".{normalized_ext}")


def is_candidate_file(filename, ext="*"):
    """
    True when *filename* is worth reading.

    Known binary extensions are never offered for import, so a folder upload
    that happens to include images or archives does not stall the progress bar
    on data that cannot contain records.
    """
    return _is_candidate_file(filename, ext=ext)


def collect_files_from_path(target_path, ext="*", on_found=None,
                            report_every=200, stats=None):
    """
    Return a sorted list of readable text files from a file or directory path.

    ``on_found(count, total_bytes)`` is called while walking so a caller can
    show "found 4,318 files / 2.1 GB" for very large trees, where the walk
    itself is slow enough to be worth reporting. ``stats`` is an optional dict
    that receives ``{'skipped': n}``: how many files were seen and ignored,
    which the import screen shows so "2 of 9 files" is not a mystery.
    """
    def note(count, total, skipped):
        if stats is not None:
            stats['skipped'] = skipped
        if on_found is not None:
            on_found(count, total)

    if os.path.isfile(target_path):
        size = file_size(target_path)
        note(1, size or 0, 0)
        return [target_path]
    if not os.path.isdir(target_path):
        return []

    matched = []
    skipped = 0
    total_bytes = 0
    for root, dirs, filenames in os.walk(target_path):
        # Skip hidden or system directories
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".") and d != "__pycache__"
        )
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            if not _is_candidate_file(filename, ext=ext):
                skipped += 1
                continue
            path = os.path.join(root, filename)
            matched.append(path)
            size = file_size(path)
            if size:
                total_bytes += size
            if len(matched) % report_every == 0:
                note(len(matched), total_bytes, skipped)

    if matched or skipped:
        note(len(matched), total_bytes, skipped)
    return matched


def scan_path(target_path, ext="*", on_found=None, stats=None):
    """
    Return ``(files, total_bytes)`` for a file or folder path.

    Used by the import screen as a dry run: it tells the user exactly how many
    files and how many bytes the import is about to read, which is also what
    makes the resulting percentage meaningful.
    """
    files = collect_files_from_path(
        target_path, ext=ext, on_found=on_found, stats=stats
    )
    total_bytes = 0
    for path in files:
        size = file_size(path)
        if size:
            total_bytes += size
    return files, total_bytes


def import_path(target_path, ext="*", on_file=None, tracker=None,
                files=None, on_start=None):
    """
    Import all valid lines from a single file or recursively from every
    matching file inside a folder.

    ``on_file`` is called as ``on_file(filepath, stats, total_created,
    index, files_total)``; the first four arguments match the previous
    signature, so the ``importfolder`` report keeps working. ``on_start`` is
    called as ``on_start(filepath, index, files_total)`` before a file is read,
    which is what lets a progress line name the file it is on instead of the
    one it finished.

    Returns ``(total_created, total_skipped, files_count)``.
    """
    if files is None:
        files = collect_files_from_path(target_path, ext=ext)
    if not files:
        return 0, 0, 0

    totals = ImportStats()
    processed_files = 0

    for index, filepath in enumerate(files, start=1):
        stats = ImportStats()
        error = None
        if on_start is not None:
            on_start(filepath, index, len(files))
        try:
            with open(filepath, "rb") as handle:
                stats = import_stream(handle, tracker=tracker)
        except OSError as exc:
            error = str(exc)
            stats = ImportStats()

        totals.merge(stats)
        processed_files += 1
        if on_file is not None:
            on_file(filepath, stats, totals.created, index, len(files))
        if error and tracker is not None:
            tracker.file_error(filepath, error)

    return totals.created, totals.skipped, processed_files
